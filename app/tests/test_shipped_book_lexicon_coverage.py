"""Goal 5.5's coverage scan must count the shipped books, not the report text.

The first version of this scan parsed the discovery script's STDOUT with awk
and produced three "terms" that were words from the report's own prose -
`candidate`, `only;`, `scripts,` - inflating the uncovered set by 3 and the
term count by 2. These tests pin the parts that failed.
"""
import importlib.util
import json
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "app", "experiments",
                      "shipped_book_lexicon_coverage.py")


def _mod():
    spec = importlib.util.spec_from_file_location("shipcov", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Parsing(unittest.TestCase):
    def setUp(self):
        self.m = _mod()

    def test_words_are_whole_words_not_substrings(self):
        """`same` inside `sameness` is not an occurrence of `same`. Substring
        matching would inflate every number in this scan."""
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "b.json")
            json.dump([{"speaker": "A", "text": "Sameness and samurai.",
                        "instruct": ""}], open(p, "w", encoding="utf-8"))
            words = self.m.terms_in_script(p)
        self.assertIn("samurai", words)
        self.assertIn("sameness", words)
        self.assertNotIn("same", words)

    def test_sidecars_are_not_mistaken_for_books(self):
        """scripts/ holds generation checkpoints and voice configs beside the
        books; counting them would multiply the book count."""
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("Book One.json",
                      "Book One.json.generation_quality.json",
                      "Book One.voice_config.json"):
                json.dump([], open(os.path.join(tmp, n), "w", encoding="utf-8"))
            found = self.m.shipped_scripts(tmp)
        self.assertEqual(["Book One.json"],
                         [os.path.basename(f) for f in found])

    def test_the_two_states_are_read_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.json")
            json.dump({"entries": {"Alpha": {}, "beta": {}},
                       "could_not_fix": [{"term": "Gamma"}, {"term": "delta"}]},
                      open(p, "w", encoding="utf-8"))
            entries, unfixable = self.m.load_states(p)
        self.assertEqual({"alpha", "beta"}, entries)
        self.assertEqual({"gamma", "delta"}, unfixable)
        self.assertEqual(set(), entries & unfixable)

    def test_a_term_in_neither_state_is_what_the_goal_counts(self):
        """The whole point: a shipped term that was never measured is the only
        thing keeping 5.5 open, so it must not fall into either bucket."""
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.json")
            json.dump({"entries": {"tsundere": {}},
                       "could_not_fix": [{"term": "aahaha"}]},
                      open(p, "w", encoding="utf-8"))
            entries, unfixable = self.m.load_states(p)
        present = {"tsundere", "aahaha", "pachinko"}
        neither = [t for t in present if t not in entries and t not in unfixable]
        self.assertEqual(["pachinko"], neither)


if __name__ == "__main__":
    unittest.main()
