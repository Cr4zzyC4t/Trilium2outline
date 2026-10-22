# trilium2outline

A quick tool I threw together with AI to convert a [Trilium](https://github.com/TriliumNext/Trilium) markdown export zip to an [Outline](https://github.com/outline/outline) import zip.

> [!WARNING]
> I have only tested this script on MacOS 27.0.1. I believe it will work on all platform however I make no guarantee

> [!NOTE]
> I will not be maintaining this project. Feel free to create your own forks or send a merge request with the fix to add features and fix issues.


## Usage

```bash
python main.py --src SRC --collection COLLECTION [-h] [--out OUT]
                [--zip ZIP] [--apply] [--keep-html-img] [--keep-dims]
                [--no-slug]
```

### Flags

| Flag | Use |
|------|-----|
| `-h` or `--help` | shows the help info |
| `--apply` | write files + build the zip (default is a dry run) |
| `--keep-html-img` | keep <img> tags instead of converting to Markdown |
| `--keep-dims` | keep width/height attributes on `<img>` |
| `--keep-html-tables` | leave `<table>` HTML alone (Outline will likely drop it)|
| `--no-slug` | keep original asset filenames (UNSAFE for Outline) |
