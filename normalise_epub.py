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
  * EPUB 2's per-element `opf:role` / `opf:file-as` / `opf:scheme` still hang off
    the Dublin Core elements, which EPUB 3 forbids -- Calibre writes all three,
    and epubcheck rejects each one;
  * a stylesheet is declared twice in the manifest;
  * images the chapters link to are missing from the manifest;
  * the stylesheet pulls device fonts through res:// URLs;
  * the stylesheet also reaches for font files the book does not carry -- that
    one src goes, and the rule's local() fallbacks are kept, so a reader that
    has the device font still matches it;
  * a <title> with nothing in it -- RSC-005, and every page of an epubBuilder
    book has one.  The document's own <h1> names the page, and the package's
    dc:title covers the pages that have no heading;
  * a data-* attribute HTML does not define, which a Kindle-made book leaves on
    its landmarks nav;
  * a `properties` token EPUB 3 does not define, on a manifest item or on a
    spine <itemref> -- a Duokan book tags two of the latter
    properties="duokan-page-fullscreen";
  * an NCX whose playOrder disagrees between navPoints naming the same target,
    or leaves gaps in the sequence;
  * mimetype ends in CRLF instead of the bare media type;
  * image names are stored as GBK bytes with no UTF-8 flag, so epubcheck cannot
    open the archive at all.

Upgrade to EPUB 3:

  * package/@version -> 3.0, plus the required <meta property="dcterms:modified">;
  * a nav.xhtml built from the book's own NCX outline, declared
    properties="nav" (the NCX is kept for 2.x compatibility);
  * the cover declared properties="cover-image";
  * properties="svg" on a content document that carries inline SVG;
  * the NCX's identity reconciled with the package's -- the package's own
    dc:identifier wins, and an NCX that already agrees is left alone;
  * every content document put through the splitter's own `normalise_html`,
    which is what makes an EPUB-2-era document valid EPUB 3 without touching
    the words.

A cover is never required: a book with none is written without one and noted
NO-COVER.  A file that is already a conformant EPUB 3 *and* free of the repairs
above is reported OK and copied through unchanged, so this is safe to run
repeatedly.

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
from xml.sax.saxutils import escape

from epub_paths import SPLIT, WEB_COVERS
from split_epubs import (BUNDLES, collapse_same_target, duplicate_ids,
                         finalise_toc, fix_dead_css, invalid_data_attrs,
                         nav_aria_attrs, ncx_document, render_nav)

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
NCX_UID = re.compile(r'(<meta\s+name="dtb:uid"\s+content=")[^"]*(")', re.I)
# Calibre writes <meta content="..." name="dtb:uid"/> -- attributes in the
# other order -- which the pattern above (written name-first) cannot see, so
# that book keeps a foreign identity and epubcheck reports NCX-001 forever.
# The repair therefore reads the attributes instead of matching their order.
META_TAG = re.compile(r'(?is)<meta\b[^>]*>')
META_ATTR = re.compile(r'''([\w:-]+)\s*=\s*(?:"[^"]*"|'[^']*')''')
CONTENT_ATTR = re.compile(r'''(\bcontent\s*=\s*)(?:"[^"]*"|'[^']*')''')
DCTERMS = re.compile(r'<meta\b[^>]*\bproperty\s*=\s*"dcterms:modified"', re.I)

# EPUB 3 defines exactly these properties on a manifest <item>, and these on a
# spine <itemref>; epubcheck rejects any other token as OPF-027.  A Duokan-made
# book tags two itemrefs `duokan-page-fullscreen`.
ITEM_PROPERTIES = frozenset((
    'cover-image', 'mathml', 'nav', 'remote-resources', 'scripted', 'svg',
    'switch'))
ITEMREF_PROPERTIES = frozenset(('page-spread-left', 'page-spread-right'))
ITEMREF_PROP = re.compile(
    r'''(?is)(<itemref\b[^>]*?)\s+properties\s*=\s*["']([^"']*)["']''')

