"""Qwen alignment pure core (#1844) — fail-first contract before production.

Covers sentence splitting (donor algorithm port incl. CJK), exact
normalized token coverage, fragment time derivation without invention,
and the 180s subwindow planner. All fixtures are synthetic sentences.
"""
import unittest

import qwen_align as qa


class SentenceSplitTests(unittest.TestCase):
    def test_plain_sentences(self):
        self.assertEqual([(0, 12), (12, 25), (25, 38)],
                         qa.split_sentences("Hello world. Hello again. Hello world!"))
        full = qa.cover_ranges("Hello world. Hello again. Hello world!",
                               qa.split_sentences("Hello world. Hello again. Hello world!"))
        self.assertEqual([(0, 12), (12, 25), (25, 38)], full)

    def test_abbreviations_do_not_split(self):
        ranges = qa.split_sentences("Dr. Smith went home. He slept.")
        self.assertEqual([(0, 20), (20, 30)], ranges)

    def test_ellipsis_and_quotes_stay_together(self):
        ranges = qa.split_sentences('Wait... He said "stop." Then left.')
        self.assertEqual([(0, 7), (7, 23), (23, 34)], ranges)

    def test_decimal_does_not_split(self):
        ranges = qa.split_sentences("Price was 3.50 euros. Cheap.")
        self.assertEqual([(0, 21), (21, 28)], ranges)

    def test_cjk_terminators(self):
        ranges = qa.split_sentences("你好，世界。 再见，世界！ 今天天气晴？")
        # Ranges cover the stripped segments starting at each character.
        self.assertEqual(3, len(ranges))
        self.assertEqual(0, ranges[0][0])

    def test_empty_text(self):
        self.assertEqual([], qa.split_sentences(""))
        self.assertEqual([], qa.split_sentences("   "))
        self.assertEqual([], qa.cover_ranges("", []))

    def test_cover_ranges_cover_every_character(self):
        text = "One. Two! Three? "
        ranges = qa.cover_ranges(text, qa.split_sentences(text))
        joined = "".join(text[s:e] for s, e in ranges)
        self.assertEqual(text, joined)
        self.assertEqual(0, ranges[0][0])
        self.assertEqual(len(text), ranges[-1][1])


class AlignerLanguageTests(unittest.TestCase):
    def test_supported_names_roundtrip(self):
        for code, name in (("en", "English"), ("eng", "English"),
                           ("English", "English"), ("zh", "Chinese"),
                           ("zho", "Chinese"), ("ja", "Japanese"),
                           ("de", "German"), ("fr", "French"),
                           ("es", "Spanish"), ("it", "Italian"),
                           ("pt", "Portuguese"), ("ru", "Russian"),
                           ("ko", "Korean"), ("yue", "Cantonese")):
            self.assertEqual(name, qa.get_aligner_language_name(code))

    def test_unsupported_and_empty_fail(self):
        for bad in ("ar", "Arabic", "hi", "", None, 42):
            with self.assertRaises(ValueError):
                qa.get_aligner_language_name(bad)

    def test_supported_set_is_exactly_the_forced_aligner_eleven(self):
        self.assertEqual(
            {"Chinese", "English", "Cantonese", "French", "German",
             "Italian", "Japanese", "Korean", "Portuguese", "Russian",
             "Spanish"},
            set(qa.SUPPORTED_ALIGNER_LANGUAGES))

    def test_is_supported(self):
        self.assertTrue(qa.is_aligner_language_supported("en"))
        self.assertFalse(qa.is_aligner_language_supported("Arabic"))


