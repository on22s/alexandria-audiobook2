"""Reveal review and real pending-audio publication after voice timeline edits."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
import soundfile as sf
from project import ProjectManager
from speaker_traits import is_possible_gender_reveal
from tests import test_state_personas as fixtures
from tests import test_state_voices_js as ui


class VoiceTimelineAudioTests(unittest.TestCase):
    def fixture(self, directory, stack):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import voices
        root = Path(directory)
        manager = ProjectManager(directory)
        script = [{'speaker': 'A', 'text': 'Before.'}, {'speaker': 'B', 'text': 'Other.'},
                  {'speaker': 'A', 'text': 'Changed.'}, {'speaker': 'A', 'text': 'Back.'}]
        (root / 'annotated_script.json').write_text(json.dumps(script))
        (root / 'state.json').write_text('{"active_book_id":"book","book_generation":"one"}')
        config = {'A': {'type': 'custom', 'voice': 'Ryan', 'versions': {
            'different': {'type': 'custom', 'voice': 'Serena'},
            'same': {'type': 'custom', 'voice': 'Ryan', 'description': None}}},
            'B': {'type': 'custom', 'voice': 'Ryan'}}
        path = root / 'voice_config.json'; path.write_text(json.dumps(config))
        rows = []
        for index, row in enumerate(script):
            audio = f'voicelines/original_{index}.wav'
            sf.write(root / audio, np.full(240, .1), 24000)
            rows.append({**row, 'id': index, 'uid': f'u{index}', 'status': 'done',
                         'audio_path': audio, 'custom': {'keep': index}})
        manager.save_chunks(rows)
        for name, value in [('SCRIPT_PATH', str(root / 'annotated_script.json')),
                            ('VOICE_CONFIG_PATH', str(path)), ('CHUNKS_PATH', manager.chunks_path),
                            ('project_manager', manager)]:
            stack.enter_context(patch.object(voices, name, value))
        app = FastAPI(); app.include_router(voices.router)
        client = stack.enter_context(TestClient(app))
        return root, manager, client, path, config, rows

    def apply(self, client, points):
        token = client.get('/api/voice_config/snapshot').json()['book_token']
        response = client.post('/api/voices/A/version_timeline', json={'book_token': token, 'points': points})
        self.assertEqual(response.status_code, 200, response.text)

    def test_apply_noop_clear_and_full_save_only_reset_effectively_changed_rows(self):
        for route in ('specialized', 'full_save'):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root, manager, client, path, config, rows = self.fixture(directory, stack)
                original_audio = {row['audio_path']: (root / row['audio_path']).read_bytes() for row in rows}
                points = [{'from_index': 2, 'version_id': 'different'}, {'from_index': 3, 'version_id': None}]
                if route == 'specialized':
                    self.apply(client, points)
                else:
                    snapshot = client.get('/api/voice_config/snapshot').json()
                    body = copy.deepcopy(snapshot['config']); body['A']['version_timeline'] = points
                    response = client.post('/api/voice_config/save', json={'voices': body,
                        'book_token': snapshot['book_token'], 'revision': snapshot['revision']})
                    self.assertEqual(response.status_code, 200, response.text)
                saved = manager.load_chunks()
                self.assertEqual([row['id'] for row in saved if row['status'] != 'done'], [2])
                self.assertIsNone(saved[2]['audio_path'])
                self.assertTrue(saved[2]['voice_revision'])
                for index in (0, 1, 3):
                    self.assertEqual(saved[index], rows[index])
                for name, data in original_audio.items():
                    self.assertEqual((root / name).read_bytes(), data)
                # Identical choices and irrelevant metadata preserve chunk bytes.
                before = Path(manager.chunks_path).read_bytes()
                self.apply(client, points)
                self.assertEqual(Path(manager.chunks_path).read_bytes(), before)
                snapshot = client.get('/api/voice_config/snapshot').json(); body = snapshot['config']
                body['A']['ready'] = True
                response = client.post('/api/voice_config/save', json={'voices': body,
                    'book_token': snapshot['book_token'], 'revision': snapshot['revision']})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(Path(manager.chunks_path).read_bytes(), before)
                # Clearing an applied timeline makes the existing state audio pending too.
                current = manager.load_chunks(); current[2].update(status='done', audio_path=rows[2]['audio_path'])
                manager.save_chunks(current)
                token = client.get('/api/voice_config/snapshot').json()['book_token']
                response = client.delete('/api/voices/A/version_timeline', params={'book_token': token})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual([r['id'] for r in manager.load_chunks() if r['status'] != 'done'], [2])

    def test_equivalent_voice_version_does_not_invalidate_audio(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _, manager, client, _, _, _ = self.fixture(directory, stack)
            before = Path(manager.chunks_path).read_bytes()
            self.apply(client, [{'from_index': 2, 'version_id': 'same'}])
            self.assertEqual(Path(manager.chunks_path).read_bytes(), before)

    def test_wrong_book_and_revision_leave_audio_and_voices_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _, manager, client, path, _, _ = self.fixture(directory, stack)
            snapshot = client.get('/api/voice_config/snapshot').json()
            chunks_before, voices_before = Path(manager.chunks_path).read_bytes(), path.read_bytes()
            response = client.post('/api/voices/A/version_timeline', json={'book_token': '0' * 64,
                'points': [{'from_index': 2, 'version_id': 'different'}]})
            self.assertEqual(response.status_code, 409, response.text)
            body = copy.deepcopy(snapshot['config']); body['A']['version_timeline'] = [{'from_index': 2, 'version_id': 'different'}]
            response = client.post('/api/voice_config/save', json={'book_token': snapshot['book_token'],
                'revision': '0' * 64, 'voices': body})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(Path(manager.chunks_path).read_bytes(), chunks_before)
            self.assertEqual(path.read_bytes(), voices_before)

    def test_apply_during_render_prevents_old_audio_from_being_published(self):
        for mode in ('single', 'native_batch'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root, manager, client, _, _, rows = self.fixture(directory, stack)
                original_audio = (root / rows[2]['audio_path']).read_bytes()
                def render(text, instruct, speaker, config, output):
                    self.apply(client, [{'from_index': 2, 'version_id': 'different'}])
                    sf.write(output, np.full(240, .8), 24000)
                    return True
                def batch(chunks, config, output, seed):
                    self.apply(client, [{'from_index': 2, 'version_id': 'different'}])
                    for chunk in chunks:
                        sf.write(Path(output) / f"temp_batch_{chunk['index']}.wav", np.full(240, .8), 24000)
                    return {'completed': [chunk['index'] for chunk in chunks], 'failed': []}
                manager.engine = SimpleNamespace(generate_voice=render, generate_batch=batch)
                if mode == 'single':
                    success, message = manager.generate_chunk_audio(2)
                    self.assertFalse(success)
                else:
                    result = manager.generate_chunks_batch([2], batch_size=1)
                    self.assertEqual(result['completed'], [])
                    message = result['failed'][0][1]
                self.assertIn('generation inputs', message)
                saved = manager.load_chunks()[2]
                self.assertEqual(saved['status'], 'pending')
                self.assertIsNone(saved['audio_path'])
                self.assertEqual((root / rows[2]['audio_path']).read_bytes(), original_audio)

    def test_journal_invalidation_and_regeneration_publish_a_real_replacement(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, _, _, rows = self.fixture(directory, stack)
            with manager._chunks_lock:
                manager._get_status_journal_locked().apply_update('u2', {'status': 'generating'})
            self.apply(client, [{'from_index': 2, 'version_id': 'different'}])
            self.assertEqual(manager.load_chunks()[2]['status'], 'pending')
            def render(text, instruct, speaker, config, output):
                self.assertEqual(config['A']['voice'], 'Serena')
                sf.write(output, np.full(240, .8), 24000)
                return True
            manager.engine = SimpleNamespace(generate_voice=render)
            stack.enter_context(patch('project._export_audio_segment', side_effect=RuntimeError('CPU WAV fixture')))
            success, message = manager.generate_chunk_audio(2)
            self.assertTrue(success, message)
            saved = manager.load_chunks()[2]
            self.assertEqual(saved['status'], 'done')
            audio, rate = sf.read(root / saved['audio_path'])
            self.assertEqual(rate, 24000)
            self.assertAlmostEqual(float(audio.mean()), .8, places=3)
            self.assertEqual(saved['custom'], rows[2]['custom'])

    def test_parallel_journal_worker_cannot_publish_after_timeline_changes(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, _, _, rows = self.fixture(directory, stack)
            original_audio = (root / rows[2]['audio_path']).read_bytes()
            def render(text, instruct, speaker, config, output):
                self.assertIsNotNone(manager._active_journal_identity)
                self.apply(client, [{'from_index': 2, 'version_id': 'different'}])
                sf.write(output, np.full(240, .8), 24000)
                return True
            manager.engine = SimpleNamespace(generate_voice=render)
            result = manager.generate_chunks_parallel([2], max_workers=1)
            self.assertEqual(result['completed'], [])
            self.assertIn('generation inputs', result['failed'][0][1])
            self.assertEqual(manager.load_chunks()[2]['status'], 'pending')
            self.assertIsNone(manager.load_chunks()[2]['audio_path'])
            self.assertEqual((root / rows[2]['audio_path']).read_bytes(), original_audio)

    def test_failed_voice_publication_is_conservative_and_keeps_old_audio(self):
        from routers import voices
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _, rows = self.fixture(directory, stack)
            old_config = path.read_bytes(); audio = (root / rows[2]['audio_path']).read_bytes()
            token = client.get('/api/voice_config/snapshot').json()['book_token']
            def refuse_save(config, target):
                self.assertFalse(manager._chunks_lock.acquire(blocking=False),
                                 'chunk capture must remain locked through voice publication')
                self.assertEqual(manager._read_chunks()[2]['status'], 'pending')
                raise OSError('fixture voice publication failure')
            with patch('voice_config_store.atomic_json_write', side_effect=refuse_save):
                with self.assertRaisesRegex(OSError, 'voice publication failure'):
                    voices._mutate_book_voice_entry('A', lambda entry: entry.update(
                        version_timeline=[{'from_index': 2, 'version_id': 'different'}]), token)
            self.assertEqual(path.read_bytes(), old_config)
            self.assertEqual(manager.load_chunks()[2]['status'], 'pending')
            self.assertEqual((root / rows[2]['audio_path']).read_bytes(), audio)

    def test_alias_ensemble_and_narrator_effective_voices_are_included(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _, manager, _, _, config, rows = self.fixture(directory, stack)
            config['Alias'] = {'alias_of': 'A'}
            config['Group'] = {'type': 'ensemble', 'members': ['A', 'B']}
            config['NARRATOR'] = {'type': 'custom', 'voice': 'Ryan', 'narrator_strategy': 'focus'}
            before = copy.deepcopy(config)
            config['A']['version_timeline'] = [{'from_index': 2, 'version_id': 'different'}]
            for name, extra in [('Alias', {}), ('Group', {}), ('NARRATOR', {'focus_speaker': 'A'})]:
                chunk = {**rows[2], **extra, 'speaker': name}
                self.assertNotEqual(manager.get_chunk_voice_inputs(before, chunk, 2),
                                    manager.get_chunk_voice_inputs(config, chunk, 2))


class RevealReviewTests(unittest.TestCase):
    def test_only_known_gender_changes_with_small_age_differences_are_flagged(self):
        previous = {'gender': 'male', 'age_group': 'child'}
        for age in ('child', 'young_child'):
            self.assertTrue(is_possible_gender_reveal(previous, {'gender': 'female', 'age_group': age}))
        for current in ({'gender': 'male', 'age_group': 'child'}, {'gender': 'unknown', 'age_group': 'child'},
                        {'gender': 'female', 'age_group': 'unknown'}, {'gender': 'female', 'age_group': 'adult'}):
            self.assertFalse(is_possible_gender_reveal(previous, current))

    def test_reveal_panel_and_main_choice_never_save_before_apply(self):
        runner = ui.VoiceStatesJsTests()
        data = ui.suggestion(); data['states'][1].update(gender='female', possible_gender_reveal=True)
        html = runner.render(data)
        self.assertIn('Possible identity reveal', html)
        self.assertIn('Choose Main voice throughout', html)
        self.assertNotIn('Possible identity reveal', runner.render(ui.suggestion()))
        result = runner.run_js(r'''
const selects=[{value:'version:a',disabled:false},{value:'version:b',disabled:false},{value:'version:unsafe',disabled:true}];
context.chooseMainVoiceStates({disabled:false,closest:()=>({querySelectorAll:()=>selects})});
console.log(JSON.stringify({values:selects.map(s=>s.value),calls}));''', {})
        self.assertEqual(result['values'], ['main', 'main', 'version:unsafe'])
        self.assertEqual(result['calls'], [])

    def test_main_voice_save_finishes_before_apply_and_failure_stops_the_action(self):
        runner = ui.VoiceStatesJsTests()
        result = runner.run_js(r"""
