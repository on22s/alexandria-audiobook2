"""Real persistence/restart boundaries for review journals; no model inference."""

import copy
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

import evaluation_reviews as er
from tests.test_evaluation_reviews import _fp, _mk


class ReviewDecisionRecoveryTests(unittest.TestCase):
    def _interrupt(self, directory, boundary):
        session = _mk(directory, blind=False, build={"commit": "fixture"})
        path = Path(er._session_path(directory, session["session_id"]))
        original = path.read_bytes()
        code = r"""
import os, sys
import evaluation_reviews as er
from tests.test_evaluation_reviews import _fp
root, sid, boundary = sys.argv[1:]
write, unlink = er.atomic_json_write, er.os.unlink
journal = er._decision_path(root, sid)
store = er._store_path(root, "voice_x")
session = er._session_path(root, sid)
def interrupted_write(document, path):
    write(document, path)
    if (boundary == "prepared" and path == journal) or (boundary == "published" and path == store):
        os._exit(93)
def interrupted_unlink(path, *args, **kwargs):
    unlink(path, *args, **kwargs)
    if (boundary == "consumed" and path == session) or (boundary == "removed" and path == journal):
        os._exit(93)
er.atomic_json_write = interrupted_write
er.os.unlink = interrupted_unlink
er.submit(root, "voice_x", sid, "B", _fp(), rating=4, notes="café interrupted decision")
raise RuntimeError("fixture did not hit its interruption boundary")
"""
        child = subprocess.run([sys.executable, "-c", code, directory, session["session_id"], boundary],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(93, child.returncode, child.stdout + child.stderr)
        # The child is confirmed dead. Age only its disposable lock markers so
        # production's existing 120-second abandoned-lock policy applies unchanged.
        for target in (path, Path(er._store_path(directory, "voice_x"))):
            marker = Path(str(target) + ".lock")
            self.assertTrue(marker.exists())
            os.utime(marker, (0, 0))
        return path, original, Path(er._decision_path(directory, session["session_id"]))

    def test_restart_at_each_boundary_replays_only_consumed_decisions_once(self):
        for boundary in ("prepared", "consumed", "published", "removed"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                session, original, journal = self._interrupt(directory, boundary)
                store = Path(er._store_path(directory, "voice_x"))
                expected = json.loads(journal.read_text())["record"] if journal.exists() else json.loads(store.read_text())["reviews"][0]
                if boundary == "prepared":
                    self.assertEqual(original, session.read_bytes())
                    self.assertFalse(store.exists())
                    self.assertEqual([], er.list_reviews(directory, "voice_x"))
                    self.assertFalse(journal.exists())
                    self.assertEqual(original, session.read_bytes())
                    retried = er.submit(directory, "voice_x", session.stem, "A", _fp(), notes="actual retry")
                    self.assertEqual("production", retried["human"]["choice_role"])
                    self.assertEqual("actual retry", er.list_reviews(directory, "voice_x")[0]["human"]["notes"])
                else:
                    self.assertFalse(session.exists())
                    self.assertEqual([expected], er.list_reviews(directory, "voice_x"))
                    self.assertFalse(journal.exists())
                    committed = store.read_bytes()
                    self.assertEqual([expected], er.list_reviews(directory, "voice_x"))
                    self.assertEqual(committed, store.read_bytes())
                    with self.assertRaises(er.ReviewError):
                        er.submit(directory, "voice_x", session.stem, "A", _fp())
                    self.assertEqual(committed, store.read_bytes())

    def test_cleanup_recovers_pending_decision_then_cannot_resurrect_history(self):
        for boundary in ("consumed", "published"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                session, _, journal = self._interrupt(directory, boundary)
                result = er.cleanup(directory, "voice_x")
                self.assertEqual(1, result["removed_count"])
                self.assertGreater(result["freed_bytes"], 0)
                self.assertFalse(journal.exists())
                self.assertFalse(session.exists())
                self.assertFalse(Path(er._store_path(directory, "voice_x")).exists())
                self.assertEqual([], er.list_reviews(directory, "voice_x"))
                self.assertEqual(0, er.summarize(directory, "voice_x")["count"])

    def test_submit_recovers_prior_consumed_decision_before_rejecting_unknown_session(self):
        with tempfile.TemporaryDirectory() as directory:
            session, _, journal = self._interrupt(directory, "consumed")
            expected = json.loads(journal.read_text())["record"]
            with self.assertRaisesRegex(er.ReviewError, "unknown or has expired"):
                er.submit(directory, "voice_x", session.stem, "A", _fp())
            self.assertFalse(journal.exists())
            self.assertEqual([expected], json.loads(Path(er._store_path(directory, "voice_x")).read_text())["reviews"])

    def test_journal_preparation_failure_keeps_exact_session_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            session = _mk(directory)
            path = Path(er._session_path(directory, session["session_id"]))
            original = path.read_bytes()
            with patch.object(er, "atomic_json_write", side_effect=OSError(5, "prepare error")):
                with self.assertRaisesRegex(er.ReviewError, "prepare review decision"):
                    er.submit(directory, "voice_x", session["session_id"], "A", _fp())
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([], er.list_reviews(directory, "voice_x"))
            er.submit(directory, "voice_x", session["session_id"], "tie", _fp())
            self.assertEqual(1, len(er.list_reviews(directory, "voice_x")))

    def test_store_publication_failure_preserves_exact_decision_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            session = _mk(directory, blind=False)
            store = Path(er._store_path(directory, "voice_x"))
            write = er.atomic_json_write
            def fail_store(document, path):
                if Path(path) == store:
                    raise OSError(5, "publication error")
                return write(document, path)
            with patch.object(er, "atomic_json_write", side_effect=fail_store):
                with self.assertRaisesRegex(er.ReviewError, "pending recovery"):
                    er.submit(directory, "voice_x", session["session_id"], "B", _fp(), notes="saved vote")
                journal = Path(er._decision_path(directory, session["session_id"]))
                before = journal.read_bytes()
                expected = json.loads(before)["record"]
                self.assertFalse(Path(er._session_path(directory, session["session_id"])).exists())
                self.assertFalse(store.exists())
                with self.assertRaisesRegex(er.ReviewError, "publish pending"):
                    er.list_reviews(directory, "voice_x")
                self.assertEqual(before, journal.read_bytes())
                self.assertFalse(store.exists())
            self.assertEqual(1, er.summarize(directory, "voice_x")["preferred_candidate"])
            self.assertEqual([expected], er.list_reviews(directory, "voice_x"))
            self.assertFalse(journal.exists())

    def test_journal_removal_failure_blocks_trimming_and_cleanup_until_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            session = _mk(directory)
            journal = Path(er._decision_path(directory, session["session_id"]))
            unlink = er.os.unlink
            def fail_journal(path, *args, **kwargs):
                if Path(path) == journal:
                    raise PermissionError("journal removal error")
                return unlink(path, *args, **kwargs)
            with patch.object(er.os, "unlink", side_effect=fail_journal):
                with self.assertRaisesRegex(er.ReviewError, "finish pending"):
                    er.submit(directory, "voice_x", session["session_id"], "tie", _fp())
                store = Path(er._store_path(directory, "voice_x"))
                committed, pending = store.read_bytes(), journal.read_bytes()
                next_session = _mk(directory)
                for operation in (lambda: er.list_reviews(directory, "voice_x"),
                                  lambda: er.cleanup(directory, "voice_x"),
                                  lambda: er.submit(directory, "voice_x", next_session["session_id"], "A", _fp())):
                    with self.assertRaisesRegex(er.ReviewError, "finish pending"):
                        operation()
                    self.assertEqual(committed, store.read_bytes())
                    self.assertEqual(pending, journal.read_bytes())
                self.assertTrue(Path(er._session_path(directory, next_session["session_id"])).exists())
            expected = json.loads(pending)["record"]
            self.assertEqual([expected], er.list_reviews(directory, "voice_x"))
            self.assertFalse(journal.exists())
            self.assertEqual(1, er.cleanup(directory, "voice_x")["removed_count"])
            self.assertEqual([], er.list_reviews(directory, "voice_x"))

    def test_invalid_or_unreadable_journal_never_fabricates_history(self):
        for damage in ("syntax", "shape", "identity", "human", "timestamp", "evidence", "unreadable"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                _, _, journal = self._interrupt(directory, "consumed")
                document = json.loads(journal.read_text())
                if damage == "syntax":
                    journal.write_text("{broken")
                elif damage == "shape":
                    journal.write_text("null")
                elif damage != "unreadable":
                    if damage == "identity":
                        document["session"]["session_id"] = "different-session"
                    elif damage == "human":
                        del document["record"]["human"]
                    elif damage == "timestamp":
                        document["record"]["created_at"] = "broken"
                    elif damage == "evidence":
                        document["record"]["evidence"] = {}
                    journal.write_text(json.dumps(document))
                before = journal.read_bytes()
                real_open = open
                def unreadable(path, *args, **kwargs):
                    if Path(path) == journal:
                        raise PermissionError("unreadable journal")
                    return real_open(path, *args, **kwargs)
                with patch("builtins.open", side_effect=unreadable) if damage == "unreadable" else patch.object(er, "MAX_REVIEWS", er.MAX_REVIEWS):
                    with self.assertRaises(er.ReviewError):
                        er.list_reviews(directory, "voice_x")
                self.assertEqual(before, journal.read_bytes())
                self.assertFalse(Path(er._store_path(directory, "voice_x")).exists())

    def test_changed_or_null_original_session_is_never_treated_as_consumed(self):
        for replacement in (None, {"unexpected": "replacement"}):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as directory:
                session, _, journal = self._interrupt(directory, "prepared")
                session.write_text(json.dumps(replacement))
                before, pending = session.read_bytes(), journal.read_bytes()
                with self.assertRaisesRegex(er.ReviewError, "session identity changed"):
                    er.list_reviews(directory, "voice_x")
                self.assertEqual(before, session.read_bytes())
                self.assertEqual(pending, journal.read_bytes())
                self.assertFalse(Path(er._store_path(directory, "voice_x")).exists())

    def test_existing_id_with_different_record_fails_instead_of_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, journal = self._interrupt(directory, "published")
            store = Path(er._store_path(directory, "voice_x"))
            document = json.loads(store.read_text())
            document["reviews"][0]["human"]["notes"] = "conflicting record"
            store.write_text(json.dumps(document))
            original, pending = store.read_bytes(), journal.read_bytes()
            with self.assertRaisesRegex(er.ReviewError, "conflicts with recorded history"):
                er.list_reviews(directory, "voice_x")
            self.assertEqual(original, store.read_bytes())
            self.assertEqual(pending, journal.read_bytes())

    def test_submit_preserves_input_objects_and_recovery_keeps_existing_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            for index in range(er.MAX_REVIEWS):
                session = _mk(directory)
                er.submit(directory, "voice_x", session["session_id"], "tie", _fp(), notes=str(index))
            _, _, journal = self._interrupt(directory, "consumed")
            record = json.loads(journal.read_text())["record"]
            history = er.list_reviews(directory, "voice_x")
            self.assertEqual(er.MAX_REVIEWS, len(history))
            self.assertEqual(record, history[0])
            self.assertNotIn("0", [item["human"]["notes"] for item in history])
            fingerprint, build = _fp(), {"commit": "input"}
            pairs = [{"id": "p", "text": "hello"}]
            audio = {"production": {"p": "/a.wav"}, "candidate": {"p": "/b.wav"}}
            before = copy.deepcopy((fingerprint, build, pairs, audio))
            session = er.create_session(directory, "voice_x", "cand1", fingerprint, pairs, audio, build=build)
            er.submit(directory, "voice_x", session["session_id"], "A", fingerprint)
            self.assertEqual(before, (fingerprint, build, pairs, audio))


    def test_directory_sync_failure_preserves_recoverable_boundary_artifacts(self):
        for boundary in ("prepared", "consumed", "published"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                session = _mk(directory, blind=False)
                session_path = Path(er._session_path(directory, session["session_id"]))
                journal = Path(er._decision_path(directory, session["session_id"]))
                store = Path(er._store_path(directory, "voice_x"))
                original = session_path.read_bytes()
                fail_path = {"prepared": journal, "consumed": session_path, "published": store}[boundary]
                sync = er._sync_directory
                def fail_sync(path):
                    if Path(path) == fail_path:
                        raise OSError(5, "directory sync error")
                    return sync(path)
                with patch.object(er, "_sync_directory", side_effect=fail_sync):
                    with self.assertRaises(er.ReviewError):
                        er.submit(directory, "voice_x", session["session_id"], "B", _fp())
                expected = json.loads(journal.read_text())["record"]
                self.assertEqual(boundary == "prepared", session_path.exists())
                self.assertEqual(boundary == "published", store.exists())
                if boundary == "prepared":
                    self.assertEqual(original, session_path.read_bytes())
                    self.assertEqual([], er.list_reviews(directory, "voice_x"))
                    self.assertEqual(original, session_path.read_bytes())
                else:
                    committed = store.read_bytes() if store.exists() else None
                    # A recovered ID already on disk still needs its directory sync
                    # before the final recovery marker may be removed.
                    if boundary == "published":
                        before = journal.read_bytes()
                        with patch.object(er, "_sync_directory", side_effect=fail_sync):
                            with self.assertRaisesRegex(er.ReviewError, "publish pending"):
                                er.list_reviews(directory, "voice_x")
                        self.assertEqual(before, journal.read_bytes())
                        self.assertEqual(committed, store.read_bytes())
                    self.assertEqual([expected], er.list_reviews(directory, "voice_x"))
                    if committed is not None:
                        self.assertEqual(committed, store.read_bytes())
                self.assertFalse(journal.exists())

    def test_corrupted_session_id_is_refused_before_preparation_or_consumption(self):
        with tempfile.TemporaryDirectory() as directory:
            session = _mk(directory)
            path = Path(er._session_path(directory, session["session_id"]))
            document = json.loads(path.read_text())
            document["session_id"] = "different-session"
            path.write_text(json.dumps(document))
            original = path.read_bytes()
            with self.assertRaisesRegex(er.ReviewError, "session identity is invalid"):
                er.submit(directory, "voice_x", session["session_id"], "A", _fp())
            self.assertEqual(original, path.read_bytes())
            self.assertFalse(Path(er._decision_path(directory, session["session_id"])).exists())
            self.assertEqual([], er.list_reviews(directory, "voice_x"))

    def test_other_adapter_operations_do_not_consume_or_replay_this_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, journal = self._interrupt(directory, "consumed")
            pending = journal.read_bytes()
            self.assertEqual([], er.list_reviews(directory, "voice_y"))
            self.assertEqual(0, er.cleanup(directory, "voice_y")["removed_count"])
            self.assertEqual(pending, journal.read_bytes())
            self.assertFalse(Path(er._store_path(directory, "voice_x")).exists())
            self.assertEqual(1, len(er.list_reviews(directory, "voice_x")))


    def test_recovery_requires_consumption_directory_sync_before_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            session, _, journal = self._interrupt(directory, "consumed")
            pending = journal.read_bytes()
            sync = er._sync_directory
            def fail_sync(path):
                if Path(path) == session:
                    raise OSError(5, "consumption sync error")
                return sync(path)
            with patch.object(er, "_sync_directory", side_effect=fail_sync):
                with self.assertRaisesRegex(er.ReviewError, "publish pending"):
                    er.list_reviews(directory, "voice_x")
            self.assertEqual(pending, journal.read_bytes())
            self.assertFalse(Path(er._store_path(directory, "voice_x")).exists())
            self.assertEqual([json.loads(pending)["record"]], er.list_reviews(directory, "voice_x"))

    def test_failed_journal_deletion_sync_restores_replay_barrier(self):
        with tempfile.TemporaryDirectory() as directory:
            opened = _mk(directory)
            journal = Path(er._decision_path(directory, opened["session_id"]))
            sync = er._sync_directory
            def fail_deletion_sync(path):
                if Path(path) == journal and not journal.exists():
                    raise OSError(5, "deletion sync error")
                return sync(path)
            with patch.object(er, "_sync_directory", side_effect=fail_deletion_sync):
                with self.assertRaisesRegex(er.ReviewError, "finish pending"):
                    er.submit(directory, "voice_x", opened["session_id"], "tie", _fp())
                store = Path(er._store_path(directory, "voice_x"))
                committed = store.read_bytes()
                pending = journal.read_bytes()
                for operation in (lambda: er.list_reviews(directory, "voice_x"),
                                  lambda: er.cleanup(directory, "voice_x")):
                    with self.assertRaisesRegex(er.ReviewError, "finish pending"):
                        operation()
                    self.assertEqual(committed, store.read_bytes())
                    self.assertEqual(pending, journal.read_bytes())
            self.assertEqual([json.loads(pending)["record"]], er.list_reviews(directory, "voice_x"))
            self.assertFalse(journal.exists())
            self.assertEqual(1, er.cleanup(directory, "voice_x")["removed_count"])
            self.assertEqual([], er.list_reviews(directory, "voice_x"))


class ReviewSessionAgeTests(unittest.TestCase):
    def _clock(self, now):
        real_datetime = datetime.datetime
        class FixedNow(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return now if tz is not None else now.replace(tzinfo=None)
        return patch.object(er.datetime, "datetime", FixedNow)

    def test_http_access_refuses_expired_and_invalid_timestamps_without_pruning(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import lora
        now = datetime.datetime.now(datetime.timezone.utc)
        cases = (("fresh", now.isoformat(), True),
                 ("boundary", (now - datetime.timedelta(hours=6)).isoformat(), True),
                 ("old", (now - datetime.timedelta(hours=7)).isoformat(), False),
                 ("just-expired", (now - datetime.timedelta(hours=6, microseconds=1)).isoformat(), False),
                 ("naive", now.replace(tzinfo=None).isoformat(), False),
                 ("invalid", "not-a-time", False), ("null", None, False), ("missing", None, False))
        for name, timestamp, eligible in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                model_dir = Path(directory) / "models"
                model_dir.mkdir()
                audio = model_dir / "probe.wav"
                with wave.open(str(audio), "wb") as handle:
                    handle.setnchannels(1)
                    handle.setsampwidth(2)
                    handle.setframerate(24000)
                    handle.writeframes(b"\x00\x00" * 24)
                reviews = str(Path(directory) / "reviews")
                opened = er.create_session(reviews, "voice_x", "cand1", _fp(),
                    [{"id": "p1", "text": "probe"}],
                    {role: {"p1": str(audio)} for role in ("production", "candidate")}, blind=False)
                session = Path(er._session_path(reviews, opened["session_id"]))
                document = json.loads(session.read_text())
                if name == "missing":
                    del document["created_at"]
                else:
                    document["created_at"] = timestamp
                session.write_text(json.dumps(document))
                before = session.read_bytes()
                app = FastAPI()
                app.include_router(lora.router)
                base = f"/api/lora/models/voice_x/review/session/{opened['session_id']}"
                with self._clock(now), patch.object(lora, "EVALUATION_REVIEWS_DIR", reviews), \
                     patch.object(lora, "LORA_MODELS_DIR", str(model_dir)), \
                     patch.object(lora, "_current_evidence_fingerprint", return_value=_fp()), TestClient(app) as client:
                    playback = client.get(base + "/audio/A/p1")
                    response = client.post(base, json={"choice": "A"})
                self.assertEqual(200 if eligible else 404, playback.status_code, playback.text)
                self.assertEqual(200 if eligible else 409, response.status_code, response.text)
                if eligible:
                    self.assertEqual(audio.read_bytes(), playback.content)
                    self.assertFalse(session.exists())
                    self.assertEqual(1, len(er.list_reviews(reviews, "voice_x")))
                else:
                    self.assertIn("expired", response.json()["detail"])
                    self.assertEqual(before, session.read_bytes())
                    self.assertEqual([], er.list_reviews(reviews, "voice_x"))

    def test_prune_aborts_unconsumed_preparation_before_expiry_deletion(self):
        helper = ReviewDecisionRecoveryTests()
        with tempfile.TemporaryDirectory() as directory:
            session, _, journal = helper._interrupt(directory, "prepared")
            future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)
            with self._clock(future):
                self.assertEqual([session.name], er.prune_sessions(directory))
            self.assertFalse(session.exists())
            self.assertFalse(journal.exists())
            self.assertEqual([], er.list_reviews(directory, "voice_x"))

    def test_consumed_decision_recovers_after_age_limit_without_reopening_session(self):
        helper = ReviewDecisionRecoveryTests()
        with tempfile.TemporaryDirectory() as directory:
            session, _, journal = helper._interrupt(directory, "consumed")
            expected = json.loads(journal.read_text())["record"]
            future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)
            with self._clock(future):
                self.assertEqual([expected], er.list_reviews(directory, "voice_x"))
                with self.assertRaises(er.ReviewError):
                    er.get_session_audio_path(directory, session.stem, "A", "p1")
                with self.assertRaises(er.ReviewError):
                    er.submit(directory, "voice_x", session.stem, "A", _fp())
            self.assertEqual([expected], er.list_reviews(directory, "voice_x"))
            self.assertFalse(session.exists())
            self.assertFalse(journal.exists())

    def test_prune_cannot_delete_session_when_preparation_abort_fails(self):
        helper = ReviewDecisionRecoveryTests()
        with tempfile.TemporaryDirectory() as directory:
            session, original, journal = helper._interrupt(directory, "prepared")
            pending = journal.read_bytes()
            future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)
            unlink = er.os.unlink
            def fail_abort(path, *args, **kwargs):
                if Path(path) == journal:
                    raise PermissionError("cannot abort preparation")
                return unlink(path, *args, **kwargs)
            with self._clock(future), patch.object(er.os, "unlink", side_effect=fail_abort):
                with self.assertRaisesRegex(er.ReviewError, "finish pending"):
                    er.prune_sessions(directory)
            self.assertEqual(original, session.read_bytes())
            self.assertEqual(pending, journal.read_bytes())
            with self._clock(future):
                self.assertEqual([session.name], er.prune_sessions(directory))
            self.assertEqual([], er.list_reviews(directory, "voice_x"))
            self.assertFalse(journal.exists())
