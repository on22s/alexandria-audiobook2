"""Phase 6 — human evaluation-review + blind A/B session tests.

Exercises evaluation_reviews.py directly (pure module, no server) across the
plan's success criteria: blind labels hide identity, stale evidence is rejected,
history is bounded/append-only, and feedback structurally cannot promote.
"""

import ast
import inspect
import json
import os
import tempfile
import threading
import unittest

import evaluation_reviews as er

PROD = {"evidence": {"evaluation_spec_sha256": "ps", "checkpoint_sha256": "pc"}}
CAND = {"evidence": {"evaluation_spec_sha256": "cs", "checkpoint_sha256": "cc"}}
PAIRS = [{"id": "p1", "text": "hello", "seed": 1}]
AUDIO = {"production": {"p1": "/models/voice/p1.wav"},
         "candidate": {"p1": "/models/voice/candidates/c1/p1.wav"}}


def _fp():
    return er.evidence_fingerprint(PROD, CAND)


def _mk(d, **kwargs):
    return er.create_session(d, "voice_x", "cand1", _fp(), PAIRS, AUDIO, **kwargs)


def _session_labels(reviews_dir, session_id):
    with open(er._session_path(reviews_dir, session_id), encoding="utf-8") as fh:
        return json.load(fh)["labels"]


class BlindSessionTests(unittest.TestCase):
    def test_session_payload_hides_identity(self):
        with tempfile.TemporaryDirectory() as d:
            sess = _mk(d)
            blob = json.dumps(sess)
            # No role words, labels, fingerprint, or audio paths reach the client.
            self.assertNotIn("production", blob)
            self.assertNotIn("candidate", blob)
            self.assertNotIn("labels", blob)
            self.assertNotIn("fingerprint", blob)
            self.assertNotIn(".wav", blob)
            self.assertEqual({"id", "text"}, set(sess["pairs"][0].keys()))
            # The audio paths are stored server-side, reachable only via the getter.
            path_a = er.get_session_audio_path(d, sess["session_id"], "A", "p1")
            path_b = er.get_session_audio_path(d, sess["session_id"], "B", "p1")
            self.assertNotEqual(path_a, path_b)
            self.assertEqual({path_a, path_b},
                             {"/models/voice/p1.wav", "/models/voice/candidates/c1/p1.wav"})

    def test_blind_assignment_varies_across_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            roles_for_a = set()
            for _ in range(50):
                sess = _mk(d)
                roles_for_a.add(_session_labels(d, sess["session_id"])["A"])
            self.assertEqual({"production", "candidate"}, roles_for_a)

    def test_non_blind_session_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            sess = _mk(d, blind=False)
            self.assertEqual("production", _session_labels(d, sess["session_id"])["A"])


class SubmitTests(unittest.TestCase):
    def test_choice_resolves_to_the_hidden_role(self):
        with tempfile.TemporaryDirectory() as d:
            sess = _mk(d, automated_recommended="cand1")
            labels = _session_labels(d, sess["session_id"])
            res = er.submit(d, "voice_x", sess["session_id"], "A", _fp(),
                            rating=5, notes="clear winner")
            self.assertEqual(labels["A"], res["revealed"]["choice_role"])
            self.assertEqual("cand1", res["automated"]["recommended_candidate"])
            self.assertEqual(5, res["human"]["rating"])
            # Session consumed.
            self.assertFalse(os.path.exists(er._session_path(d, sess["session_id"])))

    def test_tie_is_supported(self):
        with tempfile.TemporaryDirectory() as d:
            sess = _mk(d)
            res = er.submit(d, "voice_x", sess["session_id"], "tie", _fp())
            self.assertEqual("tie", res["human"]["choice_role"])

    def test_stale_evidence_is_rejected_and_writes_no_record(self):
        with tempfile.TemporaryDirectory() as d:
            sess = _mk(d)
            changed = dict(_fp(), production_checkpoint_sha256="DIFFERENT")
            with self.assertRaises(er.ReviewError):
                er.submit(d, "voice_x", sess["session_id"], "A", changed)
            self.assertEqual([], er.list_reviews(d, "voice_x"))

    def test_unknown_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(er.ReviewError):
                er.submit(d, "voice_x", "review_deadbeef", "A", _fp())

    def test_wrong_adapter_for_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            sess = _mk(d)
            with self.assertRaises(er.ReviewError):
                er.submit(d, "voice_y", sess["session_id"], "A", _fp())

    def test_invalid_choice_and_rating_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            sess = _mk(d)
            with self.assertRaises(er.ReviewError):
                er.submit(d, "voice_x", sess["session_id"], "C", _fp())
            sess2 = _mk(d)
            with self.assertRaises(er.ReviewError):
                er.submit(d, "voice_x", sess2["session_id"], "A", _fp(), rating=9)

    def test_concurrent_double_submit_records_exactly_once(self):
        # A double-click (two submits for one session) must produce ONE record,
        # not two. The pending session is the atomic single-use claim.
        with tempfile.TemporaryDirectory() as d:
            sess = _mk(d)
            successes, errors = [], []
            barrier = threading.Barrier(8)

            def worker():
                barrier.wait()  # release all threads together to maximize overlap
                try:
                    er.submit(d, "voice_x", sess["session_id"], "A", _fp())
                    successes.append(1)
                except er.ReviewError:
                    errors.append(1)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(1, sum(successes))
            self.assertEqual(7, sum(errors))
            self.assertEqual(1, len(er.list_reviews(d, "voice_x")))
            # Session consumed exactly once.
            self.assertFalse(os.path.exists(er._session_path(d, sess["session_id"])))

    def test_notes_are_clamped_and_unicode_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            sess = _mk(d)
            note = "café 日本語 " + "z" * 5000
            res = er.submit(d, "voice_x", sess["session_id"], "A", _fp(), notes=note)
            self.assertLessEqual(len(res["human"]["notes"]), er.MAX_NOTE_CHARS)
            self.assertTrue(res["human"]["notes"].startswith("café 日本語"))


