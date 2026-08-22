"""The homograph probe is only interpretable if its pairing survives.

The whole design rests on each word appearing twice under opposite readings.
Lose that and a model which always says REED scores 50% and looks merely
mediocre instead of looking like a model that never disambiguates at all.
These tests hold the properties the analysis will depend on.
"""
import json
import sys
import unittest
from pathlib import Path

APP = Path(__file__).parent.parent
sys.path.insert(0, str(APP))

from experiments.homograph_probe import build_items, load_words

WORDS = APP / "experiments" / "homograph_words.json"


class HomographSetTests(unittest.TestCase):

    def setUp(self):
        self.words = load_words(WORDS)

    def test_every_word_forces_two_different_pronunciations(self):
        for entry in self.words:
            self.assertNotEqual(
                entry["a"]["say"], entry["b"]["say"],
                "%s lists the same pronunciation twice, so it tests nothing"
                % entry["word"])

    def test_every_sentence_actually_contains_its_word(self):
        for entry in self.words:
            for key in ("a", "b"):
                self.assertIn(
                    entry["word"], entry[key]["sentence"].lower(),
                    "%s/%s does not contain the word it is probing"
                    % (entry["word"], key))

    def test_the_two_sentences_of_a_pair_are_different(self):
        for entry in self.words:
            self.assertNotEqual(entry["a"]["sentence"], entry["b"]["sentence"])

    def test_each_side_carries_a_plain_english_gloss(self):
        """The listener sees the gloss, not IPA. If it is empty they cannot choose."""
        for entry in self.words:
            for key in ("a", "b"):
                self.assertTrue(entry[key]["gloss"].strip(),
                                "%s/%s has no gloss" % (entry["word"], key))


class HomographItemTests(unittest.TestCase):

    def setUp(self):
        self.items = build_items(load_words(WORDS), 20260822)

    def test_two_items_per_word(self):
        counts = {}
        for it in self.items:
            counts[it["word"]] = counts.get(it["word"], 0) + 1
        self.assertTrue(all(v == 2 for v in counts.values()),
                        "every word must appear exactly twice: %s" % counts)

    def test_a_pair_is_never_adjacent(self):
        for i in range(len(self.items) - 1):
            self.assertNotEqual(
                self.items[i]["word"], self.items[i + 1]["word"],
                "adjacent pair at %d turns judgement into comparison" % i)

    def test_the_offered_alternative_is_the_other_reading(self):
        """The listener picks between two options; the wrong one must be the
        word's OTHER pronunciation, not an unrelated string."""
        by_word = {}
        for it in self.items:
            by_word.setdefault(it["word"], []).append(it)
        for word, pair in by_word.items():
            first, second = pair
            self.assertEqual(first["other_say"], second["expected_say"], word)
            self.assertEqual(second["other_say"], first["expected_say"], word)

    def test_the_shuffle_is_seeded(self):
        again = build_items(load_words(WORDS), 20260822)
        self.assertEqual([i["clip"] if "clip" in i else i["sentence"] for i in again],
                         [i["sentence"] for i in self.items])
        other = build_items(load_words(WORDS), 1)
        self.assertNotEqual([i["sentence"] for i in other],
                            [i["sentence"] for i in self.items],
                            "a different seed must give a different order")


if __name__ == "__main__":
    unittest.main()