class TokenValidationTests(unittest.TestCase):
    @staticmethod
    def tokens(words, step=0.5, start=0.0):
        return [{"text": w, "start_time": start + i * step,
                 "end_time": start + (i + 1) * step}
                for i, w in enumerate(words)]

    def test_exact_coverage_passes(self):
        text = "Hello Synthetic World."
        tokens = self.tokens(["Hello", "Synthetic", "World"], step=0.4)
        spans, unmatched = qa.map_tokens_to_text(text, tokens)
        self.assertEqual([], unmatched)
        self.assertEqual(3, len(spans))
        validated = qa.get_validated_aligner_tokens(tokens, 2.0)
        self.assertEqual(3, len(validated))

    def test_coverage_mismatch_fails(self):
        # Aligner returns fabricated/extra words: exact correspondence fails.
        text = "Hello Synthetic World."
        tokens = self.tokens(["Hello", "Synthetic", "World", "Extra"], step=0.4)
        spans, unmatched = qa.map_tokens_to_text(text, tokens)
        self.assertEqual(["Extra"], [t["text"] for t in unmatched])
        with self.assertRaises(ValueError):
            qa.require_exact_coverage(text, tokens)

    def test_missing_token_fails(self):
        text = "Hello Synthetic World."
        tokens = self.tokens(["Hello", "Synthetic"], step=0.4)
        with self.assertRaises(ValueError):
            qa.require_exact_coverage(text, tokens)

    def test_punctuation_and_case_insensitive(self):
        text = "Hello, SYNTHETIC world!"
        tokens = self.tokens(["hello", "synthetic", "world"], step=0.4)
        spans, unmatched = qa.map_tokens_to_text(text, tokens)
        self.assertEqual([], unmatched)
        qa.require_exact_coverage(text, tokens)

    def test_cjk_char_granularity(self):
        text = "你好世界，测试音频。"
        # Aligner emits one token per Chinese character.
        words = ["你", "好", "世", "界", "测", "试", "音", "频"]
        tokens = self.tokens(words, step=0.3)
        spans, unmatched = qa.map_tokens_to_text(text, tokens)
        self.assertEqual([], unmatched)
        qa.require_exact_coverage(text, tokens)

    def test_normalized_stream_agrees_with_aa2(self):
        # Char stream of the aligner core must match AA2 get_normalized_words
        # with apostrophes left out (#1844): epub_readaloud's span-group
        # slicing converts word offsets to stream offsets this way.
        from epub_readaloud import get_normalized_words
        for text in ("Hello Synthetic World.", "Dr. Smith's 3.50 euros!",
                     "Dr. Smith’s turn", "你好世界。", "Café — naïve test",
                     "Her handle was Dark_Knight."):
            self.assertEqual(
                "".join(get_normalized_words(text)).replace("'", "").replace("_", ""),
                qa.get_normalized_stream(text)[0])

    def test_curly_apostrophe_matches_aligner_drop(self):
        # Reproduces #1844: EPUB text uses U+2019 as the apostrophe. The
        # upstream Qwen3-ForcedAligner processor (is_kept_char) keeps only
        # the ASCII apostrophe, so "one’s" is emitted as "ones". The old
        # AA2 normalization folded U+2019 -> ASCII "'" and kept it, so the
        # stream read "one's" and require_exact_coverage raised
        # "1 unmatched token(s), first: 'ones'". Confirmed failing on the
        # unpatched code before this fix.
        text = "one\u2019s"
        tokens = [{"text": "ones", "start_time": 0.0, "end_time": 0.4}]
        spans = qa.require_exact_coverage(text, tokens)
        self.assertEqual(1, len(spans))
        self.assertEqual((0, len(text)), (spans[0][1], spans[0][2]))
        # The dropped apostrophe is shared by the stream and the token key.
        self.assertEqual("ones", qa.get_normalized_stream(text)[0])
        self.assertEqual("ones", qa.get_token_key("ones"))

    def test_underscore_words_match_aligner_drop(self):
        # The upstream is_kept_char keeps letters (L*), numbers (N*) and the
        # ASCII apostrophe only, so "Dark_Knight" reaches us as "DarkKnight"
        # and a "____" blank vanishes. Speech preparation leaves underscores
        # in the spoken text, so the stream must leave them out too.
        import unicodedata

        def upstream_clean(word):
            return "".join(ch for ch in word
                           if ch == "'" or unicodedata.category(ch)[0] in "LN")

        for text in ("Her handle was Dark_Knight.", "[Skill_Lv_3] gained.",
                     "Name: ____ said hi.", "snake_case."):
            with self.subTest(text=text):
                words = [upstream_clean(w) for w in text.split()]
                tokens = [{"text": w, "start_time": i * 0.4, "end_time": (i + 1) * 0.4}
                          for i, w in enumerate(word for word in words if word)]
                spans = qa.require_exact_coverage(text, tokens)
                self.assertEqual(len(tokens), len(spans))

    def test_ascii_apostrophe_token_matches_either_text_style(self):
        # The aligner keeps an ASCII apostrophe ("one's") but drops a curly
        # one ("ones"); the book and the script may use either style, so
        # every combination must correspond (#1844).
        for text in ("one's", "one’s"):
            for token_text in ("one's", "ones"):
                with self.subTest(text=text, token=token_text):
                    tokens = [{"text": token_text, "start_time": 0.0, "end_time": 0.4}]
                    self.assertEqual(1, len(qa.require_exact_coverage(text, tokens)))

    def test_fabricated_word_still_rejected_after_apostrophe_fix(self):
        # A genuinely different word must still fail closed: the apostrophe
        # fix must not weaken the exact-correspondence check.
        text = "one\u2019s"
        tokens = [{"text": "once", "start_time": 0.0, "end_time": 0.4}]
        with self.assertRaises(ValueError):
            qa.require_exact_coverage(text, tokens)

    def test_partial_coverage_still_rejected_with_apostrophes(self):
        # Full coverage is still required: dropping one apostrophe-affected
        # token leaves the stream uncovered and must raise, never fall back.
        text = "one\u2019s own"
        tokens = [{"text": "ones", "start_time": 0.0, "end_time": 0.4}]
        with self.assertRaises(ValueError):
            qa.require_exact_coverage(text, tokens)

    def test_nonfinite_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            tokens = [{"text": "Hello", "start_time": 0.0, "end_time": bad}]
            with self.assertRaises(ValueError):
                qa.get_validated_aligner_tokens(tokens, 2.0)

    def test_reversed_rejected(self):
        with self.assertRaises(ValueError):
            qa.get_validated_aligner_tokens(
                [{"text": "A", "start_time": 1.0, "end_time": 0.5}], 2.0)

    def test_zero_length_kept_as_a_point(self):
        # QwenLM/Qwen3-ASR#197: qwen-asr 0.0.6 reports [t, t] for real,
        # audible words; the point is kept exactly, never stretched.
        tokens = qa.get_validated_aligner_tokens(
            [{"text": "A", "start_time": 0.5, "end_time": 0.5}], 2.0)
        self.assertEqual([(0.5, 0.5)], [(t["start_time"], t["end_time"]) for t in tokens])

    def test_out_of_bounds_rejected(self):
        with self.assertRaises(ValueError):
            qa.get_validated_aligner_tokens(
                [{"text": "A", "start_time": -0.1, "end_time": 0.5}], 2.0)
        with self.assertRaises(ValueError):
            qa.get_validated_aligner_tokens(
                [{"text": "A", "start_time": 1.5, "end_time": 2.5}], 2.0)

    def test_bool_and_string_times_rejected(self):
        with self.assertRaises(ValueError):
            qa.get_validated_aligner_tokens(
                [{"text": "A", "start_time": True, "end_time": 1.0}], 2.0)
        with self.assertRaises(ValueError):
            qa.get_validated_aligner_tokens(
                [{"text": "A", "start_time": "0.0", "end_time": 1.0}], 2.0)

    def test_overlap_and_saturation_rejected(self):
        # Overlap: second token starts before previous ends.
        with self.assertRaises(ValueError):
            qa.get_validated_aligner_tokens(
                [{"text": "A", "start_time": 0.0, "end_time": 1.0},
                 {"text": "B", "start_time": 0.5, "end_time": 1.5}], 2.0)
        # Saturation: two tokens with identical (start, end) — a failed
        # instrument, never a success.
        with self.assertRaises(ValueError):
            qa.get_validated_aligner_tokens(
                [{"text": "A", "start_time": 0.0, "end_time": 1.0},
                 {"text": "B", "start_time": 0.0, "end_time": 1.0}], 2.0)

    def test_upstream_zero_length_patterns_accepted(self):
        # #197's Chinese badcase: 我 = [2.72, 2.72] beside 们 = [2.72, 2.88].
        badcase = [{"text": "对", "start_time": 2.40, "end_time": 2.56},
                   {"text": "我", "start_time": 2.72, "end_time": 2.72},
                   {"text": "们", "start_time": 2.72, "end_time": 2.88}]
        self.assertEqual(3, len(qa.get_validated_aligner_tokens(badcase, 4.0)))
        # #55: several characters sharing one instant.
        cluster = [{"text": c, "start_time": 1.0, "end_time": 1.0} for c in "的我们"]
        cluster.append({"text": "好", "start_time": 1.0, "end_time": 1.2})
        self.assertEqual(4, len(qa.get_validated_aligner_tokens(cluster, 4.0)))

    def test_zero_length_does_not_excuse_other_faults(self):
        cases = {
            "overlap": [{"text": "A", "start_time": 0.0, "end_time": 1.0},
                        {"text": "B", "start_time": 0.5, "end_time": 0.5}],
            "backwards": [{"text": "A", "start_time": 1.0, "end_time": 1.0},
                          {"text": "B", "start_time": 0.5, "end_time": 0.5}],
            "out of bounds": [{"text": "A", "start_time": 2.5, "end_time": 2.5}],
        }
        for name, tokens in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                qa.get_validated_aligner_tokens(tokens, 2.0)

    def test_sentence_of_only_zero_length_words_still_refuses(self):
        text = "Hi there."
        tokens = qa.get_validated_aligner_tokens(
            [{"text": "Hi", "start_time": 0.4, "end_time": 0.4},
             {"text": "there", "start_time": 0.4, "end_time": 0.4}], 2.0)
        spans = qa.require_exact_coverage(text, tokens)
        with self.assertRaisesRegex(ValueError, "no positive duration"):
            qa.derive_fragment_times(qa.cover_ranges(text, qa.split_sentences(text)), spans)

    def test_zero_length_word_inside_a_sentence_keeps_real_bounds(self):
        text = "We went to a shop. Then home."
        tokens = qa.get_validated_aligner_tokens(
            [{"text": "We", "start_time": 0.0, "end_time": 0.2},
             {"text": "went", "start_time": 0.2, "end_time": 0.5},
             {"text": "to", "start_time": 0.5, "end_time": 0.5},
             {"text": "a", "start_time": 0.5, "end_time": 0.5},
             {"text": "shop", "start_time": 0.5, "end_time": 0.9},
             {"text": "Then", "start_time": 1.2, "end_time": 1.4},
             {"text": "home", "start_time": 1.4, "end_time": 1.8}], 2.0)
        spans = qa.require_exact_coverage(text, tokens)
        times = qa.derive_fragment_times(qa.cover_ranges(text, qa.split_sentences(text)), spans)
        self.assertEqual([(0.0, 0.9), (1.2, 1.8)], times)

    def test_no_monotonic_clamping(self):
        """A non-monotonic token sequence must raise, never be repaired."""
        tokens = [{"text": "A", "start_time": 1.0, "end_time": 2.0},
                  {"text": "B", "start_time": 0.2, "end_time": 0.8}]
        with self.assertRaises(ValueError):
            qa.get_validated_aligner_tokens(tokens, 3.0)

    def test_cap_enforced_for_slice_duration(self):
        self.assertEqual(180.0, qa.MAX_ALIGN_SECONDS)
        with self.assertRaises(ValueError):
            qa.require_slice_within_limit(181.0)


