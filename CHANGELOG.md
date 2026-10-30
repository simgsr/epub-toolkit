# Changelog

All notable changes to this project are documented here.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `check_filenames.py`: holds every EPUB to one filename convention --
  `Author - Book name.epub`, or `Author - Series 01 Book name.epub` when the
  book belongs to a series.  The author, series and number are read from the
  book's own package document rather than guessed from the name, so a file
  called `The Russian Revolution - A New History (2017) (Sean McMeekin)
  (z-library.sk, 1lib.sk, z-lib.sk).epub` is reported for the author buried at
  the end and for the download-site tag.  `--fix` repairs the name it was given
  -- the author moves to the front, the site tag goes, the whitespace is
  collapsed -- and never replaces it with the metadata title, which is usually
  shorter than what the name says.  `make names` gates the pipeline on it;
  `make names-fix` applies it.  A multi-book bundle is reported and skipped: it
  is a source the splitter finds by its `match` substring, so holding it to the
  library convention would fail the gate on every freshly downloaded bundle
  before anything had been split.  A series takes part in the name only when the
  book declares a NUMBER for it -- "Mistborn 01" is what sorts a shelf, while a
  publisher's set such as 阿兰·德波顿作品全集 carries no `group-position` and
  would only repeat the author inside the book name.

### Fixed

- `verify_epubs.py` rejected a fragment-only nav link such as `href="#toc"`,
  resolving it against the nav document's directory instead of the document
  itself and reporting `nav link -> missing file` for a link that was correct.
  The same mis-resolution in the NCX check is fixed with it.
- `normalise_epub.py` copied a structurally-sound EPUB 3 through untouched,
  discarding the repairs it had just computed.  It now also detects and repairs
  the defects that live in the content documents and the package, so a book that
  needs one of them is rewritten instead of passed over: an empty `<title>`
  (RSC-005), a stylesheet `@font-face` whose font file the book does not carry
  (RSC-007), a `properties` token EPUB 3 does not define on a manifest item or a
  spine `<itemref>` (OPF-027), a Kindle `data-*` attribute that is not a legal
  custom data attribute (HTM_061), and an NCX whose `playOrder` disagrees
  between navPoints naming the same target, or leaves gaps in the sequence.
- `normalise_epub.py` declared the wrong manifest item as the cover, leaving
  `properties="cover-image"` on the cover *page* rather than the cover *image*.
  Two things were wrong: EPUB 2's `<meta name="cover" content="…"/>` was only
  read with its attributes in that order, which misses the equally common
  reversed spelling, and the fallback then took the first manifest item whose
  href mentioned "cover" — for a book carrying both, that is `cover.xhtml`.  The
  `<meta>` is now read in either order, only an image can be the cover, and a
  stray token left on any other item is cleared.
- `normalise_epub.py` wrote a book arriving from outside into `split/` under a
  free name, so every run left another `name (2)`, `name (3)` … beside the first
  copy.  A book now lands on its own name, rewriting the output it produced
  earlier in place.
- `verify_epubs.py` reported `cover image suspiciously small` when the property
  sat on the cover's XHTML page.  That is true of the 600 byte page it then
  read, but it sent you looking for a small image; the non-image carrying the
  property is now named.

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
