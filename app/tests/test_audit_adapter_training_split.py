"""GOALS.md 2.7: which shipped adapters saw their own validation clips.

The failure this guards against is a count that looks clean and is wrong: the manifest's sample_count
went stale after retrains (on 2026-09-29 it said 15 adapters trained on all 200 clips while the adapters'
own metadata said 6), and an adapter with no readable metadata must never be counted as train-only.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.audit import audit_adapter_training_split as aud


class SplitAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = self.temp.name
        self.manifest = []

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, name, num_samples=180, manifest_count=None, weights=b"weights", meta_hash="auto", raw_meta=None):
        os.makedirs(os.path.join(self.dir, name), exist_ok=True)
        with open(os.path.join(self.dir, name, aud.WEIGHTS), "wb") as handle:
            handle.write(weights)
        meta = {"num_samples": num_samples}
        if meta_hash == "auto":
            meta["checkpoint_sha256"] = hashlib.sha256(weights).hexdigest()
        elif meta_hash:
            meta["checkpoint_sha256"] = meta_hash
        with open(os.path.join(self.dir, name, "training_meta.json"), "w", encoding="utf-8") as handle:
            handle.write(raw_meta if raw_meta is not None else json.dumps(meta))
        self.manifest.append({"id": name, "sample_count": num_samples if manifest_count is None else manifest_count})

    def run_audit(self, **kwargs):
        with open(os.path.join(self.dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(self.manifest, handle)
        return aud.audit(self.dir, **kwargs)

    def klass(self, result, name):
        return next(r["klass"] for r in result["adapters"] if r["id"] == name)

    def test_the_two_sample_counts_are_told_apart(self):
        self.adapter("clean", 180)
        self.adapter("leaky", 200)
        result = self.run_audit()
        self.assertEqual({"train-only": 1, "all-clips": 1}, result["summary"]["counts"])
        self.assertEqual(["leaky"], result["summary"]["all_clips"])

    def test_an_unusual_count_is_reported_with_its_number_not_called_clean(self):
        self.adapter("small", 88)
        result = self.run_audit()
        self.assertEqual("other-count", self.klass(result, "small"))
        self.assertEqual({"small": 88}, result["summary"]["other_count"])

    def test_missing_metadata_is_never_counted_as_train_only(self):
        self.manifest.append({"id": "ghost", "sample_count": 180})      # in the manifest, no directory
        os.makedirs(os.path.join(self.dir, "empty"))
        self.manifest.append({"id": "empty", "sample_count": 180})      # directory, no training_meta.json
        result = self.run_audit()
        self.assertEqual("no-metadata", self.klass(result, "ghost"))
        self.assertEqual("no-metadata", self.klass(result, "empty"))
        self.assertNotIn("train-only", result["summary"]["counts"])
        self.assertEqual(["empty", "ghost"], sorted(result["summary"]["unclassified"]))

    def test_malformed_or_wrongly_typed_metadata_is_listed_not_guessed(self):
        self.adapter("truncated", raw_meta='{"num_samples": 18')
        self.adapter("no_key", raw_meta="{}")
        self.adapter("as_string", raw_meta='{"num_samples": "180"}')
        self.adapter("as_bool", raw_meta='{"num_samples": true}')
        result = self.run_audit()
        for name in ("truncated", "no_key", "as_string", "as_bool"):
            self.assertEqual("unreadable", self.klass(result, name), name)
        self.assertNotIn("train-only", result["summary"]["counts"])

    def test_a_stale_manifest_does_not_override_the_adapters_own_record(self):
        self.adapter("retrained", 180, manifest_count=200)        # manifest still says 200
        self.adapter("really_leaky", 200)
        result = self.run_audit()
        self.assertEqual(2, result["summary"]["manifest_claims_all_clips"])   # what a manifest count would say
        self.assertEqual(["really_leaky"], result["summary"]["all_clips"])    # what is true
        self.assertEqual(["retrained"], result["summary"]["manifest_disagrees"])

    def test_directories_outside_the_manifest_are_ignored(self):
        self.adapter("shipped", 180)
        os.makedirs(os.path.join(self.dir, "stray"))
        with open(os.path.join(self.dir, "stray", "training_meta.json"), "w") as handle:
            json.dump({"num_samples": 200}, handle)
        self.assertEqual(1, self.run_audit()["summary"]["adapters"])

    def test_tampered_weights_are_caught_when_the_hash_is_verified(self):
        self.adapter("honest", 180)
        self.adapter("swapped", 180, meta_hash="0" * 64)          # metadata describes some other weights
        self.adapter("nohash", 180, meta_hash=None)               # nothing to verify against
        summary = self.run_audit(verify_hash=True)["summary"]
        self.assertEqual(["swapped"], summary["weights_do_not_match_meta"])
        self.assertEqual(["nohash"], summary["weights_unverifiable"])

    def test_output_is_sorted_so_reruns_diff_cleanly(self):
        for name in ("b", "c", "a"):
            self.adapter(name, 200)
        self.assertEqual(["a", "b", "c"], self.run_audit()["summary"]["all_clips"])

    def test_cli_fails_loudly_when_an_adapter_cannot_be_classified(self):
        self.adapter("fine", 180)
        self.manifest.append({"id": "ghost", "sample_count": 180})
        with open(os.path.join(self.dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(self.manifest, handle)
        with patch.object(sys, "argv", ["audit", "--models-dir", self.dir]):
            with patch("builtins.print"):
                with self.assertRaises(SystemExit) as caught:
                    aud.main()
        self.assertEqual(2, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
