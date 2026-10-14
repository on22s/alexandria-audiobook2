"""Saving snapshots must not replace a batch-owned saved book family."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
import core
from routers import script, scripts_library as library
from review_script import _checkpoint_path
from tests import test_saved_book_publication as saved_publication


CLEAN = ['Review complete: 1 -> 1 entries', 'Total changes: 0']


class SavedDestinationOwnershipTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        helper = saved_publication.SavedBookPublicationTests()
        with helper.fixture() as (root, api), contextlib.ExitStack() as stack:
            states = {'batch_review': {'running': False, 'cancel': False}}
            for module in (core, script, library):
                stack.enter_context(patch.object(module, 'process_state', states))
            for name, value in (('SCRIPTS_DIR', root / 'scripts'), ('ROOT_DIR', root),
                                ('CHARACTER_ALIASES_PATH', root / 'aliases.json')):
                stack.enter_context(patch.object(script, name, str(value)))
            stack.enter_context(patch.object(core, '_task_claims', {}))
            stack.enter_context(patch.object(core, '_gpu_leases', {}))
            stack.enter_context(patch.object(core, '_saved_book_publications', {}))
            stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=False))
            stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
            stack.enter_context(patch.object(script, '_init_task_log', return_value=None))
            stack.enter_context(patch.object(script, '_write_batch_review_report', return_value=None))
            with TestClient(api) as client:
                try:
                    yield root, client, states, helper
                finally:
                    for name, owner in list(core._task_claims.items()):
                        core.release_gpu_task_claim(name, owner['id'])

    def start(self, names=('book',), **kwargs):
        background = BackgroundTasks()
        kwargs.setdefault('dedupe_speakers', False)
        result = asyncio.run(script.review_script_batch_start(
            script.BatchReviewRequest(script_names=list(names), **kwargs), background))
        self.assertEqual('started', result['status'])
        return background

    def test_pending_selected_destinations_refuse_but_distinct_snapshot_remains_allowed(self):
        with self.fixture() as (root, client, states, helper):
            names = ['book', *[f'pending_{n}' for n in range(999)]]
            self.start(names)
            before = helper.artifacts(root)
            for name in ('book', 'pending_998'):
                response = client.post('/api/scripts/save', json={'name': name})
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before, helper.artifacts(root))
            response = client.post('/api/scripts/save', json={'name': 'distinct'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual((root / 'annotated_script.json').read_bytes(),
                             (root / 'scripts/distinct.json').read_bytes())
            self.assertNotIn('tasks', states['batch_review'], 'scope must exist before worker initialization')

    def test_loaded_review_and_cancellation_preserve_family_until_exact_worker_exit(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), self.fixture() as (root, client, states, helper):
                entered, release = threading.Event(), threading.Event()
                errors = []
                book = root / 'scripts/book.json'
                checkpoint = Path(_checkpoint_path(str(book)))
                loaded = book.read_bytes()
                def stream(*_args, **_kwargs):
                    self.assertEqual(loaded, book.read_bytes())
                    checkpoint.write_bytes(b'synthetic resumable review checkpoint')
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('review worker not released')
                    if not states['batch_review']['cancel']:
                        book.write_bytes(loaded)
                    return 0, CLEAN
                def run():
                    try:
                        asyncio.run(background())
                    except BaseException as error:
                        errors.append(error)
                background = self.start()
                with patch.object(script, '_stream_subprocess_to_logs', side_effect=stream):
                    worker = threading.Thread(target=run)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(3))
                        before = helper.artifacts(root)
                        response = client.post('/api/scripts/save', json={'name': 'book'})
                        self.assertEqual(409, response.status_code, response.text)
                        self.assertEqual(before, helper.artifacts(root))
                        states['batch_review']['cancel'] = cancelled
                        self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'distinct'}).status_code)
                    finally:
                        release.set()
                        worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual([], errors)
                self.assertEqual(b'synthetic resumable review checkpoint', checkpoint.read_bytes())
                self.assertFalse(core.is_task_running('batch_review'))
                self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
                self.assertFalse(checkpoint.exists())

    def test_selected_book_remains_owned_between_forward_and_backward_passes(self):
        with self.fixture() as (root, client, states, helper):
            (root / 'scripts/second.json').write_text('[{"speaker":"Second","text":"Second book."}]')
            background = self.start(('book', 'second'), dedupe_speakers=True,
                                    find_nicknames=True, bidirectional=True)
            attempts = []
            def stream(command, *_args, **_kwargs):
                response = client.post('/api/scripts/save', json={'name': 'book'})
                attempts.append((states['batch_review']['current_pass'], response.status_code))
                self.assertEqual(409, response.status_code, response.text)
                return 0, [] if any(str(arg).endswith('find_nicknames.py') for arg in command) else CLEAN
            with patch.object(script, '_stream_subprocess_to_logs', side_effect=stream):
                asyncio.run(background())
            self.assertEqual(8, len(attempts))
            self.assertEqual({'fwd', 'bwd'}, {phase for phase, _ in attempts})
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)

    def test_running_flag_cannot_retire_a_started_claim_before_callback_cleanup(self):
        with self.fixture() as (root, client, states, helper):
            background = BackgroundTasks()
            def callback():
                states['batch_review']['running'] = False
                before = helper.artifacts(root)
                self.assertEqual(409, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
                self.assertEqual(before, helper.artifacts(root))
                self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'distinct'}).status_code)
            core.schedule_claimed_background_task(background, 'batch_review', callback,
                saved_destinations=[str(root / 'scripts/book.json')])
            asyncio.run(background())
            self.assertEqual({}, core._task_claims)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)

    def test_registration_failure_releases_scope_without_leaking_ownership(self):
        with self.fixture() as (root, client, states, helper):
            background = BackgroundTasks()
            with patch.object(background, 'add_task', side_effect=RuntimeError('registration failed')):
                with self.assertRaisesRegex(RuntimeError, 'registration failed'):
                    asyncio.run(script.review_script_batch_start(
                        script.BatchReviewRequest(script_names=['book']), background))
            self.assertEqual({}, core._task_claims)
            self.assertFalse(states['batch_review']['running'])
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)

    def test_legacy_scope_and_unknown_admission_gap_refuse_conservatively(self):
        with self.fixture() as (root, client, states, helper):
            states['batch_review'].update(running=True, tasks=[{'name': 'book', 'status': 'done'}])
            self.assertEqual(409, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'distinct'}).status_code)
            states['batch_review']['tasks'] = []
            self.assertEqual(409, client.post('/api/scripts/save', json={'name': 'other'}).status_code)
            states['batch_review']['tasks'] = [{'status': 'running'}]
            self.assertEqual(409, client.post('/api/scripts/save', json={'name': 'other'}).status_code)

    def test_claim_scope_is_copied_canonical_and_retired_with_its_exact_owner(self):
        with self.fixture() as (root, client, states, helper):
            alias = root / 'alias'
            alias.symlink_to(root / 'scripts', target_is_directory=True)
            destinations = [str(alias / 'book.json')]
            first = core.reserve_background_task('batch_review', saved_destinations=destinations)
            destinations.clear()
            self.assertEqual(409, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            core.release_gpu_task_claim('batch_review', first)
            second = core.reserve_background_task('batch_review', saved_destinations=[str(root / 'scripts/second.json')])
            self.assertFalse(core.release_gpu_task_claim('batch_review', first))
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            self.assertEqual(409, client.post('/api/scripts/save', json={'name': 'second'}).status_code)
            core.release_gpu_task_claim('batch_review', second)

    def test_save_publication_excludes_new_batch_admission_until_commit(self):
        import book_state_transaction as books
        with self.fixture() as (root, client, states, helper):
            entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
            errors = []
            publish = books.apply_book_state_locked
            def hold(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('save publication not released')
                return publish(*args, **kwargs)
            def save():
                try:
                    self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
                except BaseException as error:
                    errors.append(error)
            def start():
                try:
                    self.start()
                    self.fail('batch entered an unfinished save transaction')
                except core.HTTPException as error:
                    self.assertEqual(409, error.status_code)
                except BaseException as error:
                    errors.append(error)
                finally:
                    attempted.set()
            with patch.object(library, 'apply_book_state_locked', side_effect=hold):
                saver = threading.Thread(target=save)
                worker = threading.Thread(target=start)
                saver.start()
                try:
                    self.assertTrue(entered.wait(2))
                    worker.start()
                    self.assertTrue(attempted.wait(2), 'batch refusal must not wait for Save file I/O')
                finally:
                    release.set()
                    saver.join(5)
                    if worker.ident is not None:
                        worker.join(5)
            self.assertFalse(saver.is_alive() or worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual({}, core._saved_book_publications)
            self.start()
            self.assertEqual((root / 'annotated_script.json').read_bytes(), (root / 'scripts/book.json').read_bytes())

    def test_foreign_save_observes_active_review_file_lock(self):
        code = '''import asyncio,json,sys
from contextlib import contextmanager
from pathlib import Path
import core
from routers import scripts_library as lib
from utils import file_lock
root=Path(sys.argv[1])
for module in (core,lib):
 module.DATA_DIR=str(root);module.SCRIPTS_DIR=str(root/'scripts')
 module.SCRIPT_PATH=str(root/'annotated_script.json');module.VOICE_CONFIG_PATH=str(root/'voice_config.json')
lib.file_lock=lambda path:file_lock(path,timeout=.03)
try:
 result=asyncio.run(lib.save_script(lib.ScriptSaveRequest(name=sys.argv[2])))
 print(json.dumps({'status':200,'result':result}))
except lib.HTTPException as error:print(json.dumps({'status':error.status_code}))
'''
        with self.fixture() as (root, client, states, helper):
            before = helper.artifacts(root)
            observed = []
            background = self.start()
            def stream(*_args, **_kwargs):
                for name in ('book', 'distinct'):
                    process = subprocess.run([sys.executable, '-c', code, str(root), name],
                                             capture_output=True, text=True, timeout=8)
                    self.assertEqual(0, process.returncode, process.stderr)
                    observed.append(json.loads(process.stdout.splitlines()[-1])['status'])
                self.assertEqual(before, {name: value for name, value in helper.artifacts(root).items()
                                          if not name.startswith('distinct.')})
                return 0, CLEAN
            with patch.object(script, 'clear_checkpoint'), \
                    patch.object(script, '_stream_subprocess_to_logs', side_effect=stream):
                asyncio.run(background())
            self.assertEqual([409, 200], observed)

    def test_save_waits_for_real_generation_launch_without_holding_task_mutex(self):
        from tests.test_batch_script_output_admission import BatchScriptOutputAdmissionTests
        entered, proceed, save_entered = threading.Event(), threading.Event(), threading.Event()
        native_environment = core.get_gpu_task_environment
        native_guard = library.ensure_saved_book_destination_available
        statuses, errors = [], []
        @contextlib.contextmanager
        def guard(path):
            with native_guard(path):
                save_entered.set()
                yield
        def environment(*args, **kwargs):
            entered.set()
            if not proceed.wait(3):
                raise AssertionError('launch barrier not released')
            return native_environment(*args, **kwargs)
        with BatchScriptOutputAdmissionTests().fixture() as (root, source, output, job, state, client):
            def stream(*args, **kwargs):
                code = 'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(\'[ {"text":"generated"} ]\');print("synthetic generation")'
                return core._stream_subprocess_to_logs([sys.executable, '-c', code, str(output)], str(root), state)
            def generate():
                try:
                    script._run_batch_script_job(job, state, None, 1)
                except BaseException as error:
                    errors.append(error)
            def save():
                try:
                    statuses.append(client.post('/api/scripts/save', json={'name': 'book'}).status_code)
                except BaseException as error:
                    errors.append(error)
            with patch.object(core, 'get_gpu_task_environment', side_effect=environment), \
                    patch.object(library, 'ensure_saved_book_destination_available', side_effect=guard), \
                    patch.object(script, '_stream_subprocess_to_logs', side_effect=stream):
                worker, saver = threading.Thread(target=generate), threading.Thread(target=save)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    saver.start()
                    self.assertTrue(save_entered.wait(2))
                    acquired = core._gpu_lock.acquire(blocking=False)
                    if acquired:
                        core._gpu_lock.release()
                    self.assertTrue(acquired, 'Save must release task mutex before waiting for generation output')
                finally:
                    proceed.set()
                    worker.join(5)
                    if saver.ident is not None:
                        saver.join(5)
            self.assertFalse(worker.is_alive() or saver.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([200], statuses)
            self.assertEqual('done', state['tasks'][0]['status'])
            self.assertEqual(source.read_bytes(), output.read_bytes())
            self.assertEqual({}, core._saved_book_publications)
