#!/usr/bin/env python3
"""
Hold every EPUB to one filename convention.

    Author - Book name.epub
    Author - Series 01 Book name.epub

The author comes first, then " - ", then the book's own name.  A book that
belongs to a series carries the series name and its number in front of the book
name, separated by a space -- the number is what makes the shelf sort correctly,
and `01` (not `1`) is what keeps it sorting past 9.

The check reads each book's own package document rather than trusting the file
name, so it knows the author, the series and the number even when the name is
wrong.  That is the whole point: the usual defect is a downloaded file called
"The Russian Revolution - A New History (2017) (Sean McMeekin) (z-library.sk,
1lib.sk, z-lib.sk).epub", which has the author buried at the end behind a
site advert.

`--fix` renames such a file into the convention.  It repairs the NAME it was
given -- moving the author to the front, dropping the download-site tag,
collapsing the whitespace -- and never replaces it with the metadata title,
because a filename often says more than the package does (a subtitle the
dc:title dropped) and losing that would be a different, silent data loss.

Only the standard library is used.

Usage:
    python3 check_filenames.py             # report every EPUB's name
    python3 check_filenames.py --fix       # rename the ones that break it
    python3 check_filenames.py split/*.epub
"""

import argparse
import os
import re
import sys
import zipfile

from epub_paths import SPLIT, inputs, outputs
from normalise_epub import is_bundle
from split_epubs import plain_author

#: The separator the convention is built on.
SEP = ' - '

# Download-site tags that end up in a filename and belong nowhere in a library.
# Matched as a whole parenthesised group so only the tag goes, not the name.
JUNK_GROUP = re.compile(
    r'''\s*\((?:(?:z-library|z-lib|1lib|3lib|libgen|annas-archive|annasarchive'''
    r'''|bookzz|bookfi|manybooks|www\.[^)]*|https?://[^)]*)[^)]*)\)''',
    re.I)
JUNK_BARE = re.compile(
    r'''(?:\s*[-–]\s*)?(?:z-library|z-lib\.org|1lib|libgen|annas-archive)''', re.I)

# A series number is digits, optionally decimal ("2.5") -- what Calibre writes.
SERIES_INDEX = re.compile(r'^\d+(?:\.\d+)?$')

AUTHOR_TRAIL = re.compile(r'\s*\(([^()]*)\)\s*$')


def package_document(z):
    """The path of the book's OPF inside the archive."""
    names = [n for n in z.namelist() if n.lower().endswith('.opf')]
    if not names:
        return None
    # the shallowest .opf is the package; anything else is a stray copy
    return min(names, key=lambda n: (n.count('/'), len(n)))


def read_metadata(path):
    """What the book says about itself: author, title, series, series number."""
    meta = {'author': '', 'title': '', 'series': '', 'index': ''}
    try:
        with zipfile.ZipFile(path) as z:
            opf = package_document(z)
            if opf is None:
                return meta
            text = z.read(opf).decode('utf-8', 'replace')
    except (zipfile.BadZipFile, OSError):
        return meta

    def grab(pattern, flags=re.S):
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else ''

    meta['author'] = grab(r'<dc:creator\b[^>]*>(.*?)</dc:creator\s*>')
    meta['title'] = grab(r'<dc:title\b[^>]*>(.*?)</dc:title\s*>')
    # EPUB 2 / Calibre spelling first, then the EPUB 3 collection vocabulary
    meta['series'] = (grab(r'''name\s*=\s*["']calibre:series["'][^>]*'''
                           r'''content\s*=\s*["']([^"']*)["']''', re.I)
                      or grab(r'''content\s*=\s*["']([^"']*)["'][^>]*'''
                              r'''name\s*=\s*["']calibre:series["']''', re.I)
                      or grab(r'<meta\b[^>]*property\s*=\s*["\']'
                              r'belongs-to-collection["\'][^>]*>(.*?)</meta\s*>'))
    meta['index'] = (grab(r'''name\s*=\s*["']calibre:series_index["'][^>]*'''
                          r'''content\s*=\s*["']([^"']*)["']''', re.I)
                     or grab(r'''content\s*=\s*["']([^"']*)["'][^>]*'''
                             r'''name\s*=\s*["']calibre:series_index["']''', re.I)
                     or grab(r'<meta\b[^>]*property\s*=\s*["\']group-position'
                             r'["\'][^>]*>(.*?)</meta\s*>'))
    for key in meta:
        meta[key] = re.sub(r'\s+', ' ', meta[key]).strip()
    return meta


