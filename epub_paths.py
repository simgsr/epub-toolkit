#!/usr/bin/env python3
"""Where the EPUB toolkit keeps its files.

One place for the directories, so the Makefile and the scripts cannot drift
apart: the Makefile exports `EPUB_SPLIT_DIR` / `EPUB_REPORT_DIR` from its own
variables, and every script reads them through here.

Everything is stdlib and side-effect free, and it must stay importable from any
working directory -- Python puts this file's own directory on `sys.path`, so
`import epub_paths` works wherever the script is run from.  Running a script
with `python3 -I` would drop that and break the import.
"""

import glob
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

#: Where finished books are written.  The user moves them out by hand, so this
#: directory is never cleaned automatically.
SPLIT = os.environ.get('EPUB_SPLIT_DIR') or os.path.join(ROOT, 'split')

#: One epubcheck JSON report per book lands here.
REPORTS = os.environ.get('EPUB_REPORT_DIR') or os.path.join(ROOT, 'epubcheck-out')

#: Optional directory of web-sourced covers, named "<key>-<NN>.jpg".  Unset by
#: default: a book with no cover of its own is written without one and noted,
#: never skipped.
WEB_COVERS = os.environ.get('EPUB_WEB_COVERS') or None


def inputs(patterns=('*.epub',)):
    """The EPUBs the user has dropped into the project root, sorted."""
    found = []
    for pattern in patterns:
        found += glob.glob(os.path.join(ROOT, pattern))
    return sorted(set(found))


def outputs():
    """The finished books currently sitting in SPLIT/."""
    return sorted(glob.glob(os.path.join(SPLIT, '*.epub')))
