"""Effective window policy edits share AA2's task fence through engine reset."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import core
from routers import system
from synthesis_settings import get_synthesis_window_settings
from task_ownership import acquire_task_lease, TaskOwnershipBusy
from tests import test_occurrence_pronunciation_panel as panel_fixtures
from tests.test_occurrence_pronunciation_generation import rule
from tests.test_support import use_private_cpu_fixture_lock


class SynthesisWindowSettingsAdmissionTests(unittest.TestCase):
    def setUp(self):
        panel_fixtures.OccurrencePanelTests.setUp(self)
        self.client.app.include_router(system.router)
        for name, value in (('CONFIG_PATH', self.pm.config_path), ('project_manager', self.pm)):
            active = patch.object(system, name, value); active.start(); self.addCleanup(active.stop)

    def policy(self, chars=20, enabled=True):
        return get_synthesis_window_settings({'enabled': enabled, 'budgets': {'local.custom': {'max_chars': chars}}})

    def save(self, policy, **settings):
        profile = dict(base_url='http://127.0.0.1:1234/v1', api_key='fixture', model_name='fixture')
        return self.client.post('/api/config', json={'llm': profile, 'llm_local': profile, 'llm_mode': 'local',
            'tts': {'mode': 'local', 'language': 'English', 'synthesis_windows': policy, **settings}})

    def store(self, policy):
        Path(self.pm.config_path).write_text(json.dumps({'tts': {'mode': 'local', 'language': 'English', 'synthesis_windows': policy}}))
        self.pm.invalidate_config_cache()

    def engine(self, policy):
        value = SimpleNamespace(_synthesis_windows=policy, close_external=Mock())
        value.get_model_settings = lambda: {'mode': 'local', 'language': 'English', 'synthesis_windows': policy}
        return value

    @use_private_cpu_fixture_lock()
    def test_pending_exact_owner_blocks_enable_change_and_disable_without_writes(self):
        for old, proposed in (({}, self.policy()), (self.policy(), self.policy(30)), (self.policy(), {})):
            with self.subTest(old=old, proposed=proposed):
                self.store(old); before = Path(self.pm.config_path).read_bytes(); rows = self.pm.get_saved_chunks()
                owner = core.reserve_background_task('audio', cpu_only=True)
                core.process_state['audio']['running'] = False  # pending claim itself remains authoritative
                try:
                    with patch.object(self.pm, 'reset_engine') as reset:
                        result = self.save(proposed)
                    self.assertEqual(409, result.status_code, result.text); reset.assert_not_called()
                    self.assertEqual(before, Path(self.pm.config_path).read_bytes()); self.assertEqual(rows, self.pm.get_saved_chunks())
                finally: core.release_gpu_task_claim('audio', owner)

    def test_foreign_cpu_owner_blocks_policy_change_with_idle_local_state(self):
        before = Path(self.pm.config_path).read_bytes()
        owner = acquire_task_lease(str(self.root), 'audio', (), cpu_only=True)
        try:
            with patch.object(self.pm, 'reset_engine') as reset:
                result = self.save(self.policy())
            self.assertEqual(409, result.status_code, result.text); reset.assert_not_called()
            self.assertEqual(before, Path(self.pm.config_path).read_bytes())
        finally: owner.close()

    def test_task_admission_stays_held_until_engine_reset_finishes(self):
        entered = threading.Event(); release = threading.Event(); original = self.pm.reset_engine
        def reset():
            entered.set(); self.assertTrue(release.wait(5)); original()
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.pm, 'reset_engine', side_effect=reset):
            request = pool.submit(self.save, self.policy())
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaises(TaskOwnershipBusy): acquire_task_lease(str(self.root), 'audio', (), cpu_only=True)
            finally: release.set()
            self.assertEqual(200, request.result(5).status_code)
        owner = acquire_task_lease(str(self.root), 'audio', (), cpu_only=True); owner.close()

    @use_private_cpu_fixture_lock()
    def test_actual_engine_policy_difference_is_fenced_even_when_saved_proposal_is_equal(self):
        self.store(self.policy(30)); engine = self.engine(self.policy(20)); self.pm.engine = engine
        before = Path(self.pm.config_path).read_bytes(); owner = core.reserve_background_task('audio', cpu_only=True)
        try:
            result = self.save(self.policy(30))
            self.assertEqual(409, result.status_code, result.text)
            self.assertEqual(before, Path(self.pm.config_path).read_bytes()); self.assertIs(engine, self.pm.engine)
            engine.close_external.assert_not_called()
        finally: core.release_gpu_task_claim('audio', owner)
        result = self.save(self.policy(30)); self.assertEqual(200, result.status_code, result.text)
        engine.close_external.assert_called_once(); self.assertIsNone(self.pm.engine)

    @use_private_cpu_fixture_lock()
    def test_disabled_policy_edits_keep_legacy_saves_without_engine_inspection(self):
        self.store(self.policy(enabled=False)); before = self.pm.get_saved_chunks()
        owner = core.reserve_background_task('audio', cpu_only=True)
        try:
            with patch.object(self.pm, 'get_pronunciation_preview_settings', side_effect=AssertionError('unneeded engine peek')):
                result = self.save(self.policy(30, enabled=False), max_new_tokens=1234)
            self.assertEqual(200, result.status_code, result.text); self.assertEqual(before, self.pm.get_saved_chunks())
        finally: core.release_gpu_task_claim('audio', owner)

    @use_private_cpu_fixture_lock()
    def test_semantically_equal_enabled_policy_keeps_unrelated_save_behavior(self):
        compact = {'enabled': True, 'budgets': {'local.custom': {'max_chars': 20}}}
        self.store(compact); self.pm.engine = self.engine(self.policy())
        owner = core.reserve_background_task('audio', cpu_only=True)
        try:
            result = self.save(self.policy(), max_new_tokens=1234)
            self.assertEqual(200, result.status_code, result.text)
        finally: core.release_gpu_task_claim('audio', owner)

    def test_config_write_failure_keeps_cached_engine_and_releases_admission(self):
        self.store(self.policy()); engine = self.engine(self.policy()); self.pm.engine = engine
        config = Path(self.pm.config_path).read_bytes(); rows = self.pm.get_saved_chunks()
        with patch.object(system, 'atomic_json_write', side_effect=OSError('synthetic disk failure')):
            result = self.save(self.policy(30))
        self.assertEqual(409, result.status_code, result.text); self.assertEqual(config, Path(self.pm.config_path).read_bytes())
        self.assertEqual(rows, self.pm.get_saved_chunks()); self.assertIs(engine, self.pm.engine); engine.close_external.assert_not_called()
        owner = acquire_task_lease(str(self.root), 'audio', (), cpu_only=True); owner.close()

    def test_window_only_change_keeps_malformed_unrelated_chunks_bytes(self):
        path = Path(self.pm.chunks_path); path.write_bytes(b'legacy malformed chunks')
        result = self.save(self.policy())
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual(b'legacy malformed chunks', path.read_bytes())

    def test_combined_language_and_windows_change_preserves_conditional_word_invalidation(self):
        rows = self.pm.get_saved_chunks(); rows[0]['text'] = 'Ann Ann'
        rows[0]['pronunciation_rules'] = [rule(restrictions={'language': 'English'})]; self.pm.save_chunks(rows)
        before = copy.deepcopy(rows); audio = (self.root/rows[0]['audio_path']).read_bytes()
        result = self.save(self.policy(), language='Japanese')
        self.assertEqual(200, result.status_code, result.text); saved = self.pm.get_saved_chunks()
        self.assertEqual('pending', saved[0]['status']); self.assertIsNone(saved[0]['audio_path'])
        self.assertEqual(before[1], saved[1]); self.assertEqual(audio, (self.root/before[0]['audio_path']).read_bytes())

    def test_final_normalization_preserves_latest_unsent_field(self):
        original = system.ensure_book_switch_allowed
        @contextlib.contextmanager
        def barrier(*args, **kwargs):
            document = json.loads(Path(self.pm.config_path).read_text())
            document['tts']['max_new_tokens'] = 777
            Path(self.pm.config_path).write_text(json.dumps(document))
            self.pm.invalidate_config_cache()
            with original(*args, **kwargs):
                yield
        with patch.object(system, 'ensure_book_switch_allowed', barrier):
            response = self.save(self.policy())
        self.assertEqual(200, response.status_code, response.text)
        saved = json.loads(Path(self.pm.config_path).read_text())
        self.assertEqual(777, saved['tts']['max_new_tokens'])
        self.assertTrue(saved['tts']['synthesis_windows']['enabled'])

    def test_final_snapshot_handles_new_conditional_rule_and_actual_language(self):
        original = system.ensure_book_switch_allowed
        @contextlib.contextmanager
        def barrier(*args, **kwargs):
            rows = self.pm.get_saved_chunks()
            rows[0]['text'] = 'Ann Ann'
            rows[0]['pronunciation_rules'] = [rule(restrictions={'language': 'English'})]
            self.pm.save_chunks(rows)
            active = self.engine(self.policy())
            active.get_model_settings = lambda: {'mode': 'local', 'language': 'Japanese', 'synthesis_windows': self.policy()}
            self.pm.engine = active
            with original(*args, **kwargs):
                yield
        before = self.pm.get_saved_chunks()
        audio = (self.root/before[0]['audio_path']).read_bytes()
        with patch.object(system, 'ensure_book_switch_allowed', barrier):
            response = self.save(self.policy())
        self.assertEqual(200, response.status_code, response.text)
        saved = self.pm.get_saved_chunks()
        self.assertEqual('pending', saved[0]['status'])
        self.assertIsNone(saved[0]['audio_path'])
        self.assertEqual(before[1], saved[1])
        self.assertEqual(audio, (self.root/before[0]['audio_path']).read_bytes())
        self.assertIsNone(self.pm.engine)
