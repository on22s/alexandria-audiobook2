"""Long-row timing repair (#1844) — sequential measured-cursor windows.

Fail-first regressions for the combined recipe proven in the parent
diagnostics (20s text target, 3s tail, 3s growth, measured previous-end
cursor, encoder mask + score decoder in the worker). All inference here
is the test-only fake worker; no torch/qwen/model loads in this module.
"""
import json
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

import qwen_align as qa
import qwen_runtime as qr

RATE = 24000
FAKE = Path(__file__).resolve().parent / "fixtures" / "qwen_fake_worker.py"


def _text(sentences=6, words_per=20):
    parts = []
    for s in range(sentences):
        words = ["w%dx%d" % (s, i) for i in range(words_per)]
        parts.append(" ".join(words) + ".")
    return " ".join(parts)


def _write_pcm(path, frames):
    with open(path, "wb") as handle:
        handle.write(b"\x00\x00" * frames)


def _capability(tmp):
    return {"available": True, "python": sys.executable,
            "model_dir": str(tmp), "device": "cpu", "dtype": "float32",
            "devices": [], "cpu_fallback": True, "reason": None}


def _collect(tmp, text, duration_sec, *, prepared=None, extra_rows=None):
    from readalong_export import collect_aligned_rows
    frames = int(round(duration_sec * RATE))
    pcm = Path(tmp) / "assembled.pcm"
    _write_pcm(pcm, frames)
    data = {"layout": (1, RATE, 2), "bounds": [(0, frames)],
            "timeline": [({"text": text}, None, 0)], "pcm_path": str(pcm)}
    capability = _capability(tmp)
    with patch.object(qr, "get_qwen_worker_command",
                      lambda _py: [sys.executable, str(FAKE)]):
        return collect_aligned_rows(
            data, "English", capability, lambda: False, str(Path(tmp) / "stage"),
            prepared_texts=[text] if prepared is None else prepared)


class PlannerTests(unittest.TestCase):
    def test_proven_scheduling_constants(self):
        self.assertEqual(20.0, qa.DEFAULT_WINDOW_TARGET_SECONDS)
        self.assertEqual(3.0, qa.WINDOW_TAIL_SECONDS)
        self.assertEqual(3.0, qa.WINDOW_GROWTH_SECONDS)

    def test_long_row_windows_bounded(self):
        sentences = [{"start": i * 20, "end": i * 20 + 19, "words": 30}
                     for i in range(12)]
        windows = qa.get_aligner_windows(400.0, sentences)
        self.assertGreater(len(windows), 1)
        for window in windows:
            self.assertLessEqual(window["end_sec"] - window["start_sec"], 180.0 + 1e-9)

    def test_single_sentence_up_to_180_not_refused_at_20(self):
        text = " ".join("word%d" % i for i in range(100)) + "."
        sentences = [{"start": 0, "end": len(text), "words": 100}]
        windows = qa.get_aligner_windows(100.0, sentences, text=text)
        self.assertEqual(1, len(windows))
        self.assertEqual(0, windows[0]["char_start"])
        self.assertEqual(len(text), windows[0]["char_end"])

    def test_single_sentence_over_180_partitioned_with_text(self):
        text = " ".join("word%d" % i for i in range(1000)) + "."
        sentences = [{"start": 0, "end": len(text), "words": 1000}]
        windows = qa.get_aligner_windows(600.0, sentences, text=text)
        self.assertGreater(len(windows), 1)
        covered = [(w["char_start"], w["char_end"]) for w in windows]
        self.assertEqual(0, covered[0][0])
        self.assertEqual(len(text), covered[-1][1])
        for (s1, e1), (s2, _e2) in zip(covered, covered[1:]):
            self.assertEqual(e1, s2)  # contiguous, exact coverage
        for window in windows:
            self.assertLessEqual(window["end_sec"] - window["start_sec"], 180.0 + 1e-9)

    def test_single_sentence_over_180_without_text_still_refused(self):
        sentences = [{"start": 0, "end": 10, "words": 1000}]
        with self.assertRaises(ValueError):
            qa.get_aligner_windows(600.0, sentences)