class HistoryTests(unittest.TestCase):
    def _submit_one(self, d, notes):
        sess = _mk(d)
        return er.submit(d, "voice_x", sess["session_id"], "A", _fp(), notes=notes)

    def test_history_is_bounded_keeping_newest(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(er.MAX_REVIEWS + 10):
                self._submit_one(d, f"note-{i}")
            reviews = er.list_reviews(d, "voice_x")
            self.assertEqual(er.MAX_REVIEWS, len(reviews))
            # newest first, and the oldest 10 were dropped
            self.assertEqual(f"note-{er.MAX_REVIEWS + 9}", reviews[0]["human"]["notes"])
            kept_notes = {r["human"]["notes"] for r in reviews}
            self.assertNotIn("note-0", kept_notes)

    def test_existing_records_are_not_mutated_by_new_submissions(self):
        with tempfile.TemporaryDirectory() as d:
            first = self._submit_one(d, "first")
            self._submit_one(d, "second")
            reviews = er.list_reviews(d, "voice_x")
            match = [r for r in reviews if r["human"]["notes"] == "first"]
            self.assertEqual(1, len(match))
            self.assertEqual(first["review_id"], match[0]["id"])

    def test_cleanup_reports_count_and_freed_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            self._submit_one(d, "a")
            self._submit_one(d, "b")
            result = er.cleanup(d, "voice_x")
            self.assertEqual(2, result["removed_count"])
            self.assertGreater(result["freed_bytes"], 0)
            self.assertEqual([], er.list_reviews(d, "voice_x"))

    def test_list_on_missing_store_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual([], er.list_reviews(d, "never_reviewed"))

    def test_summarize_tallies_preferences(self):
        with tempfile.TemporaryDirectory() as d:
            # Force known roles by using non-blind sessions (A == production).
            for choice, _role in (("A", "production"), ("B", "candidate"),
                                   ("B", "candidate"), ("tie", "tie")):
                sess = _mk(d, blind=False)
                er.submit(d, "voice_x", sess["session_id"], choice, _fp())
            summary = er.summarize(d, "voice_x")
            self.assertEqual(4, summary["count"])
            self.assertEqual(1, summary["preferred_production"])
            self.assertEqual(2, summary["preferred_candidate"])
            self.assertEqual(1, summary["tie"])
            self.assertIsNotNone(summary["latest_at"])

    def test_summarize_empty_is_zeroed(self):
        with tempfile.TemporaryDirectory() as d:
            summary = er.summarize(d, "never_reviewed")
            self.assertEqual(0, summary["count"])
            self.assertIsNone(summary["latest_at"])


class SafetyTests(unittest.TestCase):
    def test_module_cannot_promote_or_touch_manifest(self):
        # Structural guarantee (not a substring check — the docstring mentions
        # "promote"): the module imports no promotion code and calls nothing
        # named like promote. Human feedback therefore cannot promote a checkpoint.
        tree = ast.parse(inspect.getsource(er))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        self.assertNotIn("core", imported)
        self.assertFalse(any("lora" in module for module in imported))
        self.assertFalse(any("promot" in module.lower() for module in imported))
        called = [node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        self.assertFalse(any("promot" in name.lower() for name in called))

    def test_prune_removes_only_aged_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            fresh = _mk(d)
            # A session pruned with a zero-age budget should be removed.
            er.prune_sessions(d, max_age_seconds=0)
            self.assertFalse(os.path.exists(er._session_path(d, fresh["session_id"])))


if __name__ == "__main__":
    unittest.main()


class SessionTimezonePruningTests(unittest.TestCase):
    def test_timezone_free_records_are_invalid_without_blocking_aware_sessions(self):
        import datetime
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(er._sessions_dir(tmp))
            directory.mkdir()
            now = datetime.datetime.now(datetime.timezone.utc)
            timestamps = {
                "naive-old.json": "2025-01-01T00:00:00",
                "naive-recent.json": now.replace(tzinfo=None).isoformat(),
                "aware-old.json": (now - datetime.timedelta(hours=8)).isoformat(),
                "aware-offset-old.json": (now - datetime.timedelta(hours=8)).astimezone(
                    datetime.timezone(datetime.timedelta(hours=5))).isoformat(),
                "aware-recent.json": now.isoformat(),
                "invalid.json": "not-a-timestamp",
                "null.json": None,
            }
            for name, timestamp in timestamps.items():
                (directory / name).write_text(json.dumps({"created_at": timestamp}))
            recent = (directory / "aware-recent.json").read_bytes()
            self.assertEqual(set(timestamps) - {"aware-recent.json"}, set(er.prune_sessions(tmp)))
            self.assertEqual(recent, (directory / "aware-recent.json").read_bytes())
            self.assertEqual(sorted(["aware-recent.json"] + [name + ".lock" for name in timestamps]),
                             sorted(p.name for p in directory.iterdir()))
            created = _mk(tmp, blind=False)
            self.assertEqual("/models/voice/p1.wav",
                             er.get_session_audio_path(tmp, created["session_id"], "A", "p1"))
            self.assertEqual(recent, (directory / "aware-recent.json").read_bytes())


class ReviewStoreShapeTests(unittest.TestCase):
    def test_nonobject_store_listing_and_summary_are_empty_without_mutation(self):
        from pathlib import Path
        for payload in ([{'unknown': 'data'}], 'bad', 7, True, [], None):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = Path(er._store_path(directory, 'voice_x'))
                content = json.dumps(payload).encode()
                path.write_bytes(content)
                self.assertEqual([], er.list_reviews(directory, 'voice_x'))
                self.assertEqual({'count': 0, 'preferred_production': 0,
                                  'preferred_candidate': 0, 'tie': 0, 'latest_at': None},
                                 er.summarize(directory, 'voice_x'))
                self.assertEqual(content, path.read_bytes())

    def test_submission_to_nonobject_store_records_one_evidence_bound_review(self):
        from pathlib import Path
        for payload in ([{'unknown': 'data'}], 'bad', 7, True, [], None):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                session = _mk(directory, blind=False)
                path = Path(er._store_path(directory, 'voice_x'))
                path.write_text(json.dumps(payload), encoding='utf-8')
                result = er.submit(directory, 'voice_x', session['session_id'], 'A', _fp(), notes='known vote')
                reviews = er.list_reviews(directory, 'voice_x')
                self.assertEqual(1, len(reviews))
                self.assertEqual(result['review_id'], reviews[0]['id'])
                self.assertEqual(_fp(), reviews[0]['evidence'])
                self.assertEqual('production', reviews[0]['human']['choice_role'])
                self.assertEqual('known vote', reviews[0]['human']['notes'])
                self.assertFalse(Path(er._session_path(directory, session['session_id'])).exists())
                self.assertEqual(1, er.summarize(directory, 'voice_x')['preferred_production'])

    def test_cleanup_already_handles_nonobject_store(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(er._store_path(directory, 'voice_x'))
            content = b'["unusable"]'
            path.write_bytes(content)
            self.assertEqual({'removed_count': 0, 'freed_bytes': len(content)},
                             er.cleanup(directory, 'voice_x'))
            self.assertFalse(path.exists())


class ReviewCleanupSerializationTests(unittest.TestCase):
    def test_cleanup_waits_for_real_submission_then_removes_complete_history(self):
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            first = _mk(tmp)
            er.submit(tmp, "voice_x", first["session_id"], "tie", _fp())
            second = _mk(tmp)
            store = Path(er._store_path(tmp, "voice_x"))
            before = store.read_bytes()
            publishing, release, cleanup_started, cleanup_done = (threading.Event() for _ in range(4))
            results, errors = [], []
            original_write = er.atomic_json_write
            def paused_write(document, path):
                if Path(path) == store:
                    publishing.set()
                    if not release.wait(5):
                        raise RuntimeError("test did not release store publication")
                return original_write(document, path)
            def submit():
                try:
                    er.submit(tmp, "voice_x", second["session_id"], "tie", _fp())
                except Exception as error:
                    errors.append(error)
            def clean():
                cleanup_started.set()
                try:
                    results.append(er.cleanup(tmp, "voice_x"))
                except Exception as error:
                    errors.append(error)
                finally:
                    cleanup_done.set()
            with patch.object(er, "atomic_json_write", side_effect=paused_write):
                writer = threading.Thread(target=submit)
                remover = threading.Thread(target=clean)
                writer.start()
                try:
                    self.assertTrue(publishing.wait(3))
                    remover.start()
                    self.assertTrue(cleanup_started.wait(3))
                    cleanup_finished_before_release = cleanup_done.wait(.25)
                    visible_before_release = store.read_bytes() if store.exists() else None
                finally:
                    release.set()
                    writer.join(3)
                    if remover.ident is not None:
                        remover.join(3)
                self.assertFalse(writer.is_alive())
                self.assertFalse(remover.is_alive())
            self.assertEqual([], errors)
            self.assertFalse(store.exists(), "submission resurrected a supposedly deleted history")
            self.assertEqual(2, results[0]["removed_count"])
            self.assertGreater(results[0]["freed_bytes"], len(before))
            self.assertFalse(cleanup_finished_before_release)
            self.assertEqual(before, visible_before_release)
            self.assertFalse(Path(er._session_path(tmp, second["session_id"])).exists())

    def test_cleanup_of_absent_directory_has_no_filesystem_side_effect(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "not-created"
            self.assertEqual({"removed_count": 0, "freed_bytes": 0}, er.cleanup(str(absent), "voice_x"))
            self.assertFalse(absent.exists())


class ReviewConsumptionFailureTests(unittest.TestCase):
    def test_actual_http_consumption_errors_leave_session_and_history_retryable(self):
        from pathlib import Path
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import lora
        for error in (PermissionError("read-only sessions"), OSError(5, "session I/O error")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                first = _mk(tmp)
                er.submit(tmp, "voice_x", first["session_id"], "tie", _fp())
                pending = _mk(tmp)
                session = Path(er._session_path(tmp, pending["session_id"]))
                store = Path(er._store_path(tmp, "voice_x"))
                session_before, store_before = session.read_bytes(), store.read_bytes()
                original_unlink = er.os.unlink
                def fail_consumption(path, *args, **kwargs):
                    if Path(path) == session:
                        raise error
                    return original_unlink(path, *args, **kwargs)
                app = FastAPI()
                app.include_router(lora.router)
                endpoint = f"/api/lora/models/voice_x/review/session/{pending['session_id']}"
                with patch.object(lora, "EVALUATION_REVIEWS_DIR", tmp), \
                     patch.object(lora, "_current_evidence_fingerprint", return_value=_fp()), \
                     TestClient(app) as client:
                    with patch.object(er.os, "unlink", side_effect=fail_consumption):
                        response = client.post(endpoint, json={"choice": "tie", "rating": 4})
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertIn("consume review session", response.json()["detail"])
                    self.assertEqual(session_before, session.read_bytes())
                    self.assertEqual(store_before, store.read_bytes())
                    retry = client.post(endpoint, json={"choice": "tie", "rating": 4})
                    self.assertEqual(200, retry.status_code, retry.text)
                    self.assertFalse(session.exists())
                    reviews = er.list_reviews(tmp, "voice_x")
                    self.assertEqual(2, len(reviews))
                    self.assertEqual(1, sum(review["human"]["rating"] == 4 for review in reviews))
                    committed = store.read_bytes()
                    duplicate = client.post(endpoint, json={"choice": "tie", "rating": 4})
                    self.assertEqual(409, duplicate.status_code)
                    self.assertEqual(committed, store.read_bytes())
