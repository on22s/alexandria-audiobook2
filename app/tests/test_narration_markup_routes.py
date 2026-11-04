"""Real HTTP boundaries using synthetic saved-book fixtures."""
import copy
import json
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from narration_markup import RECEIPT_KEY
from project import ProjectManager
from routers import editor
from take_retention import KEY, get_take_history_policy, set_take_history_policy
from book_state_transaction import ensure_book_state
from tests import test_narration_markup as fixtures
from tests.test_support import use_private_cpu_fixture_lock


class NarrationMarkupRouteTests(unittest.TestCase):
    def setUp(self):
        fixtures.NarrationMarkupEditTests.setUp(self)
        states = copy.deepcopy(core.process_state)
        for state in states.values():
            state['running'] = False
        for obj, name, value in ((editor, 'project_manager', self.pm),
                (core, 'DATA_DIR', str(self.root)), (core, 'process_state', states),
                (editor, 'process_state', states), (core, '_task_claims', {}),
                (core, '_gpu_leases', {})):
            active = patch.object(obj, name, value)
            active.start()
            self.addCleanup(active.stop)
        private_lock = use_private_cpu_fixture_lock()
        private_lock.__enter__()
        self.addCleanup(private_lock.__exit__, None, None, None)
        api = FastAPI()
        api.include_router(editor.router)
        self.client = TestClient(api)
        self.addCleanup(self.client.close)

    def artifacts(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()
                and not path.name.endswith('.lock') and '.task_ownership' not in path.parts}

    def post(self, action, **fields):
        return self.client.post('/api/chunks/0/narration-markup/' + action,
            json=dict(expected_book_token=fields.pop('expected_book_token', self.token),
                      expected_uid=fields.pop('expected_uid', 'line-a'), **fields))

    def preview(self):
        response = self.post('preview')
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def apply(self, report, choice='keep', **fields):
        return self.post('apply', binding=report['binding'], decision=choice, **fields)

    def test_preview_read_only_without_engine(self):
        before = self.artifacts()
        with patch.object(self.pm, 'get_engine', side_effect=AssertionError('No engine')):
            report = self.preview()
        self.assertTrue(report['conflict'])
        self.assertEqual('Hello. ', report['spoken_text'])
        self.assertEqual(before, self.artifacts())

    def test_stale_inputs(self):
        for name in ('source.txt', 'annotated_script.json', 'voice_config.json', 'config.json', 'chunks.json', 'state.json'):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                report = self.preview()
                if name == 'source.txt':
                    path.write_bytes(b'Changed synthetic source')
                else:
                    value = json.loads(original)
                    if isinstance(value, list):
                        value[0]['text'] += ' changed'
                    else:
                        value['book_generation' if name == 'state.json' else 'changed'] = 'two'
                    path.write_text(json.dumps(value), encoding='utf-8')
                before = self.artifacts()
                response = self.apply(report)
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before, self.artifacts())
                path.write_bytes(original)

    def test_conflict_resolution_receipt_reload_and_sources(self):
        for choice, duration in (('keep', 500), ('replace', 250)):
            with self.subTest(choice=choice):
                self.pm.save_chunks(copy.deepcopy(self.rows))
                report = self.preview()
                before = self.artifacts()
                self.assertEqual(409, self.apply(report, 'apply').status_code)
                self.assertEqual(before, self.artifacts())
                with patch.object(self.pm, 'get_engine', side_effect=AssertionError('No engine')):
                    response = self.apply(report, choice)
                self.assertEqual(200, response.status_code, response.text)
                saved = ProjectManager(str(self.root)).get_saved_chunks()[0]
                self.assertEqual(('line-a', 'Hello. ', duration, [0], 3, 'Read clearly'),
                    tuple(saved[key] for key in ('uid', 'text', 'pause_after',
                          'source_entry_indices', 'chapter', 'instruct')))
                self.assertEqual('pending', saved['status'])
                self.assertIsNone(saved['audio_path'])
                self.assertIsNone(saved['drift'])
                self.assertEqual(self.original, saved[RECEIPT_KEY]['original_text'])
                self.assertEqual(choice, saved[RECEIPT_KEY]['decision'])
                for name in ('source.txt', 'annotated_script.json', 'voicelines/previous.wav'):
                    self.assertEqual(before[name], (self.root / name).read_bytes())

    def test_wrong_book_uid_decision_and_duplicate(self):
        report = self.preview()
        before = self.artifacts()
        for fields in (dict(expected_uid='other'), dict(expected_book_token='0' * 64)):
            self.assertEqual(409, self.post('preview', **fields).status_code)
            self.assertEqual(409, self.apply(report, **fields).status_code)
        self.assertEqual(422, self.apply(report, 'guess').status_code)
        self.assertEqual(before, self.artifacts())
        self.assertEqual(200, self.apply(report).status_code)
        before = self.artifacts()
        self.assertEqual('already_applied', self.apply(report).json()['status'])
        self.assertEqual(409, self.apply(report, 'replace').status_code)
        self.assertEqual(before, self.artifacts())
        self.pm.update_chunk(0, {'pause_after': 999})
        before = self.artifacts()
        self.assertEqual(409, self.apply(report).status_code)
        self.assertEqual(before, self.artifacts())

    def test_save_failure_atomicity_and_real_retention(self):
        with ensure_book_state(self.root):
            set_take_history_policy(self.pm, True, get_take_history_policy(self.root)['revision'])
        report = self.preview()
        before = self.artifacts()
        with patch.object(self.pm, '_save_chunks_locked', side_effect=OSError('injected failure')):
            response = self.apply(report)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, self.artifacts())
        response = self.apply(report)
        self.assertEqual(200, response.status_code, response.text)
        retained = ProjectManager(str(self.root)).get_saved_chunks()[0][KEY]
        self.assertEqual('voicelines/previous.wav', retained['path'])
        self.assertEqual(self.original, retained['captured']['generation_inputs']['text'])
        self.assertEqual('previous', retained['captured']['audio_revision'])
        self.assertEqual(before[retained['path']], (self.root / retained['path']).read_bytes())

    def test_actual_unstarted_task_ownership_refuses_apply(self):
        report = self.preview()
        owner = core.reserve_background_task('audio', cpu_only=True)
        try:
            before = self.artifacts()
            self.assertTrue(self.preview()['conflict'])
            self.assertEqual(before, self.artifacts())
            response = self.apply(report)
            self.assertEqual(400, response.status_code, response.text)
            self.assertIn('already running', response.json()['detail'])
            self.assertEqual(before, self.artifacts())
        finally:
            core.release_gpu_task_claim('audio', owner)
        self.assertEqual(200, self.apply(report).status_code)

    def test_nonconflict_choices_and_escaped_literal_repreview(self):
        self.pm.update_chunk(0, {'text': r'Literal \[[pause:250ms]]'})
        report = self.preview()
        before = self.artifacts()
        for choice in ('keep', 'replace'):
            self.assertEqual(409, self.apply(report, choice).status_code)
        self.assertEqual(before, self.artifacts())
        self.assertEqual(200, self.apply(report, 'apply').status_code)
        self.assertEqual('Literal [[pause:250ms]]', self.pm.get_saved_chunks()[0]['text'])
        before = self.artifacts()
        repeated = self.preview()
        self.assertFalse(repeated['changed'])
        self.assertIsNone(repeated['pause_after'])
        self.assertEqual(409, self.apply(repeated, 'apply').status_code)
        self.assertEqual(before, self.artifacts())

    def test_actual_generation_dispatch_receives_applied_text(self):
        import numpy as np
        import soundfile as sf
        from tests.test_occurrence_pronunciation_generation import engine_fixture
        engine = engine_fixture()
        self.pm.engine = engine
        seen = []
        def prepared(text, instruct, speaker, voices, output):
            seen.append(text)
            sf.write(output, np.full(2400, .2), 24000)
            return True
        with patch.object(engine, '_generate_voice_prepared', side_effect=prepared):
            result = self.pm.generate_chunk_audio(0)
            self.assertTrue(result[0], result)
            report = self.preview()
            self.assertEqual(200, self.apply(report).status_code)
            result = self.pm.generate_chunk_audio(0)
            self.assertTrue(result[0], result)
        self.assertEqual([self.original + '.', 'Hello.'], seen)
