"""Rescore-9 confirmed outcomes as failing-before/passing-after regressions."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import three_pass_generate as worker
from generation_checkpoint_deltas import GenerationCheckpointDeltas, load_generation_delta_checkpoint
from utils import atomic_json_write

from tests.test_review7_regressions import MULTI_SOURCE, Review7Regressions
from tests.test_recovery_validation_matrix import (
    SOURCE, _Harness, _config, _digest, _factory, _payloads, core, script)
from tests.test_support import capture_script_worker_starts


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


class Review9Regressions(unittest.TestCase):
    def _fixture(self, *, config=None, source_text=None, fail=('attribute',), multi=False,
                 cast=False):
        cfg = config or (_config(force_segment_llm=(multi or fail == ('segment',)),
                                 custom_chunk=500 if multi else None))
        h = _Harness({'case': 'review9'}, config=cfg, source_text=source_text, cast=cast)
        self.addCleanup(h.close)
        command, _, _, admitted = h.admit()
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
        return h, command, admitted, code

    def _protected(self, h):
        return {name: _digest(path) for name, path in h.artifact_paths().items()
                if name in ('receipt', 'checkpoint', 'manifest')}

    def test_completeness_without_source_words_total(self):
        h, command, admitted, code = self._fixture()
        checkpoint = Path(h.artifact_paths()['checkpoint'])
        manifest = Path(h.artifact_paths()['manifest'])
        value = json.loads(checkpoint.read_text())
        value['segmented'].pop()
        value['named'].pop()
        atomic_json_write(value, checkpoint)
        saved = json.loads(manifest.read_text())
        saved['progress'].pop('source_words_total', None)
        saved['progress'].pop('source_words', None)
        saved['progress']['segmented_entries'] = len(value['segmented'])
        saved['progress']['attributed_entries'] = 1
        atomic_json_write(saved, manifest)
        before = self._protected(h)
        for status, response in (
                (409, _get(h, 'recovery/detail')), (409, _post(h, 'skip')),
                (409, _post(h, 'retry')), (409, _post(h, '')),
                (409, _post(h, 'inject', {'entries': [{'n': 0, 'speaker': 'MIRA'}]}))):
            self.assertEqual(status, response.status_code, response.text[:200])
        self.assertEqual(before, self._protected(h))

    def test_generate_claim_loss_after_registration_rolls_back(self):
        h, command, _, _ = self._fixture()
        before = self._protected(h)
        published = {}
        original = script.apply_book_state_locked

        def publish(*args, **kwargs):
            result = original(*args, **kwargs)
            published['done'] = True
            return result

        original_register = core.register_claimed_background_task
        def register(*args, **kwargs):
            result = original_register(*args, **kwargs)
            claim = core._task_claims.get('script')
            if claim:
                core.release_gpu_task_claim('script', claim['id'], pending_only=True)
            return result

        with patch.object(script, 'apply_book_state_locked', side_effect=publish), \
                patch.object(script, 'register_claimed_background_task', side_effect=register), \
                patch.object(core, 'register_claimed_background_task', side_effect=register):
            response = _post(h, '')
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, self._protected(h))

    def test_start_over_preserves_work_when_reservation_or_adoption_loses_claim(self):
        start = core.start_claimed_task_thread
        for boundary in ('reserve-return', 'adoption-entry'):
            with self.subTest(boundary=boundary):
                h, _, _, _ = self._fixture(cast=True)
                cast = h.root / 'script_run_cast.json'
                before = h.snapshot(), cast.read_bytes()
                next((h.root / 'cast_lists').glob('*.json')).write_bytes(
                    b'{"cast":[{"name":"OTHER"}]}')

                def reserve(*args, **kwargs):
                    claim_id = core.reserve_background_task(*args, **kwargs)
                    if boundary == 'reserve-return':
                        self.assertTrue(core.release_gpu_task_claim('script', claim_id, pending_only=True))
                    return claim_id

                def adopt(name, claim_id, *args):
                    if boundary == 'adoption-entry':
                        self.assertTrue(core.release_gpu_task_claim(name, claim_id, pending_only=True))
                    return start(name, claim_id, *args)

                with patch.object(core, 'DATA_DIR', str(h.root)), \
                        patch.object(script, 'reserve_background_task', side_effect=reserve), \
                        patch.object(core, 'start_claimed_task_thread', side_effect=adopt), \
                        patch.object(script, 'run_process') as run:
                    response = _post(h, '', {'start_over': True})
                self.assertEqual(409, response.status_code, response.text)
                run.assert_not_called()
                self.assertEqual(before, (h.snapshot(), cast.read_bytes()))

    def test_start_over_rollback_preserves_indexed_checkpoint_and_shards(self):
        h, _, _, _ = self._fixture()
        path = Path(h.artifact_paths()['checkpoint'])
        saved = load_generation_delta_checkpoint(path)
        writer = GenerationCheckpointDeltas(path)
        writer.save_checkpoint(dict(saved, stage='attribute'))
        writer.save_checkpoint(saved)
        self.assertEqual(saved, load_generation_delta_checkpoint(path))
        shards = {file: file.read_bytes() for file in writer.directory.iterdir()}
        self.assertTrue(shards)
        before = h.snapshot()
        apply = script.apply_book_state_locked

        def revoke_before_publication(*args, **kwargs):
            claim_id = core._task_claims['script']['id']
            self.assertTrue(core.release_gpu_task_claim('script', claim_id))
            return apply(*args, **kwargs)

        with patch.object(core, 'DATA_DIR', str(h.root)), \
                patch.object(script, 'reserve_background_task', side_effect=core.reserve_background_task), \
                patch.object(script, 'apply_book_state_locked', side_effect=revoke_before_publication), \
                capture_script_worker_starts() as run:
            response = _post(h, '', {'start_over': True})
        self.assertEqual(409, response.status_code, response.text)
        run.assert_not_called()
        self.assertEqual(before, h.snapshot())
        self.assertEqual(shards, {file: file.read_bytes() for file in writer.directory.iterdir()})
        self.assertEqual(saved, load_generation_delta_checkpoint(path))
        self.assertEqual(1, len(h.scheduled))

        with capture_script_worker_starts() as run:
            accepted = _post(h, '', {'start_over': True})
        self.assertEqual(200, accepted.status_code, accepted.text)
        run.assert_called_once()
        self.assertEqual(0, load_generation_delta_checkpoint(path)['chunks_done'])
        self.assertFalse(writer.directory.exists())

    def test_attribution_rejects_self_consistent_partial_segmentation(self):
        for action in ('detail', 'inject', 'skip', 'retry', 'generate'):
            with self.subTest(action=action):
                h, _, admitted, _ = self._fixture(config=_config(custom_chunk=500),
                                                  source_text='\n\n'.join([SOURCE] * 6))
                cp = Path(h.artifact_paths()['checkpoint'])
                mp = Path(h.artifact_paths()['manifest'])
                saved = json.loads(cp.read_bytes())
                manifest = json.loads(mp.read_bytes())
                self.assertGreater(admitted['chunk_count'], 1)
                self.assertEqual(admitted['chunk_count'], saved['chunks_done'])
                chunks = script._admitted_chunk_texts(h.source.read_text(), admitted['spec'])
                words = len(script._source_words(chunks[0]))
                row_count = 0
                while len(script._source_words(' '.join(
                        row['text'] for row in saved['segmented'][:row_count]))) < words:
                    row_count += 1
                    self.assertLess(row_count, len(saved['segmented']))
                saved.update(chunks_done=1, segmented=saved['segmented'][:row_count],
                             named=saved['named'][:row_count], resolutions=saved['resolutions'][:1])
                self.assertTrue(script._completed_prefix_matches(saved['segmented'], chunks, 1))
                indices = [index for index in saved['failed']['indices'] if index < row_count]
                self.assertTrue(indices)
                saved['failed'].update(indices=indices, entries=[saved['segmented'][i] for i in indices])
                progress = manifest['progress']
                for key in ('source_words', 'source_words_total'):
                    progress.pop(key, None)
                progress.update(chunks_attempted=1, chunks_completed=1, segmented_entries=row_count,
                                attributed_entries=sum(isinstance(row, dict) for row in saved['named']))
                manifest['chunks'] = manifest['chunks'][:1]
                atomic_json_write(saved, cp)
                atomic_json_write(manifest, mp)
                before = h.snapshot()
                if action == 'detail':
                    response = _get(h, 'recovery/detail')
                else:
                    payload = {'entries': [{'n': i, 'speaker': 'MIRA'} for i in indices]} if action == 'inject' else {}
                    response = _post(h, '' if action == 'generate' else action, payload)
                self.assertEqual(409, response.status_code, response.text[:240])
                self.assertEqual(before, h.snapshot())
                self.assertEqual(1, len(h.scheduled))

    def test_ahead_of_cursor_parity_all_routes(self):
        h, command, admitted, _ = self._fixture(multi=True, source_text=MULTI_SOURCE)
        checkpoint = Path(h.artifact_paths()['checkpoint'])
        value = json.loads(checkpoint.read_text())
        value['failed']['chunk'] = 3
        atomic_json_write(value, checkpoint)
        before = self._protected(h)
        self.assertEqual(409, _get(h, 'recovery/detail').status_code)
        self.assertEqual(409, _post(h, 'inject', {'chunk': 3, 'entries': []}).status_code)
        self.assertEqual(409, _post(h, 'skip', {'chunk': 3}).status_code)
        self.assertEqual(409, _post(h, 'retry').status_code)
        self.assertEqual(409, _post(h, '').status_code)
        self.assertEqual(before, self._protected(h))

    def test_secondary_url_change_is_refused_before_requests(self):
        config = _config(failover=True)
        h = _Harness({'case': 'secondary-url'}, config=config)
        self.addCleanup(h.close)
        command, _, _, admitted = h.admit()
        live = json.loads((h.root / 'config.json').read_text())
        live['llm_remote']['base_url'] = 'http://localhost:19993/v1'
        atomic_json_write(live, h.root / 'config.json')
        seen = []
        def factory(config_value, llm, timeout):
            from llm_provider import FailoverClient, make_run_client
            def make_client(profile, *_a, **_kw):
                fake = _factory(_payloads())()
                create = fake.chat.completions.create
                def record(**kw):
                    seen.append((profile.get('base_url'), kw.get('model')))
                    return create(**kw)
                fake.chat.completions.create = record
                return fake
            with patch('llm_provider.make_llm_client', side_effect=make_client):
                client = make_run_client(config_value, llm, timeout)
            assert isinstance(client, FailoverClient)
            client.failover({'category': 'unavailable', 'status_code': 503})
            return client
        code = h.worker(command, factory)
        self.assertNotEqual(0, code)
        self.assertFalse(seen, 'no request may reach the unadmitted secondary URL')

    def test_no_url_userinfo_credentials_in_receipt(self):
        config = _config()
        config['llm']['base_url'] = 'http://USER:TEST_SECRET@127.0.0.1:1234/v1'
        h = _Harness({'case': 'userinfo'}, config=config)
        self.addCleanup(h.close)
        _, receipt_path, _, admitted = h.admit()
        raw = Path(receipt_path).read_text()
        self.assertNotIn('TEST_SECRET', raw)
        self.assertNotIn('USER:', raw)

    def test_worker_uses_configured_url_with_userinfo(self):
        config = _config()
        url = 'http://fixture:SYNTH_USERINFO@localhost:1234/v1'
        config['llm']['base_url'] = url
        h = _Harness({'case': 'userinfo-worker'}, config=config)
        self.addCleanup(h.close)
        command, receipt_path, _, _ = h.admit()
        self.assertNotIn('SYNTH_USERINFO', Path(receipt_path).read_text())
        urls = []

        def factory(config_value, llm, timeout):
            urls.append(llm['base_url'])
            return _factory(_payloads())()

        with patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')):
            self.assertEqual(0, h.worker(command, factory))
        self.assertEqual([url], urls)
        self.assertEqual(4, len(h.rows()))

    def test_receipt_strips_query_credentials_from_both_endpoints(self):
        config = _config(failover=True)
        primary = config['llm']
        secondary = config['llm_remote']
        primary['base_url'] += '?api-version=1&api%5Fkey=SYNTH_PRIMARY_QUERY'
        secondary['base_url'] += '?access_token=SYNTH_SECONDARY_QUERY&route=west'
        h = _Harness({'case': 'endpoint-query'}, config=config)
        self.addCleanup(h.close)
        command, receipt_path, _, receipt = h.admit()
        raw = Path(receipt_path).read_text()
        self.assertNotIn('SYNTH_PRIMARY_QUERY', raw)
        self.assertNotIn('SYNTH_SECONDARY_QUERY', raw)
        self.assertNotIn('api-version=1', raw)
        self.assertNotIn('route=west', raw)

        secondary['base_url'] = secondary['base_url'].replace('route=west', 'route=east')
        atomic_json_write(config, h.root / 'config.json')
        factory = Mock()
        self.assertNotEqual(0, h.worker(command, factory))
        factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