class FragmentTimeTests(unittest.TestCase):
    """Fragment clips come from actual tokens; missing tokens never invented."""

    TEXT = "Hello synthetic world. Second sentence here."

    @staticmethod
    def tokens():
        words = ["Hello", "synthetic", "world", "Second", "sentence", "here"]
        return [{"text": w, "start_time": i * 0.5,
                 "end_time": (i + 1) * 0.5} for i, w in enumerate(words)]

    def test_fragment_times_are_min_start_max_end(self):
        fragments = qa.cover_ranges(self.TEXT, qa.split_sentences(self.TEXT))
        self.assertEqual(2, len(fragments))
        spans, _ = qa.map_tokens_to_text(self.TEXT, self.tokens())
        times = qa.derive_fragment_times(fragments, spans)
        self.assertEqual((0.0, 1.5), times[0])
        self.assertEqual((1.5, 3.0), times[1])

    def test_fragment_without_tokens_fails_closed(self):
        # Punctuation-only fragment has no tokens: fail, do not invent.
        fragments = [(0, 5), (5, 9)]
        spans, _ = qa.map_tokens_to_text("Hello.***", [
            {"text": "Hello", "start_time": 0.0, "end_time": 0.5}])
        with self.assertRaises(ValueError):
            qa.derive_fragment_times(fragments, spans)


