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
    _inline_events,
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


def make_multi_epub(docs, *, media_type='application/xhtml+xml'):
    """Build a minimal synthetic EPUB 3 archive with multiple raw spine documents."""
    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
    ]
    spine_refs = []
    for idx, (href, _raw) in enumerate(docs):
        manifest_items.append(f'<item id="c{idx}" href="{href}" media-type="{media_type}"/>')
        spine_refs.append(f'<itemref idref="c{idx}"/>')
    nav_doc = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">'
        '<head><title>Nav</title></head><body>'
        '<nav epub:type="toc"><ol></ol></nav></body></html>'
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="uid">urn:test:2326-multi</dc:identifier>'
        '<dc:title>Synthetic</dc:title><dc:language>en</dc:language></metadata>'
        f'<manifest>{"".join(manifest_items)}</manifest>'
        f'<spine>{"".join(spine_refs)}</spine></package>'
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
        for href, raw in docs:
            zf.writestr(f'OEBPS/{href}', raw)
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

    def test_predicate_ignores_non_html_element_namespaces(self):
        xhtml_ns = {'h': 'http://www.w3.org/1999/xhtml'}
        for ns in ('http://www.w3.org/2000/svg', 'http://www.w3.org/1998/Math/MathML', 'urn:example'):
            with self.subTest(element_namespace=ns):
                self.assertFalse(alignment.is_html_hidden_attrs(
                    {'hidden': ''}, element_namespace=ns))
                self.assertFalse(alignment.is_html_hidden_attrs(
                    {'h:hidden': 'hidden'}, xml_namespaces=xhtml_ns, element_namespace=ns))
        for ns in (None, '', 'http://www.w3.org/1999/xhtml'):
            with self.subTest(html_namespace=ns):
                self.assertTrue(alignment.is_html_hidden_attrs(
                    {'hidden': ''}, element_namespace=ns))


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
             # A stray close makes the prepass distrust the region (Rule 15):
             # the hidden div is treated as visible, a leak, not a drop.
             'Before.\n\nSecretStill secret\n\nAfter.'),
            ('unclosed_hidden_at_eof',
             '<p>Before.</p><div hidden=""><p>Hidden until EOF',
             'Before.'),
            ('hidden_textarea_at_eof',
             '<p>Before.</p><textarea hidden>Hidden form text',
             # An unclosed textarea is RCDATA to EOF and never popped, so the
             # structural gate treats its `hidden` as absent and narrates it.
             'Before.Hidden form text\n</body></html>'),
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

    def test_tolerant_html_implied_end_tags_and_ancestor_closes_never_over_drop(self):
        cases = [
            ('li_omitted_end',
             '<ul><li hidden="">Secret item<li>Visible item</ul><p>After.</p>',
             'Visible item\n\nAfter.'),
            ('td_omitted_end',
             '<table><tr><td hidden="">Secret<td>Visible</tr></table><p>After.</p>',
             'Visible\n\nAfter.'),
            ('th_omitted_end',
             '<table><tr><th hidden="">Secret<th>Visible</tr></table><p>After.</p>',
             'Visible\n\nAfter.'),
            ('tr_omitted_end',
             '<table><tr hidden=""><td>Secret<tr><td>Visible</table><p>After.</p>',
             'Visible\n\nAfter.'),
            ('dd_omitted_end',
             '<dl><dt>T<dd hidden="">Secret<dd>Visible</dl><p>After.</p>',
             'TVisible\n\nAfter.'),
            ('dt_omitted_end',
             '<dl><dt hidden="">Secret<dt>Visible</dl><p>After.</p>',
             'Visible\n\nAfter.'),
            ('option_omitted_end',
             '<select><option hidden="">Secret<option>Visible</select><p>After.</p>',
             'Visible\n\nAfter.'),
            ('unclosed_hidden_span_closed_by_p',
             '<p>Visible start. <span hidden="">secret</p><p>After paragraph.</p>',
             'Visible start. \n\nAfter paragraph.'),
            ('unclosed_hidden_span_closed_by_div',
             '<div>Visible start. <span hidden="">secret</div><p>After paragraph.</p>',
             'Visible start. \n\nAfter paragraph.'),
            ('unclosed_hidden_em_closed_by_p',
             '<p>Visible start. <em hidden="">secret</p><p>After paragraph.</p>',
             'Visible start. \n\nAfter paragraph.'),
            ('unclosed_hidden_span_closed_by_implied_p',
             '<p>One <span hidden>x<p>Two.</p>',
             'One \n\nTwo.'),
            ('hidden_p_closed_by_form',
             '<p hidden="">Secret<form><p>Visible form</p></form><p>After.</p>',
             'Visible form\n\nAfter.'),
            ('nested_list_inside_hidden_li_stays_hidden',
             '<ul><li hidden="">Secret<ul><li>Nested secret</li></ul><li>Visible</ul><p>After.</p>',
             'Visible\n\nAfter.'),
            ('nested_table_inside_hidden_td_stays_hidden',
             '<table><tr><td hidden="">Secret<table><tr><td>Nested secret</td></tr></table>'
             '<td>Visible</tr></table><p>After.</p>',
             'Visible\n\nAfter.'),
            ('void_inside_hidden_container',
             '<div hidden=""><br><img src="x.png"><p>Secret</p></div><p>After.</p>',
             'After.'),
        ]
        for name, body, expected in cases:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

        # Every HTML5 <p>-closing start tag closes an open <p hidden="">.
        # The case list is generated from the extractor's own table so a tag
        # added to (or dropped from) the table cannot silently skip its test,
        # and the table itself is pinned to the spec's exhaustive set.
        spec_p_closers = frozenset({
            'address', 'article', 'aside', 'blockquote', 'center', 'dd',
            'details', 'dialog', 'dir', 'div', 'dl', 'dt', 'fieldset',
            'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4',
            'h5', 'h6', 'header', 'hgroup', 'hr', 'li', 'listing', 'main',
            'menu', 'nav', 'ol', 'p', 'plaintext', 'pre', 'search', 'section',
            'summary', 'table', 'ul', 'xmp',
        })
        self.assertEqual(spec_p_closers, _HTMLTextExtractor._P_AUTO_CLOSE_TAGS)
        for tag in sorted(_HTMLTextExtractor._P_AUTO_CLOSE_TAGS):
            with self.subTest(p_closer=tag):
                if tag == 'plaintext':
                    raw = make_epub(
                        '', media_type='text/html', toc_links=[],
                        raw_document='<html><body><p hidden="">Secret<plaintext>Visible')
                    expected = 'Visible'
                else:
                    body = f'<p hidden="">Secret<{tag}>Visible</{tag}><p>After.'
                    expected = 'Visible\n\nAfter.'
                    raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

        # A second <caption>/<colgroup> closes the first in table scope.
        self.assertEqual('VisibleTail.', extract_epub_text(
            'synthetic.epub', archive_bytes=make_epub(
                '<table><caption hidden="">Secret<caption>Visible</table>Tail.',
                media_type='text/html', toc_links=[])))
        colgroup_extractor = _HTMLTextExtractor({'g'})
        colgroup_extractor.feed(
            '<table><colgroup hidden=""><col><colgroup id="g"><col></table>')
        self.assertEqual(set(), colgroup_extractor.hidden_anchors)

        # An unclosed <ruby> at EOF fails open: its base text is narrated
        # instead of being lost with the collector, and a kana reading still
        # replaces its base.
        for body, expected in (
            ('<p>Before.</p><ruby>BaseWithoutClose', 'Before.BaseWithoutClose'),
            ('<ruby>Base<rt>reading', 'Base'),
            ('<p>V</p><ruby>漢字<rt>かんじ', 'V漢字'),
        ):
            with self.subTest(unclosed_ruby=body):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_li_dd_dt_implied_close_stops_at_special_elements(self):
        # HTML5 "close a li/dd/dt element" stops at any special element
        # except the rule's own exceptions, so a hidden list item stays open
        # across <section>/<blockquote> (the new item is hidden too).
        hidden_cases = [
            ('li_section',
             '<ul><li hidden="">Secret<section><li>Visible item</section></ul><p>After.</p>'),
            ('li_blockquote',
             '<ul><li hidden="">Secret<blockquote><li>Visible item</blockquote></ul><p>After.</p>'),
            ('dd_section',
             '<dl><dd hidden="">Secret<section><dd>Visible item</section></dl><p>After.</p>'),
            ('dt_blockquote',
             '<dl><dt hidden="">Secret<blockquote><dt>Visible item</blockquote></dl><p>After.</p>'),
        ]
        for name, body in hidden_cases:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual('After.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

        # address/div/p are the li rule's exceptions (address/div/p for
        # dd/dt too): the scan continues and the hidden item is closed.
        self.assertEqual(frozenset({'dd', 'dt'}), _HTMLTextExtractor._DD_DT_RULE[0])
        self.assertEqual(frozenset({'address', 'div', 'p'}),
                         _HTMLTextExtractor._DD_DT_RULE[2])
        visible_cases = [
            ('li_div_exception',
             '<ul><li hidden="">Secret<div><li>Visible item</div></ul><p>After.</p>'),
            ('li_p_exception',
             '<ul><li hidden="">Secret<p><li>Visible item</ul><p>After.</p>'),
            ('dd_div_exception',
             '<dl><dd hidden="">Secret<div><dd>Visible item</div></dl><p>After.</p>'),
            # dd/dt's spec exception list includes p: an open p (inserted into
            # the hidden dd) does not stop the close scan.
            ('dd_p_exception',
             '<dl><dd hidden="">Secret<p>Hidden para<dd>Visible item</dl><p>After.</p>'),
            ('dt_p_exception',
             '<dl><dt hidden="">Secret<p>Hidden para<dt>Visible item</dl><p>After.</p>'),
        ]
        for name, body in visible_cases:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual('Visible item\n\nAfter.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_heading_start_tag_closes_open_heading_on_valid_html5(self):
        # Omitting </h2> before another heading is valid HTML5. The heading
        # start tag closes the open p and pops a current heading, so a hidden
        # heading with an omitted end tag can never silence the next chapter.
        raw = make_epub(
            '<h1>Chapter 1</h1><h2 hidden="">Draft heading<h2>Chapter 2</h2>'
            '<p>This paragraph is visible prose that must be narrated.</p>',
            media_type='text/html', toc_links=[])
        self.assertEqual(
            'Chapter 1\n\nChapter 2\n\n'
            'This paragraph is visible prose that must be narrated.',
            extract_epub_text('synthetic.epub', archive_bytes=raw))
        # One case per heading pair, generated from the extractor's own set so
        # the coverage cannot drift from the rule.
        self.assertEqual(
            frozenset({'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}),
            _HTMLTextExtractor._HEADING_TAGS)
        for outer in sorted(_HTMLTextExtractor._HEADING_TAGS):
            for inner in sorted(_HTMLTextExtractor._HEADING_TAGS):
                with self.subTest(outer=outer, inner=inner):
                    raw = make_epub(f'<{outer} hidden="">Secret<{inner}>Visible</{inner}>'
                                    '<p>After.</p>',
                                    media_type='text/html', toc_links=[])
                    self.assertEqual('Visible\n\nAfter.',
                                     extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_ignored_start_tags_do_not_open_hidden_roots(self):
        # HTML5 ignores table-structure tokens outside a table and a nested
        # <form> start tag. An ignored token creates no element, so its
        # `hidden` attribute must not hide the rest of the chapter.
        self.assertEqual(
            frozenset({'caption', 'col', 'colgroup', 'td', 'th', 'tbody',
                       'tfoot', 'thead', 'tr'}),
            _HTMLTextExtractor._TABLE_CONTEXT_ONLY_TAGS)
        cases = [
            ('caption_outside_table',
             '<p>VISIBLEONE</p><caption hidden="">SECRETHID<p>VISIBLETWO</p>',
             'VISIBLEONESECRETHID\n\nVISIBLETWO'),
            ('td_outside_table',
             '<p>VISIBLEONE</p><td hidden="">SECRETHID<p>VISIBLETWO</p>',
             'VISIBLEONE\nSECRETHID\n\nVISIBLETWO'),
            ('th_outside_table',
             '<p>VISIBLEONE</p><th hidden="">SECRETHID<p>VISIBLETWO</p>',
             'VISIBLEONE\nSECRETHID\n\nVISIBLETWO'),
            ('tr_outside_table_with_cell',
             '<p>VISIBLEONE</p><tr hidden=""><td>SECRETHID</td><p>VISIBLETWO</p>',
             'VISIBLEONE\n\nSECRETHID\n\nVISIBLETWO'),
            ('tbody_outside_table',
             '<p>VISIBLEONE</p><tbody hidden="">SECRETHID<p>VISIBLETWO</p>',
             'VISIBLEONESECRETHID\n\nVISIBLETWO'),
            ('nested_form',
             '<form><form hidden="">SECRETHID<fieldset>VISIBLEONE</fieldset></form></form>',
             'SECRETHIDVISIBLEONE'),
            # A <tr>/<td> closes an open caption/colgroup in table scope, so
            # the caption's hidden region ends where the browser ends it.
            ('unclosed_caption_before_tr',
             '<table><caption hidden="">SECRETCAP<tr><td>VISIBLECELL</td></tr></table>',
             'VISIBLECELL'),
            ('unclosed_caption_before_td',
             '<table><caption hidden="">SECRETCAP<td>VISIBLECELL</table>',
             'VISIBLECELL'),
            ('unclosed_colgroup_before_tr',
             '<table><colgroup hidden=""><col><tr><td>VISIBLECELL</td></tr></table>',
             'VISIBLECELL'),
            # An ignored token is not pushed onto the open-element stack, so it
            # cannot stop a later implied-close scan at a special element the
            # browser never created.
            ('ignored_td_does_not_stop_dt_scan',
             '<dt><div hidden=""><td><dt>VisibleText', 'VisibleText'),
            ('ignored_caption_does_not_stop_p_scan',
             '<p><caption><span hidden=""><center>VisibleText', 'VisibleText'),
            # A nested (ignored) <form> must not run the <p>-closing rule, so
            # the open hidden <p> stays open in the model exactly as in the
            # browser; because a nested form is unmodelled error recovery, the
            # structural prepass distrusts the document and leaks it instead
            # of proving the region (Rule 15, safe direction).
            ('ignored_nested_form_poisons_hidden_decision',
             '<form><p hidden="">SECRETHID<span>X<form hidden="">VISIBLEONE',
             'SECRETHIDXVISIBLEONE'),
            ('ignored_tr_does_not_close_hidden_li',
             '<caption><li hidden=""><tr><blockquote hidden=""></li>VisibleText',
             'VisibleText'),
        ]
        self.assertEqual(
            frozenset({'tr', 'td', 'th', 'caption', 'colgroup'}),
            _HTMLTextExtractor._ROW_RULE[0])
        self.assertEqual(
            frozenset({'td', 'th', 'caption', 'colgroup'}),
            _HTMLTextExtractor._CELL_RULE[0])
        for name, body, expected in cases:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
        # A hidden caption/colgroup still hides its own content inside a table.
        raw = make_epub('<table><caption hidden="">SECRETCAP<tr><td>VisibleCell</td></tr></table>',
                        media_type='text/html', toc_links=[])
        self.assertEqual('VisibleCell', extract_epub_text('synthetic.epub', archive_bytes=raw))
        # The ignored tokens are not counted as omitted elements either.
        extractor = _HTMLTextExtractor(html_rcdata=True)
        extractor.feed('<p>V</p><caption hidden="">S<p>W</p>')
        self.assertEqual(0, extractor.hidden_elements)
        self.assertEqual('VS\n\nW', extractor.get_text())

    def test_leak_direction_limits_are_pinned(self):
        # Documented limits (findings 3 and 4): both narrate hidden text, the
        # safe direction for an audiobook, and neither drops visible text.
        # 1. Formatting reconstruction (adoption agency) is not modelled: a
        #    hidden <b>/<a> left open across a block is not reconstructed in
        #    the next block, so its text is narrated.
        for body in (
            '<p><b hidden="">SECRETHID<p>VISIBLEONE</p>',
            '<p><a hidden="">SECRETHID<header>VISIBLEONE</header></a></p>',
            '<p><b hidden="">SECRETHID<div>VISIBLEONE</div></b></p>',
        ):
            with self.subTest(adoption=body):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                # The browser reconstructs the hidden formatting element in the
                # new block and hides VISIBLEONE; this extractor narrates it.
                self.assertEqual('VISIBLEONE',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
        # 2. Python's HTMLParser honours the self-closing slash on a non-void
        #    HTML element; a browser ignores it. This is a divergence *from*
        #    browsers in the leak direction, kept because narrating text is
        #    safer than dropping it.
        raw = make_epub('<p>VISIBLEONE</p><div hidden=""/>SECRETHID<p>VISIBLETWO</p>',
                        media_type='text/html', toc_links=[])
        self.assertEqual('VISIBLEONESECRETHID\n\nVISIBLETWO',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_structural_gate_legacy_ignored_tokens_never_open_hidden_regions(self):
        # HTML's <image> is <img> (void), <keygen>/<frame> are void, and
        # <frameset> is ignored in body content. None creates a modelled
        # element, so a `hidden` on one must never silence later prose.
        for tag in ('image', 'keygen', 'frame', 'frameset'):
            with self.subTest(tag=tag):
                raw = make_epub(f'<p>ONE</p><{tag} hidden="">TWO<p>THREE</p>',
                                media_type='text/html', toc_links=[])
                self.assertEqual('ONETWO\n\nTHREE',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
                # A close after the token must not turn it into a region that
                # hides its content: the browser never inserted the element.
                raw = make_epub(f'<{tag} hidden="">SECRET</{tag}>VISWORD',
                                media_type='text/html', toc_links=[])
                self.assertEqual('SECRETVISWORD',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
        # A colgroup has no renderable text: browser-fostered text leaks.
        raw = make_epub('<table><colgroup hidden="">SECRET</colgroup>'
                        '<tr><td>VISWORD</td></tr></table>',
                        media_type='text/html', toc_links=[])
        self.assertEqual('SECRET\n\nVISWORD',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        # A table token at an in-body current node is ignored, not inserted.
        raw = make_epub('<table><summary><td hidden="">SECRET</td></summary>'
                        'VISWORD</table>', media_type='text/html', toc_links=[])
        self.assertEqual('SECRETVISWORD',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        # Character data inside a column group is fostered out of the table.
        raw = make_epub('<table hidden=""><colgroup>VISWORD</colgroup></table>',
                        media_type='text/html', toc_links=[])
        self.assertEqual('VISWORD',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        raw = make_epub('<table hidden=""><colgroup><p>VISWORD</p></colgroup></table>',
                        media_type='text/html', toc_links=[])
        self.assertEqual('VISWORD',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_unmodelled_closes_and_table_modes_are_leaks_never_drops(self):
        # Every review-4 drop repro, asserted on the full extraction: the
        # visible words are present. The secret may also be narrated (a
        # documented leak); a missing visible word would be a bug.
        cases = [
            ('b_before_p_close', '<b hidden="">SECRETHID<p></b>VISWORD'),
            ('em_before_p_close', '<em hidden="">SECRETHID<p></em>VISWORD'),
            ('nested_a', '<a hidden=""><a>VISWORD'),
            ('b_before_div_close', '<b hidden="">SECRETHID<div></b>VISWORD'),
            ('nested_button', '<button hidden="">SECRETHID<button>VISWORD'),
            ('nested_select', '<select hidden="">SECRETHID<select>VISWORD'),
            ('nested_nobr', '<nobr hidden="">SECRETHID<nobr>VISWORD'),
            ('table_form', '<table><form hidden="">VISWORD'),
            ('table_hidden_form', '<table hidden=""><form hidden="">VISWORD'),
            ('table_dt_colgroup', '<table><dt hidden=""><colgroup>VISWORD'),
            ('table_dd_tbody', '<table><dd hidden=""><tbody></tbody>VISWORD'),
        ]
        for name, body in cases:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                text = extract_epub_text('synthetic.epub', archive_bytes=raw)
                self.assertIn('VISWORD', text)
        # The same shapes at least never drop the prose before them.
        for name, body in cases:
            with self.subTest(prefix=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertTrue(extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_heading_rule_pops_only_the_current_node(self):
        # The spec's heading close is "current node only": an intervening span
        # means the second heading does not pop the hidden one. A scope scan
        # would narrate VISWORD; the browser hides it, so this pins the leak
        # direction against the surviving mutant. The raw document has no
        # closing body tag, so the h2 is only ended by the optional-end EOF
        # rule (not by an ancestor close).
        raw = make_epub('', media_type='text/html', toc_links=[],
                        raw_document='<h2 hidden="">SECRETHID<span><h3>VISWORD</h3></span>')
        self.assertEqual('', extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_prepass_poison_and_limits_are_pinned(self):
        # Unmodelled insertion-mode anomalies anywhere poison the whole
        # document: even a hidden region that closed cleanly before the
        # anomaly is treated as visible (leak) so no later restructure can
        # turn it into a drop.
        poisoned = [
            ('nested_select', '<p hidden="">SECRETHID</p><select><select>VISWORD'),
            ('nested_form', '<p hidden="">SECRETHID</p><form><form>VISWORD'),
            ('nested_anchor', '<p hidden="">SECRETHID</p><a><a>VISWORD'),
        ]
        for name, body in poisoned:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual('SECRETHIDVISWORD',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
        # An ancestor end tag blocked by a special element leaves the hidden
        # region open in the model, but the browser may close the ancestor:
        # the prepass distrusts it and narrates (leak), never dropping the
        # following prose.
        raw = make_epub('<p hidden="">SECRETHID<blockquote><ruby>'
                        '<summary hidden=""><b>HIDDENWORD</ruby>VISWORD',
                        media_type='text/html', toc_links=[])
        self.assertEqual('SECRETHID\n\nHIDDENWORDVISWORD',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        # Balanced hidden regions are still proved and omitted.
        raw = make_epub('<p>VISIBLEONE</p><div hidden="">SECRETHID</div>'
                        '<p>VISIBLETWO</p>', media_type='text/html', toc_links=[])
        self.assertEqual('VISIBLEONE\n\nVISIBLETWO',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        # UTF-16 limitation (pre-existing, out of scope): load_epub's
        # BeautifulSoup text path yields no words for a UTF-16 chapter
        # (whitespace only) while extract_epub_text decodes it. Pinned so the
        # PR body's Limit cannot drift from the code.
        utf16_doc = (
            '<?xml version="1.0" encoding="utf-16"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head>'
            '<body><p>VisibleOne.</p><p hidden="">Secret.</p>'
            '<p>VisibleTwo.</p></body></html>')
        raw = make_epub('', media_type='application/xhtml+xml', toc_links=[],
                        raw_document=utf16_doc.encode('utf-16'))
        self.assertEqual('VisibleOne.\n\nVisibleTwo.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'utf16.epub'
            epub_path.write_bytes(raw)
            self.assertEqual([], alignment.load_epub(str(epub_path)).split())

    def test_ignored_end_tags_make_a_hidden_region_unsafe(self):
        # HTML5 "any other end tag" stops at a special element and ignores the
        # token: </span> above <p> and </ruby> above <ol> do not close the
        # hidden element. The structural prepass cannot prove where that
        # region ends, so Rule 15 treats the hidden element as visible (a
        # leak) instead of keeping the region open and dropping visible text.
        bodies = [
            ('<span hidden="">Secret<p>para</span>Visible</p><p>After.</p>',
             'Secret\n\nparaVisible\n\nAfter.'),
            ('<ruby hidden="">Secret<ol></ruby>Visible', 'SecretVisible'),
            ('<div hidden="">Secret<p>para</span>Visible</p></div><p>After.</p>',
             'Secret\n\nparaVisible\n\nAfter.'),
        ]
        for body, expected in bodies:
            with self.subTest(body=body):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
        # Control: a non-special descendant is closed by the ancestor's end
        # tag exactly as in a browser, so the region is proved and stays
        # hidden.
        raw = make_epub('<div>Visible start. <span hidden="">secret</div><p>After.</p>',
                        media_type='text/html', toc_links=[])
        self.assertEqual('Visible start. \n\nAfter.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_table_foster_parented_content_stays_visible(self):
        # HTML5 foster-parents non-table content before the table, so it is
        # rendered outside a hidden table and must be narrated.
        cases = [
            ('fostered_text',
             '<table hidden="">Fostered visible<tr><td>Cell</td></tr></table><p>After.</p>',
             'Fostered visible\n\nAfter.'),
            ('fostered_block',
             '<table hidden=""><div>Fostered visible</div><tr><td>Cell</td></tr></table>'
             '<p>After.</p>',
             'Fostered visible\n\nAfter.'),
            ('fostered_from_hidden_tbody',
             '<table><tbody hidden="">Fostered in tbody<tr><td>Cell</td></tr></tbody></table>'
             '<p>After.</p>',
             'Fostered in tbody\n\nAfter.'),
            ('fostered_from_hidden_row',
             '<table><tr hidden="">Fostered in row<td>Cell</td></tr></table><p>After.</p>',
             'Fostered in row\n\nAfter.'),
            # A hidden element foster-parented out of the table (and left
            # unclosed) does not become the table's outside environment. The
            # prepass cannot prove where the browser placed it, so its
            # `hidden` is treated as absent and its text is narrated (a leak).
            ('fostered_element_above_table',
             '<table><ruby hidden="">Reading<tr>FosteredVisible</tr></table>',
             'ReadingFosteredVisible'),
            ('nested_table_closes_hidden_table',
             '<table hidden=""><table><tr><td>Inner</td></tr></table>'
             '<tr><td>Cell</td></tr></table><p>After.</p>',
             'Inner\n\nCell\n\nAfter.'),
            # Non-fostered table content stays hidden.
            ('hidden_cell',
             '<table hidden=""><tr><td>Cell</td></tr></table>', ''),
            # A hidden ancestor outside the table still hides fostered content.
            ('hidden_ancestor_outside_table',
             '<div hidden=""><table>Fostered inside hidden div<tr><td>Cell</td></tr>'
             '</table></div><p>After.</p>',
             'After.'),
        ]
        for name, body, expected in cases:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
        # XHTML is XML: there is no foster parenting, so the same invalid
        # markup keeps the text inside the hidden table and every XML path
        # (extract, read-aloud, load_epub) omits it.
        raw_x = make_epub(
            '<table hidden="">Fostered visible<tr><td>Cell</td></tr></table>'
            '<div hidden=""><table>Fostered inside hidden div<tr><td>Cell</td></tr>'
            '</table></div><p>After.</p>',
            toc_links=[])
        self.assertEqual('After.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_x))
        _root, segs, _full = get_xhtml_segments(raw_x, 'OEBPS/ch1.xhtml')
        self.assertEqual(['After.'], [s['text'] for s in segs])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'foster.epub'
            epub_path.write_bytes(raw_x)
            loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
        self.assertEqual('After.', loaded)

    def test_root_context_skips_template_doctype_subset_and_noscript(self):
        class _FakeItem:
            def __init__(self, content):
                self.content = content

        cases = [
            ('template_then_hidden_body',
             b'<html xmlns="http://www.w3.org/1999/xhtml"><head><template><p>t</p>'
             b'</template></head><body hidden=""><p>Secret.</p></body></html>', True),
            ('template_fake_body_only',
             b'<html><head><template><template><body hidden="">'
             b'</template></template></head><body><p>Visible.</p></body></html>', False),
            ('doctype_internal_subset_fake_body',
             b'<!DOCTYPE html [ <!ENTITY foo "bar"> <body hidden=""> ]>'
             b'<html><body><p>Visible.</p></body></html>', False),
            ('noscript_in_head_fake_body',
             b'<html><head><noscript><body hidden=""></noscript></head>'
             b'<body><p>Visible.</p></body></html>', False),
            ('textarea_in_head_fake_body',
             b'<html><head><textarea><body hidden=""></textarea></head>'
             b'<body><p>Visible.</p></body></html>', False),
            ('hidden_body_before_template',
             b'<html><head><template><p>t</p></template></head><body hidden="">x</body></html>',
             True),
        ]
        for name, raw, expected in cases:
            with self.subTest(case=name):
                self.assertEqual(expected,
                                 alignment._epub_item_root_context(_FakeItem(raw))[0])

        # End to end: a chapter whose real <body> is hidden after a template in
        # <head> is omitted by load_epub (the finding-5 regression), and a
        # DOCTYPE internal subset holding a fake <body hidden> does not refuse
        # the visible chapter.
        hidden_after_template = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><template><p>t</p>'
            '</template></head><body hidden=""><p>Secret.</p></body></html>')
        raw_hidden = make_epub('', raw_document=hidden_after_template, toc_links=[])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'template_hidden.epub'
            epub_path.write_bytes(raw_hidden)
            with self.assertRaisesRegex(
                    ValueError, r'no readable text after omitting HTML hidden elements'):
                alignment.load_epub(str(epub_path))
        doctype_subset = (
            '<!DOCTYPE html [ <!ENTITY foo "bar"> <body hidden=""> ]>'
            '<html><body><p>Visible.</p></body></html>')
        raw_subset = make_epub('', raw_document=doctype_subset, toc_links=[])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'doctype_subset.epub'
            epub_path.write_bytes(raw_subset)
            self.assertEqual('Visible.', ' '.join(
                alignment.load_epub(str(epub_path)).split()))

    def test_bare_svg_and_math_in_xhtml_use_xml_namespace_on_every_path(self):
        # XHTML without xmlns is still XML: a bare <svg>/<math> stays in the
        # inherited XHTML namespace, so hidden applies and every path agrees.
        bodies = [
            ('bare_svg', '<p>Before.</p><svg><text hidden="">SVG</text></svg><p>After.</p>'),
            ('bare_math', '<p>Before.</p><math hidden=""><mi>x</mi></math><p>After.</p>'),
        ]
        for name, body in bodies:
            with self.subTest(case=name):
                raw_x = make_epub(body, toc_links=[])
                self.assertEqual('Before.\n\nAfter.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw_x))
                _root, segs, _full = get_xhtml_segments(raw_x, 'OEBPS/ch1.xhtml')
                self.assertEqual(['Before.', 'After.'], [s['text'] for s in segs])
                with tempfile.TemporaryDirectory() as tmp:
                    epub_path = Path(tmp) / f'{name}.epub'
                    epub_path.write_bytes(raw_x)
                    loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
                self.assertEqual('Before. After.', loaded)

                # In text/html the tag name itself creates the foreign
                # namespace, so the same markup is narrated by the html.parser
                # reader (load_epub only reads XHTML items, and read-aloud is
                # XML, so neither applies to text/html media).
                raw_h = make_epub(body, media_type='text/html', toc_links=[])
                text_h = extract_epub_text('synthetic.epub', archive_bytes=raw_h)
                if name == 'bare_svg':
                    self.assertIn('SVG', text_h)
                else:
                    self.assertIn('x', text_h)

    def test_mathml_annotation_xml_html_integration_point(self):
        # annotation-xml with encoding text/html or application/xhtml+xml is
        # an HTML integration point: a hidden HTML <p> inside it is omitted by
        # both html.parser paths, matching the XML/read-aloud paths.
        for encoding in ('text/html', 'application/xhtml+xml'):
            body = (
                '<p>Before.</p><math><annotation-xml encoding="' + encoding + '">'
                '<p hidden="">Note</p></annotation-xml><mi>x</mi></math>'
                '<p>After.</p>')
            for media_type in ('text/html', 'application/xhtml+xml'):
                with self.subTest(encoding=encoding, media_type=media_type):
                    raw = make_epub(body, media_type=media_type, toc_links=[])
                    self.assertEqual('Before.x\n\nAfter.',
                                     extract_epub_text('synthetic.epub', archive_bytes=raw))
            # The XML paths agree (load_epub reads XHTML items only).
            raw_x = make_epub(body, media_type='application/xhtml+xml', toc_links=[])
            with tempfile.TemporaryDirectory() as tmp:
                epub_path = Path(tmp) / 'annotation.epub'
                epub_path.write_bytes(raw_x)
                loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
            self.assertEqual('Before. x After.', loaded)
        # Non-HTML encodings are not integration points. Known pinned
        # divergence: the html.parser readers model the two integration
        # encodings but not the generic "start tag that breaks out of foreign
        # content" rule, so this hidden <p> is narrated rather than omitted.
        body = ('<p>Before.</p><math><annotation-xml encoding="application/xml">'
                '<p hidden="">Note</p></annotation-xml><mi>x</mi></math>'
                '<p>After.</p>')
        raw = make_epub(body, media_type='text/html', toc_links=[])
        self.assertIn('Note',
                      extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_hidden_void_elements_are_not_counted_as_omitted_elements(self):
        body = (
            '<p>Visible.</p>'
            '<p hidden=""><br hidden><img hidden src="x.png">Secret.</p>'
            '<hr hidden><br hidden>')
        raw = make_epub(body, media_type='text/html', toc_links=[])
        text, report = extract_epub_text('synthetic.epub', archive_bytes=raw,
                                         include_report=True)
        self.assertEqual('Visible.', text)
        rows = [row for row in report['rows'] if row['kind'] == 'hidden_content']
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]['omitted_elements'])
        # Only hidden void elements: nothing omits text, so no hidden row.
        raw_voids = make_epub('<p>Visible.</p><br hidden><img hidden src="x.png">',
                              media_type='text/html', toc_links=[])
        _text, report_voids = extract_epub_text(
            'synthetic.epub', archive_bytes=raw_voids, include_report=True)
        self.assertEqual([], [row for row in report_voids['rows']
                              if row['kind'] == 'hidden_content'])
        # Same rule on the strict XML path.
        raw_xml = make_epub(
            '<p>Visible.</p><br hidden=""/><img hidden="" src="x.png"/>'
            '<div hidden=""><br hidden=""/></div>', toc_links=[])
        _text_x, report_x = extract_epub_text(
            'synthetic.epub', archive_bytes=raw_xml, include_report=True)
        rows_x = [row for row in report_x['rows'] if row['kind'] == 'hidden_content']
        self.assertEqual(1, len(rows_x))
        self.assertEqual(1, rows_x[0]['omitted_elements'])

    def test_svg_and_mathml_hidden_attributes_are_not_dropped_across_all_paths(self):
        cases = [
            ('svg_text_hidden',
             '<p>Before.</p><svg xmlns="http://www.w3.org/2000/svg">'
             '<rect hidden="" width="5" height="5"/><text hidden="">SVG text</text></svg>'
             '<p>After.</p>',
             'SVG text'),
            ('mathml_mi_hidden',
             '<p>Before.</p><math xmlns="http://www.w3.org/1998/Math/MathML">'
             '<mi hidden="">alpha</mi><mn>2</mn></math><p>After.</p>',
             'alpha'),
            ('mathml_root_hidden',
             '<p>Before.</p><math xmlns="http://www.w3.org/1998/Math/MathML" hidden="">'
             '<mi>beta</mi></math><p>After.</p>',
             'beta'),
        ]
        for name, body, expected_token in cases:
            with self.subTest(case=name):
                for media_type in ('application/xhtml+xml', 'text/html'):
                    raw = make_epub(body, media_type=media_type, toc_links=[])
                    text, report = extract_epub_text('synthetic.epub', archive_bytes=raw, include_report=True)
                    self.assertIn(expected_token, text)
                    self.assertEqual([], [r for r in report['rows'] if r['kind'] == 'hidden_content'])
                raw_x = make_epub(body, toc_links=[])
                _root, segs, full = get_xhtml_segments(raw_x, 'OEBPS/ch1.xhtml')
                self.assertIn(expected_token, full)
                with tempfile.TemporaryDirectory() as tmp:
                    epub_path = Path(tmp) / 'foreign.epub'
                    epub_path.write_bytes(raw_x)
                    loaded = alignment.load_epub(str(epub_path))
                self.assertIn(expected_token, loaded)

        # Bare HTML5 <svg>/<math> without xmlns on text/html path.
        bare_html = (
            '<p>Before.</p><svg><text hidden="">Bare SVG</text></svg>'
            '<math><mi hidden="">Bare Math</mi></math><p>After.</p>'
        )
        raw_bare = make_epub(bare_html, media_type='text/html', toc_links=[])
        bare_text = extract_epub_text('synthetic.epub', archive_bytes=raw_bare)
        self.assertIn('Bare SVG', bare_text)
        self.assertIn('Bare Math', bare_text)

        # Qwen sentence-splitting inline events also keep SVG/MathML text.
        p_svg = ET.fromstring(
            '<p xmlns="http://www.w3.org/1999/xhtml">Before '
            '<svg xmlns="http://www.w3.org/2000/svg"><text hidden="">SVG</text></svg>'
            ' after.</p>'
        )
        inline_text = ''.join(val for kind, val in _inline_events(p_svg) if kind == 'text')
        self.assertIn('SVG', inline_text)

        # An HTML ancestor with hidden still omits embedded SVG/MathML, and an
        # HTML element inside SVG <foreignObject> still obeys HTML hidden.
        html_wrapped = (
            '<p>Before.</p>'
            '<div hidden=""><svg xmlns="http://www.w3.org/2000/svg"><text>Omitted SVG</text></svg></div>'
            '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject>'
            '<div xmlns="http://www.w3.org/1999/xhtml" hidden=""><p>Omitted foreign HTML</p></div>'
            '</foreignObject></svg>'
            '<p>After.</p>'
        )
        for media_type in ('application/xhtml+xml', 'text/html'):
            raw = make_epub(html_wrapped, media_type=media_type, toc_links=[])
            text = extract_epub_text('synthetic.epub', archive_bytes=raw)
            self.assertEqual('Before.\n\nAfter.', text)
        raw_x = make_epub(html_wrapped, toc_links=[])
        _root, segs, _full = get_xhtml_segments(raw_x, 'OEBPS/ch1.xhtml')
        self.assertEqual(['Before.', 'After.'], [s['text'] for s in segs])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'wrapped.epub'
            epub_path.write_bytes(raw_x)
            loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
        self.assertEqual('Before. After.', loaded)

    def test_prefixed_xhtml_hidden_applies_to_xml_paths_and_not_text_html(self):
        body = '<p h:hidden="hidden">Prefixed secret.</p><p>Visible.</p>'
        extra = ' xmlns:h="http://www.w3.org/1999/xhtml"'
        raw_x = make_epub(body, extra_html_attrs=extra, toc_links=[])
        raw_h = make_epub(body, media_type='text/html', extra_html_attrs=extra, toc_links=[])

        self.assertEqual('Visible.', extract_epub_text('synthetic.epub', archive_bytes=raw_x))
        self.assertEqual('Prefixed secret.\n\nVisible.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_h))
        _root, segs, _full = get_xhtml_segments(raw_x, 'OEBPS/ch1.xhtml')
        self.assertEqual(['Visible.'], [s['text'] for s in segs])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'prefixed.epub'
            epub_path.write_bytes(raw_x)
            loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
        self.assertEqual('Visible.', loaded)

    def test_hidden_on_html_and_body_across_all_paths_and_all_hidden_epub_consistency(self):
        doc_tpl = '<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml"{extra}>{inner}</html>'
        all_hidden_shapes = [
            ('html_hidden', doc_tpl.format(extra=' hidden="hidden"', inner='<head><title>T</title></head><body><p>Secret.</p></body>'), True),
            ('body_hidden', doc_tpl.format(extra='', inner='<head><title>T</title></head><body hidden=""><p>Secret.</p></body>'), True),
            ('body_h_hidden', doc_tpl.format(extra=' xmlns:h="http://www.w3.org/1999/xhtml"', inner='<head><title>T</title></head><body h:hidden=""><p>Secret.</p></body>'), False),
            ('only_p_hidden', doc_tpl.format(extra='', inner='<head><title>T</title></head><body><p hidden="">Secret.</p></body>'), True),
        ]
        for name, raw_doc, also_html in all_hidden_shapes:
            with self.subTest(shape=name):
                raw_x = make_epub('', raw_document=raw_doc, toc_links=[])
                self.assertEqual('', extract_epub_text('synthetic.epub', archive_bytes=raw_x))
                if also_html:
                    raw_h = make_epub('', raw_document=raw_doc, media_type='text/html', toc_links=[])
                    self.assertEqual('', extract_epub_text('synthetic.epub', archive_bytes=raw_h))
                _root, segs, full = get_xhtml_segments(raw_x, 'OEBPS/ch1.xhtml')
                self.assertEqual([], segs)
                self.assertEqual('', full)
                with tempfile.TemporaryDirectory() as tmp:
                    epub_path = Path(tmp) / f'{name}.epub'
                    epub_path.write_bytes(raw_x)
                    with self.assertRaisesRegex(ValueError, r'no readable text after omitting HTML hidden elements'):
                        alignment.load_epub(str(epub_path))

        # Nav-only EPUB (no hidden elements) keeps the original spine-documents message.
        nav_only = make_multi_epub([])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'nav_only.epub'
            epub_path.write_bytes(nav_only)
            with self.assertRaisesRegex(ValueError, r'no readable spine documents'):
                alignment.load_epub(str(epub_path))

        # Multi-document EPUB: hidden <html> or <body> chapter is skipped while
        # subsequent visible chapters survive in all paths (kills load_epub doc_hidden mutant).
        visible_doc = doc_tpl.format(extra='', inner='<head><title>T</title></head><body><p>Chapter two visible.</p></body>')
        for name, hidden_doc in (
            ('multi_html_hidden', doc_tpl.format(extra=' hidden="hidden"', inner='<head><title>T</title></head><body><p>Chapter one secret.</p></body>')),
            ('multi_body_hidden', doc_tpl.format(extra='', inner='<head><title>T</title></head><body hidden=""><p>Chapter one secret.</p></body>')),
        ):
            with self.subTest(case=name):
                raw_multi = make_multi_epub([('ch1.xhtml', hidden_doc), ('ch2.xhtml', visible_doc)])
                self.assertEqual('Chapter two visible.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw_multi))
                _r1, segs1, _f1 = get_xhtml_segments(raw_multi, 'OEBPS/ch1.xhtml')
                _r2, segs2, _f2 = get_xhtml_segments(raw_multi, 'OEBPS/ch2.xhtml')
                self.assertEqual([], segs1)
                self.assertEqual(['Chapter two visible.'], [s['text'] for s in segs2])
                with tempfile.TemporaryDirectory() as tmp:
                    epub_path = Path(tmp) / f'{name}.epub'
                    epub_path.write_bytes(raw_multi)
                    loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
                self.assertEqual('Chapter two visible.', loaded)

        # Direct _epub_item_root_context checks: detects hidden on <html>/<body>,
        # ignores fake <body hidden> inside <style>/comments, and stops at <body>.
        class _FakeItem:
            def __init__(self, content):
                self.content = content

        self.assertTrue(alignment._epub_item_root_context(
            _FakeItem(b'<html hidden="hidden"><body><p>S</p></body></html>'))[0])
        self.assertTrue(alignment._epub_item_root_context(
            _FakeItem(b'<html><body hidden=""><p>S</p></body></html>'))[0])
        self.assertTrue(alignment._epub_item_root_context(
            _FakeItem(b'<html xmlns:h="http://www.w3.org/1999/xhtml"><body h:hidden=""><p>S</p></body></html>'))[0])
        hidden_flag, ns = alignment._epub_item_root_context(_FakeItem(
            b'<html xmlns:h="http://www.w3.org/1999/xhtml"><head>'
            b'<style>/* <body hidden> */</style><!-- <body hidden> --></head>'
            b'<body><p hidden="">S</p></body></html>'))
        self.assertFalse(hidden_flag)
        self.assertEqual('http://www.w3.org/1999/xhtml', ns.get('h'))


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

    def test_block_with_inline_text_and_hidden_block_descendant_keeps_visible_segment(self):
        # A block containing a container child plus a hidden block descendant
        # (and no direct text on the block itself) must keep the block itself as
        # the segment (preserving its id) rather than treating the hidden block
        # as a live block descendant and descending into the inner container.
        body = '<ul><li id="li1"><div>Visible item</div><p hidden="">Hidden note</p></li></ul>'
        raw = make_epub(body, toc_links=[])
        _root, segs, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
        self.assertEqual(['Visible item'], [s['text'] for s in segs])
        self.assertEqual('li1', segs[0]['fragment'])

    def test_get_optional_titles_handles_deep_nesting_without_recursion_error(self):
        xhtml = '{http://www.w3.org/1999/xhtml}'
        root = ET.Element(f'{xhtml}html')
        body = ET.SubElement(root, f'{xhtml}body')
        cur = body
        for _ in range(1500):
            cur = ET.SubElement(cur, f'{xhtml}div')
        vis = ET.SubElement(cur, f'{xhtml}p', {'id': 'vis'})
        vis.text = 'Deep visible.'
        hid = ET.SubElement(cur, f'{xhtml}p', {'id': 'hid', 'hidden': ''})
        hid.text = 'Deep hidden.'
        titles = _get_optional_titles(
            root,
            [{'element': vis, 'fragment': 'vis', 'text': 'Deep visible.'}],
            [
                {'path': 'ch1.xhtml', 'fragment': 'vis', 'label': 'Visible Title'},
                {'path': 'ch1.xhtml', 'fragment': 'hid', 'label': 'Hidden Title'},
            ],
            'ch1.xhtml',
        )
        self.assertEqual({0: ['Visible Title']}, titles)


if __name__ == '__main__':
    unittest.main()
