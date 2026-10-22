#!/usr/bin/env python3
"""trilium2outline.py - Trilium Markdown subtree export -> Outline import ZIP.

Handles the three things that break a naive export:
  * asset references   -> flat "uploads/<name>" (Outline matches textually)
  * <img class/style>  -> Markdown images or clean <img>
  * HTML <table>       -> GFM pipe tables (Outline drops raw HTML blocks)

    python trilium2outline.py --src trilium-export --collection "My Docs"
    python trilium2outline.py --src trilium-export --collection "My Docs" --apply

Flags:
    --apply             write files + build the zip (default is a dry run)
    --keep-html-img     keep <img> tags instead of converting to Markdown
    --keep-dims         keep width/height attributes on <img>
    --keep-html-tables  leave <table> HTML alone (Outline will likely drop it)
    --no-slug           keep original asset filenames (UNSAFE for Outline)
"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import shutil
import unicodedata
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote

MD_EXT = {".md", ".markdown"}

# scheme:, //host, or #fragment -> leave alone
EXTERNAL_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:|//|#)", re.I)

IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I | re.S)
IMG_SRC_RE = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
IMG_ALT_RE = re.compile(r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
ATTR_RE = re.compile(r"""([A-Za-z_:][-\w:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")

# inline links / images:  ![alt](target "title")
LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^)]*)(\))")
# reference definitions:  [label]: target "title"
REFDEF_RE = re.compile(r"^([ \t]{0,3}\[[^\]\n]+\]:[ \t]*)(<[^>]*>|\S+)(.*)$", re.M)
# HTML tables, non-greedy
TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.I | re.S)
# fenced code blocks, so we never rewrite code samples
FENCE_RE = re.compile(r"^([ \t]{0,3})(`{3,}|~{3,})[^\n]*\n.*?^[ \t]{0,3}\2[ \t]*$",
                        re.M | re.S)

TITLE_RE = re.compile(r'^(.*?)\s+"([^"]*)"$', re.S)
DROP_ATTRS = {"class", "style", "loading", "decoding", "srcset", "sizes", "data-src"}


# --------------------------------------------------------------------------
# HTML table -> GFM pipe table
# --------------------------------------------------------------------------
class _Cell:
    __slots__ = ("text", "header", "colspan", "rowspan", "align")

    def __init__(self, header: bool, colspan: int, rowspan: int, align: str):
        self.text = ""
        self.header = header
        self.colspan = colspan
        self.rowspan = rowspan
        self.align = align


INLINE_MD = {
    "b": ("**", "**"), "strong": ("**", "**"),
    "i": ("*", "*"), "em": ("*", "*"),
    "s": ("~~", "~~"), "del": ("~~", "~~"), "strike": ("~~", "~~"),
    "code": ("`", "`"),
}
BLOCK_SEP = {"p", "div", "li", "ul", "ol", "blockquote", "section", "h1", "h2",
                "h3", "h4", "h5", "h6", "pre", "figure", "figcaption", "dl", "dt", "dd"}


def _int(v: str | None, default: int = 1) -> int:
    try:
        return max(1, int(str(v).strip()))
    except (TypeError, ValueError):
        return default


def _align_of(attrs: dict[str, str]) -> str:
    v = (attrs.get("align") or "").strip().lower()
    m = re.search(r"text-align\s*:\s*(left|right|center|start|end)",
                    attrs.get("style", ""), re.I)
    if m:
        v = m.group(1).lower()
    return {"left": ":---", "start": ":---",
            "right": "---:", "end": "---:",
            "center": ":---:"}.get(v, "---")


