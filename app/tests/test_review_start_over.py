"""Start over retires only review progress after admission and book validation."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import core

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routers import script
from completed_review_receipt import get_review_receipt_path
from tests import test_llm_review_regressions as fixtures

REQUIRE_PUBLISHED_SCRIPT = script._require_published_script


class ReviewStartOverTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / 'annotated_script.json'
        self.rows = [{'speaker': 'ANN', 'text': text, 'instruct': 'Plain.'}
                     for text in ('Current accepted edit.', 'Second line.')]
        self.path.write_text(json.dumps(self.rows), encoding='utf-8')
        self.source = self.root / 'source.txt'
        self.source.write_text('Original source words.', encoding='utf-8')
        self.checkpoint = Path(str(self.path) + '.review_checkpoint.json')
        self.checkpoint.write_text('{}', encoding='utf-8')
        self.receipt = Path(get_review_receipt_path(str(self.path)))
        self.receipt.write_text('{}', encoding='utf-8')
        self.before = (self.path.read_bytes(), self.source.read_bytes())
        self.token = 'a' * 64
        self.stack.enter_context(patch.object(script, 'DATA_DIR', str(self.root)))
        self.stack.enter_context(patch.object(script, 'SCRIPT_PATH', str(self.path)))
        self.stack.enter_context(patch.object(script, '_require_published_script'))
        self.stack.enter_context(patch.object(script, 'load_app_config', return_value={}))
        self.stack.enter_context(patch('book_state_transaction.get_book_operation_token', return_value=self.token))
        self.admission = self.stack.enter_context(patch.object(script, 'check_global_gpu_lock'))
        self.reserve = self.stack.enter_context(patch.object(script, 'reserve_background_task', return_value='claim'))
        self.register = self.stack.enter_context(patch.object(script, 'register_claimed_background_task'))
        self.release = self.stack.enter_context(patch.object(script, 'release_gpu_task_claim'))
        self.schedule = self.stack.enter_context(patch.object(script, 'schedule_claimed_background_task'))
        self.stack.enter_context(patch.object(script, '_get_review_startup_path', return_value=str(self.root / 'startup.log')))
        app = FastAPI()
        app.include_router(script.router)
        self.client = self.stack.enter_context(TestClient(app))

    def post(self, endpoint='/api/review_script', **extra):
        return self.client.post(endpoint, json={'start_over': True, 'book_token': self.token,
                                                'dedupe_speakers': False, **extra})

    def assert_preserved(self):
        self.assertEqual(self.before, (self.path.read_bytes(), self.source.read_bytes()))

    def test_standard_and_contextual_retire_progress_and_receipt_without_editing_inputs(self):
        for endpoint in ('/api/review_script', '/api/review_script_contextual'):
            with self.subTest(endpoint=endpoint):
                self.checkpoint.write_text('{}')
                self.receipt.write_text('{}')
                response = self.post(endpoint)
                self.assertEqual(200, response.status_code, response.text)
                self.assertFalse(self.checkpoint.exists())
                self.assertFalse(self.receipt.exists())
                self.assert_preserved()
                self.assertIn('--force-review', self.register.call_args.args[4])
        self.assertIn('checkpoint_discarded', (self.root / 'startup.log').read_text())

    def test_busy_admission_preserves_everything(self):
        for gate in (self.admission, self.reserve):
            with self.subTest(gate=gate):
                gate.side_effect = HTTPException(409, 'Review is running')
                response = self.post()
                self.assertEqual(409, response.status_code)
                self.assertTrue(self.checkpoint.exists())
                self.assertTrue(self.receipt.exists())
                self.assert_preserved()
                gate.side_effect = None
        self.register.assert_not_called()

    def test_changed_book_and_missing_confirmation_token_refuse_without_discard(self):
        for token in ('b' * 64, None):
            with self.subTest(token=token):
                response = self.post(book_token=token)
                self.assertEqual(409, response.status_code, response.text)
                self.assertTrue(self.checkpoint.exists())
                self.assertTrue(self.receipt.exists())
                self.assert_preserved()
        self.reserve.assert_not_called()

    def test_failed_discard_releases_claim_and_does_not_start(self):
        with patch.object(script, 'remove_generation_shard_checkpoint', side_effect=PermissionError('private path')):
            response = self.post()
        self.assertEqual(409, response.status_code, response.text)
        self.assertNotIn('private path', response.text)
        self.release.assert_called_once_with('review', 'claim', pending_only=True)
        self.register.assert_not_called()
        self.assert_preserved()

    def test_next_native_review_starts_with_first_current_entry(self):
        runner = fixtures.ReviewVramTests()
        calls = []
        def review(client, model, entries, *args, **kwargs):
            calls.append(entries)
            return entries
        self.checkpoint.unlink()
        self.receipt.unlink()
        runner.run_review(self.path, [True, False], review, lambda *args, **kwargs: ({}, 0, []))
        self.assertTrue(self.checkpoint.exists(), 'fixture must leave real partial progress')
        self.assertEqual(1, len(calls))
        self.before = (self.path.read_bytes(), self.source.read_bytes())
        response = self.post()
        self.assertEqual(200, response.status_code, response.text)
        self.assert_preserved()
        calls.clear()
        runner.run_review(self.path, [True] * 5, review, lambda *args, **kwargs: ({}, 0, []))
        self.assertEqual(self.rows[0]['text'], calls[0][0]['text'])
        self.assertEqual(2, len(calls))

    def test_batch_start_over_requires_current_book_before_scheduling(self):
        response = self.post('/api/review_script/batch/start', script_names=['saved'], book_token='b' * 64)
        self.assertEqual(409, response.status_code, response.text)
        self.schedule.assert_not_called()

    def test_real_pending_claim_refuses_running_review_before_discard(self):
        self.reserve.side_effect = core.reserve_background_task
        with patch.object(core, 'process_state', {'review': {'running': True}}), \
             patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}):
            response = self.post()
        self.assertEqual(400, response.status_code, response.text)
        self.assertTrue(self.checkpoint.exists())
        self.assertTrue(self.receipt.exists())
        self.register.assert_not_called()
        self.assert_preserved()

    def test_restart_reuses_held_book_guard_for_real_publication_recheck(self):
        with patch.object(script, '_require_published_script', side_effect=REQUIRE_PUBLISHED_SCRIPT):
            response = self.post()
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.receipt.exists())
        self.assert_preserved()

    def test_batch_real_claim_discards_only_selected_review_state_before_worker(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        book = scripts / 'saved.json'
        book.write_bytes(self.path.read_bytes())
        checkpoint = Path(str(book) + '.review_checkpoint.json')
        checkpoint.write_text('{}')
        receipt = Path(get_review_receipt_path(str(book)))
        receipt.write_text('{}')
        unrelated = scripts / 'other.json.review_checkpoint.json'
        unrelated.write_text('unselected progress')
        states = {'batch_review': {'running': False, 'cancel': False}}
        calls = []
        def stream(command, _cwd, current, **kwargs):
            self.assertFalse(checkpoint.exists())
            self.assertFalse(receipt.exists())
            self.assertEqual(self.before[0], book.read_bytes())
            self.assertIn('--force-review', command)
            calls.append(command)
            return 0, ['Review complete: 2 -> 2 entries', 'Total changes: 0']
        self.schedule.side_effect = core.schedule_claimed_background_task
        with ExitStack() as stack:
            for name, value in {'DATA_DIR': str(self.root), 'process_state': states,
                    '_task_claims': {}, '_gpu_leases': {}}.items():
                stack.enter_context(patch.object(core, name, value))
            stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=False))
            for name, value in {'SCRIPTS_DIR': str(scripts), 'REPORTS_DIR': str(self.root / 'reports'),
                    'ROOT_DIR': str(self.root), 'process_state': states}.items():
                stack.enter_context(patch.object(script, name, value))
            stack.enter_context(patch.object(script, '_init_task_log', return_value=str(self.root / 'batch.log')))
            stack.enter_context(patch.object(script, '_stream_subprocess_to_logs', side_effect=stream))
            stack.enter_context(patch.object(script, '_insert_llm_summary', side_effect=lambda lines, *args, **kwargs: lines))
            stack.enter_context(patch.object(script, '_run_claimed_background_task', side_effect=lambda name, work: work()))
            response = self.post('/api/review_script/batch/start', script_names=['saved'], find_nicknames=False)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(calls))
        self.assertEqual('done', states['batch_review']['tasks'][0]['status'])
        self.assertFalse(states['batch_review']['running'])
        self.assertEqual('unselected progress', unrelated.read_text())
        self.assertTrue(self.checkpoint.exists(), 'batch restart must not discard active-book progress')
        self.assertTrue(self.receipt.exists(), 'batch restart must not retire active-book receipt')
        self.assert_preserved()
