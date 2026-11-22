import asyncio
from contextlib import contextmanager
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
import review_startup
from project import ProjectManager
from routers import lora, system


PRIVATE = 'SECRET_TOKEN_968314 SYNTHETIC_PRIVATE_BOOK_TEXT'


class DeferredFailureLoggingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        if hasattr(review_startup, '_failure_limits'):
            review_startup._failure_limits.clear()
            self.addCleanup(review_startup._failure_limits.clear)

    def records(self):
        text = review_startup.read_trace(review_startup.get_failure_log_path()).decode()
        self.assertNotIn(PRIVATE, text)
        self.assertNotIn(str(self.root), text)
        return [json.loads(line) for line in text.splitlines()]

    def assert_record(self, stage, error_type=None, resource=None):
        matches = [record for record in self.records() if record['stage'] == stage]
        self.assertEqual(1, len(matches), matches)
        record = matches[0]
        self.assertIn('task', record)
        if error_type:
            self.assertEqual(error_type, record['error_type'])
            if stage == 'llm_test_failed':
                self.assertNotIn('traceback', record)
            else:
                self.assertTrue(record['traceback'])
        if resource:
            self.assertEqual(hashlib.sha256(os.fsencode(resource)).hexdigest()[:16], record['resource_id'])
        return record

    @contextmanager
    def failing_diagnostic_sink(self, fallback):
        console = io.StringIO()
        console.close()
        logger = logging.getLogger('AlexandriaDiagnostics')
        handler = logging.StreamHandler(console)
        try:
            with patch.object(logger, 'handlers', [handler]), patch.object(logger, 'propagate', False), patch.object(logger, 'level', logging.DEBUG), patch.object(logging, 'raiseExceptions', True), patch('sys.stderr', console), patch('sys.__stderr__', fallback):
                yield
        finally:
            handler.close()

    def test_broken_console_preserves_both_conservative_gpu_fences(self):
        fallback = io.StringIO()
        with self.failing_diagnostic_sink(fallback):
            with patch('lmstudio_settings.get_active_llm_config', side_effect=RuntimeError(PRIVATE)):
                self.assertIs(True, core.is_llm_on_this_gpu({}))
            with patch.object(core, 'load_app_config', side_effect=OSError(PRIVATE)):
                self.assertIs(True, core.llm_is_on_this_gpu())
        self.assert_record('gpu_policy_read_failed', 'RuntimeError')
        self.assert_record('gpu_config_read_failed', 'OSError')
        self.assertIn('ValueError', fallback.getvalue())
        self.assertNotIn(PRIVATE, fallback.getvalue())

    def test_broken_console_preserves_retry_failure_and_exactly_one_restore(self):
        manager = ProjectManager.__new__(ProjectManager)
        manager._update_generation_fields = Mock(side_effect=OSError(PRIVATE))
        manager._restore_cancelled_generation = Mock()
        budget = SimpleNamespace(get_history=lambda: [], record=Mock(), phase='provider')
        original_error = ValueError(PRIVATE)
        fallback = io.StringIO()
        with self.failing_diagnostic_sink(fallback), patch('project.is_audio_generation_cancelled', return_value=False):
            self.assertEqual((False, str(original_error)), manager._finish_speech_retry_failure({'uid': PRIVATE}, budget, original_error))
        budget.record.assert_called_once()
        manager._restore_cancelled_generation.assert_called_once_with({'uid': PRIVATE}, speech_retry_attempts=[])
        self.assert_record('speech_retry_persist_failed', 'OSError', PRIVATE)
        self.assertIn('ValueError', fallback.getvalue())
        self.assertNotIn(PRIVATE, fallback.getvalue())

    def test_broken_console_preserves_guard_statuses_and_auth_challenge(self):
        code = '''
import io
import logging
import os
import sys
os.environ['ALEXANDRIA_AUTH_PASSWORD'] = 'SECRET_TOKEN_968314 SYNTHETIC_PRIVATE_BOOK_TEXT'
from fastapi.testclient import TestClient
import app
console = io.StringIO()
console.close()
fallback = io.StringIO()
logger = logging.getLogger('AlexandriaDiagnostics')
logger.handlers = [logging.StreamHandler(console)]
logger.propagate = False
logger.setLevel(logging.DEBUG)
logging.raiseExceptions = True
with TestClient(app.app) as client:
    previous_stderr, previous_fallback = sys.stderr, sys.__stderr__
    try:
        sys.stderr, sys.__stderr__ = console, fallback
        for index in range(30):
            response = client.post('/api/logs/browser-failure', headers={'Host': 'evil.example'})
            assert response.status_code == 400, response.status_code
            response = client.post('/api/logs/browser-failure', headers={'Origin': 'https://evil.example'})
            assert response.status_code == 403, response.status_code
            response = client.post('/api/logs/browser-failure', headers={'Authorization': os.environ['ALEXANDRIA_AUTH_PASSWORD']})
            assert response.status_code == 401, response.status_code
            assert response.headers['www-authenticate'] == 'Basic realm="Alexandria"'
    finally:
        sys.stderr, sys.__stderr__ = previous_stderr, previous_fallback
assert 'ValueError' in fallback.getvalue()
assert os.environ['ALEXANDRIA_AUTH_PASSWORD'] not in fallback.getvalue()
'''
        result = subprocess.run([os.sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for stage, status in (('http_host_refused', 400), ('http_origin_refused', 403), ('http_auth_refused', 401)):
            self.assertEqual(status, self.assert_record(stage)['status_code'])
        self.assertEqual(3, len(self.records()))

    def test_non_exception_evidence_never_reads_private_values(self):
        class PrivateValue:
            def __str__(self):
                raise AssertionError(PRIVATE)

        self.assertEqual({}, review_startup.get_exception_evidence(None))
        for error in (PRIVATE, 42, PrivateValue()):
            with self.subTest(error_type=type(error).__name__):
                self.assertEqual({'error_type': type(error).__name__}, review_startup.get_exception_evidence(error))
                self.assertTrue(review_startup.record_failure('invalid_error_value', error, task='diagnostics'))
        self.assertEqual(3, len(self.records()))

    def test_all_failed_sinks_cannot_escape_or_replace_original_exception(self):
        fallback = io.StringIO()
        fallback.close()
        original_error = RuntimeError(PRIVATE)
        trace = review_startup.ReviewStartupTrace(self.root / 'trace.log')

        def fail_original():
            raise original_error

        with self.failing_diagnostic_sink(fallback), patch.object(Path, 'open', side_effect=OSError(PRIVATE)):
            with patch.object(review_startup.logging, 'getLogger', return_value=logging.getLogger('AlexandriaDiagnostics')):
                self.assertIs(False, review_startup.record_failure('all_sinks_failed', original_error, task='diagnostics'))
                with self.assertRaises(RuntimeError) as raised:
                    trace.call('worker', fail_original)
        self.assertIs(original_error, raised.exception)

    def test_legacy_llm_helper_omits_non_exception_error_values(self):
        with patch.object(system, 'API_LOG_DIR', str(self.root)):
            path = system._log_llm_failure('test', PRIVATE)
        record = self.assert_record('llm_test_failed')
        self.assertNotIn('error_type', record)
        self.assertNotIn(PRIVATE, Path(path).read_text(encoding='utf-8'))

    def test_llm_test_failure_drops_frames_in_both_files_but_keeps_bounded_fields(self):
        class CodedError(RuntimeError):
            status_code = 401

        def build_error():
            try:
                try:
                    raise ValueError('inner ' + PRIVATE)
                except ValueError as cause:
                    raise CodedError('outer ' + PRIVATE) from cause
            except CodedError as error:
                return error

        resource = 'https://synthetic-tenant-4c1d9e2a.thundercompute.net/v1'
        with patch.object(system, 'API_LOG_DIR', str(self.root)):
            path = system._log_llm_failure('test', build_error(), operation='models', resource=resource)
        record = self.assert_record('llm_test_failed', 'CodedError', resource)
        self.assertEqual('models', record['operation'])
        self.assertEqual(401, record['status_code'])
        self.assertEqual(1, len(record['causes']))
        self.assertEqual('ValueError', record['causes'][0]['error_type'])
        self.assertNotIn('traceback', record['causes'][0])
        expected_hash = hashlib.sha256(os.fsencode(resource)).hexdigest()[:16]
        central = review_startup.read_trace(review_startup.get_failure_log_path()).decode()
        for text in (Path(path).read_text(encoding='utf-8'), central):
            self.assertNotIn('"traceback"', text)
            self.assertNotIn(PRIVATE, text)
            self.assertIn('llm_test_failed', text)
            self.assertIn(expected_hash, text)
        with patch.object(system, 'API_LOG_DIR', str(self.root)):
            system._log_llm_failure('optimize', build_error(), operation='reload', resource='synthetic')
        self.assertTrue(self.assert_record('llm_optimize_failed', 'CodedError')['traceback'])

    def test_optional_evaluation_missing_is_normal_but_corruption_is_recorded(self):
        directory = self.root / PRIVATE
        directory.mkdir()
        missing = lora._get_candidate_summary({}, str(directory), synthesis_settings={})
        self.assertEqual([], self.records())
        (directory / 'evaluation.json').write_text('{' + PRIVATE, encoding='utf-8')
        for _index in range(30):
            self.assertEqual(missing, lora._get_candidate_summary({}, str(directory), synthesis_settings={}))
        self.assert_record('candidate_evaluation_load_failed', 'JSONDecodeError', directory / 'evaluation.json')

    def test_evaluation_unreadable_encoding_and_shapes_keep_fallback(self):
        path = self.root / 'evaluation.json'
        for value in (b'\xff', b'[]', b'{"candidate_recommendation": [1]}'):
            with self.subTest(value=value):
                if hasattr(review_startup, '_failure_limits'):
                    review_startup._failure_limits.clear()
                review_startup.get_failure_log_path().unlink(missing_ok=True)
                path.write_bytes(value)
                result = lora._get_candidate_summary({}, str(self.root), synthesis_settings={})
                self.assertEqual('no_candidates', result['state'])
                self.assertEqual(0, result['evaluated_count'])
                self.assertEqual(1, len(self.records()))
        review_startup._failure_limits.clear()
        review_startup.get_failure_log_path().unlink()
        with patch.object(lora, 'open', side_effect=PermissionError(PRIVATE), create=True):
            result = lora._get_candidate_summary({}, str(self.root), synthesis_settings={})
        self.assertEqual('no_candidates', result['state'])
        self.assert_record('candidate_evaluation_load_failed', 'PermissionError', path)

    def test_conservative_gpu_policy_fences_stay_true_and_dedupe(self):
        with patch('lmstudio_settings.get_active_llm_config', side_effect=RuntimeError(PRIVATE)):
            for _index in range(100):
                self.assertIs(True, core.is_llm_on_this_gpu({}))
        self.assert_record('gpu_policy_read_failed', 'RuntimeError')
        with patch.object(core, 'load_app_config', side_effect=OSError(PRIVATE)):
            for _index in range(100):
                self.assertIs(True, core.llm_is_on_this_gpu())
        self.assert_record('gpu_config_read_failed', 'OSError')

    def test_retry_persistence_and_restore_preserve_return_and_cleanup(self):
        manager = ProjectManager.__new__(ProjectManager)
        manager._update_generation_fields = Mock(side_effect=OSError(PRIVATE))
        manager._restore_cancelled_generation = Mock(side_effect=RuntimeError(PRIVATE))
        budget = SimpleNamespace(get_history=lambda: [], record=Mock(), phase='provider')
        with patch('project.is_audio_generation_cancelled', return_value=False):
            result = manager._finish_speech_retry_failure({'uid': PRIVATE}, budget, ValueError(PRIVATE))
        self.assertEqual((False, PRIVATE), result)
        budget.record.assert_called_once()
        manager._restore_cancelled_generation.assert_called_once_with({'uid': PRIVATE}, speech_retry_attempts=[])
        self.assert_record('speech_retry_persist_failed', 'OSError', PRIVATE)
        self.assert_record('speech_retry_restore_failed', 'RuntimeError', PRIVATE)
        with patch('project.is_audio_generation_cancelled', return_value=False):
            for _index in range(30):
                self.assertEqual((False, PRIVATE), manager._finish_speech_retry_failure({'uid': PRIVATE}, budget, ValueError(PRIVATE)))
        self.assertEqual(31, manager._restore_cancelled_generation.call_count)
        self.assertEqual(2, len(self.records()))

    def test_legacy_llm_failure_uses_safe_bounded_files_and_keeps_response(self):
        client = SimpleNamespace(models=SimpleNamespace(list=Mock(side_effect=RuntimeError(PRIVATE))))
        with patch.object(system, 'API_LOG_DIR', str(self.root)), patch('llm_provider.make_llm_client', return_value=client), patch.object(system, '_validate_local_llm_base_url'), patch.object(review_startup, 'MAX_BYTES', 1800):
            for _index in range(40):
                response = system._run_llm_test({'base_url': 'http://localhost/' + PRIVATE, 'api_key': PRIVATE})
                self.assertEqual(False, response['ok'])
                self.assertEqual('models', response['step'])
                self.assertEqual('Could not read the model list from the endpoint. '
                                 'Check the base URL and credentials, then try again.', response['error'])
                self.assertTrue(Path(response['log_file']).is_file())
            files = [path for path in self.root.glob('llm_*') if path.suffix in ('.log', '.previous')]
            self.assertEqual(2, len(files))
            for path in files:
                self.assertLessEqual(path.stat().st_size, 1800)
                text = path.read_text(encoding='utf-8')
                self.assertNotIn(PRIVATE, text)
                self.assertIn('llm_test_failed', text)
                self.assertIn('RuntimeError', text)
                self.assertIn('models', text)
            from routers import script
            application = FastAPI()
            application.include_router(script.router)
            with patch.object(core, 'API_LOG_DIR', str(self.root)), TestClient(application) as download_client:
                download = download_client.get('/api/logs/llm_test?download=true')
            self.assertEqual(200, download.status_code)
            self.assertIn('llm_test.log', download.headers['Content-Disposition'])
            self.assertIn('llm_test_failed', download.text)
            self.assertNotIn(PRIVATE, download.text)

    def test_browser_endpoint_strict_size_schema_and_server_rate_limit(self):
        application = FastAPI()
        application.include_router(system.router)
        with TestClient(application) as client:
            for _index in range(50):
                response = client.post('/api/logs/browser-failure', json={'stage': 'save_flush_timer', 'error_name': 'TypeError'})
                self.assertEqual(204, response.status_code)
            self.assert_record('save_flush_timer')
            for body in ({'stage': PRIVATE, 'error_name': 'Error'}, {'stage': 'save_flush_timer', 'error_name': PRIVATE}, {'stage': 'save_flush_timer', 'error_name': 'Error', 'message': PRIVATE}, [], None):
                self.assertEqual(400, client.post('/api/logs/browser-failure', content=json.dumps(body)).status_code)
            self.assertEqual(413, client.post('/api/logs/browser-failure', content=PRIVATE * 100).status_code)
            self.assertEqual(413, client.post('/api/logs/browser-failure', content=chunks_as_sync()).status_code)
            self.assertEqual(400, client.post('/api/logs/browser-failure', content='{' + PRIVATE).status_code)
            self.assertEqual(400, client.post('/api/logs/browser-failure', content=b'\xff').status_code)
        self.assertEqual(1, len(self.records()))

        async def disconnect():
            return {'type': 'http.disconnect'}

        from starlette.requests import Request
        request = Request({'type': 'http'}, disconnect)
        self.assertEqual(400, asyncio.run(system.record_browser_failure(request)).status_code)
        self.assertEqual(1, len(self.records()))

    def test_completion_failure_keeps_models_contract_without_logging_content(self):
        client = SimpleNamespace(
            models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id=PRIVATE)])),
            chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=ValueError(PRIVATE)))))
        with patch.object(system, 'API_LOG_DIR', str(self.root)), patch('llm_provider.make_llm_client', return_value=client), patch.object(system, '_validate_local_llm_base_url'):
            response = system._run_llm_test({'base_url': PRIVATE, 'model_name': PRIVATE, 'api_key': PRIVATE})
        self.assertEqual({'ok': False, 'step': 'completion',
                          'error': 'The test request did not complete. Check the endpoint and model, then try again.',
                          'models': [PRIVATE], 'model_present': True,
                          'log_file': str(self.root / 'llm_test-latest.log')}, response)
        record = self.assert_record('llm_test_failed', 'ValueError', PRIVATE)
        self.assertEqual('completion', record['operation'])
        self.assertNotIn(PRIVATE, Path(response['log_file']).read_text(encoding='utf-8'))

    def test_optimize_legacy_evidence_rotates_without_touching_old_private_files(self):
        old = self.root / 'llm_optimize_20000101_000000_000001.log'
        old.write_text(PRIVATE, encoding='utf-8')
        with patch.object(system, 'API_LOG_DIR', str(self.root)), patch.object(review_startup, 'MAX_BYTES', 900):
            for _index in range(40):
                path = system._log_llm_failure('optimize', operation='reload', resource=PRIVATE)
                self.assertTrue(Path(path).is_file())
        self.assertEqual(PRIVATE, old.read_text(encoding='utf-8'))
        for path in (self.root / 'llm_optimize-latest.log', self.root / 'llm_optimize-latest.log.previous'):
            self.assertLessEqual(path.stat().st_size, 900)
            self.assertNotIn(PRIVATE, path.read_text(encoding='utf-8'))
            self.assertIn('llm_optimize_failed', path.read_text(encoding='utf-8'))

    def test_guard_refusals_are_rate_limited_and_preserve_responses(self):
        import app as application
        with TestClient(application.app) as client:
            for _index in range(30):
                response = client.post('/api/logs/browser-failure', headers={'Host': 'evil.example', 'Authorization': PRIVATE})
                self.assertEqual(400, response.status_code)
                response = client.post('/api/logs/browser-failure', headers={'Origin': 'https://evil.example/' + PRIVATE})
                self.assertEqual(403, response.status_code)
        self.assertEqual(400, self.assert_record('http_host_refused')['status_code'])
        self.assertEqual(403, self.assert_record('http_origin_refused')['status_code'])
        self.assertNotIn('evil.example', json.dumps(self.records()))

    def test_auth_guard_records_without_credentials_and_challenges_as_before(self):
        code = '''
import os
import base64
os.environ['ALEXANDRIA_AUTH_PASSWORD'] = 'SECRET_TOKEN_968314 SYNTHETIC_PRIVATE_BOOK_TEXT'
from fastapi.testclient import TestClient
import app
with TestClient(app.app) as client:
    for index in range(30):
        response = client.post('/api/logs/browser-failure', headers={'Authorization': os.environ['ALEXANDRIA_AUTH_PASSWORD']})
        assert response.status_code == 401
        assert response.headers['www-authenticate'] == 'Basic realm="Alexandria"'
    credential = base64.b64encode(('alexandria:' + os.environ['ALEXANDRIA_AUTH_PASSWORD']).encode()).decode()
    response = client.post('/api/logs/browser-failure', headers={'Authorization': 'Basic ' + credential},
                           json={'stage': 'save_pagehide', 'error_name': 'Error'})
    assert response.status_code == 204
'''
        result = subprocess.run([os.sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(401, self.assert_record('http_auth_refused')['status_code'])

    def test_limiter_expires_and_bounds_unique_resource_memory(self):
        with patch.object(review_startup.time, 'monotonic', return_value=100):
            for _index in range(100):
                review_startup.record_failure('poll_failed', OSError(PRIVATE), task='poll', rate_limit_seconds=300)
            self.assertEqual(1, len(self.records()))
            for index in range(1000):
                review_startup.record_failure('poll_failed', OSError(PRIVATE), task='poll', resource=str(index), rate_limit_seconds=300)
            self.assertLessEqual(len(review_startup._failure_limits), 512)
            self.assertEqual(512, len(self.records()))
        with patch.object(review_startup.time, 'monotonic', return_value=401):
            review_startup.record_failure('poll_failed', OSError(PRIVATE), task='poll', rate_limit_seconds=300)
        self.assertEqual(1, len(review_startup._failure_limits))
        self.assertEqual(513, len(self.records()))

    def test_browser_timer_and_pagehide_report_only_static_labels_locally(self):
        code = r'''
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function createSerializedSaveQueue(');
const end = source.indexOf('// --- API Helpers ---', start);
let now = 0, timer, hidden, finished = false;
const sent = [], failures = [];
const context = {Date: {now: () => now}, setTimeout: callback => {timer = callback;}, clearTimeout() {},
    fetch: (url, options) => {sent.push({url, options}); return Promise.reject(new Error('offline'));},
    window: {addEventListener: (event, callback) => {assert.strictEqual(event, 'pagehide'); hidden = callback;}}};
vm.runInNewContext(source.slice(start, end), context);
process.on('beforeExit', () => assert(finished, 'asynchronous assertions must finish'));
(async () => {
    const queue = context.createSerializedSaveQueue({write: async () => {throw new TypeError('SECRET_TOKEN_968314 SYNTHETIC_PRIVATE_BOOK_TEXT');}, onError: error => failures.push(error)});
    queue.enqueue({text: 'SECRET_TOKEN_968314 SYNTHETIC_PRIVATE_BOOK_TEXT'});
    timer(); await new Promise(setImmediate);
    assert(queue.isDirty()); assert.strictEqual(failures.length, 1);
    assert.strictEqual(sent.length, 1);
    assert.deepStrictEqual(JSON.parse(sent[0].options.body), {stage: 'save_flush_timer', error_name: 'TypeError'});
    context.flushVoiceSaves = () => queue.flush();
    const handler = source.split('\n').find(line => line.includes("window.addEventListener('pagehide'"));
    vm.runInNewContext(handler, context);
    hidden(); await new Promise(setImmediate);
    assert(queue.isDirty()); assert.strictEqual(sent.length, 2);
    assert.strictEqual(JSON.parse(sent[1].options.body).stage, 'save_pagehide');
    for (let index = 0; index < 100; index++) {timer(); hidden();}
    await new Promise(setImmediate);
    assert.strictEqual(sent.length, 2, 'same labels must dedupe');
    for (const name of ['Error', 'RangeError', 'SyntaxError', 'AbortError', 'NetworkError', 'SecurityError']) {
        context.reportBrowserFailure('save_flush_timer', {name, message: 'SECRET_TOKEN_968314 SYNTHETIC_PRIVATE_BOOK_TEXT'});
    }
    assert.strictEqual(sent.length, 5, 'at most five reports per minute');
    now = 60001;
    context.reportBrowserFailure('save_flush_timer', {name: 'SECRET_TOKEN_968314 SYNTHETIC_PRIVATE_BOOK_TEXT'});
    assert.strictEqual(sent.length, 6);
    assert.strictEqual(JSON.parse(sent[5].options.body).error_name, 'UnknownError');
    context.reportBrowserFailure('SECRET_TOKEN_968314 SYNTHETIC_PRIVATE_BOOK_TEXT', new Error());
    assert.strictEqual(sent.length, 6);
    for (const {url, options} of sent) {
        assert.strictEqual(url, '/api/logs/browser-failure');
        assert.strictEqual(options.redirect, 'error');
        assert.strictEqual(options.credentials, 'same-origin');
        assert.strictEqual(options.keepalive, true);
        assert(!options.body.includes('SECRET_TOKEN_968314'));
        assert(!options.body.includes('SYNTHETIC_PRIVATE_BOOK_TEXT'));
    }
    finished = true;
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run(['node', '-e', code, str(Path(core.STATIC_DIR, 'js', 'app-core.js'))], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


def chunks_as_sync():
    for _index in range(10):
        yield b'x' * 100