class WindowPlannerTests(unittest.TestCase):
    """Subwindows schedule inference only; clips must come from tokens."""

    @staticmethod
    def sentences(count, words_per=10):
        out = []
        cursor = 0
        text = ""
        for _ in range(count):
            piece = "word " * (words_per - 1) + "word."
            out.append({"start": cursor, "end": cursor + len(piece),
                        "words": words_per})
            cursor += len(piece) + 1
            text += piece + " "
        return out, text

    def test_short_row_is_one_window(self):
        sentences, _ = self.sentences(3)
        windows = qa.get_aligner_windows(10.0, sentences)
        self.assertEqual(1, len(windows))
        self.assertEqual(0.0, windows[0]["start_sec"])
        self.assertEqual(10.0, windows[0]["end_sec"])
        self.assertEqual(0, windows[0]["char_start"])
        self.assertEqual(sentences[-1]["end"], windows[0]["char_end"])

    def test_long_row_windows_stay_within_limit(self):
        # ~0.5s/word: 4 sentences of 40 words =~ 80s each -> two windows.
        sentences, _ = self.sentences(4, words_per=40)
        windows = qa.get_aligner_windows(320.0, sentences)
        self.assertGreater(len(windows), 1)
        for window in windows:
            self.assertLessEqual(window["end_sec"] - window["start_sec"], 180.0)
        # Sentences are never split across windows.
        covered = [(w["char_start"], w["char_end"]) for w in windows]
        for sentence in sentences:
            owners = [1 for s, e in covered
                      if not (e <= sentence["start"] or s >= sentence["end"])]
            self.assertEqual(1, len(owners))

    def test_oversized_single_sentence_refused(self):
        # One sentence needing >180s of narration: no partial alignment.
        sentences = [{"start": 0, "end": 10, "words": 1000}]
        with self.assertRaises(ValueError):
            qa.get_aligner_windows(600.0, sentences)

    def test_zero_word_text_refused(self):
        with self.assertRaises(ValueError):
            qa.get_aligner_windows(5.0, [])

    def test_bool_duration_rejected(self):
        with self.assertRaises(ValueError):
            qa.get_aligner_windows(True, [{"start": 0, "end": 2, "words": 2}])


if __name__ == "__main__":
    unittest.main()
