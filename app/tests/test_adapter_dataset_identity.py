"""An adapter's dataset is recorded, not reconstructed from a path.

A retrain's reference sample lives at <campaign>/<adapter>/data/ref.wav, so the
directory holding it is the literal word "data" - and for two adapters "train".
`adapter_sources` inferred the dataset from that directory, so 56 of 75
adapters reported "data" as their identity, matched no zip, and were silently
dropped from every fidelity run. Seventeen seeds of replication therefore
covered 18 adapters while reading as though they covered the library, and
GOALS 2.7 recorded the cause as "their source zips are absent". The zips were
there the whole time: all 101 of them.

manifest.json records `dataset_id` per adapter. That is the authority (Rule 15),
and the reason it can be trusted is a control rather than an assumption: across
every adapter whose path-derived name was already a real dataset, the manifest
agrees 19/19 with none disagreeing.

Verified by the artifact, not by these tests: with the fix, 75 of 75 adapters
resolve to a dataset that finds its zip, against 19 before.
"""
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.library_voice_fidelity import (  # noqa: E402
    PLACEHOLDER_DATASETS, adapter_sources, get_manifest_dataset_ids)

MODELS = os.path.join(REPO, "lora_models")


def build_models_dir(root, adapters, manifest=None):
    """Write a models directory the way lora_models is laid out."""
    for name, ref in adapters.items():
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "training_meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"ref_sample_audio": ref}, fh)
    if manifest is not None:
        with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
    return root


class AdapterDatasetIdentityTest(unittest.TestCase):

    def test_a_placeholder_is_replaced_by_the_recorded_dataset_id(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            build_models_dir(root,
                             {"voice_a": "/x/campaign/voice_a/data/ref.wav"},
                             [{"id": "voice_a", "dataset_id": "narrator_real_name"}])
            got = {n: ds for n, ds, _ in adapter_sources(root)}
        self.assertEqual({"voice_a": "narrator_real_name"}, got)

    def test_a_placeholder_with_no_manifest_entry_yields_no_dataset(self):
        """It must not survive as "data". Reporting a placeholder as an
        identity is what made 56 adapters look measurable and score nothing."""
        import tempfile
        for placeholder in sorted(PLACEHOLDER_DATASETS):
            with self.subTest(placeholder), tempfile.TemporaryDirectory() as root:
                build_models_dir(root,
                                 {"voice_b": f"/x/campaign/voice_b/{placeholder}/ref.wav"},
                                 [])
                self.assertEqual([], adapter_sources(root))

    def test_an_adapter_absent_from_the_manifest_still_resolves_by_path(self):
        """No regression for the ones that already worked."""
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            build_models_dir(root,
                             {"voice_c": "/x/lora_datasets/narrator_from_path/ref.wav"},
                             [])
            got = {n: ds for n, ds, _ in adapter_sources(root)}
        self.assertEqual({"voice_c": "narrator_from_path"}, got)

    def test_a_missing_manifest_is_not_an_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            build_models_dir(root,
                             {"voice_d": "/x/lora_datasets/narrator_d/ref.wav"})
            self.assertEqual({}, get_manifest_dataset_ids(root))
            self.assertEqual([("voice_d", "narrator_d")],
                             [(n, ds) for n, ds, _ in adapter_sources(root)])

    def test_the_tracked_manifest_names_a_real_dataset_for_every_adapter(self):
        """manifest.json IS tracked (lora_models/ is otherwise gitignored), so
        this is the half of the repair CI can see. The adapters themselves live
        only on the training machine, which is why the 19/19 agreement control
        and the 19 -> 75 coverage measurement are recorded in the commit
        message rather than asserted here against data CI does not have."""
        path = os.path.join(MODELS, "manifest.json")
        self.assertTrue(os.path.isfile(path), "lora_models/manifest.json is tracked")
        with open(path, encoding="utf-8") as handle:
            entries = json.load(handle)
        self.assertGreaterEqual(len(entries), 75)
        bad = [e.get("id") for e in entries
               if not e.get("dataset_id") or e["dataset_id"] in PLACEHOLDER_DATASETS]
        self.assertEqual([], bad,
                         "these manifest entries carry no usable dataset_id, so "
                         "the substitution cannot rescue them: %s" % bad[:10])
        ids = [e["id"] for e in entries]
        self.assertEqual(len(ids), len(set(ids)), "duplicate adapter ids in manifest")


if __name__ == "__main__":
    unittest.main()
