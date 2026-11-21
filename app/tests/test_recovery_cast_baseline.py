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
from routers import script
from script_recovery_guard import get_stale_recovery_state, is_script_recovery_cast_invalidated
from tests.test_script_recovery import SOURCE, _write_failed_run
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
