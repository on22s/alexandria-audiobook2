"""Rescore-7 independent-review regressions (frozen tree 8902e5d6).

Derived from /var/tmp/aa2240-independent.clr7lyIM/{check,multichunk,admission}.py.
Each test states the expected refusal/rollback and the complete-source oracle.
"""
import json
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import book_state_transaction as books
import three_pass_generate as worker
from llm_provider import make_run_client
from utils import atomic_json_write

import tests.test_recovery_validation_matrix as matrix
from tests.test_recovery_validation_matrix import (
    SOURCE, _Harness, _config, _digest, _factory, _payloads, script, core)

MULTI_SOURCE = '\n\n'.join(
    ' '.join([line] * 5) for line in (
        'MIRA looked outside at a tall green tree and a small oak beside the river bank.',
        'DANA walked very slowly along a winding path between old houses on the hill.',
        'Rain fell softly through the bare branches while the children played under the porch.',
        'Snow covered the railway tracks as a red train approached the empty wooden station.'))


def _routes(h):
    app = FastAPI()
    app.include_router(script.router)
    return TestClient(app, raise_server_exceptions=False)


def _get(h, tail):
    with _routes(h) as client:
        return client.get('/api/generate_script/' + tail)


def _post(h, tail, payload=None):
    with _routes(h) as client:
        return client.post('/api/generate_script/' + tail, json=payload or {})


