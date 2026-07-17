"""Phase 6 — router↔module integration for human evaluation review.

Builds a real evidence-valid production+candidate adapter (mirroring
test_lora_evaluation.py's fixture shape) and drives the actual comparison loader
used by the review endpoints, so the evidence-binding seam is exercised end to
end without a live server or GPU.
"""

import json
import os
import tempfile
import unittest

import evaluation_reviews as er
from lora_evidence import get_file_sha256
from routers import lora


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def _evaluation_json(checkpoint_dir, spec):
    """A version-2 evaluation result whose recorded hashes match files on disk."""
    _write(os.path.join(checkpoint_dir, "adapter_model.safetensors"),
           f"weights-{spec}".encode())
    _write(os.path.join(checkpoint_dir, "ref_sample.wav"), f"ref-{spec}".encode())
    _write(os.path.join(checkpoint_dir, "probe_0.wav"), f"probe-{spec}".encode())
    return {
        "version": 2,
        "probes": [{
            "id": "probe_0", "audio_file": "probe_0.wav",
            "audio_sha256": get_file_sha256(os.path.join(checkpoint_dir, "probe_0.wav")),
            "text": "the quick brown fox", "seed": 1, "metrics": {}, "warnings": [],
        }],
        "evidence": {
            "checkpoint_sha256": get_file_sha256(
                os.path.join(checkpoint_dir, "adapter_model.safetensors")),
            "reference_audio_sha256": get_file_sha256(
                os.path.join(checkpoint_dir, "ref_sample.wav")),
            "evaluation_spec_sha256": f"spec-{spec}",
        },
        "candidate_recommendation": {"reason": "candidate ranked higher", "ranking": []},
    }


