"""Failures independently demonstrated by the #1040 code-review-20 pass."""
import copy
import json
import subprocess
import tempfile
import unittest
import uuid
import wave
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import generate_personas as personas
from routers import voices
from speaker_traits import get_persona_state_targets
from tests import test_state_personas as state_persona_tests

state_script = state_persona_tests.state_script


class PreviewEngine:
    def generate_voice_design(self, description, sample_text):
        path = self.root / 'designed_voices/previews' / f'preview_{uuid.uuid4().hex}.wav'
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'NONE'))
            stream.writeframes(b'\0\0' * 160)
        return str(path), None


class StatePersonaReviewRegressions(unittest.TestCase):
    fixture = state_persona_tests.StateVoiceApiTests.fixture

    def test_every_full_save_refuses_wrong_boundaries_and_applied_version_deletion(self):
        for endpoint in ('/api/voice_config/save', '/api/save_voice_config'):
            for action in ('wrong_boundary', 'remove_timeline_version', 'remove_active_version'):
                with self.subTest(endpoint=endpoint, action=action), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    client, path, targets = self.fixture(directory, stack)
                    current = json.loads(path.read_text()); version = targets[1]['version_id']
                    if action == 'remove_timeline_version':
                        current['ARTHUR']['version_timeline'] = [{'from_index': targets[1]['from_entry'], 'version_id': version}]
                    if action == 'remove_active_version':
                        current['ARTHUR']['active_version'] = version
                    path.write_text(json.dumps(current))
                    snapshot = client.get('/api/voice_config/snapshot').json(); body = copy.deepcopy(snapshot['config'])
                    if action == 'wrong_boundary':
                        body['ARTHUR']['version_timeline'] = [{'from_index': 0, 'version_id': version}]
                    else:
                        del body['ARTHUR']['versions'][version]
                    before = path.read_bytes()
                    payload = {'revision': snapshot['revision'], 'book_token': snapshot['book_token'], 'voices': body} if endpoint.endswith('/save') else body
                    result = client.post(endpoint, json=payload)
                    self.assertEqual(result.status_code, 409, result.text)
                    self.assertEqual(path.read_bytes(), before)

    def test_base_edits_preserve_an_unchanged_stale_timeline(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            current = json.loads(path.read_text())
            current['ARTHUR']['version_timeline'] = [{'from_index': 1, 'version_id': targets[0]['version_id']}]
            path.write_text(json.dumps(current))
            script = state_script(); script[1]['text'] = 'edited source opening'
            Path(directory, 'annotated_script.json').write_text(json.dumps(script))
            snapshot = client.get('/api/voice_config/snapshot').json(); body = copy.deepcopy(snapshot['config'])
            body['ARTHUR']['description'] = 'Edited base only'
            result = client.post('/api/voice_config/save', json={'revision': snapshot['revision'], 'book_token': snapshot['book_token'], 'voices': body})
            self.assertEqual(result.status_code, 200, result.text)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['ARTHUR']['versions'], current['ARTHUR']['versions'])
            self.assertEqual(saved['ARTHUR']['version_timeline'], current['ARTHUR']['version_timeline'])
            self.assertEqual(saved['ARTHUR']['description'], 'Edited base only')

    def test_generic_version_save_preserves_generated_provenance_and_manual_ids(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            snapshot = client.get('/api/voice_config/snapshot').json(); before = path.read_bytes()
            target = targets[1]
            result = client.post('/api/voices/ARTHUR/versions', json={'version_id': target['version_id'], 'config': {'type': 'custom', 'voice': 'Ryan'}})
            self.assertEqual(result.status_code, 409, result.text); self.assertEqual(path.read_bytes(), before)
            config = copy.deepcopy(snapshot['config']['ARTHUR']['versions'][target['version_id']]); config['description'] = 'Edited adult'
            result = client.post('/api/voices/ARTHUR/versions', json={'version_id': target['version_id'], 'age_group': 'adult', 'config': config})
            self.assertEqual(result.status_code, 200, result.text)
            result = client.post('/api/voices/ARTHUR/versions', json={'version_id': 'state_manual', 'config': {'type': 'custom', 'voice': 'Ryan'}})
            self.assertEqual(result.status_code, 200, result.text)
            selected = client.post('/api/voices/ARTHUR/versions/' + target['version_id'] + '/select')
            self.assertEqual(selected.status_code, 200, selected.text)
            saved = client.post('/api/voices/ARTHUR/versions', json={'version_id': 'manual-copy'})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertNotIn('persona_state', saved.json()['versions']['manual-copy'])

    def test_repeated_state_defaults_only_offer_versions_that_can_apply(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            result = client.delete('/api/voices/ARTHUR/versions/' + targets[3]['version_id'] + '?book_token=' + token)
            self.assertEqual(result.status_code, 200, result.text)
            stack.enter_context(patch.object(voices, '_build_lora_candidates', return_value=[]))
            timeline = client.get('/api/voices/ARTHUR/state_timeline').json()
            self.assertEqual([v['version_id'] for v in timeline['states'][0]['sources']['versions']], [targets[0]['version_id']])
            self.assertEqual(timeline['states'][3]['sources']['versions'], [])
            source = Path(__file__).parent.parent / 'static/js/app-core.js'
            script = """const fs=require('fs'),vm=require('vm');const s=fs.readFileSync(process.argv[1],'utf8'),d=JSON.parse(process.argv[2]),c={};vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function getVoiceStateDefault('),s.indexOf('function renderVoiceStateRows(')),c);console.log(JSON.stringify(d.states.map((state,i)=>{const value=c.getVoiceStateDefault(state,d.applied,i);return{from_index:state.from_index,version_id:value.startsWith('version:')?value.slice(8):null};})));"""
            result = subprocess.run(['node', '-e', script, str(source), json.dumps(timeline)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            applied = client.post('/api/voices/ARTHUR/version_timeline', json={'book_token': token, 'points': json.loads(result.stdout)})
            self.assertEqual(applied.status_code, 200, applied.text)

    def test_literal_speaker_braces_reach_real_state_compilation(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory); script = state_script()[:22]; name = 'ALICE {MASKED}'
            for row in script:
                if row['speaker'] == 'ARTHUR':
                    row['speaker'] = name
            engine = PreviewEngine(); engine.root = root
            prompts = []
            def compile_persona(client, model, system, build_prompt, parts, params, label):
                prompts.append(build_prompt(parts))
                ref = json.loads(parts[0][1]); age = ref['sample_lines'][0].split()[0]
                return {'description': f'A natural {age} character voice.', 'ref_text': ref['sample_lines'][0]}
            stack.enter_context(patch.object(personas, 'call_llm_for_object', side_effect=RuntimeError('fixture discovery fallback')))
            stack.enter_context(patch.object(personas, 'request_persona_with_evidence', side_effect=compile_persona))
            config = {}
            failures = personas.run_advanced_persona_generation(script, [name], {}, config, None, 'fixture', engine, str(root), SimpleNamespace(batch_size=40))
            self.assertEqual(failures, [])
            self.assertEqual(len(prompts), 2)
            self.assertTrue(all(name in prompt for prompt in prompts))
            self.assertIn('settled male, teen state', prompts[0]); self.assertIn('settled male, adult state', prompts[1])
            self.assertEqual({v['description'] for v in config[name]['versions'].values()}, {'A natural teen character voice.', 'A natural adult character voice.'})

    def test_recovered_state_cli_skips_discovery_and_compilation_and_keeps_siblings(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack); root = Path(directory)
            original = json.loads(path.read_text()); target = targets[1]
            original['ARTHUR']['versions'][target['version_id']].update(description='A deliberate recovered adult voice.', ref_text='An adult state line.')
            path.write_text(json.dumps(original)); token = client.get('/api/voice_config/snapshot').json()['book_token']
            engine = PreviewEngine(); engine.root = root
            for name, value in [('get_runtime_data_dir', directory), ('load_app_config', {}), ('get_active_llm_config', {'model_name': 'fixture'}), ('ensure_ideal_settings', (False, {}, 'fixture')), ('make_run_client', object()), ('TTSEngine', engine)]:
                stack.enter_context(patch.object(personas, name, return_value=value))
            discovery = stack.enter_context(patch.object(personas, '_discover_batch_characters', side_effect=AssertionError('recovery must not rediscover')))
            compiler = stack.enter_context(patch.object(personas, '_compile_persona', side_effect=AssertionError('recovery must not recompile')))
            stack.enter_context(patch('sys.argv', ['generate_personas.py', '--advanced', '--speakers', 'ARTHUR', '--state-version', target['version_id'], '--recovered-speaker', 'ARTHUR', '--book-token', token]))
            personas.main(); discovery.assert_not_called(); compiler.assert_not_called()
            saved = json.loads(path.read_text())
            self.assertEqual(saved['ARTHUR']['versions'][targets[0]['version_id']], original['ARTHUR']['versions'][targets[0]['version_id']])
            for key in set(original['ARTHUR']) - {'versions'}:
                self.assertEqual(saved['ARTHUR'][key], original['ARTHUR'][key])
            version = saved['ARTHUR']['versions'][target['version_id']]
            self.assertEqual(version['description'], 'A deliberate recovered adult voice.')
            self.assertTrue((root / version['ref_audio']).read_bytes().startswith(b'RIFF'))

    def test_native_suggestion_sorting_keeps_collapsed_base_and_state_groups(self):
        tests = Path(__file__).parent
        result = subprocess.run(['node', str(tests / 'state_persona_review_tree_fixture.js'),
            str(tests.parent / 'static/js/app-core.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_ui_recovery_payload_saves_and_resumes_only_its_target(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            before = json.loads(path.read_text()); snapshot = client.get('/api/voice_config/snapshot').json()
            tests = Path(__file__).parent
            result = subprocess.run(['node', str(tests / 'state_persona_review_fixture.js'), str(tests.parent / 'static/js/app-core.js'), str(tests.parent / 'static/js/app-scripts.js'), json.dumps(snapshot)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            packet = json.loads(result.stdout)
            stack.enter_context(patch.object(voices, 'check_global_gpu_lock'))
            stack.enter_context(patch.object(voices, 'reserve_background_task', return_value='fixture-claim'))
            register = stack.enter_context(patch.object(voices, 'register_claimed_background_task'))
            response = client.post(packet['path'], json=packet['body'])
            self.assertEqual(response.status_code, 200, response.text)
            command = register.call_args.args[4]
            self.assertIn('--advanced', command)
            self.assertEqual(command[command.index('--state-version') + 1], targets[1]['version_id'])
            self.assertEqual(command[command.index('--book-token') + 1], snapshot['book_token'])
            saved = json.loads(path.read_text()); expected = copy.deepcopy(before)
            expected['ARTHUR']['versions'][targets[1]['version_id']].update(description='Recovered adult state voice.', character_style='Recovered adult state voice.', ref_text='Adult state line.')
            self.assertEqual(saved, expected)
