"""A term the plain reading already says is FINISHED, not uncovered.

Goal 5.5 arrived at three states by measurement, and this scan only knew two.
The third - "the engine already says it, so an entry would do harm" - was
stored in `lexicon_candidates.json` as a bare count, so a correctly-handled
term looked identical to one nobody had measured. Nine of the fifteen terms
last reported as uncovered were in that state, `manga` among them.

Each test here drives the real classification path with a candidates file it
writes itself, so the states are exercised rather than mocked.
"""
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))

from experiments.shipped_book_lexicon_coverage import load_states  # noqa: E402


def _candidates(path, entries=(), unfixable=(), plain_ok=None):
    doc = {"entries": {t: {"respelling": t} for t in entries},
           "could_not_fix": [{"term": t} for t in unfixable]}
    if plain_ok is not None:
        doc["plain_already_works"] = list(plain_ok)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


class ThirdState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_plain_already_works_is_loaded_as_its_own_state(self):
        p = _candidates(os.path.join(self.tmp, "c.json"),
                        entries=["kuchibashi"], unfixable=["deka"],
                        plain_ok=["Manga", "PACHINKO"])
        entries, unfixable, plain_ok = load_states(p)
        self.assertEqual(entries, {"kuchibashi"})
        self.assertEqual(unfixable, {"deka"})
        self.assertEqual(plain_ok, {"manga", "pachinko"},
                         "the third state must case-fold like the other two")

    def test_a_plain_ok_term_is_not_in_neither_state(self):
        """The whole bug: manga is handled, and used to count as a gap."""
        p = _candidates(os.path.join(self.tmp, "c.json"),
                        entries=["kuchibashi"], unfixable=["deka"],
                        plain_ok=["manga"])
        entries, unfixable, plain_ok = load_states(p)
        handled = entries | unfixable | plain_ok
        present = ["kuchibashi", "deka", "manga", "gaurururu"]
        neither = [t for t in present if t not in handled]
        self.assertEqual(neither, ["gaurururu"])

    def test_an_old_candidates_file_yields_an_empty_third_state(self):
        """Files written before the key existed carry only a count, and the
        list cannot be recovered from it. Come back empty and say so, rather
        than inventing a number that depends on the writer's version."""
        p = os.path.join(self.tmp, "old.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"entries": {"kuchibashi": {}},
                       "could_not_fix": [{"term": "deka"}],
                       "plain_already_says_the_word": 931}, fh)
        _, _, plain_ok = load_states(p)
        self.assertEqual(plain_ok, set())

    def test_the_producer_emits_the_list_not_only_the_count(self):
        """Guards the half of the fix that lives in the other script: without
        the list, this scan has nothing to read and the bug returns."""
        src = os.path.join(REPO, "app", "experiments",
                           "lexicon_from_measurements.py")
        with open(src, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn('"plain_already_works": sorted(fine)', body)


if __name__ == "__main__":
    unittest.main()
