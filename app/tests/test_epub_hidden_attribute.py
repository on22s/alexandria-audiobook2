"""EPUB elements marked with the HTML ``hidden`` attribute are not narrated (#2326).

Synthetic EPUB/XHTML fixtures only. Hidden handling applies only where the
browser itself parses the document as XML: a well-formed
``application/xhtml+xml`` resource. Every other HTML media type - and
malformed XHTML - keeps ``origin/main``'s output byte for byte, so hidden
content there is narrated (a documented leak) and never dropped. Covers the
shared predicates (``alexandria_alignment.is_html_hidden_attrs`` and
``html_hidden_handling_applies``), ``_omit_xml_ignored_subtrees``,
``extract_epub_text`` (with extraction report and navigation targets),
``epub_readaloud`` (chunk-timed and Qwen modes), and
``alexandria_alignment.load_epub``.
"""
import contextlib
import io
import json
import sys
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
    _local,
    build_epub_readaloud,
    get_spoken_text,
    get_xhtml_segments,
    validate_epub_readaloud,
)
from routers import script
from routers.script import (
    _HTMLTextExtractor,
    _HIDDEN_MARKUP_HINT,
    _get_anchor_id,
    _omit_xml_ignored_subtrees,
    extract_epub_text,
)
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


XHTML_NS = 'http://www.w3.org/1999/xhtml'


def _load_bytes(raw):
    with tempfile.TemporaryDirectory() as tmp:
        epub_path = Path(tmp) / 'probe.epub'
        epub_path.write_bytes(raw)
        return alignment.load_epub(str(epub_path))


def _normalised(text):
    return ' '.join(text.split())


def _three_paths(raw):
    """Return (extraction text, read-aloud segment texts, load text)."""
    extracted = extract_epub_text('synthetic.epub', archive_bytes=raw)
    _root, segments, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
    return extracted, [segment['text'] for segment in segments], _load_bytes(raw)


class HtmlHiddenPredicateTests(unittest.TestCase):
    def test_predicate_accepts_only_the_lowercase_null_namespace_attribute(self):
        # Attribute *value* never matters; only presence of the exact
        # null-namespace name `hidden` does.
        for value in ('', 'hidden', 'HIDDEN', 'false', 'until-found',
                      'UNTIL-FOUND', 'x', None):
            attrs = [('hidden', value)]
            with self.subTest(value=value, attrs=attrs):
                self.assertTrue(alignment.is_html_hidden_attrs(
                    attrs, element_namespace=XHTML_NS))
            with self.subTest(value=value, attrs={'hidden': value}):
                self.assertTrue(alignment.is_html_hidden_attrs(
                    {'hidden': value}, element_namespace=XHTML_NS))

        # XML attribute names are case-sensitive: these are ordinary
        # attributes a browser renders (gate rows a and b).
        for attrs in ({'HIDDEN': ''}, {'Hidden': ''}, {'HiDdEn': 'HIDDEN'},
                      {'hidden2': ''}, {'data-hidden': ''},
                      {'aria-hidden': 'true'}, {'style': 'display:none'}):
            with self.subTest(attrs=attrs):
                self.assertFalse(alignment.is_html_hidden_attrs(
                    attrs, element_namespace=XHTML_NS))

        # A namespaced attribute is not the null-namespace HTML attribute,
        # even when its prefix is bound to the XHTML URI (gate row c).
        for attrs in ({'{http://www.w3.org/1999/xhtml}hidden': ''},
                      {'h:hidden': 'hidden'},
                      {'x:hidden': 'hidden'},
                      {'{urn:example}hidden': 'hidden'}):
            with self.subTest(attrs=attrs):
                self.assertFalse(alignment.is_html_hidden_attrs(
                    attrs, element_namespace=XHTML_NS))

    def test_predicate_requires_the_xhtml_element_namespace(self):
        for ns in (None, '', 'urn:example',
                   'http://www.w3.org/2000/svg',
                   'http://www.w3.org/1998/Math/MathML'):
            with self.subTest(element_namespace=ns):
                self.assertFalse(alignment.is_html_hidden_attrs(
                    {'hidden': ''}, element_namespace=ns))
        self.assertTrue(alignment.is_html_hidden_attrs(
            {'hidden': ''}, element_namespace=XHTML_NS))

    def test_xml_element_predicate_resolves_prefixes_and_default_namespace(self):
        # Unprefixed element in the XHTML default namespace: hidden applies.
        self.assertTrue(alignment.is_hidden_xml_element(
            'p', {'hidden': ''}, {'': XHTML_NS}))
        # Case-sensitive attribute name and prefixed attribute: narrated.
        self.assertFalse(alignment.is_hidden_xml_element(
            'p', {'HIDDEN': ''}, {'': XHTML_NS}))
        self.assertFalse(alignment.is_hidden_xml_element(
            'p', {'h:hidden': ''}, {'': XHTML_NS, 'h': XHTML_NS}))
        # Null namespace element (xmlns="" or no xmlns): narrated.
        self.assertFalse(alignment.is_hidden_xml_element(
            'p', {'hidden': ''}, {'': ''}))
        self.assertFalse(alignment.is_hidden_xml_element(
            'p', {'hidden': ''}, {}))
        # A prefixed element bound to XHTML is an HTML element; its
        # unprefixed attribute is the HTML one.
        self.assertTrue(alignment.is_hidden_xml_element(
            'h:p', {'hidden': ''}, {'h': XHTML_NS}))
        self.assertFalse(alignment.is_hidden_xml_element(
            'h:p', {'h:hidden': ''}, {'h': XHTML_NS}))


