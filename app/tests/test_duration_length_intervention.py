import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from experiments.duration_length_intervention import (build_cache_identity,
                                                       build_short_pairs, summarize)


class DurationLengthInterventionTests(unittest.TestCase):
    def test_pairs_are_deterministic_and_use_the_shortest_complete_rows(self):
        rows = [{"id": name, "text": text, "clone_wav": wav}
                for name, text, wav in (("c", "123", "c.wav"),
                                        ("a", "1", "a.wav"),
                                        ("b", "12", "b.wav"),
                                        ("x", "", None))]
        pairs = build_short_pairs(rows, 1)
        self.assertEqual(["a", "b"], [row["id"] for row in pairs[0]])

    def test_summary_measures_matched_improvement_toward_one(self):
        summary = summarize([
            {"separate_ratio": 0.7, "grouped_ratio": 0.9},
            {"separate_ratio": 1.1, "grouped_ratio": 1.3},
        ])
        self.assertEqual(1, summary["pairs_closer_to_one"])
        self.assertEqual(1, summary["pairs_farther_from_one"])
        self.assertEqual(0, summary["pairs_tied"])

    def test_summary_does_not_call_an_unchanged_pair_worse(self):
        summary = summarize([
            {"separate_ratio": 0.8, "grouped_ratio": 0.8},
            {"separate_ratio": 0.8, "grouped_ratio": 0.9},
            {"separate_ratio": 1.1, "grouped_ratio": 1.3},
        ])
        self.assertEqual(1, summary["pairs_closer_to_one"])
        self.assertEqual(1, summary["pairs_farther_from_one"])
        self.assertEqual(1, summary["pairs_tied"])

    def test_cache_identity_changes_with_reference_audio(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [os.path.join(root, name) for name in ("input", "build", "ref")]
            for path in paths:
                with open(path, "wb") as handle:
                    handle.write(b"one")
            args = SimpleNamespace(input=paths[0], build=paths[1], seed=1)
            build = {"ref_sample": paths[2], "ref_text": "hello"}
            def hashes(requested):
                result = {}
                for path in requested:
                    if os.path.exists(path):
                        with open(path, "rb") as handle:
                            payload = handle.read()
                    else:
                        payload = path.encode()
                    result[path] = __import__("hashlib").sha256(payload).hexdigest()
                return result
            with patch("experiments.duration_length_intervention.input_sha256",
                       side_effect=hashes):
                first = build_cache_identity(args, build)
                with open(paths[2], "wb") as handle:
                    handle.write(b"two")
                second = build_cache_identity(args, build)
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
