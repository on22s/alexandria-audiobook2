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
from routers.script import (
    _HTMLTextExtractor,
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
            ('upper_attr', '<p>Start.</p><p HIDDEN="HIDDEN">Secret.</p><p>End.</p>'),
            ('mixed_case', '<p>Start.</p><p HiDdEn="Hidden">Secret.</p><p>End.</p>'),
            ('false_still_hidden', '<p>Start.</p><p hidden="false">Secret.</p><p>End.</p>'),
        ]
        for name, body in doc_cases:
            with self.subTest(case=name):
                raw = make_epub(body, toc_links=[])
                self.assertEqual(
                    'Start.\n\nEnd.',
                    extract_epub_text('synthetic.epub', archive_bytes=raw))

        # XHTML-namespaced prefixed form applies on the XHTML path.
        prefixed = '<p>Start.</p><p h:hidden="hidden">Secret.</p><p>End.</p>'
        extra = ' xmlns:h="http://www.w3.org/1999/xhtml"'
        raw = make_epub(prefixed, extra_html_attrs=extra, toc_links=[])
        self.assertEqual('Start.\n\nEnd.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

        # Nested hidden elements and hidden descendants.
        nested = ('<p>Before.</p><div hidden=""><p hidden="">Double hidden</p>'
                  '<p>Still inside outer hidden.</p></div><p>After.</p>')
        raw = make_epub(nested, toc_links=[])
        self.assertEqual('Before.\n\nAfter.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw))

        # Unnamespaced XHTML root document
        raw_unns = make_epub(
            '',
            raw_document='<html><body><p>Start.</p><p hidden="">Secret.</p><p>End.</p></body></html>',
            toc_links=[],
        )
        self.assertEqual('Start.\n\nEnd.',
                         extract_epub_text('synthetic.epub', archive_bytes=raw_unns))

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

    def test_mathml_annotation_xml_html_integration_point(self):
        # annotation-xml with encoding text/html or application/xhtml+xml is
        # an HTML integration point. Because these test documents put <math>
        # in the inherited XHTML namespace, the strict XML path applies to the
        # hidden <p>, so the XHTML paths agree. text/html narrates it.
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

    def test_prefixed_xhtml_hidden_applies_on_every_xml_path(self):
        body = '<p h:hidden="hidden">Prefixed secret.</p><p>Visible.</p>'
        extra = ' xmlns:h="http://www.w3.org/1999/xhtml"'
        raw = make_epub(body, extra_html_attrs=extra, toc_links=[])
        self.assertEqual('Visible.', extract_epub_text('synthetic.epub', archive_bytes=raw))
        _root, segs, _full = get_xhtml_segments(raw, 'OEBPS/ch1.xhtml')
        self.assertEqual(['Visible.'], [s['text'] for s in segs])
        with tempfile.TemporaryDirectory() as tmp:
            epub_path = Path(tmp) / 'prefixed.epub'
            epub_path.write_bytes(raw)
            loaded = ' '.join(alignment.load_epub(str(epub_path)).split())
        self.assertEqual('Visible.', loaded)
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
            ('body_h_hidden', doc_tpl.format(extra=' xmlns:h="http://www.w3.org/1999/xhtml"', inner='<head><title>T</title></head><body h:hidden=""><p>Secret.</p></body>')),
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
