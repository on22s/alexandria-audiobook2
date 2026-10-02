"""Failed-request recovery (issue #522 s23 / s4.3 / s4.4): the panel's detail
route, manual segmentation injection through pass 1's own gate, and the
nothing-lost skip."""
import asyncio
import copy
import core
from contextlib import contextmanager
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
    def test_generation_claim_occurs_under_checkpoint_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "annotated_script.json")
            held = False

            @contextmanager
            def tracking_lock(_path):
                nonlocal held
                held = True
                try:
                    yield
                finally:
                    held = False

            original_claim=core.claim_gpu_task
            def claim(task):
                self.assertTrue(held)
                return original_claim(task)
            state=copy.deepcopy(core.process_state)
            for value in state.values():value['running']=False

            with patch.object(script_module, "SCRIPT_PATH", script_path), \
                 patch.object(script_module, "DATA_DIR", tmp), \
                 patch.object(script_module, "three_pass_refusal", return_value=None), \
                 patch.object(script_module, "check_global_gpu_lock"), \
                 patch.object(script_module, "get_active_reasoning_effort", return_value=None), \
                 patch.object(script_module, "build_generate_script_command", return_value=["run"]), \
                 patch.object(script_module, "file_lock", side_effect=tracking_lock), \
                 patch.object(core, "claim_gpu_task", side_effect=claim) as claimed,                  patch.object(core, "process_state", state),                  patch.object(script_module, "process_state", state),                  patch.object(core, "_task_claims", {}),                  patch.object(core, "_gpu_leases", {}),                  patch.object(core, "acquire_gpu_lock", return_value=None):
                try:
                    script_module.start_script_generation(
                        script_module.BackgroundTasks(), os.path.join(tmp, "book.txt"), None)
                    claimed.assert_called_once_with("script")
                    self.assertTrue(core.is_task_running("script"))
                finally:
                    core.release_pending_task_claims()
                self.assertFalse(core.is_task_running("script"))
            self.assertFalse(held)

    def test_manual_recovery_rechecks_running_state_after_checkpoint_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = _write_failed_run(tmp)
            checkpoint_path = three_pass_checkpoint_path(script_path)
            with open(checkpoint_path, "rb") as source:
                before = source.read()
            state = script_module.process_state["script"]
            previous = state["running"]
            real_lock = script_module.file_lock

            @contextmanager
            def generation_claimed_while_waiting(path):
                with real_lock(path):
                    state["running"] = True
                    yield

            try:
                state["running"] = False
                with patch.object(script_module, "SCRIPT_PATH", script_path), \
                     patch.object(script_module, "DATA_DIR", tmp), \
                     patch.object(script_module, "file_lock",
                                  side_effect=generation_claimed_while_waiting):
                    with self.assertRaises(HTTPException) as raised:
                        script_module.apply_manual_recovery([], "manual")
                self.assertEqual(409, raised.exception.status_code)
                with open(checkpoint_path, "rb") as source:
                    self.assertEqual(before, source.read())
            finally:
                state["running"] = previous

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


