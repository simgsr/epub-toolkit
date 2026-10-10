# EPUB toolkit

[![CI](https://github.com/simgsr/epub-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/simgsr/epub-toolkit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%20%7C%203.12%20%7C%203.14-blue.svg)](pyproject.toml)

Split multi-book EPUB bundles into one book per work, bring books up to a
standard EPUB 3, and check the results. Everything runs from this folder with
`make`.

## Requirements

- **Python 3.9 or newer** — tested on 3.9, 3.12 and 3.14. There is nothing to
  install: the scripts are standard library only, so there is no
  `requirements.txt` and no virtualenv to activate.
- **`epubcheck` on `PATH`** for the full quality gate (`brew install epubcheck`).
  It is a Java tool, so it is not a Python dependency and cannot be declared in
  `pyproject.toml`. Optional — `make check-lite` skips it.

## Using it

Drop the EPUB(s) into this folder and run `make run`. Finished books are
written to `split/` — **the toolkit never moves or deletes them**; move them
wherever you keep your library yourself when you're happy with them.

Bare `make` prints the target list rather than running the pipeline, so you can
always see what is available.

```sh
make run          # split, then correct, then the whole quality gate
make plan         # just show what the splitter would do
make split-epub   # split the bundles here into split/
make correct      # repair + upgrade to EPUB 3
make check        # verify + content + epubcheck + summarise
make clean        # drop reports and caches (split/*.epub is left alone)
```

`make help` lists every target. `make -j` is safe: `run` chains the steps
serially and stops at the first real failure.

## Configuration

The scripts read three environment variables. The Makefile exports the first
two from its own `SPLIT` / `REPORTS` variables, so a shell override and a `make`
override always agree.

| variable | default | what it does |
|---|---|---|
| `EPUB_SPLIT_DIR` | `split` | where finished books are written — the one directory `verify` / `content` / `epubcheck` read |
| `EPUB_REPORT_DIR` | `epubcheck-out` | where `make epubcheck` drops one JSON report per book |
| `EPUB_WEB_COVERS` | unset | a directory of `<bundle-key>-<NN>.jpg` web covers, for a book whose bundle carries no cover of its own |

```sh
EPUB_WEB_COVERS=~/covers make split-epub
make SPLIT=books PYTHON=python3.14 run
```

## What each script does

| script | job |
|---|---|
| `split_epubs.py` | split each multi-book bundle into one EPUB per work, from the bundle's own NCX / 总目录 / structural marks (`--list` to preview) |
| `normalise_epub.py` | repair an old EPUB (namespace typo, dead identifier, undeclared images, GBK member names, `res://` fonts, CRLF mimetype) **and** upgrade it to EPUB 3, without touching the prose |
| `verify_epubs.py` | structural checks: container/OPF, manifest↔archive agreement, spine, navigation links and fragment targets, cover, no leaked set-level pages |
| `content_check.py` | prove no text was lost or invented, comparing each output document against the source bundle it came from |
| `summarise_epubcheck.py` | roll the per-book epubcheck JSON reports into one tally |
| `epub_paths.py` | the shared directory constants (see below) |

`split_epubs.py` carries a `BUNDLES` table of per-bundle configuration; its
header comment documents every key. A bundle with no entry there will not be
split — add one, using the 徐皓峰作品集 entry as the worked example.

## Conventions

- **Outputs go to `split/`.** That is the only place books are written, so
  `verify`/`content`/`epubcheck` have a single directory to check and you have a
  single place to collect from.
- **A cover is never required.** A book whose bundle carries no cover of its own
  is written without one and reported `NO-COVER`. To supply web covers, point
  `EPUB_WEB_COVERS` at a directory of images, as above.
- **Exit codes** are meant for chaining: `0` nothing to do or all good, `2` an
  input was present but produced nothing, `1` a real failure.
- `content_check.py` can only check a book whose **source bundle is still in
  this folder** (it re-plans it to compare text). Books that were not split —
  a repaired single volume — have no bundle to compare against and are skipped;
  `--strict` turns that into a failure instead.

## No books are included

This repository contains **code only**. `*.epub` is ignored at any depth, so
neither the source bundles you drop in the project root nor the finished books
in `split/` are ever committed — which also means the output directory is
recreated on demand rather than tracked. CI fails if an `.epub` ever becomes
tracked.

Point the toolkit at books you own; what you do with the output is your call.

## Layout

```
Makefile              the pipeline
epub_paths.py         shared constants: where outputs go
split_epubs.py        bundle -> one EPUB per work
normalise_epub.py     repair + upgrade to EPUB 3
verify_epubs.py       structural checks
content_check.py      no text lost or invented
summarise_epubcheck.py  tally epubcheck reports
pyproject.toml        metadata only: requires-python, no dependencies
```

## License

[MIT](LICENSE) © 2026 simgsr
