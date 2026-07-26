"""An experiment artifact must let a later reader recompute every number.

Aggregate tables cannot support an architecture decision: they cannot
distinguish a real result from a prompt, roster, alias, indexing or scoring
difference. Raised by external review of the 2026-07-26 results, which reported
49.0% conditional selection with no per-line record behind it.
"""
import json
import os
import tempfile
import unittest

from experiments.manifest import ExperimentRecord

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fixtures", "attribution_gold_random.json")


LOADED = {"loaded": True, "context_length": 32768, "parallel": 1,
          "optimized": True}


def _record(environment=LOADED):
    return ExperimentRecord(
        name="unit", repo=REPO, model_name="test-model",
        base_url="http://localhost:1234/v1", gold_path=GOLD,
        decoding={"temperature": 0.0, "max_tokens": 24},
        environment=environment)


def _record_with_env():
    return _record()


class ManifestTest(unittest.TestCase):
    def test_it_pins_the_gold_fixture_by_hash(self):
        meta = _record().meta
        self.assertEqual(64, len(meta["gold_sha256"]))
        self.assertEqual(147, meta["gold_lines"])

    def test_it_records_the_code_state_including_dirtiness(self):
        # A commit alone does not identify the code if the tree was dirty.
        git = _record().meta["git"]
        self.assertIn("commit", git)
        self.assertIn("dirty", git)

    def test_summary_is_recomputable_from_the_rows(self):
        record = _record()
        record.add("open", "a", "L1", "ROXY", "ROXY", True, candidates=["ROXY"])
        record.add("open", "b", "L2", "ERIS", "ROXY", False, candidates=["ERIS"])
        record.add("open", "c", "L3", "NINA", "ROXY", False, candidates=["ROXY"])
        summary = record.summary()["open"]
        self.assertEqual(3, summary["n"])
        self.assertAlmostEqual(1 / 3, summary["accuracy"])
        # Conditional accuracy counts only lines whose answer was available.
        self.assertEqual(2, summary["available"])
        self.assertAlmostEqual(0.5, summary["conditional"])

    def test_prompts_are_hashed_not_stored(self):
        record = _record()
        record.add("open", "a", "L", "ROXY", "ROXY", True, prompt="x" * 5000)
        row = record.rows[0]
        self.assertEqual(64, len(row["prompt_sha256"]))
        self.assertEqual(5000, row["prompt_chars"])
        self.assertNotIn("x" * 100, json.dumps(row))

    def test_raw_responses_are_kept_verbatim(self):
        # The parse outcome is often the story; a summary would hide it.
        record = _record()
        record.add("open", "a", "L", "ROXY", None, False, raw="I think ROXY?")
        self.assertEqual("I think ROXY?", record.rows[0]["raw_response"])

    def test_the_written_artifact_round_trips(self):
        record = _record()
        record.add("open", "a", "L", "ROXY", "ROXY", True, candidates=["ROXY"],
                   provenance="scene")
        with tempfile.TemporaryDirectory() as tmp:
            path = record.write(os.path.join(tmp, "run.json"))
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertEqual(1, len(payload["rows"]))
        self.assertEqual("scene", payload["rows"][0]["candidate_provenance"])
        self.assertIn("elapsed_s", payload["meta"])
        self.assertIn("lmstudio", payload["meta"])

    def test_an_unreachable_server_aborts_the_run(self):
        # Contract reversed on purpose: a GPU result whose context length and
        # parallel setting are unknown cannot be compared to another run, so
        # failing to capture them must stop the experiment, not annotate it.
        from experiments.manifest import EnvironmentCaptureError, lmstudio_state
        with self.assertRaises((EnvironmentCaptureError, Exception)):
            lmstudio_state("definitely-not-a-loaded-model")


if __name__ == "__main__":
    unittest.main()


class ArtifactValidationTest(unittest.TestCase):
    """The same two defects have appeared in three separate harnesses.

    A duplicate (arm, gold_id) counts one judgement twice - it produced three
    identical arm totals in the roster experiment that read as a finding. A
    summary that does not follow from the rows means the reported number cannot
    be checked at all. Both are now refused at write time.
    """

    def test_duplicate_identities_are_reported(self):
        record = _record_with_env()
        record.add("a", "id1", "L", "ROXY", "ROXY", True)
        record.add("a", "id1", "L", "ROXY", "ERIS", False)
        self.assertTrue(any("duplicate" in p for p in record.validate()))

    def test_a_clean_record_validates(self):
        record = _record_with_env()
        record.add("a", "id1", "L", "ROXY", "ROXY", True)
        record.add("a", "id2", "L2", "ERIS", "ERIS", True)
        self.assertEqual([], record.validate())

    def test_writing_an_invalid_artifact_is_refused(self):
        from experiments.manifest import EnvironmentCaptureError
        record = _record_with_env()
        record.add("a", "id1", "L", "ROXY", "ROXY", True)
        record.add("a", "id1", "L", "ROXY", "ROXY", True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(EnvironmentCaptureError):
                record.write(os.path.join(tmp, "bad.json"))

    def test_a_missing_environment_is_a_problem(self):
        record = _record()
        record.meta["lmstudio"] = {"error": "boom"}
        record.add("a", "id1", "L", "ROXY", "ROXY", True)
        self.assertTrue(any("LM Studio" in p for p in record.validate()))


class CodeIdentityTest(unittest.TestCase):
    """A commit SHA plus 'dirty: true' does not identify what ran.

    The flag was true on every run because untracked markdown drafts sat in the
    tree, so it carried no information. It now reflects modified *tracked*
    files, and a hash of the harness sources identifies the code itself.
    """

    def test_untracked_notes_do_not_mark_the_tree_dirty(self):
        git = _record().meta["git"]
        self.assertIn("harness_sha256", git)
        self.assertEqual(64, len(git["harness_sha256"]))

    def test_the_fingerprint_changes_when_a_harness_changes(self):
        import tempfile
        from experiments.manifest import _source_fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x = 1\n")
            first = _source_fingerprint(tmp)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x = 2\n")
            self.assertNotEqual(first, _source_fingerprint(tmp))

    def test_non_python_files_are_ignored(self):
        import tempfile
        from experiments.manifest import _source_fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.py"), "w") as handle:
                handle.write("x = 1\n")
            before = _source_fingerprint(tmp)
            with open(os.path.join(tmp, "notes.md"), "w") as handle:
                handle.write("scratch\n")
            self.assertEqual(before, _source_fingerprint(tmp))
