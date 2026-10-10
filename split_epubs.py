#!/usr/bin/env python3
"""
Split multi-book EPUB bundles (合集 / 套装) into one EPUB 3 file per book.

Design notes
------------
Book boundaries come from the bundle's toc.ncx: top-level navPoints map 1:1 to
books.  Each book's content is the contiguous run of spine documents from its
own start file up to the next book's start file.  A PDF-scan bundle instead
holds one work as a single document of page images (see plan_scan_book).

Content documents keep their ORIGINAL archive paths and the OPF is written back
at its ORIGINAL path, so every intra-book hyperlink, stylesheet reference and
image src keeps resolving with no rewriting.  Resources are copied only when the
book's documents actually reference them.

Only the standard library is used (ebooklib/bs4 are not installed).

Usage:
    python3 split_epubs.py            # build everything
    python3 split_epubs.py --list     # show the split plan, write nothing
"""

import argparse
import os
import posixpath
import re
from html import entities as html_entities
import struct
import sys
import uuid
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from epub_paths import SPLIT as OUTDIR, WEB_COVERS, inputs


class ConfigError(Exception):
    """A per-bundle config that cannot be honoured.

    Raised while planning, caught in main() and reported as exit 2, so a bad
    `book_marks` entry cannot kill the whole run with a bare traceback.
    """

NCX_NS = 'http://www.daisy.org/z3986/2005/ncx/'
OPF_NS = 'http://www.idpf.org/2007/opf'
CONT_NS = 'urn:oasis:names:tc:opendocument:xmlns:container'
N = {'n': NCX_NS, 'o': OPF_NS, 'c': CONT_NS}

MEDIA_TYPES = {
    '.xhtml': 'application/xhtml+xml', '.html': 'application/xhtml+xml',
    '.htm': 'application/xhtml+xml', '.css': 'text/css',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
    '.ttf': 'font/ttf', '.otf': 'font/otf',
    '.woff': 'font/woff', '.woff2': 'font/woff2',
    '.ncx': 'application/x-dtbncx+xml', '.js': 'text/javascript',
}

# Top-level NCX entries that describe the SET rather than a book.
FRONT_TITLES = {
    '书名页', '版权页', '目录', '总目录', '版权信息', 'Digital Lab简介',
    '扉页', '前言', '出版说明', '总序', '内容简介',
}

# A PDF-scan EPUB's bookmarks carry the 章 and 节 titles but list every one of
# them at the same level, so 第一章 and its 一/二/三 read as siblings.
SCAN_CHAPTER = re.compile(r'^第[一二三四五六七八九十百零]+[章回]$')
SCAN_SECTION = re.compile(r'^[一二三四五六七八九十百零]+$')


# Per-bundle configuration.  `embedded_cover` opts the bundle into the
# largest-image cover rule, the only one that needs a flag; the reliable cover
# rules run for every bundle (see attach_covers).  `books_page` names the
# bundle's own 总目录 when the NCX cannot supply book boundaries.
BUNDLES = [
    # ------------------------------------------------------------------
    # Per-bundle configuration -- one entry per multi-book bundle.  `match`
    # is a substring test against the source filename; `key` names the output
    # and cover files.  The engine understands:
    #
    #   key, match, author, set_title, embedded_cover   -- always
    #
    #   authors={label: author}   per-work attribution in a multi-author set
    #   cover_page='封面'          bind the cover from the page with this title
    #   cover_page_precedes=True  that page may sit one document *before* the
    #                             navPoint that opens the work: claim it back
    #   cover_pair=True           each work prints two cover pages; take the 2nd
    #   cover_precedes_book=True  each work's cover is a bare page in front of
    #                             it, so the work stops where the next cover
    #                             begins and the pages between belong to none
    #   cover_files={label: member}  pin a cover by hand (beats every heuristic)
    #   books_page=<path>         the bundle's own 总目录 supplies the book list
    #   books_with_children=True  the NCX nests the works under 辑/系列 wrappers,
    #                             so only entries that have children are books
    #   title_fix=fn              clean a nav label into a work title
    #   skip_titles={...}         set-level top-level entries that are not books
    #   drop_tail=[paths]         set-level pages printed after the last work
    #   drop_tail_marker=[text]   cut the last work at this marker's block
    #   book_marks=['file#anchor']  a flat, chapter-level NCX: these anchors
    #                             open the works (see plan_marked_books)
    #   scan_book=dict(...)       a PDF-scan EPUB, the whole work being one
    #                             document of page images (see plan_scan_book)
    #
    # `embedded_cover= True` opts this bundle into the largest-image cover
    # rule, which runs only when the reliable rules find nothing -- a first
    # page that is bare image, or a page titled 封面 (see attach_covers).  It
    # is confirmed by eye and never inferred, because it is the one rule that
    # mis-binds interior plates.  With it False, a book that has no cover of
    # its own is written without one unless EPUB_WEB_COVERS names a directory
    # of "<key>-<NN>.jpg" images (see epub_paths.web_covers).
    # ------------------------------------------------------------------

    # 徐皓峰作品集: six works in one converted file set.  Its NCX is flat --
    # 184 sibling navPoints, one per chapter, with 作者简介 (the set's own
    # author bio) in front -- so nothing marks where a work begins;
    # `book_marks` names the anchor that opens each one, every anchor a
    # `<span>` sitting immediately before that work's cover image.
    # The works are NOT aligned to the files: a converted document carries the
    # tail of one work and the head of the next (text00002.html ends 逝去的武林
    # and starts 道士下山), which is why the splitter cuts documents at a mark.
    # Each work's cover is the single 600x880 image its own pages reference,
    # read by eye: Image00006 逝去的武林, Image00000 道士下山, Image00003
    # 武士会, Image00007 国术馆, Image00001 大日坛城, Image00002 刀背藏身.
    # The set's own cover (Image00004) and its thumbnail (Image00005) are
    # referenced nowhere in the text, so the largest-image rule cannot bind
    # them here.
    # text00021.html ends with the producer's collection-wide "Table of
    # Contents", listing all six works; it belongs to the set, not to the last
    # book, and `drop_tail_marker` cuts that book at it.
    dict(key='xuhaofeng', match='徐皓峰作品集', author='徐皓峰',
         set_title='徐皓峰作品集', embedded_cover=True,
         book_marks=[
             'text00000.html#filepos0000001232',      # 逝去的武林
             'text00002.html#filepos0000471254',      # 道士下山
             'text00005.html#filepos0001157981',      # 武士会
             'text00008.html#filepos0001890028',      # 国术馆
             'text00012.html#filepos0002646370',      # 大日坛城
             'text00017.html#filepos0003747219',      # 刀背藏身
         ],
         drop_tail_marker=['Table of Contents']),

    # 崔曼莉小说集 (全4册): the ordinary case, and the contrast with the entry
    # above.  Its NCX is good enough for the engine on its own -- four
    # top-level navPoints, one per work, each a contiguous run of spine
    # documents -- so it needs neither `book_marks` nor `books_page`.
    #
    # The NCX is real but NOT declared in the OPF manifest, a legacy EPUB 2
    # defect; the splitter reads toc.ncx by path rather than through the
    # manifest, so that costs nothing here.
    #
    # Note the four navPoints are not four volumes of one novel: 浮沉.1 and
    # 浮沉.2 are two volumes of 浮沉, while 琉璃时代 and 卡卡的信仰 are
    # separate works.  The engine maps top-level navPoints 1:1 to books, which
    # is what "全4册" describes.
    #
    # embedded_cover=False, and the covers still come out right: every work
    # opens on its own printed cover, so the first-page rule binds it with no
    # flag needed.  The four picks are images/00001 (浮沉①), 00009 (浮沉②),
    # 00014 (琉璃时代) and 00028 (卡卡的信仰), each a real ~544x751
    # publisher's flat front cover, and all four were opened and looked at.
    #
    # The largest-image rule is left OFF deliberately.  On this bundle it
    # disagrees with the first-page rule for 琉璃时代, voting for
    # images/00022 -- an interior plate.  It only appears to work here because
    # the first-page rule runs first and wins; enabling it would leave a
    # wrong cover waiting behind any work whose first page it fails to read.
    dict(key='cuimanli', match='崔曼莉', author='崔曼莉',
         set_title='崔曼莉小说集', embedded_cover=False),

    # 铁凝长篇四部: four novels by 铁凝 in one converted file set -- 大浴女,
    # 玫瑰门, 无雨之城, 笨花.  Like the entry above, its NCX is good enough on
    # its own: four top-level navPoints, one per work, each a contiguous run of
    # spine documents, so it needs neither `book_marks` nor `books_page`.
    dict(key='tiening', match='铁凝', author='铁凝',
         set_title='铁凝长篇四部', embedded_cover=False),
]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def is_html(path):
    return path.lower().endswith(('.html', '.xhtml', '.htm'))


def media_type(path, blob=None):
    """Declared media type for a manifest item.

    When the first bytes of the file are available an image is sniffed rather
    than trusted: the 猫腻 bundle ships five character portraits whose bytes
    are PNG under a ".jpg" name, and taking the extension at its word makes
    epubcheck reject the item (OPF-029: the file does not match the media type
    declared for it).  Naming the real type settles OPF-029; the warning
    epubcheck still raises about the extension (PKG-022) could only be cleared
    by renaming the publisher's file, which would break the rule that a split
    book keeps every document at its original archive path.
    """
    declared = MEDIA_TYPES.get(os.path.splitext(path)[1].lower(),
                               'application/octet-stream')
    if blob is not None and declared.startswith('image/'):
        return sniff_image_type(blob) or declared
    return declared


def resolve_in(base_dir, href):
    """Resolve `href` against a base *directory* -> normalised archive path.

    Dot segments are removed the way a URL removes them (RFC 3986), so ".."
    at the container root is clamped rather than kept.  The 酒徒 stylesheet
    reaches its logo as url(../../../2/OEBPS/Images/logo.png) from 1/OPS/css/,
    which is a live reference to 2/OEBPS/Images/logo.png; normpath leaves a
    leading ".." behind and would call that file missing.
    """
    href = href.split('#', 1)[0].split('?', 1)[0]
    if not href or re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', href) or href.startswith('//'):
        return None
    parts = [] if href.startswith('/') else \
        [s for s in base_dir.split('/') if s not in ('', '.')]
    for part in href.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return '/'.join(parts)


