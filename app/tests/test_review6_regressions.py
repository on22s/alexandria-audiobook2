"""Independent-review regressions for frozen tree 05867f22 (rescore 6).

Each test is derived from a separate reproduction in
/var/tmp/aa2240-frozen.bH7eU10A/{probe,extra,boundary,crash,routes}.py.
The tests are the executable contract: they assert refusal/rollback and the
complete-original-source oracle, not merely a status code.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import book_state_transaction as books
import three_pass_generate as worker
from llm_provider import FailoverClient, make_run_client
from utils import atomic_json_write

from tests.test_recovery_validation_matrix import (
    SOURCE, _Harness, _config, _digest, _factory, _payloads, script)
from tests.test_support import intercept_book_publications


def _route(h, method, tail, payload=None, status=200):
    app = FastAPI()
    app.include_router(script.router)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = (client.get('/api/generate_script/' + tail) if method == 'get'
                    else client.post('/api/generate_script/' + tail, json=payload or {}))
    assert response.status_code == status, (tail, response.status_code, response.text[:400])
    return response


def _split(h, command=None, factory=None):
    return h.worker(command, factory or _factory(_payloads()))


class Review6Regressions(unittest.TestCase):
    def _failed(self, stage='attribute', *, config=None, cast=False, policy=None,
                receipt=True, factory=None):
        effective = config or (_config(force_segment_llm=(stage == 'segment')))
        h = _Harness({'case': 'review6-' + stage}, config=effective, cast=cast)
        self.addCleanup(h.close)
        if receipt:
            command, receipt_path, _, admitted = h.admit()
        else:
            command, receipt_path, admitted = h.admit_legacy(), h.artifact_paths()['receipt'], None
        code = h.worker(command, factory or _factory(_payloads(), fail=(stage,)),
                        policy=policy or ('fail' if stage == 'segment' else None))
        checkpoint = Path(h.artifact_paths()['checkpoint'])
        return h, command, receipt_path, admitted, checkpoint, code

    def _write_checkpoint(self, path, data):
        atomic_json_write(data, path)

    def _protected(self, h):
        return {name: _digest(path) for name, path in h.artifact_paths().items()
                if name in ('receipt', 'checkpoint', 'manifest')}

    def test_credentials_absent_from_receipt(self):
        config = _config()
        config['llm']['api_key'] = 'TEST_ONLY_SECRET_MARKER'
        config['llm']['headers'] = {'X-API-Key': 'TEST_ONLY_SECRET_MARKER'}
        h = _Harness({'case': 'credentials'}, config=config)
        self.addCleanup(h.close)
        _, receipt_path, _, receipt = h.admit()
        raw = Path(receipt_path).read_text()
        self.assertNotIn('TEST_ONLY_SECRET_MARKER', raw)
        self.assertNotIn('TEST_ONLY_SECRET_MARKER', json.dumps(receipt))

    def test_segment_source_mutation_is_refused_and_no_unrelated_rows_publish(self):
        h, command, _, _, checkpoint, _ = self._failed('segment')
        value = json.loads(checkpoint.read_text())
        value['failed']['source'] = '"Another book," said MIRA.'
        self._write_checkpoint(checkpoint, value)
        before = self._protected(h)
        _route(h, 'get', 'recovery/detail', status=409)
        _route(h, 'post', 'inject', {'chunk': 1, 'entries': [
            {'type': 'SPOKEN', 'text': 'Another book,'},
            {'type': 'NARRATOR', 'text': 'said MIRA.'}]}, status=409)
        self.assertEqual(before, self._protected(h))

    def test_segment_cursor_mutation_is_refused(self):
        h, command, _, _, checkpoint, _ = self._failed('segment')
        value = json.loads(checkpoint.read_text())
        value['failed']['chunk'] = 2
        self._write_checkpoint(checkpoint, value)
        before = self._protected(h)
        _route(h, 'post', 'skip', {'chunk': 2}, status=409)
        self.assertEqual(before, self._protected(h))
        self.assertEqual(0, json.loads(checkpoint.read_text())['chunks_done'])

    def test_instruction_index_reordering_and_oob_refused(self):
        for kind in ('reverse', 'oob'):
            with self.subTest(kind=kind):
                h, command, _, _, checkpoint, _ = self._failed('instruct')
                value = json.loads(checkpoint.read_text())
                if kind == 'reverse':
                    value['failed']['indices'] = list(reversed(value['failed']['indices']))
                    value['failed']['entries'][0]['text'] = 'A different line.'
                else:
                    value['failed']['indices'] = [99 + i for i in range(len(value['failed']['entries']))]
                self._write_checkpoint(checkpoint, value)
                before = self._protected(h)
                _route(h, 'get', 'recovery/detail', status=409)
                _route(h, 'post', 'inject', {'entries': [
                    {'n': index, 'instruct': 'Calm.'} for index in value['failed']['indices']]},
                    status=409)
                self.assertEqual(before, self._protected(h))
                self.assertLess(len(json.loads(checkpoint.read_text()).get('annotated') or []), 50)

    def test_attribution_trailing_row_loss_refused_on_every_route(self):
        for route in ('detail', 'skip', 'generate', 'retry'):
            with self.subTest(route=route):
                h, command, _, _, checkpoint, _ = self._failed('attribute')
                value = json.loads(checkpoint.read_text())
                value['segmented'].pop()
                if len(value['named']) > len(value['segmented']):
                    value['named'].pop()
                self._write_checkpoint(checkpoint, value)
                before = self._protected(h)
                if route == 'detail':
                    _route(h, 'get', 'recovery/detail', status=409)
                elif route == 'skip':
                    _route(h, 'post', 'skip', {}, status=409)
                elif route == 'generate':
                    _route(h, 'post', '', {}, status=409)
                else:
                    _route(h, 'post', 'retry', {}, status=409)
                self.assertEqual(before, self._protected(h))

    def test_manifest_contradictions_refused(self):
        h, command, _, _, checkpoint, _ = self._failed('attribute')
        manifest = Path(h.artifact_paths()['manifest'])
        value = json.loads(manifest.read_text())
        value['fingerprint'] = {'model_name': 'different'}
        value['progress']['attributed_entries'] = 999
        value['progress']['chunks_completed'] = True
        value['recovered_units'] = [{}]
        atomic_json_write(value, manifest)
        before = self._protected(h)
        _route(h, 'get', 'recovery/detail', status=409)
        _route(h, 'post', 'skip', {}, status=409)
        self.assertEqual(before, self._protected(h))

    def test_strict_json_and_nonnumeric_chunk_are_bounded(self):
        for kind in ('duplicate', 'nonnumeric'):
            with self.subTest(kind=kind):
                h, command, _, _, checkpoint, _ = self._failed('attribute')
                if kind == 'duplicate':
                    raw = checkpoint.read_text().replace(
                        '"chunks_done":', '"chunks_done": 999, "chunks_done":', 1)
                    checkpoint.write_text(raw, encoding='utf-8')
                else:
                    value = json.loads(checkpoint.read_text())
                    value['failed']['chunk'] = 'not an integer'
                    self._write_checkpoint(checkpoint, value)
                before = self._protected(h)
                for tail in ('recovery/detail',):
                    _route(h, 'get', tail, status=409)
                _route(h, 'post', 'skip', {}, status=409)
                self.assertEqual(before, self._protected(h))

    def test_no_receipt_checkpoint_detail_and_retry_parity(self):
        for custom in (False, True):
            with self.subTest(custom=custom):
                h, command, receipt_path, _, checkpoint, code = self._failed(
                    'attribute', config=_config(custom_prompts=custom), receipt=False)
                _route(h, 'get', 'recovery/detail', status=200)
                response = _route(h, 'post', 'retry', {}, status=200)
                replay = _split(h, h.scheduled[-1][4])
                self.assertEqual(0, replay, 'Retry worker must resume the legacy checkpoint')
                self.assertEqual(len(_payloads()['segment']), len(h.rows() or []))

    def test_failover_transition_recovery(self):
        config = _config()
        config.update(llm_failover=True, llm_remote={
            **config['llm'], 'base_url': 'http://localhost:1235/v1',
            'model_name': 'matrix-backup'})
        dispatched = []

        def transport(*args, **kwargs):
            client = _factory(_payloads(), fail=('attribute',))()
            create = client.chat.completions.create
            def request(**kw):
                dispatched.append(kw.get('model'))
                return create(**kw)
            client.chat.completions.create = request
            return client

        def factory(config_value, llm, timeout):
            with patch('llm_provider.make_llm_client', side_effect=transport):
                client = make_run_client(config_value, llm, timeout)
            assert isinstance(client, FailoverClient)
            client.failover({'category': 'unavailable', 'status_code': 503})
            return client

        h, command, _, _, checkpoint, _ = self._failed(
            'attribute', config=config, factory=factory)
        self.assertIn('matrix-backup', dispatched)
        _route(h, 'get', 'recovery/detail', status=200)
        _route(h, 'post', 'skip', {}, status=200)

    def test_worker_fingerprint_and_endpoint_match_receipt(self):
        config = _config()
        config['llm']['api_key'] = 'TEST_ONLY_SECRET_MARKER'
        h = _Harness({'case': 'identity'}, config=config)
        self.addCleanup(h.close)
        command, receipt_path, digest, receipt = h.admit()
        changed = json.loads((h.root / 'config.json').read_text())
        changed['llm']['base_url'] = 'http://localhost:23459/v1'
        atomic_json_write(changed, h.root / 'config.json')
        seen = {}
        real = worker.run_three_pass
        def observe(*args, **kwargs):
            seen['fingerprint'] = kwargs.get('run_spec_receipt', {}).get('fingerprint')
            seen['context'] = args[3].context_length
            return real(*args, **kwargs)
        def factory(config_value, llm, *args):
            seen['url'] = llm.get('base_url')
            return _factory(_payloads())()
        with patch.object(worker, 'run_three_pass', side_effect=observe):
            code = h.worker(command, factory)
        self.assertNotEqual(0, code, 'cross-endpoint profile change must be refused')
        self.assertNotIn('url', seen, 'no client may be built for the changed endpoint')
        independent = _Harness({'case': 'native-fingerprint'})
        self.addCleanup(independent.close)
        native_command, _, _, _ = independent.admit()
        native_command = native_command[:native_command.index('--run-spec-receipt')]
        independent.worker(native_command, _factory(_payloads()))
        native = json.loads(Path(independent.artifact_paths()['checkpoint']).read_text())
        self.assertEqual(receipt['fingerprint'], native['fingerprint'],
                         'receipt fingerprint must equal the native worker fingerprint')

    def test_worker_caps_context_from_runtime_detection(self):
        config = _config()
        config['llm']['context_length'] = 32768
        h = _Harness({'case': 'context'}, config=config)
        self.addCleanup(h.close)
        with patch.object(script, 'get_planned_request_context',
                          return_value={'known': True, 'context': 2048, 'parallel': 1}):
            command, _, _, receipt = h.admit()
        seen = {}
        real = worker.run_three_pass
        def observe(*args, **kwargs):
            seen['context'] = args[3].context_length
            return real(*args, **kwargs)
        with patch.object(worker, 'ensure_ideal_settings',
                          return_value=(None, {'context_length': 2048}, 'test')), \
                patch.object(worker, 'run_three_pass', side_effect=observe):
            code = h.worker(command, _factory(_payloads()))
        self.assertEqual(0, code)
        self.assertEqual(2048, receipt['spec']['params']['context_length'])
        self.assertEqual(2048, seen['context'])

    def test_cast_changes_at_recovery_boundaries_refused(self):
        h, command, _, _, checkpoint, _ = self._failed('attribute', cast=True)
        (h.root / 'script_run_cast.json').write_text('{"cast":[{"name":"OTHER"}]}')
        before = self._protected(h)
        _route(h, 'get', 'recovery/detail', status=409)
        self.assertEqual(before, self._protected(h))

    def test_before_replace_source_task_and_cast_guard(self):
        for kind in ('source', 'task', 'cast'):
            with self.subTest(kind=kind):
                h, command, _, _, checkpoint, _ = self._failed('attribute')
                original = script.apply_book_state_locked
                def racing(*args, **kwargs):
                    guard = kwargs.get('before_replace')
                    if kind == 'source':
                        h.source.write_text('Changed after validation.')
                    elif kind == 'task':
                        h.states['script']['running'] = True
                    else:
                        (h.root / 'script_run_cast.json').write_text('{"cast":[{"name":"OTHER"}]}')
                    if guard is not None:
                        guard()
                    return original(*args, **kwargs)
                before = self._protected(h)
                with patch.object(script, 'apply_book_state_locked', side_effect=racing):
                    _route(h, 'post', 'skip', {}, status=409)
                h.states['script']['running'] = False
                self.assertEqual(before, self._protected(h))

    def test_generate_publication_rechecks_source_bytes(self):
        h = _Harness({'case': 'start-source'})
        self.addCleanup(h.close)
        original = script.apply_book_state_locked
        def racing(*args, **kwargs):
            h.source.write_text('Different after admission.')
            return original(*args, **kwargs)
        with patch.object(script, 'apply_book_state_locked', side_effect=racing):
            _route(h, 'post', '', {}, status=409)
        self.assertIsNone(_digest(script.run_spec_receipt.receipt_path(str(h.root))))


if __name__ == '__main__':
    unittest.main()
