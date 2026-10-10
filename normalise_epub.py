#!/usr/bin/env python3
"""Bring an EPUB up to a standard, professional EPUB 3.

Two jobs, both surgical: the package document, the navigation and the archive's
own bookkeeping change, the words never do.

Repairs -- the defects a Sony/epubBuilder-era EPUB 2 is rejected for:

  * <package> spells the OPF namespace `mlns` instead of `xmlns`, so the whole
    package document sits in no namespace;
  * unique-identifier="PrimaryID" points at a dc:identifier that carries
    neither that id nor a value;
  * the non-standard <dc:builder> / <dc:builder_version> sit in metadata;
  * a stylesheet is declared twice in the manifest;
  * images the chapters link to are missing from the manifest;
  * the stylesheet pulls device fonts through res:// URLs;
  * mimetype ends in CRLF instead of the bare media type;
  * image names are stored as GBK bytes with no UTF-8 flag, so epubcheck cannot
    open the archive at all.

Upgrade to EPUB 3:

  * package/@version -> 3.0, plus the required <meta property="dcterms:modified">;
  * a nav.xhtml built from the book's own NCX outline, declared
    properties="nav" (the NCX is kept for 2.x compatibility);
  * the cover declared properties="cover-image";
  * every content document put through the splitter's own `normalise_html`,
    which is what makes an EPUB-2-era document valid EPUB 3 without touching
    the words.

A cover is never required: a book with none is written without one and noted
NO-COVER.  A file that is already a conformant EPUB 3 is reported OK and
copied through unchanged, so this is safe to run repeatedly.

Usage:
    python3 normalise_epub.py               # top-level EPUBs + everything in split/
    python3 normalise_epub.py FILE...       # just these
    python3 normalise_epub.py --dry-run     # report what would change, write nothing
    python3 normalise_epub.py --out DIR     # write elsewhere (default: split/)
    python3 normalise_epub.py --cover IMG   # force one cover onto one book
"""

import argparse
import os
import posixpath
import re
import shutil
import sys
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import zipfile

from epub_paths import SPLIT, WEB_COVERS
from split_epubs import BUNDLES, finalise_toc, render_nav

NCX_NS = 'http://www.daisy.org/z3986/2005/ncx/'
NS = {'n': NCX_NS}

MEDIA = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.css': 'text/css', '.html': 'application/xhtml+xml',
    '.xhtml': 'application/xhtml+xml', '.ttf': 'application/x-font-ttf',
    '.otf': 'application/x-font-opentype',
}

ITEM = re.compile(r'<item\b[^>]*?/>')
ATTR = re.compile(r'([\w:-]+)="([^"]*)"')
IMG_SRC = re.compile(r'<img\b[^>]*?src="([^"]+)"', re.I)
IMG_TAG = re.compile(r'<img\b[^>]*?>', re.I)
ALT_ATTR = re.compile(r'\balt\s*=', re.I)
FONT_FACE = re.compile(r'@font-face\s*\{[^}]*res://[^}]*\}\s*', re.I)
NCX_UID = re.compile(r'(<meta\s+name="dtb:uid"\s+content=")[^"]*(")', re.I)
DCTERMS = re.compile(r'<meta\b[^>]*\bproperty\s*=\s*"dcterms:modified"', re.I)

NAV_DOC = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">
  <head>
    <meta charset="utf-8"/>
    <title>目录</title>
  </head>
  <body>
    <nav epub:type="toc" id="toc">
      <h1>目录</h1>
{body}
    </nav>
  </body>