# an empty <title> is RSC-005; the document's own <h1> is the natural filler
TITLE_MATCH = re.compile(r'(?is)<title\s*/\s*>|<title\s*>(.*?)</title\s*>')
H1_MATCH = re.compile(r'(?is)<h1\b[^>]*>(.*?)</h1\s*>')
MARKUP = re.compile(r'(?s)<[^>]*>')

# navPoints naming the same document must agree on playOrder -- see repair_ncx
NAVPOINT_OPEN = re.compile(r'(?is)<navPoint\b([^>]*?)(/?>)')
PLAYORDER = re.compile(r'''(?i)\s+playOrder\s*=\s*["']([^"']*)["']''')

NAV_DOC = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">
  <head>
    <meta charset="utf-8"/>
    <title>{heading}</title>
  </head>
  <body>
    <nav epub:type="toc" id="toc">
      <h1>{heading}</h1>
{body}
    </nav>
  </body>
</html>
'''


def nav_heading(lang):
    """The contents-page title in the book's own language.

    The nav document's <h1> is what a reader sees open the navigation, so it
    must be in the book's language: 目录 for a Chinese volume, Contents
    otherwise.  Leaving the English books a Chinese heading is the one mark of
    a template written for the Chinese bundles.
    """
    return '目录' if lang.startswith('zh') else 'Contents'


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


def is_image_item(a):
    """Whether a manifest item is an image, by media-type or by extension.

    Only an image can be the cover image.  A book whose cover *document* is
    called cover.xhtml is not declaring that document to be the cover image,
    however much its name suggests it.
    """
    media = (a.get('media-type') or '').strip().lower()
    if not media:
        media = MEDIA.get(posixpath.splitext(a.get('href', ''))[1].lower(), '')
    return media.startswith('image/')


def cover_href(s, opf_dir, kept):
    """The href of the book's cover image, from either EPUB generation."""
    for a in kept:
        if ('cover-image' in (a.get('properties') or '').split()
                and is_image_item(a)):
            return posixpath.normpath(posixpath.join(opf_dir, a['href']))

    # EPUB 2 declares it with <meta name="cover" content="ID"/>.  The two
    # attributes come in either order, so take the tag apart rather than
    # matching one spelling of it.
    by_id = {a.get('id'): a for a in kept}
    by_href = {a.get('href'): a for a in kept}
    for tag in re.findall(r'<meta\b[^>]*>', s, re.I):
        attrs = dict(ATTR.findall(tag))
        if attrs.get('name', '').strip().lower() != 'cover':
            continue
        want = attrs.get('content', '').strip()
        item = by_id.get(want) or by_href.get(want)
        if item is not None and is_image_item(item):
            return posixpath.normpath(posixpath.join(opf_dir, item['href']))

    # nothing usable declared: the image whose name mentions the cover
    for a in kept:
        if is_image_item(a) and re.search(r'(?i)\bcover\b', a.get('href', '')):
            return posixpath.normpath(posixpath.join(opf_dir, a['href']))
    return None


OPF2_ATTR = re.compile(r'\s+opf:([\w-]+)="([^"]*)"')
# only these two have somewhere to go in EPUB 3; the rest are recorded nowhere
OPF2_REFINABLE = ('role', 'file-as')
DC_OPEN = re.compile(r'<dc:([\w-]+)\b([^>]*?)(/?)>', re.I)


def demote_opf2_attributes(s):
    """EPUB 2's per-element opf:* attributes -> EPUB 3 <meta refines=...>.

    EPUB 2 hung `opf:role`, `opf:file-as` and `opf:scheme` off the Dublin Core
    elements themselves.  EPUB 3 allows none of them: the first two become
    <meta> elements refining the element they sat on, which therefore has to
    gain an id, and the rest are dropped outright, there being nowhere in
    EPUB 3 to record them.  A Calibre-made book carries four of these, which is
    five epubcheck RSC-005 errors -- one creator element carries two.

    `opf:event` is the one that needs care.  Calibre stamps it on a *second*
    dc:date (`<dc:date opf:event="modification">`), and epubcheck rejects the
    attribute as RSC-005 -- and then rejects the element that carries it too,
    so one stray attribute costs two errors.  The date itself is kept: only the
    spelling of what it means is pre-EPUB 3.

    Returns (text, reasons).
    """
    refines = []
    dropped = []
    named = [0]

    def convert(m):
        tag, attrs, selfclose = m.group(1), m.group(2), m.group(3)
        found = OPF2_ATTR.findall(attrs)
        if not found:
            return m.group(0)
        attrs = OPF2_ATTR.sub('', attrs)
        kept = [(n, v) for n, v in found if n in OPF2_REFINABLE]
        dropped.extend(n for n, _v in found if n not in OPF2_REFINABLE)
        ident = None
        if kept:                       # only a refined element needs the id
            idm = re.search(r'\bid="([^"]*)"', attrs)
            if idm:
                ident = idm.group(1)
            else:
                named[0] += 1
                ident = f'opf2-{tag}-{named[0]}'
                attrs = attrs.rstrip() + f' id="{ident}"'
        for name, value in kept:
            if name == 'role':
                refines.append(f'    <meta refines="#{ident}" '
                               f'property="role" scheme="marc:relators"'
                               f'>{value}</meta>')
            else:
                refines.append(f'    <meta refines="#{ident}" '
                               f'property="file-as">{value}</meta>')
        return f'<dc:{tag}{attrs}{selfclose}>'

    out = DC_OPEN.sub(convert, s)
    reasons = []
    if refines:
        out = re.sub(r'(</metadata\s*>)',
                     lambda m: '\n'.join(refines) + '\n' + m.group(1),
                     out, count=1)
        reasons.append('EPUB 2 opf: attribute(s) moved into <meta refines>')
    if dropped:
        reasons.append('EPUB 2 opf: attribute(s) with no EPUB 3 equivalent '
                       'dropped: ' + ', '.join(sorted(set(dropped))))
    return out, reasons


def strip_itemref_properties(s):
    """Drop a spine <itemref> property EPUB 3 does not define.

    The spine is the one part of the package upgrade_opf does not rebuild, so
    an unknown token here survives on its own -- and epubcheck calls it OPF-027.
    Returns (text, reasons).
    """
    dropped = []

    def fix(m):
        tokens = m.group(2).split()
        keep = [t for t in tokens if t in ITEMREF_PROPERTIES]
        dropped.extend(t for t in tokens if t not in ITEMREF_PROPERTIES)
        return m.group(1) + (f' properties="{" ".join(keep)}"' if keep else '')

    out = ITEMREF_PROP.sub(fix, s)
    if not dropped:
        return s, []
    return out, ['undefined itemref properties token(s) removed: '
                 + ', '.join(sorted(set(dropped)))]


# EPUB 3 allows exactly ONE dc:date element (epubcheck RSC-005 on the second).
# A Calibre EPUB 2 book carries two: the publication date and a second one
# meaning "modification", which `opf:event` used to say and which EPUB 3 says
# with the dcterms:modified this upgrade adds anyway.  So the extra element is
# removed, not merged, and the first -- the publication date -- is the one kept.
DATE_ELEMENT = re.compile(r'(?is)<dc:date\b[^>]*>.*?</dc:date\s*>'
                          r'|<dc:date\b[^>]*/\s*>')


def keep_one_date(s):
    """Leave the package with a single dc:date.  Returns (text, dropped)."""
    seen = [0]

    def fix(m):
        seen[0] += 1
        return m.group(0) if seen[0] == 1 else ''

    out = DATE_ELEMENT.sub(fix, s)
    return out, max(0, seen[0] - 1)


def package_identifier(opf_text):
    """The value of the dc:identifier that package/@unique-identifier names."""
    ref = re.search(r'<package\b[^>]*\bunique-identifier\s*=\s*"([^"]+)"',
                    opf_text)
    if not ref:
        return None
    m = re.search(r'<dc:identifier\b[^>]*\bid\s*=\s*"' +
                  re.escape(ref.group(1)) + r'"[^>]*>\s*([^<]*?)\s*<',
                  opf_text)
    return m.group(1) if m else None


def upgrade_opf(s, urn, opf_dir, referenced, names, lang, cover,
                svg_docs=frozenset()):
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

    s, opf2_reasons = demote_opf2_attributes(s)
    reasons += opf2_reasons

    s, date_dropped = keep_one_date(s)
    if date_dropped:
        reasons.append(f'{date_dropped} extra dc:date element(s) dropped '
                       f'(EPUB 3 allows one)')

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

    # an undefined property token is OPF-027; drop it before we rebuild the
    # manifest, and before the additions below put the ones we do want on
    unknown = set()
    for a in kept:
        tokens = (a.get('properties') or '').split()
        allowed = [t for t in tokens if t in ITEM_PROPERTIES]
        unknown.update(t for t in tokens if t not in ITEM_PROPERTIES)
        if allowed:
            a['properties'] = ' '.join(allowed)
        else:
            a.pop('properties', None)
    if unknown:
        reasons.append('undefined properties token(s) removed: '
                       + ', '.join(sorted(unknown)))

    declared = {posixpath.normpath(posixpath.join(opf_dir, a['href']))
                for a in kept}
    missing = sorted(t for t in referenced - declared if t in names)
    if missing:
        reasons.append(f'{len(missing)} referenced file(s) undeclared')
    for n, target in enumerate(missing, 1):
        ext = posixpath.splitext(target)[1].lower()
        kept.append({'id': f'res-{n}', 'href': posixpath.relpath(target, opf_dir),
                     'media-type': MEDIA.get(ext, 'application/octet-stream')})

    # A content document carrying inline SVG has to say so in the manifest, or
    # epubcheck raises OPF-014 against the document.
    svg_bare = [a for a in kept
                if posixpath.normpath(posixpath.join(opf_dir, a['href']))
                in svg_docs
                and 'svg' not in (a.get('properties') or '').split()]
    if svg_bare:
        for a in svg_bare:
            a['properties'] = (a.get('properties', '') + ' svg').strip()
        reasons.append(f'{len(svg_bare)} document(s) use SVG but did not '
                        f'declare properties="svg"')

    # properties="cover-image" belongs on one image and on nothing else.  A
    # book may arrive with it on its cover *document* -- the page whose only
    # job is to hold the <img> -- which leaves verify_epubs.py reading a 600
    # byte page as though it were the cover image.  Clear every item the token
    # does not belong on before declaring the one it does.
    rel = posixpath.relpath(cover, opf_dir) if cover is not None else None
    stray = [a for a in kept
             if 'cover-image' in (a.get('properties') or '').split()
             and (a.get('href') != rel or not is_image_item(a))]
    for a in stray:
        keep = [t for t in a['properties'].split() if t != 'cover-image']
        if keep:
            a['properties'] = ' '.join(keep)
        else:
            a.pop('properties', None)
    if stray:
        reasons.append(f'cover-image removed from {len(stray)} item(s) it does '
                       f'not belong on')

    # the cover must be declared the EPUB 3 way
    if cover is not None:
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
    s, ref_reasons = strip_itemref_properties(s)
    return s, reasons + ref_reasons


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


def empty_title(blob):
    """True when the document's <title> carries nothing at all."""
    m = TITLE_MATCH.search(blob.decode('utf-8', 'replace'))
    return m is not None and (m.group(1) is None or not m.group(1).strip())


def fill_empty_title(blob, fallback):
    """Give an empty <title> the document's own heading, or the book's name.

    `<title></title>` is RSC-005, and an epubBuilder-era book leaves every page
    that way.  The page's own <h1> keeps each document announcing itself, and
    the package's dc:title covers the pages that have no heading.  A title that
    already says something is never touched.
    """
    if not empty_title(blob):
        return blob
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob
    heading = H1_MATCH.search(raw)
    title = ' '.join(MARKUP.sub(' ', heading.group(1)).split()) if heading else ''
    title = title or fallback
    if not title:
        return blob
    return TITLE_MATCH.sub(lambda m: '<title>' + escape(title) + '</title>',
                           raw, count=1).encode('utf-8')


def play_orders(ncx_text, ncx_dir):
    """The playOrder every navPoint should carry, in document order.

    epubcheck has two rules here, and together they fix the numbering: two
    navPoints that resolve to the same reference must give the same playOrder,
    and the values must then run 1..n with no gaps.  So the order belongs to
    the reference rather than to the navPoint, and references are numbered by
    first appearance -- the rule render_ncx() already follows when this project
    writes an NCX of its own.  The fragment counts as part of the reference:
    two anchors in one document are different targets and must not collide.

    An NCX that carries no playOrder at all is left alone: nothing is
    inconsistent, and stamping orders onto a book that opted out of them is a
    change the caller did not ask for.  Returns [] for that case, and for an
    NCX with no navPoints; None if it will not parse.
    """
    try:
        root = ET.fromstring(ncx_text)
    except ET.ParseError:
        return None
    keys, numbered = [], False
    for point in root.iter():
        if point.tag.rsplit('}', 1)[-1] != 'navPoint':
            continue
        content = point.find('n:content', NS)
        src = (content.get('src') if content is not None else '') or ''
        file, _, frag = src.partition('#')
        target = posixpath.normpath(
            posixpath.join(ncx_dir, file)) if file else ''
        keys.append(target + '#' + frag)
        numbered = numbered or point.get('playOrder') is not None
    if not numbered:
        return []
    seen = {}
    return [seen.setdefault(key, len(seen) + 1) for key in keys]


# an XML Name, which is what an `id` value has to be
XML_NAME = re.compile(r'^[A-Za-z_][\w.\-]*$')
NCX_ID = re.compile(r'''(\sid\s*=\s*)(?:"([^"]*)"|'([^']*)')''')