class Review7Regressions(unittest.TestCase):
    def _fixture(self, *, config=None, cast=False, source_text=None, with_receipt=True,
                 fail=('attribute',), multi=False):
        cfg = config or (_config(force_segment_llm=(multi or fail == ('segment',)),
                                 custom_chunk=500 if multi else None))
        h = _Harness({'case': 'review7'}, config=cfg, cast=cast, source_text=source_text)
        self.addCleanup(h.close)
        if with_receipt:
            command, receipt_path, _, admitted = h.admit()
        else:
            command, receipt_path, admitted = h.admit_legacy(), h.artifact_paths()['receipt'], None
        paths = h.artifact_paths()
        with ExitStack() as stack:
            stack.enter_context(patch.object(worker, 'ensure_ideal_settings',
                                             return_value=(None, {}, 'offline')))
            if multi:
                pick = matrix._pick

                def fail_second(tag, payloads, fail_set, calls, text=''):
                    if tag == 'segment' and 'DANA walked' in text.split('SOURCE TEXT:')[-1]:
                        raise RuntimeError('fixture segment failure')
                    return pick(tag, payloads, fail_set, calls, text)
                stack.enter_context(patch.object(matrix, '_pick', side_effect=fail_second))
                code = h.worker(command, _factory(_payloads()))
            else:
                code = h.worker(command, _factory(_payloads(), fail=fail))
        checkpoint = Path(paths['checkpoint'])
        manifest = Path(paths['manifest'])
        return h, command, receipt_path, admitted, checkpoint, manifest, code

    def _write(self, path, data):
        atomic_json_write(data, path)

    def _protected(self, h):
        return {name: _digest(path) for name, path in h.artifact_paths().items()
                if name in ('receipt', 'checkpoint', 'manifest')}

    def test_nested_provider_header_credential_not_persisted(self):
        config = _config()
        config['llm']['provider_headers'] = {'X-Api-Key': 'SYNTHETIC_QA_VALUE'}
        h = _Harness({'case': 'secret'}, config=config)
        self.addCleanup(h.close)
        _, receipt_path, _, _ = h.admit()
        self.assertNotIn('SYNTHETIC_QA_VALUE', Path(receipt_path).read_text())

    def test_no_cross_endpoint_credentials(self):
        config = _config()
        config['llm']['api_key'] = 'KEY_A'
        h = _Harness({'case': 'cross-endpoint'}, config=config)
        self.addCleanup(h.close)
        command, _, _, receipt = h.admit()
        live = json.loads((h.root / 'config.json').read_text())
        live['llm']['base_url'] = 'http://localhost:23459/v1'
        live['llm']['api_key'] = 'KEY_B'
        atomic_json_write(live, h.root / 'config.json')
        seen = {}
        def factory(config_value, llm, timeout):
            seen['endpoint'] = llm.get('base_url')
            seen['key'] = llm.get('api_key')
            return _factory(_payloads())()
        code = h.worker(command, factory)
        self.assertNotEqual(0, code, 'must refuse before pairing A endpoint with B credential')
        self.assertNotIn('KEY_B', json.dumps(receipt))

    def test_multichunk_valid_prefix_resumes_via_generate_and_retry(self):
        for route in ('generate', 'retry'):
            with self.subTest(route=route):
                h, _, _, receipt, checkpoint, _, code = self._fixture(
                    multi=True, source_text=MULTI_SOURCE)
                self.assertNotEqual(0, code)
                self.assertEqual(1, json.loads(checkpoint.read_text())['chunks_done'])
                self.assertEqual(200, _get(h, 'recovery/detail').status_code)
                response = (_post(h, '') if route == 'generate' else _post(h, 'retry'))
                self.assertEqual(200, response.status_code, response.text)
                with patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')):
                    resume_command = h.scheduled[-1][4]
                    resume_code = h.worker(resume_command, _factory(_payloads()))
                self.assertEqual(0, resume_code)
                rows = h.rows() or []
                joined = ''.join(row['text'] for row in rows)
                self.assertEqual(4, receipt['chunk_count'])
                for paragraph in ('tall green tree', 'winding path', 'bare branches', 'railway tracks'):
                    self.assertIn(paragraph, joined)

    def test_cursor_must_be_next_unprocessed_chunk(self):
        h, command, _, receipt, checkpoint, manifest, code = self._fixture(
            multi=True, source_text=MULTI_SOURCE)
        value = json.loads(checkpoint.read_text())
        records = worker.get_segment_chunk_records(
            MULTI_SOURCE, receipt['spec']['chunk_size'],
            __import__('generate_script').LLMGenParams(**receipt['spec']['params']))
        value['failed']['chunk'] = 2
        value['failed']['source'] = records[1]['text']
        value['chunks_done'] = 0
        self._write(checkpoint, value)
        progress = json.loads(manifest.read_text())['progress']
        progress['chunks_attempted'] = 0
        progress['chunks_completed'] = 0
        atomic_json_write(json.loads(manifest.read_text()), manifest)
        before = self._protected(h)
        for route in ('detail', 'inject', 'skip', 'retry', ''):
            response = (_get(h, 'recovery/detail') if route == 'detail' else
                        _post(h, route, {'chunk': 2, 'entries': []} if route == 'inject' else {}))
            self.assertEqual(409, response.status_code, (route, response.text[:200]))
        self.assertEqual(before, self._protected(h))

    def test_complete_source_checks_do_not_depend_on_attempt_details(self):
        h, command, _, _, checkpoint, manifest, code = self._fixture()
        value = json.loads(checkpoint.read_text())
        value['failed']['attempts'] = []
        value['segmented'].pop()
        value['named'].pop()
        self._write(checkpoint, value)
        for route in ('detail', 'skip', 'retry', ''):
            response = (_get(h, 'recovery/detail') if route == 'detail'
                        else _post(h, route))
            self.assertEqual(409, response.status_code, (route, response.text[:200]))

    def test_instruction_recovery_refuses_changed_retained_speaker(self):
        h, command, _, _, checkpoint, manifest, code = self._fixture(fail=('instruct',))
        value = json.loads(checkpoint.read_text())
        value['named'][0]['speaker'] = 'DANA'
        self._write(checkpoint, value)
        before = self._protected(h)
        self.assertEqual(409, _get(h, 'recovery/detail').status_code)
        self.assertEqual(409, _post(h, 'skip').status_code)
        self.assertEqual(before, self._protected(h))

    def test_worker_pins_attribution_target_and_cast_before_requests(self):
        config = _config()
        config['generation']['three_pass_attribute_target_chars'] = 250
        h = _Harness({'case': 'identity'}, config=config, cast=True)
        self.addCleanup(h.close)
        command, _, _, receipt = h.admit()
        live = json.loads((h.root / 'config.json').read_text())
        live['generation']['three_pass_attribute_target_chars'] = 750
        atomic_json_write(live, h.root / 'config.json')
        (h.root / 'script_run_cast.json').write_text('{"cast":[{"name":"OTHER"}]}')
        seen = {}
        real = worker.run_three_pass
        def observe(*args, **kwargs):
            seen['target'] = kwargs.get('attribute_target_chars')
            seen['cast_digest'] = (kwargs.get('cast') or {}).get('sha256')
            return real(*args, **kwargs)
        with patch.object(worker, 'run_three_pass', side_effect=observe):
            code = h.worker(command, _factory(_payloads()))
        self.assertNotEqual(0, code, 'must refuse changed attribution target or cast before work')

    def test_changed_configured_secondary_is_refused_in_recovery(self):
        config = _config(failover=True)
        h = _Harness({'case': 'secondary'}, config=config)
        self.addCleanup(h.close)
        command, _, _, receipt = h.admit()
        live = json.loads((h.root / 'config.json').read_text())
        live['llm_remote']['model_name'] = 'different-secondary'
        atomic_json_write(live, h.root / 'config.json')
        code = h.worker(command, _factory(_payloads(), fail=('attribute',)))
        self.assertNotEqual(0, code, 'an unadmitted secondary must be refused before work')
        self.assertFalse(_get(h, 'recovery/detail').json().get('recoverable'))

    def test_refused_start_over_preserves_prior_checkpoint_and_manifest(self):
        h, command, _, _, checkpoint, manifest, code = self._fixture()
        before = self._protected(h)
        original = script.apply_book_state_locked
        def race(*args, **kwargs):
            if 'state.json' in args[1]:
                h.source.write_text('Source changed at admission.')
            return original(*args, **kwargs)
        with patch.object(script, 'apply_book_state_locked', side_effect=race):
            response = _post(h, '', {'start_over': True})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, self._protected(h))
        self.assertEqual(len(h.scheduled), len(h.scheduled))

    def test_lost_claim_prevents_state_and_receipt_publication(self):
        h = _Harness({'case': 'lost-claim'}, config=_config())
        self.addCleanup(h.close)
        before = h.snapshot()
        claim = {}
        original_reserve = core.reserve_background_task
        def reserve(*args, **kwargs):
            claim['id'] = original_reserve(*args, **kwargs)
            return claim['id']
        original_publish = script.apply_book_state_locked
        def lose(*args, **kwargs):
            core.release_gpu_task_claim('script', claim['id'], pending_only=True)
            return original_publish(*args, **kwargs)
        with patch.object(script, 'reserve_background_task', side_effect=reserve), \
                patch.object(script, 'register_claimed_background_task',
                             side_effect=lambda *a, **kw: None), \
                patch.object(script, 'apply_book_state_locked', side_effect=lose):
            with _routes(h) as client:
                response = client.post('/api/generate_script', json={})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, h.snapshot())

    def test_strict_json_and_stage_parity_on_retry_and_generate(self):
        for mutation in ('duplicate', 'stage'):
            with self.subTest(mutation=mutation):
                h, command, _, _, checkpoint, manifest, code = self._fixture()
                if mutation == 'duplicate':
                    checkpoint.write_text(checkpoint.read_text().replace(
                        '"chunks_done":', '"chunks_done": 77, "chunks_done":', 1))
                else:
                    value = json.loads(checkpoint.read_text())
                    value['stage'] = 'segment_failed'
                    self._write(checkpoint, value)
                before = self._protected(h)
                self.assertEqual(409, _get(h, 'recovery/detail').status_code)
                self.assertEqual(409, _post(h, 'retry').status_code)
                self.assertEqual(409, _post(h, '').status_code)
                self.assertEqual(before, self._protected(h))

    def test_pre_receipt_checkpoint_manual_recovery(self):
        h, command, receipt_path, receipt, checkpoint, manifest, code = self._fixture(with_receipt=False)
        self.assertFalse(Path(receipt_path).exists())
        self.assertEqual(200, _get(h, 'recovery/detail').status_code)
        self.assertEqual(200, _post(h, 'skip').status_code)
        with patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')):
            self.assertEqual(0, h.worker(command, _factory(_payloads())))
        self.assertIsNotNone(h.rows())

    def test_deleted_required_cast_is_refused_before_recovery_write(self):
        h, command, _, _, checkpoint, manifest, code = self._fixture(cast=True)
        (h.root / 'script_run_cast.json').unlink()
        before = self._protected(h)
        self.assertEqual(409, _get(h, 'recovery/detail').status_code)
        self.assertEqual(409, _post(h, 'skip').status_code)
        self.assertEqual(before, self._protected(h))

    def test_manifest_fingerprint_and_empty_history_independently_refused(self):
        for mutation in ('fingerprint', 'history'):
            with self.subTest(mutation=mutation):
                h, command, _, _, checkpoint, manifest, code = self._fixture()
                value = json.loads(manifest.read_text())
                if mutation == 'fingerprint':
                    value['fingerprint'] = {'model_name': 'DIFFERENT'}
                else:
                    value['recovered_units'] = [{}]
                atomic_json_write(value, manifest)
                before = self._protected(h)
                self.assertEqual(409, _get(h, 'recovery/detail').status_code)
                self.assertEqual(409, _post(h, 'skip').status_code)
                self.assertEqual(before, self._protected(h))

    def test_instruct_fallback_full_row_oracle(self):
        control = _Harness({'case': 'fallback-control'}, config=_config())
        self.addCleanup(control.close)
        control_command, _, _, _ = control.admit()
        with patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')):
            control.worker(control_command, _factory(_payloads()))
        base_rows = [{key: row[key] for key in ('speaker', 'text')} for row in control.rows()]

        h, *_ = self._fixture(fail=('instruct',))
        self.assertEqual(200, _post(h, 'skip').status_code)
        response = _post(h, '')
        self.assertEqual(200, response.status_code, response.text)
        command = h.scheduled[-1][4]
        with patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')):
            self.assertEqual(0, h.worker(command, _factory(_payloads())))
        expected = [dict(row, instruct=script.default_instruct(row)) for row in base_rows]
        self.assertEqual(expected, h.rows())

    def test_segment_skip_settles_source_word_count(self):
        h, command, _, _, checkpoint, manifest, code = self._fixture(fail=('segment',))
        self.assertEqual(200, _post(h, 'skip', {'chunk': 1}).status_code)
        progress = json.loads(manifest.read_text())['progress']
        self.assertEqual(progress['source_words'], progress['source_words_completed'])


if __name__ == '__main__':
    unittest.main()
