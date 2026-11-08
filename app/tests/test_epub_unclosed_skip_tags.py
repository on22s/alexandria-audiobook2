"""Bug #1795: an unclosed skipped element must not silently drop a chapter.

Synthetic EPUB fixtures only.  The production extractor silently swallowed
the rest of a spine resource when its <title>/<style>/<script> was never
closed (and ignored a stray skipped-element close), so a three-chapter
book imported as only the first and last chapters.  These tests pin the
narrow repair: unbalanced *skipped* elements are an actionable error,
while ordinary tolerant HTML and legitimate empty/non-text resources are
still accepted.
"""
import html.parser
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from routers import script
from tests import test_upload_publication_source as uploads
from tests.test_epub_extraction_report import archive as report_archive


def make_epub(middle_body, *, before_last_manifest='', before_last_spine='',
              extra_files=None):
    """A synthetic three-spine EPUB: first / middle / last."""
    package = (
        '<package><manifest>'
        '<item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="middle" href="middle.xhtml" media-type="application/xhtml+xml"/>'
        + before_last_manifest
        + '<item id="last" href="last.xhtml" media-type="application/xhtml+xml"/>'
        '</manifest><spine>'
        '<itemref idref="first"/><itemref idref="middle"/>'
        + before_last_spine
        + '<itemref idref="last"/></spine></package>')
    members = [
        ('META-INF/container.xml',
         '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
         '<rootfile full-path="book.opf"/></container>'),
        ('book.opf', package),
        ('first.xhtml', '<html><body><p>First readable chapter.</p></body></html>'),
        ('middle.xhtml', middle_body),
        ('last.xhtml', '<html><body><p>Final readable chapter.</p></body></html>'),
    ]
    if extra_files:
        members.extend(extra_files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as target:
        for name, data in members:
            target.writestr(zipfile.ZipInfo(name), data)
    return output.getvalue()


GOOD_MIDDLE = '<html><body><p>Middle readable chapter.</p></body></html>'


def extract(raw, *, report=False):
    return script.extract_epub_text(
        'synthetic.epub', archive_bytes=raw, include_report=report)


class UnclosedSkipTagTests(unittest.TestCase):
    def test_each_unclosed_skip_tag_in_middle_raises_bytes_and_path(self):
        for tag in ('title', 'style', 'script'):
            with self.subTest(tag=tag):
                body = (f'<html><head><{tag}>Skipped header</head><body>'
                        '<p>Missing middle chapter.</p></body></html>')
                raw = make_epub(body)
                # Captured bytes, ordinary (non-report) mode.
                with self.assertRaisesRegex(ValueError, r'middle\.xhtml') as caught:
                    extract(raw)
                message = str(caught.exception)
                self.assertIn(tag, message)
                self.assertIn('unclosed', message.lower())
                # The error names the resource/tag without dumping user text.
                self.assertNotIn('Missing middle chapter', message)
                self.assertNotIn('Skipped header', message)
                # Captured bytes, report mode: also an error, not a partial
                # report that silently omits the chapter.
                with self.assertRaisesRegex(ValueError, r'middle\.xhtml'):
                    extract(raw, report=True)
                # Filesystem path, both modes.
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'source.epub'
                    path.write_bytes(raw)
                    with self.assertRaisesRegex(ValueError, r'middle\.xhtml'):
                        script.extract_epub_text(str(path))
                    with self.assertRaisesRegex(ValueError, r'middle\.xhtml'):
                        script.extract_epub_text(str(path), include_report=True)

    def test_mispaired_skip_close_a_depth_count_misses_raises(self):
        cases = [
            # A stray skipped-element close after a properly closed one.
            # The old depth counter ignored it (depth already zero).
            ('stray_style',
             '<html><body><title>Head</title></style>'
             '<p>Middle readable chapter.</p></body></html>', 'style'),
            ('stray_title',
             '<html><body><style>p{}</style></title>'
             '<p>Middle readable chapter.</p></body></html>', 'title'),
            ('stray_script',
             '<html><body><script>var x=1;</script></script>'
             '<p>Middle readable chapter.</p></body></html>', 'script'),
            # A close for a different skipped element inside raw content is
            # literal text, so the enclosing element is left unclosed.
            ('crossed_close',
             '<html><body><style>code</script>'
             '<p>Middle readable chapter.</p></body></html>', 'style'),
            # Markup that looks like a nested skipped element inside <title>
            # is literal; the trailing </style> is then a stray close.
            ('literal_nest_then_stray',
             '<html><body><title><style>literal</title></style>'
             '<p>Middle readable chapter.</p></body></html>', 'style'),
        ]
        for name, body, tag in cases:
            with self.subTest(case=name):
                raw = make_epub(body)
                with self.assertRaisesRegex(ValueError, r'middle\.xhtml') as caught:
                    extract(raw)
                self.assertIn(tag, str(caught.exception))
                with self.assertRaisesRegex(ValueError, r'middle\.xhtml'):
                    extract(raw, report=True)

    def test_proper_paired_case_and_selfclosing_skip_tags_accepted(self):
        body = ('<html><head><TITLE>Head words</TITLE><STYLE>p{}</STYLE>'
                '<SCRIPT>var x=1;</SCRIPT><title/><style/><script/>'
                '<Title>Again</Title></head>'
                '<body><p id="anchor">Middle readable chapter.</p></body></html>')
        text = extract(make_epub(body))
        self.assertIn('First readable chapter.', text)
        self.assertIn('Middle readable chapter.', text)
        self.assertIn('Final readable chapter.', text)
        self.assertNotIn('Head words', text)

    def test_literal_skip_markup_inside_raw_content_is_not_nesting(self):
        body = ('<html><body>'
                '<script>var s = "</title><style>";</script>'
                '<style>/* </script> </title> */ p { color: red; }</style>'
                '<title><style>not a nested element</style></title>'
                '<p>Middle readable chapter.</p></body></html>')
        text = extract(make_epub(body))
        self.assertIn('Middle readable chapter.', text)
        self.assertNotIn('not a nested element', text)

    def test_ordinary_tolerant_html_remains_accepted(self):
        # The existing tolerant-XHTML contract: an unclosed paragraph is
        # not a skipped element and must not be rejected.
        body = '<html><body><p>Middle tolerant words &amp; 10'
        text = extract(make_epub(body))
        self.assertIn('Middle tolerant words & 10', text)
        self.assertIn('First readable chapter.', text)
        self.assertIn('Final readable chapter.', text)
        # Report mode agrees with ordinary mode on tolerant input.
        reported, _report = extract(make_epub(body), report=True)
        self.assertEqual(text, reported)

    def test_text_accessors_reject_unclosed_skip_without_close(self):
        # Validation lives in the text accessors, so a caller that only
        # feeds (the original extraction shape) still fails on an open
        # skipped element instead of receiving narration with the rest of
        # the resource swallowed.
        extractor = script._HTMLTextExtractor()
        extractor.feed('<html><body><p>Before.</p><title>never closed')
        with self.assertRaisesRegex(ValueError, r'unclosed <title>'):
            extractor.get_text()
        extractor = script._HTMLTextExtractor()
        extractor.feed('<html><body><p>Before.</p><title>never closed')
        with self.assertRaisesRegex(ValueError, r'unclosed <title>'):
            extractor.get_text_with_anchor_positions()

    def test_incomplete_eof_markup_is_not_flushed_into_narration(self):
        # feed-only semantics: an incomplete markup fragment at EOF is
        # discarded, exactly as before. Validating skip balance must not
        # flush it into the extracted text via close().
        for tail in ('</', '<'):
            with self.subTest(tail=tail):
                body = '<html><body><p>Middle readable chapter.</p>' + tail
                self.assertEqual(
                    'First readable chapter.\n\nMiddle readable chapter.\n\n'
                    'Final readable chapter.', extract(make_epub(body)))

    def test_rejected_title_buffer_is_not_decoded_before_rejection(self):
        # Bounded fault injection: if decoding the skipped <title> payload
        # fails (simulated allocation failure), extraction must still report
        # the unclosed <title> - the payload must never reach unescape.
        # html.parser makes no unescape call for this payload during feed();
        # the old close()-based path made exactly one, on the whole buffer.
        payload = '&amp;' * 200
        body = '<html><body><title>' + payload
        raw = make_epub(body)
        original_unescape = html.parser.unescape

        def fail_only_on_skipped_payload(value):
            if value == payload:
                raise MemoryError(
                    'simulated allocation failure decoding skipped title payload')
            return original_unescape(value)

        with patch('html.parser.unescape', side_effect=fail_only_on_skipped_payload):
            with self.assertRaisesRegex(ValueError, r'middle\.xhtml') as caught:
                extract(raw)
        message = str(caught.exception)
        self.assertIn('unclosed <title>', message)
        self.assertNotIn(payload, message)

    def test_legitimate_empty_and_nontext_resources_remain_accepted(self):
        raw = make_epub(
            GOOD_MIDDLE,
            before_last_manifest=(
                '<item id="empty" href="empty.xhtml" '
                'media-type="application/xhtml+xml"/>'
                '<item id="image" href="image.png" media-type="image/png"/>'),
            before_last_spine='<itemref idref="empty"/><itemref idref="image"/>',
            extra_files=[
                ('empty.xhtml', '<html><body></body></html>'),
                ('image.png', b'\x89PNG\r\n\x1a\nsynthetic-image-bytes'),
            ])
        text = extract(raw)
        self.assertEqual(
            'First readable chapter.\n\nMiddle readable chapter.\n\n'
            'Final readable chapter.', text)
        reported, _report = extract(raw, report=True)
        self.assertEqual(text, reported)
        # An entirely empty middle spine resource is likewise accepted.
        text = extract(make_epub('<html><body></body></html>'))
        self.assertEqual(
            'First readable chapter.\n\nFinal readable chapter.', text)

    def test_healthy_text_anchor_and_title_output_preserved(self):
        # Existing report-fixture book: anchors, inserted navigation
        # titles and title suppression must be unchanged by the repair.
        raw = report_archive()
        expected = extract(raw)
        text, report = extract(raw, report=True)
        self.assertEqual(expected, text)
        self.assertTrue(text.startswith('Spine starts here.'))
        self.assertNotIn('Repeated generic title', text)
        self.assertIn('Before the first target. Café 42.', text)
        self.assertIn('First title', text)
        self.assertIn('Later title', text)
        self.assertLess(text.index('Later title'), text.index('First title'))
        self.assertEqual(
            ['second.xhtml', 'body.xhtml'],
            [row['resource'] for row in report['rows'] if row['kind'] == 'spine'])


class UnclosedSkipTagUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_middle_is_http400_without_publication_or_selection(self):
        body = ('<html><head><title>Skipped header</head><body>'
                '<p>Missing middle chapter.</p></body></html>')
        bad = make_epub(body)
        good = make_epub(GOOD_MIDDLE)
        async with uploads.UploadPublicationSourceTests().environment() as (
                root, directory, client):
            # A pre-existing successful import, selected as the active book.
            first = await client.post(
                '/api/upload?select_active=true',
                files={'file': ('book.epub', good, 'application/epub+zip')})
            self.assertEqual(200, first.status_code, first.text)
            before = {item.name: item.read_bytes() for item in directory.iterdir()}
            state_path = root / 'state.json'
            state_before = state_path.read_bytes() if state_path.exists() else None
            self.assertIsNotNone(state_before)

            for name, params in (
                    ('default', {}),
                    ('report', {'include_extraction_report': 'true',
                                'select_active': 'false'})):
                with self.subTest(mode=name):
                    query = '&'.join(f'{key}={value}' for key, value in params.items())
                    url = '/api/upload' + (f'?{query}' if query else '')
                    # Reuse the pre-existing book's filename too: the
                    # retained/original bytes must survive a failed
                    # re-import under the same name.
                    response = await client.post(
                        url, files={'file': ('book.epub', bad,
                                             'application/epub+zip')})
                    self.assertEqual(400, response.status_code, response.text)
                    detail = response.json()['detail']
                    self.assertIn('Failed to process EPUB', detail)
                    self.assertIn('middle.xhtml', detail)
                    self.assertIn('title', detail)
                    self.assertNotIn('Missing middle chapter', detail)
                    # No partial publication, no source leak, no new
                    # retained EPUB, and the pre-existing files are
                    # byte-for-byte untouched.
                    self.assertEqual(
                        before,
                        {item.name: item.read_bytes()
                         for item in directory.iterdir()})
                    # Active selection is not mutated by the refusal.
                    state_after = (state_path.read_bytes()
                                   if state_path.exists() else None)
                    self.assertEqual(state_before, state_after)

    async def test_malformed_upload_without_preexisting_book_leaves_no_trace(self):
        bad = make_epub('<html><body><p>ok</p><script>never closed</body></html>')
        async with uploads.UploadPublicationSourceTests().environment() as (
                root, directory, client):
            response = await client.post(
                '/api/upload?include_extraction_report=true&select_active=false',
                files={'file': ('bad.epub', bad, 'application/epub+zip')})
            self.assertEqual(400, response.status_code, response.text)
            self.assertIn('middle.xhtml', response.json()['detail'])
            self.assertIn('script', response.json()['detail'])
            self.assertEqual([], list(directory.iterdir()))
            self.assertFalse((root / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
