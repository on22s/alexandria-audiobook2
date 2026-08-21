"""Hand-checked cases for the WP2021 reader, and the check that caught its bug.

A first version of this reader used a SQuAD-shaped re-release, ran clean,
passed its tests and reported 99.8% resolved - and was wrong, because it had
guessed which field meant what. `consistency_report` is the check that
distinguishes a decoded format from a guess, so the tests below exercise it on
instances that are deliberately BROKEN, not only on good ones.
"""
import os
import tempfile
import unittest

from experiments.wp2021_fixture import (CENTRE, WINDOW_SENTENCES, build,
                                        consistency_report, parse_instances,
                                        read_characters)

CHARACTERS = "1 孙少平 少平\n0 田晓霞 晓霞 霞\n1 田福军 福军\n"


def write(text, suffix=".txt"):
    handle, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def instance(centre='“进来吧。”', gold=1, candidates="[1, 0]", filler="旁白。"):
    lines = [filler] * WINDOW_SENTENCES
    lines[CENTRE] = centre
    lines[3] = "晓霞看见了他。"          # names character 1 in the window
    return "\n".join(lines) + "\n%s\n%d\n" % (candidates, gold)


class CharacterListTests(unittest.TestCase):
    def test_file_order_is_the_contract(self):
        # The corpus refers to characters BY INDEX, so reordering this list
        # would relabel every quote in the dataset.
        chars = read_characters(write(CHARACTERS))
        self.assertEqual([c[0] for c in chars], ["孙少平", "田晓霞", "田福军"])
        self.assertEqual(chars[1][2], "female")
        self.assertEqual(chars[1][1], ["田晓霞", "晓霞", "霞"])

    def test_a_malformed_line_raises(self):
        for bad in ("2 孙少平\n", "孙少平 少平\n", "1\n"):
            with self.assertRaises(ValueError, msg=bad):
                read_characters(write(bad))

    def test_an_empty_character_file_raises(self):
        with self.assertRaises(ValueError):
            read_characters(write("\n\n"))


class ParseTests(unittest.TestCase):
    def test_one_well_formed_instance_round_trips(self):
        got = parse_instances(write(instance()))
        self.assertEqual(len(got), 1)
        context, candidates, gold = got[0]
        self.assertEqual(len(context), WINDOW_SENTENCES)
        self.assertEqual(candidates, [1, 0])
        self.assertEqual(gold, 1)
        self.assertEqual(context[CENTRE], "“进来吧。”")

    def test_two_instances_are_found_independently(self):
        self.assertEqual(len(parse_instances(write(instance() + instance()))), 2)

    def test_a_non_numeric_gold_raises(self):
        with self.assertRaises(ValueError):
            parse_instances(write(instance().replace("\n1\n", "\n田晓霞\n")))

    def test_a_truncated_window_raises_rather_than_shifting(self):
        # Losing lines must fail loudly: a short window silently re-centres
        # the quote and attributes every later instance to the wrong speaker.
        short = "\n".join(["旁白。"] * 5) + "\n[1]\n1\n"
        with self.assertRaises(ValueError):
            parse_instances(write(short))

    def test_a_file_with_no_instances_raises(self):
        with self.assertRaises(ValueError):
            parse_instances(write("just prose\nand more prose\n"))


class ConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.chars = read_characters(write(CHARACTERS))

    def _report(self, text):
        return consistency_report(parse_instances(write(text)), self.chars)

    def test_a_good_instance_satisfies_every_property(self):
        r = self._report(instance())
        for key in ("window_is_21_sentences", "centre_line_is_a_quote",
                    "gold_is_among_candidates", "gold_index_in_range",
                    "gold_named_in_window"):
            self.assertEqual(r[key], 1, key)

    def test_a_centre_line_that_is_not_a_quote_is_caught(self):
        # The single property that would have exposed the earlier bug.
        r = self._report(instance(centre="他洗完了脸。"))
        self.assertEqual(r["centre_line_is_a_quote"], 0)
        self.assertEqual(r["instances"], 1)

    def test_a_gold_outside_the_candidate_list_is_caught(self):
        r = self._report(instance(gold=2, candidates="[1, 0]"))
        self.assertEqual(r["gold_is_among_candidates"], 0)

    def test_a_gold_never_named_in_the_window_is_caught(self):
        r = self._report(instance(gold=2, candidates="[2]"))
        self.assertEqual(r["gold_named_in_window"], 0)

    def test_a_gold_index_past_the_character_list_is_caught(self):
        r = self._report(instance(gold=99, candidates="[99]"))
        self.assertEqual(r["gold_index_in_range"], 0)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.chars = read_characters(write(CHARACTERS))

    def test_an_entry_carries_the_canonical_speaker_and_gender(self):
        entries, skipped = build(parse_instances(write(instance())), self.chars)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["expected_speaker"], "田晓霞")
        self.assertEqual(entries[0]["gender"], "female")
        self.assertEqual(entries[0]["line"], "“进来吧。”")
        self.assertEqual(entries[0]["candidates"], ["田晓霞", "孙少平"])
        self.assertEqual(skipped, {})

    def test_context_is_split_either_side_of_the_centre(self):
        entries, _ = build(parse_instances(write(instance())), self.chars)
        self.assertIn("晓霞看见了他。", entries[0]["prev_context"])
        self.assertNotIn("“进来吧。”", entries[0]["prev_context"])
        self.assertNotIn("“进来吧。”", entries[0]["next_context"])

    def test_an_out_of_range_gold_is_skipped_not_guessed(self):
        entries, skipped = build(
            parse_instances(write(instance(gold=99, candidates="[99]"))),
            self.chars)
        self.assertEqual(entries, [])
        self.assertEqual(skipped["gold index out of range"], 1)
