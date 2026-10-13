"""Render admission owns its initiating book and exact rows, without inference."""
import asyncio
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
import core
from book_state_transaction import get_book_operation_token
from project import ProjectManager, get_chunk_generation_requests
from routers import editor
from tests import test_saved_book_publication as saved_book_tests


class BookRenderAdmissionTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            pm = ProjectManager(directory)
            pm.save_chunks([{'id': i, 'text': f'Source row {i}.', 'speaker': 'ALICE',
                             'instruct': '', 'status': 'pending'} for i in range(3)])
            (root / 'state.json').write_text(json.dumps({'active_book_id': 'A', 'book_generation': 'one'}))
            (root / 'annotated_script.json').write_text('[{"text":"Source row 0.","speaker":"ALICE"}]')
            states = copy.deepcopy(core.process_state)
            for state in states.values():
                state['running'] = False
            for owner, name, value in ((core, 'DATA_DIR', directory),
                    (core, 'process_state', states), (editor, 'process_state', states),
                    (core, '_task_claims', {}), (core, '_gpu_leases', {}),
                    (editor, 'project_manager', pm), (editor, 'SCRIPT_PATH', str(pm.script_path))):
                stack.enter_context(patch.object(owner, name, value))
            stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
            stack.enter_context(patch.object(editor, 'load_app_config', return_value={}))
            api = FastAPI()
            api.include_router(editor.router)
            try:
                yield root, pm, states, api
            finally:
                for name, claim in list(core._task_claims.items()):
                    core.release_gpu_task_claim(name, claim['id'])

    def test_all_modes_refuse_stale_book_generation_and_row_without_mutation(self):
        for mode in ('single', 'parallel', 'fast'):
            for change in ('book', 'generation', 'row'):
                with self.subTest(mode=mode, change=change), self.fixture() as (root, pm, states, api), TestClient(api) as client:
                    snapshot = client.get('/api/chunks/status').json()
                    token = snapshot['book_token']
                    uid = snapshot['chunks'][1]['uid']
                    if change == 'row':
                        pm.delete_chunk(0)
                    else:
                        (root / 'state.json').write_text(json.dumps({
                            'active_book_id': 'B' if change == 'book' else 'A', 'book_generation': 'two'}))
                    before = pm.load_chunks()
                    engine = Mock()
                    with patch.object(pm, 'get_engine', engine):
                        if mode == 'single':
                            response = client.post('/api/chunks/1/generate', params={
                                'expected_book_token': token, 'expected_uid': uid})
                        else:
                            response = client.post('/api/generate_batch' + ('_fast' if mode == 'fast' else ''), json={
                                'indices': [1], 'expected_book_token': token, 'expected_uids': [uid]})
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(before, pm.load_chunks())
                    engine.assert_not_called()
                    self.assertEqual({}, core._task_claims)
                    self.assertFalse(states['audio']['running'])

    def test_pending_admission_excludes_switch_and_owns_lease_until_worker_finishes(self):
        for mode in ('single', 'parallel', 'fast'):
            with self.subTest(mode=mode), self.fixture() as (root, pm, states, _api):
                token = get_book_operation_token(root)
                uid = pm.load_chunks()[1]['uid']
                checked = []
                def refuse_switch(*args):
                    with self.assertRaises(HTTPException) as refusal:
                        with core.ensure_book_switch_allowed():
                            self.fail('Switch entered while admission held a task lease')
                    self.assertEqual(409, refusal.exception.status_code)
                    checked.append(True)
                    return {} if mode != 'single' else original(*args)
                original = editor.ensure_audio_generation_requests
                helper = 'ensure_audio_generation_requests' if mode == 'single' else 'load_app_config'
                tasks = BackgroundTasks()
                method = 'generate_chunk_audio' if mode == 'single' else 'generate_chunks_' + ('parallel' if mode == 'parallel' else 'batch')
                result = (True, 'fixture') if mode == 'single' else {'completed': [1], 'failed': []}
                with patch.object(editor, helper, side_effect=refuse_switch), patch.object(pm, method, return_value=result) as generation:
                    if mode == 'single':
                        response = asyncio.run(editor.generate_chunk_endpoint(1, tasks, token, uid))
                    else:
                        request = editor.BatchGenerateRequest(indices=[1], expected_book_token=token, expected_uids=[uid])
                        endpoint = editor.generate_batch_endpoint if mode == 'parallel' else editor.generate_batch_fast_endpoint
                        response = asyncio.run(endpoint(request, tasks))
                    self.assertEqual('started', response['status'])
                    self.assertEqual([True], checked)
                    self.assertTrue(states['audio']['running'])
                    self.assertEqual('pending', core._task_claims['audio']['phase'])
                    asyncio.run(tasks())
                    kwargs = generation.call_args.kwargs
                    captured = kwargs['admitted_chunk'] if mode == 'single' else kwargs['admitted_requests'][1]
                    self.assertEqual(uid, captured['uid'])
                    self.assertEqual('Source row 1.', captured['text'])
                self.assertEqual({}, core._task_claims)
                self.assertFalse(states['audio']['running'])
                with core.ensure_book_switch_allowed():
                    pass

    def test_invalid_selection_registration_failure_and_cancellation_release_own_claim(self):
        with self.fixture() as (_root, _pm, states, api), TestClient(api) as client:
            for body in ({'indices': [-1]}, {'indices': [0], 'expected_uids': []},
                         {'indices': [0, 0], 'expected_uids': ['wrong', 'wrong']}):
                self.assertIn(client.post('/api/generate_batch', json=body).status_code, (409, 422))
                self.assertEqual({}, core._task_claims)
                self.assertFalse(states['audio']['running'])
            class RefusedRegistration:
                def add_task(self, *args):
                    raise RuntimeError('registration refused')
            with self.assertRaisesRegex(RuntimeError, 'registration refused'):
                asyncio.run(editor.generate_chunk_endpoint(0, RefusedRegistration()))
            self.assertEqual({}, core._task_claims)
            with patch.object(editor, 'ensure_audio_generation_requests', side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(editor.generate_chunk_endpoint(0, BackgroundTasks()))
            self.assertEqual({}, core._task_claims)

    def test_admitted_uid_and_inputs_are_checked_before_every_engine_mode(self):
        for mode in ('single', 'parallel', 'fast'):
            for change in ('row', 'text'):
                with self.subTest(mode=mode, change=change), self.fixture() as (_root, pm, _states, _api):
                    captured = get_chunk_generation_requests(pm.load_chunks(), [1])
                    before_capture = copy.deepcopy(captured)
                    if change == 'row':
                        pm.delete_chunk(0)
                    else:
                        pm.update_chunk(1, {'text': 'Changed after admission.'})
                    before = pm.load_chunks()
                    with patch.object(pm, 'get_engine') as engine, self.assertRaisesRegex(ValueError, 'changed'):
                        if mode == 'single':
                            pm.generate_chunk_audio(1, admitted_chunk=captured[1])
                        elif mode == 'parallel':
                            pm.generate_chunks_parallel([1], admitted_requests=captured)
                        else:
                            pm.generate_chunks_batch([1], admitted_requests=captured)
                    engine.assert_not_called()
                    self.assertEqual(before, pm.load_chunks())
                    self.assertEqual(before_capture, captured)
                    self.assertIsNone(pm._active_journal_identity)

    def test_capture_ignores_status_progress_but_deep_copies_admitted_inputs(self):
        with self.fixture() as (_root, pm, _states, _api):
            rows = pm.load_chunks()
            captured = get_chunk_generation_requests(rows, [1])
            rows[1]['status'] = 'error'
            rows[1]['error'] = 'old attempt'
            accepted = get_chunk_generation_requests(rows, [1], captured)
            self.assertEqual('pending', accepted[1]['status'])
            accepted[1]['text'] = 'Local result mutation.'
            self.assertEqual('Source row 1.', captured[1]['text'])


class BookSaveIdentityTests(unittest.TestCase):
    def test_save_refuses_switched_book_family_but_allows_same_book_edits(self):
        for change in ('book', 'generation', 'source'):
            with self.subTest(change=change), saved_book_tests.SavedBookPublicationTests().fixture() as (root, api), TestClient(api) as client:
                token = get_book_operation_token(root)
                if change == 'source':
                    (root / 'annotated_script.json').write_text('[{"speaker":"Alice","text":"An edited source."}]')
                else:
                    (root / 'state.json').write_text(json.dumps({'active_book_id': 'B' if change == 'book' else 'new-book',
                                                               'book_generation': 'new-generation'}))
                before = saved_book_tests.SavedBookPublicationTests().artifacts(root)
                response = client.post('/api/scripts/save', json={'name': 'book', 'expected_book_token': token})
                self.assertEqual(200 if change == 'source' else 409, response.status_code, response.text)
                if change == 'source':
                    self.assertEqual((root / 'annotated_script.json').read_bytes(), (root / 'scripts/book.json').read_bytes())
                else:
                    self.assertEqual(before, saved_book_tests.SavedBookPublicationTests().artifacts(root))
