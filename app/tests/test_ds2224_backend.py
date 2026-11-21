"""Backend guards for dataset-builder row CAS, persisted metadata and safe acks."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
from routers import dataset_builder as routes


class Ds2224BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.builder = self.root / 'builder'
        self.data = self.root / 'data'
        self.builder.mkdir()
        self.data.mkdir()
        source = self.root / 'source.wav'
        sf.write(source, np.full(240, 0.25), 24000)

        def render(**kwargs):
            sf.write(source, np.full(240, 0.25), 24000)
            return str(source), kwargs['sample_text']

        self.engine = SimpleNamespace(generate_voice_design=Mock(side_effect=render))
        self.patches = [
            patch.object(routes, 'DATASET_BUILDER_DIR', str(self.builder)),
            patch.object(core, 'DATA_DIR', str(self.data)),
            patch.object(routes, 'process_state', {
                'dataset_builder': {'running': False, 'logs': [], 'cancel': False}}),
            patch.object(routes, 'check_global_gpu_lock'),
            patch.object(routes.project_manager, 'get_engine', return_value=self.engine),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = FastAPI()
        self.app.add_exception_handler(core.AppError, core.handle_app_error)
        self.app.include_router(routes.router)

    def save(self, state, name='voice'):
        work = self.builder / name
        work.mkdir(exist_ok=True)
        path = work / 'state.json'
        path.write_text(json.dumps(state), encoding='utf-8')
        return path

    def load(self, name='voice'):
        return routes._load_builder_state(name)

    def revision(self, row):
        return routes.get_dataset_row_revision(row)

    def test_ds2224_full_list_cas_rejects_stale_snapshot_without_writing(self):
        path = self.save({'description': 'root', 'global_seed': '', 'samples': [
            {'text': 'one', 'emotion': '', 'seed': '', 'status': 'done'},
            {'text': 'two', 'emotion': '', 'seed': '', 'status': 'pending'}]})
        before = path.read_bytes()
        revisions = {index: self.revision(row)
                     for index, row in enumerate(self.load()['samples'])}
        rows = [{'text': 'changed', 'emotion': '', 'seed': ''},
                {'text': 'other', 'emotion': '', 'seed': ''}]
        stale = {**revisions, 1: '0' * 64}
        with TestClient(self.app) as client:
            conflict = client.post('/api/dataset_builder/update_rows', json={
                'name': 'voice', 'rows': rows,
                'expected_count': 2, 'expected_revisions': stale})
            missing_count = client.post('/api/dataset_builder/update_rows', json={
                'name': 'voice', 'rows': rows, 'expected_revisions': revisions})
            extra_key = client.post('/api/dataset_builder/update_rows', json={
                'name': 'voice', 'rows': rows, 'expected_count': 2,
                'expected_revisions': {**revisions, 2: 'a' * 64}})
            self.assertEqual(before, path.read_bytes())
            legacy = client.post('/api/dataset_builder/update_rows', json={
                'name': 'voice', 'rows': rows})
            current = {index: self.revision(row)
                       for index, row in enumerate(self.load()['samples'])}
            accepted = client.post('/api/dataset_builder/update_rows', json={
                'name': 'voice', 'rows': rows,
                'expected_count': 2, 'expected_revisions': current})
        for response in (conflict, missing_count, extra_key):
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual('Dataset rows changed; reload before saving',
                             response.json()['detail'])
        self.assertEqual(200, legacy.status_code, legacy.text)
        self.assertEqual(200, accepted.status_code, accepted.text)
        self.assertEqual(['changed', 'other'],
                         [row['text'] for row in self.load()['samples']])
        self.assertEqual([self.revision(row) for row in self.load()['samples']],
                         accepted.json()['row_revisions'])

    def test_ds2224_configured_empty_project_uses_persisted_description_and_seed(self):
        self.save({'description': 'configured voice', 'global_seed': '37', 'samples': []})
        captured = {}

        def start(*args, **kwargs):
            captured['task'], captured['resolved'] = args[2], args[3]
            captured['definitions'] = args[4]
            return SimpleNamespace()

        body = {'name': 'voice', 'description': 'posted root',
                'samples': [{'text': 'later'}], 'indices': [0], 'global_seed': 5}
        with patch.object(routes, 'reserve_background_task', return_value='owned'), \
             patch.object(routes, 'apply_builder_task_owner'), \
             patch.object(routes, 'start_claimed_task_thread', side_effect=start), \
             patch.object(routes, 'release_gpu_task_claim'), \
             TestClient(self.app) as client:
            first = client.post('/api/dataset_builder/generate_batch', json=body)
            self.assertEqual(200, first.status_code, first.text)
            initial = captured['resolved'][0]
            captured['task'](captured['resolved'], captured['definitions'])
            self.assertEqual('configured voice', self.load()['samples'][0]['description'])
            self.assertEqual('configured voice',
                             self.engine.generate_voice_design.call_args.kwargs['description'])
            captured.clear()
            second = client.post('/api/dataset_builder/generate_batch', json=body)
            self.assertEqual(200, second.status_code, second.text)
            renewed = captured['resolved'][0]
        self.assertEqual(('configured voice', 37),
                         (initial['description'], initial['generated_seed']))
        self.assertEqual(('configured voice', 37),
                         (renewed['description'], renewed['generated_seed']))

    def test_ds2224_placeholder_keeps_shape_after_meta_and_uses_recovery_text(self):
        self.save({'description': 'configured voice', 'global_seed': '37',
                   'samples': [{'status': 'pending'}]})
        with patch.object(routes, 'claim_gpu_task', return_value='owned'), \
             patch.object(routes, 'release_gpu_task_claim'), \
             TestClient(self.app) as client:
            meta = client.post('/api/dataset_builder/update_meta', json={
                'name': 'voice', 'description': 'new root', 'global_seed': '37'})
            self.assertEqual(200, meta.status_code, meta.text)
            stored = self.load()['samples'][0]
            self.assertEqual({'status': 'pending'}, stored)
            token = meta.json()['row_revisions'][0]
            self.assertEqual(self.revision(stored), token)
            generated = client.post('/api/dataset_builder/generate_sample', json={
                'dataset_name': 'voice', 'sample_index': 0,
                'description': 'posted voice', 'text': 'recovered line',
                'seed': 0, 'inherit_seed': True,
                'expected_count': 1, 'expected_revision': token})
        self.assertEqual(200, generated.status_code, generated.text)
        call = self.engine.generate_voice_design.call_args.kwargs
        self.assertEqual(('recovered line', 'new root', 37),
                         (call['sample_text'], call['description'], call['seed']))
        self.assertEqual('recovered line', self.load()['samples'][0]['text'])

    def test_ds2224_unchanged_legacy_description_survives_full_list_save(self):
        self.save({'description': '', 'global_seed': '', 'samples': [
            {'text': 'real fixture', 'emotion': 'calm',
             'description': 'original voice, calm', 'status': 'done',
             'audio_url': '/old.wav', 'generated_seed': -1},
            {'status': 'pending'}]})
        with patch.object(routes, 'claim_gpu_task', return_value='owned'), \
             patch.object(routes, 'release_gpu_task_claim'), \
             TestClient(self.app) as client:
            saved = client.post('/api/dataset_builder/update_rows', json={
                'name': 'voice',
                'rows': [{'text': 'real fixture', 'emotion': 'calm', 'seed': ''},
                         {'text': 'appended', 'emotion': '', 'seed': ''}]})
            self.assertEqual(200, saved.status_code, saved.text)
            row0 = self.load()['samples'][0]
            self.assertEqual('original voice, calm', row0['description'])
            self.assertEqual('done', row0['status'])
            self.assertEqual(-1, row0['generated_seed'])
            token = saved.json()['row_revisions'][0]
            self.assertEqual(self.revision(row0), token)
            generated = client.post('/api/dataset_builder/generate_sample', json={
                'dataset_name': 'voice', 'sample_index': 0,
                'description': 'posted root', 'text': 'real fixture', 'seed': 9,
                'expected_count': 2, 'expected_revision': token})
        self.assertEqual(200, generated.status_code, generated.text)
        self.assertEqual('original voice, calm',
                         self.engine.generate_voice_design.call_args.kwargs['description'])

    def test_ds2224_lstat_permission_error_is_not_first_run_or_missing(self):
        expected = {'description': 'root', 'global_seed': '', 'samples': [
            {'text': 'existing', 'emotion': '', 'seed': ''}]}
        self.save(expected)
        state_path = str(self.builder / 'voice' / 'state.json')
        original_lstat = os.lstat

        def denied(target, *args, **kwargs):
            if os.fspath(target) == state_path:
                raise PermissionError(13, 'Permission denied', state_path)
            return original_lstat(target, *args, **kwargs)

        with patch.object(routes.os, 'lstat', side_effect=denied), \
             patch.object(routes, 'claim_gpu_task') as single_claim, \
             patch.object(routes, 'reserve_background_task') as batch_claim, \
             TestClient(self.app) as client:
            single = client.post('/api/dataset_builder/generate_sample', json={
                'dataset_name': 'voice', 'sample_index': 0,
                'description': 'root', 'text': 'existing', 'seed': 0})
            batch = client.post('/api/dataset_builder/generate_batch', json={
                'name': 'voice', 'description': 'root',
                'samples': [{'text': 'existing'}], 'indices': [0]})
            status = client.get('/api/dataset_builder/status/voice')
        self.assertEqual(500, single.status_code, single.text)
        self.assertEqual('error.dataset.state_unreadable', single.json()['message_key'])
        self.assertEqual(500, batch.status_code, batch.text)
        self.assertEqual('error.dataset.state_unreadable', batch.json()['message_key'])
        self.assertEqual(500, status.status_code, status.text)
        self.assertEqual('error.dataset.state_unreadable', status.json()['message_key'])
        self.assertIn('could not be read', status.json()['detail'])
        single_claim.assert_not_called()
        batch_claim.assert_not_called()
        self.assertEqual(expected, self.load())
        Path(state_path).write_bytes(b'{')
        with TestClient(self.app) as client:
            truncated = client.get('/api/dataset_builder/status/voice')
        self.assertEqual(500, truncated.status_code, truncated.text)
        self.assertEqual('error.dataset.state_unreadable', truncated.json()['message_key'])
        self.assertEqual(b'{', Path(state_path).read_bytes())

    def test_ds2224_update_meta_hides_diagnostic_but_token_matches_full_record(self):
        self.save({'description': 'root', 'global_seed': '', 'samples': [
            {'text': 'failed', 'emotion': '', 'seed': '', 'status': 'error',
             'error': 'PRIVATE-DS2224-DIAGNOSTIC'},
            {'text': 'healthy', 'emotion': '', 'seed': '', 'status': 'done'}]})
        with TestClient(self.app) as client:
            response = client.post('/api/dataset_builder/update_meta', json={
                'name': 'voice', 'description': 'root', 'global_seed': ''})
            status = client.get('/api/dataset_builder/status/voice')
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn('PRIVATE-DS2224-DIAGNOSTIC', response.text)
        body = response.json()
        self.assertNotIn('PRIVATE-DS2224-DIAGNOSTIC', json.dumps(body['samples']))
        self.assertNotIn('error', body['samples'][0])
        full = self.load()['samples']
        self.assertIn('error', full[0])
        self.assertEqual([self.revision(row) for row in full], body['row_revisions'])
        self.assertEqual(200, status.status_code, status.text)
        self.assertNotIn('PRIVATE-DS2224-DIAGNOSTIC', status.text)
        self.assertEqual(body['row_revisions'], status.json()['row_revisions'])
        with patch.object(routes, 'claim_gpu_task', return_value='owned'), \
             patch.object(routes, 'release_gpu_task_claim'), \
             TestClient(self.app) as client:
            retry = client.post('/api/dataset_builder/generate_sample', json={
                'dataset_name': 'voice', 'sample_index': 0,
                'description': 'root', 'text': 'failed', 'seed': 0,
                'expected_count': 2, 'expected_revision': status.json()['row_revisions'][0]})
        self.assertEqual(200, retry.status_code, retry.text)

    def test_ds2224_empty_resolved_text_is_refused_without_engine_call(self):
        self.save({'description': 'root', 'global_seed': '', 'samples': [
            {'text': '   ', 'emotion': '', 'seed': '', 'status': 'pending'}]})
        token = self.revision(self.load()['samples'][0])
        with patch.object(routes, 'reserve_background_task') as reserve, \
             TestClient(self.app) as client:
            batch = client.post('/api/dataset_builder/generate_batch', json={
                'name': 'voice', 'description': 'root',
                'samples': [{'text': ''}], 'indices': [0],
                'expected_count': 1, 'expected_revisions': {'0': token}})
        self.assertEqual(400, batch.status_code, batch.text)
        self.assertEqual('error.dataset.empty_generated_text', batch.json()['message_key'])
        reserve.assert_not_called()
        self.engine.generate_voice_design.assert_not_called()

    def test_ds2224_batch_regeneration_keeps_legacy_row_description(self):
        self.save({'description': '', 'global_seed': '', 'samples': [
            {'text': 'real fixture', 'emotion': 'calm',
             'description': 'original voice, calm', 'status': 'done',
             'audio_url': '/old.wav'}]})
        token = self.revision(self.load()['samples'][0])
        captured = {}

        def start(*args, **kwargs):
            captured['task'], captured['resolved'] = args[2], args[3]
            captured['definitions'] = args[4]
            return SimpleNamespace()

        with patch.object(routes, 'reserve_background_task', return_value='owned'), \
             patch.object(routes, 'apply_builder_task_owner'), \
             patch.object(routes, 'start_claimed_task_thread', side_effect=start), \
             patch.object(routes, 'release_gpu_task_claim'), \
             TestClient(self.app) as client:
            response = client.post('/api/dataset_builder/generate_batch', json={
                'name': 'voice', 'description': 'posted root',
                'samples': [{'text': 'real fixture', 'emotion': 'calm'}], 'indices': [0],
                'expected_count': 1, 'expected_revisions': {'0': token}})
            self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('original voice, calm', captured['resolved'][0]['description'])


if __name__ == '__main__':
    unittest.main()