def tidy_author(raw):
    """A dc:creator as it would be written in a filename.

    "McMeekin, Sean;" -> "Sean McMeekin": Calibre and the library catalogues
    write "Last, First" with an optional trailing separator, and a filename is
    the one place that spelling must not survive.  "阿兰·德波顿(Alain de Botton)"
    -> "阿兰·德波顿": a CJK name carrying its Latin gloss in brackets is the same
    author, and the splitter already decides that question (plain_author), so
    this asks it the same way rather than inventing a second rule.  A name with
    neither a comma nor a bracketed gloss is left exactly as it is.
    """
    name = raw.strip()
    name = re.split(r'[;；]', name)[0].strip()
    if ',' in name:
        last, _, first = name.partition(',')
        last, first = last.strip(), first.strip()
        name = f'{first} {last}' if first else last
    name = plain_author(name)
    return re.sub(r'\s+', ' ', name).strip(' .')


def series_prefix(meta):
    """The "Series 01 " a filename should carry, or '' when it should carry none.

    A series earns its place in a filename through its NUMBER: "Mistborn 01" is
    what makes a shelf sort in reading order.  A collection with no number gets
    no prefix, which is the normal case for a publisher's set -- the books split
    out of 阿兰·德波顿作品全集 declare their set as `collection-type="series"` with
    no `group-position`, so "阿兰·德波顿 - 阿兰·德波顿作品全集 爱情笔记" would repeat
    the author inside the book name and imply an order those fourteen
    independent works do not have.  The set is recorded in the metadata
    (belongs-to-collection, dc:source) where it belongs.
    """
    series = meta.get('series', '').strip()
    index = meta.get('index', '').strip()
    if not series or not index or not SERIES_INDEX.match(index):
        return ''
    if re.match(r'^\d+\.0+$', index):            # Calibre writes 1.0 for 1
        index = index.split('.', 1)[0]
    if len(index) == 1:
        index = index.zfill(2)                   # 01, so 10 sorts after 9
    return f'{series} {index} '


def strip_junk(stem):
    """Remove download-site tags from a filename stem."""
    out = JUNK_GROUP.sub('', stem)
    out = JUNK_BARE.sub('', out)
    return re.sub(r'\s+', ' ', out).strip(' -')


def split_stem(stem):
    """(author, body) as the name is written now; author is '' when unseparated."""
    clean = strip_junk(stem)
    if SEP not in clean:
        return '', clean
    author, _, body = clean.partition(SEP)
    return author.strip(), body.strip()


def trailing_author(stem):
    """The author a "… (Author)" suffix names, or ''.

    `stem` must already have had its download-site tags stripped: the site tag
    is itself the last parenthesised group, and reading it as an author is how
    a filename ends up "fixed" to "Author - Title (1lib.sk, z-lib.sk)".
    """
    m = AUTHOR_TRAIL.search(stem)
    if not m:
        return ''
    inner = m.group(1).strip()
    # "(2017)" is a year, not an author
    if not inner or re.match(r'^\d{4}$', inner) or SERIES_INDEX.match(inner):
        return ''
    return inner


def drop_trailing_group(stem):
    """`stem` without its final "(...)" group."""
    return AUTHOR_TRAIL.sub('', stem).strip()


def expected_stem(path, meta):
    """The filename this book should have, repaired from the one it has."""
    stem = os.path.splitext(os.path.basename(path))[0]
    clean = strip_junk(stem)
    tail = trailing_author(clean)
    body = drop_trailing_group(clean) if tail else clean
    author = tidy_author(meta.get('author', '')) or tail

    if not author:
        head, after = split_stem(clean)
        author, body = head, (after or body)
    # drop an author the body still repeats at the front ("Author - Author - X")
    for lead in (author + SEP, author + ' -', author):
        if body.casefold().startswith(lead.casefold()):
            body = body[len(lead):].lstrip(' -')
            break
    body = re.sub(r'\s+', ' ', body).strip(' -')
    if not author or not body:
        return None

    prefix = series_prefix(meta)
    if prefix:
        # The body may already open with the series name and a wrong number --
        # "Mistborn 02" for a book whose own metadata says 2.5 -- so that run is
        # replaced, not kept in front of the right one.
        series = meta['series'].strip()
        m = re.match(re.escape(series) + r'(?:\s+\d+(?:\.\d+)?)?\s+', body,
                     re.I)
        if m and body[m.end():].strip():
            body = body[m.end():]
        body = prefix + body
    return f'{author}{SEP}{body}'