def _build_evaluated_adapter(models_dir):
    adapter_dir = os.path.join(models_dir, "voice")
    candidate_dir = os.path.join(adapter_dir, "candidates", "cand1")
    os.makedirs(candidate_dir)
    with open(os.path.join(adapter_dir, "evaluation.json"), "w", encoding="utf-8") as fh:
        json.dump(_evaluation_json(adapter_dir, "prod"), fh)
    with open(os.path.join(candidate_dir, "evaluation.json"), "w", encoding="utf-8") as fh:
        json.dump(_evaluation_json(candidate_dir, "cand"), fh)
    manifest_path = os.path.join(models_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump([{
            "id": "voice",
            "evaluation": {"recommended_candidate": "cand1", "status": "pass"},
            "evaluation_candidates": [{"id": "cand1"}],
        }], fh)
    return adapter_dir, candidate_dir, manifest_path


class ReviewIntegrationTests(unittest.TestCase):
    def test_full_blind_review_flow_over_real_evidence(self):
        with tempfile.TemporaryDirectory() as models, tempfile.TemporaryDirectory() as reviews:
            _adapter, _candidate, manifest_path = _build_evaluated_adapter(models)
            manifest_before = open(manifest_path, encoding="utf-8").read()

            comparison, prod, cand = lora._load_candidate_comparison_full(
                "voice", models, manifest_path)
            fingerprint = er.evidence_fingerprint(prod, cand)
            audio_by_role = {
                "production": lora._role_audio_paths(prod, os.path.join(models, "voice")),
                "candidate": lora._role_audio_paths(
                    cand, os.path.join(models, "voice", "candidates", "cand1")),
            }

            session = er.create_session(reviews, "voice", comparison["candidate_id"],
                                        fingerprint, comparison["probe_pairs"], audio_by_role,
                                        automated_recommended=comparison["candidate_id"])
            # No identity — and no filesystem path — leaks into the client payload.
            self.assertNotIn("production", json.dumps(session))
            self.assertNotIn("candidate", json.dumps(session))
            self.assertNotIn(".wav", json.dumps(session))
            # But the server can still resolve each blind label to a real file.
            self.assertTrue(os.path.isfile(
                er.get_session_audio_path(reviews, session["session_id"], "A", "probe_0")))

            result = er.submit(reviews, "voice", session["session_id"], "A", fingerprint,
                               rating=4, notes="A was cleaner")
            self.assertIn(result["revealed"]["choice_role"], ("production", "candidate"))
            self.assertEqual("cand1", result["automated"]["recommended_candidate"])

            # Human feedback must not have touched the manifest/promotion state.
            self.assertEqual(manifest_before, open(manifest_path, encoding="utf-8").read())

    def test_mutated_checkpoint_rejects_a_stale_submission(self):
        with tempfile.TemporaryDirectory() as models, tempfile.TemporaryDirectory() as reviews:
            adapter_dir, _candidate, manifest_path = _build_evaluated_adapter(models)

            comparison, prod, cand = lora._load_candidate_comparison_full(
                "voice", models, manifest_path)
            opened_fingerprint = er.evidence_fingerprint(prod, cand)
            audio_by_role = {
                "production": lora._role_audio_paths(prod, os.path.join(models, "voice")),
                "candidate": lora._role_audio_paths(
                    cand, os.path.join(models, "voice", "candidates", "cand1")),
            }
            session = er.create_session(reviews, "voice", comparison["candidate_id"],
                                        opened_fingerprint, comparison["probe_pairs"],
                                        audio_by_role)

            # Retrain production: its checkpoint bytes change, so the recorded
            # evidence hash no longer matches. The comparison loader (used to
            # recompute the current fingerprint at submit time) now 409s.
            _write(os.path.join(adapter_dir, "adapter_model.safetensors"), b"RETRAINED")
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                lora._load_candidate_comparison_full("voice", models, manifest_path)

            # Even if it had loaded, the fresh fingerprint differs from the one
            # captured at session open, so the module rejects the submission and
            # writes no record.
            changed = dict(opened_fingerprint, production_checkpoint_sha256="mutated")
            with self.assertRaises(er.ReviewError):
                er.submit(reviews, "voice", session["session_id"], "A", changed)
            self.assertEqual([], er.list_reviews(reviews, "voice"))


class ReviewAudioProxyTests(unittest.TestCase):
    def test_proxy_streams_in_range_file_and_blocks_escapes(self):
        from unittest import mock
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as models, tempfile.TemporaryDirectory() as reviews:
            _build_evaluated_adapter(models)
            comparison, prod, cand = lora._load_candidate_comparison_full(
                "voice", models, manifest_path=os.path.join(models, "manifest.json"))
            audio_by_role = {
                "production": lora._role_audio_paths(prod, os.path.join(models, "voice")),
                "candidate": lora._role_audio_paths(
                    cand, os.path.join(models, "voice", "candidates", "cand1")),
            }
            with mock.patch.object(lora, "EVALUATION_REVIEWS_DIR", reviews), \
                 mock.patch.object(lora, "LORA_MODELS_DIR", models):
                sess = er.create_session(reviews, "voice", "cand1",
                                         er.evidence_fingerprint(prod, cand),
                                         comparison["probe_pairs"], audio_by_role)
                sid = sess["session_id"]
                # A valid label streams a file inside the models dir.
                response = lora._serve_review_audio("voice", sid, "A", "probe_0")
                self.assertTrue(lora.is_path_inside(response.path, models))
                # An unknown label / probe is a 404, not a leak.
                with self.assertRaises(HTTPException):
                    lora._serve_review_audio("voice", sid, "C", "probe_0")
                with self.assertRaises(HTTPException):
                    lora._serve_review_audio("voice", sid, "A", "nope")

            # A session whose stored path escapes the models dir is refused.
            outside = os.path.join(reviews, "secret.txt")
            _write(outside, b"secret")
            evil = er.create_session(
                reviews, "voice", "cand1", er.evidence_fingerprint(prod, cand),
                comparison["probe_pairs"],
                {"production": {"probe_0": outside}, "candidate": {"probe_0": outside}},
                blind=False)
            with mock.patch.object(lora, "EVALUATION_REVIEWS_DIR", reviews), \
                 mock.patch.object(lora, "LORA_MODELS_DIR", models):
                with self.assertRaises(HTTPException):
                    lora._serve_review_audio("voice", evil["session_id"], "A", "probe_0")


if __name__ == "__main__":
    unittest.main()
