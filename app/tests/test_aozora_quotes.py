"""Hand-checked cases for the Aozora quote reader, from real Kokoro sentences.

Rule 21: every fixture below is a real sentence from the corpus, and the
pre-quote cases are the ones that exposed the reader's first bug - it looked
only AFTER the quote, so `…先生は、「もう帰りませんか」といって…` scored
`none` and "no local evidence" read 70.7% when it meant "no POST-quote frame".
One test asserts the after-only view still fails them, so these fixtures cannot
quietly stop discriminating.
"""
import unittest

from experiments.aozora_quotes import (extract, local_evidence, strip_aozora)

# Real sentences from 夏目漱石『こころ』.
PRE_QUOTE = "しばらくして海の中で起き上がるように姿勢を改めた先生は、"
AFTER_PRONOUN = "と私は大きな声を出した。"
NEXT_SENTENCE = "\n　父は去年の暮倒れた時に私に向かっていったと同じ言葉を"
CONSECUTIVE = "\n「何だかそれは私にも解らないが、自殺する人は"


class LocalEvidenceTests(unittest.TestCase):
    def test_subject_after_the_quote_is_found(self):
        kind, mention, where = local_evidence(AFTER_PRONOUN)
        self.assertEqual((kind, where), ("pronoun_subject", "after"))
        self.assertEqual(mention, "私")

    def test_subject_before_the_quote_is_found(self):
        # The case the first version missed entirely.
        kind, _, where = local_evidence("といって私を促した。", PRE_QUOTE)
        self.assertEqual((kind, where), ("named_subject", "before"))

    def test_an_after_only_reader_would_miss_the_pre_quote_case(self):
        # Pins the bug: with no `before` supplied this must NOT be found,
        # so the fixture keeps discriminating if the regex is ever loosened.
        kind, _, _ = local_evidence("といって私を促した。")
        self.assertEqual(kind, "none")

    def test_attribution_in_the_following_sentence_is_not_adjacent(self):
        # 父 is named, but in the NEXT sentence, not in a frame touching the
        # quote. `none` is the correct answer and does not mean unrecoverable.
        kind, _, _ = local_evidence(NEXT_SENTENCE)
        self.assertEqual(kind, "none")

    def test_consecutive_dialogue_has_no_local_evidence(self):
        self.assertEqual(local_evidence(CONSECUTIVE)[0], "none")

    def test_first_person_is_labelled_not_discarded(self):
        # In Japanese a bare 私 is frequently the CORRECT speaker, so it must
        # be its own category rather than folded into `none`.
        for pronoun in ("私", "僕", "俺", "彼女"):
            kind, _, _ = local_evidence("と%sは言った。" % pronoun)
            self.assertEqual(kind, "pronoun_subject", pronoun)

    def test_a_named_speaker_after_the_quote_is_not_called_a_pronoun(self):
        kind, mention, _ = local_evidence("と先生は言った。")
        self.assertEqual(kind, "named_subject")
        self.assertEqual(mention, "先生")


class StripTests(unittest.TestCase):
    def test_ruby_is_removed_from_inside_quotations(self):
        # Ruby sits INSIDE quotes, so leaving it corrupts the quote text.
        self.assertEqual(strip_aozora("「先生《せんせい》」"), "「先生」")

    def test_input_notes_are_removed(self):
        self.assertEqual(strip_aozora("本文［＃地から１字上げ］"), "本文")

    def test_the_ruby_anchor_is_removed(self):
        self.assertEqual(strip_aozora("｜先生《せんせい》です"), "先生です")

    def test_text_without_a_header_rule_survives(self):
        # Falling back to nothing would silently yield zero quotations.
        self.assertIn("「はい」", strip_aozora("前書き\n「はい」\n"))


class ExtractTests(unittest.TestCase):
    def test_quotes_are_found_in_order_with_context(self):
        entries = extract("　甲は「一つ目」と言った。乙は「二つ目」と答えた。", 8)
        self.assertEqual([e["quote_text"] for e in entries], ["一つ目", "二つ目"])
        self.assertLess(entries[0]["offset"], entries[1]["offset"])

    def test_no_entry_carries_a_speaker_field(self):
        # This file is NOT gold. A speaker key would invite it being scored.
        for entry in extract("「あ」と私は言った。", 5):
            self.assertNotIn("expected_speaker", entry)

    def test_nested_brackets_do_not_produce_a_runaway_span(self):
        entries = extract("「外「内」」", 4)
        self.assertTrue(all(len(e["quote_text"]) < 6 for e in entries))