def fix_ncx_ids(text):
    """Give every NCX element an id that is an XML name, and a unique one.

    A converter that numbers its navPoints "001", "002" ... writes an `id` that
    starts with a digit, which no XML name may do; epubcheck rejects every one
    of them as RSC-005 ("value of attribute id is invalid").  Nothing
    references a navPoint's id -- reading order is `playOrder`, a target is
    `content/@src` -- so they are renamed rather than dropped: a fresh `npN`
    for an id that cannot stay, and the first holder keeps its name when two
    navPoints share one.

    Returns (text, renamed-count).
    """
    used, fresh = set(), [0]
    renamed = [0]

    def fix_attr(m):
        prefix, value = m.group(1), m.group(2) if m.group(2) is not None \
            else m.group(3)
        quote = '"' if m.group(2) is not None else "'"
        if XML_NAME.match(value) and value not in used:
            used.add(value)
            return m.group(0)
        while True:
            fresh[0] += 1
            candidate = f'np{fresh[0]}'
            if candidate not in used:
                break
        used.add(candidate)
        renamed[0] += 1
        return f'{prefix}{quote}{candidate}{quote}'

    out = NCX_ID.sub(fix_attr, text)
    return out, renamed[0]


def set_ncx_uid(text, ident):
    """Stamp the package identifier on the NCX's dtb:uid meta, in any order.

    Returns (text, whether anything was rewritten).
    """
    changed = [0]

    def fix(m):
        tag = m.group(0)
        names = {a.split('=', 1)[0].strip().lower()
                 for a in META_ATTR.findall(tag)}
        if 'name' not in names or 'content' not in names:
            return tag
        if re.search(r'''\bname\s*=\s*["']dtb:uid["']''', tag, re.I) is None:
            return tag
        new = CONTENT_ATTR.sub(lambda c: c.group(1) + f'"{ident}"', tag, count=1)
        if new != tag:
            changed[0] += 1
        return new

    return META_TAG.sub(fix, text), bool(changed[0])


