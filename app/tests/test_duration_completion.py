"""Native duration artifacts/CLI/Bash guards on known PCM pairs, without TTS."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import wave

REPO = Path(__file__).resolve().parents[2]


def write_wav(path, frames=2400):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        handle.writeframes(b'\x00\x10' * frames)


class DurationCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        app = self.root / "app"
        (app / "experiments").mkdir(parents=True)
        for rel in ("experiments/duration_length_intervention.py", "tts.py", "experiments/generation.py"):
            shutil.copyfile(REPO / "app" / rel, app / rel)
        (app / "config.json").write_text('{}')
        spec = importlib.util.spec_from_file_location("fixture_duration", app / "experiments/duration_length_intervention.py")
        self.duration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.duration)
        self.addCleanup(lambda: sys.path.remove(str(app)) if str(app) in sys.path else None)
        self.runtime = self.root / "ab_test_runtime"
        (self.runtime / "experiments").mkdir(parents=True)
        (self.runtime / "kokoro_same_speaker_eval").mkdir()
        self.args = types.SimpleNamespace(
            input=str(self.runtime / "experiments/kokoro_same_speaker_generate.json"),
            build=str(self.runtime / "kokoro_same_speaker_eval/build.json"),
            out_dir=str(self.runtime / "duration_length_intervention"),
            out=str(self.runtime / "experiments/duration_length_intervention.json"), pairs=10, seed=1234)
        self.rows = []
        for index in range(20):
            human, clone = self.root / f"human_{index}.wav", self.root / f"clone_{index}.wav"
            write_wav(human, 4800)
            write_wav(clone, 2400)
            self.rows.append(dict(id=str(index), text="A" * (index + 1),
                                 human_wav=str(human), clone_wav=str(clone)))
        Path(self.args.input).write_text(json.dumps({"rows": self.rows}))
        ref = self.root / "reference.wav"
        write_wav(ref)
        self.build = dict(ref_sample=str(ref), ref_text="Reference")
        Path(self.args.build).write_text(json.dumps(self.build))
        self.document = self.make_completed()

    def make_completed(self, grouped_frames=9600):
        pairs = self.duration.build_short_pairs(self.rows, self.args.pairs)
        identity = self.duration.get_duration_measurement_identity(self.args, self.build, pairs)
        results = []
        for index, (left, right) in enumerate(pairs):
            path = self.duration.get_grouped_duration_path(self.args, index, identity)
            write_wav(Path(path), grouped_frames)
            results.append(self.duration.get_duration_pair_result(index, left, right, path))
        document = dict(design="same text/reference/seed; separate vs newline-grouped", seed=1234,
                        cache_identity=identity, rows=results, summary=self.duration.summarize(results))
        Path(self.args.out).write_text(json.dumps(document))
        return document

    def validate(self):
        return self.duration.get_completed_duration_result(self.args.out, self.args)

    def test_complete_improving_and_worsening_measurements_are_reusable(self):
        for frames, grouped_ratio in ((9600, 1.0), (2400, 0.25)):
            with self.subTest(frames=frames):
                document = self.make_completed(frames)
                before = Path(self.args.out).read_bytes()
                self.assertEqual(document, self.validate())
                self.assertEqual(10, document["summary"]["n"])
                self.assertEqual(grouped_ratio, document["rows"][0]["grouped_ratio"])
                self.assertEqual(before, Path(self.args.out).read_bytes())

    def test_partial_wrong_scope_forged_ratios_and_summary_refuse(self):
        changes = [lambda d: d["rows"].pop(), lambda d: d["rows"].reverse(),
                   lambda d: d.update(seed=99), lambda d: d["rows"][0].update(pair=True),
                   lambda d: d["rows"][0].update(ids=["wrong", "ids"]),
                   lambda d: d["rows"][0].update(characters=999),
                   lambda d: d["rows"][0].update(grouped_ratio=float("nan")),
                   lambda d: d["rows"][0].update(separate_ratio=True),
                   lambda d: d["rows"][0].update(grouped_wav="unrelated.wav"),
                   lambda d: d["summary"].update(n=1),
                   lambda d: d["summary"].update(grouped_median=True),
                   lambda d: d["summary"].update(pairs_closer_to_one=0)]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                document = copy.deepcopy(self.document)
                change(document)
                Path(self.args.out).write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    self.validate()

    def test_changed_audio_config_reference_or_manifest_cannot_reuse_result(self):
        paths = [Path(self.rows[0]["human_wav"]), Path(self.rows[0]["clone_wav"]),
                 Path(self.build["ref_sample"]), self.root / "app/config.json",
                 Path(self.args.input), Path(self.args.build)]
        for path in paths:
            with self.subTest(file=path.name):
                original = path.read_bytes()
                Path(self.args.out).write_text(json.dumps(self.document))
                path.write_bytes(original + b" ")
                with self.assertRaises(ValueError):
                    self.validate()
                path.write_bytes(original)

    def test_missing_corrupt_or_changed_grouped_audio_refuses(self):
        path = self.root / self.document["rows"][0]["grouped_wav"]
        for kind in ("missing", "corrupt", "changed"):
            with self.subTest(kind=kind):
                self.make_completed()
                if kind == "missing":
                    path.unlink()
                elif kind == "corrupt":
                    path.write_bytes(b"invalid WAV")
                else:
                    write_wav(path, 4800)
                with self.assertRaises(ValueError):
                    self.validate()

    def run_producer(self, mutate=False):
        for path in Path(self.args.out_dir).glob("*.wav"):
            path.unlink()
        Path(self.args.out).unlink()
        def render(engine, text, instruct, speaker, config, ref, wav):
            write_wav(Path(wav), 9600)
            if mutate:
                write_wav(Path(self.rows[0]["human_wav"]), 7200)
        fake_tts = types.ModuleType("tts")
        fake_tts.TTSEngine = lambda config: object()
        args = ["duration", "--input", self.args.input, "--build", self.args.build,
                "--out-dir", self.args.out_dir, "--out", self.args.out]
        with patch.object(sys, "argv", args), patch.dict(sys.modules, {"tts": fake_tts}), \
                patch.object(self.duration, "render", side_effect=render):
            self.duration.main()

    def test_actual_producer_publishes_all_ten_pairs_and_native_wav_artifacts(self):
        self.run_producer()
        document = self.validate()
        self.assertEqual(10, len(document["rows"]))
        for row in document["rows"]:
            self.assertEqual(0.5, row["separate_ratio"])
            self.assertEqual(1.0, row["grouped_ratio"])
            with wave.open(str(self.root / row["grouped_wav"])) as handle:
                self.assertEqual(9600, handle.getnframes())
                self.assertEqual(b'\x00\x10' * 9600, handle.readframes(9600))

    def test_producer_refuses_final_artifact_when_inputs_change_during_generation(self):
        with self.assertRaisesRegex(RuntimeError, "inputs changed"):
            self.run_producer(mutate=True)
        self.assertFalse(Path(self.args.out).exists())

    def run_chain_boundary(self, kind, *, worker_rc=0, generated_valid=True):
        if kind == "missing":
            Path(self.args.out).unlink()
        elif kind == "partial":
            Path(self.args.out).write_text('{"rows":[]}')
        elif kind == "malformed":
            Path(self.args.out).write_text('{"rows":')
        elif kind == "stale":
            write_wav(Path(self.rows[0]["clone_wav"]), 4800)
        old = Path(self.args.out).read_bytes() if Path(self.args.out).exists() else None
        repaired = self.make_completed()
        payload = self.root / "generated.json"
        payload.write_text(json.dumps(repaired) if generated_valid else '{}')
        if old is None:
            Path(self.args.out).unlink()
        else:
            Path(self.args.out).write_bytes(old)
        path = Path(os.environ.get("ALEXANDRIA_TEST_RESEARCH_SOURCE",
                                  str(REPO / "run_chains/remaining_gpu_research.sh")))
        source = path.read_text()
        start = source.index('duration_out=')
        block = source[start:source.index('\nadapter_out=', start)]
        script = ('set -uo pipefail\nrepo=$1; runtime=$2; python=$3\n'
                  'stage() { echo DISPATCH; cp "$FIXTURE_PAYLOAD" "$duration_out"; return "$FIXTURE_RC"; }\n'
                  + block + '\necho NEXT_STAGE\n')
        return subprocess.run(['bash', '-c', script, 'fixture', str(self.root), str(self.runtime), sys.executable],
            capture_output=True, text=True, timeout=10,
            env=dict(os.environ, PYTHONPATH=str(REPO / 'app'), FIXTURE_PAYLOAD=str(payload),
                     FIXTURE_RC=str(worker_rc)))

    def test_chain_reuses_only_complete_result(self):
        result = self.run_chain_boundary("complete")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("DISPATCH", result.stdout)
        self.assertIn("NEXT_STAGE", result.stdout)

    def test_chain_regenerates_partial_malformed_stale_or_missing_results(self):
        for kind in ("partial", "malformed", "stale", "missing"):
            with self.subTest(kind=kind):
                self.make_completed()
                result = self.run_chain_boundary(kind)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("DISPATCH", result.stdout)
                self.assertIn("NEXT_STAGE", result.stdout)

    def test_failed_worker_and_incomplete_success_stop_later_research(self):
        for worker_rc, generated_valid in ((7, True), (0, False)):
            with self.subTest(worker_rc=worker_rc):
                self.make_completed()
                result = self.run_chain_boundary("missing", worker_rc=worker_rc, generated_valid=generated_valid)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn("DISPATCH", result.stdout)
                self.assertNotIn("NEXT_STAGE", result.stdout)
