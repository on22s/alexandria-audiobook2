"""Failed-request recovery (issue #522 s23 / s4.3 / s4.4): the panel's detail
route, manual segmentation injection through pass 1's own gate, and the
nothing-lost skip."""
import asyncio
import copy
import core
import hashlib
from contextlib import contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from book_state_transaction import get_book_operation_token
from generate_script import LLMGenParams
from pass_quality import split_outer_quote_regions
from routers import script as script_module
from three_pass_generate import (get_prepared_source, get_segment_chunk_records,
                                 three_pass_checkpoint_path, three_pass_fingerprint,
                                 three_pass_manifest_path, validate_three_pass_checkpoint)
from utils import atomic_json_write

SOURCE = ('"Where is he?" asked Holmes. "Gone," said I. Sherlock Holmes frowned and '
          'looked at the door for a long moment before he spoke again.')
FILLER = 'The quiet river moved past the old stone bridge. Nothing else happened that day. '
FRONT_MATTER = ('Manifesto.\nTranslator introduction.\nContents.\n'
                'Original Web Novel Chapter ― Complete.\n'
                'Original Translation by A. Translator.\n\n')
ATTESTED = ['Holmes watched the door.', 'Holmes looked at the window.',
            'Watson waited outside.', 'Watson closed the door.']


def _write_run_receipt(tmp, source_text, options=None, *, with_receipt=True):
    """Bind a fixture to the selected source, with a receipt for modern runs."""
    Path(os.path.join(tmp, 'config.json')).write_text(json.dumps({
        'llm_mode': 'local',
        'llm': {'model_name': 'fixture-model',
                'base_url': 'http://localhost:1234/v1'},
        'generation': {}}), encoding='utf-8')
    state_path = os.path.join(tmp, 'state.json')
    with open(state_path, encoding='utf-8') as handle:
        state = json.load(handle)
    book = state['input_file_path']
    raw = hashlib.sha256(Path(book).read_bytes()).hexdigest()
    selection = state.get('script_generation_selection') or {
        'book_token': get_book_operation_token(tmp, state=state),
        'source_sha256': raw, 'source_path': book}
    state['script_generation_selection'] = selection
    state['script_generation_options'] = options or state.get('script_generation_options') or {}
    with open(state_path, 'w', encoding='utf-8') as handle:
        json.dump(state, handle)
    if not with_receipt:
        return
    prepared, _ = get_prepared_source(
        book, state['script_generation_options'].get('strip_front_matter'), owning_root=tmp)
    with patch.object(script_module, 'CONFIG_PATH', os.path.join(tmp, 'config.json')):
        receipt = script_module.resolve_authoritative_run_spec(
            prepared, book, state['script_generation_options'], selection,
            state.get('script_generation_run_id'))
    Path(script_module.run_spec_receipt.receipt_path(tmp)).write_bytes(
        script_module.run_spec_receipt.receipt_bytes(receipt))


def _fixture_chunk_total(text):
    return len(get_segment_chunk_records(text, 3000, LLMGenParams()))


def _long_source(mentions=(), length=10834):
    text = (' '.join(mentions) + ' ') if mentions else ''
    while len(text) < length:
        text += FILLER
    return text[:length]


def _write_fingerprinted_attribute_run(tmp, source_text, entries, roster=('HOLMES', 'WATSON'),
                                       cast=None, strip_front_matter=False, selection=True,
                                       source_digest=None):
    """A genuine writer-shaped checkpoint: prepared-text fingerprint, saved
    preparation options and a raw selection digest all agree with the source."""
    script_path = os.path.join(tmp, 'annotated_script.json')
    book = os.path.join(tmp, 'book.txt')
    prefix_rows = 'Prelude. ' + ' '.join(
        ('"' + row['text'] + '"') if row.get('type') == 'SPOKEN' else row.get('text', '')
        for row in entries if row.get('text'))
    source_with_rows = prefix_rows + ' ' + source_text
    Path(book).write_text(source_with_rows, encoding='utf-8')
    prepared, _ = get_prepared_source(book, strip_front_matter, owning_root=tmp)
    stripped = prepared
    cast_sha = hashlib.sha256(cast).hexdigest() if cast is not None else None
    state = {'input_file_path': book, 'script_generation_input_file': book,
             'script_generation_options': {'strip_front_matter': strip_front_matter,
                                           'cast_sha256': cast_sha},
             'script_generation_run_id': 'a' * 32}
    if selection:
        state['script_generation_selection'] = {
            'source_sha256': hashlib.sha256(Path(book).read_bytes()).hexdigest(),
            'source_path': book,
            'book_token': get_book_operation_token(tmp, state=state)}
    atomic_json_write(state, os.path.join(tmp, 'state.json'))
    if cast is not None:
        Path(os.path.join(tmp, 'script_run_cast.json')).write_bytes(cast)
    _write_run_receipt(tmp, prepared)
    receipt = script_module.run_spec_receipt.load_receipt(tmp)
    fingerprint = dict(receipt['fingerprint'])
    if source_digest is not None:
        fingerprint['source_sha256'] = source_digest
    remainder = ' '.join(prepared.split()[len(prefix_rows.split()):])
    segmented = ([{'type': 'NARRATOR', 'text': 'Prelude.'}] + list(entries)
                 + [{'type': 'NARRATOR', 'text': remainder}])
    done = receipt['chunk_count']
    atomic_json_write({
        'fingerprint': fingerprint, 'run_id': receipt['run_id'],
        'run_spec_sha256': receipt['sha256'],
        'status': 'failed', 'failed_pass': 'attribute',
        'chunks': [{'index': index + 1, 'resolution': 'clean'} for index in range(done)],
        'counts': {}, 'passes': {}, 'diagnostic_failures': [],
        'progress': {'chunks_total': done, 'chunks_attempted': done,
                     'chunks_completed': done, 'source_words': len(stripped.split()),
                     'source_words_total': len(stripped.split()),
                     'segmented_entries': len(segmented), 'attributed_entries': 1},
        'telemetry': {'model_name': fingerprint['model_name'],
                      'cast': ({'sha256': cast_sha, 'names': 2} if cast is not None else None)}},
        three_pass_manifest_path(script_path))
    atomic_json_write({
        'fingerprint': fingerprint, 'stage': 'attribute_failed', 'chunks_done': done,
        script_module.run_spec_receipt.CHECKPOINT_DIGEST:
            script_module.run_spec_receipt.get_checkpoint_spec_digest(receipt),
        'segmented': segmented,
        'named': [{'text': 'Prelude.', 'speaker': 'NARRATOR'}] + [None] * (len(segmented) - 1),
        'annotated': [], 'resolutions': ['clean'] * done, 'elapsed_s': {}, 'diagnostic_failures': [],
        'failed': {'pass': 'attribute', 'indices': [1, 2, 3], 'entries': list(entries),
                   'roster': list(roster), 'cast_sha256': cast_sha,
                   'reason': 'attribute LLM unavailable; refusing fallback output',
                   'attempts': []}}, three_pass_checkpoint_path(script_path))
    return script_path


