"""Failed-request recovery (issue #522 s23 / s4.3 / s4.4): the panel's detail
route, manual segmentation injection through pass 1's own gate, and the
nothing-lost skip."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers import script as script_module
from three_pass_generate import three_pass_checkpoint_path, three_pass_manifest_path
from utils import atomic_json_write

SOURCE = ('"Where is he?" asked Holmes. "Gone," said I. Sherlock Holmes frowned and '
          'looked at the door for a long moment before he spoke again.')


def _write_failed_attribute_run(tmp):
    script_path = os.path.join(tmp, "annotated_script.json")
    atomic_json_write({"fingerprint": "fp", "status": "failed", "failed_pass": "attribute",
                       "chunks": [], "counts": {}, "passes": {}, "diagnostic_failures": []},
                      three_pass_manifest_path(script_path))
    entries = [{"type": "SPOKEN", "text": "Where is he?"}, {"type": "NARRATOR", "text": "asked Holmes."},
               {"type": "SPOKEN", "text": "Gone,"}]
    atomic_json_write({
        "fingerprint": "fp", "stage": "attribute_failed", "chunks_done": 1,
        "segmented": [{"type": "NARRATOR", "text": "Chapter one."}] + entries,
        "named": [{"text": "Chapter one.", "speaker": "NARRATOR"}, None, None, None],
        "annotated": [], "resolutions": ["clean"], "elapsed_s": {}, "diagnostic_failures": [],
        "failed": {"pass": "attribute", "indices": [1, 2, 3], "entries": entries,
                   "roster": ["HOLMES", "WATSON"], "reason": "attribute LLM unavailable; refusing fallback output",
                   "attempts": [{"attempt": 1, "outcome": "api_error", "error_category": "connection_error",
                                 "http_status": None, "error": "Connection error."}]}},
        three_pass_checkpoint_path(script_path))
    atomic_json_write({"input_file_path": "book.txt", "script_generation_input_file": "book.txt"},
                      os.path.join(tmp, "state.json"))
    return script_path


def _write_failed_run(tmp):
    script_path = os.path.join(tmp, "annotated_script.json")
    atomic_json_write({"fingerprint": "fp", "status": "failed", "failed_pass": "segment",
                       "failed_chunk": 2, "chunks": [], "counts": {}, "passes": {},
                       "diagnostic_failures": []}, three_pass_manifest_path(script_path))
    atomic_json_write({
        "fingerprint": "fp", "stage": "segment_failed", "chunks_done": 1,
        "segmented": [{"type": "NARRATOR", "text": "Chapter one."}],
        "named": [], "annotated": [], "resolutions": ["clean", "fail"],
        "elapsed_s": {}, "diagnostic_failures": [],
        "failed": {"pass": "segment", "chunk": 2, "chunks_total": 3, "source": SOURCE,
                   "failure_codes": ["low_source_token_recall"],
                   "attempts": [{"attempt": 1, "outcome": "api_error", "error_category": "rate_limited",
                                 "http_status": 429, "error": "RateLimitError: slow down",
                                 "next_retry_seconds": 1.0},
                                {"attempt": 2, "outcome": "response_rejected",
                                 "failure_codes": ["low_source_token_recall"],
                                 "finish_reason": "stop", "completion_tokens": 40}]}},
        three_pass_checkpoint_path(script_path))
    atomic_json_write({"input_file_path": "book.txt", "script_generation_input_file": "book.txt"},
                      os.path.join(tmp, "state.json"))
    return script_path


def _write_relaxed_quote_run(tmp):
    script_path = _write_failed_run(tmp)
    checkpoint_path = three_pass_checkpoint_path(script_path)
    with open(checkpoint_path, encoding="utf-8") as handle:
        checkpoint = json.load(handle)
    checkpoint["failed"]["quoted_must_be_spoken"] = False
    checkpoint["failed"]["unquoted_must_be_narrator"] = True
    atomic_json_write(checkpoint, checkpoint_path)
    return script_path


class RecoveryTests(unittest.TestCase):
    def _run(self, fn, *args, writer=_write_failed_run):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = writer(tmp)
            with patch.object(script_module, "SCRIPT_PATH", script_path), \
                 patch.object(script_module, "DATA_DIR", tmp), \
                 patch.object(script_module, "CONFIG_PATH", os.path.join(tmp, "config.json")):
                script_module.process_state["script"]["running"] = False
                try:
                    result = asyncio.run(fn(*args))
                except HTTPException as exc:
                    with open(three_pass_checkpoint_path(script_path)) as handle:
                        return exc, json.load(handle)
                with open(three_pass_checkpoint_path(script_path)) as handle:
                    return result, json.load(handle)

    def test_detail_shows_where_why_and_the_exact_prompt(self):
        detail, _ = self._run(script_module.generate_script_recovery_detail)
        self.assertTrue(detail["recoverable"])
        self.assertEqual((2, 3, 1), (detail["failed_chunk"], detail["chunks_total"], detail["chunks_done"]))
        self.assertEqual(429, detail["attempts"][0]["http_status"])
        self.assertEqual("rate_limited", detail["attempts"][0]["error_category"])
        self.assertEqual(["low_source_token_recall"], detail["attempts"][1]["failure_codes"])
        self.assertEqual(SOURCE, detail["source"])
        self.assertIn(SOURCE, detail["prompt"]["user"])
        self.assertIn("retry_multiplier", detail["retry_profile"])

    def test_inject_refuses_a_segmentation_that_drops_text(self):
        bad = script_module.InjectSegmentationRequest(chunk=2, entries=[
            {"type": "SPOKEN", "text": "Where is he?"}])
        exc, checkpoint = self._run(script_module.generate_script_inject, bad)
        self.assertEqual(422, exc.status_code)
        self.assertTrue(exc.detail["findings"])
        self.assertEqual("segment_failed", checkpoint["stage"])   # nothing advanced

    def test_inject_accepts_a_faithful_segmentation_and_advances_the_checkpoint(self):
        good = script_module.InjectSegmentationRequest(chunk=2, entries=[
            {"type": "SPOKEN", "text": "Where is he?"},
            {"type": "NARRATOR", "text": "asked Holmes."},
            {"type": "SPOKEN", "text": "Gone,"},
            {"type": "NARRATOR", "text": "said I. Sherlock Holmes frowned and looked at the door "
                                         "for a long moment before he spoke again."}])
        result, checkpoint = self._run(script_module.generate_script_inject, good)
        self.assertEqual({"accepted": True, "chunk": 2, "chunks_done": 2, "resolution": "manual"}, result)
        self.assertEqual("segment", checkpoint["stage"])
        self.assertEqual(5, len(checkpoint["segmented"]))
        self.assertEqual(["clean", "manual"], checkpoint["resolutions"])
        self.assertIsNone(checkpoint["failed"])

    def test_recovery_uses_the_quote_controls_from_the_failed_run(self):
        detail, _ = self._run(script_module.generate_script_recovery_detail,
                              writer=_write_relaxed_quote_run)
        self.assertIn("Quoted text is not required to be SPOKEN",
                      detail["prompt"]["system"])
        merged = script_module.InjectSegmentationRequest(chunk=2, entries=[
            {"type": "NARRATOR", "text": SOURCE}])
        result, checkpoint = self._run(
            script_module.generate_script_inject, merged,
            writer=_write_relaxed_quote_run)
        self.assertTrue(result["accepted"])
        self.assertEqual("NARRATOR", checkpoint["segmented"][-1]["type"])

    def test_skip_narrates_the_chunk_as_is(self):
        result, checkpoint = self._run(script_module.generate_script_skip,
                                       script_module.SkipChunkRequest(chunk=2))
        self.assertEqual("narrated_as_is", result["resolution"])
        added = checkpoint["segmented"][1:]
        self.assertEqual(["SPOKEN", "NARRATOR", "SPOKEN", "NARRATOR"], [e["type"] for e in added])
        self.assertEqual("Where is he?", added[0]["text"])
        self.assertEqual(2, checkpoint["chunks_done"])

    def test_attribute_failure_detail_inject_and_skip(self):
        detail, _ = self._run(script_module.generate_script_recovery_detail,
                              writer=_write_failed_attribute_run)
        self.assertEqual("attribute", detail["failed_pass"])
        self.assertEqual([1, 2, 3], detail["batch_indices"])
        self.assertEqual("connection_error", detail["attempts"][0]["error_category"])
        self.assertIn("Where is he?", detail["prompt"]["user"])
        self.assertIn("HOLMES", detail["prompt"]["user"])
        # a label for a NARRATOR line other than NARRATOR is refused by pass 2's gate
        bad = script_module.InjectSegmentationRequest(entries=[
            {"n": 0, "speaker": "HOLMES"}, {"n": 1, "speaker": "HOLMES"}, {"n": 2, "speaker": "WATSON"}])
        exc, checkpoint = self._run(script_module.generate_script_inject, bad,
                                    writer=_write_failed_attribute_run)
        self.assertEqual(422, exc.status_code)
        self.assertEqual("attribute_failed", checkpoint["stage"])
        good = script_module.InjectSegmentationRequest(entries=[
            {"n": 0, "speaker": "HOLMES"}, {"n": 1, "speaker": "NARRATOR"}, {"n": 2, "speaker": "WATSON"}])
        result, checkpoint = self._run(script_module.generate_script_inject, good,
                                       writer=_write_failed_attribute_run)
        self.assertEqual("manual", result["resolution"])
        self.assertEqual("attribute", checkpoint["stage"])
        self.assertEqual(["NARRATOR", "HOLMES", "NARRATOR", "WATSON"],
                         [e["speaker"] for e in checkpoint["named"]])
        self.assertEqual("Where is he?", checkpoint["named"][1]["text"])
        result, checkpoint = self._run(script_module.generate_script_skip, script_module.SkipChunkRequest(),
                                       writer=_write_failed_attribute_run)
        self.assertEqual("narrated_as_is", result["resolution"])
        self.assertEqual(["NARRATOR", "UNKNOWN", "NARRATOR", "UNKNOWN"],
                         [e["speaker"] for e in checkpoint["named"]])

    def test_wrong_chunk_and_running_generation_are_refused(self):
        exc, _ = self._run(script_module.generate_script_skip, script_module.SkipChunkRequest(chunk=1))
        self.assertEqual(409, exc.status_code)
        with tempfile.TemporaryDirectory() as tmp:
            script_path = _write_failed_run(tmp)
            with patch.object(script_module, "SCRIPT_PATH", script_path), \
                 patch.object(script_module, "DATA_DIR", tmp):
                script_module.process_state["script"]["running"] = True
                try:
                    with self.assertRaises(HTTPException) as ctx:
                        asyncio.run(script_module.generate_script_skip(script_module.SkipChunkRequest(chunk=2)))
                finally:
                    script_module.process_state["script"]["running"] = False
        self.assertEqual(409, ctx.exception.status_code)


if __name__ == "__main__":
    unittest.main()
