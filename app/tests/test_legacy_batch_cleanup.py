"""Legacy batch staging cleanup after a non-cancellation engine exception.

Covers issue 1798: when the production ``generate_chunks_batch`` dispatches a
batch and the engine writes some ``temp_batch_<idx>.wav`` files and then raises,
the owned unpublished legacy temps for that dispatched batch must be retired
while the original exception, published audio, refused rows and unrelated
queued slots are preserved. Registered workspace outputs are never handed to
the legacy remover (their retirement is identity-based and separate).
"""
import contextlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from tests import test_chunk_generation_identity

_AMPLITUDE = {"A": 0.1, "B": 0.2, "C": 0.3}


def _write_wav(path, text):
    sf.write(path, np.full(4000, _AMPLITUDE.get(text[0], 0.4), dtype="float32"), 16000)


class LegacyBatchCleanupTests(unittest.TestCase):
    def setUp(self):
        self._fixture = test_chunk_generation_identity.ChunkGenerationIdentityTests()

    def get_manager(self, root):
        return self._fixture.get_manager(root)

    def publish_prior(self, manager, root, indices):
        rows = manager.load_chunks()
        published = {}
        for i in indices:
            path = Path(manager.voicelines_dir) / ("prior-%d.wav" % i)
            _write_wav(path, rows[i]["text"])
            rows[i]["audio_path"] = str(path.relative_to(root))
            rows[i]["status"] = "done"
            published[str(path.relative_to(root))] = path.read_bytes()
        manager.save_chunks(rows)
        return published

    def test_engine_partial_write_then_raise_cleans_owned_temps(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.get_manager(root)
            published = self.publish_prior(manager, root, (0, 1))
            unrelated = root / "temp_batch_2.wav"
            unrelated.write_bytes(b"unrelated queued slot")
            error = RuntimeError("synthetic partial batch failure")
            seen = []

            def batch(chunks, config, output, seed):
                for row in chunks:
                    _write_wav(Path(output) / ("temp_batch_%d.wav" % row["index"]), row["text"])
                    seen.append(row["index"])
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)
            with self.assertRaises(RuntimeError) as ctx:
                manager.generate_chunks_batch([0, 1], batch_size=2)
            self.assertIs(error, ctx.exception)
            self.assertEqual([0, 1], seen)
            # Owned unpublished stages are retired; unrelated slot untouched.
            self.assertFalse((root / "temp_batch_0.wav").exists())
            self.assertFalse((root / "temp_batch_1.wav").exists())
            self.assertEqual(b"unrelated queued slot", unrelated.read_bytes())
            # Published bytes and references preserved.
            for rel, data in published.items():
                self.assertEqual(data, (root / rel).read_bytes())
                samples, rate = sf.read(root / rel)
                self.assertEqual(16000, rate)
            saved = manager.load_chunks()
            refs = list(published)
            for i in (0, 1):
                self.assertEqual("pending", saved[i]["status"])
                self.assertEqual(refs[i], saved[i]["audio_path"])
                self.assertEqual({"keep": "ABC"[i]}, saved[i]["custom"])
                self.assertEqual(120, saved[i]["pause_after"])

    def test_later_batch_failure_preserves_earlier_publication_and_future_temp(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.get_manager(root)
            future = root / "temp_batch_2.wav"
            future.write_bytes(b"future queued slot")
            error = RuntimeError("second batch failure")
            calls = []

            def batch(chunks, config, output, seed):
                calls.append([c["index"] for c in chunks])
                for row in chunks:
                    _write_wav(Path(output) / ("temp_batch_%d.wav" % row["index"]), row["text"])
                if len(calls) == 1:
                    return {"completed": [chunks[0]["index"]], "failed": []}
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)
            with patch("project._export_audio_segment", side_effect=RuntimeError("force actual WAV fallback")):
                with self.assertRaises(RuntimeError) as ctx:
                    manager.generate_chunks_batch([0, 1, 2], batch_size=1)
            self.assertIs(error, ctx.exception)
            self.assertEqual([[0], [1]], calls)
            saved = manager.load_chunks()
            # Earlier real publication survives with valid audio.
            self.assertEqual("done", saved[0]["status"])
            published_path = root / saved[0]["audio_path"]
            self.assertTrue(published_path.exists())
            samples, rate = sf.read(published_path)
            self.assertEqual((4000, 16000), (len(samples), rate))
            self.assertAlmostEqual(0.1, float(samples.mean()), places=3)
            # Failed current batch temp retired; future queued temp remains.
            self.assertFalse((root / "temp_batch_0.wav").exists())
            self.assertFalse((root / "temp_batch_1.wav").exists())
            self.assertEqual(b"future queued slot", future.read_bytes())
            self.assertEqual("pending", saved[1]["status"])
            self.assertIsNone(saved[1]["audio_path"])
            self.assertEqual("pending", saved[2]["status"])

    def test_partial_write_with_absent_stage_stays_quiet(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.get_manager(root)
            error = RuntimeError("only first stage written")

            def batch(chunks, config, output, seed):
                _write_wav(Path(output) / "temp_batch_0.wav", chunks[0]["text"])
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)
            with self.assertRaises(RuntimeError) as ctx:
                manager.generate_chunks_batch([0, 1], batch_size=2)
            self.assertIs(error, ctx.exception)
            self.assertFalse((root / "temp_batch_0.wav").exists())
            self.assertFalse((root / "temp_batch_1.wav").exists())
            saved = manager.load_chunks()
            self.assertEqual(["pending", "pending"], [saved[i]["status"] for i in (0, 1)])

    def test_permanent_deletion_denial_retains_original_and_attempts_other_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.get_manager(root)
            error = RuntimeError("denied cleanup failure")
            denied = root / "temp_batch_0.wav"

            def batch(chunks, config, output, seed):
                for row in chunks:
                    _write_wav(Path(output) / ("temp_batch_%d.wav" % row["index"]), row["text"])
                attempts.clear()  # Count only the failure-cleanup attempts.
                armed.append(True)
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)
            real_remove = os.remove
            attempts = {}
            armed = []

            def fake_remove(path):
                attempts[str(path)] = attempts.get(str(path), 0) + 1
                # Deny only the failure-cleanup phase, never batch preparation
                # (remove_stale_audio), so dispatch semantics are unchanged.
                if armed and os.path.abspath(path) == os.path.abspath(denied):
                    raise OSError("simulated permanent deletion denial")
                return real_remove(path)

            out = io.StringIO()
            with patch("os.remove", side_effect=fake_remove), contextlib.redirect_stdout(out):
                with self.assertRaises(RuntimeError) as ctx:
                    manager.generate_chunks_batch([0, 1], batch_size=2)
            # Original exception identity preserved despite cleanup denial.
            self.assertIs(error, ctx.exception)
            # Bounded retry policy preserved (3 attempts); other owned path retired.
            self.assertEqual(3, attempts.get(str(denied)))
            self.assertIn(str(denied), out.getvalue())
            self.assertFalse((root / "temp_batch_1.wav").exists())
            # Explicit residual under permanent denial; the TemporaryDirectory
            # fixture retires it after the patch. No universal deletion claim.
            self.assertTrue(denied.exists())
            saved = manager.load_chunks()
            self.assertEqual(["pending", "pending"], [saved[i]["status"] for i in (0, 1)])

    def test_stale_insert_edit_during_failing_batch_preserves_live_rows(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.get_manager(root)
            published = self.publish_prior(manager, root, (0,))
            error = RuntimeError("failure after structural edit")
            inserted_snapshot = []

            def batch(chunks, config, output, seed):
                manager.insert_chunk(0)
                manager.update_chunk(1, {"text": "inserted safe"})
                manager._update_chunk_fields(1, status="editing", error="keep inserted")
                inserted_snapshot.extend(manager.load_chunks()[1:2])
                for row in chunks:
                    _write_wav(Path(output) / ("temp_batch_%d.wav" % row["index"]), row["text"])
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)
            with self.assertRaises(RuntimeError) as ctx:
                manager.generate_chunks_batch([0, 1], batch_size=2)
            self.assertIs(error, ctx.exception)
            saved = manager.load_chunks()
            # Live inserted/edited row preserved exactly; never overwritten by a
            # stale whole-list save.
            self.assertEqual(inserted_snapshot[0], saved[1])
            self.assertEqual("inserted safe", saved[1]["text"])
            self.assertEqual("editing", saved[1]["status"])
            # Dispatched owned temps retired; published prior file intact.
            self.assertFalse((root / "temp_batch_0.wav").exists())
            self.assertFalse((root / "temp_batch_1.wav").exists())
            for rel, data in published.items():
                self.assertEqual(data, (root / rel).read_bytes())
                self.assertEqual(rel, next(r["audio_path"] for r in saved if r["uid"] == "uid-A"))

    def test_refused_row_temp_is_not_deleted_by_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.get_manager(root)
            # Unrelated pre-existing legacy temp for a row that will be refused
            # during batch preparation (never dispatched to the engine).
            sentinel = root / "temp_batch_1.wav"
            sentinel.write_bytes(b"refused row unrelated temp")
            error = RuntimeError("failure after refused row")
            original_update = manager._update_generation_fields
            update_calls = []
            edited = []

            def update_with_edit(chunk, **kwargs):
                update_calls.append(chunk.get("uid"))
                # Edit uid-B's live row only on its SECOND generation-field
                # update: the outer reservation passes, but the later
                # batch-preparation guard then compares the original captured
                # row against the changed live uid and returns None, so row 1
                # is in batch_indices yet never admitted to batch_chunks.
                if chunk.get("uid") == "uid-B" and update_calls.count("uid-B") == 2:
                    edited.append(True)
                    manager.update_chunk(1, {"text": "C changed"})
                return original_update(chunk, **kwargs)

            def batch(chunks, config, output, seed):
                self.assertEqual([0], [c["index"] for c in chunks])
                _write_wav(Path(output) / "temp_batch_0.wav", chunks[0]["text"])
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)
            with patch.object(manager, "_update_generation_fields", side_effect=update_with_edit):
                with self.assertRaises(RuntimeError) as ctx:
                    manager.generate_chunks_batch([0, 1], batch_size=2)
            self.assertIs(error, ctx.exception)
            # Preparation guard for uid-B really ran twice (reservation + prep).
            self.assertEqual(["uid-A", "uid-B", "uid-A", "uid-B"], update_calls)
            self.assertEqual([True], edited)
            # Dispatched row cleaned; refused row's temp NOT deleted.
            self.assertFalse((root / "temp_batch_0.wav").exists())
            self.assertTrue(sentinel.exists(), "refused row temp must survive failure cleanup")
            self.assertEqual(b"refused row unrelated temp", sentinel.read_bytes())
            saved = manager.load_chunks()
            self.assertEqual("C changed", saved[1]["text"])
            self.assertEqual("pending", saved[0]["status"])
            self.assertEqual("pending", saved[1]["status"])

    def test_secondary_cleanup_exception_warns_and_preserves_original(self):
        # A per-index secondary failure in the retirement helper itself (not
        # the bounded remover's OSError path) must be logged as a WARNING,
        # must not mask the original engine exception, and must not stop
        # retirement of the other owned paths.
        from synthesis_recovery_context import RecoveryAdmissionError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.get_manager(root)
            error = RuntimeError("engine failure with secondary cleanup error")

            def batch(chunks, config, output, seed):
                for row in chunks:
                    _write_wav(Path(output) / ("temp_batch_%d.wav" % row["index"]), row["text"])
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)
            original_remove = manager._remove_batch_temp_file

            def flaky_remove(idx):
                if idx == 0:
                    raise RecoveryAdmissionError("registered owner guard denied legacy cleanup")
                return original_remove(idx)

            with patch.object(manager, "_remove_batch_temp_file", side_effect=flaky_remove), \
                    self.assertLogs("project", level="WARNING") as logs, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as ctx:
                    manager.generate_chunks_batch([0, 1], batch_size=2)
            self.assertIs(error, ctx.exception)
            warnings = [line for line in logs.output if "could not retire failed batch temp file" in line]
            self.assertEqual(1, len(warnings), logs.output)
            self.assertIn("RecoveryAdmissionError", warnings[0])
            # Other owned path retired; first residual is explicit and retired
            # by the TemporaryDirectory fixture after the patch.
            self.assertFalse((root / "temp_batch_1.wav").exists())
            self.assertTrue((root / "temp_batch_0.wav").exists())
            saved = manager.load_chunks()
            self.assertEqual(["pending", "pending"], [saved[i]["status"] for i in (0, 1)])

    def test_reset_failure_does_not_mask_original_engine_error(self):
        # A secondary failure in the post-failure state reset must warn and
        # preserve the original engine exception (identity + traceback).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.get_manager(root)
            published = self.publish_prior(manager, root, (0, 1))
            error = RuntimeError("synthetic engine failure")
            secondary = OSError("synthetic reset failure")

            def batch(chunks, config, output, seed):
                for row in chunks:
                    _write_wav(Path(output) / ("temp_batch_%d.wav" % row["index"]), row["text"])
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)
            with patch.object(manager, "_reset_captured_generations", side_effect=secondary), \
                    self.assertLogs("project", level="WARNING") as logs, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as ctx:
                    manager.generate_chunks_batch([0, 1], batch_size=2)
            self.assertIs(error, ctx.exception)
            warnings = [line for line in logs.output if "reset" in line]
            self.assertEqual(1, len(warnings), logs.output)
            self.assertIn("OSError", warnings[0])
            # Owned temps retired before the reset seam failed; published
            # audio bytes and references intact.
            self.assertFalse((root / "temp_batch_0.wav").exists())
            self.assertFalse((root / "temp_batch_1.wav").exists())
            for rel, data in published.items():
                self.assertEqual(data, (root / rel).read_bytes())
            # The reset never completed, so no reset success is claimed:
            # rows remain in their pre-reset generating state.
            saved = manager.load_chunks()
            self.assertEqual(["generating", "generating"], [saved[i]["status"] for i in (0, 1)])

    def test_admission_failure_defers_retirement_and_preserves_original(self):
        # Under failed admission the cancellation predicate's authority is
        # UNKNOWN: retirement and reset must be deferred (fail closed), the
        # secondary admission error warned about, and the original engine
        # exception preserved. The live predicate is unpatched; a real typed
        # owner admit callback breaks only after the engine has written.
        from audio_generation_owner import AudioGenerationOwner, ensure_audio_generation_owner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.get_manager(root)
            published = self.publish_prior(manager, root, (0, 1))
            error = RuntimeError("synthetic engine failure")
            secondary = OSError("synthetic admission failure")
            broken = []
            calls = {"remove": [], "reset": []}

            @contextlib.contextmanager
            def admit(**kwargs):
                if broken:
                    raise secondary
                yield

            def batch(chunks, config, output, seed):
                for row in chunks:
                    _write_wav(Path(output) / ("temp_batch_%d.wav" % row["index"]), row["text"])
                broken.append(True)
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)

            def recording_remove(idx):
                calls["remove"].append(idx)
                raise AssertionError("legacy remover called under unknown admission")

            def recording_reset(*args, **kwargs):
                calls["reset"].append(args)
                raise AssertionError("state reset called under unknown admission")

            with ensure_audio_generation_owner(AudioGenerationOwner("a" * 32, admit)), \
                    patch.object(manager, "_remove_batch_temp_file", side_effect=recording_remove), \
                    patch.object(manager, "_reset_captured_generations", side_effect=recording_reset), \
                    self.assertLogs("project", level="WARNING") as logs, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as ctx:
                    manager.generate_chunks_batch([0, 1], batch_size=2)
                self.assertIs(error, ctx.exception)
                warnings = [line for line in logs.output if "admission" in line]
                self.assertEqual(1, len(warnings), logs.output)
                self.assertIn("OSError", warnings[0])
                # Fail closed: no unlink, no reset under unknown authority.
                self.assertEqual([], calls["remove"])
                self.assertEqual([], calls["reset"])
                # Owned stages remain explicitly deferred for a later safe
                # attempt; published audio intact.
                self.assertTrue((root / "temp_batch_0.wav").exists())
                self.assertTrue((root / "temp_batch_1.wav").exists())
                for rel, data in published.items():
                    self.assertEqual(data, (root / rel).read_bytes())
                saved = manager.load_chunks()
                self.assertEqual(set(published), {saved[i]["audio_path"] for i in (0, 1)})
            # The TemporaryDirectory fixture retires the deferred residual
            # after the owner context has exited.

    def test_engine_mutated_row_index_cannot_steer_cleanup(self):
        # The engine receives batch_chunks by reference and can mutate a
        # row's index before raising. Retirement must use the indexes
        # admitted before the engine was handed the rows: owned stages are
        # retired, the unsubmitted future slot is never deleted.
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.get_manager(root)
            published = self.publish_prior(manager, root, (0, 1))
            future = root / "temp_batch_2.wav"
            future.write_bytes(b"future unsubmitted slot")
            error = RuntimeError("synthetic engine failure")

            def batch(chunks, config, output, seed):
                for row in chunks:
                    _write_wav(Path(output) / ("temp_batch_%d.wav" % row["index"]), row["text"])
                chunks[0]["index"] = 2
                raise error

            manager.engine = SimpleNamespace(generate_batch=batch)
            with self.assertRaises(RuntimeError) as ctx:
                manager.generate_chunks_batch([0, 1], batch_size=2)
            self.assertIs(error, ctx.exception)
            self.assertFalse((root / "temp_batch_0.wav").exists())
            self.assertFalse((root / "temp_batch_1.wav").exists())
            self.assertEqual(b"future unsubmitted slot", future.read_bytes())
            for rel, data in published.items():
                self.assertEqual(data, (root / rel).read_bytes())
            saved = manager.load_chunks()
            self.assertEqual(["pending", "pending"], [saved[i]["status"] for i in (0, 1)])

    def test_unprintable_secondary_repr_preserves_original_engine_error(self):
        # A secondary exception whose __repr__ throws but whose __str__ is
        # printable must never replace the original: the original engine error is
        # preserved, the WARNING still carries the secondary class/message,
        # unknown admission still authorizes no unlink/reset, other admitted
        # paths are still retired after a helper failure, and a failed reset
        # is diagnosed without claiming success.
        from audio_generation_owner import AudioGenerationOwner, ensure_audio_generation_owner

        class BadReprError(OSError):
            def __str__(self):
                return "printable secondary message"

            def __repr__(self):
                raise RuntimeError("repr boom")

        for mode in ("admission", "cleanup", "reset"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager = self.get_manager(root)
                published = self.publish_prior(manager, root, (0, 1))
                error = RuntimeError("synthetic engine failure")
                secondary = BadReprError()
                broken = []
                calls = {"remove": [], "reset": []}

                @contextlib.contextmanager
                def admit(**kwargs):
                    if broken:
                        raise secondary
                    yield

                def batch(chunks, config, output, seed):
                    for row in chunks:
                        _write_wav(Path(output) / ("temp_batch_%d.wav" % row["index"]), row["text"])
                    if mode == "admission":
                        broken.append(True)
                    raise error

                manager.engine = SimpleNamespace(generate_batch=batch)
                patches = []
                if mode == "cleanup":
                    original_remove = manager._remove_batch_temp_file

                    def flaky_remove(idx):
                        calls["remove"].append(idx)
                        if idx == 0:
                            raise secondary
                        return original_remove(idx)

                    patches.append(patch.object(manager, "_remove_batch_temp_file", side_effect=flaky_remove))
                if mode == "reset":
                    def failing_reset(*args, **kwargs):
                        calls["reset"].append(args)
                        raise secondary

                    patches.append(patch.object(manager, "_reset_captured_generations", side_effect=failing_reset))
                if mode == "admission":
                    def denied_remove(idx):
                        calls["remove"].append(idx)
                        raise AssertionError("legacy remover called under unknown admission")

                    def denied_reset(*args, **kwargs):
                        calls["reset"].append(args)
                        raise AssertionError("state reset called under unknown admission")

                    patches.append(patch.object(manager, "_remove_batch_temp_file", side_effect=denied_remove))
                    patches.append(patch.object(manager, "_reset_captured_generations", side_effect=denied_reset))
                owner_context = contextlib.ExitStack()
                if mode == "admission":
                    owner_context.enter_context(ensure_audio_generation_owner(AudioGenerationOwner("a" * 32, admit)))
                with owner_context, contextlib.ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    with self.assertLogs("project", level="WARNING") as logs, \
                            contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaises(RuntimeError) as ctx:
                            manager.generate_chunks_batch([0, 1], batch_size=2)
                    self.assertIs(error, ctx.exception)
                    warnings = [line for line in logs.output
                                if "BadReprError" in line and "printable secondary message" in line]
                    self.assertEqual(1, len(warnings), logs.output)
                    for rel, data in published.items():
                        self.assertEqual(data, (root / rel).read_bytes())
                    if mode == "admission":
                        self.assertEqual([], calls["remove"])
                        self.assertEqual([], calls["reset"])
                        self.assertTrue((root / "temp_batch_0.wav").exists())
                        self.assertTrue((root / "temp_batch_1.wav").exists())
                    elif mode == "cleanup":
                        self.assertFalse((root / "temp_batch_1.wav").exists())
                        self.assertTrue((root / "temp_batch_0.wav").exists())
                    else:
                        self.assertFalse((root / "temp_batch_0.wav").exists())
                        self.assertFalse((root / "temp_batch_1.wav").exists())
                        saved = manager.load_chunks()
                        self.assertEqual(["generating", "generating"],
                                         [saved[i]["status"] for i in (0, 1)])

    def test_registered_output_is_not_handed_to_legacy_remover(self):
        # Real synthesis_recovery_context binding/owner fixtures: while a
        # registered output is bound, _remove_batch_temp_file (the helper the
        # new failure branch reuses, as do cancellation/finalization) must not
        # hand anything to the legacy remover. This qualifies the guard only;
        # it is not end-to-end registered workspace retirement proof.
        from contextlib import nullcontext

        from audio_generation_owner import AudioGenerationOwner, ensure_audio_generation_owner
        from synthesis_recovery_context import ensure_synthesis_batch_outputs
        import synthesis_workspace as workspace

        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = self.get_manager(root)
            owner = AudioGenerationOwner("a" * 32, lambda **kwargs: nullcontext())
            registered = root / workspace.DIRECTORY / ("0" * 32) / "assembled-parent.wav"
            registered.parent.mkdir(parents=True)
            registered.write_bytes(b"registered workspace output")
            legacy = root / "temp_batch_0.wav"
            legacy.write_bytes(b"legacy temp")
            calls = []
            original_remove = manager._remove_temp_file

            def recording_remove(path):
                calls.append(str(path))
                return original_remove(path)

            with ensure_audio_generation_owner(owner), \
                    ensure_synthesis_batch_outputs(str(root), {0: str(registered)}), \
                    patch.object(manager, "_remove_temp_file", side_effect=recording_remove):
                manager._remove_batch_temp_file(0)
            self.assertEqual([], calls)
            self.assertEqual(b"registered workspace output", registered.read_bytes())
            self.assertEqual(b"legacy temp", legacy.read_bytes())


if __name__ == "__main__":
    unittest.main()