class HtmlHiddenExtractionTests(unittest.TestCase):
    def test_issue_reproduction_all_extraction_paths(self):
        # Direct _HTMLTextExtractor (no hidden handling of its own).
        ext = _HTMLTextExtractor()
        ext.feed(ISSUE_SNIPPET)
        self.assertEqual('Seen first.\n\nNever shown.\n\nNever shown either.\n\nSeen last.',
                         script.normalize_extracted_text(ext.get_text()))

        # Full XHTML EPUB extraction
        raw = make_epub(ISSUE_SNIPPET, toc_links=[])
        self.assertEqual('Seen first.\n\nSeen last.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

        # Full text/html EPUB extraction: every non-XHTML media type keeps
        # origin/main's tolerant output byte for byte (hidden narrated).
        raw_html = make_epub(ISSUE_SNIPPET, media_type='text/html', toc_links=[])
        self.assertEqual('Seen first.\n\nNever shown.\n\nNever shown either.\n\nSeen last.',
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

    def test_well_formed_xhtml_hidden_is_omitted(self):
        doc_cases = [
            ('empty', '<p>Start.</p><p hidden="">Secret.</p><p>End.</p>'),
            ('canonical', '<p>Start.</p><p hidden="hidden">Secret.</p><p>End.</p>'),
            ('until_found', '<p>Start.</p><p hidden="until-found">Secret.</p><p>End.</p>'),
            ('until_found_upper', '<p>Start.</p><p hidden="UNTIL-FOUND">Secret.</p><p>End.</p>'),
            ('false_still_hidden', '<p>Start.</p><p hidden="false">Secret.</p><p>End.</p>'),
        ]
        for name, body in doc_cases:
            with self.subTest(case=name):
                raw = make_epub(body, toc_links=[])
                extracted, segments, loaded = _three_paths(raw)
                self.assertEqual('Start.\n\nEnd.', extracted)
                self.assertEqual(['Start.', 'End.'], segments)
                self.assertEqual('Start. End.', _normalised(loaded))

        # Nested hidden elements and hidden descendants.
        nested = ('<p>Before.</p><div hidden=""><p hidden="">Double hidden</p>'
                  '<p>Still inside outer hidden.</p></div><p>After.</p>')
        raw = make_epub(nested, toc_links=[])
        extracted, segments, loaded = _three_paths(raw)
        self.assertEqual('Before.\n\nAfter.', extracted)
        self.assertEqual(['Before.', 'After.'], segments)
        self.assertEqual('Before. After.', _normalised(loaded))

    def test_gate_row_a_uppercase_attribute_is_narrated(self):
        # Chrome and Firefox render `<p HIDDEN="">` (XML attribute names are
        # case-sensitive), so every path must keep the token.
        raw = make_epub('<p>Before.</p><p HIDDEN="">UPPERATTR</p><p>After.</p>',
                        toc_links=[])
        extracted, segments, loaded = _three_paths(raw)
        self.assertEqual('Before.\n\nUPPERATTR\n\nAfter.', extracted)
        self.assertEqual(['Before.', 'UPPERATTR', 'After.'], segments)
        self.assertEqual('Before. UPPERATTR After.', _normalised(loaded))

    def test_gate_row_b_mixed_case_attribute_is_narrated(self):
        for spelling in ('Hidden', 'HiDdEn'):
            with self.subTest(spelling=spelling):
                raw = make_epub(
                    f'<p>Before.</p><p {spelling}="">MIXEDATTR</p><p>After.</p>',
                    toc_links=[])
                extracted, segments, loaded = _three_paths(raw)
                self.assertEqual('Before.\n\nMIXEDATTR\n\nAfter.', extracted)
                self.assertEqual(['Before.', 'MIXEDATTR', 'After.'], segments)
                self.assertEqual('Before. MIXEDATTR After.', _normalised(loaded))

    def test_gate_row_c_prefixed_hidden_attribute_is_narrated(self):
        # A namespaced attribute is not the null-namespace HTML attribute,
        # whether the prefix is declared on <html> or on the element.
        for name, body, extra in (
            ('prefix_on_html', '<p h:hidden="">PREFIXATTR</p>',
             ' xmlns:h="http://www.w3.org/1999/xhtml"'),
            ('prefix_on_element',
             '<p xmlns:h="http://www.w3.org/1999/xhtml" h:hidden="">PREFIXATTR</p>',
             ''),
        ):
            with self.subTest(case=name):
                raw = make_epub(f'<p>Before.</p>{body}<p>After.</p>',
                                extra_html_attrs=extra, toc_links=[])
                extracted, segments, loaded = _three_paths(raw)
                self.assertEqual('Before.\n\nPREFIXATTR\n\nAfter.', extracted)
                self.assertEqual(['Before.', 'PREFIXATTR', 'After.'], segments)
                self.assertEqual('Before. PREFIXATTR After.', _normalised(loaded))

    def test_gate_row_d_null_namespace_elements_are_narrated(self):
        # `xmlns=""` puts the element (and subtree) in no namespace, so the
        # HTML UA rule scoped to XHTML never applies.
        cases = [
            ('null_ns_div', '<div xmlns=""><p hidden="">NULLNS</p></div>'),
            ('null_ns_p', '<p xmlns="" hidden="">NULLNSP</p>'),
        ]
        for name, body in cases:
            token = 'NULLNS' if name == 'null_ns_div' else 'NULLNSP'
            with self.subTest(case=name):
                raw = make_epub(f'<p>Before.</p>{body}<p>After.</p>', toc_links=[])
                extracted, segments, loaded = _three_paths(raw)
                self.assertEqual(f'Before.\n\n{token}\n\nAfter.', extracted)
                self.assertEqual(['Before.', token, 'After.'], segments)
                self.assertEqual(f'Before. {token} After.', _normalised(loaded))

    def test_gate_row_e_document_without_xmlns_is_narrated(self):
        # No xmlns at all: every element is in the null namespace, so a
        # lowercase `hidden` is not the HTML attribute. Extraction and
        # load_epub narrate it; read-aloud refuses the document as not XHTML
        # on both trees (pre-existing behaviour, not hidden handling).
        raw = make_epub(
            '',
            raw_document=(
                '<html><head><title>t</title></head><body>'
                '<p>Before.</p><p hidden="">NONSROOT</p><p>After.</p>'
                '</body></html>'),
            toc_links=[])
        self.assertEqual('Before.\n\nNONSROOT\n\nAfter.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        self.assertEqual('Before. NONSROOT After.', _normalised(_load_bytes(raw)))
        with self.assertRaisesRegex(ValueError, r'not in the XHTML namespace'):
            get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')

    def test_browser_controls_still_apply(self):
        # Lowercase hidden omits; an uppercase *element* name is still an
        # XHTML element, so `hidden` applies; values are irrelevant; an
        # explicitly SVG-namespaced text element is not an HTML element.
        omitted = [
            ('lowercase_p', '<p hidden="">SECRET</p>'),
            ('uppercase_element', '<P hidden="">SECRET</P>'),
            ('hidden_value_upper', '<p hidden="HIDDEN">SECRET</p>'),
            ('hidden_false', '<p hidden="false">SECRET</p>'),
        ]
        for name, body in omitted:
            with self.subTest(case=name):
                raw = make_epub(f'<p>Before.</p>{body}<p>After.</p>', toc_links=[])
                extracted, segments, loaded = _three_paths(raw)
                self.assertNotIn('SECRET', extracted)
                self.assertNotIn('SECRET', segments)
                self.assertNotIn('SECRET', loaded)
        svg = ('<p>Before.</p><svg xmlns="http://www.w3.org/2000/svg">'
               '<text hidden="">SVGTEXT</text></svg><p>After.</p>')
        raw = make_epub(svg, toc_links=[])
        extracted, _segments, loaded = _three_paths(raw)
        self.assertIn('SVGTEXT', extracted)
        self.assertIn('SVGTEXT', loaded)

    def test_bare_boolean_hidden_in_text_html_is_narrated_documented_leak(self):
        # A bare HTML boolean attribute is not well-formed XML, so the document
        # gets no hidden handling: the XML parse is not attempted and the
        # tolerant extractor narrates the text. This is the documented leak.
        for body in (
            '<p>Start.</p><p hidden>Secret.</p><p>End.</p>',
            '<p>Start.</p><p HIDDEN>Secret.</p><p>End.</p>',
        ):
            with self.subTest(body=body):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(
                    'Start.\n\nSecret.\n\nEnd.',
                    extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_malformed_text_html_has_no_hidden_handling_and_narrates_hidden(self):
        # Every document below is well-formed HTML soup but not well-formed
        # XML. Output must be the legacy tolerant output: hidden text is
        # narrated (a documented leak), never dropped, and no hidden_content
        # report row is emitted.
        cases = [
            ('unclosed_hidden_div',
             '<p>Before.</p><div hidden=""><p>Unclosed child</div><p>After.</p>',
             'Before.\n\nUnclosed child\n\nAfter.'),
            ('implicit_p_close',
             '<p>Before.</p><p hidden="">Hidden without close<p>After.</p>',
             'Before.\n\nHidden without close\n\nAfter.'),
            ('stray_close',
             '<p>Before.</p><div hidden=""><p>Secret</span>Still secret</p></div><p>After.</p>',
             'Before.\n\nSecretStill secret\n\nAfter.'),
            ('unclosed_hidden_at_eof',
             '<p>Before.</p><div hidden=""><p>Hidden until EOF',
             'Before.\n\nHidden until EOF'),
            ('unclosed_void',
             '<p>Start.</p><p hidden="">Secret.</p><br><p>End.</p>',
             'Start.\n\nSecret.\n\nEnd.'),
            ('html_entity',
             '<p>Start.</p><p hidden="">A&nbsp;B.</p><p>End.</p>',
             'Start.\n\nA B.\n\nEnd.'),
            ('malformed_script',
             '<p>Start.</p><script>if (a < b) {}</script><p hidden="">Secret.</p><p>End.</p>',
             'Start.\n\nSecret.\n\nEnd.'),
        ]
        for name, body, expected in cases:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
                text, report = extract_epub_text(
                    'synthetic.epub', archive_bytes=raw, include_report=True)
                self.assertEqual(expected, text)
                self.assertEqual([], [r for r in report['rows']
                                      if r['kind'] == 'hidden_content'])

        # Hidden-free malformed documents are unchanged too.
        raw = make_epub('<p>One.<p>Two <b>bold',
                        media_type='text/html', toc_links=[])
        self.assertEqual('One.\n\nTwo bold',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_text_html_rev6_rows_are_byte_identical_to_origin_main(self):
        # Every row is well-formed XML *and* text/html. Review 6 showed the
        # r5 XML-first route omitted text a browser renders outside the hidden
        # region; the final scope removes that route, so each row must match
        # origin/main's tolerant output byte for byte (hidden narrated, never
        # dropped). Expected strings were captured from origin/main.
        rows = [
            ('hidden_p_div',
             '<p>PRE.</p><p hidden="hidden">SEC<div>VIS</div></p><p>POST.</p>',
             'PRE.\n\nSEC\n\nVIS\n\nPOST.'),
            ('hidden_p_table',
             '<p>PRE.</p><p hidden="hidden">SEC<table><tr><td>VIS</td></tr></table></p><p>POST.</p>',
             'PRE.\n\nSEC\n\nVIS\n\nPOST.'),
            ('hidden_p_ul',
             '<p>PRE.</p><p hidden="hidden">SEC<ul><li>VIS</li></ul></p><p>POST.</p>',
             'PRE.\n\nSEC\n\nVIS\n\nPOST.'),
            ('hidden_p_h2',
             '<p>PRE.</p><p hidden="hidden">SEC<h2>VIS</h2></p><p>POST.</p>',
             'PRE.\n\nSEC\n\nVIS\n\nPOST.'),
            ('hidden_p_blockquote',
             '<p>PRE.</p><p hidden="hidden">SEC<blockquote>VIS</blockquote></p><p>POST.</p>',
             'PRE.\n\nSEC\n\nVIS\n\nPOST.'),
            ('hidden_p_p',
             '<p>PRE.</p><p hidden="hidden">SEC<p>VIS</p></p><p>POST.</p>',
             'PRE.\n\nSEC\n\nVIS\n\nPOST.'),
            ('hidden_td_caption',
             '<p>PRE.</p><table><tr><td hidden="hidden">SEC<caption>VIS</caption></td></tr></table><p>POST.</p>',
             'PRE.\n\nSECVIS\n\nPOST.'),
            ('hidden_th_caption',
             '<p>PRE.</p><table><tr><th hidden="hidden">SEC<caption>VIS</caption></th></tr></table><p>POST.</p>',
             'PRE.\n\nSECVIS\n\nPOST.'),
            ('hidden_td_colgroup',
             '<p>PRE.</p><table><tr><td hidden="hidden">SEC<colgroup><col/></colgroup>VIS</td></tr></table><p>POST.</p>',
             'PRE.\n\nSECVIS\n\nPOST.'),
            ('foster_p_from_table',
             '<p>PRE.</p><table hidden="hidden"><tr><td>SEC</td></tr><p>VIS</p></table><p>POST.</p>',
             'PRE.\n\nSEC\n\nVIS\n\nPOST.'),
            ('foster_div_from_table',
             '<p>PRE.</p><table hidden="hidden"><div>VIS</div><tr><td>SEC</td></tr></table><p>POST.</p>',
             'PRE.\n\nVIS\n\nSEC\n\nPOST.'),
            ('foster_from_tr',
             '<p>PRE.</p><table><tr hidden="hidden">SEC<p>VIS</p><td>X1</td></tr></table><p>POST.</p>',
             'PRE.\n\nSEC\n\nVIS\nX1\n\nPOST.'),
            ('nested_a',
             '<p>PRE.</p><a hidden="hidden">SEC<a>VIS</a></a><p>POST.</p>',
             'PRE.SECVIS\n\nPOST.'),
            ('nested_button',
             '<p>PRE.</p><button hidden="hidden">SEC<button>VIS</button></button><p>POST.</p>',
             'PRE.SECVIS\n\nPOST.'),
            ('nested_nobr',
             '<p>PRE.</p><nobr hidden="hidden">SEC<nobr>VIS</nobr></nobr><p>POST.</p>',
             'PRE.SECVIS\n\nPOST.'),
            ('nested_select',
             '<p>PRE.</p><select hidden="hidden"><option>SEC</option><select><option>VIS</option></select></select><p>POST.</p>',
             'PRE.SECVIS\n\nPOST.'),
            ('h1_h2',
             '<p>PRE.</p><h1 hidden="hidden">SEC<h2>VIS</h2></h1><p>POST.</p>',
             'PRE.\n\nSEC\n\nVIS\n\nPOST.'),
            ('select_input_p',
             '<p>PRE.</p><select hidden="hidden"><option>SEC</option><input value="i"/><p>VIS</p></select><p>POST.</p>',
             'PRE.SEC\n\nVIS\n\nPOST.'),
            ('bare_svg',
             '<p>PRE.</p><svg><text hidden="hidden">SEC</text></svg><p>VIS</p>',
             'PRE.SEC\n\nVIS'),
            ('bare_math',
             '<p>PRE.</p><math><mi hidden="hidden">SEC</mi></math><p>VIS</p>',
             'PRE.SEC\n\nVIS'),
        ]
        self.assertEqual(20, len(rows))
        for name, body, expected in rows:
            with self.subTest(case=name):
                raw = make_epub(body, media_type='text/html', toc_links=[])
                text, report = extract_epub_text(
                    'synthetic.epub', archive_bytes=raw, include_report=True)
                self.assertEqual(expected, text)
                self.assertEqual([], [row for row in report['rows']
                                      if row['kind'] == 'hidden_content'])

    def test_text_html_hidden_never_gets_hidden_handling(self):
        # Well-formed text/html shapes that r5 omitted through the XML route
        # now keep origin/main's narrated output exactly.
        cases = [
            ('issue', ISSUE_SNIPPET,
             'Seen first.\n\nNever shown.\n\nNever shown either.\n\nSeen last.'),
            ('prefixed',
             '<p>Start.</p><p h:hidden="hidden">Secret.</p><p>End.</p>',
             'Start.\n\nSecret.\n\nEnd.'),
            ('all_hidden_html', None, 'Secret.'),
            ('all_hidden_body', None, 'Secret.'),
            ('voids', '<p>Visible.</p><br hidden=""/><img hidden="" src="x.png"/>',
             'Visible.'),
        ]
        extra = ' xmlns:h="http://www.w3.org/1999/xhtml"'
        for name, body, expected in cases:
            with self.subTest(case=name):
                if name == 'all_hidden_html':
                    raw_doc = ('<?xml version="1.0" encoding="utf-8"?>\n<html '
                               'xmlns="http://www.w3.org/1999/xhtml" hidden="hidden">'
                               '<head><title>T</title></head><body><p>Secret.</p></body></html>')
                    raw = make_epub('', media_type='text/html', raw_document=raw_doc,
                                    toc_links=[])
                elif name == 'all_hidden_body':
                    raw_doc = ('<?xml version="1.0" encoding="utf-8"?>\n<html '
                               'xmlns="http://www.w3.org/1999/xhtml">'
                               '<head><title>T</title></head><body hidden=""><p>Secret.</p></body></html>')
                    raw = make_epub('', media_type='text/html', raw_document=raw_doc,
                                    toc_links=[])
                else:
                    raw = make_epub(body, media_type='text/html', toc_links=[],
                                    extra_html_attrs=extra if name == 'prefixed' else '')
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_hidden_handling_predicate_is_xhtml_and_well_formed_only(self):
        # The one predicate (Rule 15) both paths call: XHTML media type AND
        # well-formed XML. Parameters/case are normalized as RFC 6838 says.
        for media_type in ('application/xhtml+xml', 'application/xhtml+xml; charset=utf-8',
                           'Application/XHTML+XML'):
            with self.subTest(media_type=media_type):
                self.assertTrue(alignment.html_hidden_handling_applies(
                    media_type, xml_well_formed=True))
                self.assertFalse(alignment.html_hidden_handling_applies(
                    media_type, xml_well_formed=False))
        for media_type in ('text/html', 'TEXT/HTML; charset=utf-8', '',
                           'application/xhtml', 'text/xhtml'):
            with self.subTest(media_type=media_type):
                self.assertFalse(alignment.html_hidden_handling_applies(
                    media_type, xml_well_formed=True))
        self.assertTrue(alignment.is_well_formed_xml(b'<html><body><p>x</p></body></html>'))
        for malformed in (b'<html><body><p hidden>x</p></body></html>',
                          b'<html><body><p>A&nbsp;B</p></body></html>',
                          b'<html><body><div><p>x</body></html>'):
            with self.subTest(malformed=malformed):
                self.assertFalse(alignment.is_well_formed_xml(malformed))
        # An unknown declared encoding must decline, not raise.
        self.assertFalse(alignment.is_well_formed_xml(
            b'<?xml version="1.0" encoding="bogus"?><html/>'))

    def test_well_formed_probe_matches_the_extraction_parser(self):
        # Rule 15: extraction and load_epub must agree on "well-formed". The
        # probe uses the extraction pass's own Expat configuration, so for any
        # document a skip opening makes the pass parse, both accept or both
        # reject (undefined entity included).
        documents = [
            '<html><head><title>T</title></head><body><p>x</p></body></html>',
            '<html><head><title>A&nbsp;B</title></head><body><p>x</p></body></html>',
            '<html><head><title>T</title></head><body><p hidden>x</p></body></html>',
            '<html><head><title>T</title></head><body><div><p>x</body></html>',
            '<html><head><title>T</title></head><body><p hidden="x">a</p></body></html>',
            # A declared external entity is well-formed and is never fetched
            # or expanded; the extraction pass accepts the same bytes.
            ('<?xml version="1.0"?><!DOCTYPE html ['
             '<!ENTITY xxe SYSTEM "file:///nonexistent">]>'
             '<html><head><title>&xxe;</title></head><body><p>x</p></body></html>'),
        ]
        for document in documents:
            with self.subTest(document=document):
                well_formed = alignment.is_well_formed_xml(document)
                if well_formed:
                    self.assertIsInstance(_omit_xml_ignored_subtrees(document), str)
                else:
                    with self.assertRaises(ValueError):
                        _omit_xml_ignored_subtrees(document)

        # Internal entities are reported to the default handler, not expanded:
        # a DTD bomb that ElementTree refuses for amplification is simply
        # well-formed here, and the extraction pass accepts the same bytes.
        # Because both use the same non-expanding configuration, neither path
        # can materialise the expansion (review 7 finding 2).
        entities = ['<!ENTITY lol "lol">']
        for level in range(1, 7):
            entities.append('<!ENTITY lol%d "%s">' % (
                level, ('&lol;' if level == 1 else f'&lol{level - 1};') * 10))
        bomb = ('<?xml version="1.0"?><!DOCTYPE html [%s]>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                '<title>&lol6;</title></head><body><p>x</p></body></html>'
                % ''.join(entities))
        self.assertTrue(alignment.is_well_formed_xml(bomb))
        with self.assertRaises(ET.ParseError):
            ET.fromstring(bomb.encode('utf-8'))
        self.assertIsInstance(_omit_xml_ignored_subtrees(bomb), str)

    def test_undefined_entity_keeps_main_narration_of_hidden_content(self):
        # &nbsp; is undefined in XML, so the probe and the extraction pass both
        # decline hidden handling. load_epub then narrates the hidden text
        # exactly as origin/main did (review 7 finding 2).
        document = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            '<title>A&nbsp;B</title></head>'
            '<body><p hidden="x">Secret.</p><p>Visible.</p></body></html>')
        raw = make_epub('', raw_document=document, toc_links=[], head='')
        with self.assertRaisesRegex(ValueError, r'undefined entity'):
            extract_epub_text('synthetic.epub', archive_bytes=raw)
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'entity.epub'
            epub_path.write_bytes(raw)
            loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
        self.assertIn('Secret.', loaded)
        self.assertIn('Visible.', loaded)

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
        # The documents need the XHTML default namespace for `hidden` to apply.
        NS = ' xmlns="http://www.w3.org/1999/xhtml"'
        self_closing_cases = [
            ('same_tag_self_closing',
             f'<html{NS}><body><div><div hidden=""/></div><p>After.</p></body></html>',
             f'<html{NS}><body><div></div><p>After.</p></body></html>'),
            ('gt_in_attr_self_closing',
             f'<html{NS}><body><div><div title="a>b" hidden=""/></div><p>After.</p></body></html>',
             f'<html{NS}><body><div></div><p>After.</p></body></html>'),
            ('slash_gt_in_text_paired',
             f'<html{NS}><body><div><div hidden="">a/></div></div><p>After.</p></body></html>',
             f'<html{NS}><body><div></div><p>After.</p></body></html>'),
            ('void_self_closing',
             f'<html{NS}><body><p>Before.</p><br hidden=""/><img hidden="hidden" src="x.png"/><p>After.</p></body></html>',
             f'<html{NS}><body><p>Before.</p><p>After.</p></body></html>'),
        ]
        for name, source, expected in self_closing_cases:
            with self.subTest(case=name):
                ET.fromstring(source.encode('utf-8'))
                self.assertEqual(expected, _omit_xml_ignored_subtrees(source))
                raw_case = make_epub('', raw_document=source, toc_links=[])
                self.assertIn('After.', extract_epub_text('synthetic.epub', archive_bytes=raw_case))

    def test_hidden_void_nested_and_malformed_markup_paths(self):
        # Well-formed hidden void elements on the XHTML path.
        html_void = (
            '<p>First.</p>'
            '<br hidden=""/><img hidden="" src="a.png"/><hr hidden=""/>'
            '<input hidden="" type="text"/><p hidden=""/>'
            '<p>Second.</p>'
        )
        raw = make_epub(html_void, toc_links=[])
        self.assertEqual('First.\n\nSecond.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

        # Nested hidden inside hidden and nested same-tag elements inside hidden.
        nested = (
            '<p>Before.</p>'
            '<div hidden="">'
            '<div>Nested div<p hidden="">Double hidden</p><br/></div>'
            '<p>Still inside outer hidden.</p>'
            '</div>'
            '<p>After.</p>'
        )
        raw = make_epub(nested, toc_links=[])
        self.assertEqual('Before.\n\nAfter.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

        # Review 7 finding 1: a malformed XHTML document with no skip-tag
        # opening is narrated exactly as origin/main narrated it, even when a
        # quoted hidden attribute is present. The hidden attribute may engage
        # the strict parse only for a document the shared predicate accepts
        # (well-formed), so this is main's output byte for byte.
        bad_xhtml = (
            '<html><body><p>Before.</p>'
            '<div hidden=""><p>Unclosed hidden secret</body></html>'
        )
        raw_bad = make_epub('', raw_document=bad_xhtml, toc_links=[], head='')
        self.assertEqual('Before.\n\nUnclosed hidden secret',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_bad))
        # Control: the same malformed document with a <title> opening already
        # took the strict parse on origin/main, and still rejects with the
        # same error (no hidden-specific message).
        bad_with_title = (
            '<html><head><title>T</title></head><body><p>Before.</p>'
            '<div hidden=""><p>Unclosed hidden secret</body></html>'
        )
        raw_title = make_epub('', raw_document=bad_with_title, toc_links=[], head='')
        with self.assertRaisesRegex(ValueError, r'invalid XML: mismatched tag') as caught:
            extract_epub_text('synthetic.epub', archive_bytes=raw_title)
        self.assertNotIn('element is open', str(caught.exception))
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
        raw = make_epub(body, toc_links=[])
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

        # Non-XHTML: no hidden handling and no report claim; the secret is
        # narrated byte for byte as origin/main did.
        raw_html = make_epub(body, media_type='text/html', toc_links=[])
        text_html, report_html = extract_epub_text(
            'synthetic.epub', archive_bytes=raw_html, include_report=True)
        self.assertIn(secret, text_html)
        self.assertEqual([], [r for r in report_html['rows']
                              if r['kind'] == 'hidden_content'])

    def test_content_policy_claims_hidden_only_when_omission_applied(self):
        # Review 7 finding 5: the policy line is emitted only when hidden
        # omission applied to an included resource. A hidden-free or
        # non-XHTML book therefore keeps origin/main's exact content_policy.
        origin_main_policy = ('spine_order; style_script_title_not_spoken; '
                              'navigation_document_not_spoken; no_front_back_matter_exclusion')
        hidden_body = '<p>One.</p><p hidden="x">Secret.</p>'

        _text, hidden_free = extract_epub_text(
            'synthetic.epub', archive_bytes=make_epub('<p>One.</p><p>Two.</p>', toc_links=[]),
            include_report=True)
        self.assertEqual(origin_main_policy, hidden_free['content_policy'])
        self.assertEqual([], [r for r in hidden_free['rows'] if r['kind'] == 'hidden_content'])

        _text, text_html = extract_epub_text(
            'synthetic.epub',
            archive_bytes=make_epub(hidden_body, media_type='text/html', toc_links=[]),
            include_report=True)
        self.assertEqual(origin_main_policy, text_html['content_policy'])
        self.assertEqual([], [r for r in text_html['rows'] if r['kind'] == 'hidden_content'])

        _text, hidden_xhtml = extract_epub_text(
            'synthetic.epub', archive_bytes=make_epub(hidden_body, toc_links=[]),
            include_report=True)
        self.assertIn('html_hidden_not_spoken', hidden_xhtml['content_policy'])
        self.assertEqual(1, len([r for r in hidden_xhtml['rows']
                                 if r['kind'] == 'hidden_content']))

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
        raw = make_epub(body, toc_links=toc_links)
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

        # Non-XHTML narrates the same document, so every target resolves into
        # visible text exactly as origin/main resolved it.
        raw_html = make_epub(body, media_type='text/html', toc_links=toc_links)
        text_html, report_html = extract_epub_text(
            'synthetic.epub', archive_bytes=raw_html, include_report=True)
        self.assertIn('Hidden body.', text_html)
        self.assertIn('Hidden duplicate.', text_html)
        by_frag_html = {r['fragment']: r for r in report_html['rows']
                        if r['kind'] == 'navigation_target'}
        for fragment in ('h-self', 'h-desc', 'h-name'):
            self.assertNotEqual('target_in_hidden_subtree',
                                by_frag_html[fragment]['reason'])

    def test_load_epub_decides_hidden_from_the_xml_walk_not_an_html_scan(self):
        # `load_epub` no longer guesses the document root from an HTML parser;
        # the same Expat walk extraction uses decides hidden regions. A
        # `<body hidden>` nested in <template>, <noscript> or <textarea> is
        # not the document body, so the real visible body survives.
        walker_cases = [
            ('template_fake_body_only',
             '<html xmlns="http://www.w3.org/1999/xhtml"><head><template>'
             '<body hidden=""/></template></head><body><p>Visible.</p></body></html>'),
            ('noscript_fake_body',
             '<html xmlns="http://www.w3.org/1999/xhtml"><head><noscript>'
             '<body hidden=""/></noscript></head><body><p>Visible.</p></body></html>'),
            ('textarea_fake_body',
             '<html xmlns="http://www.w3.org/1999/xhtml"><head><textarea>'
             '<body hidden=""/></textarea></head><body><p>Visible.</p></body></html>'),
            ('hidden_real_body',
             '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
             '</head><body hidden=""><p>Secret.</p></body></html>'),
            ('hidden_root',
             '<html xmlns="http://www.w3.org/1999/xhtml" hidden=""><body>'
             '<p>Secret.</p></body></html>'),
        ]
        for name, raw_doc in walker_cases:
            with self.subTest(case=name):
                scan = alignment.strip_hidden_xml(raw_doc)
                self.assertIsNotNone(scan)

        # A fake <body hidden> in head-only content does not hide the chapter.
        for name, raw_doc in (
            ('template_fake_body_only', walker_cases[0][1]),
            ('noscript_fake_body', walker_cases[1][1]),
            ('textarea_fake_body', walker_cases[2][1]),
        ):
            with self.subTest(end_to_end=name):
                raw = make_epub('', raw_document=raw_doc, toc_links=[])
                self.assertIn('Visible.', _load_bytes(raw))

        # A real hidden <body> after a template is still refused.
        hidden_after_template = walker_cases[3][1]
        raw_hidden = make_epub('', raw_document=hidden_after_template, toc_links=[])
        with self.assertRaisesRegex(
                ValueError, r'no readable text after omitting HTML hidden elements'):
            _load_bytes(raw_hidden)

        # A DOCTYPE internal subset can never contain the document body.
        doctype_subset = (
            '<!DOCTYPE html [ <!ENTITY foo "bar"> <body hidden=""> ]>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Visible.</p>'
            '</body></html>')
        raw_subset = make_epub('', raw_document=doctype_subset, toc_links=[])
        self.assertEqual('Visible.', _normalised(_load_bytes(raw_subset)))

    def test_hidden_body_is_a_subtree_and_keeps_visible_siblings(self):
        # Review 8 finding 1: a hidden <body> (or any hidden element) cuts
        # only that subtree. Visible text around it is browser-rendered and
        # origin/main narrated it, so load_epub must keep it instead of
        # blanking the item or failing the book.
        single = (
            '<?xml version="1.0"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '</head><p>OUTBEFORE</p><body hidden=""><p>SEC</p></body></html>')
        raw = make_epub('', raw_document=single, toc_links=[])
        self.assertEqual('OUTBEFORE',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        _root, segs, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
        self.assertEqual([], segs)
        self.assertEqual('OUTBEFORE', _normalised(_load_bytes(raw)))

        # Silent multi-chapter variant: a visible body beside a hidden body in
        # the same document must keep the visible chapter text.
        two_bodies = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '</head><body><p>VISCH1</p></body>'
            '<body hidden=""><p>SECCH1</p></body></html>')
        visible_chapter = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '</head><body><p>VISCH2</p></body></html>')
        raw_multi = make_multi_epub([('ch1.xhtml', two_bodies),
                                     ('ch2.xhtml', visible_chapter)])
        self.assertEqual('VISCH1\n\nVISCH2',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_multi))
        _root, segs1, _full = get_xhtml_segments(raw_multi, 'OEBPS/ch1.xhtml')
        self.assertEqual(['VISCH1'], [segment['text'] for segment in segs1])
        self.assertEqual('VISCH1 VISCH2', _normalised(_load_bytes(raw_multi)))

        # A body whose parent is any <html> element, not only the root, is
        # still just a subtree: the visible sibling after it survives.
        nested = (
            '<foo><html xmlns="http://www.w3.org/1999/xhtml"><body hidden="">'
            '<p>SEC</p></body></html><p>VIS</p></foo>')
        raw_nested = make_epub('', raw_document=nested, toc_links=[])
        self.assertEqual('VIS',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_nested))
        self.assertEqual('VIS', _normalised(_load_bytes(raw_nested)))

        # Every decided refusal still fires when no visible text remains.
        hidden_body_only = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '</head><body hidden=""><p>SEC</p></body></html>')
        hidden_root = (
            '<html xmlns="http://www.w3.org/1999/xhtml" hidden=""><body>'
            '<p>SEC</p></body></html>')
        for name, document in (('hidden_body_only', hidden_body_only),
                               ('hidden_root', hidden_root)):
            with self.subTest(refusal=name):
                with self.assertRaisesRegex(
                        ValueError,
                        r'no readable text after omitting HTML hidden elements'):
                    _load_bytes(make_epub('', raw_document=document, toc_links=[]))
        raw_all_hidden = make_multi_epub(
            [('ch1.xhtml', hidden_body_only), ('ch2.xhtml', hidden_body_only)])
        with self.assertRaisesRegex(
                ValueError, r'no readable text after omitting HTML hidden elements'):
            _load_bytes(raw_all_hidden)

    def test_entirely_hidden_is_decided_from_the_substituted_xml_text(self):
        # Review 9 finding 1: ebooklib returns only body content, so flow
        # content after </body> was invisible to the refusal decision. The
        # shared walker's substituted XML is the source of "visible text
        # remains", so this chapter is not refused even though load_epub's
        # own body-only text is empty (exactly origin/main's load output on
        # the hidden-deleted document).
        document = (
            '<?xml version="1.0"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '</head><body><p hidden="">SEC</p></body><p>VIS</p></html>')
        raw = make_epub('', raw_document=document, toc_links=[])
        self.assertEqual('VIS',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        self.assertEqual('', _normalised(_load_bytes(raw)))

    def test_head_title_is_not_hidden_text_but_a_body_title_is(self):
        # Review 9 finding 5: neither path narrates a head <title>, so a hidden
        # root whose only text is the head title removed no narrated text and
        # is not refused. A body <title> is narrated by origin/main's load, so
        # hidden body-title text still counts as removed narration.
        head_title = (
            '<html xmlns="http://www.w3.org/1999/xhtml" hidden="">'
            '<head><title>T</title></head><body><br/></body></html>')
        scan = alignment.strip_hidden_xml(head_title)
        self.assertIsNotNone(scan)
        self.assertFalse(scan.hidden_text_removed)
        raw_head = make_epub('', raw_document=head_title, toc_links=[])
        self.assertEqual('', _normalised(_load_bytes(raw_head)))
        self.assertEqual('',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_head))

        body_title = (
            '<html xmlns="http://www.w3.org/1999/xhtml" hidden="">'
            '<head></head><body><title>Body title</title></body></html>')
        scan = alignment.strip_hidden_xml(body_title)
        self.assertIsNotNone(scan)
        self.assertTrue(scan.hidden_text_removed)
        with self.assertRaisesRegex(
                ValueError, r'no readable text after omitting HTML hidden elements'):
            _load_bytes(make_epub('', raw_document=body_title, toc_links=[]))

    def test_hidden_ancestor_body_is_not_a_readaloud_segment_root(self):
        # Review 9 finding 3: read-aloud used the first <body> in document
        # order even when a hidden ancestor contained it, narrating hidden
        # text. A body under a hidden ancestor is not a segment root; use the
        # next visible body.
        both_bodies = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '</head><div hidden=""><body><p>TB</p></body></div>'
            '<body><p>VIS</p></body></html>')
        raw = make_epub('', raw_document=both_bodies, toc_links=[])
        _root, segments, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
        self.assertEqual(['VIS'], [segment['text'] for segment in segments])
        self.assertEqual('VIS',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        self.assertEqual('VIS', _normalised(_load_bytes(raw)))

        # A hidden <body> itself is not visible either; the next body wins.
        hidden_body_first = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '</head><body hidden=""><p>TB</p></body>'
            '<body><p>VIS</p></body></html>')
        raw_body = make_epub('', raw_document=hidden_body_first, toc_links=[])
        _root, segments, _full = get_xhtml_segments(raw_body, 'OEBPS/ch1.xhtml')
        self.assertEqual(['VIS'], [segment['text'] for segment in segments])

        # No visible body exists, so nothing is segmented even though a tail
        # outside the hidden ancestor is not reachable through a body root.
        only_hidden_ancestor = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '</head><div hidden=""><body><p>TB</p></body></div>'
            '<p>TAIL</p></html>')
        raw_hidden = make_epub('', raw_document=only_hidden_ancestor, toc_links=[])
        _root, segments, _full = get_xhtml_segments(
            raw_hidden, 'OEBPS/ch1.xhtml')
        self.assertEqual([], segments)

    def test_bare_svg_and_math_without_xmlns_use_the_xhtml_namespace(self):
        # XHTML is XML: a bare <svg>/<math> without xmlns stays in the
        # inherited XHTML namespace, exactly as a browser's XML parser reads
        # it, so hidden applies. text/html gets no hidden handling at all and
        # narrates both tokens byte for byte as origin/main did.
        bodies = [
            ('bare_svg', '<p>PRE.</p><svg><text hidden="">SEC</text></svg><p>VIS</p>'),
            ('bare_math', '<p>PRE.</p><math><mi hidden="">SEC</mi></math><p>VIS</p>'),
        ]
        for name, body in bodies:
            with self.subTest(case=name):
                raw_x = make_epub(body, toc_links=[])
                self.assertEqual(
                    'PRE.\n\nVIS',
                    extract_epub_text('synthetic.epub', archive_bytes=raw_x))
                _root, segs, _full = get_xhtml_segments(raw_x, 'OEBPS/ch1.xhtml')
                self.assertEqual(['PRE.', 'VIS'], [s['text'] for s in segs])
                with tempfile.TemporaryDirectory() as tmp:
                    epub_path = Path(tmp) / f'{name}.epub'
                    epub_path.write_bytes(raw_x)
                    loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
                self.assertEqual('PRE. VIS', loaded)

                raw_html = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual(
                    'PRE.SEC\n\nVIS',
                    extract_epub_text('synthetic.epub', archive_bytes=raw_html))

    def test_annotation_xml_in_inherited_xhtml_namespace_obeys_hidden(self):
        # A bare <math> without xmlns stays in the inherited XHTML namespace
        # in XML, so its annotation-xml child and the hidden <p> are XHTML
        # elements and hidden applies (exactly as a browser's XML parser
        # reads the document). This is namespace inheritance, not an HTML
        # integration point. text/html gets no hidden handling.
        for encoding in ('text/html', 'application/xhtml+xml', 'application/xml'):
            body = (
                '<p>Before.</p><math><annotation-xml encoding="' + encoding + '">'
                '<p hidden="">Note</p></annotation-xml><mi>x</mi></math>'
                '<p>After.</p>')
            with self.subTest(encoding=encoding):
                raw = make_epub(body, toc_links=[])
                self.assertEqual('Before.x\n\nAfter.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
                with tempfile.TemporaryDirectory() as tmp:
                    epub_path = Path(tmp) / 'annotation.epub'
                    epub_path.write_bytes(raw)
                    loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
                self.assertEqual('Before. x After.', loaded)
                raw_html = make_epub(body, media_type='text/html', toc_links=[])
                self.assertEqual('Before.\n\nNotex\n\nAfter.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw_html))

    def test_foreign_content_children_do_not_reenter_html(self):
        # Review 7 finding 3: XML has no HTML integration points. A child of an
        # explicitly namespaced SVG foreignObject / MathML annotation-xml
        # without its own xmlns stays in the foreign namespace, where hidden is
        # not an HTML attribute. Extraction and load_epub therefore agree and
        # narrate it, as origin/main did.
        cases = [
            ('foreignObject', 'FO',
             '<p>Before.</p><svg xmlns="http://www.w3.org/2000/svg"><foreignObject>'
             '<p hidden="x">FO</p></foreignObject></svg><p>After.</p>',
             'Before.\n\nFO\n\nAfter.', 'Before. FO After.'),
            ('annotation_xml', 'AX',
             '<p>Before.</p><math xmlns="http://www.w3.org/1998/Math/MathML">'
             '<annotation-xml encoding="text/html"><p hidden="x">AX</p></annotation-xml>'
             '<mi>m</mi></math><p>After.</p>',
             'Before.\n\nAXm\n\nAfter.', 'Before. AX m After.'),
        ]
        for name, token, body, expected_extract, expected_load in cases:
            with self.subTest(case=name):
                raw = make_epub(body, toc_links=[])
                self.assertEqual(expected_extract,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))
                _root, segments, full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
                self.assertIn(token, full)
                with tempfile.TemporaryDirectory() as tmp:
                    epub_path = Path(tmp) / f'{name}.epub'
                    epub_path.write_bytes(raw)
                    loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
                self.assertEqual(expected_load, loaded)
                # An explicit XHTML child still obeys hidden on both paths.
                explicit = body.replace('<p hidden="x">', '<p xmlns="http://www.w3.org/1999/xhtml" hidden="x">')
                raw_explicit = make_epub(explicit, toc_links=[])
                self.assertNotIn('FO' if name == 'foreignObject' else 'AX',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw_explicit))
                with tempfile.TemporaryDirectory() as tmp:
                    epub_path = Path(tmp) / f'{name}_explicit.epub'
                    epub_path.write_bytes(raw_explicit)
                    loaded_explicit = ' '.join(alignment.load_epub(str(epub_path)).split())
                self.assertNotIn('FO' if name == 'foreignObject' else 'AX', loaded_explicit)

    def test_hidden_void_elements_are_not_counted_as_omitted_elements(self):
        body = (
            '<p>Visible.</p>'
            '<p hidden=""><br hidden=""/><img hidden="" src="x.png"/>Secret.</p>'
            '<hr hidden=""/><br hidden=""/>'
        )
        raw = make_epub(body, toc_links=[])
        text, report = extract_epub_text('synthetic.epub', archive_bytes=raw,
                                         include_report=True)
        self.assertEqual('Visible.', text)
        rows = [row for row in report['rows'] if row['kind'] == 'hidden_content']
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]['omitted_elements'])
        # Only hidden void elements: nothing omits text, so no hidden row.
        raw_voids = make_epub(
            '<p>Visible.</p><br hidden=""/><img hidden="" src="x.png"/>',
            toc_links=[])
        _text, report_voids = extract_epub_text(
            'synthetic.epub', archive_bytes=raw_voids, include_report=True)
        self.assertEqual([], [row for row in report_voids['rows']
                              if row['kind'] == 'hidden_content'])

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
        raw_x = make_epub(html_wrapped, toc_links=[])
        self.assertEqual('Before.\n\nAfter.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_x))
        _root, segs, _full = get_xhtml_segments(raw_x, 'OEBPS/ch1.xhtml')
        self.assertEqual(['Before.', 'After.'], [s['text'] for s in segs])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'wrapped.epub'
            epub_path.write_bytes(raw_x)
            loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
        self.assertEqual('Before. After.', loaded)
        # Non-XHTML: no hidden handling, both tokens narrated as origin/main.
        raw_html = make_epub(html_wrapped, media_type='text/html', toc_links=[])
        text_html = extract_epub_text('synthetic.epub', archive_bytes=raw_html)
        self.assertIn('Omitted SVG', text_html)
        self.assertIn('Omitted foreign HTML', text_html)

    def test_prefixed_xhtml_hidden_is_narrated_on_every_xml_path(self):
        # Review gate 1 finding 1c: `h:hidden` is a namespaced attribute, not
        # the null-namespace HTML attribute, so both browsers show the text.
        body = '<p h:hidden="hidden">Prefixed secret.</p><p>Visible.</p>'
        extra = ' xmlns:h="http://www.w3.org/1999/xhtml"'
        raw = make_epub(body, extra_html_attrs=extra, toc_links=[])
        self.assertEqual('Prefixed secret.\n\nVisible.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))
        _root, segs, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
        self.assertEqual(['Prefixed secret.', 'Visible.'],
                         [s['text'] for s in segs])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'prefixed.epub'
            epub_path.write_bytes(raw)
            loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
        self.assertEqual('Prefixed secret. Visible.', loaded)
        # Non-XHTML narrates it, exactly as origin/main did.
        raw_html = make_epub(body, media_type='text/html', extra_html_attrs=extra,
                             toc_links=[])
        self.assertEqual('Prefixed secret.\n\nVisible.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_html))

    def test_hidden_on_html_and_body_across_all_paths_and_all_hidden_epub_consistency(self):
        doc_tpl = '<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml"{extra}>{inner}</html>'
        all_hidden_shapes = [
            ('html_hidden', doc_tpl.format(extra=' hidden="hidden"', inner='<head><title>T</title></head><body><p>Secret.</p></body>')),
            ('body_hidden', doc_tpl.format(extra='', inner='<head><title>T</title></head><body hidden=""><p>Secret.</p></body>')),
            ('only_p_hidden', doc_tpl.format(extra='', inner='<head><title>T</title></head><body><p hidden="">Secret.</p></body>')),
        ]
        for name, raw_doc in all_hidden_shapes:
            with self.subTest(shape=name):
                raw_x = make_epub('', raw_document=raw_doc, toc_links=[])
                self.assertEqual('', extract_epub_text('synthetic.epub', archive_bytes=raw_x))
                _root, segs, full = get_xhtml_segments(raw_x, 'OEBPS/ch1.xhtml')
                self.assertEqual([], segs)
                self.assertEqual('', full)
                with tempfile.TemporaryDirectory() as tmp:
                    epub_path = Path(tmp) / f'{name}.epub'
                    epub_path.write_bytes(raw_x)
                    with self.assertRaisesRegex(ValueError, r'no readable text after omitting HTML hidden elements'):
                        alignment.load_epub(str(epub_path))
                # Non-XHTML narrates the secret exactly as origin/main did.
                raw_html = make_epub('', raw_document=raw_doc, media_type='text/html',
                                     toc_links=[])
                self.assertEqual('Secret.',
                                 extract_epub_text('synthetic.epub', archive_bytes=raw_html))

        # Gate row c: a prefixed `h:hidden` on <body> is not the HTML
        # attribute, so the chapter is narrated on every path.
        body_h_hidden = doc_tpl.format(
            extra=' xmlns:h="http://www.w3.org/1999/xhtml"',
            inner='<head><title>T</title></head><body h:hidden=""><p>Secret.</p></body>')
        raw_prefixed = make_epub('', raw_document=body_h_hidden, toc_links=[])
        self.assertEqual('Secret.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_prefixed))
        _root, prefixed_segs, prefixed_full = get_xhtml_segments(
            raw_prefixed, 'OEBPS/ch1.xhtml')
        self.assertEqual(['Secret.'], [s['text'] for s in prefixed_segs])
        self.assertIn('Secret.', prefixed_full)
        self.assertIn('Secret.', _load_bytes(raw_prefixed))

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

        # Finding 6: the hidden-specific refusal fires only when every spine
        # document is entirely hidden. One hidden chapter plus one empty
        # visible chapter behaves as origin/main (empty text, no refusal).
        empty_visible_doc = doc_tpl.format(
            extra='', inner='<head><title>T</title></head><body></body>')
        hidden_doc = doc_tpl.format(
            extra='', inner='<head><title>T</title></head><body hidden=""><p>Secret.</p></body>')
        raw_mixed = make_multi_epub([('ch1.xhtml', hidden_doc), ('ch2.xhtml', empty_visible_doc)])
        self.assertEqual('', extract_epub_text('synthetic.epub', archive_bytes=raw_mixed))
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'mixed.epub'
            epub_path.write_bytes(raw_mixed)
            # origin/main returns the empty visible chapter's whitespace
            # rather than the hidden-specific refusal.
            self.assertFalse(alignment.load_epub(str(epub_path)).strip())
        # Two hidden chapters: the hidden-specific refusal does fire.
        raw_all_hidden = make_multi_epub([('ch1.xhtml', hidden_doc), ('ch2.xhtml', hidden_doc)])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'all_hidden.epub'
            epub_path.write_bytes(raw_all_hidden)
            with self.assertRaisesRegex(ValueError, r'no readable text after omitting HTML hidden elements'):
                alignment.load_epub(str(epub_path))
        # A single empty visible chapter (no hidden content) keeps the generic
        # empty result, not the hidden-specific refusal.
        raw_empty = make_multi_epub([('ch1.xhtml', empty_visible_doc)])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'empty_visible.epub'
            epub_path.write_bytes(raw_empty)
            self.assertFalse(alignment.load_epub(str(epub_path)).strip())

    def test_hidden_markup_hint_is_a_linear_conservative_superset(self):
        # One linear substring check engages the strict pass; the walker then
        # makes the hidden decision. It must fire for the exact browser
        # attribute AND for a DTD internal-subset `ATTLIST` that defaults
        # `hidden` onto elements. False positives (prose, class, aria) are
        # harmless: the walk finds nothing hidden and keeps the document.
        for source in ('<p hidden="">', '<p class="x" hidden="">',
                       "<p\nhidden='x'>", '<p hidden = "x">',
                       '<!DOCTYPE html [<!ATTLIST p hidden CDATA "x">]>',
                       '<!DOCTYPE html [<!ENTITY h "hidden">]>'):
            with self.subTest(fires=source):
                self.assertIn(_HIDDEN_MARKUP_HINT, source)
        # A document without the word takes the unchanged fast path; the
        # false-positive shapes above still narrate their visible text.
        self.assertNotIn(_HIDDEN_MARKUP_HINT, '<p>Plain.</p>')
        for source, expected in (
            ('<p class="hidden">Visible one.</p>', 'Visible one.'),
            ('<p title="hidden = 5">Visible two.</p>', 'Visible two.'),
            ('<p aria-hidden="true">Visible three.</p>', 'Visible three.'),
        ):
            with self.subTest(kept=source):
                raw = make_epub(source, toc_links=[])
                self.assertEqual(expected,
                                 extract_epub_text('synthetic.epub', archive_bytes=raw))

    def test_other_dtd_attribute_default_is_unchanged(self):
        # A DTD that declares some OTHER attribute is not hidden handling:
        # every path matches the same document under a DTD that declares an
        # attribute on a different element, and the walker reports no hidden
        # subtree. (The cross-tree probe compares both to origin/main.)
        body = ('<html xmlns="' + XHTML_NS + '"><head><title>T</title></head>'
                '<body><p>Same one.</p><div>Same two.</div></body></html>')
        other_attribute = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE html [<!ATTLIST p class CDATA "default-class">]>'
            + body)
        control = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE html [<!ATTLIST div class CDATA "default-class">]>'
            + body)
        self.assertFalse(alignment.strip_hidden_xml(other_attribute).saw_hidden)
        other_extract, other_segments, other_load = _three_paths(
            make_epub('', raw_document=other_attribute, toc_links=[]))
        control_extract, control_segments, control_load = _three_paths(
            make_epub('', raw_document=control, toc_links=[]))
        self.assertEqual('Same one.\n\nSame two.', other_extract)
        self.assertEqual(control_extract, other_extract)
        self.assertEqual(control_segments, other_segments)
        self.assertEqual(_normalised(control_load), _normalised(other_load))

    def test_dtd_default_hidden_is_omitted_on_all_paths(self):
        # Merge gate 2 finding 1: an internal-subset ATTLIST can default
        # `hidden` onto every <p>. Both browsers hide those elements and so
        # must every path; the body has no literal `hidden=` anywhere. The
        # second document has no skip-tag opening, so the only reason the
        # extraction walk runs is the hidden hint itself.
        for head in ('<head><title>T</title></head>', '<head></head>'):
            with self.subTest(head=head):
                dtd_hidden = (
                    '<?xml version="1.0" encoding="utf-8"?>\n'
                    '<!DOCTYPE html [<!ATTLIST p hidden CDATA "x">]>\n'
                    f'<html xmlns="{XHTML_NS}">{head}'
                    '<body><p>VISA</p><div>VISB</div></body></html>')
                scan = alignment.strip_hidden_xml(dtd_hidden)
                self.assertIsNotNone(scan)
                self.assertTrue(scan.saw_hidden)
                self.assertNotIn('VISA', scan.visible)
                self.assertIn('VISB', scan.visible)
                extracted, segments, loaded = _three_paths(
                    make_epub('', raw_document=dtd_hidden, toc_links=[]))
                self.assertEqual('VISB', extracted)
                self.assertEqual(['VISB'], segments)
                self.assertNotIn('VISA', loaded)
                self.assertIn('VISB', loaded)

    def test_hidden_root_with_markup_like_prolog_text_is_not_reparsed(self):
        # Merge gate 2 finding 5: a prolog comment or PI containing `<p` made
        # the old text hint try to parse the element-less remainder, so lxml
        # logged a decode warning. The walker's own answer decides instead.
        visible = (
            f'<?xml version="1.0"?>'
            f'<html xmlns="{XHTML_NS}"><head><title>V</title></head>'
            '<body><p>VISCH2</p></body></html>')
        cases = {
            'comment_lt': (
                f'<?xml version="1.0"?>\n<!-- <p a comment -->'
                f'<html xmlns="{XHTML_NS}" hidden=""><head><title>T</title>'
                '</head><body><p>SEC</p></body></html>'),
            'pi_lt': (
                f'<?xml version="1.0"?>\n<?foo <p?>'
                f'<html xmlns="{XHTML_NS}" hidden=""><body><p>SEC</p>'
                '</body></html>'),
            'trailing_comment': (
                f'<?xml version="1.0"?>'
                f'<html xmlns="{XHTML_NS}" hidden=""><body><p>SEC</p>'
                '</body></html><!-- <b -->'),
        }
        for name, document in cases.items():
            with self.subTest(case=name):
                scan = alignment.strip_hidden_xml(document)
                self.assertIsNotNone(scan)
                self.assertFalse(scan.saw_visible_element)
                raw = make_multi_epub([('ch1.xhtml', document),
                                       ('ch2.xhtml', visible)])
                captured = io.StringIO()
                with contextlib.redirect_stderr(captured):
                    loaded = _load_bytes(raw)
                self.assertEqual('VISCH2', _normalised(loaded))
                self.assertNotIn('could not be decoded', captured.getvalue())
        # A hidden body beside a visible root still has an element left, so
        # its visible remainder is parsed (the flag is not "nothing hidden").
        scan = alignment.strip_hidden_xml(
            f'<html xmlns="{XHTML_NS}"><head><title>T</title></head>'
            '<body hidden=""><p>SEC</p></body></html>')
        self.assertTrue(scan.saw_visible_element)

    def test_hidden_load_agrees_on_declared_single_byte_and_utf16_encodings(self):
        # Review gate 1: extraction, read-aloud and load must agree for the
        # same hidden-bearing document in a declared non-UTF-8 encoding.
        for encoding, codec in (('windows-1252', 'cp1252'),
                                ('iso-8859-1', 'latin-1'),
                                ('utf-16', 'utf-16')):
            with self.subTest(encoding=encoding):
                source = (
                    f'<?xml version="1.0" encoding="{encoding}"?>\n'
                    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title>'
                    '</head><body><p>Before café.</p><p hidden="">SECRET</p>'
                    '<p>After.</p></body></html>')
                raw = make_epub('', raw_document=source.encode(codec), toc_links=[])
                extracted, segments, loaded = _three_paths(raw)
                self.assertEqual('Before café.\n\nAfter.', extracted)
                self.assertEqual(['Before café.', 'After.'], segments)
                self.assertEqual('Before café. After.', _normalised(loaded))

    def test_substituted_markup_normalizes_the_declaration_to_utf8(self):
        # The substituted value is re-encoded as UTF-8, so the XML declaration
        # must not keep naming the old single-byte/UTF-16 encoding.
        encoded = alignment._utf8_markup(
            '<?xml version="1.0" encoding="windows-1252"?>\n<p>café</p>')
        self.assertEqual(
            b'<?xml version="1.0" encoding="utf-8"?>\n<p>caf\xc3\xa9</p>',
            encoded)

    def test_unknown_declared_encoding_declines_hidden_handling(self):
        # is_well_formed_xml and extraction's decoder both reject an unknown
        # declared encoding; load_epub must narrate the hidden text like
        # origin/main rather than applying hidden handling to it.
        raw_doc = (
            b'<?xml version="1.0" encoding="bogus"?>\n'
            b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title>'
            b'</head><body><p>Before.</p><p hidden="">SECRET</p>'
            b'<p>After.</p></body></html>')
        self.assertFalse(alignment.is_well_formed_xml(raw_doc))
        raw = make_epub('', raw_document=raw_doc, toc_links=[])
        with self.assertRaises(LookupError):
            extract_epub_text('synthetic.epub', archive_bytes=raw)
        self.assertIn('SECRET', _load_bytes(raw))

    def test_bad_encoding_name_load_stays_byte_identical_to_origin_main(self):
        # Merge gate 3 finding 1: a hidden-free XHTML item whose declaration
        # names a non-ASCII or NUL encoding is refused by extraction (#2368)
        # but was narrated by origin/main's load_epub. Hidden handling must
        # decline on the decode failure and leave ebooklib's path unchanged.
        rows = {
            'non_ascii_name': (
                b'<?xml version="1.0" encoding="\xe9"?>',
                UnicodeDecodeError,
                "ascii' codec can't decode byte 0xe9"),
            'nul_name': (
                b'<?xml version="1.0" encoding="ut\x00f-8"?>',
                ValueError,
                'embedded null character'),
        }
        body = (
            b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            b'</head><body><p>Hello world.</p></body></html>')
        for name, (declaration, extraction_error, message) in rows.items():
            with self.subTest(case=name):
                raw = make_epub('', raw_document=declaration + body,
                                toc_links=[])
                # origin/main's literal load_epub output, pinned byte for byte.
                self.assertEqual(
                    '\n \n \n \n \n Hello world. \n \n \n', _load_bytes(raw))
                with self.assertRaisesRegex(extraction_error, message):
                    extract_epub_text('synthetic.epub', archive_bytes=raw)
                with self.assertRaisesRegex(
                        ValueError, '^Spine document is malformed: '
                                    'OEBPS/ch1.xhtml$'):
                    get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')

    def test_load_epub_hidden_script_only_is_not_entirely_hidden(self):
        # `script` text was never narrated, so hiding it removed no narrated
        # text: this is origin/main's empty result, not the hidden refusal.
        raw = make_epub('<script hidden="">var secret = 1;</script>', toc_links=[])
        self.assertFalse(_load_bytes(raw).strip())

    def test_malformed_xhtml_load_epub_narrates_hidden_like_origin_main(self):
        # Review 6 finding 2: load_epub must apply hidden handling only when
        # the shared predicate says the XHTML document is well-formed XML;
        # extraction deliberately narrates these same documents. Both now
        # behave as origin/main (hidden narrated, no hidden-specific refusal).
        bodies = [
            ('bare_boolean', '<p hidden>OnlyText</p>', 'OnlyText'),
            ('unclosed', '<div hidden=""><p>OnlyText', 'OnlyText'),
        ]
        for name, body, token in bodies:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmp:
                    raw = make_epub(body, toc_links=[])
                    epub_path = Path(tmp) / f'{name}.epub'
                    epub_path.write_bytes(raw)
                    loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
                self.assertIn(token, loaded)

    def test_load_epub_hidden_void_only_document_is_not_entirely_hidden(self):
        # Review 6 finding 3: a document whose only hidden elements are void
        # elements ('<br hidden/>', '<img hidden/>') omitted no text, so it is
        # not "entirely hidden" and keeps origin/main's empty result. A hidden
        # element that does remove words still triggers the refusal.
        raw = make_epub('<br hidden=""/><img hidden="" src="x.png"/>', toc_links=[])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'voids.epub'
            epub_path.write_bytes(raw)
            self.assertFalse(alignment.load_epub(str(epub_path)).strip())
        raw_words = make_epub('<p hidden="">Words.</p>', toc_links=[])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'words.epub'
            epub_path.write_bytes(raw_words)
            with self.assertRaisesRegex(
                    ValueError, r'no readable text after omitting HTML hidden elements'):
                alignment.load_epub(str(epub_path))

    def test_anchor_id_helper_matches_the_tolerant_extractor_lookup(self):
        # Item 6: the XML-path helper must agree with the inline lookup in
        # _HTMLTextExtractor.handle_starttag on id / xml:id / a@name / none.
        cases = [
            ('<p id="a1">x</p>', 'a1'),
            ('<p xml:id="b1">x</p>', 'b1'),
            ('<a name="c1">x</a>', 'c1'),
            ('<a name="d1" id="d2">x</a>', 'd2'),
        ]
        for markup, anchor_id in cases:
            with self.subTest(markup=markup):
                extractor = _HTMLTextExtractor(anchor_ids={anchor_id})
                extractor.feed(markup)
                self.assertEqual(1, extractor.anchor_counts.get(anchor_id))
        self.assertIsNone(_get_anchor_id('div', [('name', 'n1')]))
        self.assertIsNone(_get_anchor_id('p', []))
        self.assertEqual('e1', _get_anchor_id('p', {'id': 'e1'}))
        self.assertEqual('f1', _get_anchor_id('p', {'xml:id': 'f1'}))
        self.assertEqual('g1', _get_anchor_id('a', {'name': 'g1'}))
        self.assertIsNone(_get_anchor_id('a', {'id': None}))
        # Expat reports raw attribute names while HTMLParser lowercases them.
        # Lowercasing here is what makes the two lookups agree on <p ID="x">.
        self.assertEqual('h1', _get_anchor_id('p', {'ID': 'h1'}))
        self.assertEqual('i1', _get_anchor_id('p', {'XML:ID': 'i1'}))
        self.assertEqual('j1', _get_anchor_id('a', {'NAME': 'j1'}))

    def test_uppercase_id_on_a_hidden_element_resolves_as_a_hidden_target(self):
        # Review 8 finding 4: the extractor lowercases attribute names, so a
        # TOC target #x on <p ID="x" hidden=""> must be reported as
        # target_in_hidden_subtree, not unresolved_anchor.
        body = '<p ID="x" hidden="">Secret.</p><p>Visible.</p>'
        raw = make_epub(body, toc_links=[('ch1.xhtml#x', 'Upper ID')])
        text, report = extract_epub_text(
            'synthetic.epub', archive_bytes=raw, include_report=True)
        self.assertEqual('Visible.', text)
        row = next(r for r in report['rows']
                   if r['kind'] == 'navigation_target' and r['fragment'] == 'x')
        self.assertEqual('target_in_hidden_subtree', row['reason'])
        self.assertIsNone(row['position'])

    def test_extraction_and_load_share_one_hidden_subtree_walk(self):
        # Review 8 finding 3 (Rule 15): both callers use the one
        # alexandria_alignment walker, so for a document with only hidden
        # subtrees their substitutions are byte-identical.
        document = (
            '<?xml version="1.0"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>VIS</p>'
            '<div hidden=""><p>SEC</p></div><p>TAIL</p></body></html>')
        expected = (
            '<?xml version="1.0"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>VIS</p>'
            '<p>TAIL</p></body></html>')
        self.assertEqual(expected, alignment.cut_xml_subtrees(document).visible)
        self.assertEqual(expected, _omit_xml_ignored_subtrees(document))


class FuzzKnownBugFilterTests(unittest.TestCase):
    """The fuzz harness's #2368 filter must follow the shared decode helper."""

    def test_known_bug_filter_accepts_the_shared_xhtml_decode_frames(self):
        fuzz_dir = str(Path(__file__).resolve().parents[2] / 'tools' / 'fuzz')
        if fuzz_dir not in sys.path:
            sys.path.insert(0, fuzz_dir)
        import _common  # noqa: PLC0415 - tools/fuzz is not on the test path

        # The three review-gate-3 inputs: a non-ASCII declared name (raised in
        # _decode_head_encoding), NUL inside the name (decode_epub_markup), and
        # an unknown codec (decode_epub_markup). All are open bug #2368.
        # Catch by hand: unittest's assertRaises strips the traceback, which
        # the predicate needs.
        for data in (b'<?xml version="1.0" encoding="\xe9"?><a/>',
                     b'<?xml version="1.0" encoding="ut\x00f-8"?><a/>',
                     b'<?xml version="1.0" encoding="bogus"?><a/>'):
            with self.subTest(data=data):
                try:
                    script.get_epub_xhtml_text(data)
                except (LookupError, ValueError, UnicodeError) as exc:
                    self.assertTrue(
                        _common.is_known_xhtml_encoding_error(exc),
                        f'{type(exc).__name__} not filtered')
                else:
                    self.fail('expected a declared-encoding decode error')

        # Same type and message, but raised outside the two #2368 sites.
        try:
            b'x'.decode('ut\x00f-8', errors='replace')
        except ValueError as exc:
            self.assertFalse(_common.is_known_xhtml_encoding_error(exc))
        else:
            self.fail('expected ValueError("embedded null character")')

        # A real UnicodeError from another alexandria_alignment function: same
        # file, but not one of the decode sites.
        try:
            alignment._utf8_markup('\ud800')
        except UnicodeEncodeError as exc:
            self.assertFalse(_common.is_known_xhtml_encoding_error(exc))
        else:
            self.fail('expected UnicodeEncodeError from _utf8_markup')


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

    def test_text_html_readaloud_narrates_hidden_like_origin_main(self):
        # The read-aloud segmentation uses the same document-scope predicate:
        # a text/html spine item is not XHTML, so hidden attributes are
        # stripped before the walk and the hidden text is narrated exactly as
        # origin/main. The same body as XHTML omits it.
        body = ('<p>Seen first.</p><p hidden="hidden">Never shown.</p>'
                '<p>Seen last.</p>')
        raw_html = make_epub(body, media_type='text/html', toc_links=[])
        _root, _segments, full = get_xhtml_segments(raw_html, 'OEBPS/ch1.xhtml')
        self.assertIn('Never shown.', full)
        raw_xhtml = make_epub(body, toc_links=[])
        _root, _segments, hidden_applied = get_xhtml_segments(
            raw_xhtml, 'OEBPS/ch1.xhtml')
        self.assertNotIn('Never shown.', hidden_applied)
        self.assertIn('Seen first.', hidden_applied)

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
                    # to whole-paragraph clip 'p-inline'). A hidden element is
                    # skipped exactly like _EXCLUDE, never cloned as an atom,
                    # so no hidden markup survives in the output XHTML.
                    with zipfile.ZipFile(io.BytesIO(built)) as zf:
                        ch1_xml = zf.read('OEBPS/ch1.xhtml').decode('utf-8')
                    self.assertNotIn('Hidden aside.', ch1_xml)
                    self.assertNotIn('hidden=""', ch1_xml.split('p-inline')[1]
                                     .split('</p>')[0])
                    self.assertNotIn('p-inline', clip_ids)
                    self.assertEqual(9, len(clips), clips)
                else:
                    self.assertGreaterEqual(len(clips), 1)

    def test_alignment_loader_handles_hidden_namespaces_and_out_of_scope_attrs(self):
        body = (
            '<p>Visible one.</p>'
            '<p hidden="hidden">Drop canonical.</p>'
            '<p hidden="until-found">Drop until found.</p>'
            '<p h:hidden="hidden">Keep xhtml prefixed.</p>'
            '<p x:hidden="hidden">Keep foreign prefixed.</p>'
            '<p HIDDEN="">Keep upper attribute.</p>'
            '<p xmlns="" hidden="">Keep null namespace.</p>'
            '<p style="display:none">Keep CSS.</p>'
            '<p aria-hidden="true">Keep aria.</p>'
        )
        raw = make_epub(
            body,
            extra_html_attrs=' xmlns:h="http://www.w3.org/1999/xhtml" xmlns:x="urn:example"',
            toc_links=[],
        )
        loaded = _load_bytes(raw)
        for kept in ('Visible one.', 'Keep xhtml prefixed.', 'Keep foreign prefixed.',
                     'Keep upper attribute.', 'Keep null namespace.',
                     'Keep CSS.', 'Keep aria.'):
            with self.subTest(kept=kept):
                self.assertIn(kept, loaded)
        self.assertNotIn('Drop canonical.', loaded)
        self.assertNotIn('Drop until found.', loaded)

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

    def test_block_descendant_inside_script_counts_like_origin_main(self):
        # Merge gate 2 finding 2: origin/main counted a block inside an XHTML
        # <script>/<style> element when deciding whether a block is a segment
        # root. Only hidden subtrees are newly skipped. These documents are
        # hidden-free, so segments and ids must match origin/main exactly.
        cases = [
            ('<blockquote id="q"><div id="d1">Quote here.</div>'
             '<div id="d2">More quote.</div><script><h2>s</h2></script>'
             '</blockquote>',
             [('d1', 'Quote here.'), ('d2', 'More quote.')]),
            ('<li id="li1"><div id="d3">Text one here.</div>'
             '<script><p>x</p></script></li><p id="p2">Second para.</p>',
             [('d3', 'Text one here.'), ('p2', 'Second para.')]),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                raw = make_epub(body, toc_links=[])
                _root, segs, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
                self.assertEqual(expected,
                                 [(s['fragment'], s['text']) for s in segs])
        # The blockquote shape builds exactly as origin/main's did: the
        # script stays in the document and is not segmented.
        raw = make_epub(cases[0][0])
        _text, built, _report = self._build_from_extracted_script(raw, qwen=False)
        with zipfile.ZipFile(io.BytesIO(built)) as zf:
            ch1_xml = zf.read('OEBPS/ch1.xhtml').decode('utf-8')
        self.assertIn('<script>', ch1_xml)
        self.assertIn('Quote here.', ch1_xml)
        self.assertIn('More quote.', ch1_xml)

    def test_dtd_default_hidden_builds_without_the_sync_refusal(self):
        # Merge gate 2 finding 1: extraction now omits DTD-defaulted hidden
        # elements, so the read-aloud build gets the same visible text and no
        # longer refuses a book origin/main built.
        document = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html [<!ATTLIST p hidden CDATA "x">]>\n'
            f'<html xmlns="{XHTML_NS}"><head><title>T</title></head>'
            '<body><p>Visible para one.</p><div>Div text two.</div>'
            '<p>Para three.</p></body></html>')
        raw = make_epub('', raw_document=document)
        for qwen in (False, True):
            with self.subTest(qwen=qwen):
                text, built, _report = self._build_from_extracted_script(
                    raw, qwen=qwen)
                self.assertIn('Div text two.', text)
                self.assertNotIn('Visible para one.', text)
                with zipfile.ZipFile(io.BytesIO(built)) as zf:
                    ch1_xml = zf.read('OEBPS/ch1.xhtml').decode('utf-8')
                self.assertIn('hidden="x"', ch1_xml)
        _root, segments, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
        self.assertEqual(['Div text two.'], [s['text'] for s in segments])

    def test_optional_titles_keep_ids_inside_script_and_style_as_origin_main(self):
        # Review 7 finding 4: hidden handling's scope is hidden subtrees only.
        # Ids inside script/style stay indexed exactly as origin/main indexed
        # them, so a hidden-free book speaks the same navigation labels.
        xhtml = '{http://www.w3.org/1999/xhtml}'
        root = ET.fromstring(
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
            '<style id="s1">p{}</style><script id="sc1">x()</script></head>'
            '<body><p id="p1">One.</p><p id="p2">Two.</p></body></html>')
        body = root.find(f'{xhtml}body')
        segments = [{'element': p, 'text': ''.join(p.itertext())} for p in body]
        toc = [
            {'path': 'ch1.xhtml', 'fragment': 's1', 'label': 'StyleLabel'},
            {'path': 'ch1.xhtml', 'fragment': 'sc1', 'label': 'ScriptLabel'},
            {'path': 'ch1.xhtml', 'fragment': 'p2', 'label': 'Two'},
        ]
        self.assertEqual({0: ['StyleLabel', 'ScriptLabel'], 1: ['Two']},
                         _get_optional_titles(root, segments, toc, 'ch1.xhtml'))

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

    def test_hidden_inline_element_is_skipped_not_atomised_in_spans(self):
        # Review 8 finding 2: a hidden element is skipped exactly like
        # _EXCLUDE. It emits no atom and no clone, and the coalesced text
        # events equal the document with the hidden subtree deleted.
        body = ('<p id="p1">Before <span hidden=""><em>Hidden</em></span>'
                'after.</p>')
        raw = make_epub(body, toc_links=[])
        _root, segs, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
        segment = next(s for s in segs if s['text'].startswith('Before'))
        events = _inline_events(segment['element'])
        self.assertEqual(['text'], [kind for kind, _ in events])
        self.assertEqual('Before after.',
                         ''.join(value for _, value in events))
        self.assertEqual('Before after.', segment['text'])

        minus = make_epub('<p id="p1">Before after.</p>', toc_links=[])
        _root, segs_minus, _full = get_xhtml_segments(minus, 'OEBPS/ch1.xhtml')
        segment_minus = next(s for s in segs_minus
                             if s['text'].startswith('Before'))
        self.assertEqual(
            [(kind, value if isinstance(value, str) else _local(value.tag))
             for kind, value in events],
            [(kind, value if isinstance(value, str) else _local(value.tag))
             for kind, value in _inline_events(segment_minus['element'])])


if __name__ == '__main__':
    unittest.main()
