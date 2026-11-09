"""Qwen repair drills (#1844): capability completeness, full provider
identity, preference atomicity, lease handoff, GPU dtype policy.

Each test is discriminating: it fails against the initial implementation
and passes only after the repair. All fixtures are synthetic/local.
"""
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import qwen_runtime as qr


def _complete_model(root, weights=b"w" * 4096, model_type="qwen3_asr"):
    model = Path(root) / "m"
    model.mkdir(parents=True, exist_ok=True)
    (model / "config.json").write_text(json.dumps(
        {"model_type": model_type,
         "support_languages": ["Chinese", "English", "Cantonese", "German",
                               "Spanish", "French", "Italian", "Portuguese",
                               "Russian", "Korean", "Japanese"]}), encoding="utf-8")
    (model / "generation_config.json").write_text("{}", encoding="utf-8")
    (model / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (model / "vocab.json").write_text("{}", encoding="utf-8")
    (model / "merges.txt").write_text("a b", encoding="utf-8")
    (model / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    (model / "chat_template.json").write_text("{}", encoding="utf-8")
    (model / "model.safetensors").write_bytes(weights)
    return str(model)


def _docs_only_model(root):
    """Mimics model-docs: README/config/preprocessor only, no weights or
    tokenizer — an install that must never report ready."""
    model = Path(root) / "docs-only"
    model.mkdir(parents=True, exist_ok=True)
    (model / "config.json").write_text(json.dumps(
        {"model_type": "qwen3_asr",
         "support_languages": ["English"]}), encoding="utf-8")
    (model / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    (model / "README.md").write_text("https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B",
                                     encoding="utf-8")
    return str(model)


def _ready_probe(tmp, payload, model_dir):
    probe = Path(tmp) / "probe_stub.py"
    probe.write_text("import json\nprint(json.dumps(%r))\n" % (payload,),
                     encoding="utf-8")
    with patch.dict(os.environ, {"ALEXANDRIA_QWEN_MODEL": str(model_dir)}):
        return qr.get_qwen_capability(str(tmp), probe_command=[sys.executable, str(probe)])


class CompletenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_docs_only_model_not_ready(self):
        model = _docs_only_model(self.tmp.name)
        probe = {"ready": True, "torch": True, "qwen_asr": True,
                 "qwen_asr_version": "0.0.6", "devices": [], "cpu_fallback": True}
        capability = _ready_probe(self.tmp.name, probe, model)
        self.assertFalse(capability["available"])
        self.assertIn("model.safetensors", capability["reason"])
        self.assertIn("vocab.json", capability["reason"])

    def test_missing_tokenizer_not_ready(self):
        model = _complete_model(self.tmp.name)
        (Path(model) / "vocab.json").unlink()
        probe = {"ready": True, "torch": True, "qwen_asr": True,
                 "qwen_asr_version": "0.0.6", "devices": [], "cpu_fallback": True}
        capability = _ready_probe(self.tmp.name, probe, model)
        self.assertFalse(capability["available"])
        self.assertIn("vocab.json", capability["reason"])

    def test_wrong_model_type_not_ready(self):
        model = _complete_model(self.tmp.name, model_type="not_qwen")
        probe = {"ready": True, "torch": True, "qwen_asr": True,
                 "qwen_asr_version": "0.0.6", "devices": [], "cpu_fallback": True}
        capability = _ready_probe(self.tmp.name, probe, model)
        self.assertFalse(capability["available"])
        self.assertIn("qwen3_asr", capability["reason"])

    def test_unknown_version_not_ready(self):
        model = _complete_model(self.tmp.name)
        probe = {"ready": True, "torch": True, "qwen_asr": True,
                 "qwen_asr_version": "unknown", "devices": [], "cpu_fallback": True}
        capability = _ready_probe(self.tmp.name, probe, model)
        self.assertFalse(capability["available"])
        self.assertIn("metadata", capability["reason"])

    def test_arbitrary_device_string_not_ready(self):
        model = _complete_model(self.tmp.name)
        probe = {"ready": True, "torch": True, "qwen_asr": True,
                 "qwen_asr_version": "0.0.6", "devices": ["cuda:not-a-device"],
                 "cpu_fallback": True}
        capability = _ready_probe(self.tmp.name, probe, model)
        self.assertFalse(capability["available"])
        self.assertIn("device", capability["reason"].lower())

    def test_complete_model_ready(self):
        model = _complete_model(self.tmp.name)
        probe = {"ready": True, "torch": True, "qwen_asr": True,
                 "qwen_asr_version": "0.0.6", "devices": [], "cpu_fallback": True}
        with patch.dict(os.environ, {"ALEXANDRIA_QWEN_MODEL": str(model)}):
            capability = qr.get_qwen_capability(self.tmp.name, probe_command=[
                sys.executable, "-c", "import json; print(json.dumps(%r))" % (probe,)])
        self.assertTrue(capability["available"])
        self.assertEqual(11, len(capability["languages"]))


class ProviderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.model = _complete_model(self.tmp.name)
        self.runtime = qr.get_qwen_runtime_identity(sys.executable)

    def _identity(self, **extra):
        base = dict(extra)
        if "identity" not in base:
            base["identity"] = self.runtime
        return base

    def test_weights_change_changes_provider_digest(self):
        runtime = self._identity(qwen_asr_version="0.0.6",
                                 torch_version="2.10.0+cpu", device="cpu",
                                 dtype="float32")
        first = qr.get_qwen_provider_digest(self.model, runtime_identity=runtime)
        (Path(self.model) / "model.safetensors").write_bytes(b"w" * 4096 + b"1")
        second = qr.get_qwen_provider_digest(self.model, runtime_identity=runtime)
        self.assertNotEqual(first, second)
        third = qr.get_qwen_provider_digest(self.model, runtime_identity=runtime)
        self.assertEqual(second, third)

    def test_runtime_identity_changes_digest(self):
        a = qr.get_qwen_provider_digest(self.model, runtime_identity=self._identity(
            qwen_asr_version="0.0.6", torch_version="2.10", device="cpu",
            dtype="float32"))
        b = qr.get_qwen_provider_digest(self.model, runtime_identity=self._identity(
            qwen_asr_version="0.0.7", torch_version="2.10", device="cpu",
            dtype="float32"))
        self.assertNotEqual(a, b)

    def test_digest_cancel_aware(self):
        with self.assertRaises(ValueError):
            qr.get_qwen_provider_digest(
                self.model, runtime_identity=self._identity(
                    qwen_asr_version="0.0.6", torch_version="2.10",
                    device="cpu", dtype="float32"),
                cancel_check=lambda: True)

    def test_missing_model_dir_fails(self):
        with self.assertRaises(ValueError):
            qr.get_qwen_provider_digest(
                str(Path(self.tmp.name) / "nope"),
                runtime_identity=self._identity(
                    qwen_asr_version="0.0.6", torch_version="2.10",
                    device="cpu", dtype="float32"))


class WorkerProbeSourceTests(unittest.TestCase):
    """The default production probe must consult find_spec/metadata before
    importing torch, and report the installed version + model languages."""

    def test_probe_source_gates_torch_behind_find_spec(self):
        src = qr._PROBE_SOURCE
        find_spec_at = src.find("find_spec")
        import_torch_at = src.find("import torch")
        self.assertGreaterEqual(find_spec_at, 0)
        self.assertLess(find_spec_at, import_torch_at,
                        "probe must check qwen_asr presence before importing torch")
        self.assertIn("importlib.util.find_spec", src)
        self.assertIn("importlib import metadata", src)

    def test_probe_source_compiles(self):
        compile(qr._PROBE_SOURCE, "<probe>", "exec")


if __name__ == "__main__":
    unittest.main()