def _write_failed_attribute_run(tmp):
    script_path = os.path.join(tmp, "annotated_script.json")
    atomic_json_write({"fingerprint": "fp", "status": "failed", "failed_pass": "attribute",
                       "chunks": [], "counts": {}, "passes": {}, "diagnostic_failures": []},
                      three_pass_manifest_path(script_path))
    entries = [{"type": "SPOKEN", "text": "Where is he?"},
               {"type": "NARRATOR", "text": "asked Holmes. Watson watched the door."},
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
    book = os.path.join(tmp, 'book.txt')
    Path(book).write_text(SOURCE, encoding='utf-8')
    atomic_json_write({"input_file_path": book, "script_generation_input_file": book,
                       "script_generation_options": {"strip_front_matter": False,
                                                     "cast_sha256": None},
                       "script_generation_run_id": "a" * 32},
                      os.path.join(tmp, "state.json"))
    _write_run_receipt(tmp, SOURCE, with_receipt=False)
    return script_path


def _write_fingerprinted_pass_run(tmp, pass_name):
    """Writer-shaped modern segment_failed / instruct_failed artifact pair."""
    script_path = os.path.join(tmp, 'annotated_script.json')
    source_text = SOURCE
    entries = [{"type": row['type'], "text": row['text']}
               for row in split_outer_quote_regions(source_text) if row.get('text', '').strip()]
    if pass_name == 'segment':
        segmented, named, annotated, failed = [], [], [], {
            'pass': 'segment', 'chunk': 1, 'chunks_total': 1,
            'source': source_text, 'failure_codes': [], 'attempts': []}
        stage = 'segment_failed'
        segmented_entries, attributed_entries = 0, 0
    else:
        segmented = entries
        named = [{'speaker': 'HOLMES', 'text': row['text']} if i % 2 == 0
                 else {'speaker': 'NARRATOR', 'text': row['text']}
                 for i, row in enumerate(entries)]
        annotated = [dict(named[0], instruct='Calm.')]
        failed = {'pass': 'instruct', 'indices': list(range(1, len(named))),
                  'entries': [{'speaker': row['speaker'], 'text': row['text']}
                              for row in named[1:]],
                  'delivery_direction': 'off', 'attempts': []}
        stage = 'instruct_failed'
        segmented_entries, attributed_entries = len(entries), len(named)
    book = os.path.join(tmp, 'book.txt')
    Path(book).write_text(source_text, encoding='utf-8')
    atomic_json_write({'input_file_path': book, 'script_generation_input_file': book,
                       'script_generation_options': {'strip_front_matter': False,
                                                     'cast_sha256': None},
                       'script_generation_run_id': 'a' * 32},
                      os.path.join(tmp, 'state.json'))
    _write_run_receipt(tmp, source_text)
    receipt = script_module.run_spec_receipt.load_receipt(tmp)
    fingerprint = receipt['fingerprint']
    atomic_json_write({'fingerprint': fingerprint, 'run_id': receipt['run_id'],
                       'run_spec_sha256': receipt['sha256'], 'status': 'failed',
                       'failed_pass': pass_name, 'chunks': [{'index': 1, 'resolution': 'fail'}],
                       'progress': {'chunks_total':
                                    _fixture_chunk_total(source_text),
                                    'chunks_attempted': 0 if pass_name == 'segment' else 1,
                                    'segmented_entries': segmented_entries,
                                    'attributed_entries': attributed_entries},
                       'telemetry': {'model_name': fingerprint['model_name']}},
                      three_pass_manifest_path(script_path))
    atomic_json_write({'fingerprint': fingerprint, 'stage': stage,
                       script_module.run_spec_receipt.CHECKPOINT_DIGEST:
                           script_module.run_spec_receipt.get_checkpoint_spec_digest(receipt),
                       'chunks_done': 0 if pass_name == 'segment' else 1,
                       'segmented': segmented, 'named': named, 'annotated': annotated,
                       'resolutions': ['fail'], 'elapsed_s': {}, 'diagnostic_failures': [],
                       'failed': failed}, three_pass_checkpoint_path(script_path))
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
    book = os.path.join(tmp, 'book.txt')
    Path(book).write_text(SOURCE, encoding='utf-8')
    atomic_json_write({"input_file_path": book, "script_generation_input_file": book,
                       "script_generation_options": {"strip_front_matter": False,
                                                     "cast_sha256": None},
                       "script_generation_run_id": "a" * 32},
                      os.path.join(tmp, "state.json"))
    _write_run_receipt(tmp, SOURCE, with_receipt=False)
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


def _write_failed_instruct_run(tmp, direction=None):
    """All text here is synthetic. `direction=None` writes an old checkpoint."""
    script_path = os.path.join(tmp, "annotated_script.json")
    atomic_json_write({"fingerprint": "fp", "status": "failed", "failed_pass": "instruct",
                       "chunks": [], "counts": {}, "passes": {}, "diagnostic_failures": []},
                      three_pass_manifest_path(script_path))
    entries = [{"speaker": "HOLMES", "text": "Where is he?"},
               {"speaker": "WATSON", "text": "Gone,"}]
    failed = {"pass": "instruct", "indices": [1, 2], "entries": entries,
              "reason": "instruct LLM unavailable; refusing fallback output",
              "attempts": [{"attempt": 1, "outcome": "api_error", "error_category": "connection_error",
                            "http_status": None, "error": "Connection error."}]}
    if direction is not None:
        failed["delivery_direction"] = direction
    atomic_json_write({
        "fingerprint": "fp", "stage": "instruct_failed", "chunks_done": 1,
        "segmented": [{"type": "NARRATOR", "text": "Chapter one."}],
        "named": [{"speaker": "NARRATOR", "text": "Chapter one."}] + entries,
        "annotated": [], "resolutions": ["clean"], "elapsed_s": {}, "diagnostic_failures": [],
        "failed": failed}, three_pass_checkpoint_path(script_path))
    book = os.path.join(tmp, 'book.txt')
    Path(book).write_text(SOURCE, encoding='utf-8')
    atomic_json_write({"input_file_path": book, "script_generation_input_file": book,
                       "script_generation_options": {"strip_front_matter": False,
                                                     "cast_sha256": None},
                       "script_generation_run_id": "a" * 32},
                      os.path.join(tmp, "state.json"))
    _write_run_receipt(tmp, SOURCE, with_receipt=False)
    return script_path


def _instruct_writer(direction):
    def write(tmp):
        return _write_failed_instruct_run(tmp, direction)
    return write


class RecoveryTests(unittest.TestCase):
    def test_generation_claim_occurs_under_checkpoint_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "annotated_script.json")
            atomic_json_write({"input_file_path": os.path.join(tmp, "book.txt")}, os.path.join(tmp, "state.json"))
            with open(os.path.join(tmp, "book.txt"), "w", encoding="utf-8") as source:
                source.write("Fixture source.")
            held = set()

            @contextmanager
            def tracking_lock(path):
                held.add(path)
                try:
                    yield
                finally:
                    held.discard(path)

            original_claim=core.claim_gpu_task
            def claim(task, **kwargs):
                self.assertIn(three_pass_checkpoint_path(script_path), held)
                return original_claim(task, **kwargs)
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
                    claimed.assert_called_once_with("script", defer_process_state=True)
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

    def _inject_http(self, entries, writer=_write_failed_attribute_run, setup=None,
                     route='inject', payload=None, method='post'):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = writer(tmp)
            checkpoint_path = three_pass_checkpoint_path(script_path)
            manifest_path = three_pass_manifest_path(script_path)
            if setup is not None:
                setup(tmp)
            with open(checkpoint_path, "rb") as handle:
                before_checkpoint = handle.read()
            with open(manifest_path, "rb") as handle:
                before_manifest = handle.read()
            app = FastAPI()
            app.include_router(script_module.router)
            previous = script_module.process_state["script"]["running"]
            script_module.process_state["script"]["running"] = False
            try:
                with patch.object(script_module, "SCRIPT_PATH", script_path), \
                     patch.object(script_module, "DATA_DIR", tmp), \
                     patch.object(script_module, "CONFIG_PATH", os.path.join(tmp, "config.json")):
                    with TestClient(app) as client:
                        url = "/api/generate_script/" + route
                        if method == 'get':
                            response = client.get(url)
                        else:
                            response = client.post(
                                url, json=payload if payload is not None else {"entries": entries})
                with open(checkpoint_path, "rb") as handle:
                    after_checkpoint = handle.read()
                with open(manifest_path, "rb") as handle:
                    after_manifest = handle.read()
                return (response, before_checkpoint, after_checkpoint,
                        before_manifest, after_manifest)
            finally:
                script_module.process_state["script"]["running"] = previous

    def _fingerprinted_writer(self, source_text, **kwargs):
        entries = [{"type": "SPOKEN", "text": "Where is he?"},
                   {"type": "NARRATOR", "text": "said Holmes."},
                   {"type": "SPOKEN", "text": "Gone,"}]
        def write(tmp):
            return _write_fingerprinted_attribute_run(tmp, source_text, entries, **kwargs)
        return write

    def test_attribute_inject_refuses_invented_speaker_and_preserves_both_artifacts(self):
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "FUTURE_ME"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}])
        self.assertEqual(422, response.status_code)
        self.assertEqual(["speaker_not_in_source"],
                         [f["code"] for f in response.json()["detail"]["findings"]])
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_book_name_attested_twice_outside_failed_batch_is_accepted(self):
        source = _long_source(['MIRA walked beside the river.', 'MIRA waited at the gate.'])
        self.assertGreater(len(source), 5000)
        response, _, checkpoint_after, _, _ = self._inject_http([
            {"n": 0, "speaker": "MIRA"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "MIRA"}], writer=self._fingerprinted_writer(source, roster=['MIRA']))
        self.assertEqual(200, response.status_code)
        self.assertEqual('manual', response.json()['resolution'])
        validate_three_pass_checkpoint(json.loads(checkpoint_after))

    def test_book_name_attested_once_in_long_source_is_refused(self):
        source = _long_source(['MIRA walked beside the river.'])
        self.assertEqual(10834, len(source))
        response, checkpoint_before, checkpoint_after, _, _ = self._inject_http([
            {"n": 0, "speaker": "MIRA"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "MIRA"}], writer=self._fingerprinted_writer(source, roster=['MIRA']))
        self.assertEqual(422, response.status_code)
        self.assertIn('speaker_not_in_source',
                      [f['code'] for f in response.json()['detail']['findings']])
        self.assertEqual(checkpoint_before, checkpoint_after)

    def test_source_speech_tag_contradiction_is_refused(self):
        source = ('"Where is he?" said Holmes. "Gone," said Holmes. ' + ' '.join(ATTESTED) + ' ' + FILLER * 130)
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "FUTURE_ME"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=self._fingerprinted_writer(source))
        self.assertEqual(422, response.status_code)
        self.assertIn('speaker_not_in_source',
                      [f['code'] for f in response.json()['detail']['findings']])
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_roster_only_lowercase_attestation_is_refused_like_live(self):
        source = _long_source(['She planted a rose by the wall.', 'Another rose grew near the gate.'])
        response, _, _, _, _ = self._inject_http([
            {"n": 0, "speaker": "ROSE"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "ROSE"}], writer=self._fingerprinted_writer(source, roster=['ROSE']))
        self.assertEqual(422, response.status_code)
        self.assertIn('speaker_not_in_source',
                      [f['code'] for f in response.json()['detail']['findings']])

    def test_verified_cast_alias_persists_the_canonical_name(self):
        source = _long_source(ATTESTED)
        cast = json.dumps([{"name": "HOLMES", "aliases": ["SHERLOCK"]},
                           {"name": "WATSON", "aliases": ["JOHN"]}]).encode('utf-8')
        response, _, checkpoint_after, _, _ = self._inject_http([
            {"n": 0, "speaker": "SHERLOCK"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}],
            writer=self._fingerprinted_writer(source, cast=cast))
        self.assertEqual(200, response.status_code)
        self.assertEqual('HOLMES', json.loads(checkpoint_after)['named'][1]['speaker'])

    def test_stale_or_unbound_cast_cannot_admit_an_alias(self):
        source = _long_source(ATTESTED)
        for cast, expected in ((None, 422), (b'[{"name":"HOLMES","aliases":["SHERLOCK"]}]', 409)):
            with self.subTest(cast=cast):
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    if cast is not None:
                        Path(os.path.join(tmp, 'script_run_cast.json')).write_bytes(cast)
                response, _, _, _, _ = self._inject_http([
                    {"n": 0, "speaker": "SHERLOCK"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(expected, response.status_code)
                if expected == 422:
                    self.assertIn('speaker_not_in_source',
                                  [f['code'] for f in response.json()['detail']['findings']])

    def test_changed_or_missing_source_is_refused_without_legacy_downgrade(self):
        source = _long_source(ATTESTED)
        for change in ('changed', 'missing'):
            with self.subTest(change=change):
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    book = os.path.join(tmp, 'book.txt')
                    if change == 'missing':
                        os.unlink(book)
                    else:
                        Path(book).write_text(source + ' A different ending.', encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertNotIn('book.txt', response.json()['detail'])
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_saved_front_matter_option_is_used_for_the_source_proof(self):
        source = FRONT_MATTER + _long_source(ATTESTED)
        entries = [{"type": "SPOKEN", "text": "Where is he?"},
                   {"type": "NARRATOR", "text": "said Holmes."},
                   {"type": "SPOKEN", "text": "Gone,"}]
        def write(tmp):
            return _write_fingerprinted_attribute_run(
                tmp, source, entries, roster=['HOLMES', 'WATSON'], strip_front_matter=True)
        response, _, _, _, _ = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=write)
        self.assertEqual(200, response.status_code)

    def test_malformed_fingerprint_digest_is_not_downgraded(self):
        source = _long_source(ATTESTED)
        writer = self._fingerprinted_writer(source, source_digest='not-a-digest')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_retained_text_or_type_mismatch_is_refused(self):
        source = _long_source(ATTESTED)
        for field in ('text', 'type'):
            with self.subTest(field=field):
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
                    checkpoint['segmented'][1][field] = 'CHANGED'
                    Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_writer_segment_failed_inject_and_skip_are_accepted(self):
        for route, payload in (
                ('inject', {'chunk': 1, 'entries': [
                    {'type': row['type'], 'text': row['text']}
                    for row in split_outer_quote_regions(SOURCE) if row.get('text', '').strip()]}),
                ('skip', {'chunk': 1})):
            with self.subTest(route=route):
                writer = lambda tmp: _write_fingerprinted_pass_run(tmp, 'segment')
                response, _, checkpoint_after, _, _ = self._inject_http(
                    [], writer=writer, route=route, payload=payload)
                self.assertEqual(200, response.status_code)
                validate_three_pass_checkpoint(json.loads(checkpoint_after), 1)

    def test_first_chunk_segment_failure_with_pending_receipt_is_accepted(self):
        writer = lambda tmp: _write_fingerprinted_pass_run(tmp, 'segment')
        def setup(tmp):
            cpath = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
            mpath = three_pass_manifest_path(os.path.join(tmp, 'annotated_script.json'))
            checkpoint = json.loads(Path(cpath).read_text(encoding='utf-8'))
            checkpoint['chunks_done'] = 0
            Path(cpath).write_text(json.dumps(checkpoint), encoding='utf-8')
            manifest = json.loads(Path(mpath).read_text(encoding='utf-8'))
            manifest['progress']['chunks_attempted'] = 0
            manifest['chunks'] = [{'index': 1, 'resolution': 'pending'}]
            Path(mpath).write_text(json.dumps(manifest), encoding='utf-8')
        response, _, checkpoint_after, _, _ = self._inject_http(
            [], writer=writer, setup=setup, payload={'chunk': 1, 'entries': [
                {'type': row['type'], 'text': row['text']}
                for row in split_outer_quote_regions(SOURCE) if row.get('text', '').strip()]})
        self.assertEqual(200, response.status_code)
        validate_three_pass_checkpoint(json.loads(checkpoint_after), 1)

    def test_committed_manifest_progress_is_derived_from_checkpoint(self):
        writer = self._fingerprinted_writer(_long_source(ATTESTED))
        response, _, checkpoint_after, _, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer)
        self.assertEqual(200, response.status_code)
        manifest = json.loads(manifest_after)
        checkpoint = json.loads(checkpoint_after)
        self.assertNotIn('failed_pass', manifest)
        self.assertEqual(len(checkpoint['segmented']),
                         manifest['progress']['segmented_entries'])
        self.assertEqual(sum(isinstance(row, dict) for row in checkpoint['named']),
                         manifest['progress']['attributed_entries'])

    def test_writer_instruct_failed_inject_and_skip_are_accepted(self):
        for route, payload in (
                ('inject', {'chunk': 1, 'entries': [
                    {'n': n, 'instruct': 'Calm, measured.'} for n in (0, 1, 2)]}),
                ('skip', {})):
            with self.subTest(route=route):
                writer = lambda tmp: _write_fingerprinted_pass_run(tmp, 'instruct')
                response, _, checkpoint_after, _, _ = self._inject_http(
                    [], writer=writer, route=route, payload=payload)
                self.assertEqual(200, response.status_code)
                validate_three_pass_checkpoint(json.loads(checkpoint_after), 1)

    def test_trailing_row_removed_from_segmentation_and_named_is_refused(self):
        source = _long_source(ATTESTED)
        writer = self._fingerprinted_writer(source)
        def setup(tmp):
            path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
            checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
            checkpoint['segmented'] = checkpoint['segmented'][:-1]
            checkpoint['named'] = checkpoint['named'][:-1]
            Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_missing_or_inflated_progress_is_refused(self):
        for change in ('missing', 'inflated'):
            with self.subTest(change=change):
                source = _long_source(ATTESTED)
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    cpath = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    mpath = three_pass_manifest_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(cpath).read_text(encoding='utf-8'))
                    manifest = json.loads(Path(mpath).read_text(encoding='utf-8'))
                    if change == 'missing':
                        manifest.pop('progress')
                    else:
                        checkpoint['chunks_done'] = 500
                        manifest['progress']['chunks_total'] = 500
                    Path(cpath).write_text(json.dumps(checkpoint), encoding='utf-8')
                    Path(mpath).write_text(json.dumps(manifest), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_boolean_missing_or_empty_model_identity_is_not_legacy(self):
        for change in ('bool', 'deleted', 'empty-model'):
            with self.subTest(change=change):
                source = _long_source(ATTESTED)
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    cpath = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    mpath = three_pass_manifest_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(cpath).read_text(encoding='utf-8'))
                    manifest = json.loads(Path(mpath).read_text(encoding='utf-8'))
                    if change == 'bool':
                        checkpoint['fingerprint'] = True
                        manifest['fingerprint'] = True
                    elif change == 'deleted':
                        checkpoint.pop('fingerprint')
                        manifest.pop('fingerprint')
                    else:
                        checkpoint['fingerprint']['model_name'] = ''
                        manifest['fingerprint']['model_name'] = ''
                    Path(cpath).write_text(json.dumps(checkpoint), encoding='utf-8')
                    Path(mpath).write_text(json.dumps(manifest), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertGreaterEqual(response.status_code, 400)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_nested_model_binding_and_pass_transition_are_refused(self):
        for change in ('nested-binding', 'stage-switch'):
            with self.subTest(change=change):
                source = _long_source(ATTESTED)
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    cpath = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(cpath).read_text(encoding='utf-8'))
                    if change == 'nested-binding':
                        checkpoint['fingerprint']['model_binding'] = {
                            'primary_model': 'other', 'failover_model': None,
                            'failover_used': False}
                    else:
                        checkpoint['stage'] = 'instruct_failed'
                    Path(cpath).write_text(json.dumps(checkpoint), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_selection_replaced_after_admission_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fingerprinted_writer(_long_source(ATTESTED))(tmp)
            state_path = os.path.join(tmp, 'state.json')
            with open(state_path, encoding='utf-8') as handle:
                original_selection = json.load(handle)['script_generation_selection']
            with open(state_path, encoding='utf-8') as handle:
                state = json.load(handle)
            state['book_generation'] = 'new-generation'
            state['script_generation_selection'] = {
                **original_selection, 'book_token': 'x' * 64}
            with open(state_path, 'w', encoding='utf-8') as handle:
                json.dump(state, handle)
            checkpoint_path = three_pass_checkpoint_path(path)
            before = open(checkpoint_path, 'rb').read()
            script_module.process_state['script']['running'] = False
            with patch.object(script_module, 'SCRIPT_PATH', path), \
                 patch.object(script_module, 'DATA_DIR', tmp), \
                 patch.object(script_module, 'CONFIG_PATH', os.path.join(tmp, 'config.json')):
                with self.assertRaises(HTTPException) as raised:
                    script_module.apply_manual_recovery(
                        [{'n': 0, 'speaker': 'HOLMES'},
                         {'n': 1, 'speaker': 'NARRATOR'},
                         {'n': 2, 'speaker': 'WATSON'}], 'manual',
                        admission_selection=original_selection)
            self.assertEqual(409, raised.exception.status_code)
            self.assertEqual(before, open(checkpoint_path, 'rb').read())

    def test_one_sided_cast_evidence_is_refused(self):
        for change in ('manifest-missing', 'failed-missing'):
            with self.subTest(change=change):
                source = _long_source(ATTESTED)
                cast = json.dumps([{"name": "HOLMES", "aliases": ["SHERLOCK"]}]).encode('utf-8')
                writer = self._fingerprinted_writer(source, cast=cast)
                def setup(tmp):
                    cpath = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    mpath = three_pass_manifest_path(os.path.join(tmp, 'annotated_script.json'))
                    if change == 'manifest-missing':
                        manifest = json.loads(Path(mpath).read_text(encoding='utf-8'))
                        manifest['telemetry']['cast'] = None
                        Path(mpath).write_text(json.dumps(manifest), encoding='utf-8')
                    else:
                        checkpoint = json.loads(Path(cpath).read_text(encoding='utf-8'))
                        checkpoint['failed'].pop('cast_sha256')
                        Path(cpath).write_text(json.dumps(checkpoint), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "SHERLOCK"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_model_and_pass_identity_contradictions_are_refused(self):
        for field, value in (('model_name', 'other'), ('primary_model', 'other'),
                             ('failed_pass', 'segment')):
            with self.subTest(field=field):
                source = _long_source(ATTESTED)
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    path = three_pass_manifest_path(os.path.join(tmp, 'annotated_script.json'))
                    manifest = json.loads(Path(path).read_text(encoding='utf-8'))
                    if field == 'failed_pass':
                        manifest['failed_pass'] = value
                    elif field == 'model_name':
                        manifest['telemetry']['model_name'] = value
                    else:
                        manifest['primary_model'] = value
                    Path(path).write_text(json.dumps(manifest), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_nan_and_duplicate_key_state_is_refused(self):
        for raw in ('{"input_file_path": NaN}',
                    '{"input_file_path": "a", "input_file_path": "b"}'):
            with self.subTest(raw=raw):
                def setup(tmp):
                    Path(os.path.join(tmp, 'state.json')).write_text(raw, encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_malformed_saved_roster_variants_are_refused_across_routes(self):
        for roster in (None, False, 0, '', {}, ['', 'HOLMES'], ['   '], [1]):
            with self.subTest(roster=roster):
                def setup(tmp):
                    path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
                    checkpoint['failed']['roster'] = roster
                    Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
                for route in ('inject', 'skip', 'recovery/detail'):
                    with self.subTest(route=route):
                        payload = None if route == 'recovery/detail' else (
                            {"entries": [{"n": 0, "speaker": "HOLMES"},
                                         {"n": 1, "speaker": "NARRATOR"},
                                         {"n": 2, "speaker": "WATSON"}]})
                        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http(
                            [], setup=setup, route=route, payload=payload,
                            method='get' if route == 'recovery/detail' else 'post')
                        self.assertEqual(409, response.status_code)
                        self.assertEqual(checkpoint_before, checkpoint_after)
                        self.assertEqual(manifest_before, manifest_after)

    def test_null_saved_or_submitted_rows_are_bounded_conflicts(self):
        for kind in ('saved-entry', 'submitted-row', 'list-speaker'):
            with self.subTest(kind=kind):
                def setup(tmp):
                    if kind != 'saved-entry':
                        return
                    path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
                    checkpoint['failed']['entries'][0] = None
                    Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
                entries = ([{"n": 0, "speaker": ["HOLMES"]},
                            {"n": 1, "speaker": "NARRATOR"},
                            {"n": 2, "speaker": "WATSON"}] if kind == 'list-speaker'
                           else [{"n": 0, "speaker": "HOLMES"},
                                 {"n": 1, "speaker": "NARRATOR"},
                                 {"n": 2, "speaker": "WATSON"}])
                payload = {'entries': [None]} if kind == 'submitted-row' else {'entries': entries}
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http(
                    [], setup=setup, payload=payload)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_malformed_manifest_receipt_types_are_bounded_conflicts(self):
        for field, value in (('cast', []), ('recovered_units', {}), ('nan', float('nan'))):
            with self.subTest(field=field):
                source = _long_source(ATTESTED)
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    path = three_pass_manifest_path(os.path.join(tmp, 'annotated_script.json'))
                    manifest = json.loads(Path(path).read_text(encoding='utf-8'))
                    if field == 'cast':
                        manifest['telemetry']['cast'] = value
                    elif field == 'nan':
                        manifest['telemetry']['estimate'] = value
                    else:
                        manifest['recovered_units'] = value
                    Path(path).write_text(json.dumps(manifest), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_out_of_range_chunks_done_is_refused_before_publication(self):
        source = _long_source(ATTESTED)
        writer = self._fingerprinted_writer(source)
        def setup(tmp):
            path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
            checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
            checkpoint['chunks_done'] = 500
            Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_partial_or_mismatched_writer_identity_is_not_downgraded(self):
        changes = (
            ('checkpoint', 409, lambda c, m: c['fingerprint'].pop('source_sha256')),
            ('settings', 409, lambda c, m: c['fingerprint'].__setitem__('settings_sha256', 'a' * 64)),
            ('manifest', 409, lambda c, m: m['fingerprint'].__setitem__('source_sha256', 'b' * 64)),
        )
        for name, expected, change in changes:
            with self.subTest(name=name):
                source = _long_source(ATTESTED)
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    cpath = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    mpath = three_pass_manifest_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(cpath).read_text(encoding='utf-8'))
                    manifest = json.loads(Path(mpath).read_text(encoding='utf-8'))
                    change(checkpoint, manifest)
                    Path(cpath).write_text(json.dumps(checkpoint), encoding='utf-8')
                    Path(mpath).write_text(json.dumps(manifest), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(expected, response.status_code)
                if expected >= 400:
                    self.assertEqual(checkpoint_before, checkpoint_after)
                    self.assertEqual(manifest_before, manifest_after)

    def test_malformed_state_or_selection_is_refused(self):
        def setup_factory(kind):
            def setup(tmp):
                path = os.path.join(tmp, 'state.json')
                if kind == 'invalid-json':
                    Path(path).write_text('{broken', encoding='utf-8')
                elif kind == 'non-dict':
                    Path(path).write_text('[]', encoding='utf-8')
                else:
                    state = json.loads(Path(path).read_text(encoding='utf-8'))
                    state['script_generation_selection'] = []
                    Path(path).write_text(json.dumps(state), encoding='utf-8')
            return setup
        for kind in ('invalid-json', 'non-dict', 'non-dict-selection'):
            with self.subTest(kind=kind):
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], setup=setup_factory(kind))
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_malformed_saved_evidence_is_refused_without_a_server_error(self):
        for field, value in (('entries', None), ('roster', [1, 2])):
            with self.subTest(field=field):
                def setup(tmp):
                    path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
                    checkpoint['failed'][field] = (
                        [value] + checkpoint['failed']['entries'][1:] if field == 'entries' else value)
                    Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_cast_replaced_between_hash_and_load_confers_no_exemption(self):
        source = _long_source(ATTESTED)
        cast = json.dumps([{"name": "HOLMES", "aliases": ["SHERLOCK"]}]).encode('utf-8')
        substituted = json.dumps([{"name": "HOLMES", "aliases": ["FUTURE_ME"]}]).encode('utf-8')
        def setup(tmp):
            Path(os.path.join(tmp, 'script_run_cast.json')).write_bytes(substituted)
        with patch.object(script_module, 'get_file_sha256',
                          return_value=hashlib.sha256(cast).hexdigest()):
            response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                {"n": 0, "speaker": "SHERLOCK"},
                {"n": 1, "speaker": "NARRATOR"},
                {"n": 2, "speaker": "WATSON"}],
                writer=self._fingerprinted_writer(source, cast=cast), setup=setup)
        self.assertEqual(422, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_missing_full_segmentation_is_refused_before_publication(self):
        source = _long_source(ATTESTED)
        writer = self._fingerprinted_writer(source)
        def setup(tmp):
            path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
            checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
            checkpoint.pop('segmented')
            Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_trailing_segmentation_after_batch_is_refused_before_publication(self):
        source = _long_source(ATTESTED)
        writer = self._fingerprinted_writer(source)
        def setup(tmp):
            path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
            checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
            checkpoint['segmented'].append({'type': 'NARRATOR', 'text': 'Trailing narration.'})
            checkpoint['named'].append(None)
            checkpoint['segmented'] = checkpoint['segmented'][:-1]
            Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_changed_book_generation_is_refused_by_inject(self):
        source = _long_source(ATTESTED)
        writer = self._fingerprinted_writer(source)
        def setup(tmp):
            path = os.path.join(tmp, 'state.json')
            state = json.loads(Path(path).read_text(encoding='utf-8'))
            state['book_generation'] = 'another-generation'
            Path(path).write_text(json.dumps(state), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_replaced_book_token_is_refused_by_inject(self):
        source = _long_source(ATTESTED)
        writer = self._fingerprinted_writer(source)
        def setup(tmp):
            path = os.path.join(tmp, 'state.json')
            state = json.loads(Path(path).read_text(encoding='utf-8'))
            state['script_generation_selection']['book_token'] = 'x'
            Path(path).write_text(json.dumps(state), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_truncated_segmentation_is_refused_even_when_indices_are_in_range(self):
        source = _long_source(ATTESTED)
        writer = self._fingerprinted_writer(source)
        def setup(tmp):
            path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
            checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
            checkpoint['segmented'] = checkpoint['segmented'][:-1]
            Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_occupied_spoken_row_is_refused_but_writer_narration_prefill_is_allowed(self):
        source = _long_source(ATTESTED)
        for occupied, expected in (({"speaker": "SOMEONE", "text": "Where is he?"}, 409),
                                   ({"speaker": "NARRATOR", "text": "said Holmes."}, 200)):
            with self.subTest(expected=expected):
                writer = self._fingerprinted_writer(source)
                def setup(tmp):
                    path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
                    checkpoint['named'][1 if expected == 409 else 2] = occupied
                    Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
                    mpath = three_pass_manifest_path(os.path.join(tmp, 'annotated_script.json'))
                    manifest = json.loads(Path(mpath).read_text(encoding='utf-8'))
                    manifest['progress']['attributed_entries'] = sum(
                        isinstance(row, dict) for row in checkpoint['named'])
                    Path(mpath).write_text(json.dumps(manifest), encoding='utf-8')
                response, _, _, _, _ = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(expected, response.status_code)

    def test_cast_added_after_the_failed_batch_cannot_exempt_an_invented_name(self):
        source = _long_source(ATTESTED)
        writer = self._fingerprinted_writer(source)
        def setup(tmp):
            cast = json.dumps([{"name": "HOLMES", "aliases": ["FUTURE_ME"]}]).encode('utf-8')
            Path(os.path.join(tmp, 'script_run_cast.json')).write_bytes(cast)
            path = os.path.join(tmp, 'state.json')
            state = json.loads(Path(path).read_text(encoding='utf-8'))
            state['script_generation_options']['cast_sha256'] = hashlib.sha256(cast).hexdigest()
            Path(path).write_text(json.dumps(state), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "FUTURE_ME"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_substituted_cast_cannot_exempt_a_name_not_bound_by_the_writer(self):
        source = _long_source(ATTESTED)
        bound = json.dumps([{"name": "HOLMES", "aliases": ["SHERLOCK"]}]).encode('utf-8')
        substituted = json.dumps([{"name": "HOLMES", "aliases": ["FUTURE_ME"]}]).encode('utf-8')
        def setup(tmp):
            Path(os.path.join(tmp, 'script_run_cast.json')).write_bytes(substituted)
            path = os.path.join(tmp, 'state.json')
            state = json.loads(Path(path).read_text(encoding='utf-8'))
            state['script_generation_options']['cast_sha256'] = hashlib.sha256(substituted).hexdigest()
            Path(path).write_text(json.dumps(state), encoding='utf-8')
        response, _, _, _, _ = self._inject_http([
            {"n": 0, "speaker": "FUTURE_ME"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}],
            writer=self._fingerprinted_writer(source, cast=bound), setup=setup)
        self.assertEqual(409, response.status_code)

    def test_incomplete_selection_with_changed_stripped_matter_fails_closed(self):
        source = FRONT_MATTER + _long_source(ATTESTED)
        entries = [{"type": "SPOKEN", "text": "Where is he?"},
                   {"type": "NARRATOR", "text": "said Holmes."},
                   {"type": "SPOKEN", "text": "Gone,"}]
        def write(tmp):
            return _write_fingerprinted_attribute_run(
                tmp, source, entries, strip_front_matter=True)
        def setup(tmp):
            book = os.path.join(tmp, 'book.txt')
            Path(book).write_text(source.replace('Translator introduction.', 'Revised introduction.'),
                                  encoding='utf-8')
            path = os.path.join(tmp, 'state.json')
            state = json.loads(Path(path).read_text(encoding='utf-8'))
            state['script_generation_selection'].pop('source_sha256')
            Path(path).write_text(json.dumps(state), encoding='utf-8')
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], writer=write, setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_malformed_indices_are_refused_before_failed_evidence_is_retired(self):
        for indices in ([], [1, 1, 2], [1, 2, 9], [1, 2], [1, 2, True]):
            with self.subTest(indices=indices):
                writer = self._fingerprinted_writer(_long_source(ATTESTED))
                def setup(tmp):
                    path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
                    checkpoint['failed']['indices'] = indices
                    Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    def test_explicit_legacy_shape_without_indices_maps_the_empty_prefix(self):
        for named in ([], [None, None, None]):
            with self.subTest(named=named):
                def setup(tmp):
                    path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
                    checkpoint['failed'].pop('indices')
                    checkpoint['segmented'] = list(checkpoint['failed']['entries'])
                    checkpoint['named'] = named
                    Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
                response, _, checkpoint_after, _, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], setup=setup)
                self.assertEqual(200, response.status_code)
                self.assertEqual([0, 1, 2], response.json()['batch_indices'])
                self.assertEqual([0, 1, 2],
                                 json.loads(manifest_after)['recovered_units'][0]['batch_indices'])
                self.assertEqual(['HOLMES', 'NARRATOR', 'WATSON'],
                                 [row['speaker'] for row in json.loads(checkpoint_after)['named']])

    def test_writer_deterministic_narration_survives_mixed_batch_injection(self):
        source = _long_source(['Mira waited by the river.', 'Mira looked at the door.',
                               'Holmes watched the window.', 'Holmes turned away.'])
        entries = [{"type": "SPOKEN", "text": "Mira waited."},
                   {"type": "SPOKEN", "text": "..."},
                   {"type": "SPOKEN", "text": "Is it time?"}]
        def write(tmp):
            path = _write_fingerprinted_attribute_run(
                tmp, source, entries, roster=['MIRA', 'HOLMES'])
            checkpoint_path = three_pass_checkpoint_path(path)
            checkpoint = json.loads(Path(checkpoint_path).read_text(encoding='utf-8'))
            checkpoint['named'][2] = {'speaker': 'NARRATOR', 'text': '...'}
            Path(checkpoint_path).write_text(json.dumps(checkpoint), encoding='utf-8')
            manifest_path = three_pass_manifest_path(path)
            manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
            manifest['progress']['attributed_entries'] = sum(
                isinstance(row, dict) for row in checkpoint['named'])
            Path(manifest_path).write_text(json.dumps(manifest), encoding='utf-8')
            return path
        response, _, checkpoint_after, _, _ = self._inject_http([
            {"n": 0, "speaker": "MIRA"},
            {"n": 1, "speaker": "UNKNOWN"},
            {"n": 2, "speaker": "HOLMES"}], writer=write)
        self.assertEqual(200, response.status_code)
        self.assertEqual(['NARRATOR', 'MIRA', 'NARRATOR', 'HOLMES'],
                         [row['speaker'] for row in json.loads(checkpoint_after)['named'][:4]])

    def test_nonregular_bound_cast_is_neither_a_crash_nor_an_exemption(self):
        source = _long_source(ATTESTED)
        cast = json.dumps([{"name": "HOLMES", "aliases": ["SHERLOCK"]}]).encode('utf-8')
        for kind, expected in (('directory', 409), ('regular', 200)):
            with self.subTest(kind=kind):
                def setup(tmp):
                    path = os.path.join(tmp, 'script_run_cast.json')
                    if kind == 'directory':
                        Path(path).unlink(missing_ok=True)
                        os.makedirs(path, exist_ok=True)
                    else:
                        Path(path).write_bytes(cast)
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "SHERLOCK"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}],
                    writer=self._fingerprinted_writer(source, cast=cast), setup=setup)
                self.assertEqual(expected, response.status_code)
                if expected >= 400:
                    self.assertEqual(checkpoint_before, checkpoint_after)
                    self.assertEqual(manifest_before, manifest_after)
                else:
                    self.assertEqual('HOLMES',
                                     json.loads(checkpoint_after)['named'][1]['speaker'])

    def test_absent_or_malformed_segmentation_cannot_overwrite_an_occupied_row(self):
        for value in ('absent', {'type': 'SPOKEN'}):
            with self.subTest(value=value):
                writer = self._write_legacy_writer_with_occupied_row()
                def setup(tmp):
                    path = three_pass_checkpoint_path(os.path.join(tmp, 'annotated_script.json'))
                    checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
                    if value == 'absent':
                        checkpoint.pop('segmented')
                    else:
                        checkpoint['segmented'] = value
                    Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
                response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
                    {"n": 0, "speaker": "HOLMES"},
                    {"n": 1, "speaker": "NARRATOR"},
                    {"n": 2, "speaker": "WATSON"}], writer=writer, setup=setup)
                self.assertEqual(409, response.status_code)
                self.assertEqual(checkpoint_before, checkpoint_after)
                self.assertEqual(manifest_before, manifest_after)

    @staticmethod
    def _write_legacy_writer_with_occupied_row():
        def write(tmp):
            script_path = _write_failed_attribute_run(tmp)
            path = three_pass_checkpoint_path(script_path)
            checkpoint = json.loads(Path(path).read_text(encoding='utf-8'))
            checkpoint['named'][1] = {'speaker': 'WATSON', 'text': 'Where is he?'}
            Path(path).write_text(json.dumps(checkpoint), encoding='utf-8')
            return script_path
        return write

    def test_parenthetical_alias_echo_cannot_persist_an_unvalidated_label(self):
        response, _, checkpoint_after, _, _ = self._inject_http([
            {"n": 0, "speaker": "HOLMES (also: FUTURE_ME)"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}])
        self.assertEqual(200, response.status_code)
        named = json.loads(checkpoint_after)['named']
        self.assertEqual('HOLMES', named[1]['speaker'])
        self.assertNotIn('FUTURE_ME', json.dumps(named))

    def test_attribute_inject_without_recoverable_source_is_refused(self):
        def setup(tmp):
            checkpoint_path = three_pass_checkpoint_path(
                os.path.join(tmp, "annotated_script.json"))
            with open(checkpoint_path, "rb") as handle:
                checkpoint = json.loads(handle.read())
            checkpoint["segmented"] = []
            checkpoint["failed"]["entries"] = [{"type": "SPOKEN", "text": ""}]
            with open(checkpoint_path, "w", encoding="utf-8") as handle:
                json.dump(checkpoint, handle)
        response, checkpoint_before, checkpoint_after, manifest_before, manifest_after = self._inject_http([
            {"n": 0, "speaker": "HOLMES"},
            {"n": 1, "speaker": "NARRATOR"},
            {"n": 2, "speaker": "WATSON"}], setup=setup)
        self.assertEqual(409, response.status_code)
        self.assertEqual(checkpoint_before, checkpoint_after)
        self.assertEqual(manifest_before, manifest_after)

    def test_old_instruct_checkpoint_inject_keeps_direction_less_rows(self):
        request = script_module.InjectSegmentationRequest(entries=[
            {"n": 0, "instruct": "Calm, measured."},
            {"n": 1, "instruct": "Soft, resigned."}])
        result, checkpoint = self._run(script_module.generate_script_inject, request,
                                       writer=_instruct_writer(None))
        self.assertTrue(result["accepted"])
        self.assertEqual("instruct", checkpoint["stage"])
        rows = checkpoint["annotated"][1:3]
        self.assertEqual(["Calm, measured.", "Soft, resigned."], [e["instruct"] for e in rows])
        self.assertTrue(all("direction" not in e for e in rows))

    def test_gaps_inject_requires_gap_and_feeds_gap_pauses(self):
        from three_pass_generate import apply_gap_pauses
        no_gap = script_module.InjectSegmentationRequest(entries=[
            {"n": 0, "instruct": "Calm, measured."},
            {"n": 1, "instruct": "Soft, resigned."}])
        exc, checkpoint = self._run(script_module.generate_script_inject, no_gap,
                                    writer=_instruct_writer("gaps"))
        self.assertEqual(422, exc.status_code)
        self.assertEqual(["invalid_gap", "invalid_gap"],
                         [f["code"] for f in exc.detail["findings"]])
        self.assertEqual("instruct_failed", checkpoint["stage"])
        with_gap = script_module.InjectSegmentationRequest(entries=[
            {"n": 0, "instruct": "Calm, measured.", "gap": "normal"},
            {"n": 1, "instruct": "Soft, resigned.", "gap": "beat"}])
        result, checkpoint = self._run(script_module.generate_script_inject, with_gap,
                                       writer=_instruct_writer("gaps"))
        self.assertTrue(result["accepted"])
        rows = checkpoint["annotated"][1:3]
        self.assertEqual({"gap": "normal"}, rows[0]["direction"])
        self.assertEqual({"gap": "beat"}, rows[1]["direction"])
        paused = apply_gap_pauses(checkpoint["annotated"])
        self.assertEqual(900, paused[1]["pause_after"])
        self.assertNotIn("pause_after", paused[2])

    def test_directed_inject_requires_action_and_delivery_and_stores_composed_row(self):
        bare = script_module.InjectSegmentationRequest(entries=[
            {"n": 0, "instruct": "Calm, measured.", "gap": "normal", "answers": None},
            {"n": 1, "instruct": "Soft, resigned.", "gap": "normal", "answers": None}])
        exc, checkpoint = self._run(script_module.generate_script_inject, bare,
                                    writer=_instruct_writer("directed"))
        self.assertEqual(422, exc.status_code)
        self.assertEqual(["missing_action", "missing_delivery"] * 2,
                         [f["code"] for f in exc.detail["findings"]])
        self.assertEqual("instruct_failed", checkpoint["stage"])
        partial = script_module.InjectSegmentationRequest(entries=[
            {"n": 0, "gap": "normal", "action": "reassure", "answers": None},
            {"n": 1, "gap": "beat", "action": "answer", "answers": 0}])
        exc, _ = self._run(script_module.generate_script_inject, partial,
                           writer=_instruct_writer("directed"))
        self.assertEqual(422, exc.status_code)
        self.assertEqual(["missing_delivery", "missing_delivery"],
                         [f["code"] for f in exc.detail["findings"]])
        complete = script_module.InjectSegmentationRequest(entries=[
            {"n": 0, "gap": "normal", "action": "reassure", "delivery": "soft, warm", "answers": None},
            {"n": 1, "gap": "beat", "action": "answer", "delivery": "quiet, certain", "answers": 0}])
        result, checkpoint = self._run(script_module.generate_script_inject, complete,
                                       writer=_instruct_writer("directed"))
        self.assertTrue(result["accepted"])
        rows = checkpoint["annotated"][1:3]
        self.assertEqual(["Reassure: soft, warm.", "Answer: quiet, certain."],
                         [e["instruct"] for e in rows])
        self.assertEqual([{"answers": None, "action": "reassure", "delivery": "soft, warm",
                           "gap": "normal"},
                          {"answers": 0, "action": "answer", "delivery": "quiet, certain",
                           "gap": "beat"}], [e["direction"] for e in rows])

    def test_skip_on_a_gaps_checkpoint_still_stores_fallback_rows(self):
        result, checkpoint = self._run(script_module.generate_script_skip,
                                       script_module.SkipChunkRequest(),
                                       writer=_instruct_writer("gaps"))
        self.assertEqual("narrated_as_is", result["resolution"])
        rows = checkpoint["annotated"][1:3]
        self.assertEqual([script_module.default_instruct({"speaker": "HOLMES"}),
                          script_module.default_instruct({"speaker": "WATSON"})],
                         [e["instruct"] for e in rows])
        self.assertTrue(all("direction" not in e for e in rows))

    def test_instruct_detail_uses_the_failed_runs_delivery_direction(self):
        from default_prompts import load_instruct_prompts
        detail, _ = self._run(script_module.generate_script_recovery_detail,
                              writer=_instruct_writer("gaps"))
        self.assertEqual("instruct", detail["failed_pass"])
        self.assertEqual(load_instruct_prompts("gaps")[0], detail["prompt"]["system"])
        self.assertNotEqual(load_instruct_prompts("off")[0], detail["prompt"]["system"])

    def test_failed_instruct_checkpoint_persists_the_runs_delivery_direction(self):
        from tests.test_three_pass_generate import (_client_returning,
                                                    _load_orchestration_fixture_cast)
        import three_pass_generate as three_pass
        segmented = [{"type": "SPOKEN", "text": "Are you leaving already?"},
                     {"type": "SPOKEN", "text": "Then go."}]
        named = [{"n": 0, "speaker": "ANNA"}, {"n": 1, "speaker": "ANNA"}]
        source = '"Are you leaving already?" "Then go."'

        def unavailable(client, model_name, batch, params, **kwargs):
            kwargs["exhaustion_sink"].append(True)
            kwargs["attempt_observer"]({"pass": "instruct", "attempt": 1,
                                        "outcome": "api_error", "error_category": "connection_error",
                                        "http_status": None, "error": "synthetic offline",
                                        "finish_reason": None})
            return []

        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "annotated_script.json")
            client = _client_returning([segmented, named])
            with redirect_stdout(io.StringIO()), \
                    patch.object(three_pass, "instruct_batch", side_effect=unavailable):
                with self.assertRaisesRegex(RuntimeError, "instruct LLM unavailable"):
                    three_pass.run_three_pass(
                        client, "m", source,
                        script_module.LLMGenParams(max_tokens=500, delivery_direction="gaps"),
                        chunk_size=6000,
                        cast=_load_orchestration_fixture_cast(("ANNA",)),
                        output_path=output)
            checkpoint = json.loads(Path(three_pass.three_pass_checkpoint_path(output)).read_text())
        self.assertEqual("instruct_failed", checkpoint["stage"])
        self.assertEqual("gaps", checkpoint["failed"]["delivery_direction"])

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
            options = {'strip_front_matter': False, 'literal_source_preserving': False, 'first_person_narrator': 'Watson', 'start_over': True}
            response = client.post('/api/generate_script', json=options)
            self.assertEqual(200, response.status_code, response.text)
            dispatch.assert_called_once()
            tasks, input_file, request = dispatch.call_args.args
            self.assertIsInstance(tasks, script_module.BackgroundTasks)
            self.assertEqual(str(book), input_file)
            self.assertEqual(options, request.model_dump(exclude_none=True))
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
                result = Path(self._write_report(tmp))
            self.assertNotEqual(path, result)
            self.assertEqual(1, len(published))
            self.assertEqual(published[0], result.read_bytes())
            self.assertEqual(original, path.read_bytes())
            self.assertEqual({path.name, result.name}, {item.name for item in Path(tmp).iterdir()})

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


if __name__ == "__main__":
    unittest.main()
