"""Lock ordering, receipt snapshots and path-free journal error mapping."""
import asyncio
from contextlib import contextmanager, ExitStack
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from tests.test_support import use_private_cpu_fixture_lock

import book_state_transaction as books
from book_state_transaction import (BookTransactionError, BOOK_STATE_BUSY_DETAIL,
    BOOK_STORAGE_FAILURE_DETAIL, BOOK_JOURNAL_MIGRATION_DETAIL,
    BOOK_JOURNAL_UNCERTAIN_DETAIL, BOOK_EXCHANGE_DIVERGED_DETAIL)
import core
from project import ProjectManager
from routers import (editor, script, scripts_library as library,
                     speaker_review as speaker_routes)
from tests import test_narration_markup as markup_fixtures
import utils
from utils import file_lock


class ChunkEditLockOrderTests(unittest.TestCase):
    def test_edit_enters_book_journal_before_gpu_mutex(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            events = []
            real_book = editor.ensure_book_state
            real_gpu = editor._gpu_lock

            @contextmanager
            def recording_book(*args, **kwargs):
                events.append('book')
                with real_book(*args, **kwargs) as admitted:
                    yield admitted

            class RecordingGpu:
                def __enter__(self):
                    events.append('gpu')
                    return real_gpu.__enter__()

                def __exit__(self, *args):
                    return real_gpu.__exit__(*args)

            with patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                    patch.object(editor, 'ensure_book_state', side_effect=recording_book), \
                    patch.object(editor, '_gpu_lock', RecordingGpu()), \
                    patch.object(editor, 'check_global_gpu_lock'):
                result = editor._apply_chunk_edit(lambda: 'applied')

            self.assertEqual('applied', result)
            self.assertEqual(['book', 'gpu'], events)

    def test_operation_reuses_the_outer_book_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            acquired = []
            real_operator_lock = books.book_operator_locked

            def counting_lock(*args, **kwargs):
                acquired.append(True)
                return real_operator_lock(*args, **kwargs)

            def operation():
                with editor.reuse_book_state(str(root), timeout=0.25):
                    return 'applied'

            with patch.object(books, 'book_operator_locked', side_effect=counting_lock), \
                    patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                    patch.object(editor, 'check_global_gpu_lock'):
                result = editor._apply_chunk_edit(operation)

            self.assertEqual('applied', result)
            # One kernel admission only: the operation reused the outer one
            # instead of re-entering the non-reentrant file lock.
            self.assertEqual(1, len(acquired))


class DeletedChunkReceiptsTests(unittest.TestCase):
    def test_reader_snapshots_while_writers_resize_the_dict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            receipts = {}
            for index in range(8):
                receipts[f'initial-{index}'] = {'created': time.monotonic(),
                    'book': 'synthetic-book', 'chunk': {'id': index}}
            errors = []
            barrier = threading.Barrier(6)

            def reader():
                barrier.wait(timeout=2)
                for _ in range(200):
                    try:
                        editor._get_undo_take_rows()
                    except BaseException as error:
                        errors.append(error)

            def writer(tag):
                barrier.wait(timeout=2)
                for index in range(200):
                    with editor._deleted_chunk_receipts_lock:
                        receipts[f'{tag}-{index}'] = {'created': time.monotonic(),
                            'book': 'synthetic-book', 'chunk': {'id': index}}
                        if len(receipts) > 16:
                            for token in list(receipts)[:8]:
                                del receipts[token]

            original_interval = sys.getswitchinterval()
            sys.setswitchinterval(0.000001)
            try:
                with patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                        patch.object(editor, '_deleted_chunk_receipts', receipts), \
                        patch.object(editor, 'get_editor_book_identity', return_value='synthetic-book'):
                    threads = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
                    threads += [threading.Thread(target=writer, args=(f'w{tag}',), daemon=True)
                                for tag in range(3)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=5)
            finally:
                sys.setswitchinterval(original_interval)

            self.assertFalse(errors, errors[:1])
            self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_deletion_writer_uses_the_receipt_leaf_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manager = ProjectManager(folder)
            manager.save_chunks([{'id': index, 'speaker': 'SYNTHETIC',
                                  'text': f'Synthetic sentence {index}.', 'status': 'pending'}
                                 for index in range(3)])
            real_lock = editor._deleted_chunk_receipts_lock
            entered = []

            class RecordingLock:
                def __enter__(self):
                    entered.append(True)
                    return real_lock.__enter__()

                def __exit__(self, *args):
                    return real_lock.__exit__(*args)

            with patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                    patch.object(editor, 'project_manager', manager), \
                    patch.object(editor, '_deleted_chunk_receipts', {}), \
                    patch.object(editor, '_deleted_chunk_receipts_lock', RecordingLock()):
                deleted, _chunks, _token = editor.apply_chunk_deletion(1)

            self.assertTrue(entered)
            self.assertEqual('Synthetic sentence 1.', deleted['text'])


class JournalErrorMappingTests(unittest.TestCase):
    def test_lock_timeout_is_a_path_free_409(self):
        with patch.object(editor, 'ensure_book_state', side_effect=TimeoutError(
                'Could not acquire file lock on /private/data/.active_book_transaction.json.lock '
                'within 10 seconds.')):
            with self.assertRaises(HTTPException) as raised:
                editor._apply_chunk_edit(lambda: None)
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(BOOK_STATE_BUSY_DETAIL, raised.exception.detail)
        self.assertNotIn('/', raised.exception.detail)

    def test_damaged_journal_is_a_path_free_500(self):
        with patch.object(editor, 'ensure_book_state',
                          side_effect=BookTransactionError('Invalid active-book transaction')):
            with self.assertRaises(HTTPException) as raised:
                editor._apply_chunk_edit(lambda: None)
        self.assertEqual(500, raised.exception.status_code)
        self.assertEqual(BookTransactionError.DETAIL, raised.exception.detail)
        self.assertNotIn('/', raised.exception.detail)

    def test_generate_route_maps_the_same_journal_errors(self):
        for failure, status, detail in (
                (TimeoutError('Could not acquire file lock on /private/data/journal.lock'),
                 409, BOOK_STATE_BUSY_DETAIL),
                (BookTransactionError('Invalid active-book transaction'),
                 500, BookTransactionError.DETAIL)):
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(script, 'start_script_generation', side_effect=failure):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(script._start_script_generation_for_route(object(), 'source.json', None))
            self.assertEqual(status, raised.exception.status_code)
            self.assertEqual(detail, raised.exception.detail)
            self.assertNotIn('/', raised.exception.detail)

    def test_recovery_validation_failure_becomes_book_transaction_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(books, 'recover_book_state_locked',
                              side_effect=ValueError('Invalid active-book transaction')):
                with self.assertRaises(BookTransactionError) as raised:
                    with books.ensure_book_state(folder):
                        pass
            self.assertEqual('Invalid active-book transaction', str(raised.exception))
            self.assertIsInstance(raised.exception, ValueError)


@contextmanager
def editor_http_fixture(rows=None):
    """Real disposable book routed through the Editor router and its locks."""
    with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
        root = Path(folder)
        source = root / 'source.txt'
        source.write_text('“Synthetic sentence.”', encoding='utf-8')
        config = root / 'config.json'
        config.write_text('{}')
        state = root / 'state.json'
        state.write_text(json.dumps({'active_book_id': 'synthetic-book',
            'book_generation': 'generation-a', 'input_file_path': str(source)}))
        (root / 'annotated_script.json').write_text(json.dumps([
            {'speaker': 'A', 'text': 'Synthetic sentence.'}]))
        (root / 'voice_config.json').write_text(json.dumps({'A': {'voice': 'Ryan'}}))
        pm = ProjectManager(folder)
        pm.save_chunks(rows if rows is not None else [
            {'id': 0, 'uid': 'line-0', 'speaker': 'A', 'text': 'Synthetic sentence.',
             'status': 'pending'}])
        states = copy.deepcopy(core.process_state)
        for value in states.values():
            value['running'] = False
        for obj, name, value in ((editor, 'project_manager', pm),
                (editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')),
                (editor, 'DATA_DIR', str(root)), (editor, 'CONFIG_PATH', str(config)),
                (core, 'DATA_DIR', str(root)), (core, 'CONFIG_PATH', str(config)),
                (core, 'process_state', states), (editor, 'process_state', states),
                (core, '_task_claims', {}), (core, '_gpu_leases', {})):
            stack.enter_context(patch.object(obj, name, value))
        private_lock = use_private_cpu_fixture_lock()
        private_lock.__enter__()
        stack.callback(private_lock.__exit__, None, None, None)
        api = FastAPI()
        api.include_router(editor.router)
        client = TestClient(api)
        stack.callback(client.close)
        yield root, pm, states, client


def book_artifacts(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()
            and not path.name.endswith('.lock')
            and '.task_ownership' not in path.parts}


class MarkupSharedAdmissionTests(unittest.TestCase):
    """The markup routes share the Editor's one book admission, recover=False."""

    def setUp(self):
        markup_fixtures.NarrationMarkupEditTests.setUp(self)
        states = copy.deepcopy(core.process_state)
        for state in states.values():
            state['running'] = False
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for obj, name, value in ((editor, 'project_manager', self.pm),
                (editor, 'SCRIPT_PATH', str(self.root / 'annotated_script.json')),
                (editor, 'DATA_DIR', str(self.root)),
                (core, 'DATA_DIR', str(self.root)),
                (core, 'process_state', states), (editor, 'process_state', states),
                (core, '_task_claims', {}), (core, '_gpu_leases', {})):
            self.stack.enter_context(patch.object(obj, name, value))
        private_lock = use_private_cpu_fixture_lock()
        private_lock.__enter__()
        self.addCleanup(private_lock.__exit__, None, None, None)
        api = FastAPI()
        api.include_router(editor.router)
        self.client = TestClient(api)
        self.addCleanup(self.client.close)

    def post(self, action, **fields):
        return self.client.post('/api/chunks/0/narration-markup/' + action,
            json=dict(expected_book_token=fields.pop('expected_book_token', self.token),
                      expected_uid=fields.pop('expected_uid', 'line-a'), **fields))

    def preview(self):
        response = self.post('preview')
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def apply(self, report, choice='keep'):
        return self.post('apply', binding=report['binding'], decision=choice)

    def test_preview_apply_replay_share_one_admission(self):
        report = self.preview()
        started = time.monotonic()
        response = self.apply(report)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('applied', response.json()['status'])
        self.assertLess(time.monotonic() - started, 2, 'apply re-acquired the held book lock')
        response = self.apply(report)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('already_applied', response.json()['status'])

    def test_pending_journal_refuses_preview_apply_and_replay_without_recovery(self):
        report = self.preview()
        self.assertEqual(200, self.apply(report).status_code)
        (self.root / books.JOURNAL).write_text(json.dumps({
            'version': 1, 'phase': 'recovered', 'workspace': '.book-switch-' + 'a' * 32,
            'entries': []}), encoding='utf-8')
        before = book_artifacts(self.root)
        for action, fields in (('preview', {}),
                               ('apply', {'binding': report['binding'], 'decision': 'keep'})):
            with self.subTest(action=action):
                response = self.post(action, **fields)
                self.assertEqual(409, response.status_code, response.text)
                detail = response.json()['detail']
                self.assertNotIn('/', detail)
                self.assertNotIn(str(self.root), detail)
        self.assertEqual(before, book_artifacts(self.root))


class InsertRouteStorageFaultTests(unittest.TestCase):
    """Real filesystem failures on /api/chunks/0/insert stay path-free."""

    def test_held_state_lock_is_a_distinct_path_free_409(self):
        with editor_http_fixture() as (root, _pm, _states, client):
            before = book_artifacts(root)
            real_lock = utils.file_lock
            def bounded(path, *args, **kwargs):
                kwargs.setdefault('timeout', 0.2)
                return real_lock(path, *args, **kwargs)
            with file_lock(str(root / 'state.json'), timeout=3), \
                    patch.object(utils, 'file_lock', bounded):
                response = client.post('/api/chunks/0/insert')
            self.assertEqual(409, response.status_code, response.text)
            detail = response.json()['detail']
            self.assertEqual(BOOK_STATE_BUSY_DETAIL, detail)
            self.assertNotIn('/', detail)
            self.assertNotIn(str(root), detail)
            self.assertEqual(before, book_artifacts(root))

    def test_directory_at_lock_path_is_a_path_free_storage_failure(self):
        for lock_name in ('state.json.lock', books.JOURNAL + '.lock'):
            with self.subTest(lock_name=lock_name), editor_http_fixture() as (root, _pm, _states, client):
                before = book_artifacts(root)
                blocked = root / lock_name
                blocked.mkdir()
                try:
                    response = client.post('/api/chunks/0/insert')
                finally:
                    blocked.rmdir()
                self.assertEqual(409, response.status_code, response.text)
                detail = response.json()['detail']
                self.assertEqual(BOOK_STORAGE_FAILURE_DETAIL, detail)
                self.assertNotIn('/', detail)
                self.assertNotIn(str(root), detail)
                self.assertEqual(before, book_artifacts(root))


class CancelLockOrderTests(unittest.TestCase):
    """Idle cancel takes the book before the GPU mutex; foreign leases stay owned."""

    def test_bounded_edit_cancel_race_resets_the_stale_row(self):
        rows = [{'id': 0, 'uid': 'line-0', 'speaker': 'A', 'text': 'Synthetic sentence.',
                 'status': 'generating'}]
        with editor_http_fixture(rows) as (root, pm, _states, _client):
            entered, release = threading.Event(), threading.Event()
            def edit():
                with books.ensure_book_state(str(root)):
                    entered.set()
                    release.wait(3)
            holder = threading.Thread(target=edit, daemon=True)
            holder.start()
            self.assertTrue(entered.wait(2))
            result = []
            cancel = threading.Thread(target=lambda: result.append(editor._apply_audio_cancel()),
                                      daemon=True)
            cancel.start()
            try:
                time.sleep(0.05)
                self.assertTrue(cancel.is_alive(), 'cancel finished while the book was held')
                self.assertFalse(editor._gpu_lock.locked(),
                    'cancel held the GPU mutex while waiting for the book journal')
            finally:
                release.set()
            cancel.join(2)
            holder.join(2)
            self.assertFalse(cancel.is_alive())
            self.assertFalse(holder.is_alive())
            self.assertEqual({'status': 'not_running', 'reset_chunks': 1}, result[0])
            self.assertEqual('pending', pm.load_chunks()[0]['status'])

    def test_foreign_audio_lease_leaves_the_generating_row_intact(self):
        owner = ('import pathlib,sys,time\nimport core\n'
                 'core.DATA_DIR=sys.argv[1]\ncore.acquire_gpu_lock=lambda:None\n'
                 'from task_ownership import acquire_task_lease\n'
                 'lease=acquire_task_lease(sys.argv[1],"audio",set())\n'
                 '(pathlib.Path(sys.argv[1])/"lease-ready").write_text("ok")\n'
                 'while not (pathlib.Path(sys.argv[1])/"release-lease").exists(): time.sleep(.01)\n'
                 'lease.close()\n')
        rows = [{'id': 0, 'uid': 'line-0', 'speaker': 'A', 'text': 'Synthetic sentence.',
                 'status': 'generating'}]
        with editor_http_fixture(rows) as (root, pm, _states, _client):
            env = dict(os.environ)
            env.pop('PYTHONPATH', None)
            env.pop('LD_LIBRARY_PATH', None)
            process = subprocess.Popen([sys.executable, '-c', owner, str(root)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            try:
                ready = time.monotonic() + 5
                while not (root / 'lease-ready').exists() and time.monotonic() < ready:
                    time.sleep(0.01)
                self.assertTrue((root / 'lease-ready').exists(), 'foreign lease never started')
                before = book_artifacts(root)
                with self.assertRaises(HTTPException) as refused:
                    editor._apply_audio_cancel()
                self.assertEqual(409, refused.exception.status_code)
                self.assertIn('owning server', refused.exception.detail)
                self.assertEqual(before, book_artifacts(root))
                self.assertEqual('generating', pm.load_chunks()[0]['status'])
            finally:
                (root / 'release-lease').write_text('ok')
                process.wait(timeout=5)


class RecoveryClassRouteTests(unittest.TestCase):
    """Each recovery class gets one actionable path-free JSON answer per route."""

    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            source = root / 'source.txt'
            source.write_text('“Synthetic sentence.”', encoding='utf-8')
            config = root / 'config.json'
            config.write_text('{}')
            state = root / 'state.json'
            state.write_text(json.dumps({'input_file_path': str(source),
                                         'active_book_id': 'synthetic-book'}))
            (root / 'annotated_script.json').write_text('[]')
            states = copy.deepcopy(core.process_state)
            for value in states.values():
                value['running'] = False
            for obj, name, value in ((core, 'DATA_DIR', folder), (script, 'DATA_DIR', folder),
                    (script, 'SCRIPT_PATH', str(root / 'annotated_script.json')),
                    (script, 'CONFIG_PATH', str(config)),
                    (core, 'process_state', states), (script, 'process_state', states),
                    (core, '_task_claims', {}), (core, '_gpu_leases', {})):
                stack.enter_context(patch.object(obj, name, value))
            stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
            stack.enter_context(patch.object(script, 'get_active_reasoning_effort', return_value=None))
            stack.enter_context(patch.object(script, 'three_pass_refusal', return_value=None))
            stack.enter_context(patch.object(script, 'get_existing_cast_list', return_value=None))
            stack.enter_context(patch.object(script, 'build_generate_script_command', return_value=[]))
            stack.enter_context(patch.object(script, 'ensure_script_recovery_manifest',
                                             return_value={'status': 'failed'}))
            yield root, source, state

    def _route_refusals(self, root, source):
        with patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                patch.object(editor, 'DATA_DIR', str(root)):
            with self.assertRaises(HTTPException) as edit:
                editor._apply_chunk_edit(lambda: None)
        with self.assertRaises(HTTPException) as started:
            asyncio.run(script._start_script_generation_for_route(object(), str(source), None))
        with self.assertRaises(HTTPException) as retried:
            asyncio.run(script._start_script_generation_for_route(
                object(), str(source), None, require_recovery=True))
        return edit.exception, started.exception, retried.exception

    def test_valid_v1_migration_maps_identically_and_reserves_no_task(self):
        with self.fixture() as (root, source, state):
            (root / books.JOURNAL).write_text(json.dumps({
                'version': 1, 'phase': 'recovered', 'workspace': '.book-switch-' + 'a' * 32,
                'entries': [{'path': 'state.json', 'existed': True,
                    'old_sha256': hashlib.sha256(b'synthetic').hexdigest(),
                    'new_sha256': None}]}), encoding='utf-8')
            before = book_artifacts(root)
            edit, started, retried = self._route_refusals(root, source)
            for refusal in (edit, started, retried):
                self.assertEqual(409, refusal.status_code)
                self.assertEqual(BOOK_JOURNAL_MIGRATION_DETAIL, refusal.detail)
                self.assertNotIn('/', refusal.detail)
                self.assertNotIn(str(root), refusal.detail)
            self.assertEqual(before, book_artifacts(root))
            self.assertEqual({}, core._task_claims)

    def test_uncertain_publication_maps_identically_and_reserves_no_task(self):
        from book_state_transaction import BookJournalPublicationUncertain
        with self.fixture() as (root, source, state):
            before = book_artifacts(root)
            with patch.object(books, 'recover_book_state_locked',
                    side_effect=BookJournalPublicationUncertain('injected uncertain publication')):
                edit, started, retried = self._route_refusals(root, source)
            for refusal in (edit, started, retried):
                self.assertEqual(409, refusal.status_code)
                self.assertEqual(BOOK_JOURNAL_UNCERTAIN_DETAIL, refusal.detail)
                self.assertNotIn('/', refusal.detail)
                self.assertNotIn(str(root), refusal.detail)
            self.assertEqual(before, book_artifacts(root))
            self.assertEqual({}, core._task_claims)


@contextmanager
def bypass_routes_fixture():
    """One disposable book shared by the Editor, saved-book and speaker-review routers."""
    with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
        root = Path(folder)
        scripts = root / 'scripts'
        scripts.mkdir()
        (scripts / 'book_b.json').write_text(json.dumps([
            {'speaker': 'B', 'text': 'Loaded book sentence.'}]), encoding='utf-8')
        source = root / 'source.txt'
        source.write_text('Synthetic sentence.', encoding='utf-8')
        (root / 'annotated_script.json').write_text(json.dumps([
            {'speaker': 'A', 'text': 'Synthetic sentence.'}]), encoding='utf-8')
        (root / 'config.json').write_text('{}', encoding='utf-8')
        (root / 'state.json').write_text(json.dumps({
            'active_book_id': 'book-a', 'book_generation': 'g-a',
            'input_file_path': str(source)}), encoding='utf-8')
        pm = ProjectManager(folder)
        pm.save_chunks([{'id': 0, 'uid': 'line-0', 'speaker': 'A',
                         'text': 'Synthetic sentence.', 'status': 'pending'}])
        run_id = 'a' * 64
        runs = root / '.speaker_review'
        runs.mkdir(mode=0o700)
        record = {'schema': speaker_routes.SCHEMA, 'run_id': run_id, 'snapshot': run_id,
                  'attempts': 0, 'cache': {}, 'reviews': [], 'status': 'completed',
                  'selection': {'source': 'current', 'reference_name': 'book_b',
                                'max_pairs': 20, 'allow_partial': False}}
        (runs / (run_id + '.json')).write_text(json.dumps(record), encoding='utf-8')
        states = copy.deepcopy(core.process_state)
        for value in states.values():
            value['running'] = False
        shared = (
            (core, 'DATA_DIR', folder), (core, 'SCRIPTS_DIR', str(scripts)),
            (core, 'SCRIPT_PATH', str(root / 'annotated_script.json')),
            (core, 'VOICE_CONFIG_PATH', str(root / 'voice_config.json')),
            (core, 'CHUNKS_PATH', str(root / 'chunks.json')),
            (core, 'CONFIG_PATH', str(root / 'config.json')),
            (core, 'CHARACTER_ALIASES_PATH', str(root / 'character_aliases.json')),
            (core, 'process_state', states), (core, '_task_claims', {}),
            (core, '_gpu_leases', {}),
            (library, 'DATA_DIR', folder), (library, 'SCRIPTS_DIR', str(scripts)),
            (library, 'SCRIPT_PATH', str(root / 'annotated_script.json')),
            (library, 'VOICE_CONFIG_PATH', str(root / 'voice_config.json')),
            (library, 'CHUNKS_PATH', str(root / 'chunks.json')),
            (library, 'AUDIOBOOK_PATH', str(root / 'cloned_audiobook.mp3')),
            (library, 'M4B_PATH', str(root / 'audiobook.m4b')),
            (library, 'process_state', states),
            (editor, 'DATA_DIR', folder),
            (editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')),
            (editor, 'CONFIG_PATH', str(root / 'config.json')),
            (editor, 'process_state', states), (editor, 'project_manager', pm),
        )
        for obj, name, value in shared:
            stack.enter_context(patch.object(obj, name, value))
        private_lock = use_private_cpu_fixture_lock()
        private_lock.__enter__()
        stack.callback(private_lock.__exit__, None, None, None)
        api = FastAPI()
        api.include_router(editor.router)
        api.include_router(library.router)
        api.include_router(speaker_routes.router)
        client = TestClient(api)
        stack.callback(client.close)
        yield root, client, states, run_id


class SpeakerReviewApplyJournalTests(unittest.TestCase):
    def test_committed_journal_and_live_lease_refuse_apply_without_mutation(self):
        with bypass_routes_fixture() as (root, client, states, run_id):
            (root / books.JOURNAL).write_text(json.dumps({
                'version': 1, 'phase': 'committed',
                'workspace': '.book-switch-' + 'b' * 32,
                'entries': [{'path': 'state.json', 'existed': True,
                    'old_sha256': hashlib.sha256(b'old').hexdigest(),
                    'new_sha256': hashlib.sha256(b'new').hexdigest()}]}), encoding='utf-8')
            lease = core.acquire_task_lease(core.DATA_DIR, speaker_routes.TASK, core.LLM_TASKS)
            try:
                states[speaker_routes.TASK]['running'] = True
                before = book_artifacts(root)
                response = client.post('/api/speaker_review/' + run_id + '/apply', json={
                    'snapshot': run_id, 'candidate_id': 'c' * 64,
                    'alias': 'ALICE', 'canonical': 'AL'})
            finally:
                states[speaker_routes.TASK]['running'] = False
                lease.close()
            self.assertEqual(409, response.status_code, response.text)
            detail = response.json()['detail']
            self.assertEqual('Book recovery is required before previewing saved state', detail)
            self.assertNotIn('/', detail)
            self.assertNotIn(str(root), detail)
            self.assertEqual(before, book_artifacts(root))
            self.assertEqual({}, core._task_claims)


class SavedBookLoadLockOrderTests(unittest.TestCase):
    def test_load_waits_for_book_without_gpu_mutex_while_insert_proceeds(self):
        with bypass_routes_fixture() as (root, client, _states, _run_id):
            real_gpu = library._gpu_lock
            insert_has_book, allow_insert = threading.Event(), threading.Event()

            class PausingGpu:
                def __enter__(self):
                    insert_has_book.set()
                    allow_insert.wait(3)
                    return real_gpu.__enter__()

                def __exit__(self, *args):
                    return real_gpu.__exit__(*args)

            results = {}

            def do_insert():
                results['insert'] = client.post('/api/chunks/0/insert')

            def do_load():
                results['load'] = client.post('/api/scripts/load', json={'name': 'book_b'})

            with patch.object(editor, '_gpu_lock', PausingGpu()):
                insert_thread = threading.Thread(target=do_insert, daemon=True)
                load_thread = None
                try:
                    insert_thread.start()
                    self.assertTrue(insert_has_book.wait(2), 'insert never held the book journal')
                    load_thread = threading.Thread(target=do_load, daemon=True)
                    load_thread.start()
                    time.sleep(0.05)
                    self.assertTrue(load_thread.is_alive(), 'load did not wait for the book journal')
                    self.assertFalse(real_gpu.locked(),
                                     'load held the GPU mutex while waiting for the book journal')
                finally:
                    allow_insert.set()
                    insert_thread.join(3)
                    if load_thread is not None:
                        load_thread.join(3)
            self.assertFalse(insert_thread.is_alive())
            self.assertEqual(200, results['insert'].status_code, results['insert'].text)
            self.assertEqual(200, results['load'].status_code, results['load'].text)
            self.assertEqual([{'speaker': 'B', 'text': 'Loaded book sentence.'}],
                             json.loads((root / 'annotated_script.json').read_text()))
            self.assertEqual(core._get_saved_book_id('book_b'),
                             json.loads((root / 'state.json').read_text())['active_book_id'])


class JournalFaultBypassRouteTests(unittest.TestCase):
    def test_valid_v1_journal_maps_identically_for_cancel_and_delete(self):
        with bypass_routes_fixture() as (root, client, _states, _run_id):
            (root / books.JOURNAL).write_text(json.dumps({
                'version': 1, 'phase': 'recovered',
                'workspace': '.book-switch-' + 'a' * 32,
                'entries': [{'path': 'state.json', 'existed': True,
                    'old_sha256': hashlib.sha256(b'synthetic').hexdigest(),
                    'new_sha256': None}]}), encoding='utf-8')
            before = book_artifacts(root)
            responses = (client.post('/api/cancel_audio'),
                         client.delete('/api/scripts/book_b'))
            for response in responses:
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(BOOK_JOURNAL_MIGRATION_DETAIL, response.json()['detail'])
                self.assertNotIn('/', response.json()['detail'])
                self.assertNotIn(str(root), response.json()['detail'])
            self.assertEqual(before, book_artifacts(root))
            self.assertEqual({}, core._task_claims)

    def test_uncertain_and_diverged_map_identically_on_every_bypass_route(self):
        from book_state_transaction import (BookJournalPublicationUncertain,
                                            BookExchangeDiverged)
        cases = (
            (BookJournalPublicationUncertain('injected uncertain'), BOOK_JOURNAL_UNCERTAIN_DETAIL),
            (BookExchangeDiverged('injected diverged'), BOOK_EXCHANGE_DIVERGED_DETAIL),
        )
        with bypass_routes_fixture() as (root, client, _states, run_id):
            before = book_artifacts(root)
            for error, detail in cases:
                with self.subTest(error=type(error).__name__):
                    with patch.object(editor, 'ensure_book_state', side_effect=error):
                        cancel = client.post('/api/cancel_audio')
                    with patch.object(library, 'ensure_book_state', side_effect=error):
                        delete = client.delete('/api/scripts/book_b')
                    with patch.object(speaker_routes, '_apply', side_effect=error):
                        applied = client.post('/api/speaker_review/' + run_id + '/apply', json={
                            'snapshot': run_id, 'candidate_id': 'c' * 64,
                            'alias': 'ALICE', 'canonical': 'AL'})
                    for response in (cancel, delete, applied):
                        self.assertEqual(409, response.status_code, response.text)
                        self.assertEqual(detail, response.json()['detail'])
                        self.assertNotIn('/', response.json()['detail'])
                        self.assertNotIn(str(root), response.json()['detail'])
                    self.assertEqual(before, book_artifacts(root))
                    self.assertEqual({}, core._task_claims)


if __name__ == '__main__':
    unittest.main()
