"""Issue 1797: external single timeout cleanup must not mask the timeout.

Regression coverage for ``TTSEngine._external_single`` staging cleanup:
a timed-out (or failing) provider that still owns its staging file must
not let a cleanup ``OSError`` (Windows WinError 32 while a real handle is
open) replace the operation's own failure.  These tests use synthetic
providers and the real owned ``ExternalTTSExecutor``; no network, live
Gradio server, GPU, or model work runs here.
"""
import contextlib
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


def _warm_engine(engine):
    """Boundedly start the real owned executor threads before the timed call.

    No mocked clock/deadline; the provider-entry deadline is still the real
    1s (or budget 1s) clock.
    """
    cancelled = threading.Event()
    future = engine._submit_external(lambda: "warm", cancelled)
    if future.result(2) != "warm":
        raise AssertionError("executor warm-up failed")


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

    Production close already enforces a 2s bound; the 3s
    ``executor.shutdown(wait=True, timeout=3)`` below is only a
    best-effort fallback after a failed close, never a waiver of that
    failure. Every cleanup ``Exception`` is collected: if no primary is
    unwinding (``sys.exc_info()[0] is None``) the first is raised so a
    passing test cannot hide a live worker; if a primary is unwinding each
    is logged at ERROR and the primary is preserved. Only ``Exception`` is
    caught; ``BaseException`` is never swallowed. No per-thread joins.
    """
    failures = []
    for event in events:
        try:
            event.set()
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)
    executor = getattr(engine, "_external_executor", None)
    try:
        engine.close_external()
    except Exception as exc:  # noqa: BLE001
        failures.append(exc)
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


def _deny_pending(path, real_remove, owned_prefix):
    # Scope the injected denial to this fixture's own output; every unrelated
    # pending path (another thread/engine) delegates to the real remove.
    if str(path).startswith(owned_prefix + ".pending."):
        raise PermissionError("simulated pending deletion denied")
    return real_remove(path)


def _assert_stdout_primary(test, stdout, primary, output):
    test.assertIn(primary, stdout)
    test.assertNotIn("Pending audio cleanup failed", stdout)
    test.assertNotIn(str(output), stdout)
    test.assertNotIn("simulated pending deletion denied", stdout)


class ExternalSingleCleanupTests(unittest.TestCase):
    def test_native_timeout_keeps_timeout_and_retires_staging(self):
        """Real owned executor + real open staging handle.

        On Windows the provider's open handle makes the caller's timeout
        ``os.remove`` fail with WinError 32; the timeout plus its cleanup
        diagnostic must still be the sole recorded failure with ``False``
        returned.  On POSIX unlink of an open file succeeds, so the timeout
        assertions hold with quiet successful unlink and no diagnostic.
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
                    release.wait()
                    return True
                finally:
                    handle.close()
                    provider_done.set()

            try:
                _warm_engine(engine)
                if os.name == "nt":
                    with self.assertLogs("tts", level="WARNING") as logs:
                        buf = io.StringIO()
                        with contextlib.redirect_stdout(buf):
                            result, failures, exc = _run_single(engine, provider, output)
                        stdout = buf.getvalue()
                        self.assertIsNone(exc, f"cleanup masked the timeout: {exc!r}")
                        self.assertFalse(result)
                        self.assertTrue(entered.is_set())
                        self.assertFalse(provider_done.is_set())
                        self.assertFalse(staging_holder["handle"].closed)
                        self.assertEqual(1, len(failures))
                        self.assertEqual("timeout", failures[0].category)
                        self.assertIn("timed out after 1s", failures[0].detail)
                        self.assertNotIn("speech retry deadline", failures[0].detail)
                        self.assertIn("Pending audio cleanup failed",
                                      failures[0].detail)
                        self.assertIn("Pending audio cleanup failed",
                                      "\n".join(logs.output))
                        _assert_stdout_primary(self, stdout,
                                               "External TTS timed out after 1s", output)
                        self.assertEqual(b"prior bytes", output.read_bytes())
                        self.assertTrue(
                            Path(staging_holder["path"]).exists(),
                            "Windows: failed unlink must leave staging until worker retires it")
                else:
                    with self.assertNoLogs("tts", level="WARNING"):
                        buf = io.StringIO()
                        with contextlib.redirect_stdout(buf):
                            result, failures, exc = _run_single(engine, provider, output)
                        stdout = buf.getvalue()
                        self.assertIsNone(exc, f"cleanup masked the timeout: {exc!r}")
                        self.assertFalse(result)
                        self.assertTrue(entered.is_set())
                        self.assertFalse(provider_done.is_set())
                        self.assertFalse(staging_holder["handle"].closed)
                        self.assertEqual(1, len(failures))
                        self.assertEqual("timeout", failures[0].category)
                        self.assertIn("timed out after 1s", failures[0].detail)
                        self.assertNotIn("Pending audio cleanup failed",
                                         failures[0].detail)
                        _assert_stdout_primary(self, stdout,
                                               "External TTS timed out after 1s", output)
                        self.assertEqual(b"prior bytes", output.read_bytes())
                        # POSIX: unlink succeeds while handle is open.
                        self.assertFalse(
                            Path(staging_holder["path"]).exists(),
                            "POSIX: unlink of open staging must succeed quietly")
            finally:
                _settle_engine(engine, (release,))
            self.assertTrue(provider_done.wait(2))
            # The captured list is shared with the worker via ContextVar, so a
            # late provider error would have appended a second entry by now.
            self.assertEqual(1, len(failures))
            self.assertEqual("timeout", failures[0].category)
            self.assertIn("timed out", failures[0].detail)
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
                release.wait()
                return True

            real_remove = os.remove
            try:
                _warm_engine(engine)
                with patch.object(tts.os, "remove",
                                  side_effect=lambda p: _deny_pending(p, real_remove, str(output))):
                    with self.assertLogs("tts", level="WARNING") as logs:
                        buf = io.StringIO()
                        with contextlib.redirect_stdout(buf):
                            result, failures, exc = _run_single(engine, provider, output)
                        stdout = buf.getvalue()
                        self.assertIsNone(exc, f"cleanup masked the timeout: {exc!r}")
                        self.assertFalse(result)
                        self.assertEqual(1, len(failures))
                        self.assertEqual("timeout", failures[0].category)
                        self.assertIn("timed out after 1s", failures[0].detail)
                        self.assertNotIn("speech retry deadline", failures[0].detail)
                        self.assertIn("simulated pending deletion denied",
                                      failures[0].detail)
                        self.assertIn("simulated pending deletion denied",
                                      "\n".join(logs.output))
                        _assert_stdout_primary(self, stdout,
                                               "External TTS timed out after 1s", output)
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
                                      side_effect=lambda p: _deny_pending(p, real_remove, str(output))):
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
                                  side_effect=lambda p: _deny_pending(p, real_remove, str(output))):
                    with self.assertLogs("tts", level="WARNING"):
                        result, failures, exc = _run_single(engine, provider, output)
                        self.assertIsNone(exc)
                        self.assertFalse(result)
                        self.assertEqual(b"prior bytes", output.read_bytes())
                        self.assertEqual(1, len(failures))
                        self.assertIn("provider blew up", failures[0].detail)
                        self.assertNotIn("simulated pending deletion denied",
                                         failures[0].detail)
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
                with self.assertNoLogs("tts", level="WARNING"):
                    result, failures, exc = _run_single(engine, provider, output)
                    self.assertIsNone(exc)
                    self.assertTrue(result)
                    self.assertEqual([], failures)
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
                    release.wait()
                return True

            real_remove = os.remove

            def deny(path):
                if str(path).startswith(str(old) + ".pending."):
                    raise PermissionError("synthetic pending deletion denied")
                return real_remove(path)

            try:
                cancelled = threading.Event()
                warm = engine._submit_external(lambda: "warm", cancelled)
                self.assertEqual("warm", warm.result(2))
                with patch.object(engine, "_external_generate_custom",
                                  side_effect=render):
                    with patch.object(tts.os, "remove", side_effect=deny):
                        result = engine._external_batch(
                            [{"index": 0, "speaker": "A", "text": "0"}],
                            {"A": {"voice": "Ryan"}}, str(root), "custom")
                self.assertTrue(entered.is_set())
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
            cases = [
                ("budget_limit", 300, 1, None, "budget"),
                ("external_limit", 1, 300, None, "external"),
                ("budget_extended_after_submit", 300, 1, "extend", "budget"),
                ("budget_shortened_after_submit", 1, 300, "shorten", "external"),
            ]
            for name, ext_timeout, budget_seconds, mutation, expected in cases:
                with self.subTest(case=name):
                    case_root = root / name
                    case_root.mkdir()
                    engine = _make_engine(case_root)
                    engine._external_timeout = ext_timeout
                    output = case_root / "audio.wav"
                    output.write_bytes(b"prior bytes")
                    release = threading.Event()
                    entered = threading.Event()
                    staging_holder = {}
                    budget_holder = {}
                    mutation_holder = {}

                    def provider(staging_path, cancelled):
                        staging_holder["path"] = staging_path
                        _write_wav(staging_path)
                        if mutation == "extend":
                            budget_holder["budget"].deadline = time.monotonic() + 300
                            mutation_holder["ts"] = time.monotonic()
                            entered.set()
                        elif mutation == "shorten":
                            budget_holder["budget"].deadline = time.monotonic() - 1
                            mutation_holder["ts"] = time.monotonic()
                            entered.set()
                        else:
                            entered.set()
                        release.wait()
                        return True

                    real_remove = os.remove
                    try:
                        _warm_engine(engine)
                        budget = SpeechRetryBudget()
                        budget.deadline = time.monotonic() + budget_seconds
                        original_deadline = budget.deadline
                        budget_holder["budget"] = budget
                        started = time.monotonic()
                        if name == "external_limit":
                            # Public API: patch only the provider surface and
                            # guard real SDK/network dispatch off.
                            def custom_adapter(text, instruct, speaker, voice_config,
                                             output_path, endpoint=None, cancelled=None):
                                staging_holder["path"] = output_path
                                _write_wav(output_path)
                                entered.set()
                                release.wait()
                                return True

                            with ensure_speech_retry_budget(budget):
                                with patch.object(engine, "_external_generate_custom",
                                                  side_effect=custom_adapter), \
                                     patch.object(engine, "_external_endpoint",
                                                  side_effect=AssertionError("Unexpected SDK/network dispatch")), \
                                     patch.object(tts, "apply_tts_failure",
                                                  wraps=tts.apply_tts_failure) as spy, \
                                     patch.object(tts.os, "remove",
                                                  side_effect=lambda p: _deny_pending(p, real_remove, str(output))):
                                    with self.assertLogs("tts", level="WARNING") as logs:
                                        buf = io.StringIO()
                                        with contextlib.redirect_stdout(buf):
                                            result = engine.generate_voice_result(
                                                "Synthetic greeting.", "", "A",
                                                {"A": {"voice": "Ryan"}}, str(output))
                                        stdout = buf.getvalue()
                                        self.assertFalse(result.success)
                                        self.assertIsNotNone(result.failure)
                                        self.assertEqual("timeout", result.failure.category)
                                        self.assertIn("timed out after 1s", result.failure.detail)
                                        self.assertNotIn("speech retry deadline", result.failure.detail)
                                        self.assertIn("simulated pending deletion denied",
                                                      result.failure.detail)
                                        self.assertIn("simulated pending deletion denied",
                                                      "\n".join(logs.output))
                                        self.assertEqual(1, spy.call_count)
                                        spy_error = spy.call_args.args[0]
                                        self.assertIsInstance(spy_error, TimeoutError)
                                        self.assertIn("timed out after 1s", str(spy_error))
                                        self.assertIn("simulated pending deletion denied",
                                                      str(spy_error))
                                        _assert_stdout_primary(self, stdout,
                                                               "External TTS timed out after 1s", output)
                                        self.assertEqual(b"prior bytes", output.read_bytes())
                                        self.assertLess(time.monotonic() - started, 4)
                            self.assertTrue(entered.is_set())
                            self.assertTrue(Path(staging_holder["path"]).exists())
                        else:
                            with ensure_speech_retry_budget(budget):
                                with patch.object(tts.os, "remove",
                                                  side_effect=lambda p: _deny_pending(p, real_remove, str(output))):
                                    with self.assertLogs("tts", level="WARNING") as logs:
                                        buf = io.StringIO()
                                        with contextlib.redirect_stdout(buf):
                                            result, failures, exc = _run_single(engine, provider, output)
                                        stdout = buf.getvalue()
                                        self.assertIsNone(exc)
                                        self.assertFalse(result)
                                        self.assertEqual(1, len(failures))
                                        self.assertEqual("timeout", failures[0].category)
                                        if expected == "budget":
                                            primary = "speech retry deadline"
                                            self.assertIn(primary, failures[0].detail)
                                            self.assertIn("configured external limit", failures[0].detail)
                                            self.assertNotIn("timed out after 300s", failures[0].detail)
                                            _assert_stdout_primary(self, stdout,
                                                                   "speech retry deadline", output)
                                            self.assertIn("configured external limit", stdout)
                                        else:
                                            self.assertIn("timed out after 1s", failures[0].detail)
                                            self.assertNotIn("speech retry deadline", failures[0].detail)
                                            _assert_stdout_primary(self, stdout,
                                                                   "External TTS timed out after 1s", output)
                                        self.assertIn("simulated pending deletion denied",
                                                      failures[0].detail)
                                        self.assertIn("simulated pending deletion denied",
                                                      "\n".join(logs.output))
                                        self.assertEqual(b"prior bytes", output.read_bytes())
                                        self.assertLess(time.monotonic() - started, 4)
                                        if mutation is not None:
                                            self.assertIn("ts", mutation_holder)
                                            earliest = min(original_deadline, started + ext_timeout)
                                            self.assertLess(mutation_holder["ts"], earliest)
                            self.assertTrue(entered.is_set())
                            self.assertTrue(Path(staging_holder["path"]).exists())
                    finally:
                        _settle_engine(engine, (release,))
                    self.assertFalse(Path(staging_holder["path"]).exists())
                    self.assertEqual([], list(case_root.glob("*.pending.*")))

    def test_worker_finally_cleanup_denial_keeps_future_result_and_timeout(self):
        """Worker-finally denial after caller timeout (simulated policy).

        After the caller has already recorded its timeout, the still-owned
        worker's ``finally`` cleanup also runs under the same denial. That
        second ``OSError`` must be logged without turning the worker future
        into an exception. Under permanent denial the stage may remain; the
        fixture cleans its own residual after the patch ends.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _make_engine(root)
            engine._external_timeout = 1
            output = root / "audio.wav"
            output.write_bytes(b"prior bytes")
            release = threading.Event()
            holder = {}

            def provider(staging_path, cancelled):
                holder["staging"] = staging_path
                _write_wav(staging_path)
                release.wait()
                return True

            orig_submit = engine._submit_external

            def capturing_submit(function, cancelled, *args):
                holder["cancelled"] = cancelled
                future = orig_submit(function, cancelled, *args)
                holder["future"] = future
                return future

            real_remove = os.remove
            try:
                _warm_engine(engine)
                with patch.object(engine, "_submit_external",
                                 side_effect=capturing_submit):
                    with patch.object(tts.os, "remove",
                                      side_effect=lambda p: _deny_pending(p, real_remove, str(output))):
                        with self.assertLogs("tts", level="WARNING") as logs:
                            buf = io.StringIO()
                            with contextlib.redirect_stdout(buf):
                                result, failures, exc = _run_single(engine, provider, output)
                            stdout = buf.getvalue()
                            self.assertIsNone(exc, f"cleanup masked the timeout: {exc!r}")
                            self.assertFalse(result)
                            self.assertEqual(1, len(failures))
                            self.assertEqual("timeout", failures[0].category)
                            self.assertIn("timed out after 1s", failures[0].detail)
                            self.assertIn("simulated pending deletion denied",
                                          failures[0].detail)
                            _assert_stdout_primary(self, stdout,
                                                   "External TTS timed out after 1s", output)
                            # Release while denial is STILL ACTIVE so the
                            # worker-finally site itself is denied.
                            release.set()
                            worker_result = holder["future"].result(timeout=3)
                            self.assertTrue(worker_result[0])
                            self.assertIn("simulated pending deletion denied",
                                          "\n".join(logs.output))
                            self.assertEqual(1, len(failures))
                            self.assertEqual(b"prior bytes", output.read_bytes())
                self.assertEqual(b"prior bytes", output.read_bytes())
                # Permanent denial may leave the stage; remove fixture-owned
                # residual now that the patch has ended.
                residual = Path(holder["staging"])
                self.assertTrue(residual.exists())
                os.remove(residual)
                self.assertFalse(residual.exists())
            finally:
                _settle_engine(engine, (release,))


if __name__ == "__main__":
    unittest.main()
