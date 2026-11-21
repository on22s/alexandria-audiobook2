"""Stale-Retry fencing with null, absent and proven cast baselines."""
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import book_state_transaction as books
import core
from generate_script import LLMGenParams
from routers import script
from script_recovery_guard import (RECOVERY_CAST_INVALIDATED, RECOVERY_CAST_INVALIDATED_DETAIL,
                                   get_stale_recovery_state, is_script_recovery_cast_invalidated)
from tests.test_script_recovery import SOURCE, _write_failed_run
from three_pass_generate import (_save_three_pass_checkpoint, run_three_pass,
                                  three_pass_checkpoint_path, three_pass_fingerprint,
                                  three_pass_manifest_path)
from utils import atomic_json_write


class RecoveryCastBaselineTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, cast_present=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_path = Path(_write_failed_run(tmp))
            source = root / 'book.txt'
            source.write_text(SOURCE, encoding='utf-8')
            state = json.loads((root / 'state.json').read_text())
            state['input_file_path'] = str(source)
            state['script_generation_input_file'] = str(source)
            state.pop('script_generation_selection', None)
            state['script_generation_options'] = {'strip_front_matter': True}
            atomic_json_write(state, root / 'state.json')
            scheduled = []
            patches = [
                patch.object(script, 'DATA_DIR', tmp),
                patch.object(script, 'SCRIPT_PATH', str(script_path)),
                patch.object(script, 'CONFIG_PATH', str(root / 'config.json')),
                patch.object(script, 'check_global_gpu_lock'),
                patch.object(script, 'get_active_reasoning_effort', return_value=None),
                patch.object(script, 'reserve_background_task', return_value='baseline-claim'),
                patch.object(script, 'register_claimed_background_task',
                    side_effect=lambda _t, _n, _c, _r, command, *_: scheduled.append(command)),
                patch.object(core, 'process_state', {}),
                patch.object(core, '_task_claims', {}),
                patch.object(core, '_gpu_leases', {}),
            ]
            for item in patches:
                item.start()
            try:
                yield root, script_path, source, scheduled
            finally:
                for item in reversed(patches):
                    item.stop()

    def _legacy_request(self, root):
        checkpoint = script.get_current_generation_checkpoint()
        return script.GenerateScriptRequest(
            legacy_checkpoint_token=script.get_script_recovery_token(checkpoint),
            legacy_book_token=books.get_book_operation_token(str(root)))

    def _attempt_generate(self, source):
        return script.start_script_generation(script.BackgroundTasks(), str(source), None)

    def _set_cast(self, root, value):
        state = json.loads((root / 'state.json').read_text())
        options = state['script_generation_options']
        if value is None:
            options['cast_sha256'] = None
        else:
            options['cast_sha256'] = value
        atomic_json_write(state, root / 'state.json')

    def _fence(self, root, digest):
        flagged = get_stale_recovery_state(str(root), digest)
        self.assertIsNotNone(flagged)
        atomic_json_write(flagged, root / 'state.json')
        return flagged

    def _attempt_retry(self, root, source):
        return script.start_script_generation(script.BackgroundTasks(), str(source),
                                              self._legacy_request(root), require_recovery=True)

    def test_null_baseline_fails_closed_with_valid_legacy_tokens(self):
        with self.fixture() as (root, script_path, source, scheduled):
            self._set_cast(root, None)
            pair_before = {path: path.read_bytes() for path in (
                Path(script.three_pass_checkpoint_path(str(script_path))),
                Path(script.three_pass_manifest_path(str(script_path))))}
            flagged = self._fence(root, 'a' * 64)
            self.assertIsNone(flagged['script_generation_roster_cast_invalidated']['run_cast_sha256'])
            with self.assertRaises(HTTPException) as caught:
                self._attempt_retry(root, source)
            self.assertEqual(409, caught.exception.status_code)
            self.assertIn('approved cast changed', caught.exception.detail)
            self.assertEqual([], scheduled)
            self.assertTrue(is_script_recovery_cast_invalidated(str(root)))
            self.assertEqual(pair_before, {path: path.read_bytes() for path in pair_before})

    def test_absent_baseline_fails_closed_with_valid_legacy_tokens(self):
        with self.fixture() as (root, script_path, source, scheduled):
            flagged = self._fence(root, 'b' * 64)
            self.assertIsNone(flagged['script_generation_roster_cast_invalidated']['run_cast_sha256'])
            before = (root / 'state.json').read_bytes()
            with self.assertRaises(HTTPException) as caught:
                self._attempt_retry(root, source)
            self.assertEqual(409, caught.exception.status_code)
            self.assertEqual([], scheduled)
            self.assertEqual(before, (root / 'state.json').read_bytes())

    def test_castless_approved_cast_is_not_a_change(self):
        with self.fixture() as (root, _script_path, _source, _scheduled):
            self._set_cast(root, None)
            self.assertIsNone(get_stale_recovery_state(str(root), None))

    def test_proven_unchanged_cast_keeps_a_legitimate_retry(self):
        with self.fixture() as (root, _script_path, source, scheduled):
            raw = b'{"cast": []}'
            Path(script.get_run_cast_path()).write_bytes(raw)
            self._set_cast(root, hashlib.sha256(raw).hexdigest())
            self.assertIsNone(get_stale_recovery_state(str(root), hashlib.sha256(raw).hexdigest()))
            result = self._attempt_retry(root, source)
            self.assertEqual({'status': 'resuming'}, result)
            self.assertEqual(1, len(scheduled))

    def _write_worker_first_checkpoint(self, script_path):
        """A real worker pass-1 checkpoint, saved before the first manifest."""
        checkpoint = Path(three_pass_checkpoint_path(str(script_path)))
        manifest = Path(three_pass_manifest_path(str(script_path)))
        checkpoint.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        _save_three_pass_checkpoint(
            str(script_path), three_pass_fingerprint(SOURCE, 'fixture-model', 1000),
            'segment', [{'type': 'NARRATOR', 'text': 'Chapter one.'}], 1, [], [])
        self.assertTrue(checkpoint.is_file())
        self.assertFalse(manifest.exists())
        return checkpoint, manifest

    def test_worker_first_checkpoint_without_manifest_survives_approval_and_undo(self):
        with self.fixture() as (root, script_path, source, scheduled):
            checkpoint, manifest = self._write_worker_first_checkpoint(script_path)
            self._fence(root, 'a' * 64)
            checkpoint_bytes = checkpoint.read_bytes()
            with self.assertRaises(HTTPException) as approved_refusal:
                self._attempt_generate(source)
            self.assertEqual(409, approved_refusal.exception.status_code)
            self.assertEqual(RECOVERY_CAST_INVALIDATED_DETAIL, approved_refusal.exception.detail)
            undone = get_stale_recovery_state(str(root), None)
            self.assertIsNotNone(undone)
            atomic_json_write(undone, root / 'state.json')
            self.assertTrue(is_script_recovery_cast_invalidated(str(root)))
            with self.assertRaises(HTTPException) as undo_refusal:
                self._attempt_generate(source)
            self.assertEqual(approved_refusal.exception.detail, undo_refusal.exception.detail)
            self.assertEqual([], scheduled)
            self.assertEqual(checkpoint_bytes, checkpoint.read_bytes())
            self.assertFalse(manifest.exists())

    def test_no_checkpoint_and_different_source_are_not_fenced(self):
        with self.fixture() as (root, script_path, source, scheduled):
            checkpoint, manifest = self._write_worker_first_checkpoint(script_path)
            checkpoint.unlink()
            self.assertIsNone(get_stale_recovery_state(str(root), 'b' * 64))
            fingerprint = three_pass_fingerprint(SOURCE, 'fixture-model', 1000)
            fingerprint['source_sha256'] = 'f' * 64
            _save_three_pass_checkpoint(
                str(script_path), fingerprint, 'segment',
                [{'type': 'NARRATOR', 'text': 'Chapter one.'}], 1, [], [])
            state = json.loads((root / 'state.json').read_text())
            state['script_generation_selection'] = {'source_sha256': 'e' * 64}
            atomic_json_write(state, root / 'state.json')
            self.assertIsNone(get_stale_recovery_state(str(root), 'b' * 64))

    def test_recovery_get_retry_skip_and_inject_all_report_the_fence(self):
        with self.fixture() as (root, script_path, source, scheduled):
            checkpoint = Path(three_pass_checkpoint_path(str(script_path)))
            manifest = Path(three_pass_manifest_path(str(script_path)))
            self._fence(root, 'c' * 64)
            before = {path: path.read_bytes() for path in (checkpoint, manifest)}
            response = script.ensure_script_recovery_response(include_detail=True)
            self.assertFalse(response['recoverable'])
            self.assertTrue(response['start_over_required'])
            self.assertEqual(RECOVERY_CAST_INVALIDATED_DETAIL, response['detail'])
            for act in (lambda: script.apply_manual_recovery([], 'skip'),
                        lambda: script.apply_manual_recovery([], 'inject')):
                with self.assertRaises(HTTPException) as caught:
                    act()
                self.assertEqual(409, caught.exception.status_code)
                self.assertEqual(RECOVERY_CAST_INVALIDATED_DETAIL, caught.exception.detail)
            with self.assertRaises(HTTPException) as caught:
                self._attempt_retry(root, source)
            self.assertEqual(RECOVERY_CAST_INVALIDATED_DETAIL, caught.exception.detail)
            self.assertEqual([], scheduled)
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_normal_recovery_after_start_over_clears_the_fence(self):
        with self.fixture() as (root, _script_path, source, scheduled):
            self._fence(root, 'd' * 64)
            state = json.loads((root / 'state.json').read_text())
            state.pop(RECOVERY_CAST_INVALIDATED, None)
            atomic_json_write(state, root / 'state.json')
            self.assertTrue(script.ensure_script_recovery_response()['recoverable'])
            self.assertEqual({'status': 'resuming'}, self._attempt_retry(root, source))
            self.assertEqual(1, len(scheduled))

    def test_loader_fence_refuses_real_cli_without_publication_or_alteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            output = data_dir / 'annotated_script.json'
            checkpoint, _manifest = self._write_worker_first_checkpoint(output)
            state = {'input_file_path': str(data_dir / 'book.txt'),
                     'script_generation_input_file': str(data_dir / 'book.txt'),
                     'script_generation_roster_cast_invalidated': {'run_cast_sha256': None}}
            atomic_json_write(state, data_dir / 'state.json')
            source_file = data_dir / 'book.txt'
            source_file.write_text(SOURCE, encoding='utf-8')
            before = checkpoint.read_bytes()
            called = []
            from unittest.mock import patch
            import three_pass_generate as three_pass
            with patch.object(three_pass, 'segment_chunk_adaptively',
                              side_effect=lambda *a, **k: called.append(True)):
                with self.assertRaises(RuntimeError) as caught:
                    run_three_pass(object(), 'fixture-model', SOURCE, LLMGenParams(), 1000,
                                   output_path=str(output))
            self.assertEqual(RECOVERY_CAST_INVALIDATED_DETAIL, str(caught.exception))
            self.assertEqual([], called)
            self.assertEqual(before, checkpoint.read_bytes())
            self.assertFalse(Path(three_pass_manifest_path(str(output))).exists())
            self.assertFalse(output.exists())
            from three_pass_generate import _refuse_fenced_active_checkpoint
            batch = data_dir / 'batch.json'
            _refuse_fenced_active_checkpoint(str(batch))