class SequentialFakePathTests(unittest.TestCase):
    def test_long_row_sequential_fake_production_path(self):
        text = _text()
        with tempfile.TemporaryDirectory() as tmp:
            rows, report = _collect(tmp, text, 60.0)
        self.assertEqual(1, len(rows))
        self.assertGreaterEqual(report["windows"], 2)
        tokens = rows[0]["tokens"]
        self.assertEqual(text.count("w") and len(text.split()), len(tokens))
        for prev, cur in zip(tokens, tokens[1:]):
            self.assertLessEqual(prev["end_time"], cur["start_time"] + 1e-9)
            self.assertLess(prev["start_time"], prev["end_time"])
        self.assertLessEqual(tokens[-1]["end_time"], 60.0 + 1e-9)

    def test_short_row_single_window(self):
        text = "Hello synthetic world."
        with tempfile.TemporaryDirectory() as tmp:
            rows, report = _collect(tmp, text, 3.0)
        self.assertEqual(1, report["windows"])
        self.assertEqual(3, len(rows[0]["tokens"]))

    def test_repeated_speech_row(self):
        text = " ".join(["alpha bravo charlie"] * 8) + "."
        with tempfile.TemporaryDirectory() as tmp:
            rows, _report = _collect(tmp, text, 24.0)
        self.assertEqual(24, len(rows[0]["tokens"]))

    def test_variable_rate_growth_via_worker_protocol(self):
        # Hand-written job: window 0 speech runs past its first slice,
        # so the worker must grow by 3s steps before accepting.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "row.wav"
            with wave.open(str(wav), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                stream.writeframes(b"\x00\x00" * (40 * RATE))
            text = " ".join("v%d" % i for i in range(40)) + "."
            half = len(" ".join("v%d" % i for i in range(20))) + 1
            job = {"model_dir": str(root), "device": "cpu", "dtype": "float32",
                   "language": "English",
                   "rows": [{"id": "r0", "wav_path": str(wav), "text": text,
                             "duration_sec": 40.0,
                             "fake_speech_offsets": [18.0, 38.0],
                             "windows": [
                                 {"char_start": 0, "char_end": half,
                                  "est_start_sec": 0.0, "est_end_sec": 13.0},
                                 {"char_start": half, "char_end": len(text),
                                  "est_start_sec": 20.0, "est_end_sec": 33.0}]}]}
            job_path = root / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            tokens, windows, _provider = qr.run_qwen_alignment_full(
                [sys.executable, str(FAKE)], job_path, root / "result.json",
                lambda: False)
        self.assertEqual(2, len(windows["r0"]))
        total_calls = sum(w["calls"] for w in windows["r0"])
        self.assertGreater(total_calls, 2)  # growth actually happened
        row_tokens = tokens["r0"]
        self.assertEqual(40, len(row_tokens))
        # Second window starts at the measured end of the first window.
        first_last = windows["r0"][0]["tokens"][-1]["end_time"]
        second_start = windows["r0"][1]["start_frame"] / RATE
        self.assertAlmostEqual(first_last, second_start, delta=2.0 / RATE + 0.002)

    def test_final_tiny_remainder_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "row.wav"
            with wave.open(str(wav), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                stream.writeframes(b"\x00\x00" * (20 * RATE))
            text = " ".join("t%d" % i for i in range(20)) + "."
            job = {"model_dir": str(root), "device": "cpu", "dtype": "float32",
                   "language": "English",
                   "rows": [{"id": "r0", "wav_path": str(wav), "text": text,
                             "duration_sec": 20.0,
                             "fake_speech_offsets": [19.95],
                             "windows": [
                                 {"char_start": 0, "char_end": len(text),
                                  "est_start_sec": 0.0, "est_end_sec": 19.768}]}]}
            job_path = root / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            tokens, windows, _provider = qr.run_qwen_alignment_full(
                [sys.executable, str(FAKE)], job_path, root / "result.json",
                lambda: False)
        self.assertEqual(20, len(tokens["r0"]))
        window = windows["r0"][0]
        self.assertEqual(20 * RATE, window["end_frame"])  # grew to the end
        self.assertGreaterEqual(window["calls"], 2)

    def test_no_progress_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "row.wav"
            with wave.open(str(wav), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                stream.writeframes(b"\x00\x00" * (10 * RATE))
            text = " ".join("n%d" % i for i in range(10)) + "."
            job = {"model_dir": str(root), "device": "cpu", "dtype": "float32",
                   "language": "English",
                   "rows": [{"id": "r0", "wav_path": str(wav), "text": text,
                             "duration_sec": 10.0, "fake_speech_offsets": [0.0],
                             "windows": [
                                 {"char_start": 0, "char_end": len(text),
                                  "est_start_sec": 0.0, "est_end_sec": 8.0}]}]}
            job_path = root / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            with self.assertRaises(ValueError):
                qr.run_qwen_alignment_full(
                    [sys.executable, str(FAKE)], job_path, root / "result.json",
                    lambda: False)


class WorkerWindowValidationTests(unittest.TestCase):
    """Parent must not trust worker-declared windows (qa validator)."""

    TEXT = "alpha bravo charlie delta."
    PLANNED = [{"char_start": 0, "char_end": 26, "start_sec": 0.0,
                "end_sec": 8.0, "first_sentence": 0, "last_sentence": 1}]
    RATE = RATE

    def _valid(self):
        tokens = [{"text": w, "start_time": 0.5 * i, "end_time": 0.5 * i + 0.4}
                  for i, w in enumerate(["alpha", "bravo", "charlie", "delta"])]
        windows = [{"char_start": 0, "char_end": 26, "start_frame": 0,
                    "end_frame": 8 * self.RATE, "offset_sec": 0.0,
                    "slice_duration_sec": 8.0, "calls": 1, "tokens": tokens}]
        return windows, [dict(t) for t in tokens]

    def _run(self, windows, merged):
        return qa.validate_sequential_windows(
            self.TEXT, self.PLANNED, windows, merged,
            sample_rate=self.RATE, total_frames=8 * self.RATE,
            row_duration_sec=8.0)

    def test_valid_windows_pass(self):
        windows, merged = self._valid()
        validated = self._run(windows, merged)
        self.assertEqual(4, len(validated))

    def test_wrong_char_scope_fails(self):
        windows, merged = self._valid()
        windows[0]["char_end"] = 20
        with self.assertRaises(ValueError):
            self._run(windows, merged)

    def test_out_of_bounds_frame_fails(self):
        windows, merged = self._valid()
        windows[0]["end_frame"] = 9 * self.RATE
        with self.assertRaises(ValueError):
            self._run(windows, merged)

    def test_too_many_calls_fails(self):
        windows, merged = self._valid()
        windows[0]["calls"] = qa.MAX_SEQUENTIAL_CALLS_PER_WINDOW + 1
        with self.assertRaises(ValueError):
            self._run(windows, merged)

    def test_incomplete_coverage_fails(self):
        windows, merged = self._valid()
        windows[0]["tokens"] = windows[0]["tokens"][:3]
        with self.assertRaises(ValueError):
            self._run(windows, merged)

    def test_out_of_bounds_token_time_fails(self):
        windows, merged = self._valid()
        bad = dict(windows[0]["tokens"][-1])
        bad["end_time"] = 99.0
        windows[0]["tokens"][-1] = bad
        with self.assertRaises(ValueError):
            self._run(windows, merged)

    def test_merged_mismatch_fails(self):
        windows, merged = self._valid()
        merged = merged[:-1]
        with self.assertRaises(ValueError):
            self._run(windows, merged)

    def test_missing_window_fails(self):
        _windows, merged = self._valid()
        with self.assertRaises(ValueError):
            self._run([], merged)


class CancellationAndIdentityTests(unittest.TestCase):
    def test_cancel_during_row_copy_fails_closed(self):
        from readalong_export import collect_aligned_rows
        text = _text()
        with tempfile.TemporaryDirectory() as tmp:
            frames = 60 * RATE
            pcm = Path(tmp) / "assembled.pcm"
            _write_pcm(pcm, frames)
            data = {"layout": (1, RATE, 2), "bounds": [(0, frames)],
                    "timeline": [({"text": text}, None, 0)], "pcm_path": str(pcm)}
            calls = {"count": 0}

            def cancel_check():
                calls["count"] += 1
                return calls["count"] > 3

            with patch.object(qr, "get_qwen_worker_command",
                              lambda _py: [sys.executable, str(FAKE)]):
                with self.assertRaises((ValueError, Exception)):
                    collect_aligned_rows(
                        data, "English", _capability(tmp), cancel_check,
                        str(Path(tmp) / "stage"), prepared_texts=[text])

    def test_cancel_during_inference_kills_worker(self):
        text = _text()
        with tempfile.TemporaryDirectory() as tmp:
            slow = Path(tmp) / "slow.py"
            slow.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            frames = 60 * RATE
            pcm = Path(tmp) / "assembled.pcm"
            _write_pcm(pcm, frames)
            data = {"layout": (1, RATE, 2), "bounds": [(0, frames)],
                    "timeline": [({"text": text}, None, 0)], "pcm_path": str(pcm)}
            cancelled = threading.Event()
            done = threading.Event()
            outcome = []

            def run():
                from readalong_export import collect_aligned_rows
                try:
                    with patch.object(qr, "get_qwen_worker_command",
                                      lambda _py: [sys.executable, str(slow)]):
                        collect_aligned_rows(
                            data, "English", _capability(tmp), cancelled.is_set,
                            str(Path(tmp) / "stage"), prepared_texts=[text])
                except Exception as error:  # noqa: BLE001 - outcome check
                    outcome.append(str(error))
                finally:
                    done.set()

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            import time
            time.sleep(1.0)
            cancelled.set()
            self.assertTrue(done.wait(timeout=15), "cancel did not stop alignment")
            self.assertTrue(any("cancel" in m.lower() for m in outcome))

    def test_provider_digest_binds_worker_code(self):
        import hashlib
        digest = qr.get_qwen_code_digest()
        self.assertEqual(64, len(digest))
        worker = Path(qr.__file__).resolve().parent / "qwen_align_worker.py"
        decoder = Path(qr.__file__).resolve().parent / "qwen_score_decoder.py"
        expected = hashlib.sha256()
        for path in (worker, decoder):
            expected.update(path.name.encode("utf-8"))
            expected.update(b"\x00")
            expected.update(path.read_bytes())
            expected.update(b"\x00")
        self.assertEqual(expected.hexdigest(), digest)

    def test_provider_identity_drifts_with_code(self):
        import qwen_runtime
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model"
            model.mkdir()
            (model / "config.json").write_text("{}", encoding="utf-8")
            identity = qr.get_qwen_runtime_identity(sys.executable)
            runtime = {"qwen_asr_version": "0.0.6", "torch_version": "2.10.0+cpu",
                       "device": "cpu", "dtype": "float32", "identity": identity}
            first = qr.get_qwen_provider_digest(str(model), runtime_identity=runtime)
        with patch.object(qwen_runtime, "get_qwen_code_digest",
                          lambda: "0" * 64):
            with tempfile.TemporaryDirectory() as tmp:
                model = Path(tmp) / "model"
                model.mkdir()
                (model / "config.json").write_text("{}", encoding="utf-8")
                second = qr.get_qwen_provider_digest(
                    str(model), runtime_identity=runtime)
        self.assertNotEqual(first, second)


class CompatibilityTests(unittest.TestCase):
    def test_chunk_policy_still_cpu(self):
        policy = qr.get_qwen_device_policy("chunk", "auto", {"available": False})
        self.assertEqual("chunk", policy["timing_mode"])
        self.assertEqual("cpu", policy["device"])
        self.assertTrue(policy["cpu_only"])

    def test_legacy_single_window_job_still_supported(self):
        # Old-shaped job (no windows key) must still return merged tokens.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "row.wav"
            with wave.open(str(wav), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                stream.writeframes(b"\x00\x00" * RATE)
            job = {"model_dir": str(root), "device": "cpu", "dtype": "float32",
                   "language": "English",
                   "rows": [{"id": "row0", "wav_path": str(wav),
                             "text": "Hello synthetic world.", "duration_sec": 1.0}]}
            job_path = root / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            tokens = qr.run_qwen_alignment(
                [sys.executable, str(FAKE)], job_path, root / "result.json",
                lambda: False)
        self.assertEqual(["Hello", "synthetic", "world"],
                         [t["text"] for t in tokens["row0"]])


class FollowupMergedRowTests(unittest.TestCase):
    """R-TIME-1: a whole row may exceed 180s; only real calls are capped."""

    def test_parent_probe_200s_12_windows_accepted(self):
        text = "One. " * 12
        spans = qa.cover_ranges(text, qa.split_sentences(text))
        plans = qa.get_aligner_windows(
            200.0, [dict(start=a, end=b, words=1) for a, b in spans], text=text)
        self.assertEqual(12, len(plans))
        rate = 22050
        total = rate * 200
        windows = []
        merged = []
        cursor = 0
        for plan in plans:
            end = min(total, cursor + round((plan["end_sec"] - plan["start_sec"]) * rate))
            duration = (end - cursor) / rate
            piece = text[plan["char_start"]:plan["char_end"]]
            token = {"text": piece.strip().rstrip("."),
                     "start_time": 0.0, "end_time": 15.0}
            offset = cursor / rate
            windows.append(dict(char_start=plan["char_start"],
                                char_end=plan["char_end"], start_frame=cursor,
                                end_frame=end, offset_sec=offset,
                                slice_duration_sec=duration, calls=1,
                                tokens=[token]))
            merged.append(dict(token, start_time=offset, end_time=offset + 15.0))
            cursor += 15 * rate
        validated = qa.validate_sequential_windows(
            text, plans, windows, merged, sample_rate=rate,
            total_frames=total, row_duration_sec=200.0)
        self.assertEqual(12, len(validated))

    def test_single_190s_slice_still_rejected_by_default(self):
        tokens = [{"text": "Hello", "start_time": 0.0, "end_time": 1.0}]
        with self.assertRaises(ValueError):
            qa.get_validated_aligner_tokens(tokens, 190.0)

    def test_over180_fake_production_path(self):
        text = _text(sentences=12, words_per=20)
        with tempfile.TemporaryDirectory() as tmp:
            rows, report = _collect(tmp, text, 220.0)
        self.assertGreater(report["windows"], 1)
        self.assertLessEqual(rows[0]["tokens"][-1]["end_time"], 220.0 + 1e-9)


class FollowupFractionalFrameTests(unittest.TestCase):
    """R-TIME-2: full-precision frame offsets + ceil cursor at odd rates."""

    def _row(self, rate):
        text = "One. Two."
        spans = qa.cover_ranges(text, qa.split_sentences(text))
        plans = qa.get_aligner_windows(
            100.0, [dict(start=a, end=b, words=1) for a, b in spans], text=text)
        job_windows = [dict(char_start=q["char_start"], char_end=q["char_end"],
                            est_start_sec=q["start_sec"],
                            est_end_sec=q["end_sec"]) for q in plans]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        path = root / "row.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(b"\0" * (rate * 100 * 2))

        def fake_call(_model, _path, piece, _language, _decode, *_args):
            if piece.strip().startswith("One"):
                return [{"text": "One", "start_time": 0.0, "end_time": 0.08}]
            return [{"text": "Two", "start_time": 0.0, "end_time": 0.24}]

        import qwen_align_worker as worker
        with patch.object(worker, "_align_slice", fake_call):
            merged, windows = worker._align_row(
                object(), {"text": text, "wav_path": str(path),
                           "duration_sec": 100.0, "windows": job_windows},
                "English", None, root)
        return text, plans, windows, merged, rate

    def test_parent_probe_rates_accepted(self):
        for rate in (44100, 44101):
            text, plans, windows, merged, _rate = self._row(rate)
            validated = qa.validate_sequential_windows(
                text, plans, windows, merged, sample_rate=rate,
                total_frames=rate * 100, row_duration_sec=100.0)
            self.assertEqual(2, len(validated))

    def test_ceil_cursor_zero_leading_token(self):
        # 0.08s at 44101Hz = 3528.08 frames: round() would start the
        # next slice at 3528 (before the word ends); ceil gives 3529.
        text, plans, windows, merged, rate = self._row(44101)
        self.assertEqual(3529, windows[1]["start_frame"])
        self.assertGreaterEqual(merged[1]["start_time"], merged[0]["end_time"])

    def test_tiny_last_frame_remainder(self):
        # A fake-speech offset 3 frames before the row end must still
        # be reachable by frame-level growth (no 500ms-style refusal).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "row.wav"
            rate = 44101
            total_frames = 10 * rate + 3
            with wave.open(str(wav), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(rate)
                stream.writeframes(b"\x00\x00" * total_frames)
            text = " ".join("t%d" % i for i in range(10)) + "."
            speech_end = total_frames / rate
            job = {"model_dir": str(root), "device": "cpu", "dtype": "float32",
                   "language": "English",
                   "rows": [{"id": "r0", "wav_path": str(wav), "text": text,
                             "duration_sec": total_frames / rate,
                             "fake_speech_offsets": [speech_end],
                             "windows": [
                                 {"char_start": 0, "char_end": len(text),
                                  "est_start_sec": 0.0, "est_end_sec": 9.5}]}]}
            job_path = root / "job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            tokens, windows, _provider = qr.run_qwen_alignment_full(
                [sys.executable, str(FAKE)], job_path, root / "result.json",
                lambda: False)
        self.assertEqual(total_frames, windows["r0"][0]["end_frame"])
        self.assertEqual(10, len(tokens["r0"]))


class FollowupResourceBoundTests(unittest.TestCase):
    """R-TIME-3: shape/bounds before conversion; predicted check pre-inference."""

    def test_decoder_shape_sentinel_not_converted(self):
        from qwen_score_decoder import decode_timestamp_bins

        class Sentinel:
            shape = (2, 200000)
            dtype = float

            def __array__(self, *args, **kwargs):
                raise AssertionError("oversize scores were converted")

        with self.assertRaises(ValueError):
            decode_timestamp_bins(Sentinel())

    def test_decoder_real_5000_bin_geometry(self):
        from qwen_score_decoder import decode_timestamp_bins
        scores = np.zeros((2, 5000))
        scores[0, 100] = 5.0
        scores[1, 200] = 5.0
        self.assertEqual([100, 200], decode_timestamp_bins(scores))

    def test_align_slice_predicted_oversize_never_calls_model(self):
        import qwen_align_worker as worker

        class FakeProcessor:
            def encode_timestamp(self, piece, language):
                return (["word"] * 20000, "input")

        class FakeModel:
            timestamp_token_id = 151705
            timestamp_segment_time = 80
            aligner_processor = FakeProcessor()
            called = []

            def align(self, **kwargs):
                self.called.append(kwargs)
                raise AssertionError("model inference must not be called")

        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "slice.wav"
            with wave.open(str(wav), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                stream.writeframes(b"\x00\x00" * RATE)
            model = FakeModel()
            with self.assertRaises(ValueError):
                worker._align_slice(model, wav, "dense text " * 100,
                                    "English", lambda scores: [0, 1],
                                    slice_seconds=180.0)
            self.assertEqual([], model.called)

    def test_align_slice_shape_sentinel_not_listified(self):
        import qwen_align_worker as worker

        class SentinelScores:
            shape = (2, 200000)

            def __getitem__(self, key):
                return self  # view-like slicing must not materialize

            def detach(self):
                return self

            def float(self):
                raise AssertionError("oversize scores were cast before shape validation")

            def cpu(self):
                raise AssertionError("oversize scores were copied before shape validation")

            def tolist(self):
                raise AssertionError("oversize scores were listified")

        class FakeRow:
            def __getitem__(self, mask):
                return SentinelScores()

        class FakeLogits:
            def __getitem__(self, index):
                return FakeRow()

        class FakeOutput:
            logits = FakeLogits()

        class FakeThinker:
            def register_forward_hook(self, hook, with_kwargs=True):
                self.hook = hook
                outer = self

                class Handle:
                    def remove(self):
                        pass

                return Handle()

        class FakeInner:
            thinker = FakeThinker()

        class FakeItem:
            text = "Hello"
            start_time = 0.0
            end_time = 0.08

        class FakeProcessor:
            def encode_timestamp(self, piece, language):
                return (["Hello"], "input")

        class FakeModel:
            timestamp_token_id = 151705
            timestamp_segment_time = 80
            aligner_processor = FakeProcessor()
            model = FakeInner()

            def align(self, **kwargs):
                self.model.thinker.hook(
                    None, (), {"input_ids": [[151705]]}, FakeOutput())
                return [[FakeItem()]]

        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "slice.wav"
            with wave.open(str(wav), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                stream.writeframes(b"\x00\x00" * RATE)
            from qwen_score_decoder import decode_timestamp_bins
            with self.assertRaises(ValueError):
                worker._align_slice(FakeModel(), wav, "Hello", "English",
                                    decode_timestamp_bins, slice_seconds=1.0)


class FollowupDocsTests(unittest.TestCase):
    """R-TIME-4: docs must describe the actual implementation."""

    def test_readalong_docs_describe_actual_bounds(self):
        docs = (Path(__file__).resolve().parent.parent.parent
                / "docs" / "readalong.md").read_text(encoding="utf-8")
        self.assertIn("MAX_FORCE_ALIGN_INPUT_SECONDS", docs)
        self.assertIn("conservative", docs.lower())
        self.assertNotIn("upstream forced-aligner cap is 180s", docs)

    def test_third_party_notices_attribute_decoder(self):
        notices = (Path(__file__).resolve().parent.parent.parent
                   / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("hangry", notices.lower())
        self.assertIn("Apache License", notices)


class SliceCopyGeometryTests(unittest.TestCase):
    """R-TIME-5: _write_slice_wav must emit EXACT requested PCM frames."""

    def _source_wav(self, root, *, channels, width, rate, frames, seed=0):
        import qwen_align_worker as worker  # noqa: F401 (import check)
        path = root / "source.wav"
        frame_bytes = channels * width
        # Deterministic non-uniform PCM so exact-equality is meaningful.
        data = bytes(((i * 7 + seed) % 251) for i in range(frames * frame_bytes))
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(channels)
            stream.setsampwidth(width)
            stream.setframerate(rate)
            stream.writeframes(data)
        return path, data

    def _slice(self, root, source, start, end):
        import qwen_align_worker as worker
        dest = root / "slice.wav"
        seconds = worker._write_slice_wav(str(source), str(dest), start, end)
        with wave.open(str(dest), "rb") as stream:
            params = stream.getparams()
            pcm = stream.readframes(stream.getnframes())
        return dest, seconds, params, pcm

    def test_mono_16bit_subrange_exact_pcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, data = self._source_wav(
                root, channels=1, width=2, rate=1000, frames=10000)
            _dest, seconds, params, pcm = self._slice(root, source, 100, 1100)
        self.assertEqual(1000, params.nframes)
        self.assertEqual(1.0, seconds)
        self.assertEqual(data[100 * 2:1100 * 2], pcm)
        self.assertEqual((1, 2, 1000), (params.nchannels, params.sampwidth,
                                        params.framerate))

    def test_stereo_16bit_subrange_exact_pcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, data = self._source_wav(
                root, channels=2, width=2, rate=1000, frames=10000)
            _dest, seconds, params, pcm = self._slice(root, source, 100, 1100)
        self.assertEqual(1000, params.nframes)
        self.assertEqual(1.0, seconds)
        self.assertEqual(data[100 * 4:1100 * 4], pcm)

    def test_widths_1_and_4_exact_pcm(self):
        for width in (1, 4):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, data = self._source_wav(
                    root, channels=1, width=width, rate=8000, frames=5000)
                _dest, seconds, params, pcm = self._slice(root, source, 37, 1037)
                self.assertEqual(1000, params.nframes)
                self.assertEqual(data[37 * width:1037 * width], pcm)

    def test_multi_block_over_1mib_exact_pcm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames = 600000  # 1.2 MB mono16: spans multiple 1MiB blocks
            source, data = self._source_wav(
                root, channels=1, width=2, rate=24000, frames=frames + 1000)
            _dest, seconds, params, pcm = self._slice(root, source, 1000, 1000 + frames)
            self.assertEqual(frames, params.nframes)
            self.assertEqual(data[1000 * 2:(1000 + frames) * 2], pcm)

    def test_tail_eof_exact_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, data = self._source_wav(
                root, channels=1, width=2, rate=1000, frames=5000)
            _dest, seconds, params, pcm = self._slice(root, source, 4000, 5000)
            self.assertEqual(1000, params.nframes)
            self.assertEqual(data[4000 * 2:], pcm)

    def test_truncated_and_over_limit_refused(self):
        import qwen_align_worker as worker
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _data = self._source_wav(
                root, channels=1, width=2, rate=1000, frames=5000)
            for start, end in ((0, 5001), (-1, 100), (100, 100), (200, 100)):
                with self.assertRaises(ValueError):
                    worker._write_slice_wav(str(source), str(root / "s.wav"),
                                            start, end)

    def test_over_cap_legacy_window_refused_before_scratch_copy(self):
        import qwen_align_worker as worker
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _data = self._source_wav(
                root, channels=1, width=2, rate=1000, frames=181000)
            row = {"text": "One sentence.", "wav_path": str(source), "duration_sec": 181.0}
            with patch.object(worker, "_write_slice_wav", side_effect=AssertionError("must not copy")) as copy:
                with self.assertRaisesRegex(ValueError, "aligner limit"):
                    worker._align_row(object(), row, "English", None, root)
            copy.assert_not_called()

    def test_bad_emitted_geometry_rejected_before_inference(self):
        import qwen_align_worker as worker
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _data = self._source_wav(
                root, channels=1, width=2, rate=1000, frames=10000)
            text = "One. Two."
            spans = qa.cover_ranges(text, qa.split_sentences(text))
            plans = qa.get_aligner_windows(
                10.0, [dict(start=a, end=b, words=1) for a, b in spans], text=text)
            job_windows = [dict(char_start=q["char_start"], char_end=q["char_end"],
                                est_start_sec=q["start_sec"],
                                est_end_sec=q["end_sec"]) for q in plans]
            real_write = worker._write_slice_wav

            def lying_write(src, dest, start, end):
                # Writes double the requested frames but declares the
                # requested duration — the exact R-TIME-5 defect shape.
                with wave.open(str(src), "rb") as stream:
                    params = stream.getparams()
                    stream.setpos(start)
                    pcm = stream.readframes(end - start)
                with wave.open(str(dest), "wb") as out:
                    out.setparams(params)
                    out.writeframes(pcm + pcm)
                return (end - start) / float(params.framerate)

            called = []

            def spy_align(*args, **kwargs):
                called.append(args)
                raise AssertionError("inference called with bad geometry")

            row = {"text": text, "wav_path": str(source),
                   "duration_sec": 10.0, "windows": job_windows}
            with patch.object(worker, "_write_slice_wav", lying_write), \
                    patch.object(worker, "_align_slice", spy_align):
                with self.assertRaises(ValueError):
                    worker._align_row(object(), row, "English", None, root)
            self.assertEqual([], called)
            self.assertIsNotNone(real_write)


class PcmWidthStagingTests(unittest.TestCase):
    """R-TIME-6: Qwen staging must accept core-supported widths 1/2/4."""

    def _stage(self, tmp, *, channels, width, frames, start=0, end=None,
               rate=16000, fragmented=False):
        from readalong_export import _write_row_slice_wav
        end = frames if end is None else end
        frame_bytes = channels * width
        pcm = Path(tmp) / "assembled.pcm"
        raw = bytes(((i * 13 + 5) % 256) for i in range(frames * frame_bytes))
        pcm.write_bytes(raw)
        out = Path(tmp) / "row.wav"
        with open(pcm, "rb") as stream:
            if fragmented:
                real_read = stream.read

                def short_read(n=-1):
                    if n is None or n < 0:
                        n = 997
                    return real_read(min(n, 997))

                stream.read = short_read
            seconds = _write_row_slice_wav(
                stream, str(out), (channels, rate, width), start, end)
        with wave.open(str(out), "rb") as wav:
            params = wav.getparams()
            produced = wav.readframes(wav.getnframes())
        return seconds, params, produced, raw

    def test_widths_1_2_4_mono_and_stereo(self):
        for width in (1, 2, 4):
            for channels in (1, 2):
                with tempfile.TemporaryDirectory() as tmp:
                    seconds, params, produced, raw = self._stage(
                        tmp, channels=channels, width=width, frames=2000,
                        start=100, end=1100)
                self.assertEqual(1000, params.nframes)
                self.assertEqual((channels, width, 16000),
                                 (params.nchannels, params.sampwidth,
                                  params.framerate))
                self.assertEqual(1000 / 16000, seconds)
                frame_bytes = channels * width
                expected = raw[100 * frame_bytes:1100 * frame_bytes]
                if width == 1:
                    expected = bytes(
                        ((b - 256 if b >= 128 else b) + 128) % 256
                        for b in expected)
                self.assertEqual(expected, produced)

    def test_all_256_signed8_values_mapped(self):
        from readalong_export import _write_row_slice_wav
        with tempfile.TemporaryDirectory() as tmp:
            pcm = Path(tmp) / "assembled.pcm"
            raw = bytes(range(256))  # all unsigned byte values = all signed8
            pcm.write_bytes(raw)
            out = Path(tmp) / "row.wav"
            with open(pcm, "rb") as stream:
                _write_row_slice_wav(stream, str(out), (1, 16000, 1), 0, 256)
            with wave.open(str(out), "rb") as wav:
                produced = wav.readframes(wav.getnframes())
        # Independent oracle: signed s -> unsigned s+128 (Pydub bias128).
        expected = bytes((((b - 256 if b >= 128 else b) + 128) % 256)
                         for b in range(256))
        self.assertEqual(expected, produced)
        self.assertEqual(0, produced[128])    # signed -128 -> unsigned 0
        self.assertEqual(128, produced[0])    # signed 0 -> unsigned 128
        self.assertEqual(255, produced[127])  # signed +127 -> unsigned 255

    def test_exact_signed16_32_bytes(self):
        for width in (2, 4):
            with tempfile.TemporaryDirectory() as tmp:
                seconds, params, produced, raw = self._stage(
                    tmp, channels=1, width=width, frames=3000,
                    start=500, end=1500)
                self.assertEqual(raw[500 * width:1500 * width], produced)

    def test_fragmented_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seconds, params, produced, raw = self._stage(
                tmp, channels=1, width=2, frames=5000, fragmented=True)
            self.assertEqual(5000, params.nframes)
            self.assertEqual(raw, produced)

    def test_cancellation(self):
        from readalong_export import _write_row_slice_wav
        from tts import ExportCancelled
        with tempfile.TemporaryDirectory() as tmp:
            pcm = Path(tmp) / "assembled.pcm"
            pcm.write_bytes(b"\x00\x00" * 48000)
            with open(pcm, "rb") as stream:
                with self.assertRaises(ExportCancelled):
                    _write_row_slice_wav(stream, str(Path(tmp) / "o.wav"),
                                         (1, 24000, 2), 0, 24000,
                                         cancel_check=lambda: True)

    def test_malformed_widths_refused(self):
        from readalong_export import _write_row_slice_wav
        with tempfile.TemporaryDirectory() as tmp:
            pcm = Path(tmp) / "assembled.pcm"
            pcm.write_bytes(b"\x00" * 10000)
            for width in (0, 3, 5, -2, True):
                with open(pcm, "rb") as stream:
                    with self.assertRaises(ValueError):
                        _write_row_slice_wav(stream, str(Path(tmp) / "o.wav"),
                                             (1, 16000, width), 0, 100)

    def test_collect_bridge_at_32bit(self):
        # Whole bridge: collect_aligned_rows with the fake worker at
        # width 4 (no torch/model needed).
        from readalong_export import collect_aligned_rows
        text = " ".join("w%d" % i for i in range(30)) + "."
        with tempfile.TemporaryDirectory() as tmp:
            frames = 15 * RATE
            pcm = Path(tmp) / "assembled.pcm"
            pcm.write_bytes(b"\x00\x00\x00\x00" * frames)
            data = {"layout": (1, RATE, 4), "bounds": [(0, frames)],
                    "timeline": [({"text": text}, None, 0)],
                    "pcm_path": str(pcm)}
            with patch.object(qr, "get_qwen_worker_command",
                              lambda _py: [sys.executable, str(FAKE)]):
                rows, report = collect_aligned_rows(
                    data, "English", _capability(tmp), lambda: False,
                    str(Path(tmp) / "stage"), prepared_texts=[text])
        self.assertEqual(1, report["rows"])
        self.assertEqual(30, len(rows[0]["tokens"]))


if __name__ == "__main__":
    unittest.main()