def repair_ncx(raw, ident, ncx_dir):
    """Bring the NCX into line with the package: identity, order and ids.

    Three independent repairs, any of which may be a no-op:

    * the NCX has to carry the package's own identity -- an NCX naming a
      different uuid is NCX-001.  Left alone when it names no identity at all:
      there is nothing to reconcile, and inventing one would be a change the
      caller did not ask for;
    * navPoints have to agree on playOrder per reference and number 1..n with
      no gaps (see play_orders).  An NCX that already does comes back
      byte-for-byte;
    * a navPoint `id` has to be an XML name and has to be unique (see
      fix_ncx_ids).

    Returns (NCX bytes, reasons).
    """
    reasons = []
    text = raw.decode('utf-8')
    text, _uid_fixed = set_ncx_uid(text, ident)

    text, renamed = fix_ncx_ids(text)
    if renamed:
        reasons.append(f'{renamed} NCX id(s) that were not XML names renamed')

    orders = play_orders(text, ncx_dir)
    if orders:
        pending = iter(orders)

        def renumber(m):
            attrs, tail = m.group(1), m.group(2)
            order = next(pending, None)
            if order is None:
                return m.group(0)
            current = PLAYORDER.search(attrs)
            if current is None:
                return '<navPoint' + attrs + f' playOrder="{order}"' + tail
            if current.group(1) == str(order):
                return m.group(0)
            return '<navPoint' + PLAYORDER.sub(
                f' playOrder="{order}"', attrs, count=1) + tail

        text = NAVPOINT_OPEN.sub(renumber, text)
    if reasons or text.encode('utf-8') != raw:
        reasons.insert(0, 'NCX brought into line with the package')
    return text.encode('utf-8'), reasons


