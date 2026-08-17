import json
import os
import tempfile
import unittest

from analyze_generation_attempts import (
    all_chunk_records,
    attempt_count_distribution,
    default_scripts_dir,
    extract_chunk_records,
    load_manifests,
    recall_band,
    recall_histogram,
    recovery_by_band,
    split_outcomes,
)


def _attempt(outcome, recall=None, phase="full", **extra):
    a = {"attempt": 1, "phase": phase, "outcome": outcome, **extra}
    if recall is not None:
        a["quality_metrics"] = {"source_token_recall": recall}
    return a


def _manifest(chunks, status="complete", failed_chunk=None, failed_chunk_attempts=None):
    m = {"status": status, "chunks": chunks}
    if status == "failed":
        m["failed_chunk"] = failed_chunk
        m["failed_chunk_attempts"] = failed_chunk_attempts or []
    return m


class RecallBandTests(unittest.TestCase):
    def test_band_edges(self):
        self.assertEqual("<0.30", recall_band(0.0))
        self.assertEqual("<0.30", recall_band(0.29))
        self.assertEqual("0.30-0.60", recall_band(0.30))
        self.assertEqual("0.60-0.75", recall_band(0.60))
        self.assertEqual("0.60-0.75", recall_band(0.749))
        # exactly 0.75 falls in the 0.75-0.90 band, not 0.60-0.75.
        self.assertEqual("0.75-0.90", recall_band(0.75))
        self.assertEqual("0.75-0.90", recall_band(0.899))
        # exactly 0.90 falls in >=0.90, not 0.75-0.90.
        self.assertEqual(">=0.90", recall_band(0.90))
        self.assertEqual(">=0.90", recall_band(1.0))


class ExtractChunkRecordsTests(unittest.TestCase):
    def test_accepted_chunks_from_chunks_list(self):
        manifest = _manifest([
            {"chunk_number": 1, "adaptively_split": False,
             "attempts": [_attempt("accepted")]},
        ])
        records = extract_chunk_records(manifest)
        self.assertEqual(1, len(records))
        self.assertTrue(records[0]["accepted"])
        self.assertFalse(records[0]["adaptively_split"])

    def test_failed_manifest_adds_one_failed_record(self):
        manifest = _manifest(
            [{"chunk_number": 1, "adaptively_split": False,
              "attempts": [_attempt("accepted")]}],
            status="failed", failed_chunk=2,
            failed_chunk_attempts=[_attempt("quality_rejected", recall=0.4, phase="split")],
        )
        records = extract_chunk_records(manifest)
        self.assertEqual(2, len(records))
        failed = [r for r in records if not r["accepted"]]
        self.assertEqual(1, len(failed))
        self.assertEqual(2, failed[0]["chunk_number"])
        # derived from an attempt with phase == "split"
        self.assertTrue(failed[0]["adaptively_split"])

    def test_complete_manifest_has_no_failed_record(self):
        manifest = _manifest([{"chunk_number": 1, "adaptively_split": False, "attempts": []}])
        records = extract_chunk_records(manifest)
        self.assertTrue(all(r["accepted"] for r in records))


class RecallHistogramTests(unittest.TestCase):
    def test_only_quality_rejected_attempts_counted(self):
        records = all_chunk_records([_manifest([
            {"chunk_number": 1, "adaptively_split": False, "attempts": [
                _attempt("quality_rejected", recall=0.2),
                _attempt("response_rejected"),  # no recall metric, not quality_rejected
                _attempt("accepted"),
            ]},
        ])])
        hist = recall_histogram(records)
        self.assertEqual(1, hist["<0.30"])
        self.assertEqual(0, hist["0.30-0.60"])
        self.assertEqual(sum(hist.values()), 1)

    def test_missing_recall_metric_skipped(self):
        records = all_chunk_records([_manifest([
            {"chunk_number": 1, "adaptively_split": False,
             "attempts": [{"attempt": 1, "phase": "full", "outcome": "quality_rejected"}]},
        ])])
        hist = recall_histogram(records)
        self.assertEqual(0, sum(hist.values()))