def resolve(base_file, href):
    """Resolve `href` (found inside `base_file`) to a normalised archive path."""
    return resolve_in(posixpath.dirname(base_file), href)


def local(tag):
    """Element tag without its namespace: '{ns}item' -> 'item'."""
    return tag.rsplit('}', 1)[-1]


def find_all(root, name):
    """Every descendant element whose local name is `name`, any namespace.

    The 倪匡 bundle is an epubBuilder-era EPUB 2 whose package document writes
    the OPF namespace as `mlns`, so nothing in it is in the OPF namespace and a
    namespace-aware lookup sees an empty manifest and spine.  Matching on the
    local name reads that package and a well-formed one alike.
    """
    return [e for e in root.iter() if local(e.tag) == name]


def find_one(root, name):
    return next((e for e in root.iter() if local(e.tag) == name), None)


def strip_tags(html):
    html = re.sub(r'(?is)<head.*?</head>', '', html)
    return re.sub(r'(?s)<[^>]+>', ' ', html)


def plain(html):
    """Readable plain text, used for stub detection and metadata scraping."""
    return re.sub(r'\s+', ' ', strip_tags(html)).strip()


def squeeze(html):
    """Plain text with ALL whitespace removed (for marker matching)."""
    return re.sub(r'\s+', '', strip_tags(html)).replace('　', '')


