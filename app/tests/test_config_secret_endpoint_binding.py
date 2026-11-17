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


class ProviderHeaderEndpointTests(unittest.TestCase):
    """#2006 through the real route: no configured provider header VALUE may
    appear in GET /api/config, and Save must keep it without the browser ever
    holding the plaintext."""

    SECRETS = {'llm': 'Bearer llm-header-fixture', 'llm_local': 'local-header-fixture',
               'llm_remote': 'remote-header-fixture'}

    def config(self):
        local = LLMConfig(base_url='http://127.0.0.1:1234/v1', api_key='local-key', model_name='local',
                          provider_headers={'Authorization': self.SECRETS['llm_local']})
        remote = LLMConfig(base_url='http://127.0.0.1:1236/v1', api_key='remote-key', model_name='remote',
                           provider_headers={'x-api-key': self.SECRETS['llm_remote']})
        mirror = local.model_copy(update={'provider_headers': {'X-Acme-Token': self.SECRETS['llm']}})
        return AppConfig(llm=mirror, llm_local=local, llm_remote=remote, tts=TTSConfig())

    def test_get_config_response_carries_no_header_value_from_any_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            path.write_text(json.dumps(self.config().model_dump()))
            application = FastAPI(); application.include_router(system.router)
            with patch.object(system, 'CONFIG_PATH', str(path)), patch.object(system, 'DATA_DIR', tmp), \
                    patch.object(system, 'project_manager', Mock()), TestClient(application) as client:
                response = client.get('/api/config')
            self.assertEqual(200, response.status_code)
            for secret in self.SECRETS.values():
                self.assertNotIn(secret, response.text)
            body = response.json()
            self.assertEqual({'X-Acme-Token'}, set(body['llm']['provider_headers']))
            self.assertEqual({'Authorization'}, set(body['llm_local']['provider_headers']))
            self.assertEqual({'x-api-key'}, set(body['llm_remote']['provider_headers']))
            self.assertEqual({'Authorization': '[REDACTED]'}, body['llm_local']['provider_headers'])

    def test_models_route_sends_the_stored_header_not_the_sentinel(self):
        existing = {'llm_local': {'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'fixture-secret',
                                  'provider_headers': {'Authorization': 'Bearer stored'}}}
        application = FastAPI(); application.include_router(system.router)
        request = LLMConfig(base_url='http://127.0.0.1:1234/v1', api_key='[REDACTED]', model_name='fixture',
                            provider_headers={'Authorization': '[REDACTED]', 'X-Unknown': '[REDACTED]'}).model_dump()
        fake = Mock(); fake.models.list.return_value = Mock(data=[])
        seen = []
        with patch.object(system, 'load_app_config', return_value=existing), \
             patch('llm_provider.make_llm_client',
                   side_effect=lambda profile, **kwargs: seen.append(profile) or fake), \
             TestClient(application) as client:
            response = client.post('/api/llm/models', json=request)
        self.assertEqual(200, response.status_code)
        self.assertEqual({'Authorization': 'Bearer stored'}, seen[0]['provider_headers'])
        self.assertNotIn('[REDACTED]', response.text)

    def test_save_never_restores_a_header_from_a_different_endpoint(self):
        """A credential saved for endpoint A must not be written onto a profile
        now pointing at endpoint B (#2006 review finding 1). The api_key is a
        reference here, so the api_key guard does not fire and the header
        branch is the only thing standing between A's secret and B."""
        saved = {'llm_local': {'base_url': 'http://127.0.0.1:1234/v1',
                               'api_key': 'env:SAVED_REFERENCE',
                               'provider_headers': {'Authorization': 'Bearer saved-endpoint-only'}}}
        incoming = LLMConfig(base_url='http://127.0.0.1:1235/v1', api_key='env:SAVED_REFERENCE',
                             model_name='fixture',
                             provider_headers={'Authorization': '[REDACTED]', 'X-New': 'typed'})
        config = AppConfig(llm=incoming, llm_local=incoming, tts=TTSConfig())

        restored = system._restore_redacted_secrets(config, saved)

        self.assertEqual({'X-New': 'typed'}, restored.llm_local.provider_headers)
        self.assertNotIn('saved-endpoint-only', json.dumps(restored.model_dump()))
        self.assertNotIn('[REDACTED]', json.dumps(restored.model_dump()))
        # The same header at the same endpoint is still restored.
        same = incoming.model_copy(update={'base_url': 'http://127.0.0.1:1234/v1'})
        restored_same = system._restore_redacted_secrets(
            AppConfig(llm=same, llm_local=same, tts=TTSConfig()), saved)
        self.assertEqual({'Authorization': 'Bearer saved-endpoint-only', 'X-New': 'typed'},
                         restored_same.llm_local.provider_headers)

    def test_save_route_does_not_carry_a_header_to_a_changed_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            local = LLMConfig(base_url='http://127.0.0.1:1234/v1', api_key='env:SAVED_REFERENCE',
                              model_name='local',
                              provider_headers={'Authorization': 'Bearer saved-endpoint-only'})
            path.write_text(json.dumps(AppConfig(llm=local, llm_local=local,
                                                 tts=TTSConfig()).model_dump()))
            application = FastAPI(); application.include_router(system.router)
            with patch.object(system, 'CONFIG_PATH', str(path)), \
                    patch.object(system, 'DATA_DIR', tmp), \
                    patch.object(system, 'project_manager', Mock()), TestClient(application) as client:
                echoed = client.get('/api/config').json()
                self.assertEqual({'Authorization': '[REDACTED]'},
                                 echoed['llm_local']['provider_headers'])
                for section in ('llm', 'llm_local'):
                    echoed[section]['base_url'] = 'http://127.0.0.1:1235/v1'
                response = client.post('/api/config', json=echoed)
                self.assertEqual(200, response.status_code, response.text)
            self.assertNotIn('saved-endpoint-only', path.read_text())
            persisted = json.loads(path.read_text())
            self.assertEqual({}, persisted['llm_local']['provider_headers'])
            self.assertEqual({}, persisted['llm']['provider_headers'])

    def test_save_matches_a_renamed_case_header_and_keeps_the_stored_value(self):
        """HTTP header names are case-insensitive, so an editor that echoed
        `authorization` for a stored `Authorization` must not lose the value
        (#2006 review finding 2)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            local = LLMConfig(base_url='http://127.0.0.1:1234/v1', api_key='local-key',
                              model_name='local',
                              provider_headers={'Authorization': 'Bearer stored-case'})
            path.write_text(json.dumps(AppConfig(llm=local, llm_local=local,
                                                 tts=TTSConfig()).model_dump()))
            application = FastAPI(); application.include_router(system.router)
            with patch.object(system, 'CONFIG_PATH', str(path)), \
                    patch.object(system, 'DATA_DIR', tmp), \
                    patch.object(system, 'project_manager', Mock()), TestClient(application) as client:
                echoed = client.get('/api/config').json()
                for section in ('llm', 'llm_local'):
                    echoed[section]['provider_headers'] = {'authorization': '[REDACTED]'}
                response = client.post('/api/config', json=echoed)
                self.assertEqual(200, response.status_code, response.text)
            persisted = json.loads(path.read_text())
            self.assertEqual({'Authorization': 'Bearer stored-case'},
                             persisted['llm_local']['provider_headers'])
            self.assertEqual(persisted['llm_local']['provider_headers'],
                             persisted['llm']['provider_headers'])

    def test_save_echoing_sentinels_keeps_stored_values_and_applies_header_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            config = self.config()
            path.write_text(json.dumps(config.model_dump()))
            application = FastAPI(); application.include_router(system.router)
            with patch.object(system, 'CONFIG_PATH', str(path)), patch.object(system, 'DATA_DIR', tmp), \
                    patch.object(system, 'project_manager', Mock()), TestClient(application) as client:
                echoed = client.get('/api/config').json()
                self.assertEqual(200, client.post('/api/config', json=echoed).status_code)
                saved = json.loads(path.read_text())
                self.assertEqual(config.llm_local.provider_headers,
                                 saved['llm_local']['provider_headers'])
                self.assertEqual(config.llm_remote.provider_headers,
                                 saved['llm_remote']['provider_headers'])
                # `llm` is the mirror of the active (local) profile on save.
                self.assertEqual(saved['llm_local'], saved['llm'])
                self.assertNotIn('[REDACTED]', path.read_text())

                edited = client.get('/api/config').json()
                edited['llm_local']['provider_headers'] = {'Authorization': 'Bearer typed-new',
                                                           'X-Unknown': '[REDACTED]'}
                edited['llm_remote']['provider_headers'] = {}
                self.assertEqual(200, client.post('/api/config', json=edited).status_code)
            reloaded = json.loads(path.read_text())
            self.assertEqual({'Authorization': 'Bearer typed-new'},
                             reloaded['llm_local']['provider_headers'])
            self.assertEqual({}, reloaded['llm_remote']['provider_headers'])
            self.assertNotIn('[REDACTED]', path.read_text())


class ProviderHeaderResolutionBindingTests(unittest.TestCase):
    """The Test / Load-models resolvers: case-insensitive names, one endpoint
    normalisation function, and no guessing when two profiles share an endpoint
    (#2006 review findings 2 and 3)."""

    URL = 'http://127.0.0.1:1234/v1'

    def resolve(self, headers, url=None, existing=None):
        return system._resolve_redacted_provider_headers(headers, url or self.URL, existing)

    def test_headers_resolve_through_a_case_changed_name(self):
        existing = {'llm_local': {'base_url': self.URL, 'api_key': 'local-key',
                                  'provider_headers': {'Authorization': 'Bearer stored'}}}
        self.assertEqual({'Authorization': 'Bearer stored'},
                         self.resolve({'authorization': '[REDACTED]'}, existing=existing))
        # A typed value under the other spelling also lands on the stored name,
        # so one header cannot gain a second differently-cased copy. (No
        # sentinel in the editor means nothing to resolve and the editor is
        # forwarded untouched, so this goes through the restore helper.)
        self.assertEqual({'Authorization': 'Bearer typed'},
                         system._restore_provider_headers(
                             {'AUTHORIZATION': 'Bearer typed'}, existing['llm_local']['provider_headers']))

    def test_two_saved_names_differing_only_by_case_resolve_the_same_way_every_time(self):
        existing = {'llm_local': {'base_url': self.URL, 'api_key': 'local-key',
                                  'provider_headers': {'Authorization': 'first',
                                                       'authorization': 'second'}}}
        resolved = self.resolve({'AUTHORIZATION': '[REDACTED]'}, existing=existing)
        self.assertEqual({'Authorization': 'first'}, resolved)
        self.assertEqual(resolved, self.resolve({'AUTHORIZATION': '[REDACTED]'}, existing=existing))

    def test_a_shared_endpoint_between_profiles_refuses_to_guess_and_leaks_nothing(self):
        shared = 'http://127.0.0.1:1234/v1'
        existing = {'llm_mode': 'remote',
                    'llm_local': {'base_url': shared, 'api_key': 'LOCAL-KEY',
                                  'provider_headers': {'X-Tenant': 'LOCAL'}},
                    'llm_remote': {'base_url': shared, 'api_key': 'REMOTE-KEY',
                                   'provider_headers': {'X-Tenant': 'REMOTE'}}}
        existing['llm'] = dict(existing['llm_remote'])
        # The request cannot say which profile it is for, so neither credential
        # may be handed over: headers drop, the key asks for one explicitly.
        self.assertEqual({}, self.resolve({'X-Tenant': '[REDACTED]'}, existing=existing))
        with self.assertRaises(HTTPException) as error:
            system._resolve_redacted_api_key('[REDACTED]', shared, existing)
        self.assertEqual(400, error.exception.status_code)
        self.assertNotIn('LOCAL', error.exception.detail)
        self.assertNotIn('REMOTE', error.exception.detail)

    def test_a_saved_url_without_v1_matches_the_posted_normalised_url(self):
        existing = {'llm_local': {'base_url': 'http://127.0.0.1:1234', 'api_key': 'stored-key',
                                  'provider_headers': {'Authorization': 'Bearer stored'}}}
        url = 'http://127.0.0.1:1234/v1'
        self.assertEqual('stored-key', system._resolve_redacted_api_key('[REDACTED]', url, existing))
        self.assertEqual({'Authorization': 'Bearer stored'},
                         self.resolve({'Authorization': '[REDACTED]'}, url=url, existing=existing))
        # And the browser side is normalised the same way, not only the saved one.
        self.assertEqual({'Authorization': 'Bearer stored'},
                         self.resolve({'Authorization': '[REDACTED]'},
                                      url='http://127.0.0.1:1234/', existing=existing))

    def test_the_active_mirror_is_not_a_second_profile(self):
        existing = {'llm_mode': 'local',
                    'llm_local': {'base_url': self.URL, 'api_key': 'local-key',
                                  'provider_headers': {'Authorization': 'Bearer local'}},
                    'llm_remote': {'base_url': 'http://127.0.0.1:1236/v1', 'api_key': 'remote-key'}}
        existing['llm'] = dict(existing['llm_local'])
        self.assertEqual('local-key', system._resolve_redacted_api_key('[REDACTED]', self.URL, existing))
        self.assertEqual({'Authorization': 'Bearer local'},
                         self.resolve({'Authorization': '[REDACTED]'}, existing=existing))

    def test_models_route_does_not_use_another_profile_credentials_on_a_tie(self):
        shared = 'http://127.0.0.1:1234/v1'
        existing = {'llm_mode': 'remote',
                    'llm_local': {'base_url': shared, 'api_key': 'LOCAL-KEY',
                                  'provider_headers': {'X-Tenant': 'LOCAL'}},
                    'llm_remote': {'base_url': shared, 'api_key': 'REMOTE-KEY',
                                   'provider_headers': {'X-Tenant': 'REMOTE'}}}
        application = FastAPI(); application.include_router(system.router)
        request = LLMConfig(base_url=shared, api_key='[REDACTED]', model_name='fixture',
                            provider_headers={'X-Tenant': '[REDACTED]'}).model_dump()
        with patch.object(system, 'load_app_config', return_value=existing), \
             patch('llm_provider.make_llm_client') as client_factory, \
             TestClient(application) as client:
            response = client.post('/api/llm/models', json=request)
        self.assertEqual(200, response.status_code)
        self.assertEqual([], response.json()['models'])
        self.assertIn('changed endpoint', response.json()['error'])
        self.assertNotIn('LOCAL', response.text)
        self.assertNotIn('REMOTE', response.text)
        client_factory.assert_not_called()
