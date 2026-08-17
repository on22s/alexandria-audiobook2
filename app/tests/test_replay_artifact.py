"""How confidently can an artifact's origin be established?

The first version of replay_artifact answered only "replayable or not" and
wrote off 373 of 480 artifacts as unattributable. That was wrong in a
particular way: it consulted ONE source, found it empty, and reported absence.
Git records when every one of those artifacts was added, and 251 of the 373
name their producing script by convention - so the code that made them can be
read even though the arguments cannot.

The middle tier exists to hold that distinction without blurring it. It must
never emit a command: a guessed argv produces a new result wearing an old name,
which is worse than an unreproducible one because it looks fixed.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from experiments import replay_artifact as ra


class FlagRoundTripTests(unittest.TestCase):
    def test_scalars_lists_and_booleans_become_argv(self):
        argv, reason = ra.to_flags({"build": "b.json", "limit": 50,
                                    "backends": ["x", "y"], "keep": True})
        self.assertIsNone(reason)
        self.assertIn("--build", argv)
        self.assertIn("b.json", argv)
        self.assertEqual(["x", "y"], argv[argv.index("--backends") + 1:][:2])
        self.assertIn("--keep", argv)

    def test_false_and_none_are_omitted_not_passed(self):
        argv, _ = ra.to_flags({"quiet": False, "out": None, "limit": 1})
        self.assertNotIn("--quiet", argv)
        self.assertNotIn("--out", argv)

    def test_a_value_that_cannot_round_trip_is_reported_not_guessed(self):
        argv, reason = ra.to_flags({"nested": {"a": 1}})
        self.assertIsNone(argv)
        self.assertIn("nested", reason)

    def test_underscores_become_hyphens(self):
        argv, _ = ra.to_flags({"align_clips": 50})
        self.assertIn("--align-clips", argv)


class ScriptFromNameTests(unittest.TestCase):
    def test_an_artifact_named_for_its_producer_resolves(self):
        self.assertEqual("alignment_diagnosis.py",
                         ra.script_from_name("alignment_diagnosis.json"))

    def test_a_suffixed_variant_resolves_to_the_same_producer(self):
        self.assertEqual("alignment_diagnosis.py",
                         ra.script_from_name("alignment_diagnosis_trimmed.json"))

    def test_the_longest_match_wins(self):
        """`lexicon_corpus_scan` must not be claimed by a shorter `lexicon*`."""
        got = ra.script_from_name("lexicon_corpus_scan.json")
        if got is not None:
            self.assertTrue(got.startswith("lexicon_corpus_scan"),
                            f"shorter prefix won: {got}")

    def test_an_unrelated_name_resolves_to_nothing(self):
        self.assertIsNone(ra.script_from_name("zz_not_a_producer_xyz.json"))

    def test_a_non_json_path_is_rejected(self):
        self.assertIsNone(ra.script_from_name("something.txt"))


class ResolveProducerTests(unittest.TestCase):
    def _artifact(self, payload):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         dir=os.path.join(ra.REPO, "ab_test_runtime",
                                                          "experiments"))
        json.dump(payload, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_provenance_wins_and_yields_a_command(self):
        path = self._artifact({"provenance": {"script": "asr_backends.py",
                                              "args": {"limit": 5}}})
        got = ra.resolve_producer(path, "py")
        self.assertEqual("provenance", got["tier"])
        self.assertIsNotNone(got["argv"])

    def test_the_middle_tier_never_emits_a_command(self):
        """The whole point: known script, unknown args, so NO argv."""
        with patch.object(ra, "script_from_name", return_value="alignment_diagnosis.py"), \
             patch.object(ra, "added_commit", return_value=("abc1234", "2026-08-01")):
            got = ra.resolve_producer(self._artifact({"rows": []}), "py")
        self.assertEqual("git+naming", got["tier"])
        self.assertIsNone(got["argv"], "a guessed command is worse than none")
        self.assertEqual("alignment_diagnosis.py", got["script"])
        self.assertIn("git show", got["note"])

    def test_neither_source_gives_the_none_tier(self):
        with patch.object(ra, "script_from_name", return_value=None), \
             patch.object(ra, "added_commit", return_value=(None, None)):
            got = ra.resolve_producer(self._artifact({"rows": []}), "py")
        self.assertEqual("none", got["tier"])
        self.assertIsNone(got["argv"])

    def test_a_missing_producer_script_is_not_replayed(self):
        # provenance can name a script that has since been deleted; running
        # something else in its place would be silently wrong.
        path = self._artifact({"provenance": {"script": "zz_deleted_probe.py",
                                              "args": {"limit": 1}}})
        argv, reason = ra.replay_command(path, "py")
        self.assertIsNone(argv)
        self.assertIn("no longer exists", reason)


if __name__ == "__main__":
    unittest.main()
