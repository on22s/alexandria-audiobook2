import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import core
import utils
import review_startup


SECRET = "SECRET_SENTINEL_7654321"
BOOK_TEXT = "SYNTHETIC_BOOK_TEXT_DO_NOT_LOG"
PRIVATE = SECRET + " " + BOOK_TEXT


class BugReportLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict("os.environ", {"ALEXANDRIA_DATA_DIR": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def assert_private(self, value):
        self.assertNotIn(SECRET, value)
        self.assertNotIn(BOOK_TEXT, value)
        self.assertNotIn(str(self.root), value)

    @contextmanager
    def capture_failure(self, stage, task, resource=None, error_type=None):
        path = review_startup.get_failure_log_path()
        before = len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0
        with self.assertLogs('AlexandriaDiagnostics', level='WARNING') as captured:
            yield
        output = '\n'.join(captured.output)
        self.assert_private(output)
        self.assertTrue(all(record.exc_info is None for record in captured.records))
        records = [json.loads(record.getMessage()) for record in captured.records]
        matches = [record for record in records if record['stage'] == stage]
        self.assertEqual(1, len(matches), output)
        record = matches[0]
        self.assertEqual(task, record['task'])
        if resource is not None:
            self.assertEqual(hashlib.sha256(os.fsencode(resource)).hexdigest()[:16], record['resource_id'])
        if error_type is not None:
            self.assertEqual(error_type, record['error_type'])
            self.assertTrue(record['traceback'])
            for frame in record['traceback']:
                self.assertNotIn('/', frame['file'])
                self.assertNotIn('\\', frame['file'])
                self.assertGreater(frame['line'], 0)
        durable = path.read_text(encoding='utf-8')
        self.assert_private(durable)
        self.assertEqual(records, [json.loads(line) for line in durable.splitlines()[before:]])

    def run_worker(self, **kwargs):
        state = {'logs': [], 'cancel': False, 'process': None, 'pid': None}
        with patch.dict(core.process_state, {'training': state}):
            result = core._stream_subprocess_to_logs(
                [sys.executable, '-c', "print('synthetic worker output')"], str(self.root), state,
                log_file=str(self.root / (SECRET + '.log')), **kwargs)
        self.assertIsNone(state['process'])
        return result, state

    def test_json_corruption_records_context_without_exception_payload(self):
        target = self.root / (SECRET + ".json")
        target.write_text("{" + BOOK_TEXT, encoding="utf-8")
        with self.assertLogs("AlexandriaDiagnostics", level="WARNING") as captured, self.assertLogs(utils.logger) as legacy:
            self.assertEqual({}, utils.safe_load_json(target, {}))
        self.assert_private('\n'.join(legacy.output))
        output = "\n".join(captured.output)
        self.assertIn("json_load_failed", output)
        self.assertIn("JSONDecodeError", output)
        self.assertIn("safe_load_json", output)
        self.assertIn("resource_id", output)
        self.assert_private(output)

    def test_json_unreadable_preserves_default_and_records_stack(self):
        target = self.root / SECRET
        target.mkdir()
        with self.assertLogs(utils.logger) as legacy, self.capture_failure('json_load_failed', 'storage', target, 'PermissionError' if sys.platform == 'win32' else 'IsADirectoryError'):
            self.assertEqual([], utils.safe_load_json(target, []))
        self.assert_private('\n'.join(legacy.output))

    def test_json_shape_warning_is_safe_and_durable(self):
        target = self.root / SECRET
        target.write_text(json.dumps([PRIVATE]), encoding='utf-8')
        with self.assertLogs(utils.logger) as legacy, self.capture_failure('json_shape_invalid', 'storage', target):
            self.assertEqual({}, utils.safe_load_json(target, {}))
        self.assertIn('expected dict, got list', '\n'.join(legacy.output))
        self.assert_private('\n'.join(legacy.output))

    def test_missing_json_is_not_an_incident(self):
        self.assertIsNone(utils.safe_load_json(self.root / SECRET))
        self.assertFalse(review_startup.get_failure_log_path().exists())

    def test_worker_log_open_failure_keeps_execution(self):
        resource = self.root / (SECRET + '.log')
        with patch.object(core, 'open', side_effect=OSError(PRIVATE), create=True), self.capture_failure('worker_log_open_failed', 'training', resource, 'OSError'):
            (code, lines), state = self.run_worker()
        self.assertEqual(0, code)
        self.assertEqual(['synthetic worker output'], lines)
        self.assertEqual(lines, state['logs'])

    def test_worker_log_write_failure_keeps_safe_ui_warning(self):
        handle = Mock()
        handle.write.side_effect = OSError(PRIVATE)
        resource = self.root / (SECRET + '.log')
        with patch.object(core, 'open', return_value=handle, create=True), self.capture_failure('worker_log_write_failed', 'training', resource, 'OSError'):
            (code, lines), state = self.run_worker()
        self.assertEqual(0, code)
        self.assertIn('WARNING: Log file write failed (OSError)', '\n'.join(state['logs']))
        self.assert_private('\n'.join(state['logs']))
        handle.close.assert_called_once()

    def test_worker_launch_failure_preserves_error(self):
        resource = self.root / (SECRET + '.log')
        with patch.object(core, 'start_owned_subprocess', side_effect=OSError(PRIVATE)), self.capture_failure('worker_stream_failed', 'training', resource, 'OSError'):
            with self.assertRaisesRegex(OSError, SECRET):
                self.run_worker()

    def test_worker_reader_failure_records_thread_stack(self):
        class BrokenReader:
            def __iter__(self):
                raise OSError(PRIVATE)

            def close(self):
                pass

        process = Mock(pid=2345, returncode=0, stdout=BrokenReader(), _alexandria_windows_owner=None, _alexandria_control=None)
        resource = self.root / (SECRET + '.log')
        with patch.object(core, 'start_owned_subprocess', return_value=process), self.capture_failure('worker_reader_failed', 'training', resource, 'OSError'):
            (code, lines), state = self.run_worker()
        self.assertEqual((0, []), (code, lines))

    def test_nonzero_worker_exit_has_context_without_copying_stderr(self):
        state = {'logs': [], 'cancel': False}
        command = [sys.executable, '-c', 'import sys;sys.stderr.write(' + repr(PRIVATE) + ');sys.exit(7)']
        with patch.dict(core.process_state, {'training': state}), self.capture_failure('worker_exit_failed', 'training'):
            code, lines = core._stream_subprocess_to_logs(command, str(self.root), state)
        self.assertEqual(7, code)
        self.assertIn(SECRET, ''.join(lines))
        record = json.loads(review_startup.get_failure_log_path().read_text())
        self.assertEqual(7, record['exit_code'])

    def test_llm_models_failure_has_context(self):
        from routers import system
        import llm_provider
        request = system.LlmModelsRequest(base_url='http://127.0.0.1:1234/v1', api_key=SECRET)
        client = Mock()
        client.models.list.side_effect = RuntimeError(PRIVATE)
        with patch.object(llm_provider, 'make_llm_client', return_value=client), patch.object(system, '_normalize_and_validate_llm', return_value=request), self.capture_failure('llm_models_failed', 'llm', request.base_url, 'RuntimeError'):
            result = asyncio.run(system.llm_models(request))
        self.assertEqual([], result['models'])

    def test_llm_diagnostic_write_failure_is_not_silent(self):
        from routers import system
        with patch.object(system, 'open', side_effect=OSError(PRIVATE), create=True), self.capture_failure('llm_log_write_failed', 'llm', error_type='OSError'):
            self.assertEqual('', system._log_llm_failure('test', PRIVATE))

    def test_voicelab_probe_launch_timeout_and_decode_failures(self):
        from routers import voicelab
        errors = [OSError(PRIVATE), subprocess.TimeoutExpired([PRIVATE], 20)]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(voicelab, '_rocm_env', return_value={}), patch.object(voicelab.subprocess, 'run', side_effect=error), self.capture_failure('voicelab_probe_failed', 'voicelab', SECRET, type(error).__name__):
                self.assertEqual({}, voicelab._probe_voicelab_interpreter(SECRET))
        result = SimpleNamespace(returncode=0, stdout='{' + PRIVATE)
        with patch.object(voicelab, '_rocm_env', return_value={}), patch.object(voicelab.subprocess, 'run', return_value=result), self.capture_failure('voicelab_probe_failed', 'voicelab', SECRET, 'JSONDecodeError'):
            self.assertEqual({}, voicelab._probe_voicelab_interpreter(SECRET))

    def test_voicelab_probe_exit_and_empty_output(self):
        from routers import voicelab
        for code in (0, 9):
            result = SimpleNamespace(returncode=code, stdout='', stderr=PRIVATE)
            with self.subTest(code=code), patch.object(voicelab, '_rocm_env', return_value={}), patch.object(voicelab.subprocess, 'run', return_value=result), self.capture_failure('voicelab_probe_unavailable', 'voicelab', SECRET):
                self.assertEqual({}, voicelab._probe_voicelab_interpreter(SECRET))

    def test_speaker_review_outer_failure_is_durable(self):
        from routers import speaker_review
        run_id = hashlib.sha256(PRIVATE.encode()).hexdigest()
        with patch.object(core, 'DATA_DIR', str(self.root)), patch.object(speaker_review, '_read_run', side_effect=OSError(PRIVATE)), self.capture_failure('speaker_review_failed', 'speaker_review', run_id, 'OSError'):
            speaker_review._run_review(run_id, {'api_key': SECRET})

    def test_background_failure_retains_history_and_releases_state(self):
        state = {'logs': [], 'running': True, 'status': 'running', 'process': None, 'pid': None}

        def fail_callback():
            raise RuntimeError(PRIVATE)

        with patch.dict(core.process_state, {'training': state}), patch.dict(core._task_claims, {}, clear=True), patch.object(core, 'prune_runs'), patch.object(core, 'start_run', return_value=SECRET), patch.object(core, 'finish_run') as finish, self.capture_failure('background_task_failed', 'training', SECRET, 'RuntimeError'):
            core._run_claimed_background_task('training', fail_callback)
        self.assertFalse(state['running'])
        self.assertEqual('failed', state['status'])
        self.assertNotIn('run_id', state)
        finish.assert_called_once_with(core.RUN_HISTORY_DIR, SECRET, 'failed', error=PRIVATE)

    def make_http_app(self):
        import app as api
        from starlette.exceptions import HTTPException as StarletteHTTPException
        application = FastAPI()
        for exception_type in (core.AppError, StarletteHTTPException, Exception):
            application.add_exception_handler(exception_type, api.app.exception_handlers[exception_type])

        @application.get('/failure/{resource}')
        def diagnostic_fixture(resource: str):
            if resource == 'internal':
                raise RuntimeError(PRIVATE)
            if resource == 'localized':
                raise core.AppError(409, 'legacy detail', key='synthetic.key', values={'count': 1}, headers={'X-Fixture': 'kept'})
            raise HTTPException(403, PRIVATE, headers={'X-Fixture': 'kept'})

        return application

    def test_http_refusal_records_no_request_or_detail_payload(self):
        application = self.make_http_app()
        with TestClient(application) as client, self.capture_failure('http_request_refused', 'http', error_type='HTTPException'):
            response = client.get('/failure/' + SECRET, params={'text': BOOK_TEXT}, headers={'Authorization': 'Bearer ' + SECRET})
        self.assertEqual(403, response.status_code)
        self.assertEqual(PRIVATE, response.json()['detail'])
        self.assertEqual('kept', response.headers['X-Fixture'])
        record = json.loads(review_startup.get_failure_log_path().read_text())
        self.assertEqual('diagnostic_fixture', record['operation'])
        self.assertEqual(403, record['status_code'])

    def test_localized_http_response_stays_byte_compatible(self):
        with TestClient(self.make_http_app()) as client, self.capture_failure('http_request_refused', 'http', error_type='AppError'):
            response = client.get('/failure/localized')
        self.assertEqual(409, response.status_code)
        self.assertEqual({'detail': 'legacy detail', 'message_key': 'synthetic.key', 'message_values': {'count': 1}}, response.json())
        self.assertEqual('kept', response.headers['X-Fixture'])

    def test_unhandled_http_failure_retains_default_500_response(self):
        with TestClient(self.make_http_app(), raise_server_exceptions=False) as client, self.capture_failure('http_request_failed', 'http', error_type='RuntimeError'):
            response = client.get('/failure/internal')
        self.assertEqual(500, response.status_code)
        self.assertEqual('Internal Server Error', response.text)

    def test_trace_rotation_is_bounded_and_downloads_both_generations(self):
        from routers import script
        application = FastAPI()
        application.include_router(script.router)
        with patch.object(review_startup, 'MAX_BYTES', 700), self.assertLogs('AlexandriaDiagnostics'):
            for sequence in range(30):
                review_startup.record_failure('synthetic_failure', task='review', resource=PRIVATE, exit_code=sequence)
            for path in (review_startup.get_failure_log_path(), Path(str(review_startup.get_failure_log_path()) + '.previous')):
                self.assertLessEqual(path.stat().st_size, 700)
            with TestClient(application) as client:
                response = client.get('/api/logs/diagnostics?download=true')
        self.assertEqual(200, response.status_code)
        self.assertEqual('attachment; filename="diagnostics.log"', response.headers['Content-Disposition'])
        self.assert_private(response.text)
        self.assertEqual(29, json.loads(response.text.splitlines()[-1])['exit_code'])
        self.assertGreater(len(response.text.splitlines()), 1)

    def test_failure_download_is_discoverable_and_empty_state_is_explicit(self):
        from html.parser import HTMLParser
        from routers import script
        links = []

        class LinkParser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == 'a':
                    links.append(dict(attrs))

        LinkParser().feed(Path(core.STATIC_DIR, 'index.html').read_text(encoding='utf-8'))
        self.assertTrue(any(link.get('href') == '/api/logs/diagnostics?download=true' and 'download' in link for link in links))
        application = FastAPI()
        application.include_router(script.router)
        with TestClient(application) as client:
            response = client.get('/api/logs/diagnostics?download=true')
        self.assertEqual(200, response.status_code)
        self.assertIn('No failure diagnostics recorded yet', response.text)

    def test_diagnostic_disk_failure_still_has_safe_console_evidence(self):
        try:
            raise OSError(PRIVATE)
        except OSError as error:
            with patch.object(review_startup, 'file_lock', side_effect=OSError(PRIVATE)), self.assertLogs('AlexandriaDiagnostics') as captured, self.assertLogs('review_startup') as fallback:
                review_startup.record_failure('synthetic_failure', error, task='storage', resource=SECRET)
        self.assertIn('OSError', '\n'.join(captured.output))
        self.assertIn('could not be persisted', '\n'.join(fallback.output))
        self.assertIn('OSError', '\n'.join(fallback.output))
        self.assert_private('\n'.join(captured.output + fallback.output))

    def test_current_review_correlation_is_retained(self):
        trace = review_startup.ReviewStartupTrace(self.root / 'review-startup.log')
        token = review_startup.CURRENT.set(trace)
        try:
            with self.capture_failure('synthetic_failure', 'review', SECRET):
                review_startup.record_failure('synthetic_failure', task='review', resource=SECRET)
        finally:
            review_startup.CURRENT.reset(token)
        self.assertEqual(trace.correlation, json.loads(review_startup.get_failure_log_path().read_text())['id'])

    def test_exception_causes_retain_frames_but_not_messages(self):
        try:
            try:
                raise OSError(PRIVATE)
            except OSError as cause:
                raise RuntimeError(PRIVATE) from cause
        except RuntimeError as error:
            with self.capture_failure('synthetic_failure', 'storage', SECRET, 'RuntimeError'):
                review_startup.record_failure('synthetic_failure', error, task='storage', resource=SECRET)
        record = json.loads(review_startup.get_failure_log_path().read_text())
        self.assertEqual('OSError', record['causes'][0]['error_type'])
        self.assertTrue(record['causes'][0]['traceback'])

    def test_numeric_os_error_codes_are_preserved_without_filename(self):
        try:
            error = OSError(28, PRIVATE, str(self.root / SECRET))
            error.winerror = 32
            raise error
        except OSError as error:
            with self.capture_failure('synthetic_failure', 'storage', SECRET, 'OSError'):
                review_startup.record_failure('synthetic_failure', error, task='storage', resource=SECRET)
        record = json.loads(review_startup.get_failure_log_path().read_text())
        self.assertEqual(28, record['errno'])
        self.assertEqual(32, record['winerror'])

    def test_task_log_creation_failure_records_context(self):
        with patch.object(core, "_task_log_path", return_value=str(self.root / SECRET)), patch.object(core, "open", side_effect=OSError(PRIVATE), create=True):
            with self.assertLogs("AlexandriaDiagnostics", level="WARNING") as captured:
                core._init_task_log("review")
        output = "\n".join(captured.output)
        self.assertIn("task_log_create_failed", output)
        self.assertIn("review", output)
        self.assertIn("OSError", output)
        self.assert_private(output)
