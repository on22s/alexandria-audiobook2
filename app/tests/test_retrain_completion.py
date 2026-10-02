"""Actual CPU-only no-source/resume runs and durable checkpoint semantics."""
import contextlib
import copy
import io
import json
import shutil
import types
import wave
import zipfile
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments import retrain_honest as retrain
from experiments import library_voice_fidelity as fidelity


def measured(name="good", score=0.7, n=8):
    return dict(adapter=name, role="failure", new_ecapa_heldout=score,
                dur_ratio=1.0, n=n, ecapa_error=None)


def write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        handle.writeframes(b"\x00\x10" * 1600)


def make_measured_files(module, root, args, row):
    name = row["adapter"]
    base = Path(args.work) / name
    source = Path(args.models) / name / "training_meta.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    dataset = name + "dataset"
    source.write_text(json.dumps({"ref_sample_audio": str(root / dataset / "ref.wav")}))
    records = []
    for index in range(args.eval_lines):
        human, generated = base / "val" / f"clip_{index}.wav", base / f"gen_{index}.wav"
        write_wav(human)
        write_wav(generated)
        records.append(dict(audio_filepath=f"val/clip_{index}.wav", text="Held-out speech"))
    archive = Path(args.zips) / (dataset + ".zip")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("val/metadata.jsonl", "\n".join(json.dumps(record) for record in records))
        for index in range(args.eval_lines):
            handle.write(base / "val" / f"clip_{index}.wav", f"val/clip_{index}.wav")
    adapter = base / "adapter"
    adapter.mkdir(exist_ok=True)
    (adapter / "adapter_model.safetensors").write_bytes(b"known fixture weights, no model load")
    (adapter / "adapter_config.json").write_text('{"r":64}')
    (adapter / "training_meta.json").write_text('{"ref_sample_text":"Reference speech"}')
    write_wav(adapter / "ref_sample.wav")
    data = base / "data"
    data.mkdir(exist_ok=True)
    (data / "metadata.jsonl").write_text("\n".join(json.dumps(record) for record in records))
    for index in range(args.eval_lines):
        write_wav(data / "val" / f"clip_{index}.wav")
    row.update(dataset=dataset, ecapa_scores=[row["new_ecapa_heldout"]] * args.eval_lines,
               duration_ratios=[1.0] * args.eval_lines,
               scored_pairs=[[str(base / "val" / f"clip_{index}.wav"), str(base / f"gen_{index}.wav")]
                             for index in range(args.eval_lines)])
    row["measurement"] = dict(schema_version=1, settings=module.get_retrain_settings(args),
                              inputs=module.get_retrain_input_hashes(args, name))
    return row


