import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from config_settings import AppConfig, GenerationConfig, LLMConfig, TTSConfig
from routers import system


class ConfigSecretEndpointBindingTests(unittest.TestCase):
    def profile(self, url, key):
        return LLMConfig(base_url=url, api_key=key, model_name='fixture')

    def test_changed_endpoint_cannot_restore_or_resolve_saved_key(self):
        existing = {'llm': {'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'fixture-secret'},
                    'llm_local': {'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'fixture-secret'}}
        config = AppConfig(llm=self.profile('http://127.0.0.1:1235/v1', '[REDACTED]'), tts=TTSConfig())
        original = copy.deepcopy(existing)
        with self.assertRaises(HTTPException) as error:
            system._restore_redacted_secrets(config, existing)
        self.assertEqual(400, error.exception.status_code)
        with self.assertRaises(HTTPException) as error:
            system._resolve_redacted_api_key('[REDACTED]', 'http://127.0.0.1:1235/v1', existing)
        self.assertEqual(400, error.exception.status_code)
        self.assertEqual(original, existing)
        self.assertEqual('[REDACTED]', config.llm.api_key)

    def test_save_rejects_url_change_without_overwriting_config_and_allows_explicit_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            old = self.profile('http://127.0.0.1:1234/v1', 'fixture-secret')
            config = AppConfig(llm=old, llm_local=old, tts=TTSConfig())
            path.write_text(json.dumps(config.model_dump()))
            original = path.read_bytes()
            application = FastAPI(); application.include_router(system.router)
            incoming = config.model_dump()
            incoming['llm_local'].update(base_url='http://127.0.0.1:1235/v1', api_key='[REDACTED]')
            with patch.object(system, 'CONFIG_PATH', str(path)), patch.object(system, 'project_manager', Mock()), TestClient(application) as client:
                result = client.post('/api/config', json=incoming)
                self.assertEqual(400, result.status_code)
                self.assertEqual(original, path.read_bytes())
                incoming['llm_local']['api_key'] = 'explicit-new-key'
                result = client.post('/api/config', json=incoming)
                self.assertEqual(200, result.status_code)
            saved = json.loads(path.read_text())
            self.assertEqual('explicit-new-key', saved['llm_local']['api_key'])
            self.assertEqual(saved['llm_local'], saved['llm'])

    def test_mode_switch_preserves_each_profile_key_and_mirrors_new_active_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            local = self.profile('http://127.0.0.1:1234/v1', 'local-fixture')
            remote = self.profile('http://127.0.0.1:1236/v1', 'remote-fixture')
            config = AppConfig(llm=local, llm_local=local, llm_remote=remote, tts=TTSConfig())
            path.write_text(json.dumps(config.model_dump()))
            incoming = system._redact_config_secrets(config.model_dump())
            incoming['llm_mode'] = 'remote'
            application = FastAPI(); application.include_router(system.router)
            with patch.object(system, 'CONFIG_PATH', str(path)), patch.object(system, 'project_manager', Mock()), TestClient(application) as client:
                response = client.post('/api/config', json=incoming)
            self.assertEqual(200, response.status_code, response.text)
            saved = json.loads(path.read_text())
            self.assertEqual('remote-fixture', saved['llm']['api_key'])
            self.assertEqual('remote-fixture', saved['llm_remote']['api_key'])
            self.assertEqual('local-fixture', saved['llm_local']['api_key'])

    def test_test_and_models_routes_reject_unmatched_key_before_client_or_claim(self):
        existing = {'llm_local': {'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'fixture-secret'}}
        application = FastAPI(); application.include_router(system.router)
        request = self.profile('http://127.0.0.1:1235/v1', '[REDACTED]').model_dump()
        with patch.object(system, 'load_app_config', return_value=existing), \
             patch.object(system, 'claim_gpu_task') as claim, \
             patch('llm_provider.make_llm_client') as client_factory, TestClient(application) as client:
            result = client.post('/api/llm/test', json=request)
            self.assertEqual(400, result.status_code)
            models = client.post('/api/llm/models', json=request)
            self.assertEqual(200, models.status_code)
            self.assertEqual([], models.json()['models'])
            self.assertIn('changed endpoint', models.json()['error'])
            self.assertNotIn('fixture-secret', result.text + models.text)
            claim.assert_not_called()
            client_factory.assert_not_called()

    def test_get_then_save_initial_and_legacy_config_preserves_credentials(self):
        for saved in ({}, {'llm': self.profile('http://127.0.0.1:1234/v1', 'legacy-secret').model_dump()},
                      {'llm': self.profile('http://127.0.0.1:1234/v1', 'legacy-secret').model_dump(), 'llm_local': None}):
            with self.subTest(saved=bool(saved)), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, 'config.json')
                if saved:
                    path.write_text(json.dumps(saved))
                before = path.read_bytes() if path.exists() else None
                application = FastAPI()
                application.include_router(system.router)
                with patch.object(system, 'CONFIG_PATH', str(path)), patch.object(system, 'project_manager', Mock()), TestClient(application) as client:
                    response = client.get('/api/config')
                    self.assertEqual(200, response.status_code)
                    self.assertEqual(before, path.read_bytes() if path.exists() else None)
                    incoming = response.json()
                    self.assertNotIn('legacy-secret', response.text)
                    incoming['tts']['pause_same_speaker_ms'] = 321
                    result = client.post('/api/config', json=incoming)
                    self.assertEqual(200, result.status_code, result.text)
                persisted = json.loads(path.read_text())
                self.assertEqual(321, persisted['tts']['pause_same_speaker_ms'])
                self.assertEqual('legacy-secret' if saved else 'local', persisted['llm']['api_key'])
                self.assertEqual(persisted['llm']['api_key'], persisted['llm_local']['api_key'])

    def test_repeated_get_save_keeps_unused_blank_profiles_and_never_contacts_a_provider(self):
        local = self.profile('http://127.0.0.1:1234/v1', 'fixture-local').model_dump()
        remote = self.profile('http://127.0.0.1:1236/v1', 'fixture-remote').model_dump()
        for initial in ({}, {'llm': local},
                        {'llm': local, 'llm_local': local, 'llm_mode': 'local',
                         'llm_remote': {'base_url': '', 'api_key': 'local', 'model_name': ''}},
                        {'llm': remote, 'llm_remote': remote, 'llm_mode': 'remote',
                         'llm_local': {'base_url': '', 'api_key': 'inactive-fixture-key', 'model_name': ''}}):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'config.json'; path.write_text(json.dumps(initial))
                application = FastAPI(); application.include_router(system.router)
                with patch.object(system, 'CONFIG_PATH', str(path)), \
                        patch.object(system, 'project_manager', Mock()), \
                        patch('llm_provider.make_llm_client') as provider, TestClient(application) as client:
                    for cycle in range(3):
                        response = client.get('/api/config'); self.assertEqual(200, response.status_code)
                        incoming = response.json()
                        # Setup's first-save fallback for the absent unused profile.
                        if incoming.get('llm_mode', 'local') == 'local' and incoming.get('llm_remote') is None:
                            incoming['llm_remote'] = {'base_url': '', 'api_key': 'local', 'model_name': ''}
                        incoming['tts']['pause_same_speaker_ms'] = 321 + cycle
                        response = client.post('/api/config', json=incoming)
                        self.assertEqual(200, response.status_code, response.text)
                        saved = json.loads(path.read_text())
                        self.assertEqual(321+cycle, saved['tts']['pause_same_speaker_ms'])
                        self.assertNotIn('[REDACTED]', path.read_text())
                    provider.assert_not_called()
                inactive = 'llm_remote' if saved['llm_mode']=='local' else 'llm_local'
                self.assertEqual('', saved[inactive]['base_url'])
                self.assertEqual('inactive-fixture-key' if inactive=='llm_local' else 'local', saved[inactive]['api_key'])

    def test_blank_profile_cannot_restore_into_a_new_endpoint_or_a_missing_saved_profile(self):
        local = self.profile('http://127.0.0.1:1234/v1', 'fixture-local')
        for old_remote in (None, {'base_url': '', 'api_key': 'inactive-fixture-key', 'model_name': ''}):
            for incoming_url in ('', 'http://127.0.0.1:1236/v1'):
                with self.subTest(old_remote=old_remote, incoming_url=incoming_url), tempfile.TemporaryDirectory() as tmp:
                    if old_remote is not None and not incoming_url:
                        continue  # This is the permitted unchanged inactive profile.
                    path = Path(tmp)/'config.json'
                    existing = AppConfig(llm=local, llm_local=local, tts=TTSConfig()).model_dump()
                    existing['llm_remote'] = old_remote; path.write_text(json.dumps(existing)); before = path.read_bytes()
                    incoming = copy.deepcopy(existing)
                    incoming['llm_remote'] = {'base_url': incoming_url, 'api_key': '[REDACTED]', 'model_name': 'fixture'}
                    application = FastAPI(); application.include_router(system.router); manager = Mock()
                    with patch.object(system, 'CONFIG_PATH', str(path)), \
                            patch.object(system, 'project_manager', manager), TestClient(application) as client:
                        response = client.post('/api/config', json=incoming)
                    self.assertEqual(400, response.status_code, response.text)
                    self.assertEqual(before, path.read_bytes()); manager.reset_engine.assert_not_called()
                    self.assertNotIn('inactive-fixture-key', response.text)

    def test_blank_active_or_mirror_profile_still_cannot_restore_a_redacted_key(self):
        for section in ('llm', 'llm_local'):
            with self.subTest(section=section):
                local = self.profile('', 'fixture-local')
                existing = AppConfig(llm=local, llm_local=local, tts=TTSConfig()).model_dump()
                incoming = copy.deepcopy(existing); incoming[section]['api_key'] = '[REDACTED]'
                with self.assertRaises(HTTPException) as error:
                    system._restore_redacted_secrets(AppConfig(**incoming), existing)
                self.assertEqual(400, error.exception.status_code)

    def test_live_inactive_profile_regression_restores_initial_and_legacy_config(self):
        from tests import test_api
        profile = self.profile('http://127.0.0.1:1234/v1', 'fixture-local').model_dump()
        initials = [None, {}, {'llm': profile}, {'llm': profile, 'generation': None},
                    {'llm': profile, 'generation': {}},
                    {'llm': {**profile, 'base_url': 'http://127.0.0.1:1234', 'api_key': 'env:UNREAD_FIXTURE'}}]
        for initial in initials:
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'config.json'
                if initial is not None:
                    path.write_text(json.dumps(initial))
                application = FastAPI(); application.include_router(system.router)
                with patch.object(system, 'CONFIG_PATH', str(path)), \
                        patch.object(system, 'DATA_DIR', tmp), \
                        patch.object(system, 'project_manager', Mock()), \
                        patch('llm_provider.make_llm_client') as provider, TestClient(application) as client, \
                        patch.object(test_api, 'get', side_effect=client.get), \
                        patch.object(test_api, 'post', side_effect=client.post):
                    test_api.test_repeated_config_save_with_inactive_profile()
                    response = client.get('/api/config'); self.assertEqual(200, response.status_code)
                    saved = json.loads(path.read_text())
                    self.assertIsNone(saved['llm_remote'])
                    self.assertEqual(TTSConfig().pause_same_speaker_ms, saved['tts']['pause_same_speaker_ms'])
                    self.assertEqual(GenerationConfig().model_dump(), saved['generation'])
                    self.assertEqual('local' if not initial else initial['llm']['api_key'], saved['llm']['api_key'])
                    self.assertEqual(saved['llm']['api_key'], saved['llm_local']['api_key'])
                    provider.assert_not_called()

    def test_live_inactive_profile_regression_restores_custom_config_after_failure(self):
        from tests import test_api
        local = self.profile('http://127.0.0.1:1234/v1', 'fixture-local')
        remote = self.profile('http://127.0.0.1:1236/v1', 'fixture-remote')
        for mode in ('local', 'remote'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'config.json'
                initial = AppConfig(llm=local if mode=='local' else remote, llm_local=local, llm_remote=remote,
                                    llm_mode=mode, llm_remote_ssh='fixture-host', llm_failover=True,
                                    generation=GenerationConfig(temperature=.37), tts=TTSConfig()).model_dump()
                path.write_text(json.dumps(initial))
                application = FastAPI(); application.include_router(system.router)
                with patch.object(system, 'CONFIG_PATH', str(path)), \
                        patch.object(system, 'DATA_DIR', tmp), \
                        patch.object(system, 'project_manager', Mock()), \
                        patch('llm_provider.make_llm_client') as provider, TestClient(application) as client:
                    before = AppConfig(**client.get('/api/config').json()).model_dump()
                    calls = []
                    def get(target, **kwargs):
                        calls.append(target)
                        return client.get('/injected-missing') if len(calls)==2 else client.get(target, **kwargs)
                    with patch.object(test_api, 'get', side_effect=get), patch.object(test_api, 'post', side_effect=client.post):
                        with self.assertRaises(test_api.TestFailure):
                            test_api.test_repeated_config_save_with_inactive_profile()
                    self.assertEqual(before, AppConfig(**client.get('/api/config').json()).model_dump())
                    saved = json.loads(path.read_text())
                    self.assertEqual('fixture-local', saved['llm_local']['api_key'])
                    self.assertEqual('fixture-remote', saved['llm_remote']['api_key'])
                    self.assertEqual(.37, saved['generation']['temperature'])
                    self.assertEqual('fixture-host', saved['llm_remote_ssh']); self.assertTrue(saved['llm_failover'])
                    provider.assert_not_called()
