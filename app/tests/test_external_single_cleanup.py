"""Issue 1797: external single timeout cleanup must not mask the timeout.

Regression coverage for ``TTSEngine._external_single`` staging cleanup:
a timed-out (or failing) provider that still owns its staging file must
not let a cleanup ``OSError`` (Windows WinError 32 while a real handle is
open) replace the operation's own failure.  These tests use synthetic
providers and the real owned ``ExternalTTSExecutor``; no network, live
Gradio server, GPU, or model work runs here.
"""
import io
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

import tts
from audio_validation import publish_audio_output


class _Sentinel(BaseException):
    """Distinct BaseException subclass so a Ctrl-C-like signal can be
    tested without stopping the test runner."""


def _make_engine(root: Path):
    engine = tts.TTSEngine({"tts": {"mode": "external", "url": "http://fixture",
                                    "parallel_workers": 1,
                                    "external_timeout_seconds": 1}})
    return engine


def _capture_failures():
    failures = []
    token = tts._TTS_FAILURES.set(failures)
    return failures, token


def _run_single(engine, provider, output_path):
    failures, token = _capture_failures()
    try:
        result = engine._external_single(provider, str(output_path))
        return result, failures, None
    except BaseException as exc:  # noqa: BLE001
        return None, failures, exc
    finally:
        tts._TTS_FAILURES.reset(token)


def _write_wav(path):
    sf.write(path, np.full(240, 0.2), 24000, format="WAV")


def _valid_wav_bytes() -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.full(240, 0.2), 24000, format="WAV")
    return buf.getvalue()


def _settle_engine(engine, events=()):
    """Release blocked providers, close engine, retire owned executor.

    Cleanup ``Exception`` failures are collected. If no primary exception
    is unwinding (``sys.exc_info()[0] is None``) the first is raised so a
    passing test cannot hide a live worker or cleanup failure. If a primary
    failure is already unwinding, each cleanup failure is logged at ERROR
    and the primary is preserved (secondary reported, not hidden). Only
    ``Exception`` is caught; ``BaseException`` (KeyboardInterrupt/SystemExit)
    is never swallowed. Authoritative bounded retirement is
    ``executor.shutdown(wait=True, timeout=3)``; no per-thread joins.
    """
    failures = []
    for event in events:
        try:
            event.set()
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)
    try:
        engine.close_external()
    except Exception as exc:  # noqa: BLE001
        failures.append(exc)
    executor = getattr(engine, "_external_executor", None)
    if executor is not None:
        try:
            executor.shutdown(wait=True, timeout=3)
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)
    if not failures:
        return
    if sys.exc_info()[0] is not None:
        for exc in failures:
            logging.getLogger(__name__).error(
                "_settle_engine cleanup failure (primary unwinding): %r", exc)
    else:
        for exc in failures[1:]:
            logging.getLogger(__name__).error(
                "_settle_engine additional cleanup failure: %r", exc)
        raise failures[0]


def _deny_pending(path, real_remove):
    if ".pending." in str(path):
        raise PermissionError("simulated pending deletion denied")
    return real_remove(path)


