"""Real source, HTTP and publication failures found in the #1040 review."""
import copy
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import generate_personas as personas
from speaker_traits import (get_persona_state_targets, get_persona_state_entries,
                            get_persona_state_chunk_indices)
from tests import test_state_personas as fixtures
from tests.test_state_personas import state_script


class StateEvidenceRegressionTests(unittest.TestCase):
    def test_case_distinct_speakers_do_not_combine_persistence_or_evidence(self):
        script = state_script()
        dialogue = [row for row in script if row['speaker'] == 'ARTHUR']
        for index, row in enumerate(dialogue):
            row['speaker'] = 'Lee' if index % 2 else 'LEE'
        # Each has five confirming lines per segment, below the ten-line threshold.
        self.assertEqual(get_persona_state_targets(script), {})
        script = state_script()
        for row in script:
            if row['speaker'] == 'ARTHUR':
                row['speaker'] = 'Lee'
        script.extend({'speaker': 'LEE', 'text': 'other character', 'speaker_gender': 'female',
                       'speaker_age_group': 'adult'} for _ in range(10))
        targets = get_persona_state_targets(script)['Lee']
        for target in targets:
            self.assertFalse(any(row['speaker'] == 'LEE' for row in get_persona_state_entries(script, target)))

    def test_supported_narrator_labels_remain_in_each_segment(self):
        for label in ('NARRATOR', 'Narrator', 'NARRATION', 'NARRATIVE'):
            with self.subTest(label=label):
                script = state_script()
                for row in script:
                    if row['speaker'] == 'NARRATOR':
                        row['speaker'] = label
                for target in get_persona_state_targets(script)['ARTHUR']:
                    rows = get_persona_state_entries(script, target)
                    self.assertEqual(rows[0]['speaker'], label)
                    self.assertEqual(rows[0]['_source_entry_index'], target['segment_start'])
                    self.assertEqual(personas._collect_narrator_context(rows, 'ARTHUR', 4), [rows[0]['text']])

    def test_real_chunk_preparation_maps_nonverbal_and_verbalized_text_but_not_edits(self):
        from project import group_into_chunks
        script = state_script()
        script[1]['text'] = '...'
        script[12]['text'] = '...'
        script[5]['text'] = '...'
        script[6]['text'] = 'Tea & bread'
        chunks = group_into_chunks(script)
        targets = get_persona_state_targets(script)['ARTHUR']
        mapping = get_persona_state_chunk_indices(script, chunks, 'ARTHUR')
        for target in targets:
            self.assertIn(target['from_entry'], mapping)
            self.assertIn(target['age_group'] + ' dialogue ' + ('1' if target['state_number'] <= 2 else '0'),
                          chunks[mapping[target['from_entry']]]['text'])
        chunks[mapping[targets[1]['from_entry']]]['text'] += ' foreign edit'
        self.assertEqual(get_persona_state_chunk_indices(script, chunks, 'ARTHUR'), {})
        # A source row can split around a scene break. Its second part is not
        # a safe boundary when the first part merged with the previous state.
        split_script = state_script(); del split_script[11]
        for row in split_script:
            row['instruct'] = 'neutral'
            row['text'] = 'A character says ' + row['text'] + '.'
        split_script[11]['text'] = 'An adult speaks here. ━ Another adult clause follows.'
        split_chunks = group_into_chunks(split_script)
        adult = get_persona_state_targets(split_script)['ARTHUR'][1]
        mapping = get_persona_state_chunk_indices(split_script, split_chunks, 'ARTHUR')
        self.assertIn('An adult speaks here.', split_chunks[1]['text'])
        self.assertIn('teen dialogue', split_chunks[1]['text'])
        self.assertNotIn(adult['from_entry'], mapping)



