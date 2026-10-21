"""Recovery execution policy changes share the task fence through engine reset."""
from pathlib import Path
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import core
from routers import system
from tests import test_synthesis_window_settings_admission as windows
from tests.test_support import use_private_cpu_fixture_lock


class RecoverySettingsAdmissionTests(unittest.TestCase):
    setUp = windows.SynthesisWindowSettingsAdmissionTests.setUp

    def store(self, policy):
        Path(self.pm.config_path).write_text(json.dumps({'tts': {'synthesis_recovery': policy}}))
        self.pm.invalidate_config_cache()

    def save(self, policy):
        profile = dict(base_url='http://127.0.0.1:1234/v1', api_key='fixture', model_name='fixture')
        return self.client.post('/api/config', json={'llm': profile, 'llm_local': profile,
            'llm_mode': 'local', 'tts': {'synthesis_recovery': policy}})

    @use_private_cpu_fixture_lock()
    def test_live_task_blocks_enable_disable_and_enabled_budget_change(self):
        enabled = {'enabled': True, 'max_audio_mb': 256}
        for before, after in (({}, enabled), (enabled, {}), (enabled, {**enabled, 'max_audio_mb': 512})):
            with self.subTest(before=before, after=after):
                self.store(before)
                config = Path(self.pm.config_path).read_bytes()
                rows = self.pm.get_saved_chunks()
                claim = core.reserve_background_task('audio', cpu_only=True)
                try:
                    with patch.object(self.pm, 'reset_engine') as reset:
                        response = self.save(after)
                    self.assertEqual(409, response.status_code, response.text)
                    reset.assert_not_called()
                    self.assertEqual(config, Path(self.pm.config_path).read_bytes())
                    self.assertEqual(rows, self.pm.get_saved_chunks())
                finally:
                    core.release_gpu_task_claim('audio', claim)

    @use_private_cpu_fixture_lock()
    def test_actual_engine_drift_is_fenced_when_saved_and_proposed_policy_match(self):
        enabled = {'enabled': True, 'max_audio_mb': 256}
        self.store(enabled)
        engine = SimpleNamespace(_synthesis_recovery=None, close_external=Mock())
        self.pm.engine = engine
        claim = core.reserve_background_task('audio', cpu_only=True)
        try:
            response = self.save(enabled)
            self.assertEqual(409, response.status_code, response.text)
            self.assertIs(engine, self.pm.engine)
        finally:
            core.release_gpu_task_claim('audio', claim)
        rows = self.pm.get_saved_chunks()
        response = self.save(enabled)
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(self.pm.engine)
        engine.close_external.assert_called_once()
        self.assertEqual(rows, self.pm.get_saved_chunks())

    @use_private_cpu_fixture_lock()
    def test_disabled_budget_draft_edits_do_not_inspect_model_or_invalidate_audio(self):
        self.store({'enabled': False, 'max_audio_mb': 256})
        rows = self.pm.get_saved_chunks()
        claim = core.reserve_background_task('audio', cpu_only=True)
        try:
            with patch.object(self.pm, 'get_pronunciation_preview_settings', side_effect=AssertionError('model peek')):
                response = self.save({'enabled': False, 'max_audio_mb': 512})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(rows, self.pm.get_saved_chunks())
        finally:
            core.release_gpu_task_claim('audio', claim)

    def test_invalid_or_omitted_policy_preserves_saved_settings(self):
        enabled = {'enabled': True, 'max_audio_mb': 256}
        self.store(enabled)
        for bad in ({'enabled': 'true'}, {'max_audio_mb': 15}, {'max_audio_mb': 4097},
                    {'enabled': False, 'max_audio_mb': 256.5}, {'unknown': True}):
            with self.subTest(policy=bad):
                before = Path(self.pm.config_path).read_bytes()
                response = self.save(bad)
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual(before, Path(self.pm.config_path).read_bytes())
        profile = dict(base_url='http://127.0.0.1:1234/v1', api_key='fixture', model_name='fixture')
        response = self.client.post('/api/config', json={'llm': profile, 'llm_local': profile,
            'llm_mode': 'local', 'tts': {'language': 'English'}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(enabled, json.loads(Path(self.pm.config_path).read_text())['tts']['synthesis_recovery'])