class ExternalSingleCleanupTests(unittest.TestCase):
    def test_native_timeout_keeps_timeout_and_retires_staging(self):
        """Real owned executor + real open staging handle.

        On Windows the provider's open handle makes the caller's timeout
        ``os.remove`` fail with WinError 32; the timeout must still be
        recorded and ``False`` returned.  On POSIX unlink of an open file
        succeeds, so the timeout assertions hold with quiet successful
        unlink and no warning.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _make_engine(root)
            engine._external_timeout = 1
            output = root / "audio.wav"
            output.write_bytes(b"prior bytes")
            entered = threading.Event()
            release = threading.Event()
            provider_done = threading.Event()
            staging_holder = {}
            provider_error = []
            valid_bytes = _valid_wav_bytes()
            # Positive control: identical valid bytes publish fine with a
            # noncancelled event, so no-late-publication cannot pass merely
            # because invalid data would be rejected by a fence regression.
            control_staging = root / "control.pending"
            control_dest = root / "control.wav"
            control_staging.write_bytes(valid_bytes)
            self.assertTrue(publish_audio_output(str(control_staging),
                                                 str(control_dest),
                                                 threading.Event()))
            self.assertEqual(valid_bytes, control_dest.read_bytes())
            audio, rate = sf.read(str(control_dest))
            self.assertEqual((240, 24000), (len(audio), rate))

            def provider(staging_path, cancelled):
                staging_holder["path"] = staging_path
                handle = open(staging_path, "wb")
                staging_holder["handle"] = handle
                try:
                    handle.write(valid_bytes)
                    handle.flush()
                    # Signal only after open/write/flush: proves ownership.
                    entered.set()
                    if not release.wait(5):
                        provider_error.append(RuntimeError("release timeout"))
                    return True
                finally:
                    handle.close()
                    provider_done.set()

            try:
                if os.name == "nt":
                    with self.assertLogs("tts", level="WARNING") as logs:
                        result, failures, exc = _run_single(engine, provider, output)
                        self.assertIsNone(exc, f"cleanup masked the timeout: {exc!r}")
                        self.assertFalse(result)
                        self.assertTrue(entered.is_set())
                        self.assertFalse(provider_done.is_set())
                        self.assertFalse(staging_holder["handle"].closed)
                        self.assertEqual(1, len(failures))
                        self.assertEqual("timeout", failures[0].category)
                        self.assertIn("timed out", failures[0].detail)
                        self.assertIn("Pending audio cleanup failed",
                                      "\n".join(logs.output))
                        self.assertEqual(b"prior bytes", output.read_bytes())
                        self.assertTrue(
                            Path(staging_holder["path"]).exists(),
                            "Windows: failed unlink must leave staging until worker retires it")
                else:
                    with self.assertNoLogs("tts", level="WARNING"):
                        result, failures, exc = _run_single(engine, provider, output)
                        self.assertIsNone(exc, f"cleanup masked the timeout: {exc!r}")
                        self.assertFalse(result)
                        self.assertTrue(entered.is_set())
                        self.assertFalse(provider_done.is_set())
                        self.assertFalse(staging_holder["handle"].closed)
                        self.assertEqual(1, len(failures))
                        self.assertEqual("timeout", failures[0].category)
                        self.assertIn("timed out", failures[0].detail)
                        self.assertEqual(b"prior bytes", output.read_bytes())
                        # POSIX: unlink succeeds while handle is open.
                        self.assertFalse(
                            Path(staging_holder["path"]).exists(),
                            "POSIX: unlink of open staging must succeed quietly")
            finally:
                _settle_engine(engine, (release,))
            self.assertTrue(provider_done.wait(2))
            self.assertEqual([], provider_error)
            staging = Path(staging_holder["path"])
            deadline = time.monotonic() + 3
            while staging.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(staging.exists())
            self.assertEqual(b"prior bytes", output.read_bytes())

    def test_simulated_cleanup_oserror_keeps_timeout_primary(self):
        """Cross-platform policy: injected OSError cannot mask the timeout.

        Patching ``os.remove`` to raise a controlled ``PermissionError``
        exercises the secondary-cleanup condition on any OS.  This is
        simulated policy evidence, not Windows native proof.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _make_engine(root)
            engine._external_timeout = 1
            output = root / "audio.wav"
            output.write_bytes(b"prior bytes")
            release = threading.Event()
            staging_holder = {}

            def provider(staging_path, cancelled):
                staging_holder["path"] = staging_path
                _write_wav(staging_path)
                release.wait(3)
                return True

            real_remove = os.remove
            try:
                with patch.object(tts.os, "remove",
                                  side_effect=lambda p: _deny_pending(p, real_remove)):
                    with self.assertLogs("tts", level="WARNING") as logs:
                        result, failures, exc = _run_single(engine, provider, output)
                        self.assertIsNone(exc, f"cleanup masked the timeout: {exc!r}")
                        self.assertFalse(result)
                        self.assertEqual(1, len(failures))
                        self.assertEqual("timeout", failures[0].category)
                        self.assertIn("timed out", failures[0].detail)
                        self.assertIn("simulated pending deletion denied",
                                      "\n".join(logs.output))
                        self.assertEqual(b"prior bytes", output.read_bytes())
                # Patch exited; blocked worker has not yet run its finally.
                self.assertTrue(Path(staging_holder["path"]).exists())
            finally:
                _settle_engine(engine, (release,))
            self.assertFalse(Path(staging_holder["path"]).exists())
            self.assertEqual([], list(root.glob("*.pending.*")))

    def test_base_exception_sentinel_survives_cleanup_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _make_engine(root)
            engine._external_timeout = 3
            output = root / "audio.wav"
            output.write_bytes(b"prior bytes")
            sentinel = _Sentinel("sentinel")
            holder = {}

            def provider(staging_path, cancelled):
                holder["staging"] = staging_path
                _write_wav(staging_path)
                raise sentinel

            orig_submit = engine._submit_external

            def capturing_submit(function, cancelled, *args):
                holder["cancelled"] = cancelled
                future = orig_submit(function, cancelled, *args)
                holder["future"] = future
                return future

            real_remove = os.remove
            try:
                with patch.object(engine, "_submit_external",
                                 side_effect=capturing_submit):
                    with patch.object(tts.os, "remove",
                                      side_effect=lambda p: _deny_pending(p, real_remove)):
                        with self.assertLogs("tts", level="WARNING") as logs:
                            _, _, exc = _run_single(engine, provider, output)
                            self.assertIs(exc, sentinel)
                            self.assertTrue(holder["future"].done())
                            self.assertIs(holder["future"].exception(), sentinel)
                            self.assertTrue(holder["cancelled"].is_set())
                            self.assertIn("simulated pending deletion denied",
                                          "\n".join(logs.output))
                            self.assertEqual(b"prior bytes", output.read_bytes())
                # Worker already exited before caller denial; fixture-owned
                # residual remains by design (best-effort, no universal claim).
                residual = Path(holder["staging"])
                self.assertTrue(residual.exists())
                audio, rate = sf.read(str(residual))
                self.assertEqual((240, 24000), (len(audio), rate))
                os.remove(residual)
                self.assertFalse(residual.exists())
            finally:
                _settle_engine(engine, ())

    def test_provider_failure_not_masked_by_cleanup_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _make_engine(root)
            engine._external_timeout = 3
            output = root / "audio.wav"
            output.write_bytes(b"prior bytes")

            def provider(staging_path, cancelled):
                raise RuntimeError("provider blew up")

            real_remove = os.remove
            try:
                with patch.object(tts.os, "remove",
                                  side_effect=lambda p: _deny_pending(p, real_remove)):
                    with self.assertLogs("tts", level="WARNING"):
                        result, failures, exc = _run_single(engine, provider, output)
                        self.assertIsNone(exc)
                        self.assertFalse(result)
                        self.assertEqual(b"prior bytes", output.read_bytes())
                        details = [(f.category, f.detail) for f in failures]
                        self.assertTrue(
                            any("provider blew up" in d for _, d in details),
                            details)
                        self.assertFalse(
                            any("simulated pending deletion denied" in d for _, d in details),
                            details)
            finally:
                _settle_engine(engine, ())

    def test_absent_staging_cleanup_stays_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for case, ret in (("false", False), ("true", True)):
                with self.subTest(case=case):
                    engine = _make_engine(root)
                    engine._external_timeout = 3
                    output = root / f"audio_{case}.wav"
                    output.write_bytes(b"prior bytes")

                    def provider(staging_path, cancelled, _ret=ret):
                        return _ret

                    try:
                        with patch.object(tts, "publish_audio_output") as pub:
                            with self.assertNoLogs("tts", level="WARNING"):
                                result, failures, exc = _run_single(engine, provider, output)
                                self.assertIsNone(exc)
                                self.assertFalse(result)
                                self.assertEqual(b"prior bytes", output.read_bytes())
                            pub.assert_not_called()
                    finally:
                        _settle_engine(engine, ())

    def test_healthy_single_publishes_valid_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _make_engine(root)
            engine._external_timeout = 3
            output = root / "audio.wav"
            output.write_bytes(b"prior bytes")

            def provider(staging_path, cancelled):
                _write_wav(staging_path)
                return True

            try:
                result, _, exc = _run_single(engine, provider, output)
                self.assertIsNone(exc)
                self.assertTrue(result)
                audio, rate = sf.read(str(output))
                self.assertEqual(24000, rate)
                self.assertEqual(240, len(audio))
                self.assertEqual([], list(root.glob("*.pending.*")))
            finally:
                _settle_engine(engine, ())

    def test_batch_cleanup_diagnostic_shape_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = tts.TTSEngine({"tts": {"mode": "external", "url": "http://fixture",
                                            "parallel_workers": 1,
                                            "external_timeout_seconds": 1}})
            engine._external_timeout = 0.15
            release = threading.Event()
            entered = threading.Event()
            old = root / "temp_batch_0.wav"
            old.write_bytes(b"prior output")

            def render(text, instruct, speaker, config, path, **kwargs):
                _write_wav(path)
                entered.set()
                if text == "0":
                    release.wait(3)
                return True

            def deny(path):
                if "temp_batch_0.wav.pending." in str(path):
                    raise PermissionError("synthetic pending deletion denied")
                return real_remove(path)

            real_remove = os.remove
            holder = {}
            errors = []

            def run_batch():
                try:
                    with patch.object(engine, "_external_generate_custom",
                                      side_effect=render):
                        with patch.object(tts.os, "remove", side_effect=deny):
                            holder["result"] = engine._external_batch(
                                [{"index": 0, "speaker": "A", "text": "0"}],
                                {"A": {"voice": "Ryan"}}, str(root), "custom")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            try:
                cancelled = threading.Event()
                warm = engine._submit_external(lambda: "warm", cancelled)
                self.assertEqual("warm", warm.result(2))
                worker = threading.Thread(target=run_batch)
                worker.start()
                self.assertTrue(entered.wait(2),
                                "batch provider did not enter before the batch timeout")
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual([], errors)
                result = holder["result"]
                self.assertEqual([], result["completed"])
                self.assertEqual([0], [i for i, _ in result["failed"]])
                self.assertIn("timed out", result["failed"][0][1])
                self.assertIn("synthetic pending deletion denied",
                              result["failed"][0][1])
                self.assertEqual(b"prior output", old.read_bytes())
                # Blocked worker has not retired its pending stage yet.
                self.assertNotEqual([], list(root.glob("*.pending.*")))
            finally:
                _settle_engine(engine, (release,))
            self.assertEqual([], list(root.glob("*.pending.*")))

    def test_retry_budget_constrains_single_timeout(self):
        from speech_retry import SpeechRetryBudget, ensure_speech_retry_budget
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _make_engine(root)
            engine._external_timeout = 300
            output = root / "audio.wav"
            output.write_bytes(b"prior bytes")
            budget = SpeechRetryBudget()
            budget.deadline = time.monotonic() + 1
            release = threading.Event()
            staging_holder = {}

            def provider(staging_path, cancelled):
                staging_holder["path"] = staging_path
                _write_wav(staging_path)
                release.wait(3)
                return True

            real_remove = os.remove
            started = time.monotonic()
            try:
                with ensure_speech_retry_budget(budget):
                    with patch.object(tts.os, "remove",
                                      side_effect=lambda p: _deny_pending(p, real_remove)):
                        with self.assertLogs("tts", level="WARNING") as logs:
                            result, failures, exc = _run_single(engine, provider, output)
                            self.assertIsNone(exc)
                            self.assertFalse(result)
                            self.assertEqual(1, len(failures))
                            self.assertEqual("timeout", failures[0].category)
                            self.assertIn("simulated pending deletion denied",
                                          "\n".join(logs.output))
                            self.assertEqual(b"prior bytes", output.read_bytes())
                            self.assertLess(time.monotonic() - started, 4)
                self.assertTrue(Path(staging_holder["path"]).exists())
            finally:
                _settle_engine(engine, (release,))
            self.assertFalse(Path(staging_holder["path"]).exists())
            self.assertEqual([], list(root.glob("*.pending.*")))


if __name__ == "__main__":
    unittest.main()
