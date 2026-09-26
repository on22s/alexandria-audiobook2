"""The empty-roster refusal and the wall-clock projection in lora_serving_eval.

Both exist because of specific incidents, and both incidents were invisible to
every guard the harness already had.

EMPTY ROSTER. On 2026-09-26 a fixture sync replaced 8 of the 9 nine-novel gold
files on one box. The roster is built from the gold fixture's roster_additions,
so it went to 0 on every book: the model was shown no candidate names, every row
was scored against nothing, and the cell wrote 975 rows and a checkpoint while
reporting ordinary-looking accuracies. The same fault had cost ~6h on another
box the day before. Both times the roster count was printed in the per-book
header and nothing acted on it, which is why it is now fatal rather than louder.

PROJECTION. Three Muse Q1_0 cells burned ~37 hours across two boxes and produced
no artifact, because at 5-12 minutes per window they could not finish. A count of
failed or exhausted windows does NOT identify these -- test_futility_signal_
does_not_separate_good_from_futile pins the measurement that proves it, so
nobody re-proposes that guard. Wall clock does separate them.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "app"))

SOURCE = os.path.join(REPO, "app", "experiments", "lora_serving_eval.py")


class EmptyRosterGuardTest(unittest.TestCase):
    def setUp(self):
        with open(SOURCE, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_min_roster_defaults_to_refusing_an_empty_roster(self):
        """Default 1, not 0: an unset flag must still catch the incident."""
        self.assertIn('"--min-roster", type=int, default=1', self.src)

    def test_the_refusal_is_fatal_not_a_warning(self):
        """A printed warning is what already existed, twice, and was ignored."""
        i = self.src.index("if len(roster) < args.min_roster:")
        block = self.src[i:i + 1400]
        self.assertIn("raise SystemExit", block)
        self.assertIn("REFUSING", block)

    def test_the_refusal_names_the_fixture_and_its_hash(self):
        """The fix is always 'compare this file's hash to a trusted artifact',
        so the message has to carry the path and the hash or the reader has to
        go and derive them under time pressure."""
        i = self.src.index("if len(roster) < args.min_roster:")
        block = self.src[i:i + 1400]
        self.assertIn("attribution_gold_", block)
        self.assertIn("_sha256_file", block)
        self.assertIn("meta.gold_files", block)

    def test_sha256_helper_survives_a_missing_file(self):
        """It runs inside an error path; it must not raise a second error.

        Executed in isolation rather than by importing the module, which would
        need a live `openai` install just to reach one helper.
        """
        lines = self.src.splitlines()
        start = next(i for i, ln in enumerate(lines)
                     if ln.startswith("def _sha256_file("))
        end = next(i for i in range(start + 1, len(lines))
                   if lines[i] and not lines[i][0].isspace())
        ns = {}
        exec("\n".join(lines[start:end]), ns)
        got = ns["_sha256_file"]("/nonexistent/definitely/not/here.json")
        self.assertIn("unreadable", got)
        self.assertEqual(16, len(ns["_sha256_file"](SOURCE)))


class ProjectionGuardTest(unittest.TestCase):
    def setUp(self):
        with open(SOURCE, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_projection_reports_by_default_and_never_stops(self):
        """--max-hours 0 must print the ETA without ending a valid run. The
        Muse IQ2_XXS cell took 19 hours and returned +56.8; a default cap would
        have thrown that away."""
        self.assertIn('"--max-hours", type=float, default=0.0', self.src)
        i = self.src.index("if args.max_hours and eta_h > args.max_hours:")
        self.assertIn("[rate]", self.src[:i])

    def test_projection_happens_early_not_at_the_end(self):
        """An ETA printed after the run is not a guard. It fires within the
        first tenth of the windows, floored so short cells still report."""
        self.assertIn("projected_at = max(4, len(windows) // 10)", self.src)

    def test_stopping_says_it_is_resumable(self):
        i = self.src.index("STOPPING: projected")
        block = self.src[i:i + 700]
        self.assertIn("resumable", block)
        self.assertIn("checkpoint", block)

    def test_futility_signal_does_not_separate_good_from_futile(self):
        """Pins the measurement that killed the obvious guard.

        Counts taken 2026-09-26 from the two run logs. The FUTILE Q1_0 cell had
        MORE successful bindings and MORE exhausted windows than the VALUABLE
        IQ2_XXS cell, so no threshold on either count can separate them. Kept as
        a test so the idea is not re-proposed from intuition.
        """
        good = {"scoring_last_attempt": 11, "nothing_to_bind": 62, "errors": 165}
        futile = {"scoring_last_attempt": 77, "nothing_to_bind": 137, "errors": 457}
        for key in good:
            self.assertGreater(
                futile[key], good[key],
                f"{key}: the futile run no longer dominates the good run, so "
                f"this counter might separate them after all -- re-measure "
                f"before trusting any threshold built on it")


class RosterFallbackTest(unittest.TestCase):
    """The cause, not the symptom.

    load_book read only gold["roster_additions"]["names"], a field only SOME
    fixtures carry, and ignored gold["roster"], which they all carry. So the
    committed nine-novel fixtures produced an empty roster and the boxes were
    quietly running on uncommitted copies whose only difference was a duplicate
    of "roster" under "roster_additions". A fresh clone could not reproduce any
    nine-novel number.
    """
    FIXTURES = os.path.join(REPO, "app", "fixtures")
    NINE = ("emma", "mansfieldpark", "northangerabbey", "persuasion",
            "prideandprejudice", "senseandsensibility", "theawakening",
            "thesignofthefour")

    @staticmethod
    def resolve(gold):
        """The expression under test, as load_book applies it."""
        return ((gold.get("roster_additions") or {}).get("names")
                or gold.get("roster") or [])

    def test_every_committed_nine_novel_fixture_yields_a_roster(self):
        import json
        for book in self.NINE:
            path = os.path.join(self.FIXTURES,
                                f"attribution_gold_pdnc_{book}.json")
            with open(path, encoding="utf-8") as fh:
                gold = json.load(fh)
            names = self.resolve(gold)
            self.assertTrue(
                names,
                f"{book}: committed fixture resolves to an empty roster, which "
                f"is the defect this change exists to remove")

    def test_roster_additions_still_wins_when_present(self):
        """Existing arms must stay byte-identical, so the duplicate takes
        precedence wherever a box already has it."""
        gold = {"roster": ["FROM_ROSTER"],
                "roster_additions": {"names": ["FROM_ADDITIONS"]}}
        self.assertEqual(["FROM_ADDITIONS"], self.resolve(gold))

    def test_falls_back_only_when_additions_are_absent_or_empty(self):
        self.assertEqual(["FROM_ROSTER"],
                         self.resolve({"roster": ["FROM_ROSTER"]}))
        self.assertEqual(["FROM_ROSTER"],
                         self.resolve({"roster": ["FROM_ROSTER"],
                                       "roster_additions": {}}))
        self.assertEqual(["FROM_ROSTER"],
                         self.resolve({"roster": ["FROM_ROSTER"],
                                       "roster_additions": {"names": []}}))

    def test_no_roster_anywhere_still_yields_empty_for_the_guard_to_catch(self):
        """The fallback must not invent a roster; the guard is the backstop."""
        self.assertEqual([], self.resolve({}))
        self.assertEqual([], self.resolve({"roster": None}))

    def test_load_book_uses_the_fallback(self):
        with open(SOURCE, encoding="utf-8") as fh:
            src = fh.read()
        i = src.index("def load_book(")
        block = src[i:i + 2500]
        self.assertIn('gold.get("roster") or []', block)


if __name__ == "__main__":
    unittest.main()