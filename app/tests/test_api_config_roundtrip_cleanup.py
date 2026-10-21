"""Exercise actual live-API roundtrip functions through private config routes."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from config_settings import AppConfig, GenerationConfig, LLMConfig, PromptConfig, TTSConfig
from routers import system
from tests import test_api

FUNCTIONS = ('test_save_config_roundtrip', 'test_save_pause_config_roundtrip',
             'test_save_review_prompts_roundtrip', 'test_save_persona_prompts_roundtrip')


class APIConfigRoundtripCleanupTests(unittest.TestCase):
    @contextmanager
    def fixture(self, mode='local', raw=None, absent=False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.json'
            local = LLMConfig(base_url='http://127.0.0.1:1234/v1', api_key='fixture-local', model_name='local-fixture', provider_headers={'X-Fixture': 'local'}, provider_extra_body={'fixture': 'local'})
            remote = LLMConfig(base_url='http://127.0.0.1:1236/v1', api_key='fixture-remote', model_name='remote-fixture', provider_headers={'X-Fixture': 'remote'}, provider_extra_body={'fixture': 'remote'})
            seed = AppConfig(llm=local if mode=='local' else remote, llm_local=local, llm_remote=remote,
                             llm_mode=mode, llm_remote_ssh='fixture-ssh', llm_failover=True,
                             tts=TTSConfig(synthesis_windows={'enabled': True, 'budgets': {'local.custom': {'max_chars': 60, 'max_words': None}}}),
                             prompts=PromptConfig(system_prompt='custom system', user_prompt='custom user',
                                 review_system_prompt='custom review system', review_user_prompt='custom review user',
                                 persona_system_prompt='custom persona system', persona_user_prompt='custom persona user',
                                 persona_advanced_prompt='custom persona advanced'),
                             generation=GenerationConfig(temperature=.37, chunk_size=1234)).model_dump()
            if not absent:
                path.write_text(json.dumps(seed if raw is None else raw))
            application = FastAPI(); application.include_router(system.router)
            with patch.object(system, 'CONFIG_PATH', str(path)), patch.object(system, 'DATA_DIR', tmp), \
                    patch.object(system, 'project_manager', Mock()), patch.object(system, 'claim_gpu_task') as claim, \
                    patch('llm_provider.make_llm_client') as provider, TestClient(application) as client:
                try:
                    yield client, path
                finally:
                    claim.assert_not_called(); provider.assert_not_called()

    def assert_full_restoration(self, client, path, original):
        self.assertEqual(test_api.get_config_after_save_snapshot(original), AppConfig(**client.get('/api/config').json()).model_dump())
        saved = json.loads(path.read_text())
        self.assertEqual('fixture-local', saved['llm_local']['api_key'])
        self.assertEqual('fixture-remote', saved['llm_remote']['api_key'])
        self.assertEqual('fixture-local' if original['llm_mode']=='local' else 'fixture-remote', saved['llm']['api_key'])

    def run_actual(self, name, client, get=None, post=None):
        with patch.object(test_api, 'get', side_effect=get or client.get), patch.object(test_api, 'post', side_effect=post or client.post):
            getattr(test_api, name)()

    def test_four_roundtrips_preserve_profiles_and_complete_custom_config(self):
        for mode in ('local', 'remote'):
            for name in FUNCTIONS:
                with self.subTest(mode=mode, function=name), self.fixture(mode) as (client, path):
                    original = client.get('/api/config').json()
                    self.run_actual(name, client)
                    self.assert_full_restoration(client, path, original)

    def test_mid_test_get_failure_still_restores_all_settings(self):
        for mode in ('local', 'remote'):
            for name in FUNCTIONS:
                with self.subTest(mode=mode, function=name), self.fixture(mode) as (client, path):
                    original = client.get('/api/config').json(); reads = []
                    def get(target, **kwargs):
                        reads.append(target)
                        return client.get('/injected-missing') if len(reads)==2 else client.get(target, **kwargs)
                    with self.assertRaisesRegex(test_api.TestFailure, 'Expected 200, got 404'):
                        self.run_actual(name, client, get=get)
                    self.assert_full_restoration(client, path, original)

    def test_failed_initial_save_still_runs_and_checks_cleanup(self):
        for name in FUNCTIONS:
            with self.subTest(function=name), self.fixture() as (client, path):
                original = client.get('/api/config').json(); writes = []
                def post(target, **kwargs):
                    writes.append(target)
                    return client.post('/injected-missing', **kwargs) if len(writes)==1 else client.post(target, **kwargs)
                with self.assertRaisesRegex(test_api.TestFailure, 'Expected 200, got 404'):
                    self.run_actual(name, client, post=post)
                self.assertEqual(2, len(writes)); self.assert_full_restoration(client, path, original)

    def test_readback_assertion_failure_still_restores_config(self):
        fields = {'test_save_config_roundtrip': ('tts', 'language', 'wrong'),
                  'test_save_pause_config_roundtrip': ('tts', 'pause_same_speaker_ms', 1),
                  'test_save_review_prompts_roundtrip': ('prompts', 'review_user_prompt', 'wrong'),
                  'test_save_persona_prompts_roundtrip': ('prompts', 'persona_advanced_prompt', 'wrong')}
        for name in FUNCTIONS:
            with self.subTest(function=name), self.fixture() as (client, path):
                original = client.get('/api/config').json(); reads = []
                def get(target, **kwargs):
                    response = client.get(target, **kwargs); reads.append(target)
                    if len(reads)==2:
                        document = response.json(); section, field, value = fields[name]; document[section][field] = value
                        return Mock(status_code=200, json=Mock(return_value=document))
                    return response
                with self.assertRaises(test_api.TestFailure):
                    self.run_actual(name, client, get=get)
                self.assert_full_restoration(client, path, original)

    def test_cleanup_http_failure_is_reported(self):
        for name in FUNCTIONS:
            with self.subTest(function=name), self.fixture() as (client, _):
                writes = []
                def post(target, **kwargs):
                    writes.append(target)
                    return client.post('/injected-missing', **kwargs) if len(writes)==2 else client.post(target, **kwargs)
                with self.assertRaisesRegex(test_api.TestFailure, 'cleanup could not restore'):
                    self.run_actual(name, client, post=post)
                self.assertEqual(2, len(writes))

    def test_successful_cleanup_cannot_hide_lost_custom_settings(self):
        for mode in ('local', 'remote'):
            for name in FUNCTIONS:
                with self.subTest(mode=mode, function=name), self.fixture(mode) as (client, _):
                    writes = []
                    def post(target, **kwargs):
                        writes.append(target)
                        if len(writes)==2:
                            kwargs = copy.deepcopy(kwargs); kwargs['json'].update(llm_failover=False, llm_remote_ssh=None)
                        return client.post(target, **kwargs)
                    with self.assertRaisesRegex(test_api.TestFailure, 'cleanup did not restore the complete'):
                        self.run_actual(name, client, post=post)

    def test_all_six_roundtrips_accept_initial_and_legacy_default_materialization(self):
        profile = dict(base_url='http://127.0.0.1:1234/v1', api_key='fixture-local', model_name='fixture')
        cases = [None, {}, {'llm': profile}, {'llm': profile, 'generation': None}, {'llm': profile, 'generation': {}},
                 {'llm': {**profile, 'base_url': 'http://127.0.0.1:1234/', 'api_key': 'env:UNREAD_FIXTURE'}}]
        for raw in cases:
            for name in (*FUNCTIONS, 'test_synthesis_window_config_roundtrip', 'test_repeated_config_save_with_inactive_profile'):
                with self.subTest(raw=raw, function=name), self.fixture(raw=raw, absent=raw is None) as (client, path):
                    original = client.get('/api/config').json()
                    self.run_actual(name, client)
                    self.assertEqual(test_api.get_config_after_save_snapshot(original), AppConfig(**client.get('/api/config').json()).model_dump())
                    saved = json.loads(path.read_text())
                    self.assertEqual('local' if not raw else raw['llm']['api_key'], saved['llm']['api_key'])
                    self.assertEqual(GenerationConfig().model_dump(), saved['generation'])