const card={dataset:{voice:'A'},isConnected:true,querySelectorAll:()=>[]};
const button={closest:()=>card,disabled:false,innerHTML:'Apply'};
let saved=false;
context.flushVoiceSaves=async()=>{calls.push(['FLUSH']);saved=true;};
await context.applyVoiceStateSave(button,'A','Applying…',async()=>{if(!saved)throw Error('base not saved');calls.push(['APPLY']);});
context.flushVoiceSaves=async()=>{throw Error('main voice save failed');};
try{await context.applyVoiceStateSave(button,'A','Applying…',async()=>calls.push(['WRONG APPLY']));}catch(error){calls.push(['ERROR',error.message]);}
console.log(JSON.stringify({calls,disabled:button.disabled}));""", {})
        self.assertEqual(result['calls'], [['FLUSH'], ['APPLY'], ['ERROR', 'main voice save failed']])
        self.assertFalse(result['disabled'])

    def test_real_state_endpoint_reports_reveal_without_rewriting_script(self):
        from routers import voices
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            client, _, _ = fixtures.StateVoiceApiTests().fixture(directory, stack)
            script = fixtures.state_script()[:22]
            for row in script:
                if row['speaker'] == 'ARTHUR':
                    row['speaker_age_group'] = 'child'
                    row['speaker_gender'] = 'male' if row['text'].startswith('teen') else 'female'
            path = Path(directory, 'annotated_script.json'); path.write_text(json.dumps(script)); before = path.read_bytes()
            stack.enter_context(patch.object(voices, '_build_lora_candidates', return_value=[]))
            result = client.get('/api/voices/ARTHUR/state_timeline')
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual([state['possible_gender_reveal'] for state in result.json()['states']], [False, True])
            self.assertEqual(path.read_bytes(), before)
