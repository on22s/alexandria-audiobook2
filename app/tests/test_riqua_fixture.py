"""Hand-checked cases for the RiQuA reader, including ones it must REJECT.

Rule 21: a reader that quietly mislabels its columns produces a fixture that
looks entirely normal and is wrong in every row. The cases below are the ones
that would do that here - inverted relation arguments, pronoun golds admitted
as names, and a drop tally that does not add up.
"""
import json
import os
import tempfile
import unittest

from experiments.riqua_fixture import (build_text, is_name, link_quotes,
                                       parse_ann)

ANN = """T1\tQuotation 9 20\t"Good day,"
T2\tEntity 0 4\tEmma
T3\tEntity 25 38\tMr. Knightley
T4\tCue 20 24\tsaid
R1\tSpeaker Arg1:T2 Arg2:T1\t
R2\tAddressee Arg1:T3 Arg2:T1\t
R3\tCueing Arg1:T4 Arg2:T1\t
"""
TXT = "Emma  xxx\"Good day,\"said Mr. Knightley and then some trailing prose."


class IsNameTests(unittest.TestCase):
    def test_proper_names_are_names(self):
        for m in ("Emma", "Mr. Knightley", "Yegorushka", "Scrooge"):
            self.assertTrue(is_name(m), m)

    def test_bare_pronouns_are_not(self):
        # 39.4% of RiQuA speaker mentions are these. Admitting them would put
        # "he" in the roster and score the model against it.
        for m in ("he", "She", "I", "you", "them", "himself", "who", "that"):
            self.assertFalse(is_name(m), m)

    def test_lowercase_descriptions_are_not_names(self):
        # RiQuA marks these as entities; they identify a person in prose but
        # cannot be matched against a cast list.
        for m in ("the old man", "the stranger", "his companion"):
            self.assertFalse(is_name(m), m)

    def test_empty_and_none_are_not_names(self):
        for m in ("", "   ", None):
            self.assertFalse(is_name(m))


class ParseTests(unittest.TestCase):
    def _write(self, ann=ANN, txt=TXT):
        d = tempfile.mkdtemp()
        for name, body in (("t.ann", ann), ("t.txt", txt)):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        return d

    def test_spans_and_relations_round_trip(self):
        d = self._write()
        spans, relations = parse_ann(os.path.join(d, "t.ann"))
        self.assertEqual(spans["T1"][0], "Quotation")
        self.assertEqual(spans["T2"][3], "Emma")
        self.assertEqual(len(relations), 3)

    def test_discontinuous_spans_take_the_envelope(self):
        spans, _ = parse_ann(os.path.join(
            self._write(ann='T1\tQuotation 10 20;30 40\t"a" "b"\n'), "t.ann"))
        self.assertEqual((spans["T1"][1], spans["T1"][2]), (10, 40))

    def test_inverted_relation_arguments_raise(self):
        # The failure this reader exists to make impossible: if Arg1/Arg2 were
        # the other way round, every "speaker" would be a quotation and the
        # fixture would look fine.
        spans = {"T1": ("Quotation", 0, 5, "hi"), "T2": ("Entity", 6, 9, "Emma")}
        with self.assertRaises(ValueError):
            link_quotes(spans, [("Speaker", "T1", "T2")])

    def test_correct_argument_order_links(self):
        spans = {"T1": ("Quotation", 0, 5, "hi"), "T2": ("Entity", 6, 9, "Emma")}
        linked = link_quotes(spans, [("Speaker", "T2", "T1")])
        self.assertEqual(linked["T1"]["speakers"], ["Emma"])


class BuildTests(unittest.TestCase):
    def _build(self, ann=ANN, txt=TXT, window=10):
        d = tempfile.mkdtemp()
        for name, body in (("t.ann", ann), ("t.txt", txt)):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        return build_text(os.path.join(d, "t.ann"),
                          os.path.join(d, "t.txt"), window)

    def test_a_fully_named_row_is_kept_with_both_roles(self):
        entries, dropped, roster = self._build()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["expected_speaker"], "Emma")
        self.assertEqual(entries[0]["addressees"], ["Mr. Knightley"])
        self.assertEqual(entries[0]["quote_type"], "Cued")
        self.assertEqual(dropped, {})
        self.assertEqual(roster, {"Emma": 1, "Mr. Knightley": 1})

    def test_pronoun_speaker_is_dropped_not_kept_as_a_name(self):
        entries, dropped, _ = self._build(ann=ANN.replace("\tEmma", "\the"))
        self.assertEqual(entries, [])
        self.assertEqual(dropped["speaker pronoun only"], 1)

    def test_pronoun_addressee_is_dropped_separately(self):
        entries, dropped, _ = self._build(
            ann=ANN.replace("\tMr. Knightley", "\thim"))
        self.assertEqual(entries, [])
        self.assertEqual(dropped["addressee pronoun only"], 1)

    def test_a_quote_with_no_addressee_is_dropped(self):
        entries, dropped, _ = self._build(
            ann="\n".join(l for l in ANN.split("\n") if not l.startswith("R2")))
        self.assertEqual(entries, [])
        self.assertEqual(dropped["missing one side"], 1)

    def test_uncued_quotes_are_labelled_not_discarded(self):
        entries, _, _ = self._build(
            ann="\n".join(l for l in ANN.split("\n") if not l.startswith("R3")))
        self.assertEqual(entries[0]["quote_type"], "Uncued")

    def test_context_windows_come_from_the_text_around_the_span(self):
        entries, _, _ = self._build(window=10)
        self.assertTrue(entries[0]["prev_context"].endswith("xxx"))
        self.assertTrue(entries[0]["next_context"].startswith("said"))

    def test_every_quotation_is_either_kept_or_counted_as_dropped(self):
        # The accounting a caller relies on to know what a fixture represents.
        entries, dropped, _ = self._build(
            ann=ANN + 'T5\tQuotation 40 45\t"hm"\n')
        self.assertEqual(len(entries) + sum(dropped.values()), 2)