class StateApiCandidateRegressionTests(unittest.TestCase):
    fixture = fixtures.StateVoiceApiTests.fixture

    def test_orphan_cleanup_requires_current_book_and_preserves_applied_states(self):
        from speaker_traits import get_persona_state_targets
        for via_save in (False, True):
            with self.subTest(via_save=via_save), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                client, path, targets = self.fixture(directory, stack)
                script = state_script(); script.insert(0, {'speaker': 'NARRATOR', 'text': 'New opening'})
                Path(directory, 'annotated_script.json').write_text(json.dumps(script))
                orphan = targets[0]['version_id']
                self.assertNotIn(orphan, {t['version_id'] for t in get_persona_state_targets(script)['ARTHUR']})
                snapshot = client.get('/api/voice_config/snapshot').json()
                before = path.read_bytes()
                url = f'/api/voices/ARTHUR/versions/{orphan}'
                for token in (None, '0' * 64):
                    response = client.delete(url, params={'book_token': token} if token else {})
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(path.read_bytes(), before)
                config = json.loads(path.read_text()); config['ARTHUR']['active_version'] = orphan
                path.write_text(json.dumps(config))
                response = client.delete(url, params={'book_token': snapshot['book_token']})
                self.assertEqual(response.status_code, 409, response.text)
                del config['ARTHUR']['active_version']; path.write_text(json.dumps(config))
                snapshot = client.get('/api/voice_config/snapshot').json()
                expected = copy.deepcopy(config); del expected['ARTHUR']['versions'][orphan]
                legacy = client.post('/api/save_voice_config', json=expected)
                self.assertEqual(legacy.status_code, 409, legacy.text)
                self.assertEqual(json.loads(path.read_text()), config)
                if via_save:
                    response = client.post('/api/voice_config/save', json={'revision': snapshot['revision'],
                        'book_token': snapshot['book_token'], 'voices': expected})
                else:
                    response = client.delete(url, params={'book_token': snapshot['book_token']})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(json.loads(path.read_text()), expected)

    def test_narrator_alias_uses_exact_script_identity_for_validation(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, _ = self.fixture(directory, stack)
            script = state_script()
            for row in script:
                row['speaker'] = 'Narrator' if row['speaker'] == 'ARTHUR' else 'Other'
            Path(directory, 'annotated_script.json').write_text(json.dumps(script))
            targets = get_persona_state_targets(script)['Narrator']
            config = {'Narrator': {'versions': {t['version_id']: {'persona_state': t, 'type': 'clone',
                      'ref_audio': 'narrator.wav'} for t in targets}}}
            path.write_text(json.dumps(config))
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            for spelling, target in zip(('NARRATOR', 'Narrator'), targets):
                saved = json.loads(path.read_text())
                stale = copy.deepcopy(saved)
                stale['Narrator']['versions'][target['version_id']]['persona_state']['source_sha256'] = '0' * 64
                path.write_text(json.dumps(stale)); before = path.read_bytes()
                response = client.delete(f'/api/voices/{spelling}/versions/{target["version_id"]}', params={'book_token': token})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(path.read_bytes(), before)
                path.write_text(json.dumps(saved))
                response = client.delete(f'/api/voices/{spelling}/versions/{target["version_id"]}', params={'book_token': token})
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(json.loads(path.read_text())['Narrator']['versions']), 2)

    def test_invalid_state_seeds_refused_before_file_write(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            snapshot = client.get('/api/voice_config/snapshot').json(); before = path.read_bytes()
            for seed in ('', ' ', '1.2', 'bad', True, None):
                body = copy.deepcopy(snapshot['config'])
                body['ARTHUR']['versions'][targets[0]['version_id']]['seed'] = seed
                response = client.post('/api/voice_config/save', json={'revision': snapshot['revision'],
                    'book_token': snapshot['book_token'], 'voices': body})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(path.read_bytes(), before)
            body['ARTHUR']['versions'][targets[0]['version_id']]['seed'] = '-1'
            response = client.post('/api/voice_config/save', json={'revision': snapshot['revision'],
                'book_token': snapshot['book_token'], 'voices': body})
            self.assertEqual(response.status_code, 200, response.text)

    def test_blank_seed_form_normalizes_before_guarded_save(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            snapshot = client.get('/api/voice_config/snapshot').json()
            tests = Path(__file__).parent
            result = subprocess.run(['node', str(tests / 'state_persona_style_roundtrip_fixture.js'),
                str(tests.parent / 'static/js/app-core.js'), '--empty-seed'],
                input=json.dumps(snapshot), capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload['ARTHUR']['versions'][targets[0]['version_id']]['seed'], '-1')
            response = client.post('/api/voice_config/save', json={'revision': snapshot['revision'],
                'book_token': snapshot['book_token'], 'voices': payload})
            self.assertEqual(response.status_code, 200, response.text)
            saved = json.loads(path.read_text())['ARTHUR']['versions'][targets[0]['version_id']]
            self.assertEqual(int(saved['seed']), -1)

    def test_state_suggestions_preserve_scope_count_spoken_rows_and_refuse_base_apply(self):
        from routers import voices
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, _ = self.fixture(directory, stack)
            script = state_script()
            script[22:22] = [{'speaker': 'ARTHUR', 'text': '', 'speaker_gender': 'male',
                            'speaker_age_group': 'adult'} for _ in range(15)]
            Path(directory, 'annotated_script.json').write_text(json.dumps(script))
            targets = get_persona_state_targets(script)['ARTHUR']; target = targets[1]
            config = json.loads(path.read_text())
            config['ARTHUR']['versions'] = {t['version_id']: {'type': 'clone', 'ref_audio': 'fixture.wav',
                'description': t['age_group'], 'age_group': t['age_group'], 'persona_state': t} for t in targets}
            path.write_text(json.dumps(config))
            for name, result in [('_build_lora_candidates', [{'adapter_id': 'adult_voice', 'name': 'Adult',
                  'type': 'lora', 'gender': 'male', 'age_group': 'adult', 'description': 'Adult natural voice'}]),
                  ('_load_voice_library', {'casts': {}, 'favorites': []}), ('get_active_book_id', 'book')]:
                stack.enter_context(patch.object(voices, name, return_value=result))
            stack.enter_context(patch.object(voices, '_make_llm_client', side_effect=RuntimeError('offline fixture')))
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            result = voices._suggest_voices_impl(voices.SuggestVoicesRequest(characters=['ARTHUR'],
                state_version=target['version_id'], book_token=token))
            suggestion = result['suggestions']['ARTHUR']
            self.assertEqual(suggestion['line_count'], 10)
            self.assertEqual(suggestion['priority'], 'minor')
            self.assertEqual(suggestion.get('state_version'), target['version_id'])
            before = path.read_bytes()
            response = client.post('/api/suggest_voices/apply', json={'character': 'ARTHUR', 'suggestion': suggestion})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(path.read_bytes(), before)

    def test_export_checks_applied_versions_not_unused_base(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _, path, targets = self.fixture(directory, stack)
            config = json.loads(path.read_text()); config['NARRATOR'] = {'ready': True}
            config['ARTHUR']['version_timeline'] = [{'from_index': t['from_entry'], 'version_id': t['version_id']} for t in targets]
            stack.enter_context(patch.object(editor, 'DATA_DIR', directory))
            stack.enter_context(patch.object(editor, 'SCRIPT_PATH', str(Path(directory, 'annotated_script.json'))))
            schedule = stack.enter_context(patch.object(editor, 'schedule_claimed_background_task'))
            app = FastAPI(); app.include_router(editor.router)
            client = stack.enter_context(TestClient(app))
            for root_ready, states_ready, expected in ((False, True, 200), (True, False, 409)):
                config['ARTHUR']['ready'] = root_ready
                for version in config['ARTHUR']['versions'].values():
                    version['ready'] = states_ready
                path.write_text(json.dumps(config)); schedule.reset_mock()
                response = client.post('/api/merge_m4b', json={'require_ready': True})
                self.assertEqual(response.status_code, expected, response.text)
                self.assertEqual(schedule.called, expected == 200)
            # A root used before the first anchor must also be ready.
            config['ARTHUR']['ready'] = False
            for version in config['ARTHUR']['versions'].values():
                version['ready'] = True
            config['ARTHUR']['version_timeline'][0]['from_index'] += 1
            path.write_text(json.dumps(config))
            response = client.post('/api/merge_m4b', json={'require_ready': True})
            self.assertEqual(response.status_code, 409, response.text)


class StatePublicationCandidateRegressionTests(unittest.TestCase):
    def test_deleting_already_published_state_survives_final_cli_save(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import voices
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory); target = get_persona_state_targets(state_script())['ARTHUR'][0]['version_id']
            for name, value in [('SCRIPT_PATH', str(root / 'annotated_script.json')),
                                ('VOICE_CONFIG_PATH', str(root / 'voice_config.json'))]:
                stack.enter_context(patch.object(voices, name, value))
            app = FastAPI(); app.include_router(voices.router); client = stack.enter_context(TestClient(app))
            original = personas._compile_persona; deleted = []
            def compile_hook(*args, **kwargs):
                if args[6] == 'NARRATOR':
                    token = client.get('/api/voice_config/snapshot').json()['book_token']
                    response = client.delete(f'/api/voices/ARTHUR/versions/{target}', params={'book_token': token})
                    self.assertEqual(response.status_code, 200, response.text)
                    deleted.append(target)
                    self.assertNotIn(target, json.loads((root / 'voice_config.json').read_text())['ARTHUR']['versions'])
                return original(*args, **kwargs)
            with patch.object(personas, '_compile_persona', side_effect=compile_hook):
                fixtures.StatePersonaPipelineTests().run_generation(root, initial={'ARTHUR': {'type': 'clone', 'ref_audio': 'base.wav'}})
            versions = json.loads((root / 'voice_config.json').read_text())['ARTHUR']['versions']
            self.assertEqual(deleted, [target])
            self.assertNotIn(target, versions)
            self.assertEqual(len(versions), 3)

    def test_base_promotion_preserves_base_and_state_candidate_pools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); first = get_persona_state_targets(state_script())['ARTHUR'][0]
            base_pool = [{'candidate_id': 'root_saved', 'type': 'custom', 'voice': 'Ryan'}]
            state_pool = [{'candidate_id': 'state_saved', 'type': 'custom', 'voice': 'Serena'}]
            initial = {'ARTHUR': {'type': 'custom', 'voice': 'default', 'candidates': base_pool,
                'active_candidate': 'root_saved', 'versions': {first['version_id']: {'candidates': state_pool}}}}
            fixtures.StatePersonaPipelineTests().run_generation(root, initial=initial)
            voice = json.loads((root / 'voice_config.json').read_text())['ARTHUR']
            self.assertTrue(personas.voice_is_set(voice))
            self.assertEqual(voice['age_group'], first['age_group'])
            self.assertEqual(voice['candidates'], base_pool)
            self.assertEqual(voice['active_candidate'], 'root_saved')
            self.assertEqual(voice['versions'][first['version_id']]['candidates'], state_pool)

    def test_pending_only_reuses_completed_first_state_without_generating_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = get_persona_state_targets(state_script())['ARTHUR']
            versions = {t['version_id']: {'type': 'clone', 'ref_audio': t['version_id'] + '.wav',
                        'description': t['age_group'], 'age_group': t['age_group'], 'persona_state': t} for t in targets}
            initial = {'ARTHUR': {'type': 'custom', 'voice': 'default', 'versions': versions},
                       'NARRATOR': {'type': 'clone', 'ref_audio': 'narrator.wav'}}
            fixtures.StatePersonaPipelineTests().run_generation(root, initial=initial, new_only=True)
            saved = json.loads((root / 'voice_config.json').read_text())['ARTHUR']
            self.assertTrue(personas.voice_is_set(saved))
            self.assertEqual(saved['ref_audio'], versions[targets[0]['version_id']]['ref_audio'])
            self.assertEqual(saved['age_group'], targets[0]['age_group'])
            self.assertEqual(saved['versions'], versions)
            self.assertFalse((root / 'designed_voices').exists())


class StateCandidateUiTests(unittest.TestCase):
    def test_real_handlers_backup_pending_characters_and_report_empty_suggestions(self):
        tests = Path(__file__).parent
        result = subprocess.run(['node', str(tests / 'state_candidate_regressions_fixture.js'),
                                 str(tests.parent / 'static/js/app-core.js')], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
