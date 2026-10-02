"""Known scores, actual file hashes/WAVs and native Bash cache reuse; no models."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import wave
import zipfile

from experiments import verify_adapter_identity as gate
from experiments import library_voice_fidelity as fidelity
from experiments import generation

REPO = Path(__file__).resolve().parents[2]


def write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        handle.writeframes(b'\x00\x10' * 240)


def make_inputs(root):
    adapter = root / "adapter"
    adapter.mkdir(parents=True)
    (adapter / "adapter_model.safetensors").write_bytes(b"known adapter bytes")
    (adapter / "adapter_config.json").write_text('{"r":64}')
    (adapter / "training_meta.json").write_text('{"ref_sample_text":"Reference speech"}')
    write_wav(adapter / "ref_sample.wav")
    (adapter / "ref_sample.txt").write_text("Reference speech")
    dataset = root / "data"
    rows = []
    for index in range(10):
        name = f"val/clip_{index}.wav"
        write_wav(dataset / name)
        rows.append(json.dumps({"audio_filepath": name, "text": f"Held-out {index}"}))
    (dataset / "val/metadata.jsonl").write_text("\n".join(rows) + "\n")
    return adapter, dataset


def measured_document(adapter, dataset, scores=None):
    scores = scores if scores is not None else [0.8] * 10
    median = statistics.median(scores)
    return {"adapter": os.path.relpath(adapter, gate.REPO), "median_ecapa": round(median, 4),
            "lines": len(scores), "generation_failures": 0, "threshold": 0.45,
            "passed": median >= 0.45, "ecapa_scores": scores,
            "measurement": {"schema_version": 1, "requested_lines": 10, "seed": 1234,
                            "inputs": gate.get_identity_gate_inputs(adapter, dataset, 10)}}


class IdentityCompletionTests(unittest.TestCase):
    def test_complete_positive_and_negative_results_are_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, dataset = make_inputs(root)
            out = root / "gate.json"
            for scores in ([0.8] * 10, [0.4499] * 10, [0.45] * 10):
                document = measured_document(adapter, dataset, scores)
                before = copy.deepcopy(document)
                out.write_text(json.dumps(document))
                result = gate.get_completed_identity_gate(out, adapter, dataset, 10)
                self.assertEqual(before, result)
                self.assertEqual(before, document)

    def test_rejects_partial_forged_wrong_scope_and_nonfinite_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, dataset = make_inputs(root)
            out = root / "gate.json"
            good = measured_document(adapter, dataset)
            changes = [lambda d: d.pop("measurement"),
                       lambda d: d.update(passed=False),
                       lambda d: d.update(median_ecapa=0.9),
                       lambda d: d.update(lines=True),
                       lambda d: d.update(generation_failures=1),
                       lambda d: d.update(threshold=0.7),
                       lambda d: d.update(adapter="other/adapter"),
                       lambda d: d["measurement"].update(seed=99),
                       lambda d: d["measurement"].update(schema_version=True),
                       lambda d: d["measurement"].update(requested_lines=9),
                       lambda d: d.update(ecapa_scores=[0.8]),
                       lambda d: d["ecapa_scores"].__setitem__(0, float("nan")),
                       lambda d: d["ecapa_scores"].__setitem__(0, float("inf")),
                       lambda d: d["ecapa_scores"].__setitem__(0, True),
                       lambda d: d["ecapa_scores"].__setitem__(0, 1.01)]
            for index, change in enumerate(changes):
                with self.subTest(case=index):
                    document = copy.deepcopy(good)
                    change(document)
                    out.write_text(json.dumps(document))
                    with self.assertRaises(ValueError):
                        gate.get_completed_identity_gate(out, adapter, dataset, 10)
            for contents in ('{"passed": true', '[]', '{"nested":{"passed":true}}'):
                out.write_text(contents)
                with self.assertRaises(ValueError):
                    gate.get_completed_identity_gate(out, adapter, dataset, 10)

    def test_same_path_input_byte_changes_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, dataset = make_inputs(root)
            out = root / "gate.json"
            files = [adapter / "adapter_model.safetensors", adapter / "adapter_config.json",
                     adapter / "training_meta.json", adapter / "ref_sample.wav", adapter / "ref_sample.txt",
                     dataset / "val/metadata.jsonl", dataset / "val/clip_0.wav"]
            for path in files:
                with self.subTest(file=path.name):
                    original = path.read_bytes()
                    out.write_text(json.dumps(measured_document(adapter, dataset)))
                    path.write_bytes(original + b" ")
                    with self.assertRaises(ValueError):
                        gate.get_completed_identity_gate(out, adapter, dataset, 10)
                    path.write_bytes(original)

    def test_zip_identity_is_bound_to_archive_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, dataset = make_inputs(root)
            archive = root / "dataset.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                for path in sorted(dataset.rglob("*")):
                    if path.is_file():
                        handle.write(path, path.relative_to(dataset).as_posix())
            out = root / "gate.json"
            out.write_text(json.dumps(measured_document(adapter, archive)))
            self.assertTrue(gate.get_completed_identity_gate(out, adapter, archive, 10)["passed"])
            archive.write_bytes(b"changed archive")
            with self.assertRaises(ValueError):
                gate.get_completed_identity_gate(out, adapter, archive, 10)

    def run_producer(self, adapter, dataset, out, scores, mutate=False, failure=False):
        real_open = open
        def open_config(path, *args, **kwargs):
            if str(path) == str(Path(gate.APP) / "config.json"):
                return io.StringIO('{}')
            return real_open(path, *args, **kwargs)
        renders = []
        def render(engine, text, instruct, speaker, config, entry, path):
            if failure and not renders:
                renders.append(path)
                raise generation.GenerationFailed("fixture render failure")
            write_wav(Path(path))
            renders.append(path)
            if mutate:
                (adapter / "adapter_model.safetensors").write_bytes(b"changed during generation")
        fake_tts = types.ModuleType("tts")
        fake_tts.TTSEngine = lambda config: object()
        args = ["identity", "--adapter", str(adapter), "--dataset", str(dataset),
                "--lines", "10", "--out", str(out)]
        with patch.object(sys, "argv", args), patch.dict(sys.modules, {"tts": fake_tts,
                "library_voice_fidelity": fidelity}), patch("builtins.open", side_effect=open_config), \
                patch.object(generation, "render", side_effect=render), \
                patch.object(fidelity, "ecapa_pairs", return_value=(scores, None)), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                gate.main()
        return result.exception.code, renders

    def test_real_producer_publishes_complete_scores_and_failed_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, dataset = make_inputs(root)
            out = root / "gate.json"
            for score, expected in ((0.8, 0), (0.4499, 3)):
                code, renders = self.run_producer(adapter, dataset, out, [score] * 10)
                self.assertEqual(expected, code)
                self.assertEqual(10, len(renders))
                document = gate.get_completed_identity_gate(out, adapter, dataset, 10)
                self.assertEqual([score] * 10, document["ecapa_scores"])
                for path in renders:
                    with wave.open(path) as handle:
                        self.assertEqual(240, handle.getnframes())
                        self.assertEqual(b'\x00\x10' * 240, handle.readframes(240))

    def test_real_zip_producer_extracts_ten_held_out_clips_and_records_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, dataset = make_inputs(root)
            archive = root / "dataset.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                for path in sorted(dataset.rglob("*")):
                    if path.is_file():
                        handle.write(path, path.relative_to(dataset).as_posix())
            out = root / "gate.json"
            code, renders = self.run_producer(adapter, archive, out, [0.8] * 10)
            self.assertEqual(0, code)
            self.assertEqual(10, len(renders))
            self.assertTrue(gate.get_completed_identity_gate(out, adapter, archive, 10)["passed"])
            for index in range(10):
                self.assertEqual((dataset / f"val/clip_{index}.wav").read_bytes(),
                                 (adapter / f"identity_check/clip_{index}.wav").read_bytes())

    def test_producer_refuses_partial_scores_generation_failure_and_input_race(self):
        for kind in ("partial", "extra", "null", "nan", "failure", "changed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                adapter, dataset = make_inputs(root)
                out = root / "gate.json"
                scores = [0.8] * (9 if kind == "partial" else 10)
                if kind == "extra":
                    scores.append(None)
                if kind == "null":
                    scores[0] = None
                if kind == "nan":
                    scores[0] = float("nan")
                code, _ = self.run_producer(adapter, dataset, out, scores,
                                           mutate=kind == "changed", failure=kind == "failure")
                self.assertNotEqual(0, code)
                self.assertFalse(out.exists())


class IdentityChainCacheTests(unittest.TestCase):
    def run_gate_loop(self, document_kind, *, worker_rc=0, corrupt_output=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "reference_rank1_all21/voice"
            adapter, dataset = make_inputs(base)
            experiments = root / "experiments"
            experiments.mkdir()
            out = experiments / "gate_reference_rank1__voice.json"
            good = measured_document(adapter, dataset)
            if document_kind == "negative":
                good = measured_document(adapter, dataset, [0.1] * 10)
            payload = json.dumps(good)
            if document_kind == "partial":
                payload = '{"passed":true,"lines":1}'
            elif document_kind == "nested":
                payload = '{"passed":false,"nested":{"passed": true}}'
            elif document_kind == "malformed":
                payload = '{"passed": true'
            elif document_kind == "stale":
                (adapter / "adapter_model.safetensors").write_bytes(b"new weights")
            out.write_text(payload)
            source_path = Path(os.environ.get("ALEXANDRIA_TEST_CHAIN_SOURCE",
                               str(REPO / "run_chains/remaining_gpu_research.sh")))
            source = source_path.read_text()
            start = source.index('gate_failures=0\n')
            loop = source[start:source.index('\nrelease_out=', start)]
            script = root / "probe.sh"
            # A harmless fixture producer; validation invokes the real CLI without inference.
            repaired = root / "repaired.json"
            repaired.write_text(json.dumps(measured_document(adapter, dataset)) if not corrupt_output
                                else '{"passed": true}')
            script.write_text('set -uo pipefail\n' +
                'repo="$1"; runtime="$2"; python="$3"; worker_rc="$4"\n' +
                'contaminated_adapters=(voice)\n' +
                'stage() { echo DISPATCH; cp "$runtime/repaired.json" "$gate_out"; return "$worker_rc"; }\n' +
                loop + '\necho "FAILURES=$gate_failures"\n')
            result = subprocess.run(["bash", str(script), str(REPO), str(root), sys.executable,
                                     str(worker_rc)], capture_output=True, text=True, timeout=10,
                                    env=dict(os.environ, PYTHONPATH=str(REPO) + ':' + str(REPO / 'app')))
            return result

    def test_valid_pass_and_measured_failure_are_reused_without_dispatch(self):
        for kind, failures in (("positive", 0), ("negative", 1)):
            with self.subTest(kind=kind):
                result = self.run_gate_loop(kind)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotIn("DISPATCH", result.stdout)
                self.assertIn(f"FAILURES={failures}", result.stdout)

    def test_partial_nested_malformed_and_stale_cache_trigger_worker(self):
        for kind in ("partial", "nested", "malformed", "stale"):
            with self.subTest(kind=kind):
                result = self.run_gate_loop(kind)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("DISPATCH", result.stdout)
                self.assertIn("FAILURES=0", result.stdout)

    def test_false_success_and_exit_verdict_disagreement_are_counted(self):
        for worker_rc in (3, 9):
            with self.subTest(worker_rc=worker_rc):
                result = self.run_gate_loop("partial", worker_rc=worker_rc if worker_rc == 3 else 0,
                                            corrupt_output=worker_rc == 9)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("DISPATCH", result.stdout)
                self.assertIn("FAILURES=1", result.stdout)

    def test_worker_failure_is_counted_even_if_old_file_looks_successful(self):
        result = self.run_gate_loop("partial", worker_rc=2)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DISPATCH", result.stdout)
        self.assertIn("FAILURES=1", result.stdout)
