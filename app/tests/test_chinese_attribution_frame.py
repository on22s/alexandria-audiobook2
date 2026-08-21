"""Elson's method in Chinese word order, and the addressee it must not take.

English attributes after the quote, `"..." said Mr. Darcy`; Chinese attributes
before it, `黄蓉道：「...」`. So the trigram from #372 cannot fire on Chinese and
the question is whether the METHOD survives a rewritten pattern. Measured on
JY-QuotePlus, 8,144 quotations: it fires on 41.8% at .9753, against 4.0% at
.9899 for English. The method transfers and reaches ten times as far.

The correction that matters: `向X道` and `對X道` mean "said TO X". A rule that
takes that mention scores .9249; declining it scores .9753 while the share of
all quotations answered correctly does not move. Turning wrong answers into
declines is right for a pre-pass and wrong for a benchmark, and the artifact
records both so the choice is visible.
"""
import unittest

from experiments.chinese_attribution_frame import (ADDRESSEE_MARKERS, CUES,
                                                   alias_map, attribute,
                                                   evaluate, last_pre_sentence)

ALIAS = {"黄蓉": "黄蓉", "郭靖": "郭靖", "蓉儿": "黄蓉", "黄药师": "黄药师"}
SURFACES = sorted(ALIAS, key=len, reverse=True)


def guess(sentence, skip=True):
    return attribute(sentence, SURFACES, ALIAS, skip)[0]


class FrameTest(unittest.TestCase):
    def test_the_plain_frame_resolves(self):
        self.assertEqual("黄蓉", guess("黄蓉道"))
        self.assertEqual("郭靖", guess("郭靖说道"))

    def test_a_colon_at_the_boundary_is_already_stripped_by_the_caller(self):
        self.assertEqual("黄蓉", guess("黄蓉道"))

    def test_an_alias_resolves_to_its_entity(self):
        self.assertEqual("黄蓉", guess("蓉儿笑道"))

    def test_the_longest_cue_wins(self):
        """说道 must be tried before 说, or the head keeps a stray character."""
        self.assertEqual("郭靖", guess("郭靖说道"))
        self.assertIn("说道", CUES)
        self.assertLess(CUES.index("说道"), CUES.index("说"))

    def test_the_longest_mention_wins(self):
        """黄药师 must not be read as 黄 plus noise."""
        self.assertEqual("黄药师", guess("黄药师道"))


class AddresseeTest(unittest.TestCase):
    def test_a_directional_preposition_marks_who_was_spoken_to(self):
        """Every error in the naive run had this shape."""
        for marker in ADDRESSEE_MARKERS:
            self.assertIsNone(guess("郭靖%s黄蓉道" % marker), marker)

    def test_the_naive_rule_takes_the_addressee_which_is_the_bug(self):
        self.assertEqual("黄蓉", guess("郭靖向黄蓉道", skip=False))

    def test_declining_is_not_the_same_as_finding_nobody(self):
        entity, reason = attribute("郭靖向黄蓉道", SURFACES, ALIAS, True)
        self.assertIsNone(entity)
        self.assertIn("addressee", reason)


class DeclineTest(unittest.TestCase):
    def test_no_cue_at_the_boundary_declines(self):
        self.assertIsNone(guess("黄蓉走进屋里"))

    def test_a_cue_with_no_known_mention_declines(self):
        entity, reason = attribute("那老者道", SURFACES, ALIAS, True)
        self.assertIsNone(entity)
        self.assertIn("no known mention", reason)

    def test_an_empty_sentence_declines(self):
        self.assertIsNone(guess(""))


class PlumbingTest(unittest.TestCase):
    def test_the_last_non_blank_pre_sentence_is_used(self):
        row = {"context_pre": ["前面的句子。", "  ", "黄蓉道："]}
        self.assertEqual("黄蓉道", last_pre_sentence(row))

    def test_a_row_with_no_pre_context_yields_empty(self):
        self.assertEqual("", last_pre_sentence({"context_pre": []}))
        self.assertEqual("", last_pre_sentence({}))

    def test_the_alias_map_is_built_from_the_corpus_itself(self):
        rows = [{"labels": {"说话人-mention": "蓉儿", "说话人-entity": "黄蓉"}},
                {"labels": {"说话人-mention": "黄蓉", "说话人-entity": "黄蓉"}},
                {"labels": {"说话人-mention": "", "说话人-entity": "郭靖"}}]
        self.assertEqual({"蓉儿": "黄蓉", "黄蓉": "黄蓉"}, alias_map(rows))

    def test_declining_raises_accuracy_without_losing_correct_answers(self):
        """The whole argument for the addressee rule, in miniature."""
        rows = [{"context_pre": ["黄蓉道："],
                 "labels": {"说话人-mention": "黄蓉", "说话人-entity": "黄蓉"}},
                {"context_pre": ["郭靖向黄蓉道："],
                 "labels": {"说话人-mention": "郭靖", "说话人-entity": "郭靖"}}]
        naive = evaluate(rows, skip_addressee=False)
        aware = evaluate(rows, skip_addressee=True)
        self.assertEqual(1, naive["wrong"])
        self.assertEqual(0, aware["wrong"])
        self.assertEqual(naive["correct"], aware["correct"])
        self.assertGreater(aware["accuracy_where_fired"],
                           naive["accuracy_where_fired"])
