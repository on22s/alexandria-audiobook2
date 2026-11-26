import hashlib
import io
import json
import tempfile
from pathlib import Path
import unittest
import zipfile

from epub_extraction_report import EpubExtractionReport, MAX_REPORT_ROWS_BYTES
from routers import script
from tests import test_upload_publication_source as uploads


def archive(*, navigation='epub3', body=None, extra_manifest='', extra_spine='', malformed_nav=False):
    content = body or ('<html><head><title>Repeated generic title</title></head><body>'
        '<p>Before the first target. Café 42.</p><p id="later">Later words.</p>'
        '<p id="first">First words.</p><p id="first">Duplicate anchor words.</p></body></html>')
    links = [('body.xhtml#first', 'First title'), ('body.xhtml#later', 'Later title'),
             ('body.xhtml#missing', 'Missing title'), ('body.xhtml#first', 'First title'),
             ('outside.xhtml', 'Outside title')]
    if navigation == 'epub3':
        nav_item = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        nav = '<html xmlns:epub="http://www.idpf.org/2007/ops"><nav epub:type="toc">' + ''.join(
            f'<a href="{target}">{label}</a>' for target, label in links) + '</nav></html>'
        nav_path = 'nav.xhtml'
    else:
        nav_item = '<item id="nav" href="nav.ncx" media-type="application/x-dtbncx+xml"/>'
        nav = '<ncx><navMap>' + ''.join(f'<navPoint><navLabel><text>{label}</text></navLabel>'
            f'<content src="{target}"/></navPoint>' for target, label in links) + '</navMap></ncx>'
        nav_path = 'nav.ncx'
    package = ('<package><manifest><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="second" href="second.xhtml" media-type="application/xhtml+xml"/>' + nav_item + extra_manifest
        + '</manifest><spine toc="nav"><itemref idref="second"/><itemref idref="body"/>' + extra_spine + '</spine></package>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as target:
        for name, text in [('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfile full-path="book.opf"/></container>'), ('book.opf', package),
            ('body.xhtml', content), ('second.xhtml', '<html><head><title>Repeated generic title</title></head><body><p>Spine starts here.</p></body></html>'),
            (nav_path, '<broken' if malformed_nav else nav)]:
            target.writestr(zipfile.ZipInfo(name), text)
    return output.getvalue()


class EpubExtractionReportTests(unittest.TestCase):
    def extract(self, raw, report=True):
        return script.extract_epub_text('not-on-disk.epub', archive_bytes=raw, include_report=report)

    def test_epub2_and_epub3_reports_preserve_exact_extraction_and_spine_order(self):
        for navigation in ('epub2', 'epub3'):
            with self.subTest(navigation=navigation):
                raw = archive(navigation=navigation)
                expected = self.extract(raw, False)
                text, report = self.extract(raw)
                self.assertEqual(expected.encode(), text.encode())
                self.assertTrue(text.startswith('Spine starts here.'))
                self.assertNotIn('Repeated generic title', text)
                self.assertIn('Before the first target. Café 42.', text)
                self.assertLess(text.index('Later title'), text.index('First title'))
                self.assertEqual(hashlib.sha256(raw).hexdigest(), report['source_sha256'])
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), report['extracted_text_sha256'])
                self.assertEqual(['second.xhtml', 'body.xhtml'], [row['resource'] for row in report['rows'] if row['kind'] == 'spine'])
                self.assertEqual([0, 1], [row['index'] for row in report['rows'] if row['kind'] == 'spine'])
                self.assertEqual(raw, archive(navigation=navigation))

    def test_unresolved_duplicate_and_outside_targets_are_explicit(self):
        text, report = self.extract(archive())
        targets = [row for row in report['rows'] if row['kind'] == 'navigation_target']
        self.assertEqual(1, text.count('First title'))
        self.assertNotIn('Missing title', text)
        self.assertNotIn('Outside title', text)
        self.assertIn('unresolved_anchor', [row['reason'] for row in targets])
        self.assertIn('duplicate_navigation_entry', [row['reason'] for row in targets])
        self.assertIn('missing_archive_resource', [row['reason'] for row in targets])
        resolved = next(row for row in targets if row['fragment'] == 'first' and row['reason'] == 'insert_navigation_label')
        self.assertEqual(2, resolved['anchor_occurrences'])
        self.assertGreater(resolved['position'], 0)
        self.assertIn('Duplicate anchor words.', text)

    def test_missing_resources_and_malformed_navigation_preserve_fallback(self):
        raw = archive(malformed_nav=True,
            extra_manifest='<item id="lost" href="lost.xhtml" media-type="application/xhtml+xml"/><item id="image" href="image.png" media-type="image/png"/>',
            extra_spine='<itemref idref="lost"/><itemref idref="image"/><itemref idref="unknown"/>')
        text, report = self.extract(raw)
        self.assertEqual(self.extract(raw, False), text)
        reasons = {row.get('reason') for row in report['rows']}
        self.assertTrue({'missing_or_malformed_navigation', 'missing_archive_resource',
            'non_html_resource', 'missing_manifest_item', 'epub3_unavailable_or_empty_try_epub2'} <= reasons)
        self.assertNotIn('First title', text)

    def test_tolerant_xhtml_and_existing_title_suppression_are_reported_without_repair(self):
        raw = archive(body='<html><body><p>First title</p><p id="first">First title</p><p>Unclosed words &amp; 10')
        text, report = self.extract(raw)
        self.assertEqual(self.extract(raw, False), text)
        self.assertIn('Unclosed words & 10', text)
        self.assertIn('label_already_near_target', [row.get('reason') for row in report['rows']])
        self.assertIn('tolerant_html_parser', report['formatting'])

    def test_changed_text_and_archive_cannot_reuse_snapshot_identity(self):
        original, report = self.extract(archive())
        changed, newer = self.extract(archive(body='<p>Edited source.</p>'))
        self.assertNotEqual(report['source_sha256'], newer['source_sha256'])
        self.assertNotEqual(report['extracted_text_sha256'], newer['extracted_text_sha256'])
        self.assertNotEqual(report['extracted_text_sha256'], hashlib.sha256((original + ' edited').encode()).hexdigest())
        self.assertEqual('captured_archive_and_import_text_not_current_editor', report['identity_scope'])

    def test_report_budget_and_field_truncation_never_replace_text_identity(self):
        report = EpubExtractionReport(b'captured synthetic archive')
        for index in range(2000):
            report.apply_row('navigation_target', index=index, label='あ' * 2000)
        result = report.get_report('Unchanged source')
        self.assertGreater(result['omitted_rows'], 0)
        self.assertFalse(result['detail_complete'])
        self.assertEqual(result['omitted_rows'], result['omitted_rows_by_kind']['navigation_target'])
        self.assertEqual(2000, len(result['rows']) + result['omitted_rows'])
        self.assertEqual(['label'], result['rows'][0]['truncated_fields'])
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), MAX_REPORT_ROWS_BYTES + 4096)
        self.assertEqual(hashlib.sha256(b'Unchanged source').hexdigest(), result['extracted_text_sha256'])

    def test_identity_budget_is_reserved_after_navigation_detail_fills(self):
        report = EpubExtractionReport(b'archive')
        for index in range(2000):
            report.apply_row('navigation_target', label='x' * 512)
        for index in range(140):
            report.apply_row('spine', index=index, resource_sha256='a' * 64, text_sha256='b' * 64)
        result = report.get_report('text')
        self.assertEqual(140, sum(row['kind'] == 'spine' for row in result['rows']))
        self.assertNotIn('spine', result['omitted_rows_by_kind'])

    def test_path_only_report_matches_captured_bytes(self):
        raw = archive()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.epub'
            path.write_bytes(raw)
            self.assertEqual(self.extract(raw), script.extract_epub_text(str(path), include_report=True))

    def test_skipped_spine_targets_and_empty_body_labels_are_explained(self):
        raw = archive(extra_manifest='<item id="lost" href="lost.xhtml" media-type="application/xhtml+xml"/><item id="image" href="image.png" media-type="image/png"/>',
                      extra_spine='<itemref idref="lost"/><itemref idref="image"/>', body='<html><body></body></html>')
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(output, 'w') as updated:
            for info in original.infolist():
                content = original.read(info)
                if info.filename == 'nav.xhtml':
                    content = content.replace(b'</nav>', b'<a href="lost.xhtml">Lost</a><a href="image.png">Image</a><a href="body.xhtml">Empty chapter</a><a href="https://example.org">External</a><a>Absent</a><a href="body.xhtml"></a></nav>')
                updated.writestr(info, content)
        raw = output.getvalue()
        text, report = self.extract(raw)
        self.assertEqual(self.extract(raw, False), text)
        targets = {row['label']: row['reason'] for row in report['rows'] if row['kind'] == 'navigation_target'}
        self.assertEqual('missing_archive_resource', targets['Lost'])
        self.assertEqual('non_html_resource', targets['Image'])
        self.assertIn('navigation_label_only', [row.get('reason') for row in report['rows']])
        self.assertTrue({'unsupported_target', 'missing_href', 'empty_label'} <= {row['reason'] for row in report['rows'] if row['kind'] == 'navigation_link'})


class EpubReportUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_response_matches_published_text_and_default_upload(self):
        async with uploads.UploadPublicationSourceTests().environment() as (_, directory, client):
            raw = archive()
            first = await client.post('/api/upload?select_active=false', files={'file': ('sample.epub', raw)})
            self.assertEqual(200, first.status_code, first.text)
            self.assertNotIn('extraction_report', first.json())
            path = Path(first.json()['path'])
            before = path.read_bytes()
            second = await client.post('/api/upload?select_active=false&include_extraction_report=true', files={'file': ('sample.epub', raw)})
            self.assertEqual(200, second.status_code, second.text)
            self.assertEqual(before, path.read_bytes())
            self.assertTrue(second.json()['reused'])
            self.assertEqual(hashlib.sha256(before).hexdigest(), second.json()['extraction_report']['extracted_text_sha256'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), second.json()['extraction_report']['source_sha256'])
            captured = {item.name: item.read_bytes() for item in directory.iterdir()}
            failed = await client.post('/api/upload?select_active=false&include_extraction_report=true', files={'file': ('bad.epub', b'bad zip')})
            self.assertEqual(400, failed.status_code)
            self.assertEqual(captured, {item.name: item.read_bytes() for item in directory.iterdir()})
