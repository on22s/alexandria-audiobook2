"""Replay late Start Over boundaries with an indexed worker checkpoint."""
import errno
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from generation_checkpoint_deltas import GenerationCheckpointDeltas, load_generation_delta_checkpoint
from llm_provider import make_run_client
from tests import test_review9_regressions as fixtures
from tests.test_profile_failover import SERVER_ERROR
from tests.test_support import capture_script_worker_starts
from utils import atomic_json_write

core = fixtures.core
script = fixtures.script
worker = fixtures.worker


class Review11Regressions(unittest.TestCase):
    def _sharded_fixture(self):
        fixture = fixtures.Review9Regressions()
        self.addCleanup(fixture.doCleanups)
        h, _, _, _ = fixture._fixture(cast=True)
        cp = Path(h.artifact_paths()['checkpoint'])
        saved = load_generation_delta_checkpoint(cp)
        delta = GenerationCheckpointDeltas(cp)
        delta.save_checkpoint(dict(saved, stage='attribute'))
        delta.save_checkpoint(saved)
        self.assertEqual(saved, load_generation_delta_checkpoint(cp))
        self.assertTrue(list(delta.directory.iterdir()))
        cast = h.root / 'script_run_cast.json'
        next((h.root / 'cast_lists').glob('*.json')).write_bytes(
            b'{"cast":[{"name":"MIRA"},{"name":"DANA"},{"name":"OTHER"}]}')
        return h, cp, delta.directory, cast, saved

    def _start_over(self, h):
        app = FastAPI()
        app.add_middleware(core.TaskClaimMiddleware)
        app.include_router(script.router)
        with patch.object(core, 'DATA_DIR', str(h.root)), \
                patch.object(script, 'reserve_background_task', side_effect=core.reserve_background_task), \
                patch.object(script, 'register_claimed_background_task',
                             side_effect=core.register_claimed_background_task), \
                TestClient(app, raise_server_exceptions=False) as client:
            return client.post('/api/generate_script', json={'start_over': True})

    def test_enospc_staging_shard_retirement_preserves_usable_recovery(self):
        h, checkpoint, shards, cast, saved = self._sharded_fixture()
        before = h.snapshot(), cast.read_bytes()
        shard_proof = {p.name: (p.stat().st_ino, p.read_bytes()) for p in shards.iterdir()}
        apply = script.apply_book_state_locked
        mkdir = os.mkdir
        failures = []

        def fail_mkdir(name, *args, **kwargs):
            if '.book-switch-' in str(name):
                failures.append(name)
                raise OSError(errno.ENOSPC, 'synthetic out of space')
            return mkdir(name, *args, **kwargs)

        def publish(root, replacements, removals, **kwargs):
            if shards.name in removals:
                with patch.object(os, 'mkdir', side_effect=fail_mkdir):
                    return apply(root, replacements, removals, **kwargs)
            return apply(root, replacements, removals, **kwargs)

        with patch.object(script, 'apply_book_state_locked', side_effect=publish), \
                capture_script_worker_starts() as run:
            response = self._start_over(h)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(1, len(failures))
        run.assert_not_called()
        self.assertNotIn('script', core._task_claims)
        self.assertEqual(before, (h.snapshot(), cast.read_bytes()))
        self.assertEqual(shard_proof, {p.name: (p.stat().st_ino, p.read_bytes()) for p in shards.iterdir()})
        self.assertEqual(saved, load_generation_delta_checkpoint(checkpoint))

        self.assertEqual(200, fixtures._get(h, 'recovery/detail').status_code)
        self.assertEqual(200, fixtures._post(h, 'skip').status_code)
        self.assertEqual(200, fixtures._post(h, 'retry').status_code)
        with patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')):
            self.assertEqual(0, h.worker(h.scheduled[-1][4], fixtures._factory(fixtures._payloads())))
        self.assertEqual(4, len(h.rows()))

    def test_pending_cleanup_cannot_replace_started_owner_after_shard_commit(self):
        h, checkpoint, shards, _, _ = self._sharded_fixture()
        apply = script.apply_book_state_locked
        competing = []
        worker_results = []
        manifest = Path(h.artifact_paths()['manifest'])

        def publish(root, replacements, removals, **kwargs):
            result = apply(root, replacements, removals, **kwargs)
            if shards.name in removals:
                self.assertFalse(shards.exists())
                core.release_pending_task_claims()
                try:
                    competing.append(core.reserve_background_task('script'))
                except HTTPException as conflict:
                    competing.append(conflict.status_code)
            return result

        def run(command, name):
            claim = core._task_claims[name]
            worker_results.append((
                claim['phase'], claim['id'],
                load_generation_delta_checkpoint(checkpoint)['chunks_done'],
                json.loads(manifest.read_bytes())['status'], not shards.exists()))
            worker_results.append(h.worker(command, fixtures._factory(fixtures._payloads())))

        with patch.object(script, 'apply_book_state_locked', side_effect=publish), \
                patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')), \
                capture_script_worker_starts(run) as start:
            response = self._start_over(h)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({'status': 'started'}, response.json())
        start.assert_called_once()
        self.assertEqual([400], competing)
        owner = script.run_spec_receipt.load_receipt(h.root)['run_id']
        self.assertEqual([('started', owner, 0, 'incomplete', True), 0], worker_results)
        self.assertEqual(4, len(h.rows()))

    def test_explicit_start_over_can_replace_malformed_receipt_after_adoption(self):
        h, checkpoint, shards, cast, saved = self._sharded_fixture()
        receipt_path = Path(h.artifact_paths()['receipt'])
        receipt_path.write_bytes(b'{')
        before = h.snapshot(), cast.read_bytes()
        self.assertEqual(409, fixtures._get(h, 'recovery/detail').status_code)
        self.assertEqual(409, fixtures._post(h, '').status_code)
        self.assertEqual(before, (h.snapshot(), cast.read_bytes()))

        adopt = core.start_claimed_task_thread

        def lose_pending(name, claim_id, *args):
            self.assertTrue(core.release_gpu_task_claim(name, claim_id, pending_only=True))
            return adopt(name, claim_id, *args)

        with patch.object(core, 'start_claimed_task_thread', side_effect=lose_pending), \
                patch.object(script, 'run_process') as run:
            refused = self._start_over(h)
        self.assertEqual(409, refused.status_code, refused.text)
        run.assert_not_called()
        self.assertEqual(before, (h.snapshot(), cast.read_bytes()))
        self.assertEqual(saved, load_generation_delta_checkpoint(checkpoint))

        with capture_script_worker_starts() as run:
            accepted = self._start_over(h)
        self.assertEqual(200, accepted.status_code, accepted.text)
        run.assert_called_once()
        command, _ = run.call_args.args
        self.assertEqual(script.run_spec_receipt.load_receipt_file(receipt_path)['sha256'],
                         command[command.index('--run-spec-digest') + 1])
        self.assertEqual(0, load_generation_delta_checkpoint(checkpoint)['chunks_done'])
        self.assertFalse(shards.exists())

    def test_gateway_query_is_private_and_bound_on_both_runtime_profiles(self):
        config = fixtures._config(failover=True)
        primary = config['llm']
        secondary = config['llm_remote']
        primary['base_url'] += '?subscription-key=SYNTH_APIM_KEY&route=west'
        secondary['base_url'] += '?code=SYNTH_FUNCTION_KEY&route=east'
        h = fixtures._Harness({'case': 'gateway-credential-query'}, config=config)
        self.addCleanup(h.close)
        command, receipt_path, _, _ = h.admit()
        raw = Path(receipt_path).read_text()
        for field in ('subscription-key', 'SYNTH_APIM_KEY', 'code=', 'SYNTH_FUNCTION_KEY', 'route='):
            self.assertNotIn(field, raw)

        requests = []

        def factory(cfg, llm, timeout):
            def transport(profile, *_args, **_kwargs):
                client = fixtures._factory(fixtures._payloads())()
                create = client.chat.completions.create

                def request(**kwargs):
                    requests.append(profile['base_url'])
                    if profile['base_url'] == primary['base_url']:
                        raise SERVER_ERROR('synthetic provider unavailable')
                    return create(**kwargs)

                client.chat.completions.create = request
                return client

            with patch('llm_provider.make_llm_client', side_effect=transport):
                return make_run_client(cfg, llm, timeout)

        with patch.object(worker, 'ensure_ideal_settings', return_value=(None, {}, 'offline')):
            self.assertEqual(0, h.worker(command, factory))
        self.assertEqual([primary['base_url']] * 4 + [secondary['base_url']] * 2, requests)
        self.assertEqual(4, len(h.rows()))

        for profile, old, new in ((primary, 'west', 'east'), (secondary, 'east', 'west')):
            with self.subTest(profile=profile['model_name']):
                url = profile['base_url']
                profile['base_url'] = url.replace('route=' + old, 'route=' + new)
                try:
                    atomic_json_write(config, h.root / 'config.json')
                    refused = Mock()
                    self.assertNotEqual(0, h.worker(command, refused))
                    refused.assert_not_called()
                finally:
                    profile['base_url'] = url
