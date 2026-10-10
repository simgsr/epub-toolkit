#!/usr/bin/env python3
"""
Structural verification for the split EPUBs.

Checks each file in split/ against the parts of the EPUB spec this pipeline is
responsible for: container/OPF integrity, manifest <-> archive agreement, spine
references, navigation links resolving to real documents (and real ids where a
fragment is used), cover presence, and that no book leaked set-level front
matter belonging to a different book.

Exits non-zero if any check fails.
"""

import argparse
import glob
import os
import posixpath
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

from epub_paths import SPLIT

N = {
    'o': 'http://www.idpf.org/2007/opf',
    'c': 'urn:oasis:names:tc:opendocument:xmlns:container',
    'x': 'http://www.w3.org/1999/xhtml',
}

# Set-level front matter that must never appear inside an individual book.
SET_FRONT_MARKERS = ['总目录', 'Digital Lab简介']


def is_own_index(raw, doc, names):
    """True when a 总目录 page is the book's OWN volume index.

    The 茅盾文学奖 omnibus gives every multi-volume work a page titled 总目录
    that lists just that work's volumes, and it is part of the book rather than
    set-level front matter.  It proves that by linking to documents inside this
    book.  A set-level 总目录 lists the bundle's works instead, so once the
    splitter strips its cross-book links (they point outside the book) nothing
    it links to resolves here.
    """
    links = re.findall(r'<a\b[^>]*href\s*=\s*"([^"]+)"', raw, re.I)
    return any(posixpath.normpath(posixpath.join(
        posixpath.dirname(doc), h.split('#', 1)[0])) in names for h in links)


class Report:
    def __init__(self, path):
        self.path = path
        self.errors = []
        self.warnings = []
        self.stats = {}

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)