# --------------------------------------------------------------------------
# links that point at an anchor the book does not carry
# --------------------------------------------------------------------------

HREF_ATTR = re.compile(r'''(\shref\s*=\s*)(?:"([^"]*)"|'([^']*)')''')
ANCHOR_DEF = re.compile(r'''\s(?:id|name)\s*=\s*(?:"([^"]*)"|'([^']*)')''')
EXTERNAL_HREF = re.compile(r'^(?:[A-Za-z][A-Za-z0-9+.\-]*:|//)')


def anchors(blob):
    """Every fragment target a document defines: its ids and <a name=>s."""
    raw = blob.decode('utf-8', 'replace')
    found = set()
    for m in ANCHOR_DEF.finditer(raw):
        found.add(m.group(1) if m.group(1) is not None else m.group(2))
    return found


def repair_fragments(blob, path, targets, names):
    """Drop the fragment of a link whose anchor the target document lacks.

    A converted book keeps footnote references that point at an anchor the
    notes document does not define -- `_031.htm#chap3_132` for a notes file
    whose ids stop at `chap3_13`.  epubcheck calls that RSC-012 and refuses the
    book.  The reference itself is real (it says "note 132"), so the link is
    kept and only the dead `#fragment` goes: the reader still lands on the
    notes document.  Nothing else is touched -- a link to a file the book does
    not carry at all is a different defect, repaired elsewhere.

    Returns (blob, list of the fragments dropped).
    """
    from split_epubs import resolve

    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob, []
    dropped = []

    def fix(m):
        prefix = m.group(1)
        value = m.group(2) if m.group(2) is not None else m.group(3)
        quote = '"' if m.group(2) is not None else "'"
        if EXTERNAL_HREF.match(value) or '#' not in value:
            return m.group(0)
        target, _, frag = value.partition('#')
        if not frag:
            return m.group(0)
        resolved = path if not target else resolve(path, target)
        if resolved not in names:
            return m.group(0)                   # dead file: not this repair
        if frag in targets.get(resolved, ()):
            return m.group(0)
        dropped.append(frag)
        return f'{prefix}{quote}{target}{quote}'

    out = HREF_ATTR.sub(fix, raw)
    return (out.encode('utf-8') if dropped else blob), dropped