class _TableParser(HTMLParser):
    """Builds rows of _Cell from a <table> fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self.caption: list[str] = []
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._frag: list[str] = []
        self._stack: list[tuple[str, int, str | None]] = []  # (tag, frag index, payload)
        self._depth = 0

    # -- fragment helpers
    def _text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._frag)).strip()

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        self._cell.text = self._text()
        if self._row is not None:
            self._row.append(self._cell)
        self._cell, self._frag = None, []

    def _close_row(self) -> None:
        self._close_cell()
        if self._row is not None:
            self.rows.append(self._row)
            self._row = None

    # -- HTMLParser hooks
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = {k.lower(): (v or "") for k, v in attrs}

        if tag == "table":
            self._depth += 1
            return
        if self._depth > 1:            # nested table: flatten to text
            return

        if tag == "caption":
            self._frag = []
            self._stack.append(("caption", 0, None))
            return
        if tag == "tr":
            self._close_row()
            self._row = []
            return
        if tag in ("td", "th"):
            self._close_cell()
            if self._row is None:
                self._row = []
            self._cell = _Cell(header=(tag == "th"),
                                colspan=_int(a.get("colspan")),
                                rowspan=_int(a.get("rowspan")),
                                align=_align_of(a))
            self._frag = []
            return

        if self._cell is None and not self._stack:
            return

        if tag == "br":
            self._frag.append(" ")
        elif tag == "img":
            src = a.get("src", "")
            if src:
                self._frag.append(f"![{a.get('alt', '')}]({src})")
        elif tag == "a":
            self._stack.append(("a", len(self._frag), a.get("href") or None))
        elif tag in INLINE_MD:
            self._stack.append((tag, len(self._frag), None))
        elif tag in BLOCK_SEP:
            self._frag.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag == "table":
            if self._depth == 1:
                self._close_row()
            self._depth = max(0, self._depth - 1)
            return
        if self._depth > 1:
            return

        if tag in ("td", "th"):
            self._close_cell()
            return
        if tag == "tr":
            self._close_row()
            return
        if tag == "caption":
            if self._stack and self._stack[-1][0] == "caption":
                self._stack.pop()
                self.caption.append(self._text())
            self._frag = []
            return

        for i in range(len(self._stack) - 1, -1, -1):
            name, idx, payload = self._stack[i]
            if name != tag:
                continue
            del self._stack[i]
            inner = "".join(self._frag[idx:])
            if not inner.strip():
                self._frag[idx:] = [inner]
            elif tag == "a" and payload:
                self._frag[idx:] = [f"[{inner}]({payload})"]
            elif tag in INLINE_MD:
                o, c = INLINE_MD[tag]
                self._frag[idx:] = [f"{o}{inner}{c}"]
            break

    def handle_data(self, data):
        if self._depth <= 1 and (self._cell is not None or self._stack):
            self._frag.append(data)


def _build_grid(rows: list[list[_Cell]]) -> list[list[str]]:
    """Expand colspan/rowspan into a rectangular grid; span fillers are empty."""
    occupied: dict[tuple[int, int], str] = {}
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            while (r, c) in occupied:
                c += 1
            for dr in range(cell.rowspan):
                for dc in range(cell.colspan):
                    occupied[(r + dr, c + dc)] = cell.text if (dr == 0 and dc == 0) else ""
            c += cell.colspan
    if not occupied:
        return []
    nrows = max(r for r, _ in occupied) + 1
    ncols = max(c for _, c in occupied) + 1
    return [[occupied.get((r, c), "") for c in range(ncols)] for r in range(nrows)]


def _cell_md(s: str) -> str:
    s = s.replace("|", "\\|")
    return re.sub(r"\s+", " ", s).strip()


def _render_table(rows: list[list[_Cell]], caption: str) -> str | None:
    grid = _build_grid(rows)
    if not grid or not any(any(c for c in r) for r in grid):
        return None

    ncols = max(len(r) for r in grid)
    grid = [r + [""] * (ncols - len(r)) for r in grid]

    first_all_th = bool(rows) and all(c.header for c in rows[0])
    head, body = (grid[0], grid[1:]) if first_all_th else ([""] * ncols, grid)

    aligns: list[str] = []
    if rows:
        for c in rows[0]:
            aligns.extend([c.align] * c.colspan)
    aligns = (aligns + ["---"] * ncols)[:ncols]

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(_cell_md(c) for c in cells) + " |"

    out = [line(head), "| " + " | ".join(aligns) + " |"]
    out += [line(r) for r in body]
    md = "\n".join(out)

    cap = " ".join(c for c in caption).strip()
    if cap:
        md = f"**{_cell_md(cap)}**\n\n{md}"
    return md


def tables_to_markdown(text: str) -> tuple[str, int]:
    """Replace every top-level HTML <table> with a GFM pipe table."""
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        p = _TableParser()
        try:
            p.feed(m.group(0))
            p.close()
        except Exception:
            return m.group(0)
        if not p.rows:
            return m.group(0)
        md = _render_table(p.rows, "".join(p.caption))
        if not md:
            return m.group(0)
        count += 1
        # keep the table on its own block, and keep a blank line after it
        return f"\n\n{md}\n\n"

    return TABLE_RE.sub(repl, text), count


def map_outside_fences(text: str, fn) -> str:
    """Apply fn only to regions outside fenced code blocks."""
    parts, last = [], 0
    for m in FENCE_RE.finditer(text):
        parts.append(fn(text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(fn(text[last:]))
    return "".join(parts)


# --------------------------------------------------------------------------
# export context
# --------------------------------------------------------------------------
class Ctx:
    def __init__(self, src: Path, coll: Path, *, slug: bool = True, dry: bool = False):
        self.src = src
        self.coll = coll
        self.uploads = coll / "uploads"
        self.dry = dry
        self.slug = slug

        self.by_name: dict[str, list[Path]] = {}
        self.cache: dict[Path, Path] = {}
        self.hashes: dict[str, Path] = {}
        self.used: set[str] = set()
        self.unresolved: dict[str, set[str]] = {}
        self.n_links = 0
        self.n_imgs = 0
        self.n_tables = 0

        for f in src无疑.rglob("*") if False else src.rglob("*"):
            if f.is_file() and f.suffix.lower() not in MD_EXT:
                self.by_name.setdefault(f.name, []).append(f)

    def resolve(self, ref: str, src_md: Path) -> Path | None:
        clean = unquote(html.unescape(ref))
        clean = clean.split("#", 1)[0].split("?", 1)[0].strip()
        while clean.startswith("../"):
            clean = clean[3:]
        clean = clean.lstrip("/")
        if not clean:
            return None
        for base in (src_md.parent, self.src):
            p = Path(os.path.normpath(os.path.join(str(base), clean)))
            if p.is_file():
                return p
        hits = self.by_name.get(Path(clean).name, [])
        return hits[0] if len(hits) == 1 else None

    def asset_name(self, path: Path) -> str:
        stem = path.stem
        if self.slug:
            stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
            stem = re.sub(r"[^\w.\-]+", "-", stem).strip("-._") or "file"
        return stem + (path.suffix.lower() or ".bin")

    def upload(self, path: Path) -> Path:
        if path in self.cache:
            return self.cache[path]

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in self.hashes:
            self.cache[path] = self.hashes[digest]
            return self.cache[path]

        base_name = self.asset_name(path)
        name, n = base_name, 1
        while name in self.used or (not self.dry and (self.uploads / name).exists()):
            stem, ext = os.path.splitext(base_name)
            name = f"{stem}-{n}{ext}"
            n += 1

        dest = self.uploads / name
        self.used.add(name)
        if not self.dry:
            self.uploads.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)

        self.hashes[digest] = dest
        self.cache[path] = dest
        return dest

    def url(self, dest: Path, dst_md: Path | None = None) -> str:
        """Flat reference. Outline matches attachments textually against zip
        paths and never resolves them relative to the document, so any ../
        prefix fails to match and is left glued to the redirect URL."""
        return "uploads/" + quote(dest.name, safe="")

    def add_unresolved(self, ref: str, src_md: Path) -> None:
        self.unresolved.setdefault(str(src_md), set()).add(ref)


# --------------------------------------------------------------------------
# markdown rewrites
# --------------------------------------------------------------------------
def split_target(raw: str) -> tuple[str, str | None]:
    raw = raw.strip()
    if not raw:
        return "", None
    if raw.startswith("<") and raw.endswith(">"):
        return raw[1:-1].strip(), None
    m = TITLE_RE.match(raw)
    if m:
        return m.group(1).strip(), m.group(2)
    return raw, None


def rewrite_links(text: str, ctx: Ctx, src_md: Path, dst_md: Path) -> str:
    def repl(m: re.Match) -> str:
        target, title = split_target(m.group(2))
        if not target or EXTERNAL_RE.match(target):
            return m.group(0)
        p = ctx.resolve(target, src_md)
        if p is None:
            ctx.add_unresolved(target, src_md)
            return m.group(0)
        ctx.n_links += 1
        title_part = f' "{title}"' if title else ""
        return f"{m.group(1)}{ctx.url(ctx.upload(p), dst_md)}{title_part}{m.group(3)}"
    return LINK_RE.sub(repl, text)


def rewrite_refdefs(text: str, ctx: Ctx, src_md: Path, dst_md: Path) -> str:
    def repl(m: re.Match) -> str:
        head, raw, tail = m.group(1), m.group(2), m.group(3)
        if raw.startswith("<") and raw.endswith(">"):
            raw = raw[1:-1].strip()
        if not raw or EXTERNAL_RE.match(raw):
            return m.group(0)
        p = ctx.resolve(raw, src_md)
        if p is None:
            ctx.add_unresolved(raw, src_md)
            return m.group(0)
        ctx.n_links += 1
        return f"{head}{ctx.url(ctx.upload(p), dst_md)}{tail}"
    return REFDEF_RE.sub(repl, text)


def rewrite_img_tag(tag: str, new_url: str, keep_dims: bool) -> str:
    def attr(m: re.Match) -> str:
        name = m.group(1).lower()
        if name == "src":
            return f'src="{new_url}"'
        if name in DROP_ATTRS:
            return ""
        if name in {"width", "height"} and not keep_dims:
            return ""
        return m.group(0)

    out = ATTR_RE.sub(attr, tag)
    return re.sub(r"\s{2,}", " ", out).replace(" >", ">")


def rewrite_imgs(text: str, ctx: Ctx, src_md: Path, dst_md: Path,
                 *, keep_html: bool, keep_dims: bool) -> str:
    def repl(m: re.Match) -> str:
        tag = m.group(0)
        s = IMG_SRC_RE.search(tag)
        if not s:
            return tag
        ref = s.group(1) if s.group(1) is not None else s.group(2)
        if not ref or EXTERNAL_RE.match(ref):
            return tag
        p = ctx.resolve(ref, src_md)
        if p is None:
            ctx.add_unresolved(ref, src_md)
            return tag
        url = ctx.url(ctx.upload(p), dst_md)
        ctx.n_imgs += 1
        if keep_html:
            return rewrite_img_tag(tag, url, keep_dims)
        a = IMG_ALT_RE.search(tag)
        alt = (a.group(1) if a.group(1) is not None else a.group(2)) if a else ""
        return f"![{alt}]({url})"
    return IMG_TAG_RE.sub(repl, text)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path,
                    help="unzipped Trilium Markdown export directory")
    ap.add_argument("--collection", required=True, help="Outline collection name")
    ap.add_argument("--out", type=Path, default=Path("outline-tree"))
    ap.add_argument("--zip", type=Path, default=Path("outline-import.zip"))
    ap.add_argument("--apply", action="store_true", help="write files and build the zip")
    ap.add_argument("--keep-html-img", action="store_true",
                    help="keep <img> tags instead of Markdown")
    ap.add_argument("--keep-dims", action="store_true", help="keep width/height on <img>")
    ap.add_argument("--keep-html-tables", action="store_true",
                    help="do not convert HTML <table> to pipe tables")
    ap.add_argument("--no-slug", action="store_true",
                    help="keep original asset filenames (unsafe for Outline)")
    args = ap.parse_args(argv)

    if not args.src.is_dir():
        ap.error(f"--src is not a directory: {args.src}")
    if args.no_slug:
        print("warning: --no-slug can break Outline's importer for filenames "
              "containing ( ) ! ' * &", flush=True)

    coll = args.out / args.collection
    ctx = Ctx(args.src, coll, slug=not args.no_slug, dry=not args.apply)

    md_files = sorted(p for p in args.src.rglob("*") if p.suffix.lower() in MD_EXT)
    if not md_files:
        ap.error(f"no Markdown files found under {args.src}")

    for src_md in md_files:
        dst_md = coll / src_md.relative_to(args.src)
        text = src_md.read_text(encoding="utf-8")

        # 1. HTML tables -> pipe tables (outside fenced code)
        if not args.keep_html_tables:
            def _conv(s: str) -> str:
                out, n = tables_to_markdown(s)
                ctx.n_tables += n
                return out
            text = map_outside_fences(text, _conv)

        # 2. asset references
        text = rewrite_refdefs(text, ctx, src_md, dst_md)
        text = rewrite_links(text, ctx, src_md, dst_md)
        text = rewrite_imgs(text, ctx, src_md, dst_md,
                            keep_html=args.keep_html_img, keep_dims=args.keep_dims)

        # 3. sanity check: no traversal may survive
        for bad in re.findall(r"\(\s*\.\.?/[^)]*\)", text):
            ctx.unresolved.setdefault(str(src_md), set()).add(f"TRAVERSAL {bad}")

        if args.apply:
            dst_md.parent.mkdir(parents=True, exist_ok=True)
            dst_md.write_text(text, encoding="utf-8")

    if args.apply:
        args.zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.zip, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(coll.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(coll.parent).as_posix())

    print(f"{'WROTE' if args.apply else 'DRY RUN'}: "
            f"{len(ctx.cache)} assets, {ctx.n_imgs} img tags, "
            f"{ctx.n_links} links, {ctx.n_tables} tables -> {coll}"
            f"{'' if args.apply else '  (nothing written)'}")
    if args.apply:
        print(f"archive: {args.zip}")

    if ctx.unresolved:
        print("\nUnresolved (left untouched):")
        for md, refs in sorted(ctx.unresolved.items()):
            print(f"  {md}:")
            for r in sorted(refs):
                print(f"    - {r}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
