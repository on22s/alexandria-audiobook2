"""Cancellation and generated-script publication have one durable winner."""
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import book_state_transaction as books
import core
from routers import script
from script_selection_binding import capture_generation_selection, publish_generated_script


class PublicationCancellationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            source = root / 'source.txt'; source.write_text('One line.')
            old = b'[{"text":"previous"}]'
            (root / 'annotated_script.json').write_bytes(old)
            run_id = 'a' * 32
            (root / 'state.json').write_text(json.dumps({
                'input_file_path':str(source), 'active_book_id':'book',
                'book_generation':'generation', 'script_generation_run_id':run_id,
                'script_generation_base_sha256':hashlib.sha256(old).hexdigest()}))
            states = copy.deepcopy(core.process_state)
            states['script'].update(running=True, cancel=False, process=None, pid=None)
            claims = {'script':{'id':run_id, 'phase':'started'}}
            for module, name, value in ((script,'DATA_DIR',tmp), (script,'process_state',states),
                                        (core,'process_state',states), (core,'_task_claims',claims)):
                stack.enter_context(patch.object(module, name, value))
            selection = capture_generation_selection(tmp, str(source))
            def publish():
                return publish_generated_script(tmp, selection['book_token'], str(source),
                    selection['source_sha256'], b'[{"text":"One line."}]', expected_run_id=run_id)
            yield root, states, old, publish

    def test_cancel_wins_and_prevents_late_publication(self):
        with self.fixture() as (root, states, old, publish):
            self.assertEqual('cancel queued', script._cancel_script_generation_sync()['status'])
            self.assertTrue(states['script']['cancel'])
            with self.assertRaisesRegex(ValueError, 'no longer current'):
                publish()
            self.assertEqual(old, (root / 'annotated_script.json').read_bytes())

    def test_already_published_cannot_be_reported_as_cancelled(self):
        with self.fixture() as (root, states, old, publish):
            publish()
            result = script._cancel_script_generation_sync()
            self.assertEqual('published_cleanup_stop', result['status'])
            self.assertTrue(states['script']['cancel'])
            self.assertTrue(states['script']['_script_published_cleanup_stop'])
            self.assertEqual('a' * 32, json.loads((root / 'state.json').read_text())['script_generation_published_run_id'])

    def test_cancel_waits_for_publication_transaction_and_reports_the_result(self):
        with self.fixture() as (root, states, old, publish):
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            move = books._move; outcomes = []
            def pause(source, destination, **kwargs):
                result = move(source, destination, **kwargs)
                if Path(destination) == root / 'annotated_script.json':
                    entered.set()
                    if not release.wait(5): raise RuntimeError('test timeout')
                return result
            def cancel():
                try: outcomes.append(script._cancel_script_generation_sync())
                except Exception as error: outcomes.append(error)
                finally: finished.set()
            with patch.object(books, '_move', side_effect=pause):
                worker = threading.Thread(target=publish); worker.start()
                self.assertTrue(entered.wait(5))
                caller = threading.Thread(target=cancel); caller.start()
                try:
                    self.assertFalse(finished.wait(.1))
                finally:
                    release.set(); worker.join(5); caller.join(5)
            self.assertFalse(worker.is_alive()); self.assertFalse(caller.is_alive())
            self.assertEqual(1, len(outcomes)); self.assertEqual('published_cleanup_stop', outcomes[0]['status'])
            self.assertTrue(states['script']['cancel'])
            self.assertTrue(states['script']['_script_published_cleanup_stop'])
            self.assertEqual(b'[{"text":"One line."}]', (root / 'annotated_script.json').read_bytes())

    def test_failed_revoke_does_not_signal_or_change_run(self):
        with self.fixture() as (root, states, old, publish):
            with patch.object(script, 'atomic_json_write', side_effect=OSError('disk failure')), \
                    patch.object(script, '_cancel_task') as cancel:
                with self.assertRaises(HTTPException) as refused:
                    script._cancel_script_generation_sync()
                self.assertEqual(409, refused.exception.status_code)
                cancel.assert_not_called()
            self.assertFalse(states['script']['cancel'])
            self.assertEqual('a' * 32, json.loads((root / 'state.json').read_text())['script_generation_run_id'])
            publish()

    def test_process_death_during_commit_recovers_before_cancel(self):
        worker = '''import os,sys
from pathlib import Path
import book_state_transaction as books
from script_selection_binding import capture_generation_selection,publish_generated_script
root=Path(sys.argv[1]); source=root/'source.txt'; selected=capture_generation_selection(str(root),str(source))
move=books._move
def die(src,dst, **kwargs):
 result=move(src,dst, **kwargs)
 if Path(dst)==root/'annotated_script.json': os._exit(77)
 return result
books._move=die
publish_generated_script(str(root),selected['book_token'],str(source),selected['source_sha256'],b'[{"text":"One line."}]',expected_run_id='a'*32)
'''
        with self.fixture() as (root, states, old, publish):
            result = subprocess.run([sys.executable, '-c', worker, str(root)],
                                    capture_output=True, timeout=15, env=os.environ.copy())
            self.assertEqual(77, result.returncode, result.stderr.decode())
            self.assertTrue((root / books.JOURNAL).exists())
            self.assertEqual('cancel queued', script._cancel_script_generation_sync()['status'])
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertEqual(old, (root / 'annotated_script.json').read_bytes())
            self.assertTrue(states['script']['cancel'])
            self.assertNotIn('script_generation_run_id', json.loads((root / 'state.json').read_text()))

    def test_process_death_after_commit_is_not_reported_as_cancelled(self):
        worker = '''import os,sys
from pathlib import Path
import book_state_transaction as books
from script_selection_binding import capture_generation_selection,publish_generated_script
root=Path(sys.argv[1]); source=root/'source.txt'; selected=capture_generation_selection(str(root),str(source))
books._finish_transaction=lambda *args,**kwargs: os._exit(78)
publish_generated_script(str(root),selected['book_token'],str(source),selected['source_sha256'],b'[{"text":"One line."}]',expected_run_id='a'*32)
'''
        with self.fixture() as (root, states, old, publish):
            result = subprocess.run([sys.executable, '-c', worker, str(root)],
                                    capture_output=True, timeout=15, env=os.environ.copy())
            self.assertEqual(78, result.returncode, result.stderr.decode())
            self.assertTrue((root / books.JOURNAL).exists())
            self.assertEqual('published_cleanup_stop', script._cancel_script_generation_sync()['status'])
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertEqual(b'[{"text":"One line."}]', (root / 'annotated_script.json').read_bytes())
            self.assertTrue(states['script']['cancel'])

    def test_published_worker_and_descendant_stop_without_cancelling_the_script(self):
        worker = '''import subprocess,sys,time
from pathlib import Path
from script_selection_binding import capture_generation_selection,publish_generated_script
root=Path(sys.argv[1]); source=root/'source.txt'; selected=capture_generation_selection(str(root),str(source))
publish_generated_script(str(root),selected['book_token'],str(source),selected['source_sha256'],b'[{"text":"One line."}]',expected_run_id='a'*32)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])
(root/'worker-ready').write_text(str(child.pid))
sys.exit(0)
'''
        with self.fixture() as (root, states, old, publish):
            log = root / 'task.log'
            app = str(Path(__file__).resolve().parents[1])
            with patch.object(core, '_init_task_log', return_value=str(log)), \
                    patch.dict(os.environ, {'PYTHONPATH':app}):
                thread = threading.Thread(target=core.run_process,
                    args=([sys.executable, '-u', '-c', worker, str(root)], 'script', app), daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 10
                    while not (root / 'worker-ready').exists() and time.monotonic() < deadline:
                        if not thread.is_alive(): break
                        time.sleep(.02)
                    self.assertTrue((root / 'worker-ready').exists(), str(states['script']['logs']))
                    self.assertEqual('published_cleanup_stop', script._cancel_script_generation_sync()['status'])
                    thread.join(12)
                    self.assertFalse(thread.is_alive(), 'published worker and descendant did not stop')
                    self.assertFalse(states['script']['running'])
                    self.assertTrue(any('completed successfully' in line for line in states['script']['logs']))
                    self.assertFalse(any('Task script cancelled.' in line for line in states['script']['logs']))
                    self.assertEqual(b'[{"text":"One line."}]', (root / 'annotated_script.json').read_bytes())
                finally:
                    if thread.is_alive():
                        states['script']['cancel'] = True
                        thread.join(12)

    def test_cleanup_error_does_not_reclassify_a_committed_script(self):
        with self.fixture() as (root, states, old, publish):
            publish()
            states['script']['status'] = 'running'
            def fail_after_stop(*args, **kwargs):
                self.assertEqual('published_cleanup_stop', script._cancel_script_generation_sync()['status'])
                raise OSError('worker cleanup failed')
            with patch.object(core, '_init_task_log', return_value=str(root/'task.log')), \
                    patch.object(core, '_stream_subprocess_to_logs', side_effect=fail_after_stop):
                core.run_process(['stand-in'], 'script')
            self.assertEqual('done', states['script']['status'])
            self.assertTrue(any('worker cleanup reported an error' in line for line in states['script']['logs']))

    def test_signal_failure_after_publication_is_not_reported_as_a_stopped_worker(self):
        with self.fixture() as (root, states, old, publish):
            publish()
            states['script']['pid'] = 12345
            with patch.object(core, '_send_signal_tree', side_effect=OSError('signal denied')):
                with self.assertRaises(HTTPException) as refused:
                    script._cancel_script_generation_sync()
            self.assertEqual(409, refused.exception.status_code)
            self.assertIn('stopping its worker was not confirmed', refused.exception.detail)
            self.assertTrue(states['script']['running'])
            self.assertEqual(b'[{"text":"One line."}]', (root / 'annotated_script.json').read_bytes())

    def test_ui_explains_that_published_script_was_kept(self):
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'); const hint={hidden:true,textContent:''};const button={disabled:false};
const c={document:{getElementById:id=>id==='script-cancellation-status'?hint:button},API:{post:async()=>({status:'published_cleanup_stop'})},_resetPauseBtn:()=>{},showToast:()=>{}};vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('async function cancelTask('),source.indexOf('// Same "unknown error" fallback')),c);
(async()=>{assert(await c.requestScriptCancellation('script','/cancel','button','pause','script generation'));assert.match(hint.textContent,/already published/);assert.match(hint.textContent,/worker to stop/);})().catch(e=>{console.error(e);process.exit(1)});'''
        app_core = Path(__file__).resolve().parents[1] / 'static/js/app-core.js'
        result = subprocess.run(['node', '-e', code, str(app_core)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_run_history_records_published_cleanup_stop_as_completed(self):
        with self.fixture() as (root, states, old, publish):
            def callback():
                states['script']['cancel'] = True
                states['script']['_script_published_cleanup_stop'] = True
            with patch.object(core, 'prune_runs'), patch.object(core, 'start_run', return_value='fixture'), \
                    patch.object(core, 'finish_run') as finish:
                core._run_claimed_background_task('script', callback)
            self.assertEqual('completed', finish.call_args.args[2])

    def test_later_cancelled_script_slot_task_does_not_inherit_completion(self):
        with self.fixture() as (root, states, old, publish), patch.object(core, '_gpu_leases', {}):
            states['script']['_script_published_cleanup_stop'] = True
            self.assertTrue(core.release_gpu_task_claim('script', 'a'*32))
            with patch.object(core, 'check_global_gpu_lock'), patch.object(core, 'acquire_task_lease') as lease, \
                    patch.object(core, 'prune_runs'), patch.object(core, 'start_run', return_value='next'), \
                    patch.object(core, 'finish_run') as finish:
                claim = core.claim_gpu_task('script', cpu_only=True)
                self.assertNotIn('_script_published_cleanup_stop', states['script'])
                def callback(): states['script']['cancel'] = True
                core._run_claimed_background_task('script', callback)
                self.assertEqual('cancelled', finish.call_args.args[2])
                self.assertNotIn('script', core._task_claims)
                lease.return_value.close.assert_called_once()
