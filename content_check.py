#!/usr/bin/env python3
"""Prove the split did not lose (or invent) any text.

Every split book keeps its documents at their original archive paths, so each
output document can be compared against the very same path in the bundle it
came from.  The splitter rewrites tags (doctype, valign, li value, dead links,
the <title>) but must never touch the words, so the comparison is on text with
markup stripped: tags, comments, <head> and <style>/<script> bodies removed,
entities unescaped, whitespace collapsed.

One bundle (徐皓峰作品集) does not align a book to whole documents: a converted
file carries the tail of one work and the head of the next, so the splitter
cuts it (plan_marked_books) and writes each piece under its own path.  The
source side of the comparison is therefore taken through the splitter's own
`Bundle.read`, which serves the same slice for an original path and for the
derived one; that also keeps a piece written under a derived name
(text00002_p0.html) from being skipped as if it were a generated file.

Embedded images are compared byte for byte, since those are copied verbatim.

A book that was not split -- a repaired single volume -- has no bundle to
compare against, so it is skipped rather than failed; `--strict` makes that a
failure instead.  A book whose source bundle is simply not in the folder is
skipped for the same reason (the check needs it present to re-plan it).

Usage:  python3 content_check.py [FILE...] [--strict]
"""

import argparse
import glob
import html
import os
import re
import sys
import zipfile

from split_epubs import BUNDLES, Bundle, plan_bundle
from epub_paths import ROOT, SPLIT

STRIP_HEAD = re.compile(r'(?is)<head\b.*?</head>')
STRIP_SCRIPT = re.compile(r'(?is)<(script|style)\b.*?</\1>')
STRIP_COMMENT = re.compile(r'(?s)<!--.*?-->')
# A tag always opens with a letter, "/", "!" or "?".  Requiring that keeps a
# stray "<" in the text (择天记 chapter224 has one where "<p>" was meant) from
# swallowing the words that follow it up to the next ">", which would hide a
# real difference behind what looks like a tag.  See escape_stray_angles.
STRIP_TAG = re.compile(r'(?s)</?[A-Za-z!?][^>]*>')
IMAGE = re.compile(r'\.(?:jpe?g|png|gif)$', re.I)

# XML cannot represent these control characters, so the splitter drops them
# (see strip_control_chars).  Apply the same removal here: otherwise Python's
# \s would collapse a source U+001D to a space while the output has none,
# reporting a whitespace-only difference that is not a change in the text.
CTRL_CHAR = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def text_of(raw):
    raw = CTRL_CHAR.sub('', raw)
    raw = STRIP_HEAD.sub(' ', raw)
    raw = STRIP_SCRIPT.sub(' ', raw)
    raw = STRIP_COMMENT.sub(' ', raw)
    raw = STRIP_TAG.sub(' ', raw)
    return re.sub(r'\s+', ' ', html.unescape(raw)).strip()


def bundle_for(set_title):
    for cfg in BUNDLES:
        if cfg['set_title'] == set_title:
            for f in glob.glob(os.path.join(ROOT, '*.epub')):
                if cfg['match'] in os.path.basename(f):
                    return f
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('files', nargs='*', metavar='FILE',
                    help='books to check (default: everything in split/)')
    ap.add_argument('--strict', action='store_true',
                    help='a book whose source bundle is gone is a failure '
                         'rather than a skip')
    args = ap.parse_args()

    cfgs = {cfg['set_title']: cfg for cfg in BUNDLES}
    sources = {cfg['set_title']: bundle_for(cfg['set_title']) for cfg in BUNDLES}
    cache = {}

    def source_bundle(set_title, path):
        """The source bundle, with the splitter's slices already planned."""
        if set_title not in cache:
            bundle = Bundle(path, cfgs[set_title])
            plan_bundle(bundle)               # fills bundle.slices
            cache[set_title] = bundle
        return cache[set_title]

    docs = 0
    images = 0
    problems = []
    skipped = []

    for out in args.files or sorted(glob.glob(os.path.join(SPLIT, '*.epub'))):
        name = os.path.basename(out)
        zout = zipfile.ZipFile(out)
        opf = [n for n in zout.namelist() if n.endswith('.opf')][0]
        raw = zout.read(opf).decode('utf-8')
        m = re.search(r'<dc:source>(.*?)</dc:source>', raw)
        if not m:
            # a book that was not split (a repaired single volume) has no
            # bundle to compare against -- nothing to prove, so nothing to fail
            skipped.append((name, 'not a split book (no dc:source)'))
            continue
        set_title = html.unescape(m.group(1))
        src = sources.get(set_title)
        if src is None:
            why = (f'source bundle {set_title!r} is not in this folder'
                   if set_title in cfgs else
                   f'not a split book (dc:source is {set_title!r})')
            if args.strict:
                problems.append((name, f'({why})', '', ''))
            else:
                skipped.append((name, why))
            continue
        bundle = source_bundle(set_title, src)

        for path in zout.namelist():
            if path.endswith('/'):
                continue
            # a generated file (opf, nav, ncx, cover) is in neither; a split
            # document is at its original path, or under the derived name the
            # splitter wrote its slice to
            if path not in bundle.names and path not in bundle.slices:
                continue
            a, b = zout.read(path), bundle.read(path)
            if IMAGE.search(path):
                images += 1
                if a != b:
                    problems.append((name, path, 'image bytes differ', ''))
                continue
            if not path.lower().endswith(('.html', '.xhtml', '.htm')):
                continue
            docs += 1
            ta, tb = text_of(a.decode('utf-8', 'replace')), text_of(b.decode('utf-8', 'replace'))
            if ta != tb:
                i = next((k for k in range(min(len(ta), len(tb))) if ta[k] != tb[k]),
                         min(len(ta), len(tb)))
                problems.append((name, path, tb[max(0, i - 60):i + 60],
                                 ta[max(0, i - 60):i + 60]))

    print(f'{docs} documents compared   (text must be identical)')
    print(f'{images} images compared      (bytes must be identical)')
    if skipped:
        for name, why in skipped:
            print(f'  -- {name}: {why}')
    if not problems:
        print('\nno text loss, no altered image, no invented content')
        return 0

    print(f'\n{len(problems)} PROBLEM(S):')
    for name, path, want, got in problems:
        print(f'\n  {name}')
        print(f'    {path}')
        print(f'    source: {want!r}')
        print(f'    output: {got!r}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
