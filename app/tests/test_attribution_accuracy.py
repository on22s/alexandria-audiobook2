import json
import tempfile
import unittest

from attribution_accuracy import (load_gold, normalize_speaker, score_run,
                                  summarize)


class GoldSetTest(unittest.TestCase):
    """Every pipeline gate checks form, none checks whether the speaker is
    right. This is the only test that can catch a correctness regression."""

    def test_gold_set_loads_and_is_well_formed(self):
        gold = load_gold()
        self.assertGreater(len(gold["entries"]), 40)
        for item in gold["entries"]:
            self.assertTrue(item["expected_speaker"])
            self.assertEqual(item["expected_speaker"],
                             item["expected_speaker"].upper())
            self.assertIsInstance(item["entry_index"], int)
            self.assertTrue(item["line"])

    def test_disputed_entries_are_withheld_by_default(self):
        gold = load_gold()
        disputed = [e for e in gold["entries"] if e.get("disputed")]
        self.assertTrue(disputed, "the row-1 dispute should be recorded")
        for item in disputed:
            self.assertTrue(item.get("dispute_note"),
                            "a disputed entry must say why")


class ScoringTest(unittest.TestCase):

    GOLD = {"entries": [
        {"id": "a", "entry_index": 0, "line": "Are you okay, Rudi?",
         "expected_speaker": "SYLPHY"},
        {"id": "b", "entry_index": 1, "line": "Sorry.",
         "expected_speaker": "RUDI"},
    ]}

    def test_correct_and_incorrect_are_counted(self):
        named = [{"speaker": "SYLPHY", "text": "Are you okay, Rudi?"},
                 {"speaker": "ROXY", "text": "Sorry."}]
        stats = summarize(score_run(named, self.GOLD))
        self.assertEqual(stats["aligned"], 2)
        self.assertEqual(stats["correct"], 1)
        self.assertAlmostEqual(stats["accuracy"], 0.5)

    def test_misaligned_run_is_reported_not_scored_wrong(self):
        # A run that segmented differently must not look like a wrong answer.
        named = [{"speaker": "SYLPHY", "text": "Something else entirely"},
                 {"speaker": "RUDI", "text": "Sorry."}]
        stats = summarize(score_run(named, self.GOLD))
        self.assertEqual(stats["aligned"], 1)
        self.assertEqual(stats["correct"], 1)

    def test_missing_entry_is_not_a_crash(self):
        stats = summarize(score_run([], self.GOLD))
        self.assertEqual(stats["aligned"], 0)
        self.assertEqual(stats["accuracy"], 0.0)

    def test_confusion_is_recorded(self):
        named = [{"speaker": "ROXY", "text": "Are you okay, Rudi?"},
                 {"speaker": "ROXY", "text": "Sorry."}]
        stats = summarize(score_run(named, self.GOLD))
        self.assertEqual(stats["confusion"][("SYLPHY", "ROXY")], 1)
        self.assertEqual(stats["missed"]["RUDI"], 1)

    def test_case_and_whitespace_do_not_matter(self):
        named = [{"speaker": " sylphy ", "text": "Are you okay, Rudi?"},
                 {"speaker": "RUDI", "text": "Sorry."}]
        stats = summarize(score_run(named, self.GOLD))
        self.assertEqual(stats["correct"], 2)

    def test_empty_speaker_is_wrong_not_correct(self):
        named = [{"speaker": "", "text": "Are you okay, Rudi?"},
                 {"speaker": None, "text": "Sorry."}]
        stats = summarize(score_run(named, self.GOLD))
        self.assertEqual(stats["correct"], 0)

    def test_normalize(self):
        self.assertEqual(normalize_speaker("  roxy  "), "ROXY")
        self.assertEqual(normalize_speaker(None), "")




class MalformedGoldRowTests(unittest.TestCase):
    """A bad gold row must stop the scorer, not bend the accuracy number."""

    NAMED = [{"speaker": "ROXY", "text": "A line."}]
    VALID = {"id": "x", "entry_index": 0, "line": "A line.",
             "expected_speaker": "ROXY"}

    def score(self, item):
        return score_run(self.NAMED, {"entries": [item]})

    def test_a_valid_row_still_scores(self):
        self.assertTrue(self.score(dict(self.VALID))[0]["correct"])

    def test_a_skipped_disputed_row_is_not_validated(self):
        incomplete = {"id": "d", "disputed": True}
        self.assertEqual([], score_run(self.NAMED, {"entries": [incomplete]}))
        with self.assertRaises(ValueError):
            score_run(self.NAMED, {"entries": [incomplete]}, include_disputed=True)

    def test_a_scored_disputed_row_is_still_validated_and_scores(self):
        valid = dict(self.VALID, disputed=True)
        self.assertEqual([], score_run(self.NAMED, {"entries": [valid]}))
        scored = score_run(self.NAMED, {"entries": [valid]}, include_disputed=True)
        self.assertTrue(scored[0]["correct"])

    def test_malformed_rows_fail_loudly_and_name_the_row(self):
        cases = {
            "missing line": ({k: v for k, v in self.VALID.items() if k != "line"},
                             "line"),
            "missing entry_index": ({k: v for k, v in self.VALID.items()
                                     if k != "entry_index"}, "entry_index"),
            "missing id": ({k: v for k, v in self.VALID.items() if k != "id"}, "id"),
            "missing expected_speaker": ({k: v for k, v in self.VALID.items()
                                          if k != "expected_speaker"},
                                         "expected_speaker"),
            "blank expected_speaker": (dict(self.VALID, expected_speaker="  "),
                                       "expected_speaker"),
            "null expected_speaker": (dict(self.VALID, expected_speaker=None),
                                      "expected_speaker"),
            "blank line": (dict(self.VALID, line=" "), "line"),
            "string entry_index": (dict(self.VALID, entry_index="0"), "entry_index"),
            "negative entry_index": (dict(self.VALID, entry_index=-1), "entry_index"),
            "not an object": ("not a gold row", "object"),
        }
        for name, (item, expected) in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as raised:
                    self.score(item)
                message = str(raised.exception)
                self.assertIn("gold entry 0", message)
                self.assertIn(expected, message)


