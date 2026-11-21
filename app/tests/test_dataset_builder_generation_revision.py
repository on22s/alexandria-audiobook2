"""Generation bindings must reject stale row snapshots without touching state or files."""
import copy
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
from task_ownership import acquire_task_lease


class DatasetGenerationRevisionTests(unittest.TestCase):
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
        self.audio = source.read_bytes()

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
        self.app.include_router(routes.router)

    def state(self, rows, description='warm'):
        return {'description': description, 'global_seed': '', 'samples': rows}

    def save(self, state, name='voice'):
        work = self.builder / name
        work.mkdir(exist_ok=True)
        state_path = work / 'state.json'
        state_path.write_text(json.dumps(state), encoding='utf-8')
        return state_path

    def load(self, name='voice'):
        return routes._load_builder_state(name)

    def revision(self, row):
        return routes.get_dataset_row_revision(row)

    def single_request(self, text='Current', index=0, state=None, **extra):
        rows = (state or self.load())['samples']
        body = {'dataset_name': 'voice', 'sample_index': index, 'description': 'warm',
                'text': text, 'seed': 0, 'expected_count': len(rows),
                'expected_revision': self.revision(rows[index])}
        body.update(extra)
        return body

    def test_stale_single_snapshot_is_refused_before_claim_tts_or_files(self):
        state = self.save(self.state([{'text': 'Old', 'emotion': 'calm', 'status': 'done'}]))
        body = self.single_request('Stale')
        edited = copy.deepcopy(self.load())
        edited['samples'][0]['text'] = 'New'
        edited_bytes = self.save(edited).read_bytes()
        with patch.object(routes, 'claim_gpu_task') as claim, \
             patch.object(routes, 'release_gpu_task_claim') as release, \
             TestClient(self.app) as client:
            response = client.post('/api/dataset_builder/generate_sample', json=body)
        self.assertEqual(409, response.status_code, response.text)
        self.assertIn('regenerate', response.json()['detail'])
        claim.assert_not_called()
        release.assert_not_called()
        self.engine.generate_voice_design.assert_not_called()
        self.assertEqual(edited_bytes, state.read_bytes())
        self.assertFalse((self.builder / 'voice/sample_000.wav').exists())

    def test_write_between_single_preflight_and_lease_releases_owned_claim(self):
        self.save(self.state([{'text': 'Old', 'emotion': 'calm', 'status': 'done'}]))
        body = self.single_request('Fresh')

        def interleave(*args, **kwargs):
            edited = copy.deepcopy(self.load())
            edited['samples'][0]['text'] = 'Raced edit'
            self.save(edited)
            return 'owned-claim'

        with patch.object(routes, 'claim_gpu_task', side_effect=interleave) as claim, \
             patch.object(routes, 'release_gpu_task_claim') as release, \
             TestClient(self.app) as client:
            response = client.post('/api/dataset_builder/generate_sample', json=body)
        self.assertEqual(409, response.status_code, response.text)
        claim.assert_called_once()
        release.assert_called_once_with('dataset_builder', 'owned-claim')
        self.engine.generate_voice_design.assert_not_called()
        self.assertEqual('Raced edit', self.load()['samples'][0]['text'])
        self.assertFalse((self.builder / 'voice/sample_000.wav').exists())

    def test_partial_or_countless_binding_is_controlled_4xx(self):
        self.save(self.state([{'text': 'One'}, {'text': 'Two'}]))
        base = self.single_request()
        endless = dict(base)
        endless.pop('expected_count')
        other_count = dict(base, expected_count=3)
        with patch.object(routes, 'claim_gpu_task') as claim, TestClient(self.app) as client:
            for body in (endless, other_count):
                with self.subTest(body=body):
                    response = client.post('/api/dataset_builder/generate_sample', json=body)
                    self.assertIn(response.status_code, (400, 409), response.text)
            partial = {'name': 'voice', 'description': 'warm',
                       'samples': [{'text': 'One'}, {'text': 'Two'}], 'indices': [0, 1],
                       'expected_revisions': {'0': self.revision(self.load()['samples'][0])}}
            response = client.post('/api/dataset_builder/generate_batch', json=partial)
            self.assertEqual(400, response.status_code, response.text)
        claim.assert_not_called()

    def test_whole_batch_refuses_when_one_row_is_stale(self):
        state = self.save(self.state([{'text': 'One'}, {'text': 'Two'}]))
        original = state.read_bytes()
        revisions = {str(i): self.revision(row) for i, row in enumerate(self.load()['samples'])}
        stale = dict(revisions)
        stale['1'] = '0' * 64
        body = {'name': 'voice', 'description': 'warm',
                'samples': [{'text': 'One'}, {'text': 'Two'}], 'indices': [0, 1],
                'expected_count': 2, 'expected_revisions': stale}
        with patch.object(routes, 'reserve_background_task') as reserve, \
             patch.object(routes, 'start_claimed_task_thread') as start, \
             TestClient(self.app) as client:
            response = client.post('/api/dataset_builder/generate_batch', json=body)
        self.assertEqual(409, response.status_code, response.text)
        reserve.assert_not_called()
        start.assert_not_called()
        self.engine.generate_voice_design.assert_not_called()
        self.assertEqual(original, state.read_bytes())

    def test_write_between_batch_preflight_and_lease_releases_pending_claim(self):
        self.save(self.state([{'text': 'One'}, {'text': 'Two'}]))
        revisions = {str(i): self.revision(row) for i, row in enumerate(self.load()['samples'])}
        body = {'name': 'voice', 'description': 'warm',
                'samples': [{'text': 'One'}, {'text': 'Two'}], 'indices': [0, 1],
                'expected_count': 2, 'expected_revisions': revisions}

        def interleave(*args, **kwargs):
            edited = copy.deepcopy(self.load())
            edited['samples'][1]['text'] = 'Raced edit'
            self.save(edited)
            return 'pending-claim'

        with patch.object(routes, 'reserve_background_task', side_effect=interleave) as reserve, \
             patch.object(routes, 'release_gpu_task_claim') as release, \
             patch.object(routes, 'start_claimed_task_thread') as start, \
             TestClient(self.app) as client:
            response = client.post('/api/dataset_builder/generate_batch', json=body)
        self.assertEqual(409, response.status_code, response.text)
        reserve.assert_called_once()
        release.assert_called_once_with('dataset_builder', 'pending-claim', pending_only=True)
        start.assert_not_called()
        self.engine.generate_voice_design.assert_not_called()
        self.assertEqual('Raced edit', self.load()['samples'][1]['text'])

    def test_cross_process_project_lease_fences_row_edits_only_for_that_project(self):
        self.save(self.state([{'text': 'One'}]))
        self.save(self.state([{'text': 'Other'}]), name='other')
        project_key = routes._get_builder_project_key('voice')
        lease = acquire_task_lease(str(self.data), 'dataset_builder', (), resources=(project_key,))
        try:
            with TestClient(self.app) as client:
                blocked = client.post('/api/dataset_builder/edit_rows', json={
                    'name': 'voice', 'expected_count': 1,
                    'edits': [{'index': 0, 'expected_revision': self.revision(self.load()['samples'][0]),
                               'row': {'text': 'Blocked', 'emotion': '', 'seed': ''}}]})
                allowed = client.post('/api/dataset_builder/edit_rows', json={
                    'name': 'other', 'expected_count': 1,
                    'edits': [{'index': 0, 'expected_revision': self.revision(self.load('other')['samples'][0]),
                               'row': {'text': 'Allowed', 'emotion': '', 'seed': ''}}]})
        finally:
            lease.close()
        self.assertEqual(409, blocked.status_code, blocked.text)
        self.assertEqual(200, allowed.status_code, allowed.text)
        self.assertEqual('One', self.load()['samples'][0]['text'])
        self.assertEqual('Allowed', self.load('other')['samples'][0]['text'])

    def test_repeated_valid_generation_consumes_each_returned_revision(self):
        self.save(self.state([{'text': 'First', 'emotion': 'calm', 'status': 'pending'}]))
        body = self.single_request('First')
        with patch.object(routes, 'claim_gpu_task'), \
             patch.object(routes, 'release_gpu_task_claim'), \
             TestClient(self.app) as client:
            first = client.post('/api/dataset_builder/generate_sample', json=body)
            self.assertEqual(200, first.status_code, first.text)
            second_body = self.single_request('Second')
            second = client.post('/api/dataset_builder/generate_sample', json=second_body)
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(2, self.engine.generate_voice_design.call_count)
        self.assertEqual(['First', 'Second'],
                         [call.kwargs['sample_text'] for call in self.engine.generate_voice_design.call_args_list])
        self.assertEqual('Second', self.load()['samples'][0]['text'])
        self.assertEqual(self.revision(self.load()['samples'][0]), second.json()['row_revision'])

    def test_legacy_generation_uses_persisted_text_and_voice_definition(self):
        self.save(self.state([{'text': 'Saved text', 'emotion': 'calm',
                               'description': 'saved voice, calm', 'status': 'pending'}]))
        body = {'dataset_name': 'voice', 'sample_index': 0, 'description': 'stale voice',
                'text': 'Stale request text', 'seed': 0}
        with patch.object(routes, 'claim_gpu_task'), \
             patch.object(routes, 'release_gpu_task_claim'), \
             TestClient(self.app) as client:
            response = client.post('/api/dataset_builder/generate_sample', json=body)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('Saved text', self.engine.generate_voice_design.call_args.kwargs['sample_text'])
        self.assertEqual('saved voice, calm',
                         self.engine.generate_voice_design.call_args.kwargs['description'])
        self.assertEqual('Saved text', self.load()['samples'][0]['text'])

    def test_first_run_batch_creates_rows_without_revisions(self):
        body = {'name': 'new', 'description': 'warm',
                'samples': [{'text': 'Created', 'emotion': 'calm'}], 'global_seed': 3}
        captured = {}

        def start(*args, **kwargs):
            captured['claim_id'] = args[1]
            captured['task'] = args[2]
            captured['resolved'] = args[3]
            return SimpleNamespace()

        with patch.object(routes, 'reserve_background_task', return_value='owned'), \
             patch.object(routes, 'apply_builder_task_owner'), \
             patch.object(routes, 'start_claimed_task_thread', side_effect=start), \
             patch.object(routes, 'release_gpu_task_claim'), \
             TestClient(self.app) as client:
            response = client.post('/api/dataset_builder/generate_batch', json=body)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('owned', captured['claim_id'])
        self.assertTrue(os.path.isdir(self.builder / 'new'))
        captured['task'](captured['resolved'])
        rows = self.load('new')['samples']
        self.assertEqual(1, len(rows))
        self.assertEqual('Created', rows[0]['text'])
        self.assertEqual('warm, calm', rows[0]['description'])
        self.assertEqual('done', rows[0]['status'])
        self.assertEqual('Created', self.engine.generate_voice_design.call_args.kwargs['sample_text'])


if __name__ == '__main__':
    unittest.main()
