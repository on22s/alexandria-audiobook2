"""EPUB sentence-level spans (#1844) — fail-first, synthetic EPUB only.

Sentence fragments land inside the mapped element; the packager's
source preservation, URI rules, rebuild behavior and validator are the
production ones under test (via build_epub_readaloud with aligned_rows).
"""
import builtins
import io
import zipfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import epub_readaloud
from epub_readaloud import (build_epub_readaloud, validate_epub_readaloud)

XHTML = "{http://www.w3.org/1999/xhtml}"
SMIL = "{http://www.w3.org/ns/SMIL}"


def _source_epub(chapter_body):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml',
                         '<?xml version="1.0"?>'
                         '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        archive.writestr('OEBPS/content.opf',
                         '<?xml version="1.0" encoding="UTF-8"?>'
                         '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                         '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         '<dc:identifier id="id">urn:test:1</dc:identifier>'
                         '<dc:title>Synthetic Title</dc:title><dc:creator>Test Author</dc:creator>'
                         '<dc:language>en</dc:language></metadata>'
                         '<manifest>'
                         '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                         '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                         '<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
                         '</manifest>'
                         '<spine><itemref idref="c1"/></spine></package>')
        archive.writestr('OEBPS/nav.xhtml',
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
                         'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body>'
                         '<nav epub:type="toc"><ol>'
                         '<li><a href="ch1.xhtml">Chapter One</a></li>'
                         '</ol></nav></body></html>')
        archive.writestr('OEBPS/ch1.xhtml',
                         ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                          '<head><title>Chapter One</title></head><body>'
                          + chapter_body + '</body></html>'))
        archive.writestr('OEBPS/cover.jpg', b'\xff\xd8\xff\xe0\xff\xd9')
    return buffer.getvalue()


def _entries():
    return [{"index": 0, "prepared_text": "Hello synthetic world. Second sentence here.",
             "start_frame": 0, "end_frame": 48000, "sample_rate": 24000}]


def _aligned_tokens():
    words = ["Hello", "synthetic", "world", "Second", "sentence", "here"]
    return [{"text": w, "start_time": i * 0.5, "end_time": (i + 1) * 0.5}
            for i, w in enumerate(words)]