# --------------------------------------------------------------------------
# the transformation
# --------------------------------------------------------------------------

def analyse(src, cover_bytes=None, cover_name=None):
    """Everything `src` needs, computed without writing.

    Returns (reasons, entries, patched, urn, cover).  An empty `reasons` means
    the file is already a conformant EPUB 3 and `patched` should be ignored.
    """
    from split_epubs import normalise_html

    entries = read_entries(src)
    if not entries:
        raise ValueError('empty archive')
    names = [n for _i, n, _d in entries]
    nameset = set(names)
    # content documents carrying inline SVG, which the manifest must declare
    svg_docs = {n for _i, n, d in entries
                if n.lower().endswith(('.html', '.xhtml', '.htm'))
                and b'<svg' in d.lower()}
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
    # the package's own name, for a document with no <h1> of its own and an
    # empty <title> that still has to say something
    title_m = re.search(r'<dc:title\b[^>]*>\s*([^<]*?)\s*<', raw_opf)
    book_title = title_m.group(1).strip() if title_m else ''

    _, kept = manifest_items(raw_opf)
    cover = cover_href(raw_opf, opf_dir, kept or []) if kept else None
    if cover is None and cover_name:
        cover = posixpath.join(opf_dir, cover_name)

    new_opf, opf_reasons = upgrade_opf(raw_opf, urn, opf_dir, targets_in(entries),
                                       nameset, lang, cover, svg_docs)
    reasons += opf_reasons

    # The NCX has to carry the package's own identity; an NCX still naming a
    # different uuid is epubcheck NCX-001.  The package's identifier is the one
    # that counts, so read it back off the repaired OPF rather than stamping
    # the generated urn over both sides.
    ident = package_identifier(new_opf) or urn

    ncx_name = next((n for n in names if n.lower().endswith('.ncx')), None)
    ncx_dir = posixpath.dirname(ncx_name) if ncx_name else opf_dir

    # The navigation tree, folded once and rendered for both the EPUB 3 nav
    # document and the legacy NCX, so the two agree.  A converter's NCX often
    # lists a chapter twice -- its section marker and its title, both opening
    # the same page -- so the same-target runs are collapsed before either is
    # emitted.  toc_folded records whether the collapse changed anything, so a
    # book with no doubled entries keeps its own NCX and is never re-emitted.
    toc_tree = None
    toc_folded = False
    if ncx_name:
        base = finalise_toc(ncx_tree(next(d for _i, n, d in entries
                                          if n == ncx_name),
                                     ncx_dir, nameset), opf_dir)
        toc_tree = collapse_same_target(base)
        toc_folded = render_nav(base) != render_nav(toc_tree)

    # The book's own navigation document is the one file validated against the
    # nav grammar rather than the content grammar (see drop_nav_aria_attrs).
    nav_docs = {posixpath.normpath(posixpath.join(opf_dir, a['href']))
                for a in (kept or [])
                if 'nav' in (a.get('properties') or '').split()
                and a.get('href')}

    patched = {}
    retitled = 0
    bad_attrs = []
    dupes = []
    aria = []
    touched = 0
    docs = []
    for info, name, data in entries:
        if name == opf_name:
            patched[name] = new_opf.encode('utf-8')
        elif name.lower().endswith('.css'):
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                patched[name] = data
                continue
            # keep_local_fonts: a stylesheet whose @font-face lists device
            # fonts through local() only reaches for the missing file last, so
            # the dead source goes and the rule stays
            fixed = fix_dead_css(text, name, nameset,
                                 keep_local_fonts=True).encode('utf-8')
            if fixed != data:
                reasons.append(f'dead reference(s) removed from {name}')
            patched[name] = fixed
        elif name.lower().endswith('.ncx'):
            if toc_folded:
                fixed = ncx_document(toc_tree, ident, book_title, lang)\
                    .encode('utf-8')
                reasons.append('NCX folded to agree with the navigation document')
            else:
                fixed, ncx_reasons = repair_ncx(data, ident, ncx_dir)
                reasons += ncx_reasons
            patched[name] = fixed
        elif name.lower().endswith(('.html', '.xhtml', '.htm')):
            fixed = normalise_html(repair_markup(data), name, nameset,
                                   nav=name in nav_docs)
            if empty_title(fixed):
                stem = posixpath.splitext(posixpath.basename(name))[0]
                fixed = fill_empty_title(fixed, book_title or stem)
                retitled += 1
            bad_attrs += invalid_data_attrs(data)
            dupes += duplicate_ids(data)
            if name in nav_docs:
                aria += nav_aria_attrs(data)
            if fixed != data:
                touched += 1
            docs.append((info, name, data, fixed))
            patched[name] = fixed

    # A link whose `#fragment` names no anchor in the target document is
    # epubcheck RSC-012.  The repair needs the whole book -- the anchor lives in
    # another document -- so it runs once every document has been normalised
    # and its ids are final.
    targets = {name: anchors(fixed) for _i, name, _d, fixed in docs}
    dead_frags = []
    for info, name, data, fixed in docs:
        repaired, dropped = repair_fragments(fixed, name, targets, nameset)
        dead_frags += dropped
        patched[name] = repaired

    if retitled:
        reasons.append(f'{retitled} empty <title> element(s) filled')
    if bad_attrs:
        reasons.append('invalid custom data attribute(s) dropped: '
                       + ', '.join(sorted(set(bad_attrs))))
    if dupes:
        reasons.append(f'{len(dupes)} duplicate id(s) renamed: '
                       + ', '.join(sorted(set(dupes))[:6]))
    if aria:
        reasons.append('ARIA attribute(s) a navigation document forbids '
                       'dropped: ' + ', '.join(sorted(set(aria))))
    if dead_frags:
        reasons.append(f'{len(dead_frags)} link(s) to an anchor the book does '
                       f'not define: fragment dropped '
                       f'({", ".join(sorted(set(dead_frags))[:6])})')
    if touched and not (retitled or bad_attrs or dupes or aria or dead_frags):
        # nothing named above explains it, but normalise_html did change these
        # documents.  Saying so keeps the "already EPUB 3, copied in" path
        # honest: that path must only ever mean "not one byte changed".
        reasons.append(f'{touched} content document(s) normalised')

    nav_path = posixpath.join(opf_dir, 'nav.xhtml')
    if toc_tree is not None and nav_path not in names:
        # The generated nav carries the NCX's own targets, so it can inherit a
        # dead fragment the NCX had; the same repair applies to it.
        generated, dropped = repair_fragments(
            NAV_DOC.format(lang=lang, heading=nav_heading(lang),
                           body=render_nav(toc_tree)).encode('utf-8'),
            nav_path, targets, nameset)
        if dropped:
            reasons.append(f'{len(dropped)} navigation link(s) to an anchor '
                           f'the book does not define: fragment dropped')
        patched[nav_path] = generated

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