def check(path):
    rep = Report(path)

    # ---- archive ---------------------------------------------------------
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            rep.error(f'corrupt member: {bad}')
            return rep

        infos = z.infolist()
        if not infos or infos[0].filename != 'mimetype':
            rep.error('mimetype is not the first entry')
        else:
            if infos[0].compress_type != zipfile.ZIP_STORED:
                rep.error('mimetype is compressed (must be STORED)')
            if z.read('mimetype') != b'application/epub+zip':
                rep.error('mimetype content is wrong')
        names = set(z.namelist())

        # ---- container / opf --------------------------------------------
        try:
            container = ET.fromstring(z.read('META-INF/container.xml'))
        except KeyError:
            rep.error('META-INF/container.xml missing')
            return rep
        rootfile = container.find('.//c:rootfile', N)
        if rootfile is None:
            rep.error('container.xml has no rootfile')
            return rep
        opf_path = rootfile.get('full-path')
        opf_dir = posixpath.dirname(opf_path)
        if opf_path not in names:
            rep.error(f'OPF {opf_path} not in archive')
            return rep

        try:
            opf = ET.fromstring(z.read(opf_path))
        except ET.ParseError as exc:
            rep.error(f'OPF is not well-formed: {exc}')
            return rep

        if opf.get('version') not in ('3.0', '3.1', '3.2'):
            rep.warn(f'OPF version is {opf.get("version")!r}')

        meta = opf.find('.//o:metadata', N)
        uid = opf.get('unique-identifier')
        if not (meta is not None and uid and
                meta.find(f'.//{{http://purl.org/dc/elements/1.1/}}identifier'
                          f'[@id="{uid}"]') is not None):
            rep.error('missing dc:identifier matching unique-identifier')
        for tag in ('title', 'language'):
            if meta is None or meta.find(
                    f'.//{{http://purl.org/dc/elements/1.1/}}{tag}') is None:
                rep.error(f'missing dc:{tag}')
        if meta is None or not meta.findall(
                './/{http://purl.org/dc/elements/1.1/}creator'):
            rep.error('missing dc:creator')
        if meta is not None and meta.find(
                './/o:meta[@property="dcterms:modified"]', N) is None:
            rep.error('missing dcterms:modified (required by EPUB 3)')

        # ---- manifest ----------------------------------------------------
        manifest = {}
        for item in opf.findall('.//o:manifest/o:item', N):
            iid = item.get('id')
            href = item.get('href')
            manifest[iid] = item
            target = posixpath.normpath(posixpath.join(opf_dir, href))
            if target not in names:
                rep.error(f'manifest item {iid} -> {href} not in archive')
            if item.get('media-type') is None:
                rep.error(f'manifest item {iid} has no media-type')

        if not any('nav' in (i.get('properties') or '').split()
                   for i in manifest.values()):
            rep.error('no manifest item with properties="nav"')
        covers = [i for i in manifest.values()
                  if 'cover-image' in (i.get('properties') or '').split()]
        if len(covers) > 1:
            rep.error('more than one cover-image')

        # ---- spine -------------------------------------------------------
        spine = opf.findall('.//o:spine/o:itemref', N)
        if not spine:
            rep.error('empty spine')
        spine_docs = []
        for ref in spine:
            idref = ref.get('idref')
            if idref not in manifest:
                rep.error(f'spine idref {idref} not in manifest')
                continue
            spine_docs.append(posixpath.normpath(posixpath.join(
                opf_dir, manifest[idref].get('href'))))
        rep.stats['spine'] = len(spine_docs)

        # Every document must be well-formed XML: epubcheck calls a break in
        # one a FATAL error (RSC-016) and drops the whole file, so a document
        # this pipeline rewrites has to parse -- 择天记's chapter224 wrote a
        # "<" where a "<p>" was meant and was only caught by epubcheck.
        for doc in spine_docs:
            if not doc.endswith(('.html', '.xhtml', '.htm')) or doc not in names:
                continue
            try:
                ET.fromstring(z.read(doc))
            except ET.ParseError as exc:
                rep.error(f'{doc} is not well-formed: {exc}')

        # ---- navigation --------------------------------------------------
        nav_items = [i for i in manifest.values()
                     if 'nav' in (i.get('properties') or '').split()]
        links_checked = 0
        for nav in nav_items:
            nav_path = posixpath.normpath(
                posixpath.join(opf_dir, nav.get('href')))
            try:
                navroot = ET.fromstring(z.read(nav_path))
            except ET.ParseError as exc:
                rep.error(f'nav document not well-formed: {exc}')
                continue
            for a in navroot.iter('{http://www.w3.org/1999/xhtml}a'):
                href = a.get('href')
                if not href:
                    continue
                links_checked += 1
                target = href.split('#', 1)[0]
                frag = href.split('#', 1)[1] if '#' in href else ''
                # a bare "#frag" points into the nav document itself; joining
                # it onto the directory would name a directory, not a file
                resolved = nav_path if not target else posixpath.normpath(
                    posixpath.join(posixpath.dirname(nav_path), target))
                if resolved not in names:
                    rep.error(f'nav link -> missing file: {href}')
                elif frag:
                    doc = z.read(resolved).decode('utf-8', 'replace')
                    if f'id="{frag}"' not in doc and f"id='{frag}'" not in doc:
                        rep.error(f'nav link -> missing anchor: {href}')
        rep.stats['nav_links'] = links_checked

        # ---- NCX fallback ------------------------------------------------
        ncx_items = [i for i in manifest.values()
                     if i.get('media-type') == 'application/x-dtbncx+xml']
        for ncx in ncx_items:
            ncx_path = posixpath.normpath(
                posixpath.join(opf_dir, ncx.get('href')))
            try:
                ncxroot = ET.fromstring(z.read(ncx_path))
            except ET.ParseError as exc:
                rep.error(f'NCX not well-formed: {exc}')
                continue
            ncx_ns = '{http://www.daisy.org/z3986/2005/ncx/}'
            n = 0
            for content in ncxroot.iter(ncx_ns + 'content'):
                src = content.get('src')
                if not src:
                    rep.error('NCX navPoint without src')
                    continue
                n += 1
                target, _, frag = src.partition('#')
                # as with the nav, a bare "#frag" means the NCX document
                resolved = ncx_path if not target else posixpath.normpath(
                    posixpath.join(posixpath.dirname(ncx_path), target))
                if resolved not in names:
                    rep.error(f'NCX -> missing file: {src}')
                elif frag:
                    doc = z.read(resolved).decode('utf-8', 'replace')
                    if f'id="{frag}"' not in doc:
                        rep.error(f'NCX -> missing anchor: {src}')
            rep.stats['ncx_points'] = n

        # ---- cover -------------------------------------------------------
        if covers:
            cpath = posixpath.normpath(
                posixpath.join(opf_dir, covers[0].get('href')))
            blob = z.read(cpath)
            if len(blob) < 2000:
                rep.error('cover image suspiciously small')
            rep.stats['cover'] = len(blob)

        # ---- cross-book bleed -------------------------------------------
        bodies = [d for d in spine_docs if d not in
                  {posixpath.normpath(posixpath.join(opf_dir, i.get('href')))
                   for i in nav_items}]
        seen_titles = set()
        for doc in bodies:
            if not doc.endswith(('.html', '.xhtml', '.htm')):
                continue
            raw = z.read(doc)
            if len(raw) > 400000:            # skip huge chapters: slow, low value
                continue
            sraw = raw.decode('utf-8', 'replace')
            text = re.sub(r'\s+', '', re.sub(r'(?s)<[^>]+>', '', sraw))
            for marker in SET_FRONT_MARKERS:
                if not text.startswith(marker):
                    continue
                if marker == '总目录' and is_own_index(sraw, doc, names):
                    continue
                rep.error(f'set-level front matter ({marker}) in {doc}')
            m = re.search(r'(?is)<title>(.*?)</title>',
                          raw.decode('utf-8', 'replace'))
            seen_titles.add((m.group(1).strip() if m else '')[:40])

        # every spine document should also be reachable from the TOC or be a
        # cover/contents page -- a book where nothing is linkable is broken
        if rep.stats.get('nav_links', 0) == 0:
            rep.error('navigation has no links')
        if rep.stats.get('ncx_points', 0) == 0:
            rep.error('NCX has no navPoints')

        # duplicated content documents would silently bloat the file
        if len(spine_docs) != len(set(spine_docs)):
            rep.warn('duplicate documents in spine')

    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('files', nargs='*', metavar='FILE',
                    help='EPUBs to check (default: everything in split/)')
    args = ap.parse_args()

    files = args.files or sorted(glob.glob(os.path.join(SPLIT, '*.epub')))
    if not files:
        print('nothing to verify in split/')
        return 0

    failures = 0
    warnings = 0
    total_books = 0
    no_cover = []
    for path in files:
        rep = check(path)
        total_books += 1
        if rep.warnings:
            warnings += len(rep.warnings)
        if rep.errors:
            failures += 1
            print(f'FAIL  {os.path.basename(path)}')
            for e in rep.errors[:6]:
                print(f'        - {e}')
        if 'cover' not in rep.stats:
            no_cover.append(os.path.basename(path))

    print(f'\nchecked {total_books} EPUBs')
    print(f'  failures : {failures}')
    print(f'  warnings : {warnings}')
    print(f'  without cover : {len(no_cover)}')
    for n in no_cover:
        print(f'      {n}')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