class SentenceSpanTests(unittest.TestCase):
    def test_spoken_range_membership_work_is_linear(self):
        # Count semantic integer comparisons, not elapsed time. Each pass
        # receives fresh equal-valued indexes, so a list membership scan
        # exposes its quadratic comparisons while a set stays linear.
        for count in (64, 128):
            with self.subTest(sentences=count):
                comparisons = [0]

                class CountedIndex(int):
                    __hash__ = int.__hash__

                    def __eq__(self, other):
                        comparisons[0] += 1
                        return int.__eq__(self, other)

                def counted_enumerate(values, start=0):
                    for index, value in builtins.enumerate(values, start):
                        yield CountedIndex(index), value

                text = "Echo\u3002" * count
                element = ET.fromstring('<p xmlns="%s">%s</p>' %
                                        (XHTML[1:-1], text))
                entry = {"prepared_text": text, "start_frame": 0,
                         "end_frame": count * 10, "sample_rate": 1000}
                tokens = [{"text": "Echo", "start_time": index / 100,
                           "end_time": (index + 1) / 100}
                          for index in range(count)]
                row = {"segment_index": 0, "chunk_indexes": [0],
                       "start_frame": 0, "end_frame": count * 10}
                identifiers = iter("sentence-%d" % index
                                   for index in range(count))
                with patch.object(epub_readaloud, "enumerate",
                                  counted_enumerate, create=True):
                    rows, _charged_bytes = epub_readaloud._sentence_rows_for_segment(
                        {"element": element, "text": text}, row, [entry],
                        [{"tokens": tokens}], 1000,
                        lambda: next(identifiers), set())
                self.assertEqual(count, len(rows))
                self.assertEqual(text, "".join(element.itertext()))
                self.assertEqual([(index * 10, (index + 1) * 10)
                                  for index in range(count)],
                                 [(item["start_frame"], item["end_frame"])
                                  for item in rows])
                self.assertLessEqual(comparisons[0], 4 * count)

    def test_sentence_pars_inside_mapped_element(self):
        source = _source_epub('<h1>Chapter One</h1>'
                              '<p id="keep-me">Hello synthetic world. Second sentence here.</p>')
        entries = [{"index": 0, "prepared_text": "Chapter One",
                    "start_frame": 0, "end_frame": 24000, "sample_rate": 24000},
                   *(_entries())]
        entries[1] = dict(entries[1], start_frame=24000, end_frame=96000)
        heading_tokens = [{"text": "Chapter", "start_time": 0.0, "end_time": 0.5},
                          {"text": "One", "start_time": 0.5, "end_time": 1.0}]
        aligned = [{"tokens": heading_tokens}, {"tokens": _aligned_tokens()}]
        result, report = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3', audio_filename='audiobook.mp3',
            script_entries=entries, aligned_rows=aligned)
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            document = ET.fromstring(archive.read('OEBPS/ch1.xhtml'))
            smil = ET.fromstring(archive.read('OEBPS/ch1.smil'))
            other = archive.read('OEBPS/cover.jpg')
        self.assertEqual(b'\xff\xd8\xff\xe0\xff\xd9', other)  # untouched bytes
        paragraph = document.find('.//' + XHTML + 'p')
        self.assertEqual('keep-me', paragraph.get('id'))  # original id kept
        spans = paragraph.findall(XHTML + 'span')
        self.assertEqual(2, len(spans))
        self.assertEqual("Hello synthetic world.", "".join(spans[0].itertext()))
        self.assertEqual(" Second sentence here.", "".join(spans[1].itertext()))
        pars = smil.findall('.//' + SMIL + 'par')
        # One par per sentence in the aligned paragraph plus the plain heading.
        self.assertGreaterEqual(len(pars), 3)
        fragments = [par.find(SMIL + 'text').get('src').split('#', 1)[1] for par in pars]
        self.assertIn(spans[0].get('id'), fragments)
        self.assertIn(spans[1].get('id'), fragments)

    def test_all_spoken_text_exactly_once_with_inline_markup(self):
        source = _source_epub(
            '<p id="lead">Hello <em>synthetic</em> world. '
            '<a href="#x">Second</a> sentence here.</p>')
        entries = [{"index": 0,
                    "prepared_text": "Hello synthetic world. Second sentence here.",
                    "start_frame": 0, "end_frame": 72000, "sample_rate": 24000}]
        aligned = [{"tokens": _aligned_tokens()}]
        result, _report = build_epub_readaloud(
            retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3', audio_filename='audiobook.mp3',
            script_entries=entries, aligned_rows=aligned)
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            document = ET.fromstring(archive.read('OEBPS/ch1.xhtml'))
        paragraph = document.find('.//' + XHTML + 'p')
        self.assertEqual("Hello synthetic world. Second sentence here.",
                         "".join(paragraph.itertext()))
        self.assertEqual(["a", "em"], sorted(el.tag.rsplit("}", 1)[-1] for el in paragraph.iter()
                                              if el.tag in (XHTML + 'em', XHTML + 'a')))

    def test_missing_alignment_fails_closed(self):
        source = _source_epub('<p>Hello synthetic world. Second sentence here.</p>')
        with self.assertRaises(ValueError):
            build_epub_readaloud(retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3',
                                 audio_filename='audiobook.mp3',
                                 script_entries=_entries(), aligned_rows=[None])

    def test_mismatched_alignment_fails_closed(self):
        source = _source_epub('<p>Hello synthetic world. Second sentence here.</p>')
        aligned = [{"tokens": [{"text": "Fabricated", "start_time": 0.0,
                                 "end_time": 0.5}]}]
        with self.assertRaises(ValueError):
            build_epub_readaloud(retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3',
                                 audio_filename='audiobook.mp3',
                                 script_entries=_entries(), aligned_rows=aligned)

    def test_coarsened_container_is_explicitly_refused(self):
        # Direct text + block children coarsen to one segment; the sentence
        # wrapper cannot faithfully redistribute nested blocks and refuses.
        source = _source_epub('<section>Lead in text. <p>Inner paragraph sentence.</p></section>')
        entries = [{"index": 0,
                    "prepared_text": "Lead in text. Inner paragraph sentence.",
                    "start_frame": 0, "end_frame": 48000, "sample_rate": 24000}]
        aligned = [{"tokens": _aligned_tokens()}]
        with self.assertRaises(ValueError) as caught:
            build_epub_readaloud(retained_epub_bytes=source, audio_bytes=b'\xff\xfbmp3',
                                 audio_filename='audiobook.mp3',
                                 script_entries=entries, aligned_rows=aligned)
        self.assertIn("unsupported", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
