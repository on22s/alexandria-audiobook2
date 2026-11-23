"""Native Start Over handoff across cleanup failure and process death."""
import errno
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import book_state_transaction as books
import llm_provider
from tests import test_review11_regressions as prior
from tests.test_support import FramedHttpFixtureHandler, capture_script_worker_starts

fixtures = prior.fixtures
core = prior.core
script = prior.script
worker = prior.worker
APP_DIR = Path(__file__).resolve().parents[1]

_START_OVER_CHILD = """
import os
import sys
from pathlib import Path
from unittest.mock import patch
import core
from routers import script

root = Path(os.environ['ALEXANDRIA_DATA_DIR'])
apply = script.apply_book_state_locked

def die_after_commit(*args, **kwargs):
    apply(*args, **kwargs)
    os._exit(86)

def mark_dispatch(*args):
    (root / 'unexpected-dispatch').touch()

with patch.object(core, 'acquire_gpu_lock', return_value=None), \\
        patch.object(script, 'get_active_reasoning_effort', return_value=None), \\
        patch.object(script, 'apply_book_state_locked', side_effect=die_after_commit), \\
        patch.object(script, 'run_process', side_effect=mark_dispatch):
    script.start_script_generation(script.BackgroundTasks(), str(root / 'book.txt'),
                                   script.GenerateScriptRequest(start_over=True))
"""

_WORKER_CHILD = """
from contextlib import ExitStack
import os
from unittest.mock import patch
import three_pass_generate as worker
from tests.test_recovery_validation_matrix import _factory, _payloads

save = worker._save_three_pass_checkpoint

def save_then_exit(*args, **kwargs):
    save(*args, **kwargs)
    os._exit(87)

with ExitStack() as stack:
    stack.enter_context(patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')))
    if os.environ['AA2_TEST_EXIT_AFTER_CHECKPOINT'] == '1':
        stack.enter_context(patch.object(worker, '_save_three_pass_checkpoint', side_effect=save_then_exit))
    worker.main(client_factory=_factory(_payloads()))
"""


