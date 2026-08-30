"""PDNC's pseudo-characters are not answers, and must not be candidates.

`character_info.csv` lists `_group`, `_unknowable` and (once) `_narr` beside
the real characters. They appear in 21 of the corpus's 28 novels but NOT in
PrideAndPrejudice or TheSignOfTheFour, which is why the first fixtures built
looked clean and the fault survived to 2026-08-30.

Two harms, only the second of which changes a score:

  roster  the model was offered `_GROUP` and `_UNKNOWABLE` as candidates in
          some books and not others, so a cross-book comparison was partly a
          comparison of roster contents. TheAwakening carries them and is one
          of the three books behind the 89.1% author-held-out result.
  gold    MansfieldPark had 16 rows whose expected_speaker WAS a pseudo-name -
          unanswerable rows scored as ordinary ones, depressing accuracy for a
          reason that has nothing to do with the adapter.

The builder fix is verified by artifact, not by this test passing: rebuilding
PrideAndPrejudice - a novel with no pseudo-characters - reproduces the shipped
fixture byte for byte, and rebuilding the three contaminated ones changes the
roster while leaving every gold entry identical.
"""
import glob
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.pdnc_fixture import is_pseudo_speaker  # noqa: E402

FIXTURES = os.path.join(REPO, "app", "fixtures")


def gold_fixtures():
    return sorted(glob.glob(os.path.join(FIXTURES, "attribution_gold_pdnc_*.json")))


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class PseudoSpeakerTest(unittest.TestCase):

    def test_the_corpus_has_fixtures_to_check(self):
        """A silent zero here would make every assertion below vacuous."""
        self.assertGreaterEqual(len(gold_fixtures()), 9)

    def test_no_fixture_offers_a_pseudo_character_as_a_candidate(self):
        offenders = []
        for path in gold_fixtures():
            doc = load(path)
            for name in doc["roster"]:
                if is_pseudo_speaker(name):
                    offenders.append(f"{os.path.basename(path)}: {name}")
        self.assertEqual([], offenders,
                         "these rosters offer PDNC pseudo-characters as "
                         "candidates, which changes the task between books:\n  "
                         + "\n  ".join(offenders))

    def test_no_fixture_asks_for_a_pseudo_character_as_the_answer(self):
        offenders = []
        for path in gold_fixtures():
            doc = load(path)
            for entry in doc["entries"]:
                if is_pseudo_speaker(entry["expected_speaker"]):
                    offenders.append(f"{os.path.basename(path)}: {entry['id']}")
        self.assertEqual([], offenders,
                         "these gold rows are unanswerable and would be scored "
                         "as ordinary ones:\n  " + "\n  ".join(offenders[:10]))

    def test_the_predicate_accepts_every_marker_the_corpus_actually_uses(self):
        """The three found by scanning all 28 novels, plus a real name."""
        for marker in ("_group", "_unknowable", "_narr", "_GROUP", "_UNKNOWABLE"):
            with self.subTest(marker):
                self.assertTrue(is_pseudo_speaker(marker))
        for real in ("EMMA", "MR. KNIGHTLEY", "MRS. BENNET", "ANNE DE BOURGH"):
            with self.subTest(real):
                self.assertFalse(is_pseudo_speaker(real))

    def test_the_five_heldout_books_are_present_and_answerable(self):
        """The books goal 1.3 needs, and the property that makes them useful."""
        for stem in ("emma", "mansfieldpark", "northangerabbey", "persuasion",
                     "senseandsensibility"):
            path = os.path.join(FIXTURES, f"attribution_gold_pdnc_{stem}.json")
            with self.subTest(stem):
                self.assertTrue(os.path.exists(path), f"{stem} fixture missing")
                doc = load(path)
                self.assertGreater(len(doc["entries"]), 400)
                roster = set(doc["roster"])
                known = roster | {n for g in doc["aliases"] for n in g}
                unresolvable = [e["id"] for e in doc["entries"]
                                if e["expected_speaker"] not in known]
                self.assertEqual([], unresolvable[:5],
                                 f"{stem} has gold outside roster and aliases")


if __name__ == "__main__":
    unittest.main()
