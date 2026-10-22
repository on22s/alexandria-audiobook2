import base64
import hashlib
import io
import unittest
import warnings
import zipfile
from unittest.mock import patch

from PIL import Image
from epub_publication import get_epub_publication_candidate


class EpubPublicationTests(unittest.TestCase):
    def image(self):
        out = io.BytesIO()
        with Image.new('RGB', (12, 8), (20, 60, 100)) as image:
            image.save(out, format='PNG')
        return out.getvalue()

    def epub(self, *, epub3=True, metadata=None, href='../Images/cover.png',
             extra_items='', cover=None, duplicate=False, compression=zipfile.ZIP_STORED):
        metadata = metadata if metadata is not None else (
            '<dc:title>First title</dc:title><dc:title>Alternative title</dc:title>'
            '<dc:creator id="author">Fixture Author</dc:creator>'
            '<dc:creator id="reader" opf:role="nrt">Fixture Narrator</dc:creator>')
        declaration = '' if epub3 else '<meta name="cover" content="cover"/>'
        properties = ' properties="cover-image"' if epub3 else ''
        package = ('<package xmlns="http://www.idpf.org/2007/opf" '
                   'xmlns:opf="http://www.idpf.org/2007/opf" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f'<metadata>{metadata}{declaration}</metadata><manifest>'
                   f'<item id="cover" href="{href}" media-type="image/png"{properties}/>'
                   f'{extra_items}</manifest></package>')
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w', compression=compression) as archive:
            archive.writestr('META-INF/container.xml',
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfile full-path="Text/book.opf"/></container>')
            archive.writestr('Text/book.opf', package)
            archive.writestr('Images/cover.png', self.image() if cover is None else cover)
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr('Images/cover.png', self.image())
        return raw.getvalue()

    def candidate(self, raw):
        return get_epub_publication_candidate(raw, filename='fixture.epub', text_sha256='a' * 64)

    def test_epub2_and_epub3_capture_options_origin_and_complete_cover_without_writes(self):
        for epub3 in (False, True):
            raw = self.epub(epub3=epub3)
            with self.subTest(epub3=epub3), patch('builtins.open', side_effect=AssertionError('No files')):
                result = self.candidate(raw)
            self.assertEqual(['First title', 'Alternative title'], result['fields']['title'])
            self.assertEqual(['Fixture Author'], result['fields']['author'])
            self.assertEqual('Fixture Narrator', result['other_creators'][0]['name'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), result['origin']['sha256'])
            self.assertEqual('a' * 64, result['origin']['text_sha256'])
            self.assertEqual(self.image(), base64.b64decode(result['cover']['data_base64']))
            self.assertEqual('available', result['cover_status'])

    def test_unknown_role_scheme_and_ambiguous_creator_ids_do_not_invent_an_author(self):
        for metadata in (
                '<dc:creator id="x">Editor</dc:creator><meta refines="#x" property="role" scheme="custom">aut</meta>',
                '<dc:creator id="x">First</dc:creator><dc:creator id="x">Second</dc:creator>'):
            with self.subTest(metadata=metadata):
                result = self.candidate(self.epub(metadata=metadata))
                self.assertEqual([], result['fields']['author'])
                self.assertTrue(result['other_creators'])
                self.assertTrue(result['warnings'])

    def test_epub3_marc_author_and_narrator_refinements_remain_distinct(self):
        result = self.candidate(self.epub(metadata=
            '<dc:creator id="a">Writer</dc:creator><dc:creator id="n">Reader</dc:creator>'
            '<meta refines="#a" property="role" scheme="marc:relators">aut</meta>'
            '<meta refines="#n" property="role" scheme="marc:relators">nrt</meta>'))
        self.assertEqual(['Writer'], result['fields']['author'])
        self.assertEqual([{'name': 'Reader', 'roles': ['nrt']}], result['other_creators'])

    def test_external_escaping_or_url_decorated_covers_are_refused_but_metadata_remains(self):
        for href in ('https://example.invalid/cover.png', '%2e%2e/%2e%2e/cover.png',
                     '/cover.png', 'C:/cover.png', 'dir\\cover.png', '../Images/cover.png?x', '../Images/cover.png#x'):
            with self.subTest(href=href):
                result = self.candidate(self.epub(href=href))
                self.assertEqual('unavailable', result['cover_status'])
                self.assertIsNone(result['cover'])
                self.assertEqual(['Fixture Author'], result['fields']['author'])

    def test_duplicate_members_and_conflicting_declarations_never_pick_a_cover(self):
        self.assertEqual('unavailable', self.candidate(self.epub(duplicate=True))['cover_status'])
        result = self.candidate(self.epub(extra_items=
            '<item id="other" href="../Images/other.png" media-type="image/png" properties="cover-image"/>'))
        self.assertEqual('ambiguous', result['cover_status'])
        self.assertIsNone(result['cover'])

    def test_truncated_cover_and_small_output_budget_preserve_metadata_only(self):
        for options in ({'cover': self.image()[:-20]}, {}):
            with self.subTest(options=bool(options)), patch('epub_publication.MAX_SOURCE_COVER_BYTES', 20):
                result = self.candidate(self.epub(**options))
                self.assertEqual('unavailable', result['cover_status'])
                self.assertEqual(['Fixture Author'], result['fields']['author'])

    def test_missing_declared_cover_does_not_hide_conflicting_declarations(self):
        for epub3 in (False, True):
            with self.subTest(epub3=epub3):
                result = self.candidate(self.epub(epub3=epub3, metadata=
                    '<dc:title>Fixture title</dc:title><dc:creator>Fixture Author</dc:creator>'
                    '<meta name="cover" content="missing-cover"/>'))
                self.assertEqual('ambiguous', result['cover_status'])
                self.assertIsNone(result['cover'])
                self.assertTrue(result['warnings'])
                self.assertEqual(['Fixture title'], result['fields']['title'])
                self.assertEqual(['Fixture Author'], result['fields']['author'])

    def test_many_duplicate_creators_keep_author_and_role_ambiguity_semantics(self):
        metadata = ('<dc:creator>Fixture Author</dc:creator>' * 1024
            + '<dc:creator id="reader">Fixture Reader</dc:creator>'
            + '<meta refines="#reader" property="role" scheme="marc:relators">nrt</meta>'
            + '<dc:creator id="duplicate">First</dc:creator>'
            + '<dc:creator id="duplicate">Second</dc:creator>')
        result = self.candidate(self.epub(metadata=metadata))
        self.assertEqual(['Fixture Author'], result['fields']['author'])
        self.assertEqual([{'name': 'Fixture Reader', 'roles': ['nrt']},
                          {'name': 'First', 'roles': []}, {'name': 'Second', 'roles': []}],
                         result['other_creators'])

    def test_xml_and_archive_limits_apply_before_suggestions_are_returned(self):
        with patch('epub_publication.MAX_PUBLICATION_XML_BYTES', 64):
            with self.assertRaisesRegex(ValueError, 'review limit'):
                self.candidate(self.epub())
        with patch('alexandria_alignment.EPUB_MAX_MEMBERS', 1):
            with self.assertRaisesRegex(ValueError, 'too many entries'):
                self.candidate(self.epub())

    def test_cover_expansion_guard_is_stricter_than_whole_archive_admission(self):
        result = self.candidate(self.epub(cover=self.image() + b'\0' * 50000,
                                          compression=zipfile.ZIP_DEFLATED))
        self.assertEqual('unavailable', result['cover_status'])
        self.assertIn('safe image bounds', result['warnings'][-1])


if __name__ == '__main__':
    unittest.main()
