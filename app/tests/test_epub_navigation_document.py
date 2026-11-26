"""EPUB 3 navigation documents are not story text (#2327).

Synthetic books only: a spine of nav/one/two, an EPUB 2 NCX book, and a
navigation document that also carries prose. Extraction and read-aloud
must agree about which text the book contains.
"""
import io
import unittest
import zipfile

from epub_readaloud import build_epub_readaloud
from routers.script import extract_epub_text
from tests.test_epub_readaloud import _make_source_epub, _script_entries


CHAPTERS = 'A Cold Morning\n\nStory begins.\n\nThe Bridge\n\nStory ends.'


def navigation_epub(*, version='3.0', nav_in_spine=True, nav_linear=None,
                    nav_properties='nav', nav_body=None, include_ncx=False):
    """Synthetic EPUB whose navigation document links to two chapters."""
    if nav_body is None:
        nav_body = ('<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head>'
                    '<body><nav epub:type="toc">'
                    '<a href="one.xhtml">A Cold Morning</a>'
                    '<a href="two.xhtml">The Bridge</a>'
                    '</nav></body></html>')
    one = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>A Cold Morning</title></head>'
           '<body><p>Story begins.</p></body></html>')
    two = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>The Bridge</title></head>'
           '<body><p>Story ends.</p></body></html>')
    properties = f' properties="{nav_properties}"' if nav_properties is not None else ''
    manifest = ('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"%s/>' % properties
                + '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
                + '<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>')
    if include_ncx:
        manifest += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    spine_attributes = ' toc="ncx"' if include_ncx else ''
    spine = ''
    if nav_in_spine:
        linear = f' linear="{nav_linear}"' if nav_linear is not None else ''
        spine += f'<itemref idref="nav"{linear}/>'
    spine += '<itemref idref="one"/><itemref idref="two"/>'
    package = ('<package xmlns="http://www.idpf.org/2007/opf" version="%s" unique-identifier="uid">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               '<dc:identifier id="uid">urn:test:2327</dc:identifier><dc:title>Synthetic</dc:title>'
               '<dc:language>en</dc:language></metadata>'
               '<manifest>%s</manifest><spine%s>%s</spine></package>'
               % (version, manifest, spine_attributes, spine))
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
           '<navMap><navPoint id="n1"><navLabel><text>A Cold Morning</text></navLabel>'
           '<content src="one.xhtml"/></navPoint><navPoint id="n2"><navLabel><text>The Bridge</text>'
           '</navLabel><content src="two.xhtml"/></navPoint></navMap></ncx>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml',
                         '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles><rootfile full-path="book.opf" '
                         'media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr('book.opf', package)
        archive.writestr('nav.xhtml', nav_body)
        archive.writestr('one.xhtml', one)
        archive.writestr('two.xhtml', two)
        if include_ncx:
            archive.writestr('toc.ncx', ncx)
    return output.getvalue()


def extract(raw):
    return extract_epub_text('synthetic.epub', archive_bytes=raw, include_report=True)


class EpubNavigationDocumentTests(unittest.TestCase):
    def test_spine_navigation_document_is_not_narrated(self):
        text, report = extract(navigation_epub())
        self.assertEqual(CHAPTERS, text)
        self.assertEqual(text, extract(navigation_epub(nav_in_spine=False))[0])
        spine = [row for row in report['rows'] if row['kind'] == 'spine']
        self.assertEqual(['nav', 'one', 'two'], [row['item_id'] for row in spine])
        self.assertEqual(['navigation_document', 'included', 'included'],
                         [row['reason'] for row in spine])

    def test_only_the_whole_nav_properties_token_marks_the_navigation_document(self):
        nav_body = ('<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                    '<p>Navigation words.</p></body></html>')
        for properties, skipped in (('nav', True), (' nav ', True),
                                    ('cover-image nav', True), ('navx', False),
                                    ('scripted nav-like', False), ('', False)):
            with self.subTest(properties=properties):
                text, _report = extract(navigation_epub(
                    nav_properties=properties, nav_body=nav_body))
                self.assertEqual(skipped, text == 'Story begins.\n\nStory ends.')
                self.assertEqual(not skipped, text.startswith('Navigation words.'))

    def test_prose_in_a_navigation_document_is_dropped_with_the_whole_document(self):
        nav_body = ('<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                    '<p>Preface prose stays out.</p><nav epub:type="toc">'
                    '<a href="one.xhtml">A Cold Morning</a><a href="two.xhtml">The Bridge</a>'
                    '</nav><p>Trailing prose stays out.</p></body></html>')
        text, report = extract(navigation_epub(nav_body=nav_body))
        self.assertNotIn('Preface prose stays out.', text)
        self.assertNotIn('Trailing prose stays out.', text)
        self.assertEqual(CHAPTERS, text)
        reasons = [row['reason'] for row in report['rows']
                   if row['kind'] == 'spine' and row['item_id'] == 'nav']
        self.assertEqual(['navigation_document'], reasons)

    def test_epub2_ncx_and_nav_outside_the_spine_are_unchanged(self):
        epub2 = extract(navigation_epub(version='2.0', nav_in_spine=False,
                                        include_ncx=True, nav_properties=None))
        no_spine_nav = extract(navigation_epub(nav_in_spine=False))
        for text, report in (epub2, no_spine_nav):
            self.assertEqual(CHAPTERS, text)
            self.assertNotIn('navigation_document',
                             [row.get('reason') for row in report['rows']])

    def test_nav_in_spine_with_linear_no_is_still_the_navigation_document(self):
        text, report = extract(navigation_epub(nav_linear='no'))
        self.assertEqual(CHAPTERS, text)
        spine = [row for row in report['rows'] if row['kind'] == 'spine']
        self.assertEqual('navigation_document', spine[0]['reason'])

    def test_readaloud_and_extraction_exclude_the_same_navigation_document(self):
        source = _make_source_epub()
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(output, 'w') as updated:
            for info in original.infolist():
                content = original.read(info)
                if info.filename == 'OEBPS/content.opf':
                    content = content.replace(b'<spine><itemref idref="c1"/>',
                                              b'<spine><itemref idref="nav"/><itemref idref="c1"/>')
                elif info.filename == 'OEBPS/nav.xhtml':
                    content = content.replace(b'<body>', b'<body><p>Preface prose stays out.</p>')
                updated.writestr(info, content)
        raw = output.getvalue()
        epub_bytes, report = build_epub_readaloud(
            retained_epub_bytes=raw, audio_bytes=b'\xff\xfbx',
            audio_filename='audiobook.mp3', script_entries=_script_entries())
        self.assertEqual(2, report['spine_documents'])
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as archive:
            self.assertNotIn('OEBPS/nav.smil', archive.namelist())
        text, _extraction_report = extract(raw)
        self.assertNotIn('Preface prose stays out.', text)
        self.assertIn('Hello synthetic world.', text)


if __name__ == '__main__':
    unittest.main()
