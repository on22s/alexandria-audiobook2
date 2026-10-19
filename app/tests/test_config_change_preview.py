"""Read-only preview uses Save's normalization and hides sensitive contents."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import system
from config_settings import AppConfig, GenerationConfig, TTSConfig


class ConfigChangePreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.path = self.root/'config.json'
        self.manager = Mock()
        self.profile = {'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'SYNTHETIC_API_KEY', 'model_name': 'Synthetic model'}
        self.payload = {'llm': self.profile.copy(), 'llm_local': self.profile.copy(), 'llm_mode': 'local', 'tts': {'mode': 'local'}}
        for p in [patch.object(system, 'CONFIG_PATH', str(self.path)), patch.object(system, 'project_manager', self.manager)]:
            p.start(); self.addCleanup(p.stop)
        api = FastAPI(); api.include_router(system.router)
        self.client = TestClient(api); self.addCleanup(self.client.close)

    def preview(self, payload=None):
        return self.client.post('/api/config/preview', json=self.payload if payload is None else payload)

    def test_actual_preview_matches_normalized_save_and_changes_no_file_or_engine(self):
        self.path.write_text(json.dumps(self.payload)); before = self.path.read_bytes()
        changed = copy.deepcopy(self.payload)
        changed['tts'].update(parallel_workers=3, batch_seed=0, compile_codec=True)
        changed['generation'] = {'temperature': 0, 'top_k': 0}
        response = self.preview(changed); self.assertEqual(200, response.status_code, response.text)
        data = response.json(); rows = {row['path']: row for row in data['changes']}
        self.assertEqual(('2', '3'), (rows['tts.parallel_workers']['before'], rows['tts.parallel_workers']['after']))
        self.assertEqual('0', rows['tts.batch_seed']['after']); self.assertEqual('On', rows['tts.compile_codec']['after'])
        self.assertEqual('0', rows['generation.temperature']['after'])
        self.assertGreater(len(data['defaults']), 10)
        self.assertEqual(before, self.path.read_bytes()); self.assertFalse(list(self.root.glob('*.damaged-*')))
        self.manager.reset_engine.assert_not_called()
        result = self.client.post('/api/config', json=changed); self.assertEqual(200, result.status_code, result.text)
        saved = json.loads(self.path.read_text())
        self.assertEqual(3, saved['tts']['parallel_workers']); self.assertEqual(0, saved['generation']['temperature'])
        self.manager.reset_engine.assert_called_once()
        self.assertEqual([], self.preview(changed).json()['changes'])

    def test_credentials_headers_bodies_urls_prompts_and_unknown_keys_do_not_escape(self):
        original = copy.deepcopy(self.payload)
        original['SECRET_UNKNOWN_KEY'] = 'UNMANAGED_VALUE_SECRET'
        original['llm_local'].update(provider_headers={'Authorization': 'HEADER_SECRET'}, provider_extra_body={'token': 'BODY_SECRET'})
        original['llm'] = original['llm_local'].copy()
        self.path.write_text(json.dumps(original)); before = self.path.read_bytes()
        proposed = copy.deepcopy(original); proposed['llm_local']['api_key'] = '[REDACTED]'
        proposed['llm_local']['provider_headers'] = {'Authorization': 'NEW_HEADER_SECRET'}
        proposed['llm_local']['provider_extra_body'] = {'token': 'NEW_BODY_SECRET'}
        proposed['tts']['url'] = 'http://user:URL_SECRET@127.0.0.1:7860/path?token=QUERY_SECRET'
        proposed['tts']['external_urls'] = ['http://EXTERNAL_SECRET.invalid']
        proposed['prompts'] = {'review_user_prompt': 'PROMPT_SECRET'}
        response = self.preview(proposed); self.assertEqual(200, response.status_code, response.text)
        for secret in ['SYNTHETIC_API_KEY', 'HEADER_SECRET', 'BODY_SECRET', 'URL_SECRET', 'QUERY_SECRET', 'EXTERNAL_SECRET', 'PROMPT_SECRET', 'SECRET_UNKNOWN_KEY', 'UNMANAGED_VALUE_SECRET', '[REDACTED]']:
            self.assertNotIn(secret, response.text)
        self.assertGreater(response.json()['unmanaged_fields'], 0)
        self.assertNotIn('llm_local.api_key', [r['path'] for r in response.json()['changes']])
        self.assertEqual(before, self.path.read_bytes()); self.manager.reset_engine.assert_not_called()

    def test_legacy_local_and_remote_changes_have_correct_scope_and_no_inherited_cap(self):
        legacy = {'llm': self.profile.copy(), 'tts': {'mode': 'local'}, 'generation': {'max_tokens': 16384}}
        self.path.write_text(json.dumps(legacy))
        proposed = copy.deepcopy(self.payload)
        proposed['llm_remote'] = {**self.profile, 'base_url': 'http://127.0.0.1:9999/v1', 'model_name': 'Remote synthetic', 'max_output_tokens': 2000}
        response = self.preview(proposed); self.assertEqual(200, response.status_code, response.text)
        rows = {row['path']:row for row in response.json()['changes']}
        self.assertIn('inactive after Save', rows['llm_remote.max_output_tokens']['scope'])
        self.assertNotIn('llm_local.max_output_tokens', rows)
        proposed['llm_mode'] = 'remote'; proposed['llm'] = proposed['llm_remote'].copy()
        data = self.preview(proposed).json(); self.assertEqual('remote', data['active_after'])
        self.assertEqual('Remote model · active after Save', next(r for r in data['changes'] if r['path']=='llm_remote.model_name')['scope'])
        self.assertNotIn('llm', [r['path'].split('.')[0] for r in data['changes']])

    def test_invalid_proposals_and_endpoint_sentinel_refusals_return_no_input_details(self):
        self.path.write_text(json.dumps(self.payload)); before = self.path.read_bytes()
        cases = [[], {'SECRET_PAYLOAD_KEY':'VALUE_SECRET'}]
        for field, value in [('api_key', {'nested':'INVALID_SECRET'}), ('base_url', 'http://PUBLIC_SECRET.invalid/v1')]:
            item = copy.deepcopy(self.payload); item['llm_local'][field] = value; cases.append(item)
        item = copy.deepcopy(self.payload); item['llm_local'].update(base_url='http://127.0.0.1:9999/v1',api_key='[REDACTED]');cases.append(item)
        item = copy.deepcopy(self.payload);item['tts'].update(design_text_min_chars=100,design_text_max_chars=10);cases.append(item)
        item = copy.deepcopy(self.payload);item['prompts']={'attribution_preset':'private'};item['prompt_presets']=[{'name':'private','variant':'default','user_prompt':'UNKNOWN_PRESET_SECRET'}];cases.append(item)
        for item in cases:
            response = self.preview(item); self.assertEqual(400,response.status_code,response.text)
            for secret in ['SECRET_PAYLOAD_KEY','VALUE_SECRET','INVALID_SECRET','PUBLIC_SECRET','UNKNOWN_PRESET_SECRET','[REDACTED]']:
                self.assertNotIn(secret,response.text)
        self.assertEqual(before,self.path.read_bytes());self.manager.reset_engine.assert_not_called()

    def test_get_form_mode_switch_previews_and_saves_each_bound_key_in_both_directions(self):
        initial = copy.deepcopy(self.payload)
        initial['llm_remote'] = {**self.profile, 'base_url':'http://127.0.0.1:9999/v1', 'api_key':'REMOTE_SYNTHETIC_KEY'}
        self.path.write_text(json.dumps(initial))
        for mode, key in [('remote','REMOTE_SYNTHETIC_KEY'), ('local','SYNTHETIC_API_KEY')]:
            proposed = self.client.get('/api/config').json()
            proposed['llm_mode'] = mode
            proposed['llm'] = proposed['llm_' + mode].copy()
            before = self.path.read_bytes()
            response = self.preview(proposed)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(mode, response.json()['active_after'])
            self.assertEqual(before, self.path.read_bytes())
            changed = copy.deepcopy(proposed)
            changed['llm_' + mode]['base_url'] = 'http://127.0.0.1:4321/v1'
            changed['llm'] = changed['llm_' + mode].copy()
            self.assertEqual(400, self.preview(changed).status_code)
            self.assertEqual(400, self.client.post('/api/config',json=changed).status_code)
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(200, self.client.post('/api/config',json=proposed).status_code)
            saved = json.loads(self.path.read_text())
            self.assertEqual(key,saved['llm']['api_key'])
            self.assertEqual('SYNTHETIC_API_KEY',saved['llm_local']['api_key'])
            self.assertEqual('REMOTE_SYNTHETIC_KEY',saved['llm_remote']['api_key'])

    def test_missing_and_damaged_baselines_are_explicit_without_backup_or_creation(self):
        data=self.preview().json();self.assertEqual('missing',data['baseline']);self.assertFalse(self.path.exists())
        self.path.write_bytes(b'{ damaged config SECRET_BYTES');before=self.path.read_bytes()
        response=self.preview();self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('damaged',response.json()['baseline']);self.assertGreater(response.json()['warning_count'],0)
        self.assertNotIn('SECRET_BYTES',response.text);self.assertEqual(before,self.path.read_bytes())
        self.assertFalse(list(self.root.glob('*.damaged-*')));self.manager.reset_engine.assert_not_called()

    def test_unsent_fields_and_preset_models_are_preserved_and_presenter_does_not_mutate(self):
        initial=copy.deepcopy(self.payload);initial['generation']={'temperature':.5,'three_pass_chunk_size':9000}
        initial['prompts']={'pass1_prompt_presets':[{'name':'mine','system_prompt':'Synthetic saved template','user_prompt':'Synthetic user'}]}
        self.path.write_text(json.dumps(initial))
        changed=copy.deepcopy(self.payload);changed['generation']={'temperature':0};changed['prompts']={}
        before=copy.deepcopy(changed);response=self.preview(changed);self.assertEqual(200,response.status_code,response.text)
        self.assertNotIn('generation.three_pass_chunk_size',[r['path'] for r in response.json()['changes']])
        self.assertNotIn('prompts.pass1_prompt_presets',[r['path'] for r in response.json()['changes']])
        self.assertEqual(before,changed)
        self.assertEqual(200,self.client.post('/api/config',json=changed).status_code)
        self.assertEqual(9000,json.loads(self.path.read_text())['generation']['three_pass_chunk_size'])
        self.assertEqual('mine',json.loads(self.path.read_text())['prompts']['pass1_prompt_presets'][0]['name'])
        self.assertEqual([],self.preview(changed).json()['changes'])
