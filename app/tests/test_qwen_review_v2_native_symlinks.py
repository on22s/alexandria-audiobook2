"""Native symlink availability report (#1844 follow-up, SEPARATELY QUALIFIED).

This module runs in a separate POSIX CI step requiring zero skips,
outside the portable suite. Manual runs attempt to
create real symlinks and either exercises native no-follow behavior
where the OS permits it, or skips with the exact OS error. Native
symlink behavior is never claimed where creation is unavailable;
the portable fault-injected predicate tests live in
test_qwen_review_v2_repairs.py and always run.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _model(tmp):
    """COMPLETE valid fixture: every required file present."""
    import qwen_runtime as _qr
    model = Path(tmp) / "model"
    model.mkdir()
    (model / "config.json").write_text(json.dumps(
        {"model_type": "qwen3_asr", "thinker_config": {"model_type": "qwen3_forced_aligner"}, "support_languages": ["English"]}),
        encoding="utf-8")
    (model / "model.safetensors").write_bytes(b"12345678")
    for name in _qr._REQUIRED_MODEL_FILES:
        path = model / name
        if not path.exists():
            path.write_text("{}", encoding="utf-8")
    return model


class NativeSymlinkTests(unittest.TestCase):
    def test_venv_symlink_entry_survives_identity_revalidation_native(self):
        import subprocess
        import qwen_runtime as qr
        if os.name == "nt":
            self.skipTest("POSIX venv symlink qualification; run the separate Linux/macOS step")
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "qwen-env"
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--symlinks", str(env)],
                           check=True, timeout=60, capture_output=True)
            entry = env / "bin" / "python"
            self.assertTrue(entry.is_symlink(), "POSIX qualification must exercise a real venv symlink")
            site = subprocess.check_output(
                [str(entry), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                text=True, timeout=30).strip()
            metadata = Path(site) / "qwen_asr-0.0.6.dist-info" / "METADATA"
            metadata.parent.mkdir(parents=True)
            metadata.write_text("Metadata-Version: 2.1\nName: qwen-asr\nVersion: 0.0.6\n", encoding="utf-8")
            first = qr.get_qwen_runtime_identity(str(entry))
            second = qr.get_qwen_runtime_identity(first["python"])
            self.assertEqual(str(entry), first["python"])
            self.assertEqual("0.0.6", first["packages"]["qwen-asr"])
            self.assertEqual(first["runtime_digest"], second["runtime_digest"])
            metadata.write_text("Metadata-Version: 2.1\nName: qwen-asr\nVersion: 0.0.7\n", encoding="utf-8")
            changed = qr.get_qwen_runtime_identity(first["python"])
            self.assertNotEqual(first["runtime_digest"], changed["runtime_digest"])

    def test_symlinked_required_file_not_ready_native(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = _model(tmp)
            target = Path(tmp) / "elsewhere.json"
            target.write_text("{}", encoding="utf-8")
            link = model / "vocab.json"
            link.unlink()
            try:
                os.symlink(str(target), str(link))
            except (OSError, NotImplementedError) as exc:
                self.skipTest("native symlink unavailable here: %s" % exc)
            with patch.object(qr, "get_qwen_python_candidates",
                              lambda _r: [sys.executable]), \
                    patch.object(qr, "get_qwen_model_candidates",
                              lambda _r: [str(model)]), \
                    patch.object(qr, "run_owned_capture") as run_cap:
                class Completed:
                    returncode = 0
                    stdout = json.dumps(
                        {"ready": True, "qwen_asr_version": "0.0.6",
                         "torch_version": "x", "devices": [],
                         "cpu_fallback": True}).encode("utf-8")
                run_cap.return_value = Completed()
                cap = qr.get_qwen_capability(tmp, status_only=True)
            self.assertFalse(cap["available"])
            with self.assertRaises(ValueError):
                qr.get_qwen_model_file_digest(str(model))

    def test_symlinked_directory_rejected_native(self):
        import qwen_runtime as qr
        with tempfile.TemporaryDirectory() as tmp:
            model = _model(tmp)
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "extra.bin").write_bytes(b"evil")
            try:
                os.symlink(str(outside), str(model / "linked"))
            except (OSError, NotImplementedError) as exc:
                self.skipTest("native symlink unavailable here: %s" % exc)
            with self.assertRaises(ValueError):
                qr.get_qwen_model_file_digest(str(model))

    def test_symlinked_preference_refused_native(self):
        from readalong_export import get_readaloud_timing_preference
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real.json"
            target.write_text(json.dumps(
                {"timing_mode": "chunk", "timing_device": "cpu",
                 "timing_language": None}), encoding="utf-8")
            try:
                os.symlink(str(target), str(Path(tmp) / "readalong_timing.json"))
            except (OSError, NotImplementedError) as exc:
                self.skipTest("native symlink unavailable here: %s" % exc)
            with self.assertRaises(ValueError):
                get_readaloud_timing_preference(tmp)


if __name__ == "__main__":
    unittest.main()
