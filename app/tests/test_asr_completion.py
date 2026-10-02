"""Known PCM/score fixtures, actual producer and native cache CLI; no inference."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import wave

REPO = Path(__file__).resolve().parents[2]


def write_wav(path, frames=1600, rate=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        handle.writeframes(b'\x00\x10' * frames)


class AsrCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        script = self.root / "app/experiments/asr_backends.py"
        script.parent.mkdir(parents=True)
        shutil.copyfile(REPO / "app/experiments/asr_backends.py", script)
        package = self.root / "app/silero_vad"
        (package / "data").mkdir(parents=True)
        (package / "__init__.py").write_text('raise AssertionError("fixture model must never be loaded")\n')
        (package / "data/silero_vad.jit").write_bytes(b"known fixture VAD model bytes, never loaded")
        metadata = self.root / "app/silero_vad-0.0.1.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: silero-vad\nVersion: 0.0.1\n")
        (metadata / "RECORD").write_text("silero_vad/__init__.py,,\nsilero_vad/data/silero_vad.jit,,\n")
        spec = importlib.util.spec_from_file_location("fixture_asr", script)
        self.asr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.asr)
        inserted = str(self.root / "app")
        self.addCleanup(lambda: sys.path.remove(inserted) if inserted in sys.path else None)
        self.runtime = self.root / "ab_test_runtime"
        (self.runtime / "kokoro_same_speaker_eval").mkdir(parents=True)
        (self.runtime / "experiments").mkdir()
        self.args = types.SimpleNamespace(build=str(self.runtime / "kokoro_same_speaker_eval/build.json"),
            backends=["silero_whisper_cpp"], lang="ja", row_offset=20, limit=10, align_clips=10,
            score_readings=False, keep_hypotheses=False,
            whisper_cpp_bin=str(self.root / "whisper.cpp/build/bin/whisper-cli"),
            whisper_cpp_model=str(self.root / "whisper.cpp/models/ggml-base.bin"),
            out=str(self.runtime / "experiments/asr_silero_whisper_ja_offset20.json"))
        self.large_model = self.root / "whisper.cpp/models/ggml-large-v3.bin"
        for path in (Path(self.args.whisper_cpp_bin), Path(self.args.whisper_cpp_model), self.large_model):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"known local fixture bytes, never executed or loaded")
        rows = []
        for index in range(30):
            path = self.root / f"human/{index}.wav"
            write_wav(path)
            rows.append(dict(id=str(index), human_wav=str(path.relative_to(self.root)), text="あい"))
        Path(self.args.build).write_text(json.dumps({"test": rows}))
        self.rows = self.asr.get_asr_rows(self.args)
        self.truth = [self.asr.get_alignment_boundary(row, index * 0.6, 0.1) for index, row in enumerate(self.rows)]
        self.document = self.make_document()

    def make_document(self, poor=False):
        scores = [self.asr.word_error_rate("あい", "うえ" if poor else "あい")] * 10
        segments = [[0.0, 0.1, "あい"]] if poor else [[row["start"], row["end"], row["text"]] for row in self.truth]
        record = dict(n=10, failed=0, failures=[], processed_ids=[row["id"] for row in self.rows],
                      wer_scores=scores, reading_scores=[], clips_without_timestamps=0,
                      wer_mean=round(statistics.mean(scores), 4), wer_median=round(statistics.median(scores), 4))
        document = dict(build=os.path.relpath(self.args.build, self.root), limit=10, language="ja",
            whisper_cpp_model="ggml-base.bin", backends=self.args.backends,
            measurement=self.asr.get_asr_measurement_identity(self.args, self.rows),
            results={"silero_whisper_cpp":record}, alignment_truth_clips=10,
            alignment_segments={"silero_whisper_cpp":segments},
            alignment={"silero_whisper_cpp":self.asr.score_alignment(self.truth, segments)})
        Path(self.args.out).write_text(json.dumps(document))
        return document

    def validate(self):
        return self.asr.get_completed_asr_result(self.args.out, self.args)

    def test_complete_good_and_poor_outcomes_reuse_actual_score_and_timestamp_evidence(self):
        for poor, expected in ((False, 0.0), (True, 1.0)):
            with self.subTest(poor=poor):
                document = self.make_document(poor)
                before = Path(self.args.out).read_bytes()
                self.assertEqual(expected, document["results"]["silero_whisper_cpp"]["wer_mean"])
                self.assertEqual(document, self.validate())
                self.assertEqual(before, Path(self.args.out).read_bytes())

    def test_rejects_partial_wrong_sample_forged_scores_and_unmeasured_alignment(self):
        changes = [lambda d: d["measurement"].update(row_offset=0),
            lambda d: d["measurement"].update(schema_version=True),
            lambda d: d["results"]["silero_whisper_cpp"].update(wer_mean=False),
            lambda d: d["results"]["silero_whisper_cpp"]["processed_ids"].pop(),
            lambda d: d["results"]["silero_whisper_cpp"].update(n=1),
            lambda d: d["results"]["silero_whisper_cpp"].update(failed=1),
            lambda d: d["results"]["silero_whisper_cpp"]["wer_scores"].pop(),
            lambda d: d["results"]["silero_whisper_cpp"]["wer_scores"].__setitem__(0, float("nan")),
            lambda d: d["results"]["silero_whisper_cpp"].update(wer_mean=0.7),
            lambda d: d.update(language="en"), lambda d: d.update(alignment_truth_clips=9),
            lambda d: d["alignment_segments"].update(silero_whisper_cpp=[]),
            lambda d: d["alignment_segments"]["silero_whisper_cpp"][0].__setitem__(0, float("inf")),
            lambda d: d["alignment"]["silero_whisper_cpp"].update(scored=1)]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                document = copy.deepcopy(self.document)
                change(document)
                Path(self.args.out).write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    self.validate()

    def test_changed_audio_model_binary_build_or_harness_invalidates_cache(self):
        paths = [self.root / self.rows[0]["human_wav"], self.large_model,
                 Path(self.args.whisper_cpp_bin), Path(self.args.build),
                 self.root / "app/silero_vad/data/silero_vad.jit",
                 self.root / "app/experiments/asr_backends.py"]
        for path in paths:
            with self.subTest(path=path.name):
                old = path.read_bytes()
                Path(self.args.out).write_text(json.dumps(self.document))
                path.write_bytes(old + b" ")
                with self.assertRaises(ValueError):
                    self.validate()
                path.write_bytes(old)

    def producer(self, kind="complete"):
        Path(self.args.out).unlink()
        calls = []
        def backend(wav, model, binary, language="ja"):
            calls.append(wav)
            if kind == "failed" and len(calls) == 1:
                raise RuntimeError("fixture transcription failure")
            if kind == "changed":
                self.large_model.write_bytes(b"changed during run")
            if Path(wav).name == "probe.wav":
                return "あい", [] if kind == "no-alignment" else [
                    (row["start"], row["end"], row["text"]) for row in self.truth]
            return "あい", [(0.0, 0.1, "あい")]
        args = ["asr", "--build", self.args.build, "--backends", "silero_whisper_cpp", "--lang", "ja",
                "--row-offset", "20", "--limit", "10", "--align-clips", "10",
                "--whisper-cpp-bin", self.args.whisper_cpp_bin, "--whisper-cpp-model", self.args.whisper_cpp_model,
                "--out", self.args.out]
        if self.args.keep_hypotheses:
            args.append("--keep-hypotheses")
        code = 0
        with patch.object(sys, "argv", args), patch.object(self.asr, "run_silero_whisper_cpp", side_effect=backend), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                self.asr.main()
            except SystemExit as result:
                code = result.code
        return code, calls

    def test_actual_producer_records_full_clip_scores_and_native_alignment_probe(self):
        code, calls = self.producer()
        self.assertEqual(0, code)
        self.assertEqual(11, len(calls))
        document = self.validate()
        self.assertEqual([0.0] * 10, document["results"]["silero_whisper_cpp"]["wer_scores"])
        with wave.open(str(self.runtime / "asr_bench/probe.wav")) as handle:
            self.assertEqual(96000, handle.getnframes())
            self.assertEqual(16000, handle.getframerate())

    def test_hypotheses_requested_by_producer_are_retained_and_rescorable(self):
        self.args.keep_hypotheses = True
        code, _ = self.producer()
        self.assertEqual(0, code)
        document = self.validate()
        self.assertEqual(10, len(document["results"]["silero_whisper_cpp"]["hypotheses"]))
        document["results"]["silero_whisper_cpp"]["hypotheses"][0]["hypothesis"] = "うえ"
        Path(self.args.out).write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "hypothesis disagrees"):
            self.validate()

    def test_partial_transcription_or_missing_alignment_is_recorded_but_refused(self):
        for kind in ("failed", "no-alignment"):
            with self.subTest(kind=kind):
                code, _ = self.producer(kind)
                self.assertEqual(3, code)
                self.assertTrue(Path(self.args.out).exists())
                with self.assertRaises(ValueError):
                    self.validate()

    def test_model_changed_during_run_prevents_publication(self):
        code, _ = self.producer("changed")
        self.assertNotEqual(0, code)
        self.assertFalse(Path(self.args.out).exists())

    def run_boundary(self, kind, *, worker_rc=0, generated_valid=True):
        if kind == "missing":
            Path(self.args.out).unlink()
        elif kind == "partial":
            Path(self.args.out).write_text('{"results":{}}')
        elif kind == "malformed":
            Path(self.args.out).write_text('{"results":')
        elif kind == "stale":
            self.large_model.write_bytes(b"new local model bytes")
        old = Path(self.args.out).read_bytes() if Path(self.args.out).exists() else None
        repaired = self.make_document()
        payload = self.root / "generated.json"
        payload.write_text(json.dumps(repaired) if generated_valid else '{}')
        if old is None:
            Path(self.args.out).unlink()
        else:
            Path(self.args.out).write_bytes(old)
        path = Path(os.environ.get("ALEXANDRIA_TEST_RESEARCH_SOURCE", str(REPO / "run_chains/remaining_gpu_research.sh")))
        source = path.read_text()
        start = source.index('asr_out=')
        block = source[start:source.index('\nduration_out=', start)]
        script = ('set -uo pipefail\nrepo=$1; runtime=$2; python=$3\n'
            'stage() { echo DISPATCH; cp "$FIXTURE_PAYLOAD" "$asr_out"; return "$FIXTURE_RC"; }\n'
            + block + '\necho NEXT_STAGE\n')
        return subprocess.run(['bash', '-c', script, 'fixture', str(self.root), str(self.runtime), sys.executable],
            capture_output=True, text=True, timeout=10, env=dict(os.environ, PYTHONPATH=str(REPO / 'app'),
            FIXTURE_PAYLOAD=str(payload), FIXTURE_RC=str(worker_rc)))

    def test_chain_reuses_full_result(self):
        result = self.run_boundary("complete")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("DISPATCH", result.stdout)
        self.assertIn("NEXT_STAGE", result.stdout)

    def test_chain_regenerates_partial_malformed_stale_and_missing_cache(self):
        for kind in ("partial", "malformed", "stale", "missing"):
            with self.subTest(kind=kind):
                self.make_document()
                result = self.run_boundary(kind)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("DISPATCH", result.stdout)
                self.assertIn("NEXT_STAGE", result.stdout)

    def test_failed_worker_or_incomplete_success_blocks_later_research(self):
        for worker_rc, generated_valid in ((7, True), (0, False)):
            with self.subTest(worker_rc=worker_rc):
                self.make_document()
                result = self.run_boundary("missing", worker_rc=worker_rc, generated_valid=generated_valid)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn("DISPATCH", result.stdout)
                self.assertNotIn("NEXT_STAGE", result.stdout)
