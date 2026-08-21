"""The narrator prior in the two-stage arm, and the ways it could be a no-op.

Two levers on attribution are measured separately and have never been combined:
the context window (400 -> 3200 chars, worth 52.9% -> 65.6% tonight) and the
first-person narrator prior (61.7% -> 79.4%, pdnc_narrator_prior__clean-3book).
They plausibly attack the SAME errors - the wide-context run over-attributes to
the narrator, predicting DR. WATSON 216 times against a gold of 148, with
Holmes -> Watson the single largest confusion at 76 - so the combination may
gain far less than the sum, which is the reason to measure it.

The failure mode this pins is a prior that is accepted and then applied to
nothing: a run reporting the baseline under the treatment's name.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class PromptTest(unittest.TestCase):
    def setUp(self):
        from experiments import two_stage_attribution as m
        self.m = m
        self.entry = {"line": "Hello.", "prev_context": "p", "next_context": "n"}
        self.roster = ["DR. WATSON", "MR. SHERLOCK HOLMES"]

    def test_without_a_narrator_the_prompt_is_unchanged(self):
        """The control arm must be byte-identical to the run already measured,
        or the comparison is against a different experiment."""
        self.assertEqual(self.m.build_prompt(self.entry, self.roster),
                         self.m.build_prompt(self.entry, self.roster, None))

    def test_the_prior_is_appended_and_names_the_narrator(self):
        got = self.m.build_prompt(self.entry, self.roster, "DR. WATSON")
        base = self.m.build_prompt(self.entry, self.roster)
        self.assertTrue(got.startswith(base), "the prior must APPEND, not rewrite")
        self.assertIn("DR. WATSON", got[len(base):])
        self.assertIn("first person", got[len(base):].lower())

    def test_it_uses_the_shared_helper_not_a_second_wording(self):
        """pdnc_narrator_prior measured +17.8 points with a specific wording.
        A second phrasing here would be a different intervention under the
        same name."""
        from narrator_prompt import add_narrator_prior
        self.assertEqual(add_narrator_prior(self.m.build_prompt(self.entry, self.roster),
                                            "DR. WATSON"),
                         self.m.build_prompt(self.entry, self.roster, "DR. WATSON"))

    def test_a_narrator_of_NARRATOR_is_rejected(self):
        """`THE NARRATOR` is not a roster identity, and accepting it would
        teach the model to answer the very label the roster forbids."""
        from narrator_prompt import get_valid_narrator_name
        with self.assertRaises(Exception):
            get_valid_narrator_name("NARRATOR")


class WiringTest(unittest.TestCase):
    def test_an_unmatched_narrator_argument_is_fatal(self):
        """A prior applied to no book reports the BASELINE under the
        treatment's name - the quietest way to fake a result."""
        src = (REPO / "app" / "experiments"
               / "two_stage_attribution.py").read_text(encoding="utf-8")
        self.assertIn("names no fixture in this run", src)

    def test_the_artifact_records_which_books_got_a_prior(self):
        src = (REPO / "app" / "experiments"
               / "two_stage_attribution.py").read_text(encoding="utf-8")
        self.assertIn('record.meta["narrators"]', src)

    def test_the_recorded_prompt_includes_the_prior(self):
        """--keep-prompts exists so the artifact shows what was actually
        asked; recording the un-prior'd prompt would misreport the run."""
        src = (REPO / "app" / "experiments"
               / "two_stage_attribution.py").read_text(encoding="utf-8")
        self.assertIn("build_prompt(entry, roster, narrator)", src)


if __name__ == "__main__":
    unittest.main()
