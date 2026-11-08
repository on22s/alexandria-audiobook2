"""Bug #1795: an unclosed skipped element must not silently drop a chapter.

Synthetic EPUB fixtures only.  The production extractor silently swallowed
the rest of a spine resource when its <title>/<style>/<script> was never
closed (a stray skipped-element close is tolerated - see the CDATA/comment
compatibility controls), so a three-chapter book imported as only the first
and last chapters.  These tests pin the
narrow repair: a genuinely unclosed skipped element is an actionable error,
while ordinary tolerant HTML and legitimate empty/non-text resources are
still accepted.
"""
import html.parser
import io
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

from routers import script
from tests import test_upload_publication_source as uploads
from tests.test_epub_extraction_report import archive as report_archive


def make_epub(middle_body, *, before_last_manifest='', before_last_spine='',
              extra_files=None, middle_name='middle.xhtml'):
    """A synthetic three-spine EPUB: first / middle / last."""
    package = (
        '<package><manifest>'
        '<item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>'
        f'<item id="middle" href="{middle_name}" media-type="application/xhtml+xml"/>'
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
        (middle_name, middle_body),
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

    def test_stray_skip_close_is_tolerated_but_crossed_close_still_raises(self):
        # A skipped-element close with no skipped element open cannot leave
        # pending skipped content, so it is tolerated (the original
        # extractor's behaviour). This matters for legitimate well-formed
        # XHTML: HTMLParser scans script/style/title as raw/RCDATA text, so
        # a literal closing sequence inside XML CDATA or a comment closes
        # its raw mode early and the real close then looks "stray" here.
        # Rejecting it would refuse well-formed books.
        tolerated = [
            ('stray_style',
             '<html><body><title>Head</title></style>'
             '<p>Middle readable chapter.</p></body></html>'),
            ('stray_title',
             '<html><body><style>p{}</style></title>'
             '<p>Middle readable chapter.</p></body></html>'),
            ('stray_script',
             '<html><body><script>var x=1;</script></script>'
             '<p>Middle readable chapter.</p></body></html>'),
            # Markup that looks like a nested skipped element inside <title>
            # is literal; the trailing </style> is then a stray close.
            ('literal_nest_then_stray',
             '<html><body><title><style>literal</title></style>'
             '<p>Middle readable chapter.</p></body></html>'),
        ]
        for name, body in tolerated:
            with self.subTest(case=name):
                raw = make_epub(body)
                text = extract(raw)
                self.assertIn('Middle readable chapter.', text)
                self.assertIn('First readable chapter.', text)
                self.assertIn('Final readable chapter.', text)
                reported, _report = extract(raw, report=True)
                self.assertEqual(text, reported)
        # By contrast, a wrong close inside raw content is literal data, so
        # the enclosing skipped element is genuinely still open at the end
        # of the resource and must still error.
        crossed = make_epub(
            '<html><body><style>code</script>'
            '<p>Middle readable chapter.</p></body></html>')
        with self.assertRaisesRegex(ValueError, r'middle\.xhtml') as caught:
            extract(crossed)
        self.assertIn('style', str(caught.exception))
        self.assertIn('unclosed', str(caught.exception))
        with self.assertRaisesRegex(ValueError, r'middle\.xhtml'):
            extract(crossed, report=True)

    def test_wellformed_cdata_and_comment_resources_are_accepted(self):
        # Each body is well-formed XML (ET control below): only ]]> ends a
        # CDATA section and only --> ends a comment, so a literal skipped-
        # element close inside either is legal data. The XML-aware path
        # omits those true ignored subtrees; HTMLParser's raw-text scanning
        # alone would end the skipped element at the literal sequence.
        # Only acceptance and that the surrounding chapters survive are
        # pinned here; exact-output metamorphic coverage is in
        # test_xml_literal_markup_in_ignored_subtrees_is_omitted_exactly.
        cases = [
            ('script_cdata',
             '<?xml version="1.0" encoding="utf-8"?>'
             '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
             '<script><![CDATA[var end="</script>";]]></script>'
             '<p>Middle readable chapter.</p></body></html>'),
            ('style_cdata',
             '<?xml version="1.0" encoding="utf-8"?>'
             '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
             '<style><![CDATA[p { content: "</style>"; }]]></style></head><body>'
             '<p>Middle readable chapter.</p></body></html>'),
            ('title_comment',
             '<?xml version="1.0" encoding="utf-8"?>'
             '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
             '<title><!-- </title> -->Head words</title></head><body>'
             '<p>Middle readable chapter.</p></body></html>'),
        ]
        for name, body in cases:
            with self.subTest(case=name):
                ET.fromstring(body.encode('utf-8'))  # well-formedness control
                raw = make_epub(body)
                text = extract(raw)
                self.assertIn('First readable chapter.', text)
                self.assertIn('Middle readable chapter.', text)
                self.assertIn('Final readable chapter.', text)
                reported, _report = extract(raw, report=True)
                self.assertEqual(text, reported)

    def test_xml_literal_markup_in_ignored_subtrees_is_omitted_exactly(self):
        # Metamorphic: inserting a genuinely closed XML ignored subtree
        # whose CDATA/comment/PI content contains literal skipped-element
        # closes and opens must not change narration at all. HTMLParser's
        # raw scanning alone both falsely rejects (a literal close+open
        # with no later real close of the phantom) and silently drops the
        # chapter (the phantom later closes), so exact equality is the
        # meaningful assertion, alongside the ET well-formedness control.
        expected = ('First readable chapter.\n\nMiddle readable chapter.\n\n'
                    'Final readable chapter.')
        for tag in ('script', 'style', 'title'):
            for opened in ('script', 'style', 'title'):
                for kind in ('cdata', 'comment', 'pi'):
                    with self.subTest(tag=tag, opened=opened, kind=kind):
                        literal = f'</{tag}><{opened}>'
                        if kind == 'cdata':
                            payload = f'<![CDATA[var t = "{literal}";]]>'
                        elif kind == 'comment':
                            payload = f'<!-- {literal} -->ignored words'
                        else:
                            payload = f'<?note {literal} ?>ignored words'
                        body = (f'<html><body><{tag}>{payload}</{tag}>'
                                '<p>Middle readable chapter.</p></body></html>')
                        ET.fromstring(body.encode('utf-8'))
                        self.assertEqual(expected, extract(make_epub(body)))

    def test_xml_omission_preserves_unicode_crlf_encodings_and_namespaces(self):
        # Outside the omitted subtree, the lexical stream is untouched:
        # Unicode text, CRLF, namespace/prefixed elements, nested ignored
        # elements and self-closing ignored elements all survive exactly.
        cases = [
            ('unicode',
             '<html><body><script><![CDATA[caf\u00e9 </script>]]>'
             '</script><p>Middle caf\u00e9 \u65e5\u672c\u8a9e.</p>'
             '</body></html>',
             'First readable chapter.\n\nMiddle caf\u00e9 \u65e5\u672c\u8a9e.\n\n'
             'Final readable chapter.'),
            ('default_namespace',
             '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
             '<script><![CDATA[var t="</script><style>";]]></script>'
             '<p>Middle readable chapter.</p></body></html>',
             'First readable chapter.\n\nMiddle readable chapter.\n\n'
             'Final readable chapter.'),
            # A prefixed name is NOT a skipped element under the existing
            # exact-tag semantics, so its text is ordinary narration.
            ('prefixed_foreign_retained',
             '<html xmlns:x="urn:x"><body>'
             '<x:script>var hidden = 1;</x:script>'
             '<p>Middle readable chapter.</p></body></html>',
             'First readable chapter.\n\nvar hidden = 1;\n\n'
             'Middle readable chapter.\n\nFinal readable chapter.'),
            ('nested_actual',
             '<html><body><script><style><![CDATA[</style><script>]]>'
             '</style></script><p>Middle readable chapter.</p>'
             '</body></html>',
             'First readable chapter.\n\nMiddle readable chapter.\n\n'
             'Final readable chapter.'),
            ('selfclosing',
             '<html><body><script/><style/><title/>'
             '<p>Middle readable chapter.</p></body></html>',
             'First readable chapter.\n\nMiddle readable chapter.\n\n'
             'Final readable chapter.'),
        ]
        for name, body, expected in cases:
            with self.subTest(case=name):
                ET.fromstring(body.encode('utf-8'))
                self.assertEqual(expected, extract(make_epub(body)))
        # CRLF outside the subtree: insertion must not change extraction.
        crlf_base = ('<html><body>\r\n<p>Middle readable chapter.</p>'
                     '\r\n</body></html>')
        crlf_variant = ('<html><body>\r\n<script><![CDATA[</script>]]>'
                        '</script>\r\n<p>Middle readable chapter.</p>'
                        '\r\n</body></html>')
        ET.fromstring(crlf_variant.encode('utf-8'))
        self.assertEqual(extract(make_epub(crlf_base)),
                         extract(make_epub(crlf_variant)))
        # UTF-16 and Latin-1 member bytes (BOM / encoding declaration are
        # honoured by the existing decoder; the XML copy is re-encoded).
        utf16 = ('<?xml version="1.0" encoding="UTF-16"?>'
                 '<html><body><script><![CDATA[var t="</script>";]]>'
                 '</script><p>Middle caf\u00e9 chapter.</p>'
                 '</body></html>').encode('utf-16')
        self.assertEqual(
            'First readable chapter.\n\nMiddle caf\u00e9 chapter.\n\n'
            'Final readable chapter.', extract(make_epub(utf16)))
        latin1 = ('<?xml version="1.0" encoding="ISO-8859-1"?>'
                  '<html><body><script><![CDATA[var t="</script>";]]>'
                  '</script><p>Middle caf\u00e9 chapter.</p>'
                  '</body></html>').encode('iso-8859-1')
        self.assertEqual(
            'First readable chapter.\n\nMiddle caf\u00e9 chapter.\n\n'
            'Final readable chapter.', extract(make_epub(latin1)))

    def test_selfclosing_ignored_span_stops_at_its_own_tag_end(self):
        # Direct helper property: the removed span for a self-closing
        # ignored element ends after its own '/>', never at a following
        # parent end tag - even when that end tag is the very next markup.
        cases = [
            ('<html><body><div><script/></div>'
             '<p>Middle readable chapter.</p></body></html>',
             '<html><body><div></div>'
             '<p>Middle readable chapter.</p></body></html>'),
            ('<html><body><div><script/> </div>'
             '<p>Middle readable chapter.</p></body></html>',
             '<html><body><div> </div>'
             '<p>Middle readable chapter.</p></body></html>'),
            ('<html><body><div><script/><!-- note --></div>'
             '<p>Middle readable chapter.</p></body></html>',
             '<html><body><div><!-- note --></div>'
             '<p>Middle readable chapter.</p></body></html>'),
            # Paired outer ignored element whose last child self-closes:
            # the outer span is still the whole subtree, nothing more.
            ('<html><body><script><style/></script>'
             '<p>Middle readable chapter.</p></body></html>',
             '<html><body><p>Middle readable chapter.</p></body></html>'),
            ('<html><body><script/><p>Middle readable chapter.</p>'
             '</body></html>',
             '<html><body><p>Middle readable chapter.</p></body></html>'),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                ET.fromstring(source.encode('utf-8'))
                self.assertEqual(
                    expected, script._omit_xml_ignored_subtrees(source))
        # The serious form: deleting a following </textarea> would leave
        # HTMLParser's raw mode open and swallow the whole chapter.
        source = ('<html><body><textarea>Preface<script/></textarea>'
                  '<p>Middle readable chapter.</p></body></html>')
        ET.fromstring(source.encode('utf-8'))
        self.assertEqual(
            '<html><body><textarea>Preface</textarea>'
            '<p>Middle readable chapter.</p></body></html>',
            script._omit_xml_ignored_subtrees(source))
        # <textarea> is not a skipped element, so its text is ordinary
        # narration; the regression was losing the whole following
        # chapter, not exposing "Preface".
        self.assertEqual(
            'First readable chapter.\n\nPreface\n\nMiddle readable chapter.\n\n'
            'Final readable chapter.', extract(make_epub(source)))

    def test_case_insensitive_ignored_tags_in_cdata_are_omitted_exactly(self):
        # The HTML extractor casefolds skip names, so the XML path must
        # too: uppercase/mixed-case ignored elements with literal closes
        # and opens in CDATA are omitted exactly, not falsely rejected.
        expected = ('First readable chapter.\n\nMiddle readable chapter.\n\n'
                    'Final readable chapter.')
        for tag in ('SCRIPT', 'STYLE', 'TITLE', 'ScRiPt', 'sTyLe'):
            with self.subTest(tag=tag):
                body = (f'<html><body><{tag}>'
                        f'<![CDATA[x="</{tag}><style>";]]></{tag}>'
                        '<p>Middle readable chapter.</p></body></html>')
                ET.fromstring(body.encode('utf-8'))
                self.assertEqual(expected, extract(make_epub(body)))
        # A mixed-case *prefixed* name is still not a skipped element
        # (exact-tag semantics), so its readable text is retained.
        body = ('<html xmlns:x="urn:x"><body><x:ScRiPt>'
                'Middle readable words</x:ScRiPt>'
                '<p>Middle readable chapter.</p></body></html>')
        ET.fromstring(body.encode('utf-8'))
        text = extract(make_epub(body))
        self.assertIn('Middle readable words', text)
        self.assertIn('Middle readable chapter.', text)
        self.assertIn('First readable chapter.', text)
        self.assertIn('Final readable chapter.', text)

    def test_foreign_prefixed_elements_retain_readable_text(self):
        # Prefixed names in a foreign vocabulary are ordinary elements
        # under the existing exact-tag semantics: their readable text is
        # narration and must be preserved, never silently omitted.
        expected = ('First readable chapter.\n\nMiddle readable chapter.\n\n'
                    'Final readable chapter.')
        for tag in ('script', 'style', 'title'):
            for uri in ('urn:readable-vocabulary', 'http://example.org/ns'):
                with self.subTest(tag=tag, uri=uri):
                    body = (f'<html xmlns:x="{uri}"><body>'
                            f'<x:{tag}>Middle readable chapter.</x:{tag}>'
                            '</body></html>')
                    ET.fromstring(body.encode('utf-8'))
                    self.assertEqual(expected, extract(make_epub(body)))
        for tag in ('ScRiPt', 'sTyLe', 'TiTlE'):
            with self.subTest(tag=tag):
                body = ('<html xmlns:x="urn:readable-vocabulary"><body>'
                        f'<x:{tag}>Middle readable chapter.</x:{tag}>'
                        '</body></html>')
                ET.fromstring(body.encode('utf-8'))
                self.assertEqual(expected, extract(make_epub(body)))
        # A name that merely starts with a skip name is likewise ordinary.
        body = ('<html><body><scriptx>Middle readable chapter.</scriptx>'
                '</body></html>')
        ET.fromstring(body.encode('utf-8'))
        self.assertEqual(expected, extract(make_epub(body)))

    def test_metadata_literals_do_not_become_phantom_skips(self):
        # Valid XML PIs and DOCTYPE internals outside ignored elements are
        # nonspoken metadata; their literal skip markup must not reach
        # HTMLParser as phantom start tags. ET controls each body.
        expected = ('First readable chapter.\n\nMiddle readable chapter.\n\n'
                    'Final readable chapter.')
        cases = [
            ('pi_outside_later_style',
             '<html><body><?note </script><style> ?>'
             '<p>Middle readable chapter.</p><style>p{}</style>'
             '</body></html>'),
            ('pi_outside_later_script',
             '<html><body><?note </script><style> ?>'
             '<p>Middle readable chapter.</p><script>code</script>'
             '</body></html>'),
            ('dtd_comment',
             '<!DOCTYPE html [<!-- </script><style> -->]>'
             '<html><body><p>Middle readable chapter.</p>'
             '<style>p{}</style></body></html>'),
            ('dtd_entity',
             '<!DOCTYPE html [<!ENTITY x "</style><style>">]>'
             '<html><body><script>code</script>'
             '<p>Middle readable chapter.</p></body></html>'),
            ('external_public_doctype',
             '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0//EN" '
             '"http://example.org/x.dtd">'
             '<html><body><p>Middle readable chapter.</p>'
             '<script>code</script></body></html>'),
            ('combined_metadata',
             '<!DOCTYPE html [<!ENTITY x "</style><style>">]>'
             '<html><body><?note </script> ?>'
             '<script><![CDATA[var t="</script>";]]></script>'
             '<p>Middle readable chapter.</p></body></html>'),
            ('whitespace_opening',
             '<html><body><script >code</script>'
             '<p>Middle readable chapter.</p></body></html>'),
            ('newline_opening',
             '<html><body><script\n>code</script>'
             '<p>Middle readable chapter.</p></body></html>'),
        ]
        for name, body in cases:
            with self.subTest(case=name):
                ET.fromstring(body.encode('utf-8'))
                self.assertEqual(expected, extract(make_epub(body)))
        # CRLF outside the metadata is preserved (metamorphic equality).
        crlf_base = ('<html><body>\r\n<p>Middle readable chapter.</p>'
                     '\r\n</body></html>')
        crlf_variant = ('<html><body>\r\n<?note </script><style> ?>\r\n'
                        '<p>Middle readable chapter.</p>\r\n</body></html>')
        ET.fromstring(crlf_variant.encode('utf-8'))
        self.assertEqual(extract(make_epub(crlf_base)),
                         extract(make_epub(crlf_variant)))
        # An XML failure after metadata discards the candidate: the helper
        # returns the original lexical string unchanged.
        broken = '<?note </script><style> ?><html><body><p>Middle tolerant'
        self.assertEqual(broken, script._omit_xml_ignored_subtrees(broken))

    def test_declared_codec_surrogate_fails_closed_with_actionable_error(self):
        # The existing decoder accepts arbitrary declared Python codecs.
        # 'unicode_escape' turns the literal ASCII sequence \ud800 into a
        # lone surrogate in the decoded string, which cannot be encoded
        # as UTF-8 for the XML pass. That must fail closed with an
        # actionable error, not silently fall back to the raw-HTML path
        # that drops the middle chapter.
        payload = (b'<?xml version="1.0" encoding="unicode_escape"?>'
                   b'<html><body><script><![CDATA[var t = "\\ud800"; '
                   b'var u = "</script><style>";]]></script>'
                   b'<p>Middle readable chapter.</p>'
                   b'<style>p{}</style></body></html>')
        decoded = script.get_epub_xhtml_text(payload)
        # Setup premise: the actual production decoder yields a surrogate.
        self.assertTrue(any(0xD800 <= ord(char) <= 0xDFFF for char in decoded))
        raw = make_epub(payload)
        for report in (False, True):
            with self.subTest(report=report):
                with self.assertRaisesRegex(ValueError, r'spine index 1') as caught:
                    extract(raw, report=report)
                message = str(caught.exception)
                self.assertIn('invalid Unicode', message)
                self.assertIn('middle.xhtml', message)
                self.assertNotIn('Middle readable chapter', message)
                self.assertNotIn('\\ud800', message)
        # Control: the same declared codec with an ordinary payload (no
        # surrogate escape) still extracts exactly - the codec itself is
        # not blanket-rejected.
        control = (b'<?xml version="1.0" encoding="unicode_escape"?>'
                   b'<html><body><script><![CDATA[var t = "plain"; '
                   b'var u = "</script><style>";]]></script>'
                   b'<p>Middle readable chapter.</p>'
                   b'<style>p{}</style></body></html>')
        self.assertEqual(
            'First readable chapter.\n\nMiddle readable chapter.\n\n'
            'Final readable chapter.', extract(make_epub(control)))

    def test_xml_entity_and_doctype_are_not_expanded_or_fetched(self):
        # Internal entities must not be expanded into narration. The
        # external-DTD case below proves only that such a document is not
        # rejected and extracts normally: no fetch is possible by
        # construction (the helper registers DefaultHandler only, with no
        # external-entity handler), and a missing-path SYSTEM identifier
        # alone would not prove the absence of a fetch attempt by some
        # future loader that swallowed the error - so no stronger runtime
        # claim is made here.
        internal = ('<!DOCTYPE html [<!ENTITY sentinel '
                    '"EXPANDED_SENTINEL_1795">]>'
                    '<html><body><p>Middle &sentinel; chapter.</p>'
                    '<script>code</script></body></html>')
        text = extract(make_epub(internal))
        self.assertIn('Middle', text)
        self.assertNotIn('EXPANDED_SENTINEL_1795', text)
        external = ('<!DOCTYPE html SYSTEM '
                    '"file:///nonexistent-1795-missing.dtd">'
                    '<html><body><p>Middle readable chapter.</p>'
                    '<script>code</script></body></html>')
        self.assertIn('Middle readable chapter.', extract(make_epub(external)))

    def test_malformed_unclosed_skip_still_fail_closed(self):
        # XML-invalid for other reasons too: the tolerant HTML fallback
        # still rejects a genuinely unclosed skipped element (this is the
        # original scope, not arbitrary XML strictness - an unclosed
        # paragraph alone remains accepted elsewhere).
        body = '<html><body><div><p>Middle words.<script>never closed'
        with self.assertRaisesRegex(ValueError, r'middle\.xhtml') as caught:
            extract(make_epub(body))
        self.assertIn('unclosed <script>', str(caught.exception))
        with self.assertRaisesRegex(ValueError, r'middle\.xhtml'):
            extract(make_epub(body), report=True)

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
        # An XML failure *after* a completed ignored subtree must discard
        # the whole candidate: the helper returns the original lexical
        # string unchanged (never a partial omission), and the production
        # fallback still preserves the readable paragraph text.
        broken = ('<html><body><script>ignored</script>'
                  '<p>Middle tolerant words &amp; 10')
        self.assertEqual(broken, script._omit_xml_ignored_subtrees(broken))
        text = extract(make_epub(broken))
        self.assertIn('Middle tolerant words & 10', text)
        self.assertIn('First readable chapter.', text)
        self.assertIn('Final readable chapter.', text)

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
        # the discarded V1 close()-based attempt made exactly one, on the whole buffer.
        payload = '&amp;' * 200
        body = '<html><body><title>' + payload
        raw = make_epub(body)
        original_unescape = html.parser.unescape
        payload_calls = []

        def fail_only_on_skipped_payload(value):
            if value == payload:
                payload_calls.append(value)
                raise MemoryError(
                    'simulated allocation failure decoding skipped title payload')
            return original_unescape(value)

        with patch('html.parser.unescape', side_effect=fail_only_on_skipped_payload):
            # Armed positive control: the real stdlib parser, fed the same
            # unclosed title payload, must trip the injection on close().
            # This proves the patch is live on this interpreter; if a future
            # stdlib changes that assumption, this control fails loudly
            # instead of the negative test going silently inert.
            control = html.parser.HTMLParser()
            control.feed('<title>' + payload)
            with self.assertRaises(MemoryError):
                control.close()
            self.assertEqual(1, len(payload_calls))
            # Production extraction of the same payload must reject without
            # any further payload decode call.
            with self.assertRaisesRegex(ValueError, r'middle\.xhtml') as caught:
                extract(raw)
            self.assertEqual(1, len(payload_calls))
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

    def test_error_reason_and_spine_index_precede_long_resource_name(self):
        # A very long internal member name must not push the actionable
        # reason out of the diagnostics-truncated (2000 char) detail: the
        # static tag/reason and bounded spine index come before the
        # user-controlled path in the error message.
        long_name = 'x' * 3000 + '.xhtml'
        self.assertEqual(3006, len(long_name))
        raw = make_epub(
            '<html><head><title>Skipped header</head><body>'
            '<p>Missing middle chapter.</p></body></html>',
            middle_name=long_name)
        with self.assertRaisesRegex(ValueError, r'spine index 1') as caught:
            extract(raw)
        message = str(caught.exception)
        self.assertIn('unclosed <title>', message)
        self.assertIn(long_name, message)
        self.assertLess(message.index('spine index 1'),
                        message.index(long_name))
        self.assertLess(message.index('unclosed <title>'),
                        message.index(long_name))
        self.assertNotIn('Missing middle chapter', message)
        with self.assertRaisesRegex(ValueError, r'spine index 1'):
            extract(raw, report=True)


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

    async def test_wellformed_cdata_script_upload_persists(self):
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                '<script><![CDATA[var end="</script>";]]></script>'
                '<p>Middle readable chapter.</p></body></html>')
        ET.fromstring(body.encode('utf-8'))  # well-formedness control
        raw = make_epub(body)
        async with uploads.UploadPublicationSourceTests().environment() as (
                root, directory, client):
            response = await client.post(
                '/api/upload?select_active=false',
                files={'file': ('book.epub', raw, 'application/epub+zip')})
            self.assertEqual(200, response.status_code, response.text)
            names = sorted(item.name for item in directory.iterdir())
            self.assertIn('book.txt', names)
            self.assertIn('book.source.epub', names)
            persisted = (directory / 'book.txt').read_bytes()
            self.assertIn(b'First readable chapter.', persisted)
            self.assertIn(b'Middle readable chapter.', persisted)
            self.assertIn(b'Final readable chapter.', persisted)
            self.assertEqual(raw, (directory / 'book.source.epub').read_bytes())

    async def test_root_cdata_false_rejection_and_silent_loss_upload_persist(self):
        # The two production-verified XML cases: a literal close+open inside
        # script CDATA must neither falsely reject (no later genuine close
        # of the phantom) nor silently drop the middle chapter (a later
        # genuine <style> would otherwise close the phantom).
        cases = [
            ('false_rejection',
             '<html><body><script><![CDATA[var t = "</script><style>";]]>'
             '</script><p>Middle readable chapter.</p></body></html>'),
            ('silent_loss',
             '<html><body><script><![CDATA[var t = "</script><style>";]]>'
             '</script><p>Middle readable chapter.</p>'
             '<style>p{color:red}</style></body></html>'),
        ]
        for name, body in cases:
            with self.subTest(case=name):
                ET.fromstring(body.encode('utf-8'))  # well-formedness control
                raw = make_epub(body)
                async with uploads.UploadPublicationSourceTests().environment() as (
                        root, directory, client):
                    # Default mode: persists, middle chapter present.
                    response = await client.post(
                        '/api/upload?select_active=false',
                        files={'file': ('book.epub', raw,
                                         'application/epub+zip')})
                    self.assertEqual(200, response.status_code, response.text)
                    persisted = (directory / 'book.txt').read_bytes()
                    self.assertIn(b'First readable chapter.', persisted)
                    self.assertIn(b'Middle readable chapter.', persisted)
                    self.assertIn(b'Final readable chapter.', persisted)
                    self.assertEqual(raw,
                                     (directory / 'book.source.epub').read_bytes())
                    before = {item.name: item.read_bytes()
                              for item in directory.iterdir()}
                    # Report mode: same persisted body/source, reused.
                    reported = await client.post(
                        '/api/upload?select_active=false'
                        '&include_extraction_report=true',
                        files={'file': ('book.epub', raw,
                                         'application/epub+zip')})
                    self.assertEqual(200, reported.status_code, reported.text)
                    self.assertTrue(reported.json()['reused'])
                    self.assertIn('extraction_report', reported.json())
                    self.assertEqual(
                        before,
                        {item.name: item.read_bytes()
                         for item in directory.iterdir()})
                    # Healthy selection: selecting the book records it.
                    selected = await client.post(
                        '/api/upload?select_active=true',
                        files={'file': ('book.epub', raw,
                                         'application/epub+zip')})
                    self.assertEqual(200, selected.status_code, selected.text)
                    state = (root / 'state.json').read_bytes()
                    self.assertIn(b'book.txt', state)
                    self.assertEqual(
                        before,
                        {item.name: item.read_bytes()
                         for item in directory.iterdir()})

    async def test_foreign_prefixed_upload_persists_middle(self):
        body = ('<html xmlns:x="urn:readable-vocabulary"><body>'
                '<x:script>Middle readable chapter.</x:script>'
                '</body></html>')
        ET.fromstring(body.encode('utf-8'))  # well-formedness control
        raw = make_epub(body)
        async with uploads.UploadPublicationSourceTests().environment() as (
                root, directory, client):
            response = await client.post(
                '/api/upload?select_active=false',
                files={'file': ('book.epub', raw, 'application/epub+zip')})
            self.assertEqual(200, response.status_code, response.text)
            persisted = (directory / 'book.txt').read_bytes()
            self.assertIn(b'First readable chapter.', persisted)
            self.assertIn(b'Middle readable chapter.', persisted)
            self.assertIn(b'Final readable chapter.', persisted)
            self.assertEqual(raw, (directory / 'book.source.epub').read_bytes())
            before = {item.name: item.read_bytes()
                      for item in directory.iterdir()}
            reported = await client.post(
                '/api/upload?select_active=false'
                '&include_extraction_report=true',
                files={'file': ('book.epub', raw, 'application/epub+zip')})
            self.assertEqual(200, reported.status_code, reported.text)
            self.assertTrue(reported.json()['reused'])
            self.assertIn('extraction_report', reported.json())
            self.assertEqual(
                before,
                {item.name: item.read_bytes() for item in directory.iterdir()})
            selected = await client.post(
                '/api/upload?select_active=true',
                files={'file': ('book.epub', raw, 'application/epub+zip')})
            self.assertEqual(200, selected.status_code, selected.text)
            self.assertIn(b'book.txt', (root / 'state.json').read_bytes())

    async def test_declared_codec_surrogate_upload_is_safe_refusal(self):
        payload = (b'<?xml version="1.0" encoding="unicode_escape"?>'
                   b'<html><body><script><![CDATA[var t = "\\ud800"; '
                   b'var u = "</script><style>";]]></script>'
                   b'<p>Middle readable chapter.</p>'
                   b'<style>p{}</style></body></html>')
        bad = make_epub(payload)
        good = make_epub(GOOD_MIDDLE)
        async with uploads.UploadPublicationSourceTests().environment() as (
                root, directory, client):
            first = await client.post(
                '/api/upload?select_active=true',
                files={'file': ('book.epub', good, 'application/epub+zip')})
            self.assertEqual(200, first.status_code, first.text)
            before = {item.name: item.read_bytes() for item in directory.iterdir()}
            state_before = (root / 'state.json').read_bytes()
            for name, params in (
                    ('default', {}),
                    ('report', {'include_extraction_report': 'true',
                                'select_active': 'false'})):
                with self.subTest(mode=name):
                    query = '&'.join(f'{key}={value}' for key, value in params.items())
                    url = '/api/upload' + (f'?{query}' if query else '')
                    response = await client.post(
                        url, files={'file': ('book.epub', bad,
                                             'application/epub+zip')})
                    self.assertEqual(400, response.status_code, response.text)
                    detail = response.json()['detail']
                    self.assertIn('invalid Unicode', detail)
                    self.assertIn('spine index 1', detail)
                    self.assertIn('middle.xhtml', detail)
                    self.assertNotIn('Middle readable chapter', detail)
                    self.assertEqual(
                        before,
                        {item.name: item.read_bytes()
                         for item in directory.iterdir()})
                    self.assertEqual(state_before,
                                     (root / 'state.json').read_bytes())

    async def test_long_member_name_error_stays_actionable_and_safe(self):
        long_name = 'x' * 3000 + '.xhtml'
        bad = make_epub(
            '<html><head><title>Skipped header</head><body>'
            '<p>Missing middle chapter.</p></body></html>',
            middle_name=long_name)
        good = make_epub(GOOD_MIDDLE)
        async with uploads.UploadPublicationSourceTests().environment() as (
                root, directory, client):
            first = await client.post(
                '/api/upload?select_active=true',
                files={'file': ('book.epub', good, 'application/epub+zip')})
            self.assertEqual(200, first.status_code, first.text)
            before = {item.name: item.read_bytes() for item in directory.iterdir()}
            state_before = (root / 'state.json').read_bytes()
            for name, params in (
                    ('default', {}),
                    ('report', {'include_extraction_report': 'true',
                                'select_active': 'false'})):
                with self.subTest(mode=name):
                    query = '&'.join(f'{key}={value}' for key, value in params.items())
                    url = '/api/upload' + (f'?{query}' if query else '')
                    response = await client.post(
                        url, files={'file': ('book.epub', bad,
                                             'application/epub+zip')})
                    self.assertEqual(400, response.status_code, response.text)
                    detail = response.json()['detail']
                    # The reason survives diagnostics truncation (2000 chars)
                    # because it precedes the 3006-char resource name.
                    self.assertIn('unclosed', detail)
                    self.assertIn('title', detail)
                    self.assertIn('spine index 1', detail)
                    self.assertNotIn('Missing middle chapter', detail)
                    self.assertEqual(
                        before,
                        {item.name: item.read_bytes()
                         for item in directory.iterdir()})
                    self.assertEqual(state_before,
                                     (root / 'state.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
