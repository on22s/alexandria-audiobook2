"""State persona evidence and independently published versions (#866)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import generate_personas as personas
from speaker_traits import get_persona_state_targets, get_persona_state_entries


def state_script():
    rows = []
    for age in ('teen', 'adult', 'elderly', 'teen'):
        rows.append({'speaker': 'NARRATOR', 'text': f'{age} scene begins'})
        rows.extend({'speaker': 'ARTHUR', 'text': f'{age} dialogue {number}',
                     'speaker_gender': 'male', 'speaker_age_group': age}
                    for number in range(10))
    return rows


class StatePersonaTests(unittest.TestCase):
    def test_segments_preserve_original_indices_and_isolate_repeated_states(self):
        script = state_script()
        targets = get_persona_state_targets(script)['ARTHUR']
        self.assertEqual([t['age_group'] for t in targets], ['teen', 'adult', 'elderly', 'teen'])
        self.assertEqual(len({t['version_id'] for t in targets}), 4)
        for target in targets:
            rows = get_persona_state_entries(script, target)
            for row in rows:
                index = row['_source_entry_index']
                self.assertEqual(row['text'], script[index]['text'])
                self.assertGreaterEqual(index, target['segment_start'])
                self.assertLess(index, target['segment_end'])
            dialogue = [r['text'] for r in rows if r['speaker'] == 'ARTHUR']
            self.assertEqual(len(dialogue), 10)
            self.assertTrue(all(target['age_group'] in text for text in dialogue))
        # Unknown lead-in belongs to the first segment; noisy single labels cannot split it.
        script[1]['speaker_gender'] = 'unknown'
        script[1]['speaker_age_group'] = 'unknown'
        self.assertEqual(get_persona_state_targets(script)['ARTHUR'][0]['segment_start'], 0)

    def test_one_band_and_short_noise_preserve_existing_state_rules(self):
        script = state_script()[:11]
        script.extend({'speaker': 'ARTHUR', 'text': 'nearby age', 'speaker_gender': 'male',
                       'speaker_age_group': 'young_adult'} for _ in range(10))
        script.extend({'speaker': 'ARTHUR', 'text': 'noise', 'speaker_gender': 'female',
                       'speaker_age_group': 'elderly'} for _ in range(9))
        self.assertEqual(get_persona_state_targets(script), {})

    def test_generation_targets_versions_and_preserves_base_and_manual_versions(self):
        script = state_script()
        voice = {'ARTHUR': {'type': 'clone', 'ref_audio': 'original.wav', 'versions': {
            'manual': {'description': 'human edit'}}}}
        seen = []
        def run(entries, selected, samples, config, client, model, engine, root, args, **options):
            seen.append((copy.deepcopy(entries), copy.deepcopy(samples), options['advanced_prompt']))
            config['ARTHUR'] = {'type': 'clone', 'ref_audio': f'state{len(seen)}.wav',
                                'description': samples['ARTHUR'][0], 'ref_text': samples['ARTHUR'][0]}
            return []
        with patch.object(personas, '_run_advanced_speaker_generation', side_effect=run):
            failures, voice = personas.run_advanced_persona_generation(script, ['ARTHUR'], {}, voice,
                None, 'fixture', None, '/unused', SimpleNamespace(batch_size=40))
        self.assertEqual(failures, [])
        self.assertEqual(voice['ARTHUR']['ref_audio'], 'original.wav')
        self.assertEqual(voice['ARTHUR']['versions']['manual']['description'], 'human edit')
        self.assertEqual(len(voice['ARTHUR']['versions']), 5)
        self.assertEqual(len(seen), 4)
        target = get_persona_state_targets(script)['ARTHUR'][1]
        self.assertEqual(personas.get_pending_state_targets(script, 'ARTHUR', voice['ARTHUR'], True), [])
        self.assertEqual(personas.get_pending_state_targets(script, 'ARTHUR', voice['ARTHUR'],
                                                         version_id=target['version_id']), [target])

    def test_publication_keeps_concurrent_sibling_edit_and_refuses_target_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'voice_config.json'
            initial = {'ARTHUR': {'versions': {'a': {'description': 'old a'}, 'b': {'description': 'old b'}}}}
            generated = copy.deepcopy(initial)
            generated['ARTHUR']['versions']['a']['description'] = 'generated a'
            current = copy.deepcopy(initial)
            current['ARTHUR']['versions']['b']['description'] = 'human b'
            path.write_text(json.dumps(current))
            result = personas.save_generated_voice_config(str(path), generated, initial, ['ARTHUR'], {})
            self.assertEqual(result['ARTHUR']['versions'], {'a': {'description': 'generated a'}, 'b': {'description': 'human b'}})
            current['ARTHUR']['versions']['a']['description'] = 'human a'
            path.write_text(json.dumps(current))
            result = personas.save_generated_voice_config(str(path), generated, initial, ['ARTHUR'], {})
            self.assertEqual(result['ARTHUR']['versions']['a']['description'], 'human a')


class StateVoiceApiTests(unittest.TestCase):
    def fixture(self, directory, stack):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import voices
        root = Path(directory)
        script = state_script()
        targets = get_persona_state_targets(script)['ARTHUR']
        (root / 'annotated_script.json').write_text(json.dumps(script))
        config = {'ARTHUR': {'type': 'clone', 'ref_audio': 'base.wav', 'description': 'base',
                            'versions': {target['version_id']: {'type': 'clone',
                                'ref_audio': f"state{target['state_number']}.wav", 'description': target['age_group'],
                                'age_group': target['age_group'], 'persona_state': target}
                                for target in targets}}}
        path = root / 'voice_config.json'; path.write_text(json.dumps(config))
        (root / 'chunks.json').write_text(json.dumps([{'speaker': row['speaker'], 'text': row['text']}
                                                   for row in script]))
        (root / 'state.json').write_text('{"active_book_id":"book","book_generation":"one"}')
        for name, value in [('SCRIPT_PATH', str(root / 'annotated_script.json')),
                            ('VOICE_CONFIG_PATH', str(path)), ('CHUNKS_PATH', str(root / 'chunks.json'))]:
            stack.enter_context(patch.object(voices, name, value))
        app = FastAPI(); app.include_router(voices.router)
        client = stack.enter_context(TestClient(app))
        return client, path, targets

    def test_state_actions_and_guarded_form_save_leave_base_and_siblings_intact(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            snapshot = client.get('/api/voice_config/snapshot').json()
            scope = {'version_id': targets[1]['version_id'], 'book_token': snapshot['book_token']}
            base = json.loads(path.read_text())
            for endpoint, body in [
                ('approval', {'persona_status': 'approved'}),
                ('candidates', {'candidate_id': 'new', 'config': {'type': 'custom', 'voice': 'Serena'}}),
                ('candidates/new/select', {}),
                ('candidates/new/favorite', {'favorite': True}),
            ]:
                result = client.post('/api/voices/ARTHUR/' + endpoint, json={**scope, **body})
                self.assertEqual(result.status_code, 200, result.text)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['ARTHUR']['ref_audio'], 'base.wav')
            self.assertEqual(saved['ARTHUR']['versions'][targets[0]['version_id']],
                             base['ARTHUR']['versions'][targets[0]['version_id']])
            self.assertEqual(saved['ARTHUR']['versions'][targets[1]['version_id']]['voice'], 'Serena')
            fresh = client.get('/api/voice_config/snapshot').json()
            payload = copy.deepcopy(fresh['config'])
            payload['ARTHUR']['versions'][targets[2]['version_id']]['description'] = 'edited elderly'
            result = client.post('/api/voice_config/save', json={'revision': fresh['revision'],
                'book_token': fresh['book_token'], 'voices': payload})
            self.assertEqual(result.status_code, 200, result.text)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['ARTHUR']['description'], 'base')
            self.assertEqual(saved['ARTHUR']['versions'][targets[1]['version_id']]['voice'], 'Serena')
            self.assertEqual(saved['ARTHUR']['versions'][targets[2]['version_id']]['description'], 'edited elderly')

    def test_first_state_applies_to_unknown_leadin_dialogue_in_merged_opening_chunk(self):
        from contextlib import ExitStack
        from speaker_traits import get_persona_state_chunk_indices
        from tts import voice_config_for_chunk
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, _ = self.fixture(directory, stack)
            root = Path(directory); script = state_script()
            script.insert(1, {'speaker': 'ARTHUR', 'text': 'unknown opening line',
                              'speaker_gender': 'unknown', 'speaker_age_group': 'unknown'})
            targets = get_persona_state_targets(script)['ARTHUR']
            (root / 'annotated_script.json').write_text(json.dumps(script))
            chunks = copy.deepcopy(script)
            chunks[1]['text'] += ' ' + chunks[2]['text']; del chunks[2]
            (root / 'chunks.json').write_text(json.dumps(chunks))
            config = json.loads(path.read_text())
            config['ARTHUR']['versions'] = {target['version_id']: {
                'type': 'clone', 'ref_audio': target['age_group'] + '.wav', 'persona_state': target}
                for target in targets}
            path.write_text(json.dumps(config))
            mapping = get_persona_state_chunk_indices(script, chunks, 'ARTHUR')
            self.assertEqual(mapping[targets[0]['from_entry']], 1)
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            suggested = client.get('/api/voices/ARTHUR/state_timeline').json()
            self.assertEqual(suggested['states'][0]['from_index'], 1)
            points = [{'from_index': mapping[target['from_entry']], 'version_id': target['version_id']}
                      for target in targets]
            result = client.post('/api/voices/ARTHUR/version_timeline', json={'book_token': token, 'points': points})
            self.assertEqual(result.status_code, 200, result.text)
            saved = json.loads(path.read_text())
            self.assertEqual(voice_config_for_chunk(saved, 'ARTHUR', 1)['ARTHUR']['ref_audio'], 'teen.wav')

    def test_remove_state_keeps_base_siblings_and_audio_and_refuses_applied_or_stale(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            version_id = targets[1]['version_id']
            url = f'/api/voices/ARTHUR/versions/{version_id}'
            audio = Path(directory) / 'state2.wav'; audio.write_bytes(b'shared audio fixture')
            original = json.loads(path.read_text())
            before = path.read_bytes()
            for query in ['', '?book_token=' + '0' * 64]:
                result = client.delete(url + query)
                self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual(path.read_bytes(), before)
            for field, value in [('active_version', version_id),
                                 ('version_timeline', [{'from_index': 10, 'version_id': version_id}])]:
                applied = copy.deepcopy(original); applied['ARTHUR'][field] = value
                path.write_text(json.dumps(applied)); before = path.read_bytes()
                result = client.delete(url + '?book_token=' + token)
                self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual(path.read_bytes(), before)
            stale = copy.deepcopy(original)
            stale['ARTHUR']['versions'][version_id]['persona_state']['segment_sha256'] = '0' * 64
            path.write_text(json.dumps(stale))
            result = client.delete(url + '?book_token=' + token)   # a stale, unapplied state can be removed (#1040 review C1)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertNotIn(version_id, json.loads(path.read_text())['ARTHUR']['versions'])
            path.write_text(json.dumps(original))
            result = client.delete(url + '?book_token=' + token)
            self.assertEqual(result.status_code, 200, result.text)
            expected = copy.deepcopy(original); del expected['ARTHUR']['versions'][version_id]
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertEqual(audio.read_bytes(), b'shared audio fixture')
            self.assertEqual(client.delete(url + '?book_token=' + token).status_code, 404)

    def test_missing_token_wrong_version_and_changed_book_refuse_without_writes(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            before = path.read_bytes()
            for scope in [{'version_id': targets[0]['version_id']},
                          {'version_id': 'missing', 'book_token': token},
                          {'version_id': targets[0]['version_id'], 'book_token': '0' * 64}]:
                result = client.post('/api/voices/ARTHUR/approval', json={**scope, 'persona_status': 'approved'})
                self.assertIn(result.status_code, (404, 409), result.text)
                self.assertEqual(path.read_bytes(), before)

    def test_timeline_selects_versions_and_refuses_a_boundary_inside_a_merged_chunk(self):
        from contextlib import ExitStack
        from tts import voice_config_for_chunk
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            points = [{'from_index': target['from_entry'], 'version_id': target['version_id']} for target in targets]
            result = client.post('/api/voices/ARTHUR/version_timeline', json={'book_token': token, 'points': points})
            self.assertEqual(result.status_code, 200, result.text)
            config = json.loads(path.read_text())
            for target in targets:
                resolved = voice_config_for_chunk(config, 'ARTHUR', target['from_entry'])['ARTHUR']
                self.assertEqual(resolved['ref_audio'], f"state{target['state_number']}.wav")
            chunks_path = Path(directory) / 'chunks.json'
            chunks = json.loads(chunks_path.read_text())
            boundary = targets[1]['from_entry']
            # Removing intervening narration and joining the two dialogue rows puts the change inside audio.
            previous = boundary - 2
            chunks[previous]['text'] += ' ' + chunks[boundary]['text']
            del chunks[previous + 1:boundary + 1]
            chunks_path.write_text(json.dumps(chunks))
            before = path.read_bytes()
            result = client.post('/api/voices/ARTHUR/version_timeline', json={'book_token': token, 'points': points})
            self.assertEqual(result.status_code, 409, result.text)
            self.assertEqual(path.read_bytes(), before)


    def test_manual_state_prefixed_version_names_remain_opaque_and_compatible(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            saved = client.post('/api/voices/ARTHUR/versions', json={'book_token': token,
                'version_id': 'state_custom', 'config': {'type': 'custom', 'voice': 'Ryan'}})
            self.assertEqual(saved.status_code, 200, saved.text)
            applied = client.post('/api/voices/ARTHUR/version_timeline', json={'book_token': token,
                'points': [{'from_index': 2, 'version_id': 'state_custom'}]})
            self.assertEqual(applied.status_code, 200, applied.text)

    def test_removing_generated_state_provenance_is_refused_by_guarded_form_save(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            snapshot = client.get('/api/voice_config/snapshot').json()
            payload = copy.deepcopy(snapshot['config'])
            del payload['ARTHUR']['versions'][targets[0]['version_id']]['persona_state']
            before = path.read_bytes()
            result = client.post('/api/voice_config/save', json={'revision': snapshot['revision'],
                'book_token': snapshot['book_token'], 'voices': payload})
            self.assertEqual(result.status_code, 409, result.text)
            self.assertEqual(path.read_bytes(), before)


class StatePersonaPipelineTests(unittest.TestCase):
    def run_generation(self, root, fail_age=None, switch_book=False, initial=None, edit_future=None, new_only=False):
        from contextlib import ExitStack
        import re
        import uuid
        import wave
        script = state_script()
        (root / 'annotated_script.json').write_text(json.dumps(script))
        (root / 'voice_config.json').write_text(json.dumps(initial or {}))
        class Engine:
            changed = False
            def generate_voice_design(self, description, sample_text):
                if fail_age and sample_text.startswith(fail_age):
                    raise RuntimeError('fixture voice-design failure')
                path = root / 'designed_voices/previews' / f'preview_{uuid.uuid4().hex}.wav'
                path.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(path), 'wb') as stream:
                    stream.setparams((1, 2, 16000, 0, 'NONE', 'NONE'))
                    stream.writeframes(b'\0\0' * 160)
                if edit_future and not self.changed:
                    self.changed = True
                    config = json.loads((root / 'voice_config.json').read_text())
                    adult = get_persona_state_targets(script)['ARTHUR'][1]['version_id']
                    if edit_future == 'delete':
                        del config['ARTHUR']['versions'][adult]
                    else:
                        config['ARTHUR']['versions'][adult]['description'] = 'human edit while teen generates'
                    (root / 'voice_config.json').write_text(json.dumps(config))
                if switch_book:
                    (root / 'annotated_script.json').write_text('[{"speaker":"OTHER","text":"replacement book"}]')
                return str(path), None
        def discovery(client, model, system, prompt, params, **kwargs):
            rows = [(int(index), text) for index, text in re.findall(r'\[(\d+)\] ARTHUR: ([^\n]+)', prompt)]
            return {'ARTHUR': {'evidence': [{'entry_index': index, 'quote': text} for index, text in rows],
                               'sample_lines': [text for _, text in rows], 'voice_clues': ['supported by this segment']}}
        def compile_persona(client, model, system, build_prompt, parts, params, label, **kwargs):
            line = next(json.loads(text) for field, text in parts if field == 'sample_lines')
            return {'description': f"Natural {line.split()[0]} voice.", 'ref_text': line}
        with ExitStack() as stack:
            for name, value in [('get_runtime_data_dir', str(root)), ('load_app_config', {}),
                                ('get_active_llm_config', {'model_name': 'fixture'}),
                                ('ensure_ideal_settings', (False, {'context_length': 4096}, 'fixture')),
                                ('make_run_client', object()), ('TTSEngine', Engine())]:
                stack.enter_context(patch.object(personas, name, return_value=value))
            stack.enter_context(patch.object(personas, 'call_llm_for_object', side_effect=discovery))
            stack.enter_context(patch.object(personas, 'request_persona_with_evidence', side_effect=compile_persona))
            stack.enter_context(patch('sys.argv', ['generate_personas.py', '--advanced', '--batch-size', '5'] + (['--new-only'] if new_only else [])))
            personas.main()
        return script

    def test_real_cli_compilation_and_preview_artifacts_are_separated_by_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = self.run_generation(root)
            saved = json.loads((root / 'voice_config.json').read_text())['ARTHUR']
            targets = get_persona_state_targets(script)['ARTHUR']
            previews = []
            for target in targets:
                version = saved['versions'][target['version_id']]
                self.assertEqual(version['ref_text'], target['age_group'] + ' dialogue 0')
                self.assertEqual(version['description'], f"Natural {target['age_group']} voice.")
                reference = json.loads((root / version['persona_ref']).read_text())
                self.assertTrue(all(target['age_group'] in line for line in reference['sample_lines']))
                for observation in reference['observations']:
                    for evidence in observation['evidence']:
                        self.assertEqual(script[evidence['entry_index']]['text'], evidence['quote'])
                path = root / version['ref_audio']
                self.assertTrue(path.read_bytes().startswith(b'RIFF'))
                previews.append(str(path))
            self.assertEqual(len(set(previews)), 4)
            self.assertEqual(saved['ref_audio'], saved['versions'][targets[0]['version_id']]['ref_audio'])
            self.assertEqual(list((root / 'designed_voices/previews').glob('*.wav')), [])
            self.assertNotIn('version_timeline', saved)

    def test_failed_target_is_reported_but_successful_targets_are_already_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, 'Persona generation failed'):
                self.run_generation(root, fail_age='adult')
            saved = json.loads((root / 'voice_config.json').read_text())['ARTHUR']
            ages = [value['age_group'] for value in saved['versions'].values()]
            self.assertEqual(ages.count('teen'), 2)
            self.assertIn('elderly', ages)
            self.assertNotIn('adult', ages)

    def test_foreign_state_samples_and_source_indices_are_rejected(self):
        target = get_persona_state_targets(state_script())['ARTHUR'][1]
        rows = get_persona_state_entries(state_script(), target)
        result = personas.get_validated_state_discovery([{'name': 'ARTHUR',
            'features': ['foreign claim'], 'sample_lines': ['teen dialogue 0'],
            'evidence': [{'entry_index': 1, 'quote': 'teen dialogue 0'}]}], rows, 0, ['ARTHUR'])
        self.assertFalse(result[0]['features'])
        self.assertTrue(all(line.startswith('adult') for line in result[0]['sample_lines']))
        self.assertTrue(all(target['segment_start'] <= item['entry_index'] < target['segment_end']
                            for item in result[0]['evidence']))


    def test_book_change_during_preview_refuses_generated_state_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "Active book changed"):
                self.run_generation(root, switch_book=True)
            self.assertEqual(json.loads((root / "voice_config.json").read_text()), {})
            self.assertEqual(json.loads((root / "annotated_script.json").read_text())[0]["speaker"], "OTHER")


    def test_edit_or_deletion_of_later_state_survives_earlier_state_publication(self):
        adult = get_persona_state_targets(state_script())['ARTHUR'][1]
        original = {'ARTHUR': {'type': 'clone', 'ref_audio': 'base.wav', 'versions': {
            adult['version_id']: {'type': 'clone', 'ref_audio': 'saved-adult.wav',
                'description': 'original adult', 'persona_state': adult}}}}
        for action in ('edit', 'delete'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.run_generation(root, initial=original, edit_future=action)
                versions = json.loads((root / 'voice_config.json').read_text())['ARTHUR']['versions']
                if action == 'delete':
                    self.assertNotIn(adult['version_id'], versions)
                else:
                    self.assertEqual(versions[adult['version_id']]['description'], 'human edit while teen generates')
                    self.assertEqual(versions[adult['version_id']]['ref_audio'], 'saved-adult.wav')


class StateGenerationAdmissionTests(unittest.TestCase):
    fixture = StateVoiceApiTests.fixture

    def test_targeted_generation_command_and_stale_admission(self):
        from contextlib import ExitStack
        from routers import voices
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            stack.enter_context(patch.object(voices, 'check_global_gpu_lock'))
            stack.enter_context(patch.object(voices, 'project_manager'))
            schedule = stack.enter_context(patch.object(voices, 'schedule_claimed_background_task'))
            payload = {'speaker': 'ARTHUR', 'state_version': targets[1]['version_id'], 'book_token': token}
            result = client.post('/api/generate_personas', json=payload)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.json()['advanced'])
            command = schedule.call_args.args[3]
            self.assertIn('--advanced', command)
            self.assertEqual(command[command.index('--state-version') + 1], targets[1]['version_id'])
            self.assertEqual(command[command.index('--book-token') + 1], token)
            before = path.read_bytes()
            schedule.reset_mock()
            for invalid in [{**payload, 'book_token': '0' * 64},
                            {**payload, 'state_version': 'missing'},
                            {**payload, 'age_group': 'teen'}]:
                response = client.post('/api/generate_personas', json=invalid)
                self.assertIn(response.status_code, (409, 422), response.text)
                schedule.assert_not_called()
                self.assertEqual(path.read_bytes(), before)

    def test_state_json_recovery_keeps_the_base_and_other_state_versions(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            before = json.loads(path.read_text())
            response = client.post('/api/persona/recover', json={'speaker': 'ARTHUR',
                'state_version': targets[1]['version_id'], 'book_token': token,
                'persona_json': json.dumps({'description': 'A deliberate adult voice.', 'ref_text': 'Adult dialogue here.'})})
            self.assertEqual(response.status_code, 200, response.text)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['ARTHUR']['description'], before['ARTHUR']['description'])
            self.assertEqual(saved['ARTHUR']['versions'][targets[0]['version_id']],
                             before['ARTHUR']['versions'][targets[0]['version_id']])
            self.assertEqual(saved['ARTHUR']['versions'][targets[1]['version_id']]['description'], 'A deliberate adult voice.')

    def test_state_voice_suggestions_use_only_that_states_dialogue_and_persona(self):
        from contextlib import ExitStack
        from routers import voices
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, path, targets = self.fixture(directory, stack)
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            before = path.read_bytes()
            catalog = [{'adapter_id': 'adult_voice', 'name': 'Adult', 'type': 'lora',
                        'gender': 'male', 'age_group': 'adult', 'description': 'Adult natural voice'}]
            stack.enter_context(patch.object(voices, '_build_lora_candidates', return_value=catalog))
            stack.enter_context(patch.object(voices, '_make_llm_client', side_effect=RuntimeError('offline fixture')))
            stack.enter_context(patch.object(voices, '_load_voice_library', return_value={'casts': {}, 'favorites': []}))
            stack.enter_context(patch.object(voices, '_script_line_counts', return_value={'ARTHUR': 40}))
            stack.enter_context(patch.object(voices, 'get_active_book_id', return_value='book'))
            infer = stack.enter_context(patch.object(voices, '_infer_character_traits', wraps=voices._infer_character_traits))
            result = voices._suggest_voices_impl(voices.SuggestVoicesRequest(characters=['ARTHUR'],
                        state_version=targets[1]['version_id'], book_token=token))
            self.assertEqual(list(result['suggestions']), ['ARTHUR'])
            self.assertEqual(result['suggestions']['ARTHUR']['line_count'], 10)
            self.assertTrue(all(line.startswith('adult dialogue') for line in infer.call_args.args[2]))
            self.assertEqual(infer.call_args.args[1], 'adult')
            self.assertEqual(path.read_bytes(), before)
