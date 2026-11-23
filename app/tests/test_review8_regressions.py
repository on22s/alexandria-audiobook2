"""Rescore-8 confirmed findings as failing-before/passing-after regressions."""
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import three_pass_generate as worker
from llm_provider import FailoverClient, make_run_client
from utils import atomic_json_write

from tests.test_review7_regressions import MULTI_SOURCE, Review7Regressions
from tests.test_recovery_validation_matrix import (
    _Harness, _config, _digest, _factory, _payloads, core, script)


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


class Review8Regressions(unittest.TestCase):
    def _fixture(self, *, config=None, cast=False, source_text=None, fail=('attribute',),
                 multi=False, with_receipt=True):
        cfg = config or (_config(force_segment_llm=(multi or fail == ('segment',)),
                                 custom_chunk=500 if multi else None))
        h = _Harness({'case': 'review8'}, config=cfg, cast=cast, source_text=source_text)
        self.addCleanup(h.close)
        if with_receipt:
            command, receipt_path, _, admitted = h.admit()
        else:
            command, receipt_path, admitted = h.admit_legacy(), h.artifact_paths()['receipt'], None
        from contextlib import ExitStack
        import tests.test_recovery_validation_matrix as matrix
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')))
            if multi:
                original_pick = matrix._pick
                def fail_second(tag, payloads, fail_set, calls, text=''):
                    if tag == 'segment' and 'DANA walked' in text.split('SOURCE TEXT:')[-1]:
                        raise RuntimeError('fixture segment failure')
                    return original_pick(tag, payloads, fail_set, calls, text)
                stack.enter_context(patch.object(matrix, '_pick', side_effect=fail_second))
            code = h.worker(command, _factory(_payloads(), fail=fail))
        return h, command, receipt_path, admitted, code

    def _protected(self, h):
        return {name: _digest(path)
                for name, path in h.artifact_paths().items()
                if name in ('receipt', 'checkpoint', 'manifest')}

    def test_allowlisted_receipt_fields_have_no_nested_secrets(self):
        config = _config()
        config['llm']['provider_headers'] = {'X-Api-Key': 'MARK_HEADER'}
        config['llm']['access_token'] = 'MARK_ACCESS'
        config['llm']['refresh_token'] = 'MARK_REFRESH'
        config['llm']['provider_extra_body'] = {'custom': {'opaque': 'MARK_BODY'}}
        h = _Harness({'case': 'credentials'}, config=config)
        self.addCleanup(h.close)
        _, receipt_path, _, _ = h.admit()
        raw = Path(receipt_path).read_text()
        for marker in ('MARK_HEADER', 'MARK_ACCESS', 'MARK_REFRESH', 'MARK_BODY'):
            self.assertNotIn(marker, raw)

    def test_counter_consistent_truncation_is_refused_on_all_routes(self):
        h, command, _, _, _ = self._fixture()
        checkpoint = Path(h.artifact_paths()['checkpoint'])
        manifest = Path(h.artifact_paths()['manifest'])
        value = json.loads(checkpoint.read_text())
        value['segmented'].pop()
        value['named'].pop()
        atomic_json_write(value, checkpoint)
        saved = json.loads(manifest.read_text())
        saved['progress']['segmented_entries'] = len(value['segmented'])
        saved['progress']['attributed_entries'] = 1
        saved['progress'].pop('source_words', None)
        atomic_json_write(saved, manifest)
        before = self._protected(h)
        self.assertEqual(409, _get(h, 'recovery/detail').status_code)
        self.assertEqual(409, _post(h, 'skip').status_code)
        self.assertEqual(409, _post(h, 'retry').status_code)
        self.assertEqual(409, _post(h, '').status_code)
        self.assertEqual(before, self._protected(h))

    def test_claim_revocation_before_publication_rolls_back_start_over(self):
        h, command, _, _, _ = self._fixture()
        before = self._protected(h)
        scheduled_before = len(h.scheduled)
        original_register = core.register_claimed_background_task
        original_reserve = core.reserve_background_task
        claim = {}

        def reserve(*args, **kwargs):
            claim['id'] = original_reserve(*args, **kwargs)
            return claim['id']

        original_publish = script.apply_book_state_locked

        def lose_before_publish(*args, **kwargs):
            core.release_gpu_task_claim('script', claim['id'])
            return original_publish(*args, **kwargs)

        with patch.object(script, 'reserve_background_task', side_effect=reserve), \
                patch.object(script, 'apply_book_state_locked', side_effect=lose_before_publish), \
                patch.object(core, 'reserve_background_task', side_effect=reserve), \
                patch.object(core, 'register_claimed_background_task', side_effect=original_register):
            response = _post(h, '', {'start_over': True})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, self._protected(h))
        self.assertEqual(scheduled_before, len(h.scheduled))

    def test_changed_secondary_is_refused_before_publication(self):
        config = _config(failover=True)
        h = _Harness({'case': 'secondary'}, config=config)
        self.addCleanup(h.close)
        command, _, _, admitted = h.admit()
        live = json.loads((h.root / 'config.json').read_text())
        live['llm_remote']['model_name'] = 'different-secondary'
        atomic_json_write(live, h.root / 'config.json')
        seen = []
        def factory(config_value, llm, timeout):
            with patch('llm_provider.make_llm_client',
                       side_effect=lambda *a, **kw: _factory(_payloads())()):
                client = make_run_client(config_value, llm, timeout)
            self.assertIsInstance(client, FailoverClient)
            client.failover({'category': 'unavailable', 'status_code': 503})
            original = client.chat.completions.create
            def record(**kwargs):
                seen.append(kwargs.get('model'))
                return original(**kwargs)
            client.chat.completions.create = record
            return client
        code = h.worker(command, factory)
        self.assertNotEqual(0, code)
        self.assertFalse(seen, 'no request may reach an unadmitted secondary')

    def test_attribution_variant_is_pinned_from_receipt(self):
        h = _Harness({'case': 'variant'}, config=_config())
        self.addCleanup(h.close)
        command, _, _, admitted = h.admit()
        live = json.loads((h.root / 'config.json').read_text())
        live['prompts'] = {
            'attribution_preset': 'passage',
            'prompt_presets': [{'name': 'passage', 'variant': 'passage',
                                'system_prompt': 'CUSTOM', 'user_prompt': '{batch}'}]}
        atomic_json_write(live, h.root / 'config.json')
        seen = {}
        original = worker.run_three_pass
        def observe(*args, **kwargs):
            seen['variant'] = kwargs.get('attribute_prompt_variant')
            return original(*args, **kwargs)
        with patch.object(worker, 'run_three_pass', side_effect=observe):
            code = h.worker(command, _factory(_payloads()))
        self.assertEqual(0, code)
        self.assertEqual('default', seen['variant'])

    def test_manual_segmentation_cannot_rewind_or_replace_committed_prefix(self):
        for kind in ('rewind', 'bad-prefix'):
            with self.subTest(kind=kind):
                h, command, _, admitted, _ = self._fixture(multi=True, source_text=MULTI_SOURCE)
                checkpoint = Path(h.artifact_paths()['checkpoint'])
                value = json.loads(checkpoint.read_text())
                if kind == 'rewind':
                    value['failed']['chunk'] = 1
                    atomic_json_write(value, checkpoint)
                    payload = {'chunk': 1, 'entries': [{'type': 'NARRATOR', 'text': 'MIRA.'}]}
                else:
                    value['segmented'][0]['text'] = 'Invented fixture words.'
                    atomic_json_write(value, checkpoint)
                    payload = {'chunk': 2, 'entries': [
                        {'type': 'NARRATOR', 'text': 'Injected after a corrupt prefix.'}]}
                before = self._protected(h)
                self.assertEqual(409, _get(h, 'recovery/detail').status_code, kind)
                self.assertEqual(409, _post(h, 'inject', payload).status_code, kind)
                self.assertEqual(before, self._protected(h))

    def test_explicit_start_over_discards_malformed_checkpoint(self):
        h, command, _, _, _ = self._fixture()
        checkpoint = Path(h.artifact_paths()['checkpoint'])
        checkpoint.write_text(checkpoint.read_text().replace(
            '"chunks_done":', '"chunks_done": 9, "chunks_done":', 1))
        from tests.test_support import capture_script_worker_starts
        with capture_script_worker_starts() as run:
            response = _post(h, '', {'start_over': True})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(0, json.loads(checkpoint.read_bytes())['chunks_done'])
        run.assert_called_once()

    def test_cursor_and_manifest_parity_across_all_routes(self):
        for kind in ('cursor', 'manifest'):
            with self.subTest(kind=kind):
                h, command, _, admitted, _ = self._fixture()
                checkpoint = Path(h.artifact_paths()['checkpoint'])
                manifest = Path(h.artifact_paths()['manifest'])
                if kind == 'cursor':
                    value = json.loads(checkpoint.read_text())
                    value['failed']['chunk'] = 2
                    atomic_json_write(value, checkpoint)
                else:
                    value = json.loads(manifest.read_text())
                    value['fingerprint'] = {'model_name': 'DIFFERENT'}
                    atomic_json_write(value, manifest)
                before = self._protected(h)
                self.assertEqual(409, _get(h, 'recovery/detail').status_code, kind)
                self.assertEqual(409, _post(h, 'retry').status_code, kind)
                self.assertEqual(409, _post(h, '').status_code, kind)
                self.assertEqual(before, self._protected(h))

    def test_directory_cast_consumes_no_recoverable_work(self):
        h, command, _, _, _ = self._fixture(cast=True)
        cast_path = h.root / 'script_run_cast.json'
        cast_path.unlink()
        cast_path.mkdir()
        before = self._protected(h)
        self.assertEqual(409, _get(h, 'recovery/detail').status_code)
        self.assertEqual(409, _post(h, 'skip').status_code)
        self.assertEqual(before, self._protected(h))

    def test_hyphenated_word_count_uses_one_convention(self):
        source = '"MIRA walked." DANA-well-known looked on. ' * 6
        h, command, _, admitted, _ = self._fixture(fail=('segment',), source_text=source)
        self.assertEqual(200, _post(h, 'skip', {'chunk': 1}).status_code)
        progress = json.loads(Path(h.artifact_paths()['manifest']).read_text())['progress']
        self.assertLessEqual(progress['source_words_completed'], progress['source_words'])

    def test_runtime_preparation_uses_admitted_endpoint(self):
        h = _Harness({'case': 'heal'}, config=_config())
        self.addCleanup(h.close)
        command, _, _, admitted = h.admit()
        live = json.loads((h.root / 'config.json').read_text())
        live['llm']['base_url'] = 'http://localhost:9898/v1'
        live['llm']['api_key'] = 'KEY_B'
        atomic_json_write(live, h.root / 'config.json')
        seen = []
        def heal(mode, url, model, **kwargs):
            seen.append((url, model, kwargs.get('api_key')))
            return None, {}, 'offline'
        with patch.object(worker, 'ensure_ideal_settings', side_effect=heal):
            code = h.worker(command, _factory(_payloads()))
        self.assertNotEqual(0, code)
        self.assertFalse(seen, 'runtime self-heal must not run on a rejected endpoint')


if __name__ == '__main__':
    unittest.main()