class GoldFixtureIntegrityTest(unittest.TestCase):
    """A gold fixture is an answer key; a duplicated line votes twice.

    attribution_gold.json shipped three lines twice or three times over, so 3
    real lines cast 7 votes in every accuracy and confusion number computed
    from it. Found by audit.
    """

    FIXTURES = ("fixtures/attribution_gold.json",
                "fixtures/attribution_gold_random.json")

    def _load(self, name):
        import json
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), name)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)["entries"]

    def test_ids_are_unique_in_every_fixture(self):
        for name in self.FIXTURES:
            entries = self._load(name)
            ids = [entry["id"] for entry in entries]
            self.assertEqual(len(ids), len(set(ids)), f"duplicate id in {name}")

    def test_book_and_entry_index_are_unique_in_every_fixture(self):
        # A distinct id pointing at the same source line would double-count too.
        for name in self.FIXTURES:
            entries = self._load(name)
            keys = [(e.get("book"), e.get("entry_index")) for e in entries]
            self.assertEqual(len(keys), len(set(keys)),
                             f"duplicate (book, entry_index) in {name}")

    def test_every_entry_has_an_expected_speaker(self):
        for name in self.FIXTURES:
            for entry in self._load(name):
                self.assertTrue(str(entry.get("expected_speaker") or "").strip(),
                                f"blank expected_speaker in {name}: {entry['id']}")


class RestrictedCandidates(unittest.TestCase):
    """Only characters named near the quote are shown to the model.

    The one method effect the literature credits that is not an oracle: arXiv
    2307.03734 reports 0.40 end-to-end against 0.78 once candidates are
    restricted. `candidate_restriction.json` measured what the cheap version
    costs here - 74 candidates down to 8, right speaker retained 92.7% of the
    time - so the restricted arm only wins above 70.7% on the rows it keeps.
    """

    ROSTER = ["ELIZABETH [also: LIZZY, MISS ELIZA]",
              "MRS. BENNET [also: BENNET]",
              "COLONEL FITZWILLIAM",
              "A YOUNG LUCAS [also: YOUNG LUCAS]"]

    def _restrict(self, entry):
        from experiments.two_stage_attribution import restrict_roster
        return restrict_roster(self.ROSTER, entry)

    def test_a_character_named_in_the_window_is_kept(self):
        kept = self._restrict({"prev_context": "Elizabeth turned away.",
                               "line": "“Indeed I shall not.”",
                               "next_context": ""})
        self.assertIn(self.ROSTER[0], kept)

    def test_a_character_absent_from_the_window_is_dropped(self):
        kept = self._restrict({"prev_context": "Elizabeth turned away.",
                               "line": "“Indeed I shall not.”",
                               "next_context": ""})
        self.assertNotIn("COLONEL FITZWILLIAM", kept)

    def test_an_alias_keeps_its_character(self):
        """MRS. BENNET must survive a window that only says BENNET."""
        kept = self._restrict({"prev_context": "Bennet sighed.",
                               "line": "“Oh!”", "next_context": ""})
        self.assertIn(self.ROSTER[1], kept)

    def test_a_substring_does_not_keep_a_character(self):
        """LUCAS must not be matched inside LUCASTA - whole words only.

        The window has to name SOMEONE, or the empty-result fallback returns
        the whole roster and the assertion passes for the wrong reason. A
        first version of this test used a window naming nobody and failed
        against correct behaviour.
        """
        kept = self._restrict({"prev_context": "Elizabeth watched Lucasta.",
                               "line": "“Oh!”", "next_context": ""})
        self.assertIn(self.ROSTER[0], kept)
        self.assertNotIn(self.ROSTER[3], kept)

    def test_an_empty_window_falls_back_to_the_whole_roster(self):
        """A prompt with no candidates asks a different question entirely."""
        self.assertEqual(self.ROSTER,
                         self._restrict({"prev_context": "", "line": "",
                                         "next_context": ""}))

    def test_a_window_naming_nobody_falls_back_rather_than_emptying(self):
        kept = self._restrict({"prev_context": "The room was quiet.",
                               "line": "“Oh!”", "next_context": ""})
        self.assertEqual(self.ROSTER, kept)


if __name__ == "__main__":
    unittest.main()