class Review12Regressions(unittest.TestCase):
    def _sharded_fixture(self):
        test = prior.Review11Regressions()
        self.addCleanup(test.doCleanups)
        return test, test._sharded_fixture()

    def _child(self, h, source, args=(), *, exit_after_checkpoint=False):
        return subprocess.run(
            [sys.executable, '-c', source, *args], cwd=APP_DIR, text=True,
            env=dict(os.environ, PYTHONPATH=str(APP_DIR), ALEXANDRIA_DATA_DIR=str(h.root),
                     AA2_TEST_EXIT_AFTER_CHECKPOINT=str(int(exit_after_checkpoint))),
            capture_output=True, timeout=25)

    def _assert_seed(self, h):
        receipt = script.run_spec_receipt.load_receipt(h.root)
        checkpoint = worker._load_three_pass_checkpoint(
            str(h.script_path), receipt['fingerprint'], receipt['chunk_count'])
        self.assertIsNotNone(checkpoint, 'the admitted checkpoint must reopen with the native loader')
        self.assertEqual(('segment', 0, []), (
            checkpoint['stage'], checkpoint['chunks_done'], checkpoint['segmented']))
        manifest = json.loads(Path(h.artifact_paths()['manifest']).read_bytes())
        self.assertEqual(('incomplete', receipt['run_id']), (manifest['status'], manifest['run_id']))
        return receipt

    def test_committed_cleanup_eio_launches_worker_from_durable_seed(self):
        test, (h, _, shards, _, _) = self._sharded_fixture()
        finish = books._finish_transaction
        errors = []
        outcomes = []

        def fail_fsync(_fd):
            errors.append(errno.EIO)
            raise OSError(errno.EIO, 'synthetic committed cleanup error')

        def finish_once(*args, **kwargs):
            journal = kwargs.get('byte_data') or {}
            if journal.get('phase') == 'committed' and not errors:
                with patch.object(os, 'fsync', side_effect=fail_fsync):
                    return finish(*args, **kwargs)
            return finish(*args, **kwargs)

        def run(command, name):
            outcomes.append((core._task_claims[name]['phase'], self._assert_seed(h)['run_id']))
            outcomes.append(self._child(h, _WORKER_CHILD, command[3:]))

        with patch.object(books, '_finish_transaction', side_effect=finish_once), \
                capture_script_worker_starts(run) as dispatch, \
                patch.object(script.logger, 'warning', wraps=script.logger.warning) as warn:
            response = test._start_over(h)
        self.assertEqual([errno.EIO], errors)
        self.assertEqual(200, response.status_code, response.text)
        dispatch.assert_called_once()
        self.assertTrue(any('committed and recovered after cleanup error' in call.args[0]
                            for call in warn.call_args_list))
        receipt = script.run_spec_receipt.load_receipt(h.root)
        self.assertEqual(('started', receipt['run_id']), outcomes[0])
        self.assertEqual(0, outcomes[1].returncode, outcomes[1].stderr + outcomes[1].stdout)
        self.assertFalse((h.root / books.JOURNAL).exists())
        self.assertFalse(shards.exists())
        self.assertEqual(4, len(h.rows()))

    def test_process_death_after_commit_can_retry_from_native_seed(self):
        _, (h, _, shards, _, _) = self._sharded_fixture()
        crashed = self._child(h, _START_OVER_CHILD)
        self.assertEqual(86, crashed.returncode, crashed.stderr + crashed.stdout)
        self.assertFalse((h.root / 'unexpected-dispatch').exists())
        self.assertFalse(shards.exists())
        receipt = self._assert_seed(h)
        response = fixtures._get(h, 'recovery')
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['recoverable'])
        self.assertEqual('incomplete', response.json()['status'])
        resumed = fixtures._post(h, 'retry')
        self.assertEqual(200, resumed.status_code, resumed.text)
        command = h.scheduled[-1][4]
        self.assertIn('--require-matching-checkpoint', command)
        self.assertEqual(receipt['fingerprint'], script.run_spec_receipt.load_receipt(h.root)['fingerprint'])
        attempt = self._child(h, _WORKER_CHILD, command[3:], exit_after_checkpoint=True)
        self.assertEqual(87, attempt.returncode, attempt.stderr + attempt.stdout)
        partial = worker._load_three_pass_checkpoint(
            str(h.script_path), receipt['fingerprint'], receipt['chunk_count'])
        self.assertEqual(1, partial['chunks_done'])
        resumed = fixtures._post(h, 'retry')
        self.assertEqual(200, resumed.status_code, resumed.text)
        run = self._child(h, _WORKER_CHILD, h.scheduled[-1][4][3:])
        self.assertEqual(0, run.returncode, run.stderr + run.stdout)
        self.assertEqual(4, len(h.rows()))
        self.assertEqual('complete', json.loads(Path(h.artifact_paths()['manifest']).read_bytes())['status'])

    def test_gateway_query_reaches_chat_path_on_primary_and_failover(self):
        config = fixtures._config(failover=True)
        expected, seen = {}, []
        primary, secondary = (config[name]['model_name'] for name in ('llm', 'llm_remote'))

        class Gateway(FramedHttpFixtureHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                url = urlsplit(self.path)
                query = parse_qsl(url.query, keep_blank_values=True)
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                model = body['model']
                seen.append((model, url.path, query))
                if url.path != '/v1/chat/completions' or query != expected.get(model):
                    status, payload = 404, {'error': {'message': 'wrong gateway path or query'}}
                elif model == primary:
                    status, payload = 503, {'error': {'message': 'synthetic unavailable'}}
                else:
                    content = fixtures._factory(fixtures._payloads())().chat.completions.create(
                        **body).choices[0].message.content
                    status, payload = 200, {
                        'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': model,
                        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content},
                                     'finish_reason': 'stop'}],
                    }
                self.send_fixture_response(status, json.dumps(payload).encode(),
                                           [('Content-Type', 'application/json')])

        server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        for profile, route in (('llm', 'west'), ('llm_remote', 'east')):
            key = 'SYNTH_GATEWAY_' + route
            config[profile]['base_url'] = (
                f'http://127.0.0.1:{server.server_port}/v1?gateway={key}&route={route}&route=backup')
            expected[config[profile]['model_name']] = [('gateway', key), ('route', route), ('route', 'backup')]
        h = fixtures._Harness({'case': 'gateway-wire'}, config=config)
        self.addCleanup(h.close)
        command, receipt_path, _, _ = h.admit()
        raw = Path(receipt_path).read_text()
        self.assertNotIn('SYNTH_GATEWAY_', raw)
        self.assertNotIn('route=', raw)
        sdk = llm_provider.OpenAI

        with patch.object(llm_provider, 'OpenAI', side_effect=lambda **kw: sdk(**kw, max_retries=0)), \
                patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')):
            code = h.worker(command, llm_provider.make_run_client)
        expected_requests = [(model, '/v1/chat/completions', expected[model])
                             for model in [primary] * 4 + [secondary] * 2]
        self.assertEqual(expected_requests, seen)
        self.assertEqual(0, code)
        self.assertEqual(4, len(h.rows()))
        manifest = json.loads(Path(h.artifact_paths()['manifest']).read_bytes())
        self.assertIs(manifest['model_binding']['failover_used'], True)
