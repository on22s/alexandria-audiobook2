"""EPUB elements marked with the HTML ``hidden`` attribute are not narrated (#2326).

Synthetic EPUB/XHTML fixtures only. Covers the single shared predicate
(``alexandria_alignment.is_html_hidden_attrs``), ``_HTMLTextExtractor``,
``_omit_xml_ignored_subtrees``, ``extract_epub_text`` (with extraction report
and navigation targets), ``epub_readaloud`` (chunk-timed and Qwen modes), and
``alexandria_alignment.load_epub``.
"""
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import utils  # noqa: F401  # inserts repo root on sys.path for alexandria_alignment
import alexandria_alignment as alignment
from epub_readaloud import (
    _get_optional_titles,
    build_epub_readaloud,
    get_spoken_text,
    get_xhtml_segments,
    validate_epub_readaloud,
)
from routers import script
from routers.script import _HTMLTextExtractor, _omit_xml_ignored_subtrees, extract_epub_text
from tests.test_readaloud_extraction_followups import _clips, _script, _tokens


ISSUE_SNIPPET = (
    '<p>Seen first.</p><p hidden="hidden">Never shown.</p>\n'
    '<div hidden=""><p>Never shown either.</p></div><p>Seen last.</p>'
)


def make_epub(body, *, media_type='application/xhtml+xml', toc_links=None,
              extra_html_attrs='', raw_document=None, head='<title>Synthetic</title>'):
    """Build a minimal synthetic EPUB 3 archive with one spine document."""
    if raw_document is not None:
        doc = raw_document
    else:
        doc = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml"{extra_html_attrs}>'
            f'<head>{head}</head>\n<body>\n{body}\n</body></html>'
        )
    links = toc_links if toc_links is not None else [('ch1.xhtml', 'Chapter One')]
    nav_items = ''.join(f'<li><a href="{href}">{label}</a></li>' for href, label in links)
    nav_doc = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">'
        '<head><title>Nav</title></head><body>'
        f'<nav epub:type="toc"><ol>{nav_items}</ol></nav></body></html>'
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="uid">urn:test:2326</dc:identifier>'
        '<dc:title>Synthetic</dc:title><dc:language>en</dc:language></metadata>'
        '<manifest>'
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        f'<item id="c1" href="ch1.xhtml" media-type="{media_type}"/>'
        '</manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            'META-INF/container.xml',
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr('OEBPS/content.opf', opf)
        zf.writestr('OEBPS/nav.xhtml', nav_doc)
        zf.writestr('OEBPS/ch1.xhtml', doc)
    return buf.getvalue()


class HtmlHiddenPredicateTests(unittest.TestCase):
    def test_predicate_accepts_all_boolean_case_until_found_and_xhtml_namespace_forms(self):
        xhtml_ns = {'h': 'http://www.w3.org/1999/xhtml'}
        positives = [
            [('hidden', None)],
            [('hidden', '')],
            [('hidden', 'hidden')],
            [('hidden', 'until-found')],
            [('hidden', 'UNTIL-FOUND')],
            [('hidden', 'false')],
            [('HIDDEN', None)],
            [('Hidden', '')],
            [('HiDdEn', 'HIDDEN')],
            {'hidden': ''},
            {'hidden': 'hidden'},
            {'hidden': 'until-found'},
            {'{http://www.w3.org/1999/xhtml}hidden': ''},
            {'{http://www.w3.org/1999/xhtml}hidden': 'until-found'},
        ]
        for attrs in positives:
            with self.subTest(attrs=attrs):
                self.assertTrue(alignment.is_html_hidden_attrs(attrs))
        self.assertTrue(alignment.is_html_hidden_attrs(
            {'h:hidden': 'hidden'}, xml_namespaces=xhtml_ns))
        self.assertTrue(alignment.is_html_hidden_attrs(
            {'h:hidden': 'until-found'}, xml_namespaces=xhtml_ns))

    def test_predicate_rejects_css_aria_data_and_foreign_namespaces(self):
        negatives = [
            None,
            [],
            {},
            [('style', 'display:none')],
            [('style', 'visibility:hidden')],
            [('aria-hidden', 'true')],
            [('data-hidden', 'true')],
            [('class', 'hidden')],
            [('id', 'hidden')],
            {'{urn:example}hidden': 'hidden'},
            {'{http://www.idpf.org/2007/ops}hidden': 'hidden'},
            {'x:hidden': 'hidden'},
        ]
        for attrs in negatives:
            with self.subTest(attrs=attrs):
                self.assertFalse(alignment.is_html_hidden_attrs(attrs))
        self.assertFalse(alignment.is_html_hidden_attrs(
            {'x:hidden': 'hidden'}, xml_namespaces={'x': 'urn:example'}))


