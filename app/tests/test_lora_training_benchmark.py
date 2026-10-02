import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lora_training_benchmark import execute_fixture


class LoraTrainingBenchmarkTests(unittest.TestCase):
    def test_returned_adapter_and_metadata_links_refuse_before_metrics_publication(self):
        import subprocess
        for mode in ('metadata', 'adapter'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(tmp)
                outside_adapter = Path(tmp, 'outside-adapter')
                outside_adapter.write_bytes(b'outside weights')
                metadata = {'training_time_seconds': 2.0, 'num_samples': 1, 'epochs': 1,
                            'final_loss': 1.0, 'best_loss': 1.0, 'oom_skips': 0,
                            'checkpoint_sha256': hashlib.sha256(outside_adapter.read_bytes()).hexdigest()}
                outside_metadata = Path(tmp, 'outside-metadata.json')
                outside_metadata.write_text(json.dumps(metadata))
                before = (outside_adapter.read_bytes(), outside_metadata.read_bytes())
                def train(command, **kwargs):
                    output = Path(command[command.index('--output_dir') + 1])
                    output.mkdir()
                    adapter = output / 'adapter_model.safetensors'
                    meta = output / 'training_meta.json'
                    if mode == 'metadata':
                        adapter.write_bytes(outside_adapter.read_bytes())
                        meta.symlink_to(outside_metadata)
                    else:
                        adapter.symlink_to(outside_adapter)
                        meta.write_bytes(outside_metadata.read_bytes())
                    return subprocess.CompletedProcess(command, 0, '', '')
                with patch('lora_training_benchmark.subprocess.run', side_effect=train) as dispatch:
                    with self.assertRaisesRegex(ValueError, 'symlink'):
                        execute_fixture(fixture, 'fixture-python', 'fixture-train', str(Path(tmp, 'out')))
                dispatch.assert_called_once()
                self.assertEqual((outside_adapter.read_bytes(), outside_metadata.read_bytes()), before)

    def test_existing_output_symlink_cannot_redirect_training_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            output_root = Path(tmp, 'out')
            output_root.mkdir()
            outside = Path(tmp, 'outside')
            outside.mkdir()
            proof = outside / 'prior-adapter'
            proof.write_bytes(b'preserve')
            (output_root / fixture['id']).symlink_to(outside, target_is_directory=True)
            with patch('lora_training_benchmark.subprocess.run', side_effect=AssertionError('redirected output admitted')) as run:
                with self.assertRaises(ValueError):
                    execute_fixture(fixture, 'fixture-python', 'fixture-training', str(output_root))
            run.assert_not_called()
            self.assertEqual(proof.read_bytes(), b'preserve')
            self.assertTrue((output_root / fixture['id']).is_symlink())

    def test_worker_refuses_outside_dataset_and_metadata_before_dispatch_or_output(self):
        for mode in ('absolute-dataset', 'parent-dataset', 'metadata-link'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(tmp)
                dataset = Path(tmp, 'dataset')
                if mode == 'metadata-link':
                    outside = Path(tmp, 'outside.jsonl')
                    outside.write_bytes((dataset / 'metadata.jsonl').read_bytes())
                    (dataset / 'metadata.jsonl').unlink()
                    (dataset / 'metadata.jsonl').symlink_to(outside)
                else:
                    root = Path(tmp, 'declared-root')
                    root.mkdir()
                    fixture['root_dir'] = str(root)
                    fixture['dataset_path'] = str(dataset) if mode == 'absolute-dataset' else '../dataset'
                before = {str(p): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()}
                output = Path(tmp, 'output')
                with patch('lora_training_benchmark.subprocess.run', side_effect=AssertionError('outside fixture admitted')) as run:
                    with self.assertRaises(ValueError):
                        execute_fixture(fixture, 'fixture-python', 'fixture-train', str(output))
                run.assert_not_called()
                from benchmark_fixtures import build_lora_training_manifest, _hash_entries
                from benchmark_runner import _validate_lora_training_fixture
                with self.assertRaises(ValueError):
                    build_lora_training_manifest([{'dataset_path': fixture['dataset_path'],
                                                   'sample_count': 1}], fixture['root_dir'])
                keys = ('dataset_path', 'metadata_sha256', 'sample_count', 'audio_sha256',
                        'epochs', 'seed', 'lr', 'lora_r', 'lora_alpha', 'grad_accum', 'language')
                fixture['sha256'] = _hash_entries({key: fixture[key] for key in keys})
                with self.assertRaises(ValueError):
                    _validate_lora_training_fixture(fixture, fixture['root_dir'])
                self.assertFalse(output.exists())
                self.assertEqual(before, {str(p): p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()})

    def _fixture(self, root):
        dataset = Path(root, "dataset")
        dataset.mkdir()
        audio = Path(dataset, "sample.wav")
        audio.write_bytes(b"audio")
        metadata = Path(dataset, "metadata.jsonl")
        metadata.write_text(
            '{"audio_filepath":"sample.wav","text":"Words."}\n', encoding="utf-8")
        return {"id": "calibration", "root_dir": root, "dataset_path": "dataset",
                "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
                "sample_count": 1,
                "audio_sha256": {"sample.wav": hashlib.sha256(b"audio").hexdigest()},
                "epochs": 1, "seed": 42, "lr": 1e-6, "lora_r": 8,
                "lora_alpha": 16, "grad_accum": 1, "language": "english"}

    def test_invalid_sample_count_rejected_before_training_or_output_changes(self):
        for count in (-1, 0, 1.5, "1", None):
            with self.subTest(sample_count=count), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(tmp)
                fixture["sample_count"] = count
                output = Path(tmp, "out", fixture["id"])
                output.mkdir(parents=True)
                (output / "adapter_model.safetensors").write_bytes(b"prior adapter")
                before = {p: p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()}
                with patch("lora_training_benchmark.subprocess.run") as run:
                    with self.assertRaisesRegex(ValueError, "sample_count must be positive"):
                        execute_fixture(fixture, "python", "train.py", str(output.parent))
                run.assert_not_called()
                self.assertEqual(before, {p: p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()})

    def test_manifest_uses_same_count_rule_and_keeps_default_and_short_dataset_error(self):
        from benchmark_fixtures import build_lora_training_manifest
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            before = {p: p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()}
            for count in (-1, 0, 1.5, "1", None):
                with self.subTest(sample_count=count):
                    with self.assertRaisesRegex(ValueError, "sample_count must be positive"):
                        build_lora_training_manifest([{"dataset_path": "dataset", "sample_count": count}], tmp)
            with self.assertRaisesRegex(ValueError, "too few samples"):
                build_lora_training_manifest([{"dataset_path": "dataset"}], tmp)
            for count in (1, True):
                manifest = build_lora_training_manifest([{"dataset_path": "dataset", "sample_count": count}], tmp)
                self.assertEqual(count, manifest["fixtures"][0]["sample_count"])
                self.assertEqual({"sample.wav"}, set(manifest["fixtures"][0]["audio_sha256"]))
            self.assertEqual(before, {p: p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()})

    def test_positive_counts_preserve_selected_membership_and_real_prepared_audio(self):
        import wave
        for count in (2, True):
            with self.subTest(sample_count=count), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(tmp)
                fixture["sample_count"] = count
                dataset = Path(tmp, "dataset")
                entries, hashes = [], {}
                for index in range(3):
                    filename = f"sample{index}.wav"
                    path = dataset / filename
                    with wave.open(str(path), "wb") as audio:
                        audio.setnchannels(1)
                        audio.setsampwidth(2)
                        audio.setframerate(16000)
                        audio.writeframes(bytes([index, 0]) * 320)
                    entries.append({"audio_filepath": filename, "text": f"Words {index}."})
                    hashes[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
                metadata = dataset / "metadata.jsonl"
                metadata.write_text("".join(json.dumps(row) + "\n" for row in entries))
                fixture.update(metadata_sha256=hashlib.sha256(metadata.read_bytes()).hexdigest(), audio_sha256=hashes)
                before = {p: p.read_bytes() for p in dataset.iterdir()}
                fixture_before = json.dumps(fixture, sort_keys=True)
                captured = []
                def train(command, **kwargs):
                    prepared = Path(command[command.index("--data_dir") + 1])
                    selected = [json.loads(line) for line in (prepared / "metadata.jsonl").read_text().splitlines()]
                    captured.extend(selected)
                    for row in selected:
                        filename = row["audio_filepath"]
                        self.assertEqual(before[dataset / filename], (prepared / filename).read_bytes())
                    self.assertEqual({"metadata.jsonl", *(row["audio_filepath"] for row in selected)},
                                     {p.name for p in prepared.iterdir()})
                    output = Path(command[command.index("--output_dir") + 1])
                    output.mkdir(parents=True)
                    (output / "adapter_model.safetensors").write_bytes(b"CPU subprocess fixture")
                    (output / "training_meta.json").write_text(json.dumps({
                        "training_time_seconds": 2.0, "num_samples": len(selected), "epochs": 1,
                        "final_loss": 4.8, "best_loss": 4.8, "oom_skips": 0,
                        "checkpoint_sha256": hashlib.sha256(b"CPU subprocess fixture").hexdigest()}))
                    return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                with patch("lora_training_benchmark.subprocess.run", side_effect=train) as run:
                    metrics = execute_fixture(fixture, "python", "train.py", str(Path(tmp, "output")))
                run.assert_called_once()
                self.assertEqual(entries[:count], captured)
                self.assertEqual(len(captured), metrics["num_samples"])
                self.assertEqual(before, {p: p.read_bytes() for p in dataset.iterdir()})
                self.assertEqual(fixture_before, json.dumps(fixture, sort_keys=True))

    def test_execute_fixture_rejects_changed_audio_before_training(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            Path(tmp, "dataset", "sample.wav").write_bytes(b"changed")
            with patch("lora_training_benchmark.subprocess.run") as run, \
                 self.assertRaisesRegex(ValueError, "audio hash changed"):
                execute_fixture(fixture, "python", "train.py", str(Path(tmp, "out")))
        run.assert_not_called()

    def test_fixture_id_cannot_delete_outside_output_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            fixture["id"] = "../keep"
            keep = Path(tmp, "keep")
            keep.mkdir()
            (keep / "important").write_text("present")
            with patch("lora_training_benchmark.subprocess.run") as run, \
                 self.assertRaisesRegex(ValueError, "fixture id"):
                execute_fixture(fixture, "python", "train.py", str(Path(tmp, "out")))
            self.assertEqual("present", (keep / "important").read_text())
        run.assert_not_called()

    def test_metadata_audio_cannot_escape_temporary_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            outside = Path(tmp, "outside.wav")
            outside.write_bytes(b"outside")
            metadata = Path(tmp, "dataset", "metadata.jsonl")
            metadata.write_text('{"audio_filepath":"../outside.wav"}\n')
            fixture["metadata_sha256"] = hashlib.sha256(metadata.read_bytes()).hexdigest()
            with patch("lora_training_benchmark.subprocess.run") as run, \
                 self.assertRaisesRegex(ValueError, "unsafe training audio path"):
                execute_fixture(fixture, "python", "train.py", str(Path(tmp, "out")))
            self.assertEqual(b"outside", outside.read_bytes())
        run.assert_not_called()

    def test_execute_fixture_verifies_produced_adapter_and_reports_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture(tmp)
            output_root = Path(tmp, "output")

            def fake_run(command, **kwargs):
                output_dir = Path(command[command.index("--output_dir") + 1])
                output_dir.mkdir(parents=True)
                adapter = Path(output_dir, "adapter_model.safetensors")
                adapter.write_bytes(b"adapter")
                Path(output_dir, "training_meta.json").write_text(json.dumps({
                    "training_time_seconds": 2.0, "num_samples": 1, "epochs": 1,
                    "final_loss": 4.8, "best_loss": 4.8, "oom_skips": 0,
                    "checkpoint_sha256": hashlib.sha256(b"adapter").hexdigest()}))
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("lora_training_benchmark.subprocess.run", side_effect=fake_run):
                metrics = execute_fixture(fixture, "python", "train.py", str(output_root))
        self.assertEqual(0.5, metrics["samples_per_second"])
        self.assertEqual(0, metrics["oom_skips"])


if __name__ == "__main__":
    unittest.main()


class TrainingHashCoverageTests(unittest.TestCase):
    _fixture = LoraTrainingBenchmarkTests._fixture

    def test_every_selected_clip_requires_an_expected_hash_before_output_changes(self):
        for key in ('audio_filepath', 'audio'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                fixture = self._fixture(tmp)
                metadata = Path(tmp, 'dataset/metadata.jsonl')
                metadata.write_text(json.dumps({key: 'sample.wav', 'text': 'Words.'})+'\n')
                fixture['metadata_sha256'] = hashlib.sha256(metadata.read_bytes()).hexdigest()
                fixture['audio_sha256'] = {}
                output = Path(tmp, 'out/calibration')
                output.mkdir(parents=True)
                (output/'adapter_model.safetensors').write_bytes(b'prior adapter')
                before = {p: p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()}
                with patch('lora_training_benchmark.subprocess.run') as run:
                    with self.assertRaisesRegex(ValueError, 'unverified audio'):
                        execute_fixture(fixture, 'python', 'train.py', str(Path(tmp, 'out')))
                run.assert_not_called()
                self.assertEqual(before, {p: p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()})
