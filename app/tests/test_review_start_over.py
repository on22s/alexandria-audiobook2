"""Start over retires only review progress after admission and book validation."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import core

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routers import editor, script
from completed_review_receipt import get_review_progress_paths, get_review_receipt_path
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
        self.stack.enter_context(patch.object(script, '_get_review_startup_path', return_value=str(self.root / 'startup.log')))
        app = FastAPI()
        app.add_exception_handler(core.AppError, core.handle_app_error)
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
        self.assertEqual(2, self.reserve.call_count)
        self.assertEqual(2, self.release.call_count)
        self.register.assert_not_called()

    def test_failed_discard_releases_claim_and_does_not_start(self):
        with patch.object(script, 'remove_generation_shard_checkpoint', side_effect=PermissionError('private path')):
            response = self.post()
        self.assertEqual(409, response.status_code, response.text)
        self.assertNotIn('private path', response.text)
        self.release.assert_called_once_with('review', 'claim', pending_only=True)
        self.register.assert_not_called()
        self.assert_preserved()

    def test_registration_refusal_discloses_discard_without_rolling_back(self):
        self.register.side_effect = core.register_claimed_background_task
        for endpoint in ('/api/review_script', '/api/review_script_contextual'):
            with self.subTest(endpoint=endpoint), patch.object(core, '_task_claims', {}), \
                 patch.object(core, '_gpu_leases', {}):
                self.checkpoint.write_text('{}')
                self.receipt.write_text('{}')
                response = self.post(endpoint)
                self.assertEqual(409, response.status_code, response.text)
                self.assertFalse(self.checkpoint.exists())
                self.assertFalse(self.receipt.exists())
                self.assert_preserved()
                body = response.json()
                self.assertEqual('error.review.restart_reservation_lost', body.get('message_key'))
                self.assertIn('was discarded', body['detail'])
                self.assertIn('reservation is no longer active', body['detail'])
                self.assertIn('Start Review to begin from batch 1', body['detail'])
        self.assertEqual(2, self.release.call_count)
        self.assertIn('restart_registration_failed', (self.root / 'startup.log').read_text())

    def test_receipt_deletion_failure_discloses_partial_discard(self):
        for endpoint in ('/api/review_script', '/api/review_script_contextual'):
            with self.subTest(endpoint=endpoint), patch(
                    'completed_review_receipt.remove_completed_review_receipt',
                    side_effect=PermissionError('private receipt path')):
                self.checkpoint.write_text('{}')
                receipt_before = self.receipt.read_bytes()
                response = self.post(endpoint)
                self.assertEqual(409, response.status_code, response.text)
                self.assertFalse(self.checkpoint.exists())
                self.assertEqual(receipt_before, self.receipt.read_bytes())
                self.assert_preserved()
                body = response.json()
                self.assertEqual('error.review.restart_receipt_discard_failed', body.get('message_key'))
                self.assertIn('checkpoint was discarded', body['detail'])
                self.assertIn('receipt was kept', body['detail'])
                self.assertIn("new review didn't start", body['detail'])
                self.assertNotIn('private receipt path', response.text)
                self.assertIn('review again even if nothing changed', body['detail'])
        self.assertEqual(2, self.release.call_count)
        self.register.assert_not_called()
        self.assertNotIn('private receipt path', (self.root / 'startup.log').read_text())
        response = self.post(start_over=False, force_review=True)
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn('--force-review', self.register.call_args.args[4])
        self.assertFalse(self.checkpoint.exists())
        self.assertTrue(self.receipt.exists())
        self.assert_preserved()

    def test_unexpected_registration_failure_discloses_discard_without_private_reason(self):
        self.register.side_effect = RuntimeError('private registration reason')
        response = self.post()
        self.assertEqual(500, response.status_code, response.text)
        self.assertEqual('error.review.restart_start_failed', response.json().get('message_key'))
        self.assertIn('was discarded', response.json()['detail'])
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.receipt.exists())
        self.release.assert_called_once_with('review', 'claim', pending_only=True)
        self.assert_preserved()
        self.assertNotIn('private registration reason', response.text)
        self.assertNotIn('private registration reason', (self.root / 'startup.log').read_text())

    def test_checkpoint_cleanup_failure_discloses_possible_partial_discard(self):
        def remove_header_then_fail(*args, **kwargs):
            self.checkpoint.unlink()
            raise PermissionError('private shard path')
        with patch.object(script, 'remove_generation_shard_checkpoint', side_effect=remove_header_then_fail):
            response = self.post()
        self.assertEqual(409, response.status_code, response.text)
        self.assertIn('some progress may already have been discarded', response.json()['detail'])
        self.assertFalse(self.checkpoint.exists())
        self.assertTrue(self.receipt.exists())
        self.register.assert_not_called()
        self.release.assert_called_once_with('review', 'claim', pending_only=True)
        self.assert_preserved()
        self.assertNotIn('private shard path', response.text)

    def test_resume_listing_and_discard_share_progress_paths(self):
        import review_script
        checkpoint_path, receipt_path = get_review_progress_paths(str(self.path))
        self.assertEqual(str(self.checkpoint), checkpoint_path)
        self.assertEqual(str(self.receipt), receipt_path)
        self.assertEqual(checkpoint_path, review_script._checkpoint_path(str(self.path)))
        self.assertEqual(receipt_path, get_review_receipt_path(str(self.path)))
        saved = self.root / 'scripts'
        saved.mkdir()
        saved_checkpoint = Path(get_review_progress_paths(str(saved / 'saved.json'))[0])
        saved_checkpoint.write_text('{"completed_batches": 1}')
        self.checkpoint.write_text('{"completed_batches": 1}')
        with patch.object(editor, 'SCRIPT_PATH', str(self.path)), \
             patch.object(editor, 'SCRIPTS_DIR', str(saved)):
            self.assertCountEqual(['(active script)', 'saved'],
                                  [row['book'] for row in editor._get_review_checkpoint_rows()])
            response = self.post()
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(['saved'], [row['book'] for row in editor._get_review_checkpoint_rows()])
        self.assertFalse(Path(receipt_path).exists())
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
        self.register.assert_not_called()
        self.release.assert_called_once_with('batch_review', 'claim', pending_only=True)

    def test_busy_single_modes_refuse_before_waiting_for_book_state(self):
        from book_state_transaction import ensure_book_state
        self.admission.side_effect = core.check_global_gpu_lock
        self.reserve.side_effect = core.reserve_background_task
        entered, release = threading.Event(), threading.Event()
        errors = []
        def hold_book():
            try:
                with ensure_book_state(self.root):
                    entered.set()
                    if not release.wait(15):
                        raise AssertionError('book guard not released')
            except BaseException as error:
                errors.append(error)
        holder = threading.Thread(target=hold_book)
        holder.start()
        try:
            self.assertTrue(entered.wait(2))
            with patch.object(core, 'process_state', {'review': {'running': True}}), \
                 patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}), \
                 patch.object(script, '_require_published_script', side_effect=REQUIRE_PUBLISHED_SCRIPT):
                for endpoint in ('/api/review_script', '/api/review_script_contextual'):
                    for start_over in (False, True):
                        with self.subTest(endpoint=endpoint, start_over=start_over):
                            refused = threading.Event()
                            def start():
                                try:
                                    response = self.post(endpoint, start_over=start_over)
                                    self.assertEqual(400, response.status_code, response.text)
                                except BaseException as error:
                                    errors.append(error)
                                finally:
                                    refused.set()
                            worker = threading.Thread(target=start)
                            worker.start()
                            try:
                                self.assertTrue(refused.wait(2), 'busy review must not wait for book file I/O')
                            finally:
                                if not refused.is_set():
                                    release.set()
                                worker.join(5)
                            self.assertFalse(worker.is_alive())
        finally:
            release.set()
            holder.join(5)
        self.assertFalse(holder.is_alive())
        self.assertEqual([], errors)
        self.register.assert_not_called()
        self.assertTrue(self.checkpoint.exists())
        self.assertTrue(self.receipt.exists())
        self.assert_preserved()

    def test_publication_recheck_failure_releases_real_single_reservation(self):
        self.reserve.side_effect = core.reserve_background_task
        self.release.side_effect = core.release_gpu_task_claim
        def refuse():
            self.assertEqual('pending', core._task_claims['review']['phase'])
            raise HTTPException(409, 'Publication changed')
        states = {'review': {'running': False}}
        with patch.object(core, 'DATA_DIR', str(self.root)), \
             patch.object(core, 'process_state', states), patch.object(core, '_task_claims', {}), \
             patch.object(core, '_gpu_leases', {}), patch.object(core, 'llm_is_on_this_gpu', return_value=False), \
             patch.object(script, '_require_published_script', side_effect=refuse):
            for endpoint in ('/api/review_script', '/api/review_script_contextual'):
                response = self.post(endpoint)
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual({}, core._task_claims)
                self.assertFalse(states['review']['running'])
        self.register.assert_not_called()
        self.assertTrue(self.checkpoint.exists())
        self.assertTrue(self.receipt.exists())
        self.assert_preserved()

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
        self.reserve.side_effect = core.reserve_background_task
        self.register.side_effect = core.register_claimed_background_task
        self.release.side_effect = core.release_gpu_task_claim
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
