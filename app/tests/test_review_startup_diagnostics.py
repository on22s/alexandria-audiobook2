"""Real review admission and download diagnostics without model calls."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import core
from routers import script

class ReviewStartupDiagnosticsTests(unittest.TestCase):
    def test_admission_failure_has_durable_download_without_truncating_active_log(self):
        for endpoint, payload in (('/api/review_script', {}), ('/api/review_script_contextual', {'window_size': 4})):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                previous = Path(tmp, 'review-latest.log')
                previous.write_text('existing owned run\n', encoding='utf-8')
                app = FastAPI(); app.include_router(script.router)
                with patch.object(core, 'API_LOG_DIR', tmp), patch.object(script, '_require_published_script', side_effect=HTTPException(409, 'private source path and credentials')), patch.object(script, '_init_task_log') as truncate, TestClient(app) as client:
                    response = client.post(endpoint, json=payload)
                    self.assertEqual(409, response.status_code)
                    log = client.get('/api/logs/review?download=true')
                self.assertEqual(200, log.status_code)
                self.assertIn('admission_received', log.text)
                self.assertIn('admission_failed', log.text)
                self.assertNotIn('private source path', log.text)
                self.assertIn('existing owned run', log.text)
                self.assertEqual('existing owned run\n', previous.read_text(encoding='utf-8'))
                truncate.assert_not_called()

    def test_rejected_first_attempt_is_downloadable_before_worker_log_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=FastAPI(); app.include_router(script.router)
            with patch.object(core, 'API_LOG_DIR', tmp), patch.object(script, '_require_published_script', side_effect=HTTPException(400, 'no source')), TestClient(app) as client:
                self.assertEqual(400, client.post('/api/review_script').status_code)
                log=client.get('/api/logs/review?download=true')
            self.assertEqual(200, log.status_code)
            self.assertIn('HTTPException', log.text)


class ReviewStartupLifecycleTests(unittest.TestCase):
    def test_failed_worker_recheck_records_same_correlation_without_dispatch(self):
        import json
        from review_startup import ReviewStartupTrace
        with tempfile.TemporaryDirectory() as tmp:
            trace=ReviewStartupTrace(Path(tmp,'review-startup.log'))
            with patch.object(script,'_require_published_script', side_effect=HTTPException(409,'secret')), patch.object(script,'process_state',{'review':{}}), patch.object(script,'run_process') as dispatch:
                self.assertEqual(1,script._run_review_worker(['private-command'],'review',trace))
            records=[json.loads(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual({trace.correlation},{row['id'] for row in records})
            self.assertIn('publication_recheck_failed',[row['stage'] for row in records])
            self.assertNotIn('secret',trace.path.read_text())
            dispatch.assert_not_called()

    def test_batch_admission_failure_is_also_downloadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=FastAPI();app.include_router(script.router)
            with patch.object(core,'API_LOG_DIR',tmp),patch.object(script,'check_global_gpu_lock'),TestClient(app) as client:
                self.assertEqual(400,client.post('/api/review_script/batch/start',json={'script_names':[]}).status_code)
                log=client.get('/api/logs/batch_review?download=true')
            self.assertEqual(200,log.status_code)
            self.assertIn('admission_failed',log.text)

    def test_trace_is_bounded_and_excludes_arbitrary_options_and_error_messages(self):
        import json
        from review_startup import ReviewStartupTrace
        with tempfile.TemporaryDirectory() as tmp:
            trace=ReviewStartupTrace(Path(tmp,'trace.log'))
            with patch('review_startup.MAX_BYTES',900):
                for i in range(30):
                    trace.event('fixture',error=ValueError('private-secret'),entries=i,api_key='secret',source='book',entries_text='words')
            for path in (trace.path,Path(str(trace.path)+'.previous')):
                self.assertLessEqual(path.stat().st_size,900)
                self.assertNotIn('secret',path.read_text())
                for line in path.read_text().splitlines():
                    row=json.loads(line); self.assertEqual('ValueError',row['error_type'])
                    self.assertNotIn('api_key',row)

    def test_executor_handoff_preserves_trace_and_isolates_context(self):
        from concurrent.futures import ThreadPoolExecutor
        from review_startup import ReviewStartupTrace,CURRENT,bind_current,event
        with tempfile.TemporaryDirectory() as tmp:
            trace=ReviewStartupTrace(Path(tmp,'trace.log'))
            def run():
                with ThreadPoolExecutor(2) as executor:
                    list(executor.map(bind_current(lambda n:event('model_request_started',batch=n)),range(3)))
            trace.call('worker',run)
            self.assertEqual(3,trace.path.read_text().count('model_request_started'))
            self.assertIsNone(CURRENT.get())

    def test_real_cli_failure_records_imports_arguments_and_nonzero_exit(self):
        import json,os,subprocess,sys
        from review_startup import ENV_PATH,ENV_ID
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp,'trace.log'); correlation='a'*32
            env={**os.environ,'ALEXANDRIA_DATA_DIR':tmp,ENV_PATH:str(path),ENV_ID:correlation}
            result=subprocess.run([sys.executable,str(Path(script.BASE_DIR,'review_script.py')),'--input',str(Path(tmp,'private-missing-source.json'))],env=env,capture_output=True,timeout=40)
            self.assertNotEqual(0,result.returncode)
            records=[json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual({correlation},{row['id'] for row in records})
            self.assertTrue({'cli_imports_started','cli_imports_complete','arguments_parsed','cli_failed'} <= {row['stage'] for row in records})
            self.assertNotIn('private-missing-source',path.read_text())


class ReviewStartupProcessTests(unittest.TestCase):
    def test_real_streamed_worker_inherits_trace_and_records_completion(self):
        import json, sys
        from review_startup import ReviewStartupTrace
        with tempfile.TemporaryDirectory() as tmp:
            trace = ReviewStartupTrace(Path(tmp, 'trace.log'))
            command = [sys.executable, '-c', (
                "import os;from review_startup import ReviewStartupTrace,ENV_ID,ENV_PATH;"
                "ReviewStartupTrace(os.environ[ENV_PATH],os.environ[ENV_ID]).event('fixture_child');"
                "print('worker output')")]
            state = {'logs': [], 'cancel': False}
            result = trace.call('worker', core._stream_subprocess_to_logs,
                                command, script.BASE_DIR, state)
            self.assertEqual((0, ['worker output']), result)
            records = [json.loads(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual({trace.correlation}, {row['id'] for row in records})
            self.assertTrue({'subprocess_launch', 'fixture_child', 'subprocess_finished'}
                            <= {row['stage'] for row in records})
            child_id = next(row['child_pid'] for row in records if row['stage'] == 'subprocess_started')
            child_record = next(row for row in records if row['stage'] == 'fixture_child')
            # Native process ownership may interpose a supervisor as the direct child.
            self.assertIn(child_id, (child_record['pid'], child_record['parent_pid']))
            self.assertNotEqual(child_id, records[0]['pid'])
            self.assertEqual(0, next(row['exit_code'] for row in records
                                    if row['stage'] == 'subprocess_finished'))

    def test_concurrent_processes_write_complete_json_records(self):
        import json, subprocess, sys
        from review_startup import ReviewStartupTrace
        with tempfile.TemporaryDirectory() as tmp:
            trace = ReviewStartupTrace(Path(tmp, 'trace.log'))
            command = [sys.executable, '-c', (
                "import sys;from review_startup import ReviewStartupTrace;"
                "t=ReviewStartupTrace(sys.argv[1],sys.argv[2]);"
                "[t.event('fixture_child',entries=i) for i in range(10)]"),
                str(trace.path), trace.correlation]
            children = [subprocess.Popen(command, cwd=script.BASE_DIR, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE) for _ in range(3)]
            try:
                for child in children:
                    stdout, stderr = child.communicate(timeout=30)
                    self.assertEqual(0, child.returncode, stderr.decode(errors='replace'))
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.communicate()
            records = [json.loads(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual(30, len(records))
            self.assertEqual({trace.correlation}, {row['id'] for row in records})
            from collections import Counter
            self.assertEqual(Counter({n: 3 for n in range(10)}),
                             Counter(row['entries'] for row in records))


class ReviewStartupReadTests(unittest.TestCase):
    def test_download_includes_retained_previous_trace_after_rotation(self):
        from review_startup import ReviewStartupTrace
        with tempfile.TemporaryDirectory() as tmp:
            app = FastAPI(); app.include_router(script.router)
            trace = ReviewStartupTrace(Path(tmp, 'review-startup.log'))
            trace.event('admission_received')
            with patch('review_startup.MAX_BYTES', trace.path.stat().st_size * 2 + 32):
                trace.event('model_request_started')
                trace.event('cli_finished')
            self.assertTrue(Path(str(trace.path) + '.previous').is_file())
            with patch.object(core, 'API_LOG_DIR', tmp), TestClient(app) as client:
                log = client.get('/api/logs/review?download=true')
            self.assertEqual(200, log.status_code)
            self.assertIn('admission_received', log.text)
            self.assertIn('cli_finished', log.text)

    def test_trace_read_lock_failure_is_visible_without_leaking_error_message(self):
        from review_startup import read_trace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'review-startup.log'); path.write_text('record')
            with patch('review_startup.file_lock', side_effect=TimeoutError('private path')):
                result = read_trace(path)
            self.assertIn(b'TimeoutError', result)
            self.assertNotIn(b'private path', result)


    def test_import_failure_is_traced_before_application_imports_finish(self):
        import json, os, subprocess, sys
        from review_startup import ENV_PATH, ENV_ID
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp, 'trace.log')
            code = (
                "import builtins,runpy;original=builtins.__import__\n"
                "def import_checked(name,*args,**kwargs):\n"
                " if name=='chunk_status_journal':raise ImportError('private-module-message')\n"
                " return original(name,*args,**kwargs)\n"
                "builtins.__import__=import_checked\n"
                "runpy.run_path('review_script.py',run_name='__main__')")
            env = {**os.environ, 'ALEXANDRIA_DATA_DIR': tmp, ENV_PATH: str(trace), ENV_ID: 'b' * 32}
            result = subprocess.run([sys.executable, '-c', code], cwd=script.BASE_DIR,
                                    env=env, capture_output=True, timeout=30)
            self.assertNotEqual(0, result.returncode)
            records = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual(['cli_imports_started', 'cli_failed'], [row['stage'] for row in records])
            self.assertEqual('ImportError', records[-1]['error_type'])
            self.assertNotIn('private-module-message', trace.read_text())


class ReviewLogModeMarkupTests(unittest.TestCase):
    def test_each_review_mode_contains_its_own_always_available_log_link(self):
        from html.parser import HTMLParser
        class Links(HTMLParser):
            def __init__(self):
                super().__init__(); self.stack = []; self.links = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'a':
                    self.links.append((attrs.get('href'), tuple(self.stack)))
                if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
                    self.stack.append(attrs.get('id'))
            def handle_endtag(self, tag):
                if self.stack:
                    self.stack.pop()
        parser = Links()
        parser.feed((Path(script.BASE_DIR) / 'static/index.html').read_text(encoding='utf-8'))
        for mode, task in (('single', 'review'), ('batch', 'batch_review')):
            self.assertTrue(any(href == f'/api/logs/{task}?download=true'
                and f'review-{mode}-area' in ancestors for href, ancestors in parser.links), mode)
