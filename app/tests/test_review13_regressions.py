"""Retry keeps its admitted inputs across SDK requests and native worker exits."""
import copy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException

from tests import test_review11_regressions as admission
from tests import test_review12_regressions as prior
from tests.test_review7_regressions import MULTI_SOURCE
from tests.test_support import FramedHttpFixtureHandler, capture_script_worker_starts
from utils import atomic_json_write

fixtures = prior.fixtures
script = prior.script
worker = prior.worker

_RUN_WORKER = """
from contextlib import ExitStack
import os
import sys
from unittest.mock import patch
import llm_provider
import three_pass_generate as worker
from tests.test_recovery_validation_matrix import _factory, _payloads

mode = sys.argv.pop(1)
save = worker._save_three_pass_checkpoint

def save_then_exit(*args, **kwargs):
    save(*args, **kwargs)
    if args[2] == mode:
        os._exit(87)

with ExitStack() as stack:
    stack.enter_context(patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')))
    if mode in ('segment', 'segment_failed'):
        stack.enter_context(patch.object(worker, '_save_three_pass_checkpoint', side_effect=save_then_exit))
    if mode == 'segment_failed':
        factory = _factory(_payloads(), fail=('segment',))
    elif mode == 'offline':
        factory = _factory(_payloads())
    else:
        sdk = llm_provider.OpenAI
        stack.enter_context(patch.object(llm_provider, 'OpenAI', side_effect=lambda **kw: sdk(**kw, max_retries=0)))
        factory = llm_provider.make_run_client
    worker.main(client_factory=factory)
"""

_PANEL_HARNESS = r"""
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const text = fs.readFileSync(process.argv[1], 'utf8');
const recovery = JSON.parse(process.argv[2]);
const summary = JSON.parse(process.argv[3]);
const nodes = {};
const calls = [];
const polls = [];
const claimId = 'a'.repeat(32);
const context = {
    window: {}, currentBookFilename: 'fixture.txt',
    document: {getElementById(id) { return nodes[id] ||= {style: {}, innerHTML: 'previous'}; }},
    API: {
        async get(path) {
            calls.push(path);
            if (recovery.rejected) { throw Error('HTTP 409'); }
            return path.includes('?') ? recovery : summary;
        },
        async post(path, payload) {
            calls.push([path, Object.keys(payload)]);
            return {task_claim_id: claimId};
        }
    },
    t: (_key, text, values = {}) => text.replace(/\{(\w+)\}/g, (match, key) => values[key] ?? match),
    showToast() {}, showActionError() {}, _resetPauseBtn() {},
    pollScriptLogs(task, _activity, token) { polls.push([task, token]); },
    console: {debug() {}},
};
vm.createContext(context);
function load(start, end) {
    const a = text.indexOf(start);
    const b = text.indexOf(end, a);
    assert(a >= 0 && b > a, 'missing core slice ' + start);
    vm.runInContext(text.slice(a, b), context);
}
load('function escapeHtml(', 'function getInlineStringArgument(');
load('function isLocalVoiceLoadObsolete(', 'function setFieldFeedback(');
load('function scriptRecoveryContextMatches(', 'async function onCopyRecoveryPrompt()');
load('window.retryScriptGeneration = async', '// --- Script Batch Mode ---');
(async () => {
    await context.refreshScriptRecovery();
"""

