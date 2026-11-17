"""Dataset diagnostics retain extracted audio only while the metric owns it."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from experiments import dataset_speaker_consistency, ref_clip_match
from tests.test_upload_event_loop import wav_bytes


class ExperimentAudioCleanupTests(unittest.TestCase):
    def test_experiment_basename_expressions_handle_native_trailing_separators(self):
        experiments = Path(ref_clip_match.__file__).parent
        expected = {
            'build_unseen_holdout.py': 1, 'gate_view.py': 1,
            'ref_clip_match.py': 1, 'ref_clip_edges.py': 1,
            'text_audio_alignment_pilot.py': 1,
            'verify_adapter_identity.py': 1, 'voice_drift.py': 2,
        }
        for filename, count in expected.items():
            tree = ast.parse((experiments / filename).read_text(encoding='utf-8'))
            expressions = [node for node in ast.walk(tree)
                           if isinstance(node, ast.Call)
                           and ast.unparse(node.func) == 'os.path.basename'
                           and len(node.args) == 1
                           and isinstance(node.args[0], ast.Call)
                           and ast.unparse(node.args[0].func) in
                           ('os.path.normpath', 'd.rstrip', 'adapter.rstrip',
                            'source_dir.rstrip', 'args.source_dir.rstrip', 'args.adapter.rstrip')]
            self.assertEqual(count, len(expressions), filename)
            for expression in expressions:
                code = compile(ast.Expression(expression), filename, 'eval')
                for suffix in ('', '/', os.sep, os.sep * 2):
                    with self.subTest(filename=filename, line=expression.lineno, suffix=suffix):
                        path = str(Path('models') / 'voice') + suffix
                        args = SimpleNamespace(adapter=path, source_dir=path)
                        self.assertEqual('voice', eval(code, {
                            'os': os, 'd': path, 'adapter': path, 'source_dir': path, 'args': args}))

    def check_cli(self, module, case):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root / "models"
            adapter = models / "voice"
            adapter.mkdir(parents=True)
            audio = wav_bytes()
            (adapter / "ref_sample.wav").write_bytes(audio)
            (adapter / "training_meta.json").write_text(json.dumps({"ref_sample_audio": "/dataset/ref.wav"}))
            zips = root / "zips"
            zips.mkdir()
            archive = zips / "dataset.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                if case != "no_metadata":
                    bundle.writestr("metadata.jsonl", "\n".join(json.dumps({
                        "audio_filepath": f"clip{i}.wav", "text": "fixture"}) for i in range(2)))
                bundle.writestr("clip0.wav", audio)
                bundle.writestr("clip1.wav", audio)
            fidelity = root / "fidelity.json"
            fidelity.write_text(json.dumps({"results": [{"adapter": "voice", "dataset": "dataset", "ecapa": 0.5}]}))
            before = (archive.read_bytes(), (adapter / "ref_sample.wav").read_bytes())
            out = root / "result.json"
            allocated, measured = [], []
            real_mkdtemp = tempfile.mkdtemp

            def allocate(suffix=None, prefix=None, dir=None):
                path = real_mkdtemp(suffix=suffix, prefix=prefix, dir=str(root))
                allocated.append(Path(path))
                return path

            def ecapa(pairs):
                measured.extend(pairs)
                for pair in pairs:
                    for path in pair:
                        self.assertEqual(audio, Path(path).read_bytes())
                if case == "exception":
                    raise RuntimeError("fixture ECAPA exception")
                if case == "metric_error":
                    return None, "fixture metric error"
                return [0.75] * len(pairs), None

            args = ["diagnostic", "--zips", str(zips), "--fidelity", str(fidelity), "--out", str(out)]
            if module is ref_clip_match:
                args += ["--models", str(models)]
            with patch.object(tempfile, "mkdtemp", side_effect=allocate), \
                 patch.object(module, "ecapa", side_effect=ecapa), \
                 patch("experiments.provenance.provenance", return_value={"fixture": "CPU metric"}), \
                 patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()):
                if case == "exception":
                    with self.assertRaisesRegex(RuntimeError, "ECAPA exception"):
                        module.main()
                elif case in ("metric_error", "no_metadata"):
                    with self.assertRaises(SystemExit) as stopped:
                        module.main()
                    self.assertEqual(3, stopped.exception.code)
                else:
                    module.main()
            self.assertEqual(1, len(allocated))
            self.assertFalse(allocated[0].exists(), "extracted PCM outlived its metric owner")
            self.assertEqual(before, (archive.read_bytes(), (adapter / "ref_sample.wav").read_bytes()))
            if case == "no_metadata":
                self.assertEqual([], measured)
            else:
                self.assertTrue(measured)
            if case != "exception":
                doc = json.loads(out.read_text())
                self.assertEqual("voice", doc["results"][0]["adapter"])
                metric = "ref_vs_training" if module is ref_clip_match else "dataset_consistency"
                if case == "success":
                    self.assertEqual(0.75, doc["results"][0][metric])
                else:
                    self.assertTrue(doc["results"][0]["error"])

    def test_reference_match_cleanup_on_success_missing_metadata_metric_error_and_exception(self):
        for case in ("success", "no_metadata", "metric_error", "exception"):
            with self.subTest(case=case):
                self.check_cli(ref_clip_match, case)

    def test_dataset_consistency_cleanup_on_success_missing_metadata_metric_error_and_exception(self):
        for case in ("success", "no_metadata", "metric_error", "exception"):
            with self.subTest(case=case):
                self.check_cli(dataset_speaker_consistency, case)


if __name__ == "__main__":
    unittest.main()