def image_size(blob):
    """(width, height) for JPEG/PNG/GIF, else None."""
    if blob[:2] == b'\xff\xd8':
        i = 2
        while i < len(blob) - 9:
            if blob[i] != 0xFF:
                i += 1
                continue
            marker = blob[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack('>HH', blob[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack('>H', blob[i + 2:i + 4])[0]
        return None
    if blob[:6] in (b'GIF87a', b'GIF89a'):
        return struct.unpack('<HH', blob[6:10])
    if blob[:8] == b'\x89PNG\r\n\x1a\n':
        return struct.unpack('>II', blob[16:24])
    return None


IMAGE_MAGIC = ((b'\x89PNG\r\n\x1a\n', 'image/png'),
               (b'\xff\xd8\xff', 'image/jpeg'),
               (b'GIF87a', 'image/gif'), (b'GIF89a', 'image/gif'),
               (b'BM', 'image/bmp'))


def sniff_image_type(blob):
    """The image format the bytes really are, or None if unrecognised."""
    for magic, mime in IMAGE_MAGIC:
        if blob.startswith(magic):
            return mime
    if blob[:4] == b'RIFF' and blob[8:12] == b'WEBP':
        return 'image/webp'
    return None


# --------------------------------------------------------------------------
# bundle reading
# --------------------------------------------------------------------------

class Bundle:
    def __init__(self, path, cfg):
        self.path = path
        self.cfg = cfg
        self.zip = zipfile.ZipFile(path)
        self.names = set(self.zip.namelist())

        container = ET.fromstring(self.zip.read('META-INF/container.xml'))
        self.opf_path = find_one(container, 'rootfile').get('full-path')
        self.opf_dir = posixpath.dirname(self.opf_path)
        opf = ET.fromstring(self.zip.read(self.opf_path))

        self.manifest = {i.get('id'): i.get('href')
                         for i in find_all(opf, 'item')}
        self.spine = []
        for ref in find_all(opf, 'itemref'):
            href = self.manifest.get(ref.get('idref'))
            if href:
                self.spine.append(posixpath.normpath(
                    posixpath.join(self.opf_dir, href)))
        self.spine_index = {p: i for i, p in enumerate(self.spine)}

        # archive path -> (source path, start, end) for a document that a
        # split writes only part of (see plan_marked_books); read() serves the
        # slice, so everything downstream sees a whole document as usual.
        self.slices = {}

        ncx_path = next(n for n in self.zip.namelist()
                        if n.lower().endswith('.ncx'))
        self.ncx_dir = posixpath.dirname(ncx_path)
        root = ET.fromstring(self.zip.read(ncx_path))
        self.navmap = find_one(root, 'navMap')

    def read(self, path):
        cut = self.slices.get(path)
        if cut is None:
            return self.zip.read(path)
        src, start, end = cut
        return slice_document(self.zip.read(src).decode('utf-8', 'replace'),
                              start, end)

    def text(self, path):
        return self.read(path).decode('utf-8', 'replace')

    def head(self, path, size=16):
        """First bytes of a member, to sniff what it really is."""
        if path in self.slices:
            return self.read(path)[:size]
        with self.zip.open(path) as fh:
            return fh.read(size)

    def top_entries(self):
        """Top-level NCX navPoints: (title, resolved start file, element)."""
        out = []
        for point in self.navmap.findall('n:navPoint', N):
            label = point.find('n:navLabel/n:text', N)
            src = point.find('n:content', N).get('src')
            out.append(((label.text or '').strip(),
                        resolve_in(self.ncx_dir, src) if src else None,
                        point))
        return out

    def book_entries(self):
        """(label, start file, NCX node) for each book in the bundle.

        Normally the NCX top level is one navPoint per book.  The 21-volume
        村上春树 set instead carries one navPoint per *spine file*, so its
        book boundaries come from the bundle's own 总目录 page, sorted into
        spine order (the page lists the books in the publisher's order).
        """
        page = self.cfg.get('books_page')
        if not page:
            return self.top_entries()
        out = []
        for href, label in re.findall(
                r'<a\b[^>]*href\s*=\s*"([^"]+)"[^>]*>(.*?)</a>',
                self.text(page), re.I | re.S):
            target = resolve(page, href)
            if target not in self.spine_index:
                continue
            out.append((re.sub(r'\s+', ' ', re.sub(r'(?s)<[^>]+>', '', label)).strip(),
                        target, None))
        out.sort(key=lambda e: self.spine_index[e[1]])
        return out

    def book_begin(self, entries, i):
        """Spine index where entry i's book begins, its cover page included.

        A navPoint naming the work's 版权信息 or 书名页 rather than its 封面
        page (刘醒龙, 阿来, 小川洋子's last volume) leaves the printed cover
        one document inside the previous entry's range.  Claiming it here both
        gives the right book its cover and, because the range of book i-1 ends
        where book i begins, takes it out of the book that would otherwise
        print its neighbour's cover as its own.
        """
        start = self.spine_index[entries[i][1]]
        if self.cfg.get('cover_page_precedes') and start > 0:
            if is_cover_page(self, self.spine[start - 1]):
                return start - 1
        return start

    def book_range(self, entries, begins, i):
        """Spine documents belonging to entry i.

        Normally a book runs to the next entry's start (cover pages already
        claimed there by book_begin).  In the 古龙 layout
        (`cover_precedes_book`) it stops earlier, where the next book's cover
        page begins: the set-level pages in between (辑 and 系列 title pages,
        目录 pages, the series blurb) belong to no book, and the next work's
        cover does not belong to this one.
        """
        start = begins[i]
        if i + 1 < len(entries):
            nxt = begins[i + 1]
            if self.cfg.get('cover_precedes_book'):
                cover = self.preceding_cover(nxt)
                if cover is not None and cover > start:
                    return self.spine[start:cover]
            return self.spine[start:nxt]
        return self.spine[start:]

    def preceding_cover(self, start):
        """Spine index of the bare image page nearest before `start`, or None.

        The 古龙 bundle prints each work's cover as a bare image page in front
        of it; for a work inside a 系列 the nearest such page before it is the
        series' own boxed-set shot, which is the cover the physical book came
        in.  Every image in that bundle is one of these 35 pages (checked by
        eye), so no interior plate can be bound by mistake.
        """
        for j in range(start - 1, -1, -1):
            if bare_image_page(self.text(self.spine[j])):
                return j
        return None


# --------------------------------------------------------------------------
# per-book analysis
# --------------------------------------------------------------------------

def drop_leading_set_pages(bundle, files):
    """Remove set-title filler at the head of a book range.

    The 上海译文 set leaves a page containing only the set title in front of
    every book; it is useless inside an individual book.
    """
    title = bundle.cfg['set_title']
    i = 0
    while i < len(files):
        path = files[i]
        if not is_html(path):
            break
        raw = bundle.read(path)
        if len(raw) >= 1000:
            break
        text = plain(raw.decode('utf-8', 'replace'))
        if '版权信息' in text or not text.startswith(title[:12]):
            break
        i += 1
    return files[i:]


def is_stub(bundle, path):
    return len(squeeze(bundle.text(path))) < 5


def bare_image_page(raw):
    """A page holding an image and no text at all -- a cover/plate page."""
    return '<img' in raw.lower() and not plain(raw)


def is_cover_page(bundle, path):
    """A page that is a book's cover: nothing but an image, or titled 封面.

    The cover the publisher printed opens the work in most of these bundles
    (a bare page of image and nothing else in 史景迁, a page titled 封面 with
    the flat front cover in 小川洋子 and 江户川乱步); in 刘醒龙 and 阿来 both
    kinds occur, and either way it is the work's own cover and not a plate.
    """
    raw = bundle.text(path)
    if bare_image_page(raw):
        return True
    title = bundle.cfg.get('cover_page')
    return bool(title and re.search(
        r'(?is)<title>\s*' + re.escape(title) + r'\s*</title>', raw))


def split_frag(href):
    return (href.split('#', 1) + [''])[:2]


BODY_OPEN = re.compile(r'(?is)<body[^>]*>')
BODY_CLOSE = re.compile(r'(?is)</body\s*>')


def slice_document(text, start, end):
    """One standalone XHTML document holding [start, end) of a source file.

    A converted file wraps ALL its chapters in a single <html>/<head>/<body>,
    so a slice taken at a `filepos` mark is a bare fragment -- it has neither
    the opening tags (an interior cut starts mid-body) nor the closing ones
    (a cut that stops at the next mark ends mid-body).  Re-wrap the slice in
    the source's own head and its own closing tags, so what the splitter
    writes is a document rather than a fragment.
    """
    opening = BODY_OPEN.search(text)
    closing = BODY_CLOSE.search(text)
    head = text[:opening.end()] if opening else (
        '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n'
        '<head><title></title></head>\n<body>')
    tail = text[closing.start():] if closing else '\n</body>\n</html>\n'
    lo = max(start, opening.end()) if opening else start
    hi = min(end, closing.start()) if closing else end
    return (head + text[lo:hi] + tail).encode('utf-8')


def ncx_subtree(bundle, node, keep):
    """Convert an NCX navPoint subtree into {title, file, frag, children}."""
    out = []
    for point in node.findall('n:navPoint', N):
        label = point.find('n:navLabel/n:text', N)
        src = point.find('n:content', N).get('src') or ''
        file = resolve_in(bundle.ncx_dir, src)
        frag = split_frag(src)[1]
        children = ncx_subtree(bundle, point, keep)
        if file in keep or children:
            out.append({
                'title': (label.text or '').strip(),
                'file': file if file in keep else None,
                'frag': frag,
                'children': children,
            })
    return out


def find_toc_page(bundle, files):
    """Locate the book's own 目录 page and return its in-range links."""
    members = set(files)
    for path in files:
        if not is_html(path):
            continue
        raw = bundle.text(path)
        if not squeeze(raw).startswith('目录'):
            continue
        links = re.findall(r'<a\b[^>]*href\s*=\s*"([^"]+)"', raw, re.I)
        within = [h for h in links
                  if resolve(path, h) in members and is_html(resolve(path, h))]
        if within:
            return path, within
    return None, []


def first_label(bundle, path):
    """Label a content file from its opening block-level element.

    These books carry no heading tags: a section opens with a <blockquote>
    chapter marker, a <p> title, or a 版权信息 <p>.  Take the first block
    element that has any text, skipping leading <br> rules and page breaks, so
    the label is a title rather than a 40-character slice of the body.
    """
    raw = bundle.text(path)
    body = re.sub(r'(?is)<head.*?</head>', '', raw)
    opening = re.search(r'(?is)<body[^>]*>(.*)', body)
    if opening:
        body = opening.group(1)
    for tag in re.finditer(r'(?is)<(p|div|blockquote|h[1-6]|li)\b[^>]*>(.*?)</\1>',
                           body):
        text = re.sub(r'\s+', '', re.sub(r'(?s)<[^>]+>', '', tag.group(2)))
        if not text:
            continue
        if len(text) <= 24:
            return text
        sentence = re.split(r'[。！？；]', text)[0]
        return sentence if 0 < len(sentence) <= 24 else text[:24]
    return plain(raw)[:24]


def build_toc(bundle, entry, files):
    """Return a TOC tree for one book, trying the most reliable source first."""
    node = entry[2]
    keep = set(files)

    # 1. chapter navPoints from the source NCX
    children = ncx_subtree(bundle, node, keep) if node is not None else []
    if children:
        return children

    # 2. the book's own 目录 page -- its anchor text is the chapter titles
    toc_page, links = find_toc_page(bundle, files)
    if links:
        raw = bundle.text(toc_page)
        labels = re.findall(r'<a\b[^>]*href\s*=\s*"[^"]+"[^>]*>(.*?)</a>',
                            raw, re.I | re.S)
        labels = [re.sub(r'\s+', ' ', re.sub(r'(?s)<[^>]+>', '', label)).strip()
                  for label in labels]
        labels = [label for label in labels if label]
        out = []
        for i, href in enumerate(links):
            target = resolve(toc_page, href)
            out.append({
                'title': labels[i] if i < len(labels) else first_label(bundle, target),
                'file': target,
                'frag': split_frag(href)[1],
                'children': [],
            })
        return out

    # 3. one entry per content file, labelled by its opening heading
    out = []
    for path in files:
        if not is_html(path) or is_stub(bundle, path):
            continue
        text = plain(bundle.text(path))
        label = first_label(bundle, path)
        # decorative rules (◆◆◆) and the publisher's repeated blurb page are
        # not章节 -- leaving them in produces junk TOC rows
        if not re.search(r'[\w一-鿿]', label):
            continue
        if text.startswith('Digital Lab'):
            continue
        out.append({'title': label, 'file': path, 'frag': '', 'children': []})
    if not out:
        target = next((p for p in files if is_html(p)), None)
        out = [{'title': entry[0], 'file': target, 'frag': '', 'children': []}]
    elif len(out) == 1:
        # A book that is a single document (鸳鸯刀, 越女剑, 白马啸西风) has no
        # chapters to list, and its one file opens mid-sentence, so labelling
        # that row from the text gives a slice of prose -- "鸳鸯刀【袁冠南和
        # 萧中慧使到第九招“碧箫声里双鸣凤".  The book's own name is the honest
        # label for a book that is one unit.
        out[0]['title'] = entry[0]
    return out


def finalise_toc(nodes, opf_dir):
    """Turn archive paths into hrefs and drop anything that cannot be linked.

    A navPoint whose own target falls outside the book (the source NCX mixes
    set-level and book-level entries) has no href of its own; rather than emit
    an <a href="None">, hoist its children up in its place.
    """
    out = []
    for n in nodes:
        children = finalise_toc(n.get('children') or [], opf_dir)
        if n.get('file'):
            href = rel_from(opf_dir, n['file'])
            if n.get('frag'):
                href += '#' + n['frag']
            item = dict(n)
            item['href'] = href
            item['children'] = children
            out.append(item)
        else:
            out.extend(children)
    return out


def order_by_spine(nodes, index):
    """Sort each TOC level into reading order.

    One bundle's NCX lists two stories in the wrong order, which the nav
    "toc must be in reading order" rule rejects.  A level is only reordered
    when every entry sits in the spine, so levels that are curated or point
    outside the book keep exactly the order the source gave them.
    """
    for n in nodes:
        order_by_spine(n.get('children') or [], index)
    if len(nodes) > 1 and all(n.get('file') in index for n in nodes):
        nodes.sort(key=lambda n: index[n['file']])


def scrape_bookinfo(bundle, files):
    """Pull 书名 / 作者 off the book's own copyright page, if it has one."""
    title = author = None
    for path in files[:4]:
        if not is_html(path):
            continue
        raw = bundle.text(path)
        text = plain(raw)
        if '版权信息' not in text and '作者：' not in text and '作者:' not in text:
            continue
        m = re.search(r'书名[：:]\s*(.+?)\s*作者[：:]', text)
        if m:
            title = m.group(1).strip()
        m = re.search(r'作者[：:]\s*(.+?)(?:\s*(?:译者|责任编辑|ISBN|关注|微信|'
                      r'联系|合作|出版|字数|页数|定价)[：:]|\s*$)', text)
        if m:
            author = m.group(1).strip()
        if title or author:
            break
    if author:
        author = re.split(r'\s*ISBN', author)[0].strip()
    return title, author


def pick_embedded_cover(bundle, files):
    """Largest portrait-ish image referenced by the book -> real cover."""
    best = None
    for path in files:
        if not is_html(path):
            continue
        for href in re.findall(r'(?:src|href)\s*=\s*"([^"]+\.(?:jpe?g|png))"',
                               bundle.text(path), re.I):
            target = resolve(path, href)
            if not target or target not in bundle.names:
                continue
            size = image_size(bundle.read(target))
            if not size:
                continue
            w, h = size
            if w < 250 or h < 300 or not (1.15 <= h / w <= 2.0):
                continue
            if best is None or w * h > best[0]:
                best = (w * h, target)
    return best[1] if best else None


def cover_page_image(bundle, files, title):
    """The image on the page whose <title> is `title` -- the book's 封面 page.

    Some bundles give every work its own page literally titled 封面 (cover),
    holding the publisher's flat front cover.  That beats the largest-portrait
    heuristic: it is the designer's cover by construction, and it is steady --
    the heuristic instead picks whichever interior plate happens to be biggest
    (see the by-eye pins on 上杉谦信 / 丈夫这东西).  Returns None when the book
    has no such page, so the caller can fall back to the heuristic.
    """
    pattern = re.compile(r'(?is)<title>\s*' + re.escape(title) + r'\s*</title>')
    for path in files:
        if not is_html(path):
            continue
        blob = bundle.text(path)
        if not pattern.search(blob):
            continue
        for href in re.findall(r'(?:src|href)\s*=\s*"([^"]+\.(?:jpe?g|png))"',
                               blob, re.I):
            target = resolve(path, href)
            if target and target in bundle.names:
                return target
    return None


FRONT_IMG = re.compile(r'(?i)<img[^>]*\bsrc\s*=\s*"([^"]+)"')


def page_image(bundle, path):
    """The image a bare cover page shows, as an archive path, or None."""
    m = FRONT_IMG.search(bundle.text(path))
    if not m:
        return None
    target = resolve(path, m.group(1))
    return target if target in bundle.names else None


def first_page_cover(bundle, files):
    """The cover image when the book opens on its own cover page.

    Several of these sets print a cover for a work as the page the work opens
    on -- bare image (史景迁) or a page titled 封面 (江户川乱步, 刘醒龙, 阿来,
    小川洋子).  Bind that image and nothing else: the largest-portrait rule
    walked into 史景迁's books and bound a statistics table, a map, a
    photograph and a bibliography page as covers for nine of its twelve
    volumes.  A first page that carries text is the book already, so there is
    no cover page to bind and the book goes without one.
    """
    path = next((p for p in files if is_html(p)), None)
    if path is None:
        return None
    if not is_cover_page(bundle, path):
        return None
    return page_image(bundle, path)


def cover_pair_front(bundle, files):
    """The front cover of a bundle that prints each work's cover twice.

    史景迁's producer writes every cover as two consecutive bare image pages,
    back then front: the first carries the series list with a barcode and no
    title of its own, the second the work's title and its blurb.  Binding the
    first of the pair ships the back cover -- proved by OCR of all 24 images
    (every first page lists the twelve series titles, every second page
    carries its own work's title).  Returns None unless the range opens on two
    cover pages, so the ordinary first-page rule still covers the one work
    printed with a single cover page (追寻现代中国).
    """
    pages = [p for p in files if is_html(p)]
    if len(pages) < 2 or not (is_cover_page(bundle, pages[0])
                              and is_cover_page(bundle, pages[1])):
        return None
    return page_image(bundle, pages[1])


def retitle(blob, set_title, book_title):
    """Fix a document's <title> when it is a placeholder or the set's own name.

    Readers surface this in the window/header, so a book whose every page
    announces the 20-volume omnibus reads as broken.  Only touched when the
    existing text is empty, a placeholder, or the set title -- meaningful page
    titles are left alone.  Non-UTF-8 documents are returned untouched.
    """
    head = blob[:200].decode('ascii', 'ignore').lower()
    if 'encoding=' in head and 'utf-8' not in head and 'utf8' not in head:
        return blob
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    def sub(match):
        current = match.group(1).strip()
        if current and current not in ('未知', 'Untitled', '无题') \
                and set_title[:16] not in current:
            return match.group(0)
        return '<title>' + escape(book_title) + '</title>'

    return re.sub(r'(?is)<title>(.*?)</title>', sub, raw, count=1).encode('utf-8')


def collect_assets(bundle, files):
    """Referenced resources (css/images/fonts), closed over CSS url()/@import."""
    found = set()
    queue = [p for p in files if is_html(p)]
    seen = set(queue)
    while queue:
        path = queue.pop()
        raw = bundle.text(path)
        refs = [] if path.lower().endswith('.css') else re.findall(
            r'(?:src|href|xlink:href)\s*=\s*"([^"]+)"', raw, re.I)
        # ... plus whatever CSS the file itself carries: a style attribute or a
        # <style> block is invisible to a src/href scan, and 朱雀记 paints its
        # part-title pages with background-image: url(../Images/background.png),
        # so that image was being left behind as a dangling reference.
        refs += re.findall(r'url\(\s*[\'"]?([^\'")]+)', raw, re.I)
        refs += re.findall(r'@import\s+[\'"]([^\'"]+)', raw, re.I)
        for href in refs:
            target = resolve(path, href)
            if not target or target not in bundle.names or target in found:
                continue
            if is_html(target):
                # Only this book's own spine pages are carried.  A page
                # outside the range (the bundle's 总目录, a neighbouring
                # book's 目录) belongs to someone else and would dangle.
                continue
            found.add(target)
            if target.lower().endswith('.css') and target not in seen:
                seen.add(target)
                queue.append(target)
    return found


# --------------------------------------------------------------------------
# EPUB 3 writing
# --------------------------------------------------------------------------

def sanitise(name):
    for bad, good in ((':', '：'), ('/', '／'), ('?', '？'), ('*', '＊'),
                      ('"', '”'), ('<', '＜'), ('>', '＞'), ('|', '｜'),
                      ('\\', '＼')):
        name = name.replace(bad, good)
    return re.sub(r'\s+', ' ', name).strip().strip('.')


def plain_author(author):
    """〔英〕石黑一雄〔Kazuo Ishiguro〕 -> 石黑一雄"""
    if not author:
        return ''
    author = re.sub(r'^\s*[〔\[【（(［][^〕\]】）)］]{1,10}[〕\]】）)］]\s*', '', author)
    author = re.sub(r'[〔\[【（(［][A-Za-z][^〕\]】）)］]*[〕\]】）)］]', '', author)
    return author.strip()


def rel_from(opf_dir, target):
    """Relative href from the OPF directory to an archive path."""
    return posixpath.relpath(target, opf_dir) if opf_dir else target


def render_nav(nodes, depth=0):
    pad = '  ' * depth
    out = [f'{pad}<ol>']
    for n in nodes:
        href = n.get('href') or n.get('file')
        label = escape(n.get('title') or '')
        out.append(f'{pad}  <li><a href="{escape(href, {chr(34): "&quot;"})}">'
                   f'{label}</a>')
        if n.get('children'):
            out.append(render_nav(n['children'], depth + 2))
            out.append(f'{pad}  </li>')
        else:
            out[-1] += '</li>'
    out.append(f'{pad}</ol>')
    return '\n'.join(out)


def render_ncx(nodes, uid, counter=None):
    """NCX navMap.  The source trees often point a parent and its first child
    at the same target; playOrder must agree between navPoints sharing a
    target, while the id still has to be unique -- so they are counted apart.
    """
    if counter is None:
        counter = {'order': 0, 'id': 0, 'seen': {}}
    out = []
    for n in nodes:
        href = n.get('href') or n.get('file')
        order = counter['seen'].get(href)
        if order is None:
            counter['order'] += 1
            order = counter['order']
            counter['seen'][href] = order
        counter['id'] += 1
        out.append(f'    <navPoint id="np{counter["id"]}" playOrder="{order}">')
        out.append(f'      <navLabel><text>{escape(n.get("title") or "")}'
                   f'</text></navLabel>')
        out.append(f'      <content src="{escape(href, {chr(34): "&quot;"})}"/>')
        if n.get('children'):
            out.append(render_ncx(n['children'], uid, counter))
        out.append('    </navPoint>')
    return '\n'.join(out)


def count_nodes(nodes):
    return sum(1 + count_nodes(n.get('children') or []) for n in nodes)


COVER_PAGE = '''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh" lang="zh">
  <head>
    <meta charset="utf-8"/>
    <title>封面</title>
    <style type="text/css">
      html, body { margin:0; padding:0; height:100%; }
      body { text-align:center; }
      div { margin:0; padding:0; height:100%; }
      img { max-width:100%; max-height:100%; }
    </style>
  </head>
  <body epub:type="cover">
    <div><img src="@@SRC@@" alt="@@ALT@@"/></div>
  </body>
</html>
'''


LEGACY_DOCTYPE = re.compile(
    r'<!DOCTYPE\s+html\s+PUBLIC\s+"[^"]*"\s+"[^"]*"\s*>', re.I)
A_TAG = re.compile(r'(?is)<a\b([^>]*)>|</a\s*>')
HREF = re.compile(r'\bhref\s*=\s*"([^"]*)"', re.I)
FONT_FACE = re.compile(r'(?is)@font-face\s*\{[^}]*\}')
ABSOLUTE = re.compile(r'^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|#)')


def normalise_doctype(blob):
    """EPUB 3 wants the HTML5 doctype; the source bundles are XHTML 1.0."""
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob
    return LEGACY_DOCTYPE.sub('<!DOCTYPE html>', raw, count=1).encode('utf-8')


VALIGN = re.compile(r'(?i)\s*\bvalign\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))')
ALIGN = re.compile(r'(?i)\s*\balign\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))')
STYLE_ATTR = re.compile(r'(?i)\bstyle\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
TAG = re.compile(r'<[a-zA-Z][^>]*>')


def _add_style(tag, decls):
    """Return `tag` with the CSS declarations folded into its style attribute.

    An element that already styles itself gets the declarations appended, one
    that does not gets a style attribute of its own.  Shared by every
    presentational-attribute fix-up below.
    """
    if not decls:
        return tag
    decl = '; '.join(decls)
    style = STYLE_ATTR.search(tag)
    if style:
        body = next(g for g in style.groups() if g is not None)
        body = (body.rstrip().rstrip(';') + '; ' + decl).lstrip('; ')
        return tag[:style.start()] + f'style="{body}"' + tag[style.end():]
    close = re.search(r'\s*/?>$', tag)
    return tag[:close.start()] + f' style="{decl}"' + tag[close.start():]


def demote_valign(blob):
    """Fold the legacy `valign=` attribute into `style=`.

    valign is presentational and EPUB 3 rejects it outright (RSC-005).  It
    is meaningless on <table> but real on <tr>/<td>, so convert rather than
    delete: an element that already styles itself gets `vertical-align`
    appended, one that does not gets a style attribute of its own.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    def fix(match):
        tag = match.group(0)
        found = VALIGN.search(tag)
        if not found:
            return tag
        value = next(g for g in found.groups() if g is not None)
        value = re.sub(r'[^a-zA-Z-]', '', value)      # keep the css keyword only
        tag = tag[:found.start()] + tag[found.end():]
        return _add_style(tag, [f'vertical-align: {value}'] if value else [])

    return TAG.sub(fix, raw).encode('utf-8')


def demote_align(blob):
    """Fold the legacy `align=` attribute into `style=`.

    align is gone from HTML 5 (epubcheck RSC-005).  On an image it means float
    or centre, so map it to the matching CSS rather than to text-align, which
    would do nothing on a replaced element.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    def fix(match):
        tag = match.group(0)
        found = ALIGN.search(tag)
        if not found:
            return tag
        value = next(g for g in found.groups() if g is not None).strip().lower()
        tag = tag[:found.start()] + tag[found.end():]
        name = re.match(r'(?i)<(\w+)', tag).group(1).lower()
        if name == 'img':
            decl = {'left': 'float: left', 'right': 'float: right',
                    'center': 'display: block; margin: 0 auto'}.get(value)
        else:
            decl = (f'text-align: {value}'
                    if value in ('left', 'right', 'center', 'justify') else None)
        return _add_style(tag, [decl] if decl else [])

    return TAG.sub(fix, raw).encode('utf-8')


LI_TAG = re.compile(r'(?i)<li\b[^>]*>')
LI_VALUE = re.compile(r'''(?i)\s*\bvalue\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)''')


def drop_li_value(blob):
    """Drop the HTML4 `value=` attribute from list items (EPUB 3 rejects it).

    `value` only ever meant anything on an <li> inside an <ol>.  Every
    occurrence in these bundles sits on an <li> inside a <ul> whose
    stylesheet sets `list-style-type: disc`, where the attribute was inert
    -- so removing it leaves the rendering the source actually had.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob
    return LI_TAG.sub(lambda m: LI_VALUE.sub('', m.group(0)),
                      raw).encode('utf-8')


# The two stray attributes the export tool of the 阿来, 刘醒龙 and 江户川乱步
# bundles left behind.  Neither belongs to any version of HTML, so epubcheck
# refuses the whole document over them (RSC-005).  `active="true"` marks a
# QR-code <img> (200 of them in 阿来, 70 in 刘醒龙); never having been an
# attribute, it was ignored by every renderer, and no stylesheet in those
# bundles so much as mentions it.  `cellspacing="10"` sits on one <table> in
# 江户川乱步, whose class already carries `border-collapse: separate;
# border-spacing: 2px` -- and a presentational attribute ranks below the
# stylesheet, so a renderer that understood `cellspacing` still drew the 2px
# the class asks for.  Deleting both therefore leaves every page with the
# rendering the source actually had.
LEGACY_ATTR = re.compile(
    r'''(?i)\s*(?<![\w-])(?:active|cellspacing)\s*=\s*'''
    r'''(?:"[^"]*"|'[^']*'|[^\s>]+)''')


def drop_legacy_attrs(blob):
    """Delete the attributes no HTML version defines and nothing reads."""
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob
    return TAG.sub(lambda m: LEGACY_ATTR.sub('', m.group(0)),
                   raw).encode('utf-8')


# --- EPUB 2 markup the 金庸 bundles carry --------------------------------
#
# Those two bundles were converted from print by a tool that emitted HTML 4
# presentational markup.  Each normaliser below removes one construct EPUB 3
# rejects, without changing how the page renders: presentational attributes
# become the equivalent inline CSS, and constructs XHTML cannot express at all
# (an undeclared &nbsp;, a control character, a tag pointing at a placeholder
# of thirty X's) are repaired or dropped.

FONT_TAG = re.compile(r'(?is)<font\b([^>]*)>|</font\s*>')
FONT_ATTR = re.compile(
    r'''(?i)\s*\b(color|face|size)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))''')
# the HTML 3.2 <font size> scale, in the px values browsers settled on
FONT_SIZE = {'1': '10px', '2': '13px', '3': '16px', '4': '18px',
             '5': '24px', '6': '32px', '7': '48px'}


def font_to_span(blob):
    """Replace the obsolete <font> element with a <span> carrying the same CSS."""
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    def fix(match):
        if match.group(1) is None:                   # </font>
            return '</span>'
        tag = match.group(0)
        decls = []
        for attr in FONT_ATTR.finditer(tag):
            name = attr.group(1).lower()
            value = next(g for g in attr.groups()[1:] if g is not None)
            if name == 'size':
                decls.append('font-size: ' + FONT_SIZE.get(value, '16px'))
            elif name == 'face':
                decls.append('font-family: ' + value)
            else:
                decls.append('color: ' + value)
        tag = re.sub(r'(?i)^<font', '<span', FONT_ATTR.sub('', tag))
        return _add_style(tag, decls)

    return FONT_TAG.sub(fix, raw).encode('utf-8')


DIM_ATTR = re.compile(
    r'''(?i)\s*\b(width|height)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))''')
# HTML 5 allows width/height as attributes only on these elements
DIM_OK = frozenset(('img', 'video', 'audio', 'embed', 'object', 'input',
                    'canvas', 'iframe', 'source'))
# Before HTML 5 a renderer honoured width/height on these layout elements, so
# the attribute really did size something and is worth carrying into CSS.
DIM_LEGACY = frozenset(('table', 'td', 'th', 'col', 'colgroup', 'hr',
                        'applet', 'frame', 'frameset'))


def demote_dimensions(blob):
    """Settle the legacy `width=`/`height=` attributes on every element.

    Three cases, by what the attribute could have meant on that element:

    * On a replaced element (`img`, `video`, ...) it is valid HTML 5, so an
      integer pixel value stays put -- it lets a reader reserve the right
      space before the image loads.  A percentage is obsolete (epubcheck
      RSC-005) but presentational, so it moves into CSS, keeping exactly its
      old meaning.
    * On a legacy layout element (`table`, `td`, `hr`) a renderer really did
      honour it, so it also moves into CSS and keeps the size the page had.
    * On anything else it never sized anything at all.  徐皓峰作品集 writes
      `<p height="1em" width="2em">` on every one of its 20,000 paragraphs;
      moving that into CSS does not preserve the page, it INVENTS a box --
      each paragraph renders as a two-character-wide column with its lines
      overlapping, which is not readable at all.  Such an attribute is
      dropped instead, which is how the source itself rendered (no reader
      ever honoured it there).

    (SVG's <image>/<svg> width is a different attribute in a different
    namespace and is deliberately left alone.)
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    def fix(match):
        tag = match.group(0)
        name = re.match(r'(?i)<([\w:-]+)', tag).group(1).lower()
        if name in ('svg', 'image'):
            return tag
        keep = name in DIM_OK
        legacy = name in DIM_LEGACY
        cut, decls = [], []
        for attr in DIM_ATTR.finditer(tag):
            value = next(g for g in attr.groups()[1:] if g is not None).strip()
            if keep and value.isdigit():
                continue
            cut.append((attr.start(), attr.end()))
            if keep or legacy:
                if value.isdigit():
                    value += 'px'
                decls.append(f'{attr.group(1).lower()}: {value}')
        for start, end in reversed(cut):
            tag = tag[:start] + tag[end:]
        return _add_style(tag, decls)

    return TAG.sub(fix, raw).encode('utf-8')


META_CT = re.compile(
    r'(?is)<meta\b[^>]*\bhttp-equiv\s*=\s*["\']?content-type["\']?[^>]*>')


def drop_meta_content_type(blob):
    """Drop the HTML4 <meta http-equiv="Content-Type"> element.

    EPUB 3 allows it only with the exact value "text/html; charset=utf-8",
    and the bundles write "application/xhtml+xml", which epubcheck rejects
    (RSC-005).  The encoding is already stated in the XML declaration, so
    the element has nothing left to say.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob
    return META_CT.sub('', raw).encode('utf-8')


LINK_TAG = re.compile(r'(?is)<link\b[^>]*>')
SCRIPT_TAG = re.compile(r'(?is)<script\b[^>]*>.*?</script\s*>|<script\b[^>]*/>')
SRC = re.compile(r'\bsrc\s*=\s*"([^"]*)"', re.I)


def drop_dead_refs(blob, base, keep):
    """Drop a <link> or <script> whose target this book does not carry.

    One bundle writes a placeholder href of thirty X's for a stylesheet that
    does not exist; 将夜's chapters each load ../Misc/main.js, which the bundle
    does not ship either.  Neither element has visible text, so removing the
    whole tag loses nothing -- and the script was only a dangling reference
    besides, whose presence would otherwise force "scripted" into the OPF
    (OPF-014) for a document that no longer has any script at all.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    def live(href):
        return bool(href) and (ABSOLUTE.match(href) or resolve(base, href) in keep)

    def fix_link(match):
        found = HREF.search(match.group(0))
        return match.group(0) if not found else \
            (match.group(0) if live(found.group(1)) else '')

    def fix_script(match):
        found = SRC.search(match.group(0))
        if not found:
            return match.group(0)          # an inline script: keep it as it is
        return match.group(0) if live(found.group(1)) else ''

    return SCRIPT_TAG.sub(fix_script, LINK_TAG.sub(fix_link, raw)).encode('utf-8')


BLOCK_START = re.compile(
    r'(?is)<(?:p|div|h[1-6]|ul|ol|table|blockquote|pre|figure|dl|hr)\b')
# what each inline element means, once it has to become a block-level <div>
INLINE_CSS = {'b': 'font-weight: bold', 'strong': 'font-weight: bold',
              'i': 'font-style: italic', 'em': 'font-style: italic',
              'u': 'text-decoration: underline'}
# <a> is deliberately absent: turning a link into a <div> would drop the link
LIFTABLE = ('font', 'span', 'b', 'strong', 'i', 'em', 'u')


def _inline_tag_to_div(tag, name):
    """Rewrite an inline open tag as a <div> carrying the same styling."""
    if name == 'font':
        decls = []
        for attr in FONT_ATTR.finditer(tag):
            key = attr.group(1).lower()
            value = next(g for g in attr.groups()[1:] if g is not None)
            if key == 'size':
                decls.append('font-size: ' + FONT_SIZE.get(value, '16px'))
            elif key == 'face':
                decls.append('font-family: ' + value)
            else:
                decls.append('color: ' + value)
        tag = FONT_ATTR.sub('', tag)
        return _add_style(re.sub(r'(?i)^<font', '<div', tag), decls)
    decl = INLINE_CSS.get(name)
    return _add_style(re.sub(r'(?i)^<' + name, '<div', tag, count=1),
                      [decl] if decl else [])


def lift_block_out_of_inline(blob):
    """Replace an inline element that wraps block content with a <div>.

    A <p> is flow content and may not sit inside <b>, <span>, <font> or any
    other phrasing element -- the error epubcheck calls "element p not allowed
    here" (RSC-005).  These bundles put their 回末注 (end-of-chapter notes)
    inside <font size=..>, often inside <b> as well, so the wrapper itself has
    to become flow content.  A <div> is the right element, and the wrapper's
    own styling is carried across as CSS, so the page renders as before.

    Each pattern rules out a nested tag of the same name, so it matches the
    wrapper's own end tag and pairs the elements correctly even when a <font>
    sits inside a <b>.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    for name in LIFTABLE:
        pattern = re.compile(r'(?is)(<%s\b[^>]*>)((?:(?!</?%s\b).)*?)(</%s\s*>)'
                             % (name, name, name))

        def fix(match, name=name):
            if not BLOCK_START.search(match.group(2)):
                return match.group(0)
            return _inline_tag_to_div(match.group(1), name) + match.group(2) + '</div>'

        raw = pattern.sub(fix, raw)
    return raw.encode('utf-8')


NAMED_ENTITY = re.compile(r'&([a-zA-Z][a-zA-Z0-9]*);')
XML_BUILTIN = frozenset(('amp', 'lt', 'gt', 'quot', 'apos'))


def numeric_entities(blob):
    """Rewrite named character references as numeric ones.

    XHTML is parsed as XML, where only the five built-in entities exist, so
    the HTML-4 `&nbsp;` the bundles use is a fatal error (RSC-016).  The
    numeric form means the same character and is always in scope.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    def fix(match):
        name = match.group(1)
        if name in XML_BUILTIN:
            return match.group(0)
        char = (html_entities.html5.get(name + ';')
                or html_entities.html5.get(name))
        if char is None:
            return match.group(0)
        return ''.join(f'&#{ord(c)};' for c in char)

    return NAMED_ENTITY.sub(fix, raw).encode('utf-8')


XMLNS_ATTR = re.compile(r'(?i)(xmlns(?::[a-zA-Z]+)?\s*=\s*")([^"]*)(")')


def fix_xmlns_colon(blob):
    """Repair a namespace URI written with a full-width colon.

    One document reads xmlns="http：//www.w3.org/1999/xhtml" -- that colon is
    U+FF1A, not U+003A -- so its elements land in no known namespace and
    epubcheck rejects the whole file (RSC-005).
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    def fix(match):
        return match.group(1) + match.group(2).replace('：', ':') + match.group(3)

    return XMLNS_ATTR.sub(fix, raw).encode('utf-8')


CTRL_CHAR = re.compile(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def strip_control_chars(blob):
    """Remove control characters XML cannot represent (RSC-016 is fatal).

    One chapter carries a stray U+001D inside its text.  Stripping at the
    byte level is safe: every byte of a UTF-8 continuation is >= 0x80, so
    these values can only be standalone control characters.
    """
    return CTRL_CHAR.sub(b'', blob)


STRAY_ANGLE = re.compile(r'<(?![A-Za-z/!?])')
TAG_TOKEN = re.compile(
    r'''(?is)<(/?)([A-Za-z][-A-Za-z0-9:]*)(?:"[^"]*"|'[^']*'|[^>"'])*?(/?)>''')
VOID_TAGS = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img',
                       'input', 'link', 'meta', 'param', 'source', 'track',
                       'wbr'))


def escape_stray_angles(blob):
    """Escape a "<" that cannot begin a tag.

    One chapter of 择天记 has a typo for "<p>": the "<" is followed by the
    paragraph's opening quote, and an XML parser stops dead on it (RSC-016 is
    fatal, so the whole file is lost).  Writing it as &lt; makes the document
    well-formed while keeping exactly the character the source has -- dropping
    it, or guessing an element name, would change the text.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob
    return STRAY_ANGLE.sub('&lt;', raw).encode('utf-8')


def drop_orphan_close_tags(blob):
    """Drop a closing tag whose element was never opened.

    The same typo leaves the "</p>" after it with no "<p>" to close, which is
    just as fatal to an XML parser.  A closing tag holds no text, so dropping
    it loses nothing.  Only a tag whose name is absent from the open-element
    stack is touched: a merely misplaced one is left to the reader's own
    recovery, since guessing which element it belonged to would change the
    structure of the page.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    dead, stack = [], []
    comments = [m.span() for m in re.finditer(r'(?s)<!--.*?-->', raw)]
    for m in TAG_TOKEN.finditer(raw):
        if any(a <= m.start() < b for a, b in comments):
            continue                       # a tag written inside a comment
        close, name, selfclose = m.group(1), m.group(2).lower(), m.group(3)
        if name in VOID_TAGS:
            continue
        if close:
            if stack and stack[-1] == name:
                stack.pop()
            elif name not in stack:
                dead.append((m.start(), m.end()))
        elif not selfclose:
            stack.append(name)
    for start, end in reversed(dead):
        raw = raw[:start] + raw[end:]
    return raw.encode('utf-8')


def normalise_html(blob, path, keep):
    """Every EPUB 3 fix-up a source document needs, applied in one place."""
    blob = strip_control_chars(blob)
    blob = escape_stray_angles(blob)
    blob = drop_orphan_close_tags(blob)
    blob = numeric_entities(blob)
    blob = normalise_doctype(blob)
    blob = fix_xmlns_colon(blob)
    blob = drop_meta_content_type(blob)
    blob = fix_inline_css(blob, path, keep)
    blob = drop_dead_refs(blob, path, keep)
    blob = drop_external_links(blob, path, keep)
    blob = lift_block_out_of_inline(blob)
    blob = demote_dimensions(blob)
    blob = font_to_span(blob)
    blob = demote_align(blob)
    blob = demote_valign(blob)
    blob = drop_li_value(blob)
    blob = drop_legacy_attrs(blob)
    return blob


def drop_external_links(blob, base, keep):
    """Unwrap an <a> whose target this book does not carry.

    A book keeps the bundle's "返回总目录" link, pointing at a page that no
    longer exists here.  Unwrapping leaves the label as plain text, so the
    reference disappears without any wording being lost.

    Tags are paired by hand rather than by one regex: the bundles contain
    illegal nested anchors (a masked-URL link wrapped around a real one), and
    a non-greedy pattern closes the outer tag on the inner tag's end, leaving
    the document malformed.
    """
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    edits = []
    stack = []
    for m in A_TAG.finditer(raw):
        if m.group(1) is None:                       # </a>
            if stack:
                start, open_end, drop = stack.pop()
                if drop:                             # drop the tags, keep the words
                    edits.append((start, open_end))
                    edits.append((m.start(), m.end()))
            continue
        attrs = m.group(1)
        if attrs.endswith('/'):                      # self-closing <a/>
            continue
        found = HREF.search(attrs)
        href = found.group(1) if found else None
        drop = bool(href) and not ABSOLUTE.match(href) and \
            resolve(base, href.split('#', 1)[0]) not in keep
        stack.append((m.start(), m.end(), drop))

    if not edits:
        return blob
    edits.sort()
    out, prev = [], 0
    for start, end in edits:                         # skip edits nesting inside
        if start < prev:
            continue
        out.append(raw[prev:start])
        prev = end
    out.append(raw[prev:])
    return ''.join(out).encode('utf-8')


CSS_URL = re.compile(r'(?is)url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)')
CSS_IMPORT = re.compile(
    r'''(?is)@import\s+(?:url\(\s*)?['"]?([^'");\s]+)['"]?\s*[^;]*;''')
STYLE_BLOCK = re.compile(r'(?is)(<style\b[^>]*>)(.*?)(</style\s*>)')


def fix_dead_css(raw, base, keep):
    """Remove or neutralise every CSS reference this book does not carry.

    Three shapes, all of them dead in the source as well as here (RSC-007):

    * @font-face whose font file is missing -- the bundles carry placeholder
      sources (styles/XXXXXXXXXXXXXXXX) for fonts that were never shipped, or
      Sony device paths (res:///opt/sony/ebook/FONT/...) that only exist on
      the reader.  The rule has nothing to fall back on, so it goes whole.
    * @import of a sheet that is not there: the same, no value to fall back on.
    * any other url() -- 酒徒's stylesheet reaches for 1/OPS/Images/0000.png
      from a background declaration.  "none" is CSS's own word for "no image",
      so the declaration keeps its meaning and the reader loses nothing that
      was ever going to arrive.
    """
    def font_face(m):
        block = m.group(0)
        for href in CSS_URL.findall(block):
            if href.startswith('res:'):          # device font, never in a book
                return ''
            if ABSOLUTE.match(href) or href.startswith('data:'):
                continue
            if resolve(base, href) not in keep:
                return ''
        return block

    def import_rule(m):
        href = m.group(1)
        if ABSOLUTE.match(href) or href.startswith('//'):
            return m.group(0)
        return m.group(0) if resolve(base, href) in keep else ''

    def one_url(m):
        href = m.group(1)
        if ABSOLUTE.match(href) or href.startswith('data:'):
            return m.group(0)
        return m.group(0) if resolve(base, href) in keep else 'none'

    raw = FONT_FACE.sub(font_face, raw)
    raw = CSS_IMPORT.sub(import_rule, raw)
    return CSS_URL.sub(one_url, raw)


def fix_inline_css(blob, base, keep):
    """fix_dead_css for the CSS a document carries itself: every style
    attribute and <style> block, whose url()s a src/href scan never sees."""
    try:
        raw = blob.decode('utf-8')
    except UnicodeDecodeError:
        return blob

    def attr(m):
        if m.group(1) is not None:
            return 'style="' + fix_dead_css(m.group(1), base, keep) + '"'
        return "style='" + fix_dead_css(m.group(2), base, keep) + "'"

    def block(m):
        return m.group(1) + fix_dead_css(m.group(2), base, keep) + m.group(3)

    return STYLE_ATTR.sub(attr, STYLE_BLOCK.sub(block, raw)).encode('utf-8')


def write_epub(bundle, book, out_path):
    """Assemble one EPUB 3 file."""
    opf_dir = bundle.opf_dir
    uid = 'urn:uuid:' + str(uuid.uuid5(
        uuid.NAMESPACE_URL, bundle.cfg['key'] + '|' + book['title']))

    files = book['files']
    assets = sorted(book['assets'])

    nav_name = posixpath.join(opf_dir, 'nav.xhtml')
    ncx_name = posixpath.join(opf_dir, 'toc.ncx')
    cover_page = posixpath.join(opf_dir, 'cover.xhtml')

    cover_img = book.get('cover_archive')
    cover_bytes = book.get('cover_bytes')
    # the cover is declared once, as cover-image; a spine page that also
    # references it must not yield a second manifest item for the same file
    assets = [p for p in assets if p != cover_img]

    seen = {bundle.opf_path, ncx_name, nav_name, cover_page}
    if book.get('cover'):
        seen.add(cover_img)
    keep = set(files) | set(assets) | seen

    # Documents are fixed up once, here, so that the manifest below describes
    # what the archive really holds: a chapter whose only <script> was a dead
    # reference no longer asks for properties="scripted".
    docs = {path: retitle(normalise_html(bundle.read(path), path, keep),
                          bundle.cfg['set_title'], book['title'])
            for path in files}

    # ---- OPF -------------------------------------------------------------
    manifest = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
    ]
    spine = []
    if book.get('cover'):
        manifest.append('    <item id="cover-page" href="cover.xhtml" '
                        'media-type="application/xhtml+xml"/>')
        manifest.append(f'    <item id="cover-image" href="{escape(rel_from(opf_dir, cover_img))}" '
                        f'media-type="{media_type(cover_img, bundle.head(cover_img))}" properties="cover-image"/>')
        spine.append('    <itemref idref="cover-page"/>')

    for i, path in enumerate(files):
        # a document carrying inline <svg> must say so, or EPUB 3 readers are
        # entitled to drop the vector content; one carrying a <script> must
        # say that too (OPF-014)
        blob = docs[path]
        props = ' '.join(p for p, present in (('svg', b'<svg' in blob),
                                              ('scripted', b'<script' in blob))
                         if present)
        attr = f' properties="{props}"' if props else ''
        manifest.append(f'    <item id="doc{i}" href="{escape(rel_from(opf_dir, path))}" '
                        f'media-type="{media_type(path, blob[:16])}"{attr}/>')
        spine.append(f'    <itemref idref="doc{i}"/>')
    for i, path in enumerate(assets):
        manifest.append(f'    <item id="res{i}" href="{escape(rel_from(opf_dir, path))}" '
                        f'media-type="{media_type(path, bundle.head(path))}"/>')

    creators = ''.join(
        f'\n    <dc:creator>{escape(c)}</dc:creator>' for c in book['creators'])

    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{uid}</dc:identifier>
    <dc:title>{escape(book['title'])}</dc:title>{creators}
    <dc:language>zh</dc:language>
    <dc:source>{escape(bundle.cfg['set_title'])}</dc:source>
    <meta property="dcterms:modified">2026-10-01T00:00:00Z</meta>
    <meta property="belongs-to-collection" id="set">{escape(bundle.cfg['set_title'])}</meta>
    <meta refines="#set" property="collection-type">series</meta>
  </metadata>
  <manifest>
{chr(10).join(manifest)}
  </manifest>
  <spine toc="ncx">
{chr(10).join(spine)}
  </spine>
</package>
'''

    # ---- navigation ------------------------------------------------------
    toc = finalise_toc(book['toc'], opf_dir)
    order_by_spine(toc, {p: i for i, p in enumerate(files)})
    depth = 1
    stack = list(toc)
    while stack:
        n = stack.pop()
        if n.get('children'):
            depth = max(depth, 2)
            stack.extend(n['children'])

    first_body = next((p for p in files if is_html(p)), files[0])
    nav = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh" lang="zh">
  <head>
    <meta charset="utf-8"/>
    <title>目录</title>
  </head>
  <body>
    <nav epub:type="toc" id="toc">
      <h1>目录</h1>
{render_nav(toc)}
    </nav>
    <nav epub:type="landmarks" id="landmarks" hidden="hidden">
      <h2>guide</h2>
      <ol>
        {'<li><a epub:type="cover" href="cover.xhtml">封面</a></li>' if book.get('cover') else ''}
        <li><a epub:type="bodymatter" href="{escape(rel_from(opf_dir, first_body))}">正文</a></li>
      </ol>
    </nav>
  </body>
</html>
'''

    ncx = f'''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="zh">
  <head>
    <meta name="dtb:uid" content="{uid}"/>
    <meta name="dtb:depth" content="{depth}"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{escape(book['title'])}</text></docTitle>
  <navMap>
{render_ncx(toc, uid)}
  </navMap>
</ncx>
'''

    # ---- assemble --------------------------------------------------------
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo('mimetype', date_time=(1980, 1, 1, 0, 0, 0))
        zf.writestr(info, 'application/epub+zip', compress_type=zipfile.ZIP_STORED)

        container = '''<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
'''.format(opf=escape(bundle.opf_path, {'"': '&quot;'}))
        zf.writestr('META-INF/container.xml', container)
        zf.writestr(bundle.opf_path, opf)
        zf.writestr(ncx_name, ncx)
        zf.writestr(nav_name, nav)

        if book.get('cover'):
            zf.writestr(cover_page,
                        COVER_PAGE.replace(
                            '@@SRC@@',
                            escape(rel_from(opf_dir, cover_img), {'"': '&quot;'})
                        ).replace('@@ALT@@', escape(book['title'])))
            zf.writestr(cover_img, cover_bytes if cover_bytes is not None
                        else bundle.read(cover_img))

        for path in files + assets:
            if path in seen:
                continue
            seen.add(path)
            if path in docs:
                blob = docs[path]
            else:
                blob = bundle.read(path)
                if path.lower().endswith('.css'):
                    try:
                        blob = fix_dead_css(
                            blob.decode('utf-8'), path, keep).encode('utf-8')
                    except UnicodeDecodeError:
                        pass
            zf.writestr(path, blob)
    return uid


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def plan_bundle(bundle):
    """Return the list of books a bundle will be split into."""
    if bundle.cfg.get('scan_book'):
        return plan_scan_book(bundle)
    if bundle.cfg.get('book_marks'):
        return plan_marked_books(bundle)
    entries = bundle.book_entries()
    # In a collection whose NCX nests the works under their 系列/辑, only the
    # entries that actually have children are books; the rest are the wrappers
    # (辑 title page, 辑目录, 系列 title page) that held the set together.
    if bundle.cfg.get('books_with_children'):
        entries = [e for e in entries
                   if e[2] is not None
                   and e[2].findall('n:navPoint', N)]
    books = []
    fix = bundle.cfg.get('title_fix')
    skip = FRONT_TITLES | set(bundle.cfg.get('skip_titles') or ())
    begins = [bundle.book_begin(entries, i) for i in range(len(entries))]
    for i, entry in enumerate(entries):
        label, start, _ = entry
        title = fix(label) if fix else label
        # an entry carrying the set's own name is the bundle's cover page
        if title in skip or title == bundle.cfg['set_title']:
            continue
        if start is None or start not in bundle.spine_index:
            continue
        files = drop_leading_set_pages(bundle, bundle.book_range(entries, begins, i))
        # Set-level pages printed after the last work (a promo card, an orphan
        # back cover, a house advert) fall inside the last book's range; the
        # config names them, having been read page by page.
        drop = set(bundle.cfg.get('drop_tail') or ())
        while drop and files and files[-1] in drop:
            files.pop()
        if not files:
            continue

        scraped_title, scraped_author = scrape_bookinfo(bundle, files)

        # metadata title may be a longer form of the nav label
        meta_title = title
        if scraped_title and (scraped_title.startswith(title)
                              or title.startswith(scraped_title)):
            meta_title = scraped_title

        author = ((bundle.cfg.get('authors') or {}).get(title)
                  or bundle.cfg['author'] or scraped_author or '')
        creators = [author] if author else []

        toc = build_toc(bundle, entry, files)

        book = dict(title=meta_title, nav_title=title, creators=creators,
                    author_plain=plain_author(author or scraped_author or ''),
                    files=files, toc=toc, index=len(books) + 1)
        books.append(book)
    return books


def scan_nodes(spec, doc):
    """TOC nodes for [(title, page[, children])] pointing at index.html#page_N.

    A scan addresses its pages by the anchor each page image sits under, so a
    page number from the source is all a TOC entry needs.
    """
    def node(title, page, children=()):
        return {'title': title, 'file': doc, 'frag': f'page_{page}',
                'children': [node(*child) for child in children]}
    return [node(*item) for item in spec]


def scan_toc(bundle, doc):
    """Chapter tree for a page-scan book, folded out of its NCX.

    The scan's bookmarks name the 章 and 节 but list all of them at the same
    level, so 第一章 and its 一/二/三 read as siblings.  A chapter title opens
    a node and each plain-numeral title after it becomes a child; anything
    else (封面, 书名) is front matter, which the config lists instead.
    """
    out = []
    for label, file, node in bundle.top_entries():
        if file != doc or not label:
            continue
        frag = split_frag(node.find('n:content', N).get('src') or '')[1]
        entry = {'title': label, 'file': doc, 'frag': frag, 'children': []}
        if SCAN_CHAPTER.match(label):
            out.append(entry)
        elif SCAN_SECTION.match(label) and out:
            out[-1]['children'].append(entry)
    return out


def plan_scan_book(bundle):
    """Plan the single book a page-scan bundle holds.

    A PDF-scan conversion puts the whole work in one HTML document whose
    "pages" are full-size images, carrying the #page_N anchors the TOC aims
    at, so there is no book-sized run of spine documents to cut on: the work
    is the book.  Its document is the one holding the page images -- which is
    the file the NCX points at when the bookmarks are real ones (倾城倾国,
    星星草 上卷), but not necessarily: the three scans whose NCX holds nothing
    but "Start" point at titlepage.xhtml, the calibre cover page, so the
    document is found by its images instead.  The TOC is either written out in
    the config (倾城倾国's bookmarks carry page numbers only) or read off the
    NCX chapters.
    """
    cfg = bundle.cfg['scan_book']
    docs = [f for _, f, _ in bundle.top_entries() if f in bundle.spine_index]
    if not docs:
        return []
    doc = max(set(docs), key=docs.count)
    by_images = max(bundle.spine,
                    key=lambda p: len(FRONT_IMG.findall(bundle.text(p))))
    if len(FRONT_IMG.findall(bundle.text(by_images))) > 1:
        doc = by_images

    if cfg.get('toc') is not None:
        toc = scan_nodes(cfg['toc'], doc)
    else:
        toc = scan_nodes(cfg.get('front') or [], doc) + scan_toc(bundle, doc)
    if not toc:
        return []

    title = cfg['title']
    author = bundle.cfg['author'] or ''
    return [dict(title=title, nav_title=title,
                 creators=[author] if author else [],
                 author_plain=plain_author(author), files=[doc], toc=toc,
                 index=1)]


def flat_entries(bundle):
    """(label, file, frag) for every top-level navPoint, in NCX order.

    A 作品集 converted chapter by chapter lists every chapter as a sibling
    navPoint -- 徐皓峰作品集 has 184 of them -- so nothing in the NCX says
    where one work ends and the next begins.
    """
    out = []
    for point in bundle.navmap.findall('n:navPoint', N):
        label = point.find('n:navLabel/n:text', N)
        src = point.find('n:content', N).get('src') or ''
        out.append(((label.text or '').strip(),
                    resolve_in(bundle.ncx_dir, src), split_frag(src)[1]))
    return out


def mark_offset(raw, frag):
    """Char offset where the block carrying anchor `frag` opens.

    A work opens at `<p ...><span id="filepos0000001232"></span><img ...>`;
    cutting at the anchor itself would leave the enclosing `<p>`'s open tag
    behind and orphan its `</p>`.  The cut is the enclosing block's start.
    """
    i = raw.find('id="%s"' % frag)
    if i < 0:
        return None
    opening = raw.rfind('<p', 0, i)
    closing = raw.rfind('</p>', 0, i)
    return opening if opening > closing else i


def plan_marked_books(bundle):
    """Books of a flat-NCX 作品集 whose works are named by anchor.

    The NCX is chapter-level, so `book_marks` names the anchor that opens each
    work, in reading order; a book runs from its mark to the next, and its
    chapter list is the run of navPoints between them.  A mark can fall inside
    a document -- one converted file commonly carries the tail of one work and
    the head of the next (徐皓峰作品集: text00002.html ends 逝去的武林 and
    starts 道士下山) -- so each document is cut at its marks.  The piece from
    the first mark onwards keeps the document's own path (a book keeps its
    documents where they were, so every intra-book link still resolves); an
    earlier piece is written under a derived name (text00002_p0.html) and
    registered in `bundle.slices` so reads serve the right range.
    """
    entries = flat_entries(bundle)
    marks = []
    for spec in bundle.cfg['book_marks']:
        path, _, frag = spec.partition('#')
        want = (resolve_in(bundle.ncx_dir, path), frag)
        idx = next((i for i, e in enumerate(entries) if (e[1], e[2]) == want),
                   None)
        if idx is None:
            raise ConfigError(f'book_marks: no navPoint at {spec!r}')
        marks.append(idx)

    raw = {p: bundle.text(p) for p in bundle.spine if is_html(p)}
    spans = []
    for idx in marks:
        _, f, frag = entries[idx]
        off = mark_offset(raw[f], frag)
        if off is None:
            raise ConfigError(f'book_marks: anchor {frag!r} not in {f}')
        spans.append((bundle.spine_index[f], off))
    if any(spans[i] >= spans[i + 1] for i in range(len(spans) - 1)):
        raise ConfigError('book_marks: marks are not in reading order')

    # the marks inside each document, in order
    bounds = {}
    for k, (di, off) in enumerate(spans):
        bounds.setdefault(di, []).append((off, k))
    # the book open when a document begins is the last mark before it
    opener = {}
    for di in range(len(bundle.spine)):
        opener[di] = max((k for k, (dj, _) in enumerate(spans) if dj < di),
                         default=None)

    cuts = bundle.cfg.get('drop_tail_marker') or ()
    docs_of = {}                     # book index -> {source doc: archive path}
    for di, doc in enumerate(bundle.spine):
        text = raw.get(doc)
        if text is None:
            continue
        here = bounds.get(di, [])
        edges = [0] + [off for off, _ in here] + [len(text)]
        owners = [opener[di]] + [k for _, k in here]
        pieces = []
        for j in range(len(edges) - 1):
            a, b, owner = edges[j], edges[j + 1], owners[j]
            for marker in cuts:
                seen = text.find(marker, a, b)
                if seen < 0:
                    continue
                opening = text.rfind('<p', a, seen)
                closing = text.rfind('</p>', a, seen)
                b = opening if opening > closing else seen
            pieces.append((a, b, owner))
        used = [j for j, (a, b, owner) in enumerate(pieces)
                if owner is not None and b > a]
        if not used:
            continue
        # the piece that opens the document's first work keeps its path
        keep = next((j for j in used if j >= 1), used[0])
        stem, ext = posixpath.splitext(doc)
        for j in used:
            a, b, owner = pieces[j]
            path = doc if j == keep else f'{stem}_p{j}{ext}'
            docs_of.setdefault(owner, {})[doc] = path
            if a > 0 or b < len(text):
                bundle.slices[path] = (doc, a, b)

    books = []
    for k, idx in enumerate(marks):
        docs = docs_of.get(k) or {}
        files = [docs[p] for p in bundle.spine if p in docs]
        title = entries[idx][0]
        if not files:
            raise ConfigError(f'book_marks: {title!r} has no content')
        stop = marks[k + 1] if k + 1 < len(marks) else len(entries)
        toc = []
        for i in range(idx + 1, stop):
            label, f, frag = entries[i]
            if f not in docs:
                raise ConfigError(f'book_marks: {label!r} ({f}) falls '
                                 f'outside {title!r}')
            toc.append({'title': label, 'file': docs[f], 'frag': frag,
                        'children': []})
        author = bundle.cfg['author'] or ''
        books.append(dict(title=title, nav_title=title,
                          creators=[author] if author else [],
                          author_plain=plain_author(author), files=files,
                          toc=toc, index=k + 1))
    return books


def attach_covers(bundle, books):
    overrides = bundle.cfg.get('cover_files') or {}
    for book in books:
        cover_archive = None
        cover_bytes = None
        if bundle.cfg.get('cover_precedes_book'):
            # the page in front of the work is a bare cover page; what the
            # book needs is the image on it, not the page (writing the page
            # put an XHTML document in the manifest as the cover-image)
            start = bundle.spine_index.get(book['files'][0])
            if start is not None:
                j = bundle.preceding_cover(start)
                if j is not None:
                    cover_archive = page_image(bundle, bundle.spine[j])
        else:
            # These run for every bundle that does not set
            # `cover_precedes_book` -- there is no flag to set.  A book that
            # opens on a page holding nothing but an image, or on a page
            # titled 封面, is opening on its own printed cover (史景迁,
            # 刘醒龙, 阿来, 小川洋子, 江户川乱步), and binding that image
            # cannot mistake an interior plate for a cover.  `embedded_cover`
            # deliberately does not gate this: it used to, so a bundle whose
            # works all open on their own covers was written with none at all
            # until someone found the flag and set it by hand.
            cover_archive = first_page_cover(bundle, book['files'])
            if bundle.cfg.get('cover_pair'):
                cover_archive = (cover_pair_front(bundle, book['files'])
                                 or cover_archive)
            page = bundle.cfg.get('cover_page')
            if cover_archive is None and page:
                cover_archive = cover_page_image(bundle, book['files'], page)
            # The largest-image rule is the one that gets covers wrong -- it
            # binds interior plates, and handed a statistics table, a map, a
            # photograph and a bibliography page to nine of 史景迁's twelve
            # volumes -- so it stays opt-in, for a bundle whose works carry
            # covers the rules above cannot find.
            if (cover_archive is None and not page
                    and bundle.cfg['embedded_cover']):
                cover_archive = pick_embedded_cover(bundle, book['files'])
        # A by-eye pick beats the heuristic.  "Largest portrait image" lands on
        # an interior map (上杉谦信) or a statistics table (丈夫这东西) for some
        # volumes, and no size/ratio test can tell those from a real cover -- so
        # those two books name the archive member they must use, checked by eye.
        pinned = overrides.get(book['nav_title']) or overrides.get(book['title'])
        if pinned:
            cover_archive = posixpath.normpath(
                posixpath.join(bundle.opf_dir, pinned))
        # A book with no cover of its own is written without one -- noted, not
        # skipped.  Web covers are opt-in through EPUB_WEB_COVERS, a directory
        # of "<key>-<NN>.jpg" named after the bundle config and the book's
        # index; the bundle no longer ships a tracked covers/ folder.
        if cover_archive is None and WEB_COVERS and os.path.isdir(WEB_COVERS):
            candidate = os.path.join(
                WEB_COVERS, f"{bundle.cfg['key']}-{book['index']:02d}.jpg")
            if os.path.exists(candidate) and os.path.getsize(candidate) > 2000:
                cover_archive = posixpath.join(
                    bundle.opf_dir, 'images', 'web-cover.jpg')
                cover_bytes = open(candidate, 'rb').read()
                # avoid colliding with an existing archive member
                n = 1
                while cover_archive in bundle.names:
                    n += 1
                    cover_archive = posixpath.join(
                        bundle.opf_dir, 'images', f'web-cover{n}.jpg')
        book['cover'] = cover_archive is not None
        book['cover_archive'] = cover_archive
        book['cover_bytes'] = cover_bytes


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--list', action='store_true',
                    help='show the split plan and write nothing')
    ap.add_argument('--bundle', metavar='KEY',
                    help='only the bundle config with this key')
    args = ap.parse_args()

    cfgs = [c for c in BUNDLES
            if args.bundle is None or c['key'] == args.bundle]
    if not cfgs:
        print(f'!! no bundle config with key {args.bundle!r}')
        return 2

    here = inputs()
    matched = 0            # bundles actually present in the folder
    empty = 0              # present, but yielded no book
    total = 0
    failures = 0
    used_names = {}
    for cfg in cfgs:
        found = [f for f in here if cfg['match'] in os.path.basename(f)]
        if not found:
            continue             # this bundle is simply not in the folder
        matched += 1
        path = found[0]
        try:
            bundle = Bundle(path, cfg)
            books = plan_bundle(bundle)
        except ConfigError as exc:
            print(f'!! {os.path.basename(path)}: {exc}')
            empty += 1
            continue
        attach_covers(bundle, books)
        print(f'\n{os.path.basename(path)[:60]}')
        print(f'  -> {len(books)} books')
        if not books:
            empty += 1
        for book in books:
            book['assets'] = collect_assets(bundle, book['files'])
            name = sanitise(f"{book['author_plain']} - {book['nav_title']}")
            if name in used_names:
                used_names[name] += 1
                name = f'{name} ({used_names[name]})'
            else:
                used_names[name] = 1
            out = os.path.join(OUTDIR, name + '.epub')
            book['out'] = out
            nodes = count_nodes(book['toc'])
            flag = 'cover' if book['cover'] else 'NO-COVER'
            if not args.list:
                try:
                    write_epub(bundle, book, out)
                except Exception as exc:           # noqa: BLE001 - report and go on
                    print(f'!! {name}: {exc}')
                    failures += 1
                    continue
            print(f'   {book["index"]:2d}. {name}.epub  '
                  f'[{len(book["files"])} docs, {nodes} toc, {flag}]')
            total += 1

    if matched == 0:
        print('no multi-book bundle in this folder')
        return 0
    print(f'\n{total} books' + (' (planned)' if args.list else ' written'))
    if empty:
        return 2                 # a bundle was here but produced nothing
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
