"""Read-aloud follow-ups to #1844 and #1872, synthetic text only.

Each mapping case here refused on main at c675b04f with the tester's
message ("No unambiguous synchronization for spine text ... partial hint"),
although the script spoke the book's words exactly:

* a later chunk that runs on into the next EPUB block (not only the first
  chunk overrunning, which #1883 covered);
* speech preparation rewriting a word ("50%" is spoken "50 percent").

A genuinely different word must still refuse, now naming the word. #1872:
a capital dotted acronym followed by a sentence starter ends the sentence,
and that reaches the packaged SMIL as two clips.
"""
import io
import unittest
import xml.etree.ElementTree as ET
import zipfile

import qwen_align as qa
from epub_readaloud import (build_epub_readaloud, get_spoken_text, map_spine_to_chunks,
                            validate_epub_readaloud)
from tests.test_epub_readaloud_sentences import SMIL, _source_epub


def _spine(*texts):
    return [{"fragment": None, "text": text} for text in texts]


def _entries(*texts, rate=1000):
    return [{"index": i, "prepared_text": text, "start_frame": i * rate,
             "end_frame": (i + 1) * rate, "sample_rate": rate} for i, text in enumerate(texts)]


def _clips(result):
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        smil = ET.fromstring(archive.read('OEBPS/ch1.smil'))
    return [(par.find(SMIL + "text").get("src"), par.find(SMIL + "audio").get("clipBegin"),
             par.find(SMIL + "audio").get("clipEnd")) for par in smil.iter(SMIL + "par")]


def _tokens(text, seconds_per_word=0.5):
    words = [word.strip(".,!?\"“”") for word in text.split()]
    return [{"text": word, "start_time": i * seconds_per_word,
             "end_time": (i + 1) * seconds_per_word} for i, word in enumerate(words) if word]


class LaterChunkRunsOnTests(unittest.TestCase):
    """A chunk after the first one carries on into the next block."""

    SPINE = _spine("Alpha beta gamma. Delta epsilon.", "Zeta eta theta.")
    ENTRIES = _entries("Alpha beta gamma.", "Delta epsilon. Zeta", "eta theta.")

    def test_maps_as_one_group_with_exact_word_windows(self):
        mapping, cursor = map_spine_to_chunks(self.SPINE, self.ENTRIES, allow_span=True,
                                              require_full_coverage=True)
        self.assertEqual(3, cursor)
        # The smallest common boundary is the end of chunk 2: chunk 1
        # straddles the blocks, so both blocks share chunks 0-2 and their
        # real frames; each keeps its own word window for Qwen timing.
        self.assertEqual([[0, 1, 2], [0, 1, 2]], [row["chunk_indexes"] for row in mapping])
        self.assertEqual([(0, 5), (5, 8)], [row["span_words"] for row in mapping])
        self.assertEqual([(0, 3000)] * 2, [(r["start_frame"], r["end_frame"]) for r in mapping])

    def test_still_refused_without_span_opt_in(self):
        with self.assertRaisesRegex(ValueError, "spans multiple spine segments"):
            map_spine_to_chunks(self.SPINE, self.ENTRIES)

    def test_changed_word_in_the_overrun_still_refuses(self):
        entries = _entries("Alpha beta gamma.", "Delta epsilon. Zeta", "eta iota.")
        with self.assertRaises(ValueError):
            map_spine_to_chunks(self.SPINE, entries, allow_span=True)


class SpokenTextTests(unittest.TestCase):
    """EPUB text is compared as it was spoken, never by tolerating differences."""

    BOOK = "Prices rose 50% overnight."

    def test_speech_preparation_changes_the_words(self):
        # Guard: if this stops holding, the next test no longer exercises
        # the rewrite path and must be given another rewritten symbol.
        self.assertIn("percent", get_spoken_text(self.BOOK))

    def test_rewritten_symbol_maps_exactly(self):
        mapping, cursor = map_spine_to_chunks(_spine(self.BOOK),
                                              _entries(get_spoken_text(self.BOOK)))
        self.assertEqual(1, cursor)
        self.assertEqual([0], mapping[0]["chunk_indexes"])

    def test_different_word_refuses_and_names_it(self):
        with self.assertRaisesRegex(ValueError,
                                    r"EPUB 'method', spoken 'methods'.*audio chunk 1"):
            map_spine_to_chunks(_spine("The test method failed."),
                                _entries("The test methods failed."))

    def test_qwen_highlights_a_rewritten_block_whole_and_others_by_sentence(self):
        body = f'<p>{self.BOOK} Then it fell.</p><p>Calm came. Night fell.</p>'
        first = get_spoken_text(f"{self.BOOK} Then it fell.")
        second = "Calm came. Night fell."
        entries = _entries(first, second, rate=24000)
        entries[1]["start_frame"], entries[1]["end_frame"] = 240000, 480000
        entries[0]["end_frame"] = 240000
        result, _report = build_epub_readaloud(
            retained_epub_bytes=_source_epub(body), audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=entries,
            aligned_rows=[{"tokens": _tokens(first)}, {"tokens": _tokens(second)}])
        validate_epub_readaloud(result)
        clips = _clips(result)
        # One clip for the rewritten paragraph (no sentence split inside a
        # rewritten word), two sentence clips for the untouched one.
        self.assertEqual(3, len(clips), clips)
        self.assertEqual("00:00:00.000", clips[0][1])