DC_TITLE = re.compile(r'(?is)<dc:title\b[^>]*>(.*?)</dc:title\s*>')


def dc_title(path):
    """The book's own dc:title, or '' if the archive will not give it up."""
    try:
        with zipfile.ZipFile(path) as z:
            opf = next((n for n in z.namelist()
                        if n.lower().endswith('.opf')), None)
            if opf is None:
                return ''
            text = z.read(opf).decode('utf-8', 'replace')
    except (zipfile.BadZipFile, OSError, KeyError):
        return ''
    m = DC_TITLE.search(text)
    return re.sub(r'\s+', ' ', m.group(1)).strip() if m else ''


def is_bundle(path):
    """True for a file the splitter owns -- `correct` must leave it alone.

    The name substring is not enough on its own.  A split book is named
    "<author> - <title>" and a bundle key is usually the author's own name, so
    every book that came out of 崔曼莉's, 铁凝's or 德波顿's set answers the
    substring test and would be skipped for ever.  A bundle ALSO carries the set
    title its entry names, which no single volume out of it does, so that title
    is the second half of the identity test -- which is why an entry's
    `set_title` has to be a substring of the bundle's own dc:title.
    """
    base = os.path.basename(path)
    for cfg in BUNDLES:
        if cfg['match'] in base and cfg['set_title'] in dc_title(path):
            return True
    return False


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
                dst = os.path.join(args.out, base)
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
        # A book lands on its own name, whether it came in from outside or is
        # already in the output directory.  A top-level book is copied in on
        # every run, so taking a free name instead of overwriting would leave
        # "base (2)", "base (3)" ... behind on every run.  The output directory
        # is this tool's own, so re-running rewrites in place.
        dst = os.path.join(args.out, base)
        write_epub(dst, entries, patched)

    print(f'\n{len(todo)} file(s): {changed} rewritten, '
          f'{failures} failed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
