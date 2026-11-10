"""One script chunk spanning several EPUB blocks (#1844), synthetic EPUB only.

A heading and the paragraph after it often share one audio chunk. CPU
(chunk-timed) mode highlights them together as one wrapper block for the
chunk's real frames; Qwen mode times each block from aligned tokens. No
frame inside a chunk is invented, and mismatches still refuse.
"""
import io
import zipfile
import unittest
import xml.etree.ElementTree as ET

from epub_readaloud import (build_epub_readaloud, map_spine_to_chunks,
                            validate_epub_readaloud)
from tests.test_epub_readaloud_sentences import SMIL, XHTML, _source_epub

BODY = '<h2>Chapter 3: Mana Evaluation (2)</h2><p>The test began at dawn.</p>'
SPANNING = [{"index": 0,
             "prepared_text": "Chapter 3: Mana Evaluation (2). The test began at dawn.",
             "start_frame": 0, "end_frame": 96000, "sample_rate": 24000}]


def _spine(*texts):
    return [{"fragment": None, "text": text} for text in texts]


def _par_clips(smil):
    return [(par.find(SMIL + "text").get("src"),
             par.find(SMIL + "audio").get("clipBegin"),
             par.find(SMIL + "audio").get("clipEnd"))
            for par in smil.iter(SMIL + "par")]


class SpanMappingTests(unittest.TestCase):
    def test_span_still_refused_without_opt_in(self):
        with self.assertRaisesRegex(ValueError, "spans multiple spine segments"):
            map_spine_to_chunks(_spine("Chapter 3: Mana Evaluation (2)", "The test began at dawn."),
                                SPANNING)

    def test_span_group_rows_carry_word_windows(self):
        mapping, cursor = map_spine_to_chunks(
            _spine("Chapter 3: Mana Evaluation (2)", "The test began at dawn."),
            SPANNING, allow_span=True, require_full_coverage=True)
        self.assertEqual(1, cursor)
        self.assertEqual([0, 1], [row["segment_index"] for row in mapping])
        self.assertEqual([(0, 5), (5, 10)], [row["span_words"] for row in mapping])
        self.assertEqual({0}, {row["span_group"] for row in mapping})
        self.assertEqual([(0, 96000)] * 2, [(r["start_frame"], r["end_frame"]) for r in mapping])

    def test_span_group_spanning_a_later_chunk_boundary(self):
        # Heading + first half of a paragraph in chunk 0, rest in chunk 1.
        entries = [{"index": 0, "prepared_text": "Title. Alpha beta", "start_frame": 0,
                    "end_frame": 1000, "sample_rate": 1000},
                   {"index": 1, "prepared_text": "gamma delta.", "start_frame": 1000,
                    "end_frame": 2000, "sample_rate": 1000}]
        mapping, cursor = map_spine_to_chunks(_spine("Title", "Alpha beta gamma delta."),
                                              entries, allow_span=True)
        self.assertEqual(2, cursor)
        self.assertEqual([[0, 1], [0, 1]], [row["chunk_indexes"] for row in mapping])
        self.assertEqual([(0, 1), (1, 5)], [row["span_words"] for row in mapping])

    def test_changed_word_inside_group_still_refuses(self):
        with self.assertRaises(ValueError):
            map_spine_to_chunks(_spine("Chapter 3: Mana Evaluation (2)", "The test began at dusk."),
                                SPANNING, allow_span=True)

    def test_group_that_never_reaches_a_chunk_boundary_refuses(self):
        # The chunk continues past the last block of this spine document.
        with self.assertRaisesRegex(ValueError, "spans multiple spine segments"):
            map_spine_to_chunks(_spine("Chapter 3: Mana Evaluation (2)", "The test began"),
                                SPANNING, allow_span=True)


class SpanBuildTests(unittest.TestCase):
    def test_cpu_mode_highlights_group_as_one_block(self):
        result, _report = build_epub_readaloud(
            retained_epub_bytes=_source_epub(BODY), audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=SPANNING)
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            document = ET.fromstring(archive.read('OEBPS/ch1.xhtml'))
            smil = ET.fromstring(archive.read('OEBPS/ch1.smil'))
        wrappers = [div for div in document.iter(XHTML + "div") if div.get("id", "").startswith("aa2-g-")]
        self.assertEqual(1, len(wrappers))
        self.assertEqual(["h2", "p"], [child.tag.split("}")[1] for child in wrappers[0]])
        clips = _par_clips(smil)
        self.assertEqual(1, len(clips))
        self.assertTrue(clips[0][0].endswith("#" + wrappers[0].get("id")), clips)

    def test_cpu_mode_refuses_non_sibling_blocks(self):
        body = '<header><h2>Chapter 3: Mana Evaluation (2)</h2></header><p>The test began at dawn.</p>'
        with self.assertRaisesRegex(ValueError, "not sibling blocks"):
            build_epub_readaloud(retained_epub_bytes=_source_epub(body), audio_bytes=b'\xff\xfbmp3',
                                 audio_filename='audiobook.mp3', script_entries=SPANNING)

    def test_qwen_mode_times_each_block_from_its_own_tokens(self):
        words = ["Chapter", "3", "Mana", "Evaluation", "2", "The", "test", "began", "at", "dawn"]
        tokens = [{"text": w, "start_time": i * 0.4, "end_time": (i + 1) * 0.4}
                  for i, w in enumerate(words)]
        result, _report = build_epub_readaloud(
            retained_epub_bytes=_source_epub(BODY), audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=SPANNING,
            aligned_rows=[{"tokens": tokens}])
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            smil = ET.fromstring(archive.read('OEBPS/ch1.smil'))
        clips = _par_clips(smil)
        self.assertEqual(2, len(clips), clips)
        # Heading ends at token 5 (2.0 s); paragraph starts there.
        self.assertNotEqual(clips[0][0], clips[1][0])
        self.assertEqual(clips[0][2], clips[1][1], clips)

    def test_qwen_mode_book_and_script_apostrophe_styles_may_differ(self):
        # EPUB uses a curly apostrophe, the script an ASCII one; the aligner
        # keeps the ASCII one. Both blocks still time from their tokens.
        body = '<h2>Chapter 3</h2><p>It was one’s turn.</p>'
        entries = [{"index": 0, "prepared_text": "Chapter 3. It was one's turn.",
                    "start_frame": 0, "end_frame": 72000, "sample_rate": 24000}]
        words = ["Chapter", "3", "It", "was", "one's", "turn"]
        tokens = [{"text": w, "start_time": i * 0.5, "end_time": (i + 1) * 0.5}
                  for i, w in enumerate(words)]
        result, _report = build_epub_readaloud(
            retained_epub_bytes=_source_epub(body), audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=entries,
            aligned_rows=[{"tokens": tokens}])
        validate_epub_readaloud(result)
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            smil = ET.fromstring(archive.read('OEBPS/ch1.smil'))
        clips = _par_clips(smil)
        self.assertEqual(2, len(clips), clips)
        self.assertEqual(clips[0][2], clips[1][1], clips)

if __name__ == "__main__":
    unittest.main()