class RecoveryByBandTests(unittest.TestCase):
    def test_later_accepted_attempt_counts_as_recovery(self):
        records = all_chunk_records([_manifest([
            {"chunk_number": 1, "adaptively_split": False, "attempts": [
                _attempt("quality_rejected", recall=0.8),
                _attempt("accepted"),
            ]},
        ])])
        recovery = recovery_by_band(records)
        self.assertEqual(1, recovery["0.75-0.90"]["rejected"])
        self.assertEqual(1, recovery["0.75-0.90"]["recovered"])

    def test_no_later_accepted_attempt_is_not_recovery(self):
        records = all_chunk_records([_manifest([
            {"chunk_number": 1, "adaptively_split": False, "attempts": [
                _attempt("quality_rejected", recall=0.5),
                _attempt("quality_rejected", recall=0.4),
            ]},
        ])])
        recovery = recovery_by_band(records)
        self.assertEqual(2, recovery["0.30-0.60"]["rejected"])
        self.assertEqual(0, recovery["0.30-0.60"]["recovered"])

    def test_accepted_before_rejected_does_not_count(self):
        # accepted attempt occurring BEFORE the rejected one must not count
        # as recovery (recovery requires a LATER accepted attempt).
        records = all_chunk_records([_manifest([
            {"chunk_number": 1, "adaptively_split": False, "attempts": [
                _attempt("accepted"),
                _attempt("quality_rejected", recall=0.5),
            ]},
        ])])
        recovery = recovery_by_band(records)
        self.assertEqual(1, recovery["0.30-0.60"]["rejected"])
        self.assertEqual(0, recovery["0.30-0.60"]["recovered"])


class AttemptCountDistributionTests(unittest.TestCase):
    def test_splits_accepted_and_failed_chunks(self):
        manifest = _manifest(
            [{"chunk_number": 1, "adaptively_split": False,
              "attempts": [_attempt("accepted")]}],
            status="failed", failed_chunk=2,
            failed_chunk_attempts=[_attempt("quality_rejected", recall=0.1),
                                   _attempt("quality_rejected", recall=0.1)],
        )
        records = all_chunk_records([manifest])
        dist = attempt_count_distribution(records)
        self.assertEqual({1: 1}, dist["accepted"])
        self.assertEqual({2: 1}, dist["failed"])


class SplitOutcomesTests(unittest.TestCase):
    def test_counts_only_adaptively_split_chunks(self):
        manifest = _manifest([
            {"chunk_number": 1, "adaptively_split": True, "attempts": [_attempt("accepted")]},
            {"chunk_number": 2, "adaptively_split": False, "attempts": [_attempt("accepted")]},
        ])
        records = all_chunk_records([manifest])
        counts = split_outcomes(records)
        self.assertEqual(1, counts["accepted"])
        self.assertEqual(0, counts["failed"])


class LoadManifestsTests(unittest.TestCase):
    def test_malformed_manifest_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.json.generation_quality.json")
            with open(good, "w", encoding="utf-8") as f:
                json.dump(_manifest([{"chunk_number": 1, "adaptively_split": False, "attempts": []}]), f)

            bad_json = os.path.join(tmp, "bad.json.generation_quality.json")
            with open(bad_json, "w", encoding="utf-8") as f:
                f.write("{not valid json")

            no_chunks = os.path.join(tmp, "nochunks.json.generation_quality.json")
            with open(no_chunks, "w", encoding="utf-8") as f:
                json.dump({"status": "complete"}, f)

            not_a_manifest = os.path.join(tmp, "list.json.generation_quality.json")
            with open(not_a_manifest, "w", encoding="utf-8") as f:
                json.dump([1, 2, 3], f)

            ignored = os.path.join(tmp, "unrelated.json")
            with open(ignored, "w", encoding="utf-8") as f:
                json.dump({"chunks": []}, f)

            manifests, warnings = load_manifests(tmp)
            self.assertEqual(1, len(manifests))
            self.assertEqual(3, len(warnings))

    def test_missing_dir_returns_warning(self):
        manifests, warnings = load_manifests("/nonexistent/does/not/exist")
        self.assertEqual([], manifests)
        self.assertEqual(1, len(warnings))


class DefaultScriptsDirTests(unittest.TestCase):
    def test_defaults_to_repo_root_scripts(self):
        path = default_scripts_dir()
        self.assertTrue(path.endswith(os.path.join("scripts")))
        # Should resolve one level up from app/ (repo root), not inside app/.
        self.assertNotIn(os.path.join("app", "scripts"), path)


if __name__ == "__main__":
    unittest.main()
