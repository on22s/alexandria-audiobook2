import unittest
import json
from pathlib import Path
import tempfile

import numpy as np

from voice_clustering import cluster_voices, load_cluster_overrides


class VoiceClusteringTests(unittest.TestCase):
    def canonical(self, labels, clusters):
        return sorted(sorted(labels[index] for index in cluster) for cluster in clusters)

    def test_clustering_is_invariant_to_input_order(self):
        labels = ["b", "a", "c"]
        matrix = np.array([[1, .8, .2], [.8, 1, .3], [.2, .3, 1]])
        expected, _ = cluster_voices(labels, matrix, .45)
        permutation = [2, 0, 1]
        shuffled_labels = [labels[index] for index in permutation]
        shuffled = matrix[np.ix_(permutation, permutation)]
        actual, _ = cluster_voices(shuffled_labels, shuffled, .45)
        self.assertEqual(self.canonical(labels, expected),
                         self.canonical(shuffled_labels, actual))

    def test_complete_link_prevents_similarity_chain_overmerge(self):
        labels = ["a", "b", "c"]
        matrix = np.array([[1, .9, .2], [.9, 1, .9], [.2, .9, 1]])
        clusters, decisions = cluster_voices(labels, matrix, .45)
        self.assertEqual([["a", "b"], ["c"]], self.canonical(labels, clusters))
        self.assertEqual(1, len(decisions))

    def test_manual_merge_and_split_are_persistent_constraints(self):
        labels = ["a", "b", "c"]
        matrix = np.array([[1, .1, .9], [.1, 1, .9], [.9, .9, 1]])
        clusters, decisions = cluster_voices(
            labels, matrix, .45, {"merge": [["a", "b"]], "split": [["b", "c"]]})
        self.assertEqual([["a", "b"], ["c"]], self.canonical(labels, clusters))
        self.assertEqual("manual_merge", decisions[0]["type"])

    def test_conflicting_and_unknown_overrides_fail_loud(self):
        matrix = np.eye(2)
        with self.assertRaisesRegex(ValueError, "conflict"):
            cluster_voices(["a", "b"], matrix, .45,
                           {"merge": [["a", "b"]], "split": [["a", "b"]]})
        with self.assertRaisesRegex(ValueError, "unknown"):
            cluster_voices(["a", "b"], matrix, .45,
                           {"merge": [], "split": [["a", "missing"]]})

    def test_override_file_is_narrator_scoped_and_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "cluster_overrides.json")
            path.write_text(json.dumps({
                "version": 1,
                "narrators": {"Narrator": {"merge": [["one", "two"]], "split": []}},
            }), encoding="utf-8")
            self.assertEqual([["one", "two"]],
                             load_cluster_overrides(path, "Narrator")["merge"])
            self.assertEqual({"merge": [], "split": []},
                             load_cluster_overrides(path, "Different"))


if __name__ == "__main__":
    unittest.main()