class HtmlHiddenExtractionTests(unittest.TestCase):
    def test_issue_reproduction_all_extraction_paths(self):
        # Direct _HTMLTextExtractor
        ext = _HTMLTextExtractor()
        ext.feed(ISSUE_SNIPPET)
        self.assertEqual('Seen first.\n\nSeen last.',
                         script.normalize_extracted_text(ext.get_text()))

        # Full XHTML EPUB extraction
        raw = make_epub(ISSUE_SNIPPET, toc_links=[])
        self.assertEqual('Seen first.\n\nSeen last.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

        # Full text/html EPUB extraction
        raw_html = make_epub(ISSUE_SNIPPET, media_type='text/html', toc_links=[])
        self.assertEqual('Seen first.\n\nSeen last.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_html))

        # Read-aloud segmentation
        _root, segments, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
        self.assertEqual(['Seen first.', 'Seen last.'], [s['text'] for s in segments])

        # Alignment EPUB loader
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'repro.epub'
            epub_path.write_bytes(raw)
            loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
        self.assertEqual('Seen first. Seen last.', loaded)

    def test_all_boolean_spellings_and_until_found_omitted_in_html_and_xhtml(self):
        # HTML spellings including bare attribute, mixed case, and until-found
        # (until-found hides content until interactive find-in-page, so linear
        # narration omits it).
        html_cases = [
            ('bare', '<p>Start.</p><p hidden>Secret.</p><p>End.</p>'),
            ('empty', '<p>Start.</p><p hidden="">Secret.</p><p>End.</p>'),
            ('canonical', '<p>Start.</p><p hidden="hidden">Secret.</p><p>End.</p>'),
            ('until_found', '<p>Start.</p><p hidden="until-found">Secret.</p><p>End.</p>'),
            ('until_found_upper', '<p>Start.</p><p hidden="UNTIL-FOUND">Secret.</p><p>End.</p>'),
            ('upper_attr', '<p>Start.</p><p HIDDEN>Secret.</p><p>End.</p>'),
            ('mixed_case', '<p>Start.</p><p HiDdEn="Hidden">Secret.</p><p>End.</p>'),
            ('false_still_hidden', '<p>Start.</p><p hidden="false">Secret.</p><p>End.</p>'),
        ]
        for name, body in html_cases:
            with self.subTest(path='text/html', case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual('Start.\n\nEnd.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

        xhtml_cases = [
            ('empty', '<p>Start.</p><p hidden="">Secret.</p><p>End.</p>', ''),
            ('canonical', '<p>Start.</p><p hidden="hidden">Secret.</p><p>End.</p>', ''),
            ('until_found', '<p>Start.</p><p hidden="until-found">Secret.</p><p>End.</p>', ''),
            ('upper_attr', '<p>Start.</p><p HIDDEN="HIDDEN">Secret.</p><p>End.</p>', ''),
            ('xhtml_prefixed_attr',
             '<p>Start.</p><p h:hidden="hidden">Secret.</p><p>End.</p>',
             ' xmlns:h="http://www.w3.org/1999/xhtml"'),
            ('xhtml_prefixed_until_found',
             '<p>Start.</p><p h:hidden="until-found">Secret.</p><p>End.</p>',
             ' xmlns:h="http://www.w3.org/1999/xhtml"'),
        ]
        for name, body, extra_ns in xhtml_cases:
            with self.subTest(path='xhtml', case=name):
                raw = make_epub(body, extra_html_attrs=extra_ns, toc_links=[])
                self.assertEqual('Start.\n\nEnd.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
                _root, segments, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
                self.assertEqual(['Start.', 'End.'], [s['text'] for s in segments])

        # Unnamespaced XHTML root document
        raw_unns = make_epub(
            '',
            raw_document='<html><body><p>Start.</p><p hidden="">Secret.</p><p>End.</p></body></html>',
            toc_links=[],
        )
        self.assertEqual('Start.\n\nEnd.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_unns))

    def test_xml_pass_handles_cdata_and_same_tag_self_closing_hidden_elements(self):
        # Literal </p> inside CDATA inside a hidden element would prematurely
        # close HTMLParser if _omit_xml_ignored_subtrees did not omit it first.
        cdata_doc = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<p>Before.</p>'
            '<p hidden="hidden"><![CDATA[</p>Leaked secret]]></p>'
            '<p>After.</p></body></html>'
        )
        ET.fromstring(cdata_doc.encode('utf-8'))
        raw = make_epub('', raw_document=cdata_doc, toc_links=[])
        self.assertEqual('Before.\n\nAfter.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        omitted = _omit_xml_ignored_subtrees(cdata_doc)
        self.assertNotIn('Leaked secret', omitted)
        self.assertIn('<p>Before.</p>', omitted)
        self.assertIn('<p>After.</p>', omitted)

        # Self-closing hidden element sharing its parent's tag name must not
        # consume the parent's closing tag (even with '>' or '/>' inside attrs/text).
        self_closing_cases = [
            ('same_tag_self_closing',
             '<html><body><div><div hidden=""/></div><p>After.</p></body></html>',
             '<html><body><div></div><p>After.</p></body></html>'),
            ('gt_in_attr_self_closing',
             '<html><body><div><div title="a>b" hidden=""/></div><p>After.</p></body></html>',
             '<html><body><div></div><p>After.</p></body></html>'),
            ('slash_gt_in_text_paired',
             '<html><body><div><div hidden="">a/></div></div><p>After.</p></body></html>',
             '<html><body><div></div><p>After.</p></body></html>'),
            ('void_self_closing',
             '<html><body><p>Before.</p><br hidden=""/><img hidden="hidden" src="x.png"/><p>After.</p></body></html>',
             '<html><body><p>Before.</p><p>After.</p></body></html>'),
        ]
        for name, source, expected in self_closing_cases:
            with self.subTest(case=name):
                ET.fromstring(source.encode('utf-8'))
                self.assertEqual(expected, _omit_xml_ignored_subtrees(source))
                raw_case = make_epub('', raw_document=source, toc_links=[])
                self.assertIn('After.', extract_epub_text('synthetic.epub', archive_bytes=raw_case))

    def test_void_self_closing_nested_and_malformed_markup_in_html_and_xhtml(self):
        # Hidden void elements in HTML must not unbalance the skip stack.
        html_void = (
            '<p>First.</p>'
            '<br hidden><img hidden src="a.png"><hr hidden=""><input hidden type="text">'
            '<br hidden/><p hidden=""/>'
            '<p>Second.</p>'
        )
        raw_void = make_epub(html_void, media_type='text/html', toc_links=[])
        self.assertEqual('First.\n\nSecond.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_void))

        # Nested hidden inside hidden and nested same-tag elements inside hidden.
        nested = (
            '<p>Before.</p>'
            '<div hidden="">'
            '<div>Nested div<p hidden="">Double hidden</p><br/></div>'
            '<p>Still inside outer hidden.</p>'
            '</div>'
            '<p>After.</p>'
        )
        for media_type in ('application/xhtml+xml', 'text/html'):
            with self.subTest(case='nested', media_type=media_type):
                raw = make_epub(nested, media_type=media_type, toc_links=[])
                self.assertEqual('Before.\n\nAfter.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

        # Malformed/unclosed HTML cases on text/html path.
        malformed_html_cases = [
            ('unclosed_child_in_hidden_div',
             '<p>Before.</p><div hidden=""><p>Unclosed child</div><p>After.</p>',
             'Before.\n\nAfter.'),
            ('implicit_p_close_on_hidden_p',
             '<p>Before.</p><p hidden>Hidden without close<p>After.</p>',
             'Before.\n\nAfter.'),
            ('stray_close_inside_hidden',
             '<p>Before.</p><div hidden=""><p>Secret</span>Still secret</p></div><p>After.</p>',
             'Before.\n\nAfter.'),
            ('unclosed_hidden_at_eof',
             '<p>Before.</p><div hidden=""><p>Hidden until EOF',
             'Before.'),
            ('hidden_textarea_at_eof',
             '<p>Before.</p><textarea hidden>Hidden form text',
             'Before.'),
        ]
        for name, body, expected in malformed_html_cases:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

        # Malformed unclosed hidden element in strict XHTML rejects actionably
        # without leaking hidden content.
        bad_xhtml = (
            '<html><body><p>Before.</p>'
            '<div hidden=""><p>Unclosed hidden secret</body></html>'
        )
        raw_bad = make_epub('', raw_document=bad_xhtml, toc_links=[])
        with self.assertRaisesRegex(ValueError, r'invalid XML') as caught:
            extract_epub_text('synthetic.epub', archive_bytes=raw_bad)
        self.assertNotIn('Unclosed hidden secret', str(caught.exception))

        # Tolerant malformed XHTML whose prose merely mentions "hidden" stays accepted.
        prose_hidden = '<html><body><p>The hidden door was open.</p><p>Unclosed words'
        raw_prose = make_epub('', raw_document=prose_hidden, toc_links=[])
        self.assertEqual('The hidden door was open.\n\nUnclosed words',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_prose))

    def test_css_display_none_aria_hidden_and_foreign_namespace_are_not_dropped(self):
        body = (
            '<p style="display:none">CSS hidden stays.</p>'
            '<p style="display: none; visibility: hidden;">CSS whitespace stays.</p>'
            '<p aria-hidden="true">Aria hidden stays.</p>'
            '<p class="hidden" data-hidden="true">Class and data stay.</p>'
            '<p x:hidden="hidden">Foreign namespace stays.</p>'
            '<p xmlns:h="urn:other" h:hidden="hidden">Shadowed prefix stays.</p>'
        )
        extra_ns = ' xmlns:x="urn:example" xmlns:h="http://www.w3.org/1999/xhtml"'
        expected_lines = [
            'CSS hidden stays.',
            'CSS whitespace stays.',
            'Aria hidden stays.',
            'Class and data stay.',
            'Foreign namespace stays.',
            'Shadowed prefix stays.',
        ]
        expected_text = '\n\n'.join(expected_lines)

        raw_xhtml = make_epub(body, extra_html_attrs=extra_ns, toc_links=[])
        text, report = extract_epub_text('synthetic.epub', archive_bytes=raw_xhtml, include_report=True)
        self.assertEqual(expected_text, text)
        self.assertEqual([], [r for r in report['rows'] if r['kind'] == 'hidden_content'])

        raw_html = make_epub(body, media_type='text/html', extra_html_attrs=extra_ns, toc_links=[])
        self.assertEqual(expected_text, extract_epub_text('synthetic.epub', archive_bytes=raw_html))

        _root, segments, _full = get_xhtml_segments(raw_xhtml, 'OEBPS/ch1.xhtml')
        self.assertEqual(expected_lines, [s['text'] for s in segments])

        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'keep.epub'
            epub_path.write_bytes(raw_xhtml)
            loaded = alignment.load_epub(str(epub_path))
        for line in expected_lines:
            self.assertIn(line, loaded)

    def test_extraction_report_records_hidden_omission_without_leaking_text(self):
        secret = 'CONFIDENTIAL_HIDDEN_SENTINEL_2326'
        body = (
            '<p>Seen first.</p>\n'
            f'<p hidden="hidden">{secret}</p>\n'
            f'<div hidden=""><p hidden="">Nested {secret}</p></div>\n'
            '<p>Seen last.</p>'
        )
        for media_type in ('application/xhtml+xml', 'text/html'):
            with self.subTest(media_type=media_type):
                raw = make_epub(body, media_type=media_type, toc_links=[])
                plain = extract_epub_text('synthetic.epub', archive_bytes=raw)
                reported, report = extract_epub_text('synthetic.epub', archive_bytes=raw, include_report=True)
                self.assertEqual(plain, reported)
                self.assertEqual('Seen first.\n\nSeen last.', reported)
                self.assertIn('html_hidden_not_spoken', report['content_policy'])
                hidden_rows = [r for r in report['rows'] if r['kind'] == 'hidden_content']
                self.assertEqual(1, len(hidden_rows))
                row = hidden_rows[0]
                self.assertEqual('OEBPS/ch1.xhtml', row['resource'])
                self.assertEqual(0, row['spine_index'])
                self.assertEqual('html_hidden_attribute', row['reason'])
                # Outermost hidden elements only: <p hidden> + <div hidden> = 2
                # (the nested <p hidden> inside <div hidden> is not double-counted).
                self.assertEqual(2, row['omitted_elements'])
                self.assertEqual('line 5, <p>', row['first_location'])
                self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))

    def test_navigation_targets_inside_hidden_subtree_are_reported_and_not_inserted(self):
        body = (
            '<p id="h-self" hidden="hidden">Hidden self.</p>'
            '<div hidden=""><h2 id="h-desc">Hidden Descendant</h2><p>Hidden body.</p></div>'
            '<div hidden=""><a name="h-name">Hidden Named</a></div>'
            '<p id="dup" hidden="hidden">Hidden duplicate.</p>'
            '<p id="dup">Visible duplicate.</p>'
            '<p id="vis">Visible paragraph.</p>'
        )
        toc_links = [
            ('ch1.xhtml#h-self', 'Hidden Self Title'),
            ('ch1.xhtml#h-desc', 'Hidden Desc Title'),
            ('ch1.xhtml#h-name', 'Hidden Name Title'),
            ('ch1.xhtml#dup', 'Visible Dup Title'),
            ('ch1.xhtml#vis', 'Visible Section Title'),
            ('ch1.xhtml#missing', 'Missing Title'),
        ]
        for media_type in ('application/xhtml+xml', 'text/html'):
            with self.subTest(media_type=media_type):
                raw = make_epub(body, media_type=media_type, toc_links=toc_links)
                text, report = extract_epub_text('synthetic.epub', archive_bytes=raw, include_report=True)
                self.assertEqual(extract_epub_text('synthetic.epub', archive_bytes=raw), text)
                self.assertNotIn('Hidden Self Title', text)
                self.assertNotIn('Hidden Desc Title', text)
                self.assertNotIn('Hidden Name Title', text)
                self.assertNotIn('Missing Title', text)
                self.assertIn('Visible Dup Title', text)
                self.assertIn('Visible Section Title', text)
                self.assertIn('Visible duplicate.', text)
                self.assertIn('Visible paragraph.', text)

                by_frag = {
                    r['fragment']: r
                    for r in report['rows']
                    if r['kind'] == 'navigation_target'
                }
                for hidden_frag in ('h-self', 'h-desc', 'h-name'):
                    self.assertEqual('target_in_hidden_subtree', by_frag[hidden_frag]['reason'])
                    self.assertIsNone(by_frag[hidden_frag]['position'])
                self.assertEqual('unresolved_anchor', by_frag['missing']['reason'])
                self.assertIsNone(by_frag['missing']['position'])
                self.assertEqual('insert_navigation_label', by_frag['dup']['reason'])
                self.assertEqual('insert_navigation_label', by_frag['vis']['reason'])


class HtmlHiddenReadaloudParityTests(unittest.TestCase):
    def _build_from_extracted_script(self, epub_bytes, *, qwen):
        text, chunks = _script(epub_bytes)
        entries, aligned, frame, rate = [], [], 0, 24000
        for index, chunk in enumerate(chunks):
            prepared = get_spoken_text(chunk['text'])
            tokens = _tokens(prepared)
            length = int((len(tokens) * 0.25 + 0.05) * rate)
            entries.append({
                'index': index,
                'prepared_text': prepared,
                'start_frame': frame,
                'end_frame': frame + length,
                'sample_rate': rate,
            })
            aligned.append({'tokens': tokens})
            frame += length + rate // 10
        result, report = build_epub_readaloud(
            retained_epub_bytes=epub_bytes,
            audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3',
            script_entries=entries,
            aligned_rows=aligned if qwen else None,
        )
        validate_epub_readaloud(result)
        return text, result, report

    def test_end_to_end_script_and_readaloud_stay_in_sync_with_hidden_elements(self):
        body = (
            '<h1>Chapter One</h1>'
            '<p id="p-first">Seen first.</p>'
            '<p id="p-hidden" hidden="hidden">Never shown.</p>'
            '<div id="d-hidden" hidden=""><h2 id="h-hidden">Hidden Heading</h2><p>Never shown either.</p></div>'
            '<p id="p-inline">First sentence. <span hidden="until-found">Hidden aside.</span> Second sentence.</p>'
            '<div><span hidden="">Hidden direct lead.</span><p id="p-child">Inside div.</p></div>'
            '<p id="p-css" style="display:none">CSS visible to narrator.</p>'
            '<p id="p-aria" aria-hidden="true">Aria visible to narrator.</p>'
            '<p id="p-ruby"><ruby>漢字<rt hidden="">かんじ</rt></ruby> alone.</p>'
            '<br hidden=""/>'
            '<p id="p-last">Seen last.</p>'
        )
        toc_links = [
            ('ch1.xhtml', 'Chapter One'),
            ('ch1.xhtml#h-hidden', 'Hidden Heading'),
            ('ch1.xhtml#p-last', 'Seen last.'),
        ]
        raw = make_epub(body, toc_links=toc_links)

        # Read-aloud optional TOC titles must ignore anchors inside hidden
        # subtrees, matching script extraction's target_in_hidden_subtree rule.
        root, segs, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
        opt_titles = _get_optional_titles(
            root,
            segs,
            [
                {'path': 'OEBPS/ch1.xhtml', 'fragment': 'h-hidden', 'label': 'Hidden Heading'},
                {'path': 'OEBPS/ch1.xhtml', 'fragment': 'p-last', 'label': 'Seen last.'},
            ],
            'OEBPS/ch1.xhtml',
        )
        all_labels = [lbl for labels in opt_titles.values() for lbl in labels]
        self.assertNotIn('Hidden Heading', all_labels)
        self.assertIn('Seen last.', all_labels)

        # Direct hidden lead inside <div> must not coarsen the <div> into one segment.
        self.assertIn('Inside div.', [s['text'] for s in segs])
        self.assertEqual('p-child', next(s['fragment'] for s in segs if s['text'] == 'Inside div.'))

        for qwen in (False, True):
            with self.subTest(qwen=qwen):
                text, built, _ra_report = self._build_from_extracted_script(raw, qwen=qwen)
                self.assertNotIn('Never shown', text)
                self.assertNotIn('Hidden Heading', text)
                self.assertNotIn('Hidden aside', text)
                self.assertNotIn('Hidden direct lead', text)
                self.assertNotIn('かんじ', text)
                self.assertIn('漢字 alone.', text)
                self.assertIn('CSS visible to narrator.', text)
                self.assertIn('Aria visible to narrator.', text)

                clips = _clips(built, 'ch1')
                clip_ids = [frag for frag, _b, _e in clips]
                # Hidden paragraphs/containers must never receive SMIL clips.
                self.assertNotIn('p-hidden', clip_ids)
                self.assertNotIn('d-hidden', clip_ids)
                self.assertNotIn('h-hidden', clip_ids)
                if qwen:
                    # Inline hidden span in p-inline still allows sentence-level
                    # splitting of "First sentence." and "Second sentence."
                    # (emitting two generated span clips instead of falling back
                    # to whole-paragraph clip 'p-inline') and preserves the
                    # hidden span verbatim in the output XHTML.
                    with zipfile.ZipFile(io.BytesIO(built)) as zf:
                        ch1_xml = zf.read('OEBPS/ch1.xhtml').decode('utf-8')
                    self.assertIn('Hidden aside.', ch1_xml)
                    self.assertNotIn('p-inline', clip_ids)
                    self.assertEqual(9, len(clips), clips)
                else:
                    self.assertGreaterEqual(len(clips), 1)

    def test_alignment_loader_handles_hidden_namespaces_and_out_of_scope_attrs(self):
        body = (
            '<p>Visible one.</p>'
            '<p hidden="hidden">Drop canonical.</p>'
            '<p hidden="until-found">Drop until found.</p>'
            '<p h:hidden="hidden">Drop xhtml prefixed.</p>'
            '<p x:hidden="hidden">Keep foreign prefixed.</p>'
            '<p style="display:none">Keep CSS.</p>'
            '<p aria-hidden="true">Keep aria.</p>'
        )
        raw = make_epub(
            body,
            extra_html_attrs=' xmlns:h="http://www.w3.org/1999/xhtml" xmlns:x="urn:example"',
            toc_links=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'align.epub'
            epub_path.write_bytes(raw)
            loaded = alignment.load_epub(str(epub_path))
        self.assertIn('Visible one.', loaded)
        self.assertIn('Keep foreign prefixed.', loaded)
        self.assertIn('Keep CSS.', loaded)
        self.assertIn('Keep aria.', loaded)
        self.assertNotIn('Drop canonical.', loaded)
        self.assertNotIn('Drop until found.', loaded)
        self.assertNotIn('Drop xhtml prefixed.', loaded)


if __name__ == '__main__':
    unittest.main()
