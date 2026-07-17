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
