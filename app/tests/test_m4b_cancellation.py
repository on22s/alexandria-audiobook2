import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks
from routers import editor
from tests.test_chapter_export import _project


class M4bCancellationTests(unittest.TestCase):
    def test_router_forwards_cancel_and_progress_to_native_manager(self):
        state = {'running': True, 'logs': [], 'cancel': False}
        queued = []
        captured = {}
        def schedule(background, name, task):
            queued.append(task)
        def merge(**kwargs):
            captured.update(kwargs)
            return False, 'Export cancelled'
        with patch.dict(editor.process_state, {'m4b_export': state}), \
                patch.object(editor, 'schedule_claimed_background_task', side_effect=schedule), \
                patch.object(editor.project_manager, 'merge_m4b', side_effect=merge):
            asyncio.run(editor.merge_m4b_endpoint(editor.M4bExportRequest(), BackgroundTasks()))
            queued[0]()
            self.assertIn('cancel_check', captured)
            self.assertIn('progress_callback', captured)
            self.assertFalse(captured['cancel_check']())
            state['cancel'] = True
            self.assertTrue(captured['cancel_check']())
            captured['progress_callback']('Encoding M4B: 1.0/2.0 s; elapsed 0.1 s')
            self.assertIn('Encoding M4B:', state['logs'][-1])
            self.assertEqual('cancelled', state['result']['status'])

    def test_native_http_cancel_keeps_claim_until_worker_finishes(self):
        import threading
        import core
        from tests.test_export_task_results import ExportTaskResultTests
        with ExportTaskResultTests().fixture() as (root, states, client):
            pm, chunks = _project(root); pm.save_chunks(chunks)
            output = Path(root) / 'audiobook.m4b'; output.write_bytes(b'previous complete')
            entered, release = threading.Event(), threading.Event(); responses = []
            load = pm._load_chunks_with_audio
            def held_load(**kwargs):
                entered.set()
                self.assertTrue(release.wait(3))
                return load(**kwargs)
            self.assertEqual(400, client.post('/api/merge_m4b/cancel').status_code)
            with patch.object(editor, 'project_manager', pm), patch.object(pm, '_load_chunks_with_audio', side_effect=held_load):
                worker = threading.Thread(target=lambda: responses.append(client.post('/api/merge_m4b', json={})))
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(200, client.post('/api/merge_m4b/cancel').status_code)
                    self.assertTrue(states['m4b_export']['running'])
                    self.assertIn('m4b_export', core._task_claims)
                    self.assertEqual(400, client.post('/api/merge_m4b', json={}).status_code)
                finally:
                    release.set(); worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
            self.assertEqual(200, responses[0].status_code)
            self.assertEqual({'status':'cancelled', 'message':'Export cancelled'}, states['m4b_export']['result'])
            self.assertFalse(states['m4b_export']['running'])
            self.assertFalse(core._task_claims)
            self.assertEqual(b'previous complete', output.read_bytes())
            self.assertEqual(400, client.post('/api/merge_m4b/cancel').status_code)

    def test_cancel_after_encoder_exit_refuses_publication_and_failure_retains_stderr(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); pm, chunks = _project(tmp); pm.save_chunks(chunks)
                output = root / 'audiobook.m4b'; output.write_bytes(b'previous complete')
                cancelled = False; messages = []
                def encode(command, *args):
                    nonlocal cancelled
                    Path(command[-1]).write_bytes(b'new unpublished encode')
                    cancelled = not failure
                    return (7, 'actual encoder error') if failure else (0, '')
                with patch('m4b_encode.encode_m4b', side_effect=encode):
                    result = pm.merge_m4b(cancel_check=lambda: cancelled, progress_callback=messages.append)
                self.assertEqual((False, 'FFmpeg failed (exit 7)' if failure else 'Export cancelled'), result)
                if failure: self.assertIn('FFmpeg stderr: actual encoder error', messages)
                self.assertEqual(b'previous complete', output.read_bytes())
                self.assertFalse(list(root.glob('.m4b*')))
                self.assertFalse(list(root.glob('audiobook.m4b.pending.*')))

    def test_prelaunch_cancellation_preserves_prior_output_without_starting_encoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pm, chunks = _project(tmp); pm.save_chunks(chunks)
            output = root / 'audiobook.m4b'; output.write_bytes(b'prior complete')
            with patch('m4b_encode.start_owned_subprocess') as launch:
                self.assertEqual((False, 'Export cancelled'), pm.merge_m4b(cancel_check=lambda: True))
            launch.assert_not_called()
            self.assertEqual(b'prior complete', output.read_bytes())
            self.assertFalse(list(root.glob('.m4b*')))

    @unittest.skipUnless(sys.platform == 'linux', 'native Linux descendant reaping test')
    def test_real_encoder_cancel_reaps_descendants_and_preserves_prior_output(self):
        import m4b_encode
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pm, chunks = _project(tmp); pm.save_chunks(chunks)
            output = root / 'audiobook.m4b'; output.write_bytes(b'prior complete')
            marker = root / 'pids.json'; processes = []
            start = m4b_encode.start_owned_subprocess
            program = '''import json,os,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
open(sys.argv[1],'wb').write(b'incomplete new output')
open(sys.argv[2],'w').write('out_time_us=1000000\\nprogress=continue\\n')
open(sys.argv[3],'w').write(json.dumps([os.getpid(),child.pid]))
time.sleep(30)
'''
            def launch(command, **kwargs):
                process = start([sys.executable, '-c', program, command[-1],
                                 command[command.index('-progress') + 1], str(marker)], **kwargs)
                processes.append(process)
                return process
            started = time.monotonic()
            def cancel():
                if time.monotonic() - started > 10:
                    self.fail('encoder never reached native cancellation fixture')
                return marker.exists()
            with patch.object(m4b_encode, 'start_owned_subprocess', side_effect=launch):
                result = pm.merge_m4b(cancel_check=cancel)
            self.assertEqual((False, 'Export cancelled'), result)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIsNotNone(processes[0].poll())
            for pid in json.loads(marker.read_text()):
                self.assertFalse(Path('/proc', str(pid)).exists())
            self.assertEqual(b'prior complete', output.read_bytes())
            self.assertFalse(list(root.glob('.m4b*')))
            self.assertFalse(list(root.glob('audiobook.m4b.pending.*')))

    def test_progress_precedes_completion_and_failure_reports_stderr_tail(self):
        import m4b_encode
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / 'pending.m4b'; messages = []; processes = []
            start = m4b_encode.start_owned_subprocess
            program = '''import sys,time
open(sys.argv[1],'w').write('out_time_us=N/A\\nout_time_us=1500000\\nprogress=continue\\n')
time.sleep(.4)
sys.stderr.write('x'*6000+'native encode failure')
sys.exit(7)
'''
            def launch(command, **kwargs):
                process = start([sys.executable, '-c', program, command[command.index('-progress') + 1]], **kwargs)
                processes.append(process)
                return process
            def progress(message):
                self.assertIsNone(processes[0].poll())
                messages.append(message)
            with patch.object(m4b_encode, 'start_owned_subprocess', side_effect=launch):
                result, errors = m4b_encode.encode_m4b(['ffmpeg', str(output)], 3, progress_callback=progress)
            self.assertEqual(7, result)
            self.assertEqual(1, len(messages)); self.assertIn('1.5/3.0 s', messages[0])
            self.assertTrue(errors.endswith('native encode failure'))
            self.assertLessEqual(len(errors), 4096)
            self.assertFalse(list(root.glob('.m4b*')))

    def test_timeout_and_progress_callback_failure_reap_real_encoder(self):
        import m4b_encode
        for reason in ('timeout', 'callback'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); processes = []; start = m4b_encode.start_owned_subprocess
                def launch(command, **kwargs):
                    program = "import sys,time;open(sys.argv[1],'w').write('out_time_us=1000000\\n');time.sleep(30)"
                    process = start([sys.executable, '-c', program, command[command.index('-progress') + 1]], **kwargs)
                    processes.append(process)
                    return process
                def progress(message):
                    if reason == 'callback': raise RuntimeError('progress callback failed')
                error = subprocess.TimeoutExpired if reason == 'timeout' else RuntimeError
                with patch.object(m4b_encode, 'start_owned_subprocess', side_effect=launch):
                    with self.assertRaises(error):
                        m4b_encode.encode_m4b(['ffmpeg', str(root/'pending.m4b')], 2,
                                              progress_callback=progress, timeout=.3 if reason == 'timeout' else 5)
                self.assertIsNotNone(processes[0].poll())
                self.assertFalse(list(root.glob('.m4b*')))