class GenerateScriptStateTests(unittest.TestCase):
    def test_corrupt_or_nonobject_state_returns400_without_dispatch_or_mutation(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from pathlib import Path
        api = FastAPI()
        api.include_router(script_module.router)
        cases = (b'{"input_file_path":', b'[]', b'["book.txt"]', b'null', b'true',
                 b'7', b'"book.txt"', b'{}', b'{"input_file_path": ""}')
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(script_module, 'DATA_DIR', tmp), \
             patch.object(script_module, 'start_script_generation') as dispatch, \
             TestClient(api, raise_server_exceptions=False) as client:
            state = Path(tmp)/'state.json'
            for payload in cases:
                with self.subTest(payload=payload):
                    state.write_bytes(payload)
                    response = client.post('/api/generate_script')
                    self.assertEqual(400, response.status_code, response.text)
                    self.assertEqual('No input file found in state', response.json()['detail'])
                    dispatch.assert_not_called()
                    self.assertEqual(payload, state.read_bytes())

    def test_missing_state_remains400_and_valid_state_forwards_selected_input_and_options(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from pathlib import Path
        api = FastAPI()
        api.include_router(script_module.router)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(script_module, 'DATA_DIR', tmp), \
             patch.object(script_module, 'start_script_generation', return_value={'status': 'started'}) as dispatch, \
             TestClient(api) as client:
            state = Path(tmp)/'state.json'
            missing = client.post('/api/generate_script')
            self.assertEqual(400, missing.status_code, missing.text)
            self.assertEqual('No input file selected', missing.json()['detail'])
            dispatch.assert_not_called()
            book = Path(tmp)/'book.txt'
            book.write_text('Selected source text.', encoding='utf-8')
            payload = json.dumps({'input_file_path': str(book), 'custom': {'keep': True}}).encode()
            state.write_bytes(payload)
            options = {'strip_front_matter': False, 'first_person_narrator': 'Watson', 'start_over': True}
            response = client.post('/api/generate_script', json=options)
            self.assertEqual(200, response.status_code, response.text)
            dispatch.assert_called_once()
            tasks, input_file, request = dispatch.call_args.args
            self.assertIsInstance(tasks, script_module.BackgroundTasks)
            self.assertEqual(str(book), input_file)
            self.assertEqual(options, request.model_dump())
            self.assertEqual(payload, state.read_bytes())
            self.assertEqual('Selected source text.', book.read_text(encoding='utf-8'))


class BatchReportPublicationTests(unittest.TestCase):
    def _write_report(self, directory):
        with patch.object(script_module, 'REPORTS_DIR', directory), \
             patch.object(script_module.time, 'strftime', return_value='fixed'):
            return script_module._write_batch_review_report(
                {'tasks': [], 'totals_fwd': {'total_changes': 0}}, ['book'], False, False)

    def test_interrupted_write_preserves_old_report_and_leaves_no_temp(self):
        import builtins
        from pathlib import Path
        real_open = builtins.open
        real_fdopen = os.fdopen
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'batch_review_fixed.md'
            original = b'previous complete report\n'
            path.write_bytes(original)

            @contextmanager
            def interrupted(handle):
                with handle as stream:
                    def write(text):
                        stream.write(text[:len(text) // 2])
                        stream.flush()
                        raise OSError('injected interruption during report write')
                    from types import SimpleNamespace
                    yield SimpleNamespace(write=write, flush=stream.flush, fileno=stream.fileno)

            def wrap_open(file, mode='r', *args, **kwargs):
                handle = real_open(file, mode, *args, **kwargs)
                return interrupted(handle) if 'w' in mode and str(file).endswith('.md') else handle

            def wrap_fdopen(fd, *args, **kwargs):
                return interrupted(real_fdopen(fd, *args, **kwargs))

            with patch('builtins.open', side_effect=wrap_open), \
                 patch.object(script_module.os, 'fdopen', side_effect=wrap_fdopen):
                self.assertIsNone(self._write_report(tmp))
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([path.name], sorted(item.name for item in Path(tmp).iterdir()))

    def test_reader_sees_old_report_until_complete_replacement(self):
        from pathlib import Path
        real_replace = os.replace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'batch_review_fixed.md'
            original = b'previous complete report\n'
            path.write_bytes(original)
            published = []

            def inspect_replace(source, destination):
                self.assertEqual(original, path.read_bytes())
                content = Path(source).read_bytes()
                self.assertTrue(content.startswith(b'# Batch Review Report\n'))
                self.assertTrue(content.endswith(b'\n'))
                self.assertIn(b'**Total changes:** 0', content)
                real_replace(source, destination)
                published.append(content)

            with patch.object(script_module.os, 'replace', side_effect=inspect_replace):
                self.assertEqual(str(path), self._write_report(tmp))
            self.assertEqual(1, len(published))
            self.assertEqual(published[0], path.read_bytes())
            self.assertEqual([path.name], sorted(item.name for item in Path(tmp).iterdir()))

    def test_failed_replacement_preserves_report_and_removes_staged_file(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'batch_review_fixed.md'
            original = b'previous complete report\n'
            path.write_bytes(original)
            with patch.object(script_module.os, 'replace', side_effect=OSError('unavailable')):
                self.assertIsNone(self._write_report(tmp))
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([path.name], sorted(item.name for item in Path(tmp).iterdir()))