class RetrainCompletionTests(unittest.TestCase):
    def test_resume_retains_full_measurements_but_retries_failed_partial_and_unmeasured_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            good, low = measured(), measured("low", -0.1)
            rows = [good, low, {"adapter": "failed", "error": "train rc=1"},
                    measured("partial", n=1), measured("none", score=None),
                    measured("nan", score=float("nan")), measured("inf", score=float("inf")),
                    measured("bool", score=True), measured("bad-range", score=1.1),
                    {**measured("ecapa-failed"), "ecapa_error": "missing scorer"},
                    {**measured("bad-duration"), "dur_ratio": 0.0}]
            path.write_text(json.dumps(dict(seed=1234, reference_rank=1, results=rows)))
            before = path.read_bytes()
            self.assertEqual([good, low], retrain.load_resumed_results(path, True, 1234, 1, 8))
            self.assertEqual(before, path.read_bytes())

    def test_checkpoint_completion_covers_controls_and_only_fully_measured_rows(self):
        good, control = measured(), measured("control", 0.2)
        failure = {"adapter": "missing", "error": "source zip not found"}
        cases = [([good, failure], ["good", "missing"], [], (2, 1, False)),
                 ([good], ["good"], ["control"], (2, 1, False)),
                 ([good, control], ["good"], ["control"], (2, 2, True)),
                 ([measured("good", n=1)], ["good"], [], (1, 0, False))]
        for rows, adapters, controls, expected in cases:
            with self.subTest(expected=expected):
                before = copy.deepcopy(rows)
                result = retrain.get_retrain_completion(rows, adapters, controls, 8)
                self.assertEqual(dict(zip(("requested", "completed", "complete"), expected)), result)
                self.assertEqual(before, rows)
        with self.assertRaisesRegex(ValueError, "once"):
            retrain.get_retrain_completion([good, good], ["good"], [], 8)
        with self.assertRaisesRegex(ValueError, "once"):
            retrain.get_retrain_completion([good], ["good"], ["good"], 8)

    def test_malformed_or_duplicate_completed_resume_rows_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            for rows in ({"good": measured()}, [measured(), measured()]):
                with self.subTest(rows=rows):
                    path.write_text(json.dumps(dict(seed=1234, reference_rank=1, results=rows)))
                    with self.assertRaises(ValueError):
                        retrain.load_resumed_results(path, True, 1234, 1, 8)

    def test_resume_rejects_nonobject_and_boolean_strategy_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.json"
            for document in ([], {"seed": 1234, "reference_rank": True, "results": []},
                             {"seed": 1234.0, "reference_rank": 1, "results": []}):
                with self.subTest(document=document):
                    path.write_text(json.dumps(document))
                    with self.assertRaises(ValueError):
                        retrain.load_resumed_results(path, True, 1234, 1, 8)

    def run_no_source_producer(self, *, resumed=None, adapters=None, controls=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            (app / "experiments").mkdir(parents=True)
            (app / "experiments/voice_compare_view.py").write_text('# Native no-source fixture; rendering must not be called.\n')
            work, models, zips = root / "work", root / "models", root / "zips"
            models.mkdir()
            zips.mkdir()
            out = root / "result.json"
            real_app = Path(__file__).resolve().parents[1]
            for rel in ("train_lora.py", "tts.py", "voice_reference.py", "experiments/generation.py", "experiments/library_voice_fidelity.py"):
                shutil.copyfile(real_app / rel, app / rel)
            (app / "config.json").write_text('{}')
            settings = types.SimpleNamespace(models=str(models), zips=str(zips), work=str(work),
                epochs=6, lora_r=64, lora_alpha=128, seed=1234, reference_rank=1,
                eval_lines=8, use_medoid=False, medoid_clips=14)
            if resumed is not None:
                resumed = copy.deepcopy(resumed)
                with patch.object(retrain, "APP", str(app)):
                    for row in resumed:
                        if retrain.is_completed_retrain_row(row, 8):
                            make_measured_files(retrain, root, settings, row)
            if resumed is not None:
                out.write_text(json.dumps(dict(seed=1234, reference_rank=1, results=resumed)))
            args = ["retrain", "--adapters", *(adapters or ["missing"]), "--models", str(models),
                    "--zips", str(zips), "--work", str(work), "--out", str(out),
                    "--reference-rank", "1"]
            if controls:
                args.extend(["--controls", *controls])
            if resumed is not None:
                args.append("--resume")
            stream = io.StringIO()
            code = 0
            with patch.object(retrain, "REPO", str(root)), patch.object(retrain, "APP", str(app)), \
                    patch.object(sys, "argv", args), \
                    patch.dict(sys.modules, {"library_voice_fidelity": fidelity}), \
                    patch.object(retrain.subprocess, "run", side_effect=AssertionError("unexpected training")), \
                    contextlib.redirect_stdout(stream):
                try:
                    retrain.main()
                except SystemExit as result:
                    code = result.code
            if str(app / "experiments") in sys.path:
                sys.path.remove(str(app / "experiments"))
            document = json.loads(out.read_text())
            return code, document, stream.getvalue()

    def test_actual_failed_no_source_run_persists_failure_without_claiming_completion(self):
        code, document, output = self.run_no_source_producer()
        self.assertEqual(3, code, output)
        self.assertEqual(False, document["complete"])
        self.assertEqual(1, document["requested"])
        self.assertEqual(0, document["completed"])
        self.assertEqual("source zip not found", document["results"][0]["error"])
        self.assertIn("NO ZIP", output)

    def test_actual_resume_retries_failed_adapter_and_retains_completed_measurement(self):
        code, document, output = self.run_no_source_producer(
            resumed=[measured(), {"adapter": "missing", "error": "previous failed attempt"}],
            adapters=["good", "missing"])
        self.assertEqual(3, code, output)
        self.assertIn("RESUMED", output)
        self.assertIn("NO ZIP", output)
        self.assertEqual(2, document["requested"])
        self.assertEqual(1, document["completed"])
        self.assertFalse(document["complete"])
        self.assertEqual(2, len(document["results"]))
        self.assertEqual("source zip not found", document["results"][1]["error"])
        self.assertEqual(0.7, document["results"][0]["new_ecapa_heldout"])

    def test_complete_low_score_resume_does_not_repeat_training(self):
        code, document, output = self.run_no_source_producer(resumed=[measured("good", -0.1)], adapters=["good"])
        self.assertEqual(0, code, output)
        self.assertTrue(document["complete"])
        self.assertEqual(1, document["completed"])
        self.assertIn("RESUMED", output)
        self.assertNotIn("NO ZIP", output)

    def test_missing_control_prevents_a_successful_partial_campaign(self):
        code, document, output = self.run_no_source_producer(resumed=[measured()], adapters=["good"], controls=["missing"])
        self.assertEqual(3, code, output)
        self.assertFalse(document["complete"])
        self.assertEqual(["missing"], document["requested_controls"])
        self.assertEqual(2, document["requested"])
        self.assertEqual(1, document["completed"])
