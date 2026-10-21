"""Conditional speech dependencies follow configuration publication and reset."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import copy
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import core
from project import get_chunk_generation_inputs
from routers import system
from task_ownership import acquire_task_lease, TaskOwnershipBusy
from tests import test_occurrence_pronunciation_panel as panel_fixtures
from tests.test_occurrence_pronunciation_generation import rule
from tests.test_support import use_private_cpu_fixture_lock


class OccurrenceSettingsTests(unittest.TestCase):
    def setUp(self):
        panel_fixtures.OccurrencePanelTests.setUp(self)
        self.client.app.include_router(system.router)
        for obj, name, value in ((system, 'CONFIG_PATH', self.pm.config_path), (system, 'project_manager', self.pm)):
            active = patch.object(obj, name, value); active.start(); self.addCleanup(active.stop)
        rows = self.pm.get_saved_chunks()
        for row in rows: row['text'] = 'Ann Ann'
        self.pm.save_chunks(rows)
        (self.root/'annotated_script.json').write_text(json.dumps([{'text': 'Ann Ann', 'speaker': 'A'}]))

    def seed(self, restrictions=None, replacement='Ah-n', state='approved'):
        rows = self.pm.get_saved_chunks()
        rows[0]['pronunciation_rules'] = [{**rule(restrictions=restrictions, replacement=replacement), 'state': state}]
        self.pm.save_chunks(rows)
        return copy.deepcopy(rows)

    def save_config(self, **settings):
        profile = {'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'fixture', 'model_name': 'fixture'}
        return self.client.post('/api/config', json={'llm': profile, 'llm_local': profile, 'llm_mode': 'local',
                                                    'tts': {'mode': 'local', 'language': 'English', **settings}})

    def test_mode_and_language_only_detach_affected_effective_words_and_stale_direct_captures(self):
        for restriction, proposal in (({'language': 'English'}, {'language': 'Japanese'}),
                                      ({'engine_mode': 'local'}, {'mode': 'external'})):
            with self.subTest(restriction=restriction):
                # Restore a known current configuration and take between independent cases.
                Path(self.pm.config_path).write_text(json.dumps({'tts': {'mode': 'local', 'language': 'English'}}))
                self.pm.reset_engine()
                rows = self.pm.get_saved_chunks()
                rows[0].update(status='done', audio_path='voicelines/prior-a.wav', audio_revision='old-a')
                rows[0].pop('voice_revision', None); self.pm.save_chunks(rows)
                before = self.seed(restriction)
                old_audio = (self.root/before[0]['audio_path']).read_bytes()
                script = (self.root/'annotated_script.json').read_bytes()
                response = self.save_config(**proposal)
                self.assertEqual(200, response.status_code, response.text)
                after = self.pm.get_saved_chunks()
                self.assertEqual('pending', after[0]['status']); self.assertIsNone(after[0]['audio_path'])
                self.assertEqual(before[1], after[1]); self.assertNotEqual(get_chunk_generation_inputs(before[0]), get_chunk_generation_inputs(after[0]))
                self.assertEqual(old_audio, (self.root/before[0]['audio_path']).read_bytes())
                self.assertEqual(script, (self.root/'annotated_script.json').read_bytes())
                private = self.root/'captured.wav'; sf.write(private, np.full(2400, .2), 24000)
                with self.assertRaisesRegex(ValueError, 'generation inputs changed'):
                    self.pm._publish_generated_chunk_audio(before[0], str(private), 'A')
                self.assertIsNone(self.pm.get_saved_chunks()[0]['audio_path'])
                self.assertFalse(list((self.root/'voicelines').glob('.render-*')))

    def test_equal_effective_words_bypassed_rules_drafts_and_unrelated_settings_keep_takes(self):
        for restrictions, replacement, state in (({'language': 'English'}, 'Shared', 'approved'),
                                                  ({'language': 'German'}, 'Ah-n', 'approved'),
                                                  ({'language': 'English'}, 'Ah-n', 'draft'),
                                                  ({'speaker': 'A'}, 'Ah-n', 'approved')):
            Path(self.pm.config_path).write_text(json.dumps({'tts': {'mode': 'local', 'language': 'English'}}))
            self.pm.reset_engine(); before = self.seed(restrictions, replacement, state)
            result = self.save_config(language='Japanese', max_new_tokens=1234)
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual(before, self.pm.get_saved_chunks())
        before = self.seed({'language': 'Japanese'})
        result = self.save_config(language='Japanese', max_new_tokens=2048)
        self.assertEqual(200, result.status_code, result.text); self.assertEqual(before, self.pm.get_saved_chunks())

    @use_private_cpu_fixture_lock()
    def test_pending_owner_blocks_conditional_setting_change_but_unrelated_save_keeps_existing_policy(self):
        self.seed({'language': 'English'}); before = Path(self.pm.config_path).read_bytes()
        owner = core.reserve_background_task('audio', cpu_only=True)
        try:
            blocked = self.save_config(language='Japanese')
            self.assertEqual(409, blocked.status_code, blocked.text)
            self.assertEqual(before, Path(self.pm.config_path).read_bytes())
            allowed = self.save_config(max_new_tokens=1234)
            self.assertEqual(200, allowed.status_code, allowed.text)
            self.assertTrue(core.process_state['audio']['running'])
            # No conditional rule dependency: retain the preexisting save policy.
            rows = self.pm.get_saved_chunks(); rows[0].pop('pronunciation_rules'); self.pm.save_chunks(rows)
            allowed = self.save_config(language='Japanese')
            self.assertEqual(200, allowed.status_code, allowed.text)
        finally: core.release_gpu_task_claim('audio', owner)

    def test_config_write_failure_keeps_audio_and_conservative_input_fence(self):
        before = self.seed({'language': 'English'}); config = Path(self.pm.config_path).read_bytes()
        audio = (self.root/before[0]['audio_path']).read_bytes()
        with patch.object(system, 'atomic_json_write', side_effect=OSError('injected config failure')):
            result = self.save_config(language='Japanese')
        self.assertEqual(409, result.status_code, result.text)
        self.assertEqual(config, Path(self.pm.config_path).read_bytes())
        self.assertEqual(audio, (self.root/before[0]['audio_path']).read_bytes())
        after = self.pm.get_saved_chunks()
        self.assertEqual(before[1], after[1]); self.assertEqual('pending', after[0]['status'])
        self.assertNotEqual(get_chunk_generation_inputs(before[0]), get_chunk_generation_inputs(after[0]))
        lease = acquire_task_lease(str(self.root), 'audio', (), cpu_only=True); lease.close()

    def test_conditional_save_holds_admission_through_engine_reset(self):
        self.seed({'language': 'English'}); entered = threading.Event(); release = threading.Event()
        original = self.pm.reset_engine
        def reset():
            entered.set()
            if not release.wait(5): raise RuntimeError('test reset was not released')
            original()
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.pm, 'reset_engine', side_effect=reset):
            future = pool.submit(self.save_config, language='Japanese')
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(TaskOwnershipBusy): acquire_task_lease(str(self.root), 'audio', (), cpu_only=True)
            finally: release.set()
            self.assertEqual(200, future.result(5).status_code)

    def test_new_approval_cannot_observe_old_engine_after_unconditional_config_write(self):
        # Default/legacy installs keep config in app/, separate from book state.
        config_path = self.root/'app'/'config.json'
        config_path.parent.mkdir()
        config_path.write_bytes(Path(self.pm.config_path).read_bytes())
        self.pm.config_path = str(config_path)
        active = patch.object(system, 'CONFIG_PATH', str(config_path))
        active.start(); self.addCleanup(active.stop)
        self.seed({'speaker': 'A'}, state='draft')
        entered = threading.Event(); release = threading.Event()
        original = self.pm.reset_engine
        class Engine:
            def get_model_settings(inner): return {'mode': 'local', 'language': 'English'}
        self.pm.engine = Engine()
        def reset():
            entered.set()
            if not release.wait(5): raise RuntimeError('test reset was not released')
            original()
        request = {**self.request}
        snapshot = self.client.post('/api/pronunciation/occurrences', json=request).json()
        proposed = [{**rule(restrictions={'language': 'English'}), 'note': 'new approval'}]
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.pm, 'reset_engine', side_effect=reset):
            config_future = pool.submit(self.save_config, language='Japanese')
            self.assertTrue(entered.wait(3))
            approval = pool.submit(self.client.post, '/api/pronunciation/occurrences/save',
                                   json={**request, 'revision': snapshot['revision'], 'rules': proposed})
            try:
                with self.assertRaises(FutureTimeout): approval.result(.1)
            finally: release.set()
            self.assertEqual(200, config_future.result(5).status_code)
            response = approval.result(5); self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('Japanese', response.json()['context']['language'])
        self.assertFalse(response.json()['audio_detached'])
        self.assertEqual('scope_mismatch:language', response.json()['preview']['decisions'][0]['reason'])

    def test_dependency_comparison_uses_actual_active_engine_settings(self):
        before = self.seed({'language': 'English'})
        class Engine:
            def get_model_settings(inner): return {'mode': 'local', 'language': 'Japanese'}
        self.pm.engine = Engine()
        response = self.save_config(language='Japanese')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(before, self.pm.get_saved_chunks())
        self.assertIsNone(self.pm.engine)

    def test_same_saved_settings_reset_invalidates_words_from_different_active_engine(self):
        before = self.seed({'language': 'English'})
        class Engine:
            def get_model_settings(inner): return {'mode': 'local', 'language': 'Japanese'}
        self.pm.engine = Engine()
        response = self.save_config(language='English')
        self.assertEqual(200, response.status_code, response.text)
        after = self.pm.get_saved_chunks()
        self.assertEqual('pending', after[0]['status'])
        self.assertIsNone(after[0]['audio_path'])
        self.assertNotEqual(get_chunk_generation_inputs(before[0]), get_chunk_generation_inputs(after[0]))
        self.assertEqual(before[1], after[1])
        self.assertTrue((self.root/before[0]['audio_path']).is_file())
        self.assertIsNone(self.pm.engine)


    def test_legacy_unscoped_rows_are_not_migrated_by_a_settings_save(self):
        rows = [{'id': 0, 'text': 'Ann Ann', 'speaker': 'A', 'status': 'done',
                 'audio_path': 'voicelines/prior-a.wav'}]
        path = Path(self.pm.chunks_path); path.write_text(json.dumps(rows))
        before = path.read_bytes()
        result = self.save_config(language='Japanese')
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(before, path.read_bytes())

    def test_unchanged_speech_context_keeps_unrelated_save_policy_with_damaged_chunks(self):
        Path(self.pm.chunks_path).write_text('{damaged')
        before = Path(self.pm.chunks_path).read_bytes()
        with patch.object(self.pm, 'get_pronunciation_preview_settings',
                          side_effect=AssertionError('Unrelated save must not wait for engine inspection')):
            response = self.save_config(max_new_tokens=1234)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(before, Path(self.pm.chunks_path).read_bytes())
