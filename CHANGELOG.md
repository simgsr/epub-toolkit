# Changelog

All notable changes to this project are documented here.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-10

First public release.

### Added

- `split_epubs.py` — split a multi-book bundle (合集 / 套装) into one EPUB per
  work, taking the boundaries from the bundle's own `toc.ncx` top-level
  navPoints, its 总目录 page, or its structural marks. `--list` previews the
  plan and writes nothing.
- `normalise_epub.py` — repair a legacy EPUB and upgrade it to EPUB 3 without
  touching the prose: namespace typo, dead identifier, undeclared images, GBK
  member names, `res://` fonts, CRLF `mimetype`.
- `verify_epubs.py` — structural checks: container/OPF, manifest↔archive
  agreement, spine, navigation links and fragment targets, cover, and no leaked
  set-level pages.
- `content_check.py` — prove no text was lost or invented, by comparing each
  output document against the source bundle it came from.
- `summarise_epubcheck.py` — roll the per-book epubcheck JSON reports into one
  tally.
- `epub_paths.py` — the shared output-directory constants.
- `Makefile` driving the whole pipeline, with `help` as the default goal.
- CI running the scripts on Python 3.9, 3.12 and 3.14, on Linux and macOS.

### Notes

- Standard library only — no third-party Python packages, and no
  `requirements.txt` to install.
- This repository ships **no books**. `*.epub` is ignored, so neither your
  source bundles nor your finished output ever enters git.

[Unreleased]: https://github.com/simgsr/epub-toolkit/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/simgsr/epub-toolkit/releases/tag/v1.0.0