class CjkCutInsideRunTests(unittest.TestCase):
    """A long Chinese/Japanese sentence the chunker cuts mid-run (no spaces).

    group_into_chunks hard-cuts a Chinese sentence longer than 183
    characters (Japanese 239) when no 。！？ falls inside the limit; the cut
    lands between two characters of one unspaced run.
    """

    BOOK = "他们走进了大厅，" * 3 + "然后坐下。"

    def test_cut_inside_a_run_maps(self):
        cut = 10  # inside the second 他们走进了大厅
        mapping, cursor = map_spine_to_chunks(_spine(self.BOOK),
                                              _entries(self.BOOK[:cut], self.BOOK[cut:]))
        self.assertEqual(2, cursor)
        self.assertEqual([0, 1], mapping[0]["chunk_indexes"])

    def test_changed_character_still_refuses(self):
        changed = self.BOOK.replace("进", "近", 1)
        with self.assertRaisesRegex(ValueError, "EPUB '进', spoken '近'"):
            map_spine_to_chunks(_spine(self.BOOK), _entries(changed[:10], changed[10:]))

    def test_qwen_build_with_a_mid_run_cut(self):
        first, second = self.BOOK[:10], self.BOOK[10:]
        entries = _entries(first, second, rate=24000)
        entries[0]["end_frame"], entries[1]["start_frame"], entries[1]["end_frame"] = 240000, 240000, 720000

        def char_tokens(text):
            chars = [ch for ch in text if ch not in "，。"]
            return [{"text": ch, "start_time": i * 0.3, "end_time": (i + 1) * 0.3}
                    for i, ch in enumerate(chars)]
        body = f"<p>{self.BOOK}</p>"
        epub = _source_epub(body).replace(b"<dc:language>en", b"<dc:language>zh")
        result, _report = build_epub_readaloud(
            retained_epub_bytes=epub, audio_bytes=b'\xff\xfbmp3', audio_filename='audiobook.mp3',
            script_entries=entries,
            aligned_rows=[{"tokens": char_tokens(first)}, {"tokens": char_tokens(second)}])
        validate_epub_readaloud(result)
        self.assertGreaterEqual(len(_clips(result)), 1)


class TerminalAcronymTests(unittest.TestCase):
    """#1872: the documented terminal-acronym policy."""

    def _split(self, text):
        return [text[a:b].strip() for a, b in qa.split_sentences(text)]

    def test_acronym_then_sentence_starter_splits(self):
        self.assertEqual(["He lives in the U.S.", "He works there."],
                         self._split("He lives in the U.S. He works there."))
        self.assertEqual(["They met in the U.K.", "But not again."],
                         self._split("They met in the U.K. But not again."))

    def test_nonterminal_forms_stay_whole(self):
        for text in ("The U.S. Army marched.", "Use one, e.g. The red one.",
                     "Born in the U.S."):
            self.assertEqual([text], self._split(text), text)

    def test_existing_boundaries_unchanged(self):
        self.assertEqual(["He lives here.", "He works there."],
                         self._split("He lives here. He works there."))
        self.assertEqual(["J. Smith went home.", "He slept."],
                         self._split("J. Smith went home. He slept."))
        self.assertEqual(["Price was 3.50 euros.", "Cheap."],
                         self._split("Price was 3.50 euros. Cheap."))
        self.assertEqual(["你好，世界。", "再见！"], self._split("你好，世界。 再见！"))

    def test_split_reaches_the_packaged_smil(self):
        text = "He lives in the U.S. He works there."
        entries = _entries(text, rate=24000)
        entries[0]["end_frame"] = 240000
        result, _report = build_epub_readaloud(
            retained_epub_bytes=_source_epub(f'<p>{text}</p>'), audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=entries,
            aligned_rows=[{"tokens": _tokens(get_spoken_text(text))}])
        validate_epub_readaloud(result)
        clips = _clips(result)
        self.assertEqual(2, len(clips), clips)
        self.assertEqual(clips[0][2], clips[1][1], clips)


if __name__ == "__main__":
    unittest.main()


class ZeroLengthTokenBuildTests(unittest.TestCase):
    """QwenLM/Qwen3-ASR#197: zero-length words no longer refuse a build."""

    def test_book_builds_with_zero_length_words(self):
        text = "We went to a shop. Then we came home."
        entries = _entries(text, rate=24000)
        entries[0]["end_frame"] = 24000 * 3
        tokens = [{"text": w, "start_time": s, "end_time": e} for w, s, e in (
            ("We", 0.0, 0.2), ("went", 0.2, 0.5), ("to", 0.5, 0.5), ("a", 0.5, 0.5),
            ("shop", 0.5, 0.9), ("Then", 1.2, 1.2), ("we", 1.2, 1.4), ("came", 1.4, 1.7),
            ("home", 1.7, 2.1))]
        result, _report = build_epub_readaloud(
            retained_epub_bytes=_source_epub(f"<p>{text}</p>"), audio_bytes=b'\xff\xfbmp3',
            audio_filename='audiobook.mp3', script_entries=entries,
            aligned_rows=[{"tokens": tokens}])
        validate_epub_readaloud(result)
        clips = _clips(result)
        # Two sentence clips on real word times: 0.0-0.9 and 1.2-2.1.
        self.assertEqual([("00:00:00.000", "00:00:00.900"), ("00:00:01.200", "00:00:02.100")],
                         [(begin, end) for _src, begin, end in clips])
