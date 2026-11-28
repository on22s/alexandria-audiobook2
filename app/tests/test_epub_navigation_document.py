"""EPUB 3 navigation documents are not story text (#2327).

Synthetic books only: a spine of nav/one/two, an EPUB 2 NCX book, and a
navigation document that also carries prose. Extraction and read-aloud
must agree about which text the book contains.
"""
import io
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
import httpx
import core
from epub_package import is_navigation_document
from epub_readaloud import build_epub_readaloud
from routers import script
from routers.script import extract_epub_text
from tests.test_epub_readaloud import _make_source_epub, _script_entries


CHAPTERS = 'A Cold Morning\n\nStory begins.\n\nThe Bridge\n\nStory ends.'


def navigation_epub(*, version='3.0', nav_in_spine=True, nav_linear=None,
                    nav_properties='nav', nav_body=None, include_ncx=False,
                    chapter_in_spine=True, cover=False, omit_nav_member=False,
                    empty_body=False, ncx_declaration='<?xml version="1.0"?>'):
    """Synthetic EPUB whose navigation document links to two chapters."""
    if nav_body is None:
        nav_body = ('<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head>'
                    '<body><nav epub:type="toc">'
                    '<a href="one.xhtml">A Cold Morning</a>'
                    '<a href="two.xhtml">The Bridge</a>'
                    '</nav></body></html>')
    prose_one, prose_two = ('', '') if empty_body else ('<p>Story begins.</p>', '<p>Story ends.</p>')
    one = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>A Cold Morning</title></head>'
           f'<body>{prose_one}</body></html>')
    two = ('<html xmlns="http://www.w3.org/1999/xhtml"><head><title>The Bridge</title></head>'
           f'<body>{prose_two}</body></html>')
    properties = f' properties="{nav_properties}"' if nav_properties is not None else ''
    manifest = ('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml"%s/>' % properties
                + '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
                + '<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>')
    if cover:
        manifest += '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
    if include_ncx:
        manifest += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    spine_attributes = ' toc="ncx"' if include_ncx else ''
    spine = '<itemref idref="cover"/>' if cover else ''
    if nav_in_spine:
        linear = f' linear="{nav_linear}"' if nav_linear is not None else ''
        spine += f'<itemref idref="nav"{linear}/>'
    if chapter_in_spine:
        spine += '<itemref idref="one"/><itemref idref="two"/>'
    package = ('<package xmlns="http://www.idpf.org/2007/opf" version="%s" unique-identifier="uid">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               '<dc:identifier id="uid">urn:test:2327</dc:identifier><dc:title>Synthetic</dc:title>'
               '<dc:language>en</dc:language></metadata>'
               '<manifest>%s</manifest><spine%s>%s</spine></package>'
               % (version, manifest, spine_attributes, spine))
    ncx = (ncx_declaration + '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
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
        if not omit_nav_member:
            archive.writestr('nav.xhtml', nav_body)
        archive.writestr('one.xhtml', one)
        archive.writestr('two.xhtml', two)
        if cover:
            archive.writestr('cover.png', b'\x89PNG synthetic cover')
        if include_ncx:
            archive.writestr('toc.ncx', ncx)
    return output.getvalue()


def extract(raw):
    return extract_epub_text('synthetic.epub', archive_bytes=raw, include_report=True)


@asynccontextmanager
async def upload_client():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        uploads = root / 'uploads'
        uploads.mkdir()
        app = FastAPI()
        app.include_router(script.router)
        app.add_exception_handler(core.AppError, core.handle_app_error)
        with patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                patch.object(script, 'DATA_DIR', str(root)):
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url='http://fixture') as client:
                yield client


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
                                    ('scripted nav-like', False), ('', False),
                                    # EPUB tokens are case-sensitive: only the
                                    # lowercase manifest term names the nav doc.
                                    ('NAV', False), ('Nav', False),
                                    # XML whitespace (tab/LF/CR; FF is outside
                                    # XML 1.0 but tolerated) separates tokens;
                                    # NBSP and other Unicode spaces do not.
                                    ('\tnav\n', True), ('cover-image\tnav', True),
                                    ('nav\u00a0cover-image', False),
                                    ('\u00a0nav\u00a0', False)):
            with self.subTest(properties=properties):
                text, _report = extract(navigation_epub(
                    nav_properties=properties, nav_body=nav_body))
                self.assertEqual(skipped, text == 'Story begins.\n\nStory ends.')
                self.assertEqual(not skipped, text.startswith('Navigation words.'))

    def test_xml_whitespace_tokenisation_of_properties(self):
        # XML attribute normalization rewrites CR/LF/FF before this point, so
        # the full set is checked on the shared predicate directly.
        for properties, expected in (('nav\rnav2', True), ('nav\f', True),
                                     ('a\rb\fnav', True), ('nav\u00a0', False),
                                     ('\u2003nav', False), ('NAV', False),
                                     ('Nav', False), ('navx', False),
                                     ('', False), (None, False)):
            with self.subTest(properties=properties):
                self.assertEqual(expected, is_navigation_document(properties))

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

    def test_a_nav_only_book_whose_nav_member_is_absent_is_not_navigation_only(self):
        text, report, navigation_only = extract_epub_text(
            'synthetic.epub',
            archive_bytes=navigation_epub(chapter_in_spine=False, omit_nav_member=True),
            include_report=True,
            include_navigation_only=True,
        )
        self.assertEqual('', text)
        spine = [row for row in report['rows'] if row['kind'] == 'spine']
        self.assertEqual(['missing_archive_resource'], [row['reason'] for row in spine])
        self.assertFalse(navigation_only)

    def test_epub2_ncx_and_nav_outside_the_spine_are_unchanged(self):
        epub2 = extract(navigation_epub(version='2.0', nav_in_spine=False,
                                        include_ncx=True, nav_properties=None))
        no_spine_nav = extract(navigation_epub(nav_in_spine=False))
        for text, report in (epub2, no_spine_nav):
            self.assertEqual(CHAPTERS, text)
            self.assertNotIn('navigation_document',
                             [row.get('reason') for row in report['rows']])

    def test_toc_entries_pointing_into_the_skipped_navigation_document_are_reported(self):
        nav_body = ('<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                    '<nav epub:type="toc"><a href="nav.xhtml#top">Contents</a>'
                    '<a href="nav.xhtml">Contents again</a>'
                    '<a href="one.xhtml">A Cold Morning</a><a href="two.xhtml">The Bridge</a>'
                    '</nav></body></html>')
        text, report = extract(navigation_epub(nav_body=nav_body))
        self.assertEqual(CHAPTERS, text)
        targets = [row for row in report['rows']
                   if row['kind'] == 'navigation_target' and row['resource'] == 'nav.xhtml']
        self.assertEqual(['top', None], [row['fragment'] for row in targets])
        self.assertEqual(['navigation_document'], sorted({row['reason'] for row in targets}))

    def test_unknown_navigation_encoding_is_recorded_as_malformed(self):
        # #2370: ET.fromstring raises LookupError, not ET.ParseError, for an
        # unknown declared encoding, and the navigation readers caught only
        # KeyError/ET.ParseError.
        bogus = '<?xml version="1.0" encoding="bogus"?>'
        nav_body = (bogus + '<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                    '<nav epub:type="toc"><a href="one.xhtml">A Cold Morning</a></nav>'
                    '</body></html>')
        text, report = extract(navigation_epub(nav_body=nav_body))
        # Without a table of contents the chapter labels are not inserted.
        self.assertEqual('Story begins.\n\nStory ends.', text)
        self.assertIn('missing_or_malformed_navigation',
                      [row.get('reason') for row in report['rows']])

        epub2_text, epub2_report = extract(navigation_epub(
            version='2.0', nav_in_spine=False, include_ncx=True,
            nav_properties=None, ncx_declaration=bogus))
        self.assertEqual('Story begins.\n\nStory ends.', epub2_text)
        self.assertIn('missing_or_malformed_navigation',
                      [row.get('reason') for row in epub2_report['rows']])

    def test_manifest_href_with_invalid_ipv6_authority_does_not_abort_import(self):
        # #2377: urlsplit('http://[') raises ValueError before the href can be
        # classified as external, aborting the whole import.
        self.assertEqual((None, None), script._resolve_epub_href('book.opf', 'http://['))
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr(
                'META-INF/container.xml',
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfile full-path="book.opf"/></container>')
            archive.writestr(
                'book.opf',
                '<package><manifest>'
                '<item id="bad" href="http://[" media-type="application/xhtml+xml"/>'
                '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
                '</manifest><spine><itemref idref="bad"/><itemref idref="one"/></spine></package>')
            archive.writestr('one.xhtml', '<html><body><p>Story begins.</p></body></html>')
        text, _report = extract(raw.getvalue())
        self.assertIn('Story begins.', text)

    def test_epub2_properties_in_the_spine_is_still_the_navigation_document(self):
        # Non-conforming EPUB 2 carrying the EPUB 3 ``properties="nav"`` token:
        # extraction skips the document, as read-aloud already did (#2327).
        text, report = extract(navigation_epub(version='2.0', include_ncx=True,
                                               nav_properties='nav'))
        self.assertEqual(CHAPTERS, text)
        spine = [row for row in report['rows'] if row['kind'] == 'spine']
        self.assertEqual(['nav', 'one', 'two'], [row['item_id'] for row in spine])
        self.assertEqual('navigation_document', spine[0]['reason'])

    def test_navigation_document_missing_from_the_archive_is_a_missing_resource(self):
        # The deliberate nav skip must not hide a genuinely absent member:
        # #2324 relies on the missing-resource signal.
        text, report = extract(navigation_epub(omit_nav_member=True))
        self.assertEqual('Story begins.\n\nStory ends.', text)
        spine = [row for row in report['rows'] if row['kind'] == 'spine']
        self.assertEqual(['nav', 'one', 'two'], [row['item_id'] for row in spine])
        self.assertEqual('missing_archive_resource', spine[0]['reason'])

    def test_navigation_only_classification_is_true_only_when_no_other_text_document_exists(self):
        for options, expected in (({'nav_in_spine': True, 'chapter_in_spine': False}, True),
                                  ({'nav_in_spine': True, 'chapter_in_spine': False,
                                    'cover': True}, True),
                                  ({}, False),
                                  ({'nav_in_spine': False}, False),
                                  # A nav document absent from the archive was
                                  # never imported, so the book is not
                                  # navigation-only; the missing-resource row
                                  # carries the real signal (#2324).
                                  ({'nav_in_spine': True, 'chapter_in_spine': False,
                                    'omit_nav_member': True}, False),
                                  # A declared chapter that yields no text is
                                  # still a text document: not navigation-only.
                                  ({'empty_body': True}, False)):
            with self.subTest(options=options):
                _text, report, navigation_only = extract_epub_text(
                    'synthetic.epub',
                    archive_bytes=navigation_epub(**options),
                    include_report=True,
                    include_navigation_only=True,
                )
                self.assertEqual(expected, navigation_only)
                self.assertNotIn('navigation_only_text', report)

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


class NavigationOnlyImportTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, client, raw):
        return await client.post('/api/upload', params={'select_active': 'false'},
                                 files={'file': ('book.epub', raw, 'application/epub+zip')})

    async def test_a_navigation_only_book_names_the_skipped_navigation_document(self):
        for options in ({'chapter_in_spine': False},
                        {'chapter_in_spine': False, 'cover': True}):
            with self.subTest(options=options):
                async with upload_client() as client:
                    response = await self.post(client, navigation_epub(**options))
                self.assertEqual(400, response.status_code, response.text)
                body = response.json()
                self.assertEqual('error.upload.epub_navigation_only', body['message_key'])
                self.assertIn('navigation document', body['detail'])
                self.assertIn('contents page', body['detail'])

    async def test_a_navigation_only_refusal_uses_the_single_extraction_pass(self):
        # The refusal must come from the classification the extractor already
        # made, not from a second extraction of the same bytes.
        async with upload_client() as client:
            with patch.object(script, 'extract_epub_text',
                              wraps=script.extract_epub_text) as extractor:
                response = await self.post(client, navigation_epub(chapter_in_spine=False))
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual('error.upload.epub_navigation_only',
                         response.json()['message_key'])
        self.assertEqual(1, extractor.call_count)

    async def test_a_navigation_document_missing_from_the_archive_keeps_the_generic_refusal(self):
        # The nav member was never in the archive, so nothing was actually
        # skipped: the refusal names no navigation document and the extractor
        # reports the member as missing, not as the navigation document.
        async with upload_client() as client:
            response = await self.post(client, navigation_epub(
                chapter_in_spine=False, omit_nav_member=True))
        self.assertEqual(400, response.status_code, response.text)
        body = response.json()
        self.assertEqual('error.upload.epub_empty', body['message_key'])
        self.assertEqual('No readable text content found in EPUB.', body['detail'])

    async def test_a_book_with_a_non_navigation_text_document_keeps_the_generic_refusal(self):
        # Two declared chapters whose bodies and anchors are empty: nothing is
        # spoken, but the book is not navigation-only, so the generic refusal
        # stands. This is the over-claim guard for the navigation-only message.
        nav_body = ('<html xmlns="http://www.w3.org/1999/xhtml" '
                    'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                    '<nav epub:type="toc"><a href="one.xhtml#missing">A Cold Morning</a>'
                    '<a href="two.xhtml#missing">The Bridge</a>'
                    '</nav></body></html>')
        async with upload_client() as client:
            response = await self.post(client, navigation_epub(
                empty_body=True, nav_body=nav_body))
        self.assertEqual(400, response.status_code, response.text)
        body = response.json()
        self.assertEqual('error.upload.epub_empty', body['message_key'])
        self.assertEqual('No readable text content found in EPUB.', body['detail'])


if __name__ == '__main__':
    unittest.main()
