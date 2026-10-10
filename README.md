# EPUB toolkit

Split multi-book EPUB bundles into one book per work, bring books up to a
standard EPUB 3, and check the results. Everything runs from this folder with
`make`.

## Using it

Drop the EPUB(s) into this folder and run `make`. Finished books are written to
`split/` — **the toolkit never moves or deletes them**; move them wherever you
keep your library yourself when you're happy with them.

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
  `EPUB_WEB_COVERS` at a directory of `<bundle-key>-<NN>.jpg` images:
  `EPUB_WEB_COVERS=~/covers make split-epub`.
- **Exit codes** are meant for chaining: `0` nothing to do or all good, `2` an
  input was present but produced nothing, `1` a real failure.
- `content_check.py` can only check a book whose **source bundle is still in
  this folder** (it re-plans it to compare text). Books that were not split —
  a repaired single volume — have no bundle to compare against and are skipped;
  `--strict` turns that into a failure instead.
- `python3` here is Xcode's 3.9.6; homebrew's 3.14 is `python3.14`. Both run the
  scripts, so override if you prefer: `make PYTHON=python3.14 run`.

## Requirements

`epubcheck` on `PATH` (`brew install epubcheck`). Python is stdlib-only — no
third-party packages are needed.
