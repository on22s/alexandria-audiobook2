"""Inline ID preservation (#1844 P7) — must fail against initial code.

Reproduces the parent full-app failure: source <em id="lantern">lost its
id through sentence-span reconstruction. These tests assert original
inline ids survive in the produced artifact, exactly once.
"""
import io
import unittest
import zipfile
import xml.etree.ElementTree as ET

from epub_readaloud import build_epub_readaloud, validate_epub_readaloud
from tests.test_epub_readaloud_sentences import _source_epub

XHTML = "{http://www.w3.org/1999/xhtml}"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


def _tokens(*words, start=0.0):
    return [{"text": w, "start_time": start + i * 0.5,
             "end_time": start + (i + 1) * 0.5} for i, w in enumerate(words)]


def _all_ids(document):
    """Map value -> number of elements carrying it in any id attr.

    A value with id+xml:id on ONE element counts once.
    """
    carriers = {}
    for element in document.iter():
        values = {v for v in (element.get("id"), element.get(XML_ID)) if v}
        for value in values:
            carriers[value] = carriers.get(value, 0) + 1
    return carriers


class InlineIdTests(unittest.TestCase):
    def _build(self, body, entries, aligned):
        source = _source_epub(body)
        return build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=entries,
            aligned_rows=aligned)

    def test_inline_id_in_single_sentence_in_multi_sentence_block(self):
        # 'lantern' sits in the middle sentence of a 3-sentence block.
        body = ('<p>First line here. '
                'The <em id="lantern">lantern</em> glows softly. '
                'Last line here.</p>')
        entries = [{"index": 0,
                    "prepared_text": "First line here. The lantern glows softly. Last line here.",
                    "start_frame": 0, "end_frame": 120000, "sample_rate": 24000}]
        aligned = [{"tokens": _tokens(
            "First", "line", "here", "The", "lantern", "glows", "softly",
            "Last", "line", "here")}]
        result, _ = self._build(body, entries, aligned)
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            document = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
        ids = _all_ids(document)
        self.assertIn("lantern", ids)
        self.assertEqual(1, ids["lantern"])
        self.assertEqual("lantern", "".join(
            "".join(e.itertext()) for e in document.iter(XHTML + 'em') if e.get("id") == "lantern"))

    def test_inline_element_straddling_two_sentences_keeps_id_once(self):
        # One <a> spans the sentence boundary; its id must survive once.
        body = ('<p>First line. <a id="xref" href="#end">Continued '
                'second sentence</a> tail here.</p>'
                '<p id="end">Final words here.</p>')
        entries = [{"index": 0,
                    "prepared_text": "First line. Continued second sentence tail here.",
                    "start_frame": 0, "end_frame": 120000, "sample_rate": 24000},
                   {"index": 1,
                    "prepared_text": "Final words here.",
                    "start_frame": 120000, "end_frame": 156000, "sample_rate": 24000}]
        aligned = [{"tokens": _tokens(
            "First", "line", "Continued", "second", "sentence", "tail", "here")},
                   {"tokens": _tokens("Final", "words", "here")}]
        result, _ = self._build(body, entries, aligned)
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            document = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
        ids = _all_ids(document)
        self.assertEqual(1, ids["xref"])
        self.assertEqual(1, ids["end"])

    def test_xml_id_preserved_together_with_id(self):
        body = ('<p>First line. '
                '<em id="both" xml:id="both">Lantern kind</em> second.</p>')
        entries = [{"index": 0,
                    "prepared_text": "First line. Lantern kind second.",
                    "start_frame": 0, "end_frame": 96000, "sample_rate": 24000}]
        aligned = [{"tokens": _tokens("First", "line", "Lantern", "kind", "second")}]
        result, _ = self._build(body, entries, aligned)
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            document = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
        ids = _all_ids(document)
        self.assertEqual(1, ids["both"])
        em = [e for e in document.iter(XHTML + 'em')]
        self.assertTrue(any(e.get("id") == "both" and e.get(XML_ID) == "both" for e in em))

    def test_minted_span_id_does_not_collide_with_existing_xml_id(self):
        # Source already owns aa2-p-0001 via xml:id; minted spans must skip it.
        body = ('<p xml:id="aa2-p-0001">First line. '
                '<span id="aa2-p-0002">Nested two</span> second.</p>')
        entries = [{"index": 0,
                    "prepared_text": "First line. Nested two second.",
                    "start_frame": 0, "end_frame": 72000, "sample_rate": 24000}]
        aligned = [{"tokens": _tokens("First", "line", "Nested", "two", "second")}]
        result, _ = self._build(body, entries, aligned)
        validate_epub_readaloud(result)  # validator rejects duplicate ids
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            document = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
            smil = ET.fromstring(archive.read("OEBPS/ch1.smil"))
        ids = _all_ids(document)
        self.assertEqual(1, ids["aa2-p-0001"])
        smil_ns = "{http://www.w3.org/ns/SMIL}"
        fragments = [p.find(smil_ns + 'text').get('src').split('#', 1)[1]
                     for p in smil.findall(".//" + smil_ns + "par")]
        for fragment in fragments:
            self.assertEqual(1, ids[fragment])

    def test_nav_href_into_old_inline_id_still_resolves(self):
        # Document links to a pre-existing inline id; after any
        # redistribution, the fragment must still resolve in the output.
        body = ('<p id="source">First line. '
                '<em id="lantern">Lantern kind</em> second.</p>')
        entries = [{"index": 0,
                    "prepared_text": "First line. Lantern kind second.",
                    "start_frame": 0, "end_frame": 60000, "sample_rate": 24000}]
        aligned = [{"tokens": _tokens("First", "line", "Lantern", "kind", "second")}]
        source = _source_epub(body)
        result, _ = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=entries,
            aligned_rows=aligned)
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            document = ET.fromstring(archive.read("OEBPS/ch1.xhtml"))
        ids = _all_ids(document)
        for wanted in ("source", "lantern"):
            self.assertIn(wanted, ids)
            self.assertEqual(1, ids[wanted])


if __name__ == "__main__":
    unittest.main()