_ASSERT_PANEL = _PANEL_HARNESS + r"""
    assert.strictEqual(nodes['btn-retry-script'].style.display, 'inline-block');
    assert.strictEqual(nodes['script-recovery-panel'].style.display, 'none');
    assert.strictEqual(nodes['script-recovery-panel'].innerHTML, '');
    await context.window.retryScriptGeneration();
    assert.deepStrictEqual(JSON.parse(JSON.stringify(calls)), [
        '/api/generate_script/recovery?include_detail=true',
        '/api/generate_script/recovery', ['/api/generate_script/retry', []],
    ]);
    assert.strictEqual(nodes['btn-cancel-script'].style.display, 'inline-block');
    const token = context.window._scriptRecoveryAcceptedRun;
    assert.strictEqual(context.window.getScriptAdmissionByToken(token).claimId, claimId);
    assert.deepStrictEqual(polls, [['script', token]]);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class Review13Regressions(unittest.TestCase):
    def _harness(self, config):
        h = fixtures._Harness({'case': 'retry-authority'}, config=config, source_text=MULTI_SOURCE)
        self.addCleanup(h.close)
        return h

    def _start_over(self, h):
        with capture_script_worker_starts() as run:
            response = admission.Review11Regressions()._start_over(h)
        self.assertEqual(200, response.status_code, response.text)
        run.assert_called_once()
        return run.call_args.args[0]

    def _resume(self, h):
        reply = fixtures._post(h, 'retry')
        self.assertEqual(200, reply.status_code, reply.text)
        command = h.scheduled[-1][4]
        self.assertIn('--require-matching-checkpoint', command)
        return command

    def _worker(self, h, command, mode, expected_exit):
        result = prior.Review12Regressions()._child(h, _RUN_WORKER, [mode, *command[3:]])
        self.assertEqual(expected_exit, result.returncode, result.stderr + result.stdout)

    def test_retry_refuses_both_query_drifts_and_preserves_admitted_inputs(self):
        config = fixtures._config(force_segment_llm=True, custom_chunk=500, failover=True)
        primary = config['llm']['model_name']
        secondary = config['llm_remote']['model_name']
        requests = []
        outage = threading.Event()

        class Gateway(FramedHttpFixtureHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                url = urlsplit(self.path)
                query = dict(parse_qsl(url.query))
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                model = body['model']
                requests.append((model, url.path, query.get('route')))
                if url.path != '/v1/chat/completions' or query.get('gateway') != 'SYNTH_RETRY_KEY':
                    status, payload = 404, {'error': {'message': 'wrong gateway route'}}
                elif model == primary and outage.is_set():
                    status, payload = 503, {'error': {'message': 'synthetic outage'}}
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
        for profile in ('llm', 'llm_remote'):
            config[profile]['base_url'] = (
                f'http://127.0.0.1:{server.server_port}/v1?gateway=SYNTH_RETRY_KEY&route=initial')
        h = self._harness(config)
        original_command = self._start_over(h)
        self._worker(h, original_command, 'segment', 87)
        original = script.run_spec_receipt.load_receipt(h.root)
        checkpoint = script.get_current_generation_checkpoint()
        self.assertEqual((1, 4), (checkpoint['chunks_done'], original['chunk_count']))
        self.assertEqual([(primary, '/v1/chat/completions', 'initial')], requests)
        original_snapshot = h.snapshot()
        checkpoint_path = Path(h.artifact_paths()['checkpoint'])
        directories = list(checkpoint_path.parent.glob(checkpoint_path.name + '.parts-*'))
        shard_bytes = {path: path.read_bytes() for folder in directories for path in folder.iterdir()}
        queued = len(h.scheduled)
        for profile in ('llm', 'llm_remote'):
            with self.subTest(profile=profile):
                drift = copy.deepcopy(config)
                drift[profile]['base_url'] = config[profile]['base_url'].replace('initial', 'changed')
                atomic_json_write(drift, h.root / 'config.json')
                for route in ('retry', ''):
                    response = fixtures._post(h, route)
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(original_snapshot, h.snapshot())
                    self.assertEqual(original, script.run_spec_receipt.load_receipt(h.root))
                    self.assertEqual(queued, len(h.scheduled))
                    self.assertEqual(1, len(requests))
        self.assertEqual(shard_bytes, {path: path.read_bytes()
                                       for folder in directories for path in folder.iterdir()})
        atomic_json_write(config, h.root / 'config.json')
        command = self._resume(h)
        resumed = script.run_spec_receipt.load_receipt(h.root)
        self.assertNotEqual(original['run_id'], resumed['run_id'])
        self.assertEqual(dict(original['spec'], run_id=resumed['run_id']), resumed['spec'])
        self.assertEqual(original['fingerprint'], resumed['fingerprint'])
        self.assertEqual(resumed['sha256'], command[command.index('--run-spec-digest') + 1])
        outage.set()
        self._worker(h, command, 'finish', 0)
        self.assertEqual([primary] * 6 + [secondary] * 8, [model for model, _, _ in requests])
        self.assertTrue(all(path == '/v1/chat/completions' and route == 'initial'
                            for _, path, route in requests))
        rows = h.rows()
        self.assertEqual(20, len(rows))

        for profile in ('llm', 'llm_remote'):
            config[profile]['base_url'] = config[profile]['base_url'].replace('initial', 'fresh')
        atomic_json_write(config, h.root / 'config.json')
        start = len(requests)
        command = self._start_over(h)
        fresh = script.run_spec_receipt.load_receipt(h.root)
        self.assertNotEqual(resumed['spec']['secondary_endpoint'], fresh['spec']['secondary_endpoint'])
        self.assertNotEqual(resumed['spec']['llm_profile']['base_url'], fresh['spec']['llm_profile']['base_url'])
        self.assertNotIn('SYNTH_RETRY_KEY', Path(h.artifact_paths()['receipt']).read_text())
        outage.clear()
        self._worker(h, command, 'finish', 0)
        self.assertGreater(len(requests), start)
        self.assertTrue(all(route == 'fresh' for _, _, route in requests[start:]))
        self.assertEqual(rows, h.rows())

    def test_failed_checkpoint_manifest_gap_offers_only_retry_in_actual_panel(self):
        config = fixtures._config(force_segment_llm=True, custom_chunk=500)
        h = self._harness(config)
        command = self._start_over(h)
        self._worker(h, command, 'segment_failed', 87)
        checkpoint = script.get_current_generation_checkpoint()
        self.assertEqual(('segment_failed', 0), (checkpoint['stage'], checkpoint['chunks_done']))
        manifest_path = Path(h.artifact_paths()['manifest'])
        seed_bytes = manifest_path.read_bytes()
        seed = json.loads(seed_bytes)
        receipt = script.run_spec_receipt.load_receipt(h.root)
        self.assertEqual(('incomplete', receipt['run_id']), (seed['status'], seed['run_id']))
        summary = fixtures._get(h, 'recovery')
        combined = fixtures._get(h, 'recovery?include_detail=true')
        self.assertEqual(200, combined.status_code, combined.text)
        self.assertTrue(combined.json()['recoverable'])
        self.assertIsNone(combined.json()['detail'])
        panel = subprocess.run(
            ['node', '-e', _ASSERT_PANEL, str(prior.APP_DIR / 'static/js/app-core.js'),
             combined.text, summary.text], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, panel.returncode, panel.stderr)
        before = h.snapshot()
        self.assertEqual(409, fixtures._get(h, 'recovery/detail').status_code)
        self.assertEqual(409, fixtures._post(h, 'skip', {'chunk': 1}).status_code)
        self.assertEqual(409, fixtures._post(h, 'inject', {'chunk': 1, 'entries': []}).status_code)
        self.assertEqual(before, h.snapshot())

        for mutation in ({'run_id': 'e' * 32}, {'failed_pass': 'instruct'}, {'chunks': [{'index': 1}]}):
            with self.subTest(mutation=mutation):
                atomic_json_write(dict(seed, **mutation), manifest_path)
                before = h.snapshot()
                self.assertEqual(409, fixtures._get(h, 'recovery?include_detail=true').status_code)
                self.assertEqual(409, fixtures._post(h, 'retry').status_code)
                self.assertEqual(before, h.snapshot())
        manifest_path.write_bytes(seed_bytes)
        before = h.snapshot()
        with patch.object(script, 'register_claimed_background_task',
                          side_effect=HTTPException(status_code=409, detail='synthetic claim lost')):
            refused = fixtures._post(h, 'retry')
        self.assertEqual(409, refused.status_code)
        self.assertEqual(before, h.snapshot())
        command = self._resume(h)
        latest = script.run_spec_receipt.load_receipt(h.root)
        self.assertEqual(latest['run_id'], json.loads(manifest_path.read_bytes())['run_id'])
        self.assertNotEqual(receipt['run_id'], latest['run_id'])
        self._worker(h, command, 'offline', 0)
        rows = h.rows()
        self.assertEqual(20, len(rows))
        self.assertEqual('complete', json.loads(manifest_path.read_bytes())['status'])
        self._worker(h, self._start_over(h), 'offline', 0)
        self.assertEqual(rows, h.rows())