def violations(path, meta):
    """Every way this file name breaks the convention.  [] means it conforms."""
    stem = os.path.splitext(os.path.basename(path))[0]
    clean = strip_junk(stem)
    found = []

    if stem != stem.strip():
        found.append('leading or trailing whitespace')
    if '  ' in stem:
        found.append('runs of whitespace inside the name')
    if JUNK_GROUP.search(stem) or JUNK_BARE.search(stem):
        found.append('a download-site tag is part of the name')

    author, body = split_stem(clean)
    tail = trailing_author(clean)
    if tail:
        found.append(f'the author "{tail}" sits at the end, in parentheses')
    if not author:
        found.append(f'no "{SEP.strip()}" separator: '
                     f'expected "<Author>{SEP}<Book name>"')
    elif not body:
        found.append('nothing after the separator: the book name is missing')
    else:
        known = tidy_author(meta.get('author', ''))
        if known and author.casefold() != known.casefold() \
                and known.casefold() not in author.casefold():
            found.append(f'the author segment "{author}" does not match the '
                         f'book\'s own dc:creator "{known}"')

    prefix = series_prefix(meta)
    if prefix and body:
        want = prefix.strip()
        if not body.casefold().startswith(want.casefold()):
            found.append(f'the book declares series "{meta["series"]}", so the '
                         f'names must start "{want} " after the separator')
    return found


def rename(path, proposed):
    """Rename one file to `proposed`, returning the new path (or None)."""
    directory = os.path.dirname(os.path.abspath(path))
    target = os.path.join(directory, proposed + '.epub')
    if os.path.abspath(target) == os.path.abspath(path):
        return None
    if os.path.exists(target):
        print(f'      ! {os.path.basename(target)} already exists; skipped')
        return None
    os.rename(path, target)
    return target


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument('files', nargs='*', metavar='FILE',
                    help='EPUBs to check (default: the ones in this folder '
                         'and everything in split/)')
    ap.add_argument('--fix', action='store_true',
                    help='rename the files that break the convention')
    ap.add_argument('--quiet', action='store_true',
                    help='only report the files that break it')
    args = ap.parse_args()

    files = args.files or (inputs() + outputs())
    files = [f for f in dict.fromkeys(files) if os.path.exists(f)]
    if not files:
        print('nothing to check: no EPUBs in this folder or split/')
        return 0

    # A multi-book bundle is a SOURCE, not a book: the splitter finds it by the
    # `match` substring in its name, it is consumed rather than kept, and a
    # download leaves it called whatever the site called it.  Holding it to the
    # library convention would fail the gate on every freshly downloaded bundle
    # before anything was split, so it is reported and passed over.  The books
    # that come OUT of it land in split/ as "Author - Title" and are checked.
    bundles = [f for f in files if is_bundle(f)]
    files = [f for f in files if not is_bundle(f)]
    if bundles and not args.quiet:
        for path in bundles:
            print(f'skip  {os.path.basename(path)} (bundle: split it, and its '
                  f'books are checked in {os.path.basename(SPLIT)}/)')
    if not files:
        print('\nno single-volume EPUB to check')
        return 0

    broken = 0
    fixed = 0
    for path in files:
        base = os.path.basename(path)
        meta = read_metadata(path)
        problems = violations(path, meta)
        if not problems:
            if not args.quiet:
                print(f'ok    {base}')
            continue
        broken += 1
        print(f'BAD   {base}')
        for problem in problems:
            print(f'        - {problem}')
        proposal = expected_stem(path, meta)
        if proposal is None:
            print('        no safe repair: the book names no author to put '
                  'first')
            continue
        print(f'        -> {proposal}.epub')
        if args.fix and rename(path, proposal):
            fixed += 1

    print(f'\n{len(files)} file(s): {len(files) - broken} conform, '
          f'{broken} do not' + (f', {fixed} renamed' if args.fix else ''))
    if broken and not args.fix:
        print('run `python3 check_filenames.py --fix` to rename them')
    return 0 if broken == fixed else 1


if __name__ == '__main__':
    sys.exit(main())