</html>
'''


# --------------------------------------------------------------------------
# reading an archive whose member names may be lying
# --------------------------------------------------------------------------

def true_name(info):
    """The archive entry's real name.

    Entries written without the UTF-8 flag hold their bytes verbatim; the
    Chinese ones are GBK, which zipfile decoded as cp437.  Undo that.
    """
    name = info.filename
    if info.flag_bits & 0x800 or all(ord(c) < 128 for c in name):
        return name
    try:
        return name.encode('cp437').decode('gbk')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def read_entries(path):
    with zipfile.ZipFile(path) as z:
        return [(info, true_name(info), z.read(info.filename))
                for info in z.infolist()]


def targets_in(entries):
    """Every in-container file the content documents point at."""
    found = set()
    for _info, name, data in entries:
        if not name.lower().endswith(('.html', '.xhtml', '.htm')):
            continue
        text = data.decode('utf-8', 'replace')
        for src in IMG_SRC.findall(text):
            if src.startswith(('http:', 'https:', 'data:', 'res:')):
                continue
            href = urllib.parse.unquote(src.split('#', 1)[0])
            found.add(posixpath.normpath(
                posixpath.join(posixpath.dirname(name), href)))
    return found


# --------------------------------------------------------------------------
# the package document
# --------------------------------------------------------------------------

META_DC = re.compile(r'<dc:([\w-]+)\b[^>]*>(.*?)</dc:\1>', re.S)
META_DC_SELF = re.compile(r'\s*<dc:([\w-]+)\b[^>]*/>')
#: dc: elements whose value is required and may legitimately be empty/refilled
DC_REQUIRED = {'identifier', 'title', 'language'}
#: a language tag EPUB 3 will accept (BCP 47 shape)
LANG_OK = re.compile(r'^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$')
#: the source spells the language out; the tag it means
LANG_NAMES = {'简体中文': 'zh', '繁体中文': 'zh-Hant', '中文': 'zh',
              '汉语': 'zh', '漢語': 'zh', '简体': 'zh', '繁体': 'zh-Hant'}


def normalise_lang(text):
    """A well-formed dc:language tag, and whether it had to change.

    The Sony-era files spell the language out ("简体中文"), which EPUB 3
    rejects (OPF-092).  This corpus is Chinese, so anything unparseable that is
    not a known name lands on "zh" too; the caller reports the change.
    """
    text = (text or '').strip()
    if LANG_OK.match(text):
        return text, False
    return LANG_NAMES.get(text, 'zh'), True


def manifest_items(s):
    """The manifest's items as dicts, in order, duplicates dropped."""
    m = re.search(r'(<manifest>)(.*?)(</manifest>)', s, re.S)
    if not m:
        return None, None
    kept, seen = [], set()
    for item in ITEM.findall(m.group(2)):
        attrs = dict(ATTR.findall(item))
        href = attrs.get('href')
        if href is None or href in seen:
            continue
        seen.add(href)
        kept.append(attrs)
    return m, kept


def cover_href(s, opf_dir, kept):
    """The href of the book's cover image, from either EPUB generation."""
    for a in kept:
        if 'cover-image' in (a.get('properties') or '').split():
            return posixpath.normpath(posixpath.join(opf_dir, a['href']))
    m = re.search(r'<meta\b[^>]*\bname\s*=\s*"cover"[^>]*\bcontent\s*=\s*"([^"]+)"', s, re.I)
    if m:
        by_id = {a.get('id'): a.get('href') for a in kept}
        href = by_id.get(m.group(1))
        if href:
            return posixpath.normpath(posixpath.join(opf_dir, href))
    for a in kept:
        href = a.get('href', '')
        if re.search(r'(?i)\bcover\b', href):
            return posixpath.normpath(posixpath.join(opf_dir, href))
    return None


def upgrade_opf(s, urn, opf_dir, referenced, names, lang, cover):
    """Repair the package and move it to EPUB 3.  Returns (text, reasons)."""
    reasons = []

    if re.search(r'(?<!x)mlns=', s):
        s = re.sub(r'(?<!x)mlns=', 'xmlns=', s)
        reasons.append('OPF namespace typo "mlns"')

    m = re.search(r'(<package\b[^>]*?)\bversion="([^"]*)"', s)
    version = m.group(2) if m else None
    if version is not None and not version.startswith('3'):
        s = s[:m.start(2)] + '3.0' + s[m.end(2):]
        reasons.append(f'package version {version} -> 3.0')

    if re.search(r'<dc:builder(?:_version)?\b', s):
        s = re.sub(r'<dc:builder(?:_version)?\b[^>]*>.*?</dc:builder(?:_version)?>\s*',
                   '', s, flags=re.S)
        reasons.append('non-standard <dc:builder> removed')

    # a unique-identifier must resolve to a dc:identifier that has a value
    uid_ref = re.search(r'<package\b[^>]*\bunique-identifier\s*=\s*"([^"]+)"', s)
    have = re.search(r'<dc:identifier\b[^>]*\bid\s*=\s*"' +
                     re.escape(uid_ref.group(1)) + r'"[^>]*>\s*([^<]*?)\s*<',
                     s) if uid_ref else None
    if uid_ref and not (have and have.group(1)):
        s = re.sub(r'<dc:identifier\b[^>]*>.*?</dc:identifier>',
                   f'<dc:identifier id="{uid_ref.group(1)}">{urn}</dc:identifier>',
                   s, flags=re.S)
        reasons.append('unique-identifier pointed at no identifier')

    if not DCTERMS.search(s):
        mod = ('  <meta property="dcterms:modified">'
               '2026-01-01T00:00:00Z</meta>\n')
        s = re.sub(r'(<metadata\b[^>]*>)', lambda mm: mm.group(1) + '\n' + mod,
                   s, count=1)
        reasons.append('added the required dcterms:modified')

    # EPUB 3 requires a well-formed language tag...
    lm = re.search(r'<dc:language\b[^>]*>\s*([^<]*?)\s*</dc:language>', s)
    if lm:
        fixed, did = normalise_lang(lm.group(1))
        if did:
            s = s[:lm.start(1)] + fixed + s[lm.end(1):]
            reasons.append(f'dc:language "{lm.group(1)}" -> "{fixed}"')

    # ...and rejects a Dublin Core element with an empty value
    def drop_empty(match):
        if match.group(2).strip() or match.group(1) in DC_REQUIRED:
            return match.group(0)
        return ''
    before = s
    s = META_DC.sub(drop_empty, s)
    s = META_DC_SELF.sub(
        lambda m: m.group(0) if m.group(1) in DC_REQUIRED else '', s)
    if s != before:
        reasons.append('empty Dublin Core element(s) removed')

    m, kept = manifest_items(s)
    if m is None:
        return s, reasons
    if len(ITEM.findall(m.group(2))) != len(kept):
        reasons.append('duplicate manifest item')

    declared = {posixpath.normpath(posixpath.join(opf_dir, a['href']))
                for a in kept}
    missing = sorted(t for t in referenced - declared if t in names)
    if missing:
        reasons.append(f'{len(missing)} referenced file(s) undeclared')
    for n, target in enumerate(missing, 1):
        ext = posixpath.splitext(target)[1].lower()
        kept.append({'id': f'res-{n}', 'href': posixpath.relpath(target, opf_dir),
                     'media-type': MEDIA.get(ext, 'application/octet-stream')})

    # the cover must be declared the EPUB 3 way
    if cover is not None:
        rel = posixpath.relpath(cover, opf_dir)
        hit = next((a for a in kept if a.get('href') == rel), None)
        if hit is None:
            kept.append({'id': 'cover-image', 'href': rel,
                         'media-type': MEDIA.get(
                             posixpath.splitext(cover)[1].lower(), 'image/jpeg'),
                         'properties': 'cover-image'})
            reasons.append('cover was undeclared')
        elif 'cover-image' not in (hit.get('properties') or '').split():
            hit['properties'] = (hit.get('properties', '') + ' cover-image').strip()
            reasons.append('cover not declared properties="cover-image"')

    # a nav document, unless the book already has one
    nav_href = posixpath.relpath(posixpath.join(opf_dir, 'nav.xhtml'), opf_dir)
    if not any('nav' in (a.get('properties') or '').split() for a in kept):
        kept.append({'id': 'nav', 'href': nav_href,
                     'media-type': 'application/xhtml+xml', 'properties': 'nav'})
        reasons.append('no EPUB 3 navigation document')

    body = '\n' + '\n'.join(
        '  <item' + ''.join(f' {k}="{a[k]}"'
                            for k in ('id', 'href', 'media-type', 'properties')
                            if k in a) + '/>'
        for a in kept) + '\n'
    s = s[:m.start(2)] + body + s[m.end(2):]
    return s, reasons


# --------------------------------------------------------------------------
# navigation, from the book's own NCX
# --------------------------------------------------------------------------

def ncx_tree(ncx_bytes, ncx_dir, names):
    """The NCX outline as finalise_toc's node shape."""
    try:
        root = ET.fromstring(ncx_bytes)
    except ET.ParseError:
        return []

    def node(point):
        label = point.find('n:navLabel/n:text', NS)
        src = point.find('n:content', NS).get('src') or ''
        file, _, frag = src.partition('#')
        target = posixpath.normpath(posixpath.join(ncx_dir, file)) if file else ''
        return {'title': (label.text or '').strip() if label is not None else '',
                'file': target if target in names else None,
                'frag': frag,
                'children': [node(k) for k in point.findall('n:navPoint', NS)]}

    navmap = next((e for e in root.iter()
                   if e.tag.rsplit('}', 1)[-1] == 'navMap'), None)
    if navmap is None:
        return []
    return [node(p) for p in navmap.findall('n:navPoint', NS)]


# --------------------------------------------------------------------------
# the content documents
# --------------------------------------------------------------------------

def repair_markup(raw):
    """Give every <img> an alt attribute -- XHTML requires one."""
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw

    def fix(m):
        tag = m.group(0)
        if ALT_ATTR.search(tag):
            return tag
        head = tag[:-2] if tag.endswith('/>') else tag[:-1]
        tail = '/>' if tag.endswith('/>') else '>'
        return head.rstrip() + ' alt=""' + tail

    return IMG_TAG.sub(fix, text).encode('utf-8')


def repair_css(raw):
    return FONT_FACE.sub('', raw.decode('utf-8')).encode('utf-8')


def repair_ncx(raw, urn):
    """Point the NCX identity at the package identity."""
    return NCX_UID.sub(lambda m: m.group(1) + urn + m.group(2),
                       raw.decode('utf-8')).encode('utf-8')


# --------------------------------------------------------------------------
# the transformation
# --------------------------------------------------------------------------

def analyse(src, cover_bytes=None, cover_name=None):
    """Everything `src` needs, computed without writing.

    Returns (reasons, entries, patched, uid).  An empty `reasons` means the
    file is already a conformant EPUB 3 and `patched` should be ignored.
    """
    from split_epubs import normalise_html

    entries = read_entries(src)
    if not entries:
        raise ValueError('empty archive')
    names = [n for _i, n, _d in entries]
    nameset = set(names)
    opf_name = next((n for n in names if n.endswith('.opf')), None)
    if opf_name is None:
        raise ValueError('no package document')
    opf_dir = posixpath.dirname(opf_name)

    reasons = []
    if not (names[0] == 'mimetype'
            and dict((n, d) for _i, n, d in entries).get('mimetype')
            == b'application/epub+zip'):
        reasons.append('mimetype is not a bare first entry')
    if any(true_name(i) != i.filename for i, _n, _d in entries):
        reasons.append('archive member names not stored as UTF-8')

    raw_opf = next(d for _i, n, d in entries if n == opf_name).decode('utf-8')
    urn = 'urn:uuid:' + str(uuid.uuid5(
        uuid.NAMESPACE_URL, 'epub-normalise:' + os.path.basename(src)))
    lang_m = re.search(r'<dc:language\b[^>]*>\s*([^<]*?)\s*<', raw_opf)
    lang, _ = normalise_lang(lang_m.group(1) if lang_m else 'zh')

    _, kept = manifest_items(raw_opf)
    cover = cover_href(raw_opf, opf_dir, kept or []) if kept else None
    if cover is None and cover_name:
        cover = posixpath.join(opf_dir, cover_name)

    new_opf, opf_reasons = upgrade_opf(raw_opf, urn, opf_dir, targets_in(entries),
                                       nameset, lang, cover)
    reasons += opf_reasons

    ncx_name = next((n for n in names if n.lower().endswith('.ncx')), None)
    ncx_dir = posixpath.dirname(ncx_name) if ncx_name else opf_dir

    patched = {}
    for info, name, data in entries:
        if name == opf_name:
            patched[name] = new_opf.encode('utf-8')
        elif name.lower().endswith('.css'):
            fixed = repair_css(data)
            if fixed != data:
                reasons.append('device fonts (res://) in the stylesheet')
            patched[name] = fixed
        elif name.lower().endswith('.ncx'):
            patched[name] = repair_ncx(data, urn)
        elif name.lower().endswith(('.html', '.xhtml', '.htm')):
            fixed = normalise_html(repair_markup(data), name, nameset)
            patched[name] = fixed

    if ncx_name and not any(n == posixpath.join(opf_dir, 'nav.xhtml')
                            for n in names):
        tree = finalise_toc(ncx_tree(next(d for _i, n, d in entries
                                          if n == ncx_name),
                                     ncx_dir, nameset), opf_dir)
        patched[posixpath.join(opf_dir, 'nav.xhtml')] = (
            NAV_DOC.format(lang=lang, body=render_nav(tree))).encode('utf-8')

    if cover_bytes is not None and cover is not None:
        patched[cover] = cover_bytes

    return reasons, entries, patched, urn, cover


def write_epub(dst, entries, patched):
    """Write the rebuilt archive: mimetype first and stored, the rest as-is.

    A member that `patched` adds but the source archive does not have -- the
    generated nav.xhtml -- is written too.
    """
    tmp = dst + '.tmp'
    os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
    written = set()
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as out:
        mime = zipfile.ZipInfo('mimetype', date_time=(1980, 1, 1, 0, 0, 0))
        mime.compress_type = zipfile.ZIP_STORED
        out.writestr(mime, b'application/epub+zip')
        for info, name, data in entries:
            if name == 'mimetype':
                continue
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            out.writestr(zi, patched.get(name, data))
            written.add(name)
        for name in sorted(set(patched) - written):
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(zi, patched[name])
    os.replace(tmp, dst)


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def is_bundle(path):
    """True for a file the splitter owns -- `correct` must leave it alone."""
    base = os.path.basename(path)
    return any(cfg['match'] in base for cfg in BUNDLES)


def free_name(out_dir, base):
    """`base`, or "base (2)" / "base (3)" ... if that name is taken."""
    stem, ext = os.path.splitext(base)
    name, n = base, 1
    while os.path.exists(os.path.join(out_dir, name)):
        n += 1
        name = f'{stem} ({n}){ext}'
    return name


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('files', nargs='*', metavar='FILE',
                    help='EPUBs to normalise (default: the top-level EPUBs '
                         'that are not bundles, plus everything in split/)')
    ap.add_argument('--out', metavar='DIR', default=SPLIT,
                    help='write here (default: %(default)s)')
    ap.add_argument('--cover', metavar='IMAGE',
                    help='force this image as the cover of the one input')
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would change, write nothing')
    args = ap.parse_args()

    if args.files:
        todo = [f for f in args.files if os.path.exists(f)]
        gone = [f for f in args.files if not os.path.exists(f)]
        for f in gone:
            print(f'MISS  {f}: no such file')
        if gone:
            return 1
    else:
        from epub_paths import inputs, outputs
        todo = [f for f in inputs() if not is_bundle(f)] + outputs()
        todo = sorted(set(todo))

    if not todo:
        print('nothing to normalise')
        return 0

    if args.cover and len(todo) != 1:
        print('--cover needs exactly one input')
        return 1

    failures = 0
    changed = 0
    for src in todo:
        base = os.path.basename(src)
        in_split = os.path.dirname(os.path.abspath(src)) == \
            os.path.abspath(args.out)
        cover_bytes = cover_name = None
        if args.cover:
            cover_bytes = open(args.cover, 'rb').read()
            cover_name = posixpath.join('images',
                                        os.path.basename(args.cover))
        elif WEB_COVERS and os.path.isdir(WEB_COVERS):
            stem = os.path.splitext(base)[0]
            for ext in ('.jpg', '.jpeg', '.png'):
                cand = os.path.join(WEB_COVERS, stem + ext)
                if os.path.exists(cand) and os.path.getsize(cand) > 2000:
                    cover_bytes = open(cand, 'rb').read()
                    cover_name = posixpath.join('images', 'cover' + ext)
                    break
        try:
            reasons, entries, patched, _uid, cover = analyse(
                src, cover_bytes, cover_name)
        except Exception as exc:                       # unreadable archive
            print(f'FAIL  {base}: {exc}')
            failures += 1
            continue

        if not reasons:
            if in_split:
                print(f'OK    {base} (already EPUB 3)')
            elif args.dry_run:
                print(f'OK    {base} (already EPUB 3; would copy into {args.out}/)')
            else:
                dst = os.path.join(args.out, free_name(args.out, base))
                os.makedirs(args.out, exist_ok=True)
                shutil.copyfile(src, dst)
                print(f'OK    {base} (already EPUB 3; copied in)')
            continue

        changed += 1
        verb = 'would rewrite' if args.dry_run else 'rewrote'
        print(f'{verb} {base} -> EPUB 3')
        for r in reasons:
            print(f'        - {r}')
        if cover is None:
            print('        - NO-COVER (none found in the book)')
        if args.dry_run:
            continue
        # a book already in the output directory is rewritten in place; only a
        # coming-in book needs a free name so it cannot clobber a finished one
        dst = os.path.join(args.out, base)
        if os.path.abspath(dst) != os.path.abspath(src):
            dst = os.path.join(args.out, free_name(args.out, base))
        write_epub(dst, entries, patched)

    print(f'\n{len(todo)} file(s): {changed} rewritten, '
          f'{failures} failed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
