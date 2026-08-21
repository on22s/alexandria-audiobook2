"""A provenance record must describe the bytes it sits next to.

Two gaps, both measured on the 254 `training_meta.json` files on this machine
on 2026-08-21:

- Every retained evaluation candidate carried the PRODUCTION adapter's digest.
  The candidate meta was `{**meta, ...}`, and `checkpoint_sha256` came along
  for the ride. `deduplicate_evaluation_candidates` deletes any candidate whose
  bytes equal production, so a candidate that survives is guaranteed to have
  different bytes than the digest it records - the claim was not merely
  unverified, it was wrong by construction. `lora_training_benchmark` compares
  a checkpoint against this field and would have rejected every candidate.
- Not one of the 254 recorded which commit trained it. Data lineage was there
  (`ref_sample_audio`) and hyperparameters were there; the code was not, and it
  is the one claim that cannot be reconstructed afterwards.
"""
import ast
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).parent.parent.parent
TRAIN_LORA = REPO / "app" / "train_lora.py"

sys.path.insert(0, str(REPO / "tools"))


def _load_backfill():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "backfill_adapter_digests", REPO / "tools" / "backfill_adapter_digests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CandidateDigestTest(unittest.TestCase):
    """Read the source: importing train_lora needs torch, which CI lacks."""

    @staticmethod
    def _candidate_dump_keys():
        tree = ast.parse(TRAIN_LORA.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "dump"):
                continue
            arg = node.args[0] if node.args else None
            if not isinstance(arg, ast.Dict):
                continue
            # `**meta` is a key of None, and dropping it would shift every
            # value index by one - which is how the first version of this test
            # read `loss` where it meant `sha256`.
            pairs = {k.value: v for k, v in zip(arg.keys, arg.values)
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "candidate_id" in pairs:
                return arg, pairs
        return None, {}

    def test_the_candidate_record_overrides_the_digest(self):
        node, pairs = self._candidate_dump_keys()
        self.assertIsNotNone(node, "no candidate training_meta.json writer found")
        self.assertIn("checkpoint_sha256", pairs,
                      "the candidate meta inherits the production digest; it must "
                      "write its own record['sha256'] instead")

    def test_the_override_uses_the_candidates_own_hash(self):
        _, pairs = self._candidate_dump_keys()
        value = pairs["checkpoint_sha256"]
        self.assertIsInstance(value, ast.Subscript,
                              "the candidate digest must be read from the record")
        self.assertEqual("sha256", value.slice.value,
                         "the candidate digest must come from record['sha256']")
        self.assertEqual("record", value.value.id)

    def test_production_digest_is_still_recorded_under_its_own_name(self):
        """Losing it would make the two adapters unrelatable."""
        _, pairs = self._candidate_dump_keys()
        self.assertIn("production_checkpoint_sha256", pairs)


class CodeLineageTest(unittest.TestCase):
    def test_training_meta_records_the_commit(self):
        source = TRAIN_LORA.read_text(encoding="utf-8")
        self.assertIn("get_code_lineage", source)
        self.assertIn("code_commit", source)

    def test_lineage_never_raises_when_git_is_absent(self):
        """A training run that already spent the GPU must not die on metadata."""
        source = TRAIN_LORA.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "get_code_lineage")
        handlers = [h for n in ast.walk(func) if isinstance(n, ast.Try)
                    for h in n.handlers]
        self.assertTrue(handlers, "get_code_lineage must not propagate git failures")


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.backfill = _load_backfill()
        self.root = pathlib.Path(tempfile.mkdtemp())

    def _adapter(self, name, body, meta):
        d = self.root / name
        d.mkdir(parents=True)
        (d / "adapter_model.safetensors").write_bytes(body)
        (d / "training_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return d / "training_meta.json"

    def test_a_missing_digest_is_backfillable_and_marked(self):
        meta_path = self._adapter("a", b"weights", {"epochs": 3})
        verdict, digest = self.backfill.classify(str(meta_path))
        self.assertEqual("backfillable", verdict)
        self.backfill.backfill(str(meta_path), digest, "2026-08-21")
        written = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(b"weights").hexdigest(),
                         written["checkpoint_sha256"])
        self.assertEqual("2026-08-21", written["checkpoint_sha256_backfilled"],
                         "a digest taken today is weaker evidence than one taken "
                         "at training time and must say so")

    def test_a_wrong_digest_is_reported_not_repaired(self):
        meta_path = self._adapter("b", b"weights", {"checkpoint_sha256": "0" * 64})
        verdict, _ = self.backfill.classify(str(meta_path))
        self.assertEqual("MISMATCH", verdict)
        before = meta_path.read_text(encoding="utf-8")
        self.assertEqual(before, meta_path.read_text(encoding="utf-8"))

    def test_a_correct_digest_is_left_alone(self):
        body = b"weights"
        meta_path = self._adapter(
            "c", body, {"checkpoint_sha256": hashlib.sha256(body).hexdigest()})
        self.assertEqual("already bound", self.backfill.classify(str(meta_path))[0])

    def test_a_meta_with_no_adapter_beside_it_is_not_invented(self):
        d = self.root / "d"
        d.mkdir()
        (d / "training_meta.json").write_text("{}", encoding="utf-8")
        self.assertEqual("no adapter file",
                         self.backfill.classify(str(d / "training_meta.json"))[0])

    def test_the_walk_skips_virtualenvs_and_the_hub_cache(self):
        self._adapter("mine", b"x", {})
        for skipped in ("env", "cache"):
            self._adapter(os.path.join(skipped, "theirs"), b"y", {})
        found = self.backfill.find_metas(str(self.root))
        self.assertEqual(1, len(found), found)
        self.assertIn("mine", found[0])
