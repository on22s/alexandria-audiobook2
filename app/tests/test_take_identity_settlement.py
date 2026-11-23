"""Route-built narrator versions and numbered takes share one settled identity."""
import asyncio
from contextlib import nullcontext
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audio_take_history import AudioTakeHistory
from book_state_transaction import get_book_operation_token
from core import AppError, handle_app_error
from synthesis_recovery_identity import get_recovery_digest
from synthesis_recovery_serving import NativeRecoveryServingIdentity
from take_publication import KEY
from take_restoration import capture_restore_target, restore_editor_take
from tests import test_tts_batch_contracts as route_contracts
from tests import test_take_restoration as restoration_cases


def serving_identity(engine, request):
    return NativeRecoveryServingIdentity(
        get_recovery_digest(asdict(request)), 'c' * 64, 'd' * 64)


class SettledIdentityTests(unittest.TestCase):
    def install_cyclic_voice_book(self, root, manager, reader, healthy):
        manager.save_chunks([reader, healthy])
        script = root / 'annotated_script.json'
        script.write_text(json.dumps([reader, healthy, {'speaker': 'NARRATOR', 'text': 'Narration.'}]))
        voice = {'type': 'custom', 'voice': 'Ryan', 'seed': 42}
        Path(manager.voice_config_path).write_text(json.dumps({
            'NARRATOR': {**voice, 'narrator_strategy': 'focus'},
            'READER': {**voice, 'versions': {'B': {'type': 'custom', 'alias_of': 'NARRATOR'}},
                       'version_timeline': [{'from_index': 0, 'from_uid': reader['uid'], 'version_id': 'B'}]},
            'Y': voice}))
        return script

    def test_bad_row_settles_without_aborting_grouped_or_recovery_batch(self):
        case = restoration_cases.TakeRestorationTests()
        for grouped in (True, False):
            owner = nullcontext() if grouped else case.owner()
            with self.subTest(grouped=grouped), case.fixture() as (root, manager, _, _), owner:
                reader = dict(uid='reader', speaker='READER', text='Synthetic reader.',
                              focus_speaker='READER', status='pending')
                healthy = dict(uid='healthy', speaker='Y', text='Healthy.', status='pending')
                self.install_cyclic_voice_book(root, manager, reader, healthy)
                engine = route_contracts.get_engine()
                engine._device = 'cpu'
                calls = []

                def synth(**kwargs):
                    calls.append(kwargs['text'])
                    return [np.full(2400, .2)], 24000

                engine._init_local_custom = lambda: SimpleNamespace(generate_custom_voice=synth)
                with patch.object(manager, 'get_engine', return_value=engine), \
                        patch.object(manager, '_capture_synthesis_recovery_parent',
                                     wraps=manager._capture_synthesis_recovery_parent) as capture:
                    result = manager.generate_chunks_batch([0, 1], batch_size=2, batch_group_by_type=grouped)
                self.assertEqual([1], result['completed'])
                self.assertEqual([(0, "Alias cycle detected for speaker 'READER'")], result['failed'])
                self.assertEqual(0, result['cancelled'])
                bad, good = manager.load_chunks()
                self.assertEqual(('error', None), (bad['status'], bad.get('audio_path')))
                self.assertEqual('done', good['status'])
                self.assertEqual(2400, sf.info(root / good['audio_path']).frames)
                self.assertEqual([['Healthy.']], calls)
                self.assertEqual(['healthy'], [call.args[2]['uid'] for call in capture.call_args_list])

    def test_strategy_repair_invalidates_only_bad_old_row_and_refuses_reintroducing_cycle(self):
        case = restoration_cases.TakeRestorationTests()
        with case.fixture() as (root, manager, prior, fresh):
            reader = dict(uid='reader', speaker='READER', text='Synthetic reader.',
                          focus_speaker='READER', status='done', audio_path=str(prior.relative_to(root)))
            healthy = dict(uid='healthy', speaker='Y', text='Healthy.', status='done',
                           audio_path=str(fresh.relative_to(root)))
            script = self.install_cyclic_voice_book(root, manager, reader, healthy)
            before = manager.load_chunks()
            voice_path = Path(manager.voice_config_path)
            routes = route_contracts.voices
            app = FastAPI()
            app.include_router(routes.router)
            app.add_exception_handler(AppError, handle_app_error)
            with patch.multiple(routes, SCRIPT_PATH=str(script), VOICE_CONFIG_PATH=str(voice_path),
                                CHUNKS_PATH=manager.chunks_path, project_manager=manager), \
                    TestClient(app, raise_server_exceptions=False) as client:
                response = client.post('/api/narrator/strategy', json={'strategy': 'global'})
                self.assertEqual(200, response.status_code, response.text)
                rows = manager.load_chunks()
                self.assertEqual(('pending', None), (rows[0]['status'], rows[0]['audio_path']))
                self.assertNotEqual(before[0].get('voice_revision'), rows[0]['voice_revision'])
                self.assertEqual(before[1], rows[1])
                saved = voice_path.read_bytes()
                self.assertEqual('global', json.loads(saved)['NARRATOR']['narrator_strategy'])
                refused = client.post('/api/narrator/strategy', json={'strategy': 'focus'})
                self.assertEqual(400, refused.status_code, refused.text)
                reason = "Alias cycle detected for speaker 'READER'"
                self.assertEqual({
                    'detail': f'Could not resolve the voice alias. {reason}',
                    'message_key': 'error.voice.alias_resolution',
                    'message_values': {'reason': reason},
                }, refused.json())
                self.assertEqual(saved, voice_path.read_bytes())
                self.assertEqual(rows, manager.load_chunks())

    def save_timeline_alias(self, root, manager, row, direction, strategy='gender'):
        narrator = {'type': 'custom', 'voice': 'Ryan', 'seed': 42, 'narrator_strategy': strategy}
        alternate = {'type': 'custom', 'voice': 'Serena', 'seed': 7, 'gender': 'male',
                     'character_style': 'steady'}
        if direction == 'incoming':
            voices = {'NARRATOR': narrator, 'READER': {'type': 'custom', 'voice': 'Vivian', 'seed': 42}}
            source, target = 'READER', 'NARRATOR'
        else:
            voices = {'NARRATOR': narrator, 'TARGET': alternate, 'READER': {'alias_of': 'NARRATOR'}}
            source, target = 'NARRATOR', 'TARGET'
        script = root / 'annotated_script.json'
        script.write_text(json.dumps([{**row, 'speaker': name} for name in voices]))
        voice_path = Path(manager.voice_config_path)
        voice_path.write_text(json.dumps(voices))
        routes = route_contracts.voices
        app = FastAPI()
        app.include_router(routes.router)
        with patch.multiple(routes, SCRIPT_PATH=str(script), VOICE_CONFIG_PATH=str(voice_path),
                            CHUNKS_PATH=manager.chunks_path, project_manager=manager), TestClient(app) as client:
            def post(path, **payload):
                response = client.post(path, json=payload)
                self.assertEqual(response.status_code, 200, response.text)

            if direction == 'incoming':
                post('/api/voices/NARRATOR/versions', version_id='M', config=alternate)
            post(f'/api/voices/{source}/versions', version_id='B',
                 config={'type': 'custom', 'alias_of': target, 'gender': 'female'})
            post(f'/api/voices/{source}/version_timeline',
                 points=[{'from_index': 0, 'version_id': 'B'}])
        saved = json.loads(voice_path.read_text())
        self.assertEqual(saved[source]['version_timeline'][0]['from_uid'], row['uid'])
        self.assertEqual(saved[source]['versions']['B']['alias_of'], target)
        return saved

    def run_case(self, mode, dispatch):
        case = restoration_cases.TakeRestorationTests()
        calls = []

        class Model:
            def generate_custom_voice(self, **kwargs):
                key = (kwargs['speaker'], kwargs['instruct'], torch.initial_seed())
                calls.append(key)
                amplitude = {'Serena': .4, 'Aiden': .3}.get(key[0], .2) + (key[2] % 17) / 100
                return [np.sin(np.arange(2400) * .1) * amplitude], 24000

        def generate(manager):
            if dispatch == 'batch':
                seed = -1 if mode == 'route_alias_target' else 42
                result = manager.generate_chunks_batch([0], batch_size=2, batch_seed=seed)
                return (result['completed'] == [0] and not result['failed']), result
            return manager.generate_chunk_audio(0)

        with case.fixture() as (root, manager, _, fresh), case.owner():
            engine = case.engine()
            engine._local_custom_model = Model()
            Path(manager.config_path).write_text(json.dumps({'tts': {
                **engine.get_model_settings(), 'synthesis_recovery': engine._synthesis_recovery}}))
            manager.invalidate_config_cache()
            row = manager.load_chunks()[0]
            row['narrator_gender'] = 'male'
            if mode == 'route_alias_target':
                row['speaker'] = 'READER'
                manager.save_chunks([row])
                voices = self.save_timeline_alias(root, manager, row, 'incoming')
            elif mode in ('route', 'route_alias', 'route_active'):
                row['speaker'] = 'READER' if mode == 'route_alias' else 'NARRATOR'
                manager.save_chunks([row])
                (root / 'annotated_script.json').write_text(json.dumps([
                    {**row, 'speaker': 'NARRATOR'}]))
                base = {'type': 'custom', 'voice': 'Ryan', 'seed': 42,
                        'gender': 'female', 'narrator_strategy': 'gender'}
                voices = {'NARRATOR': base}
                if mode == 'route_alias':
                    voices['READER'] = {'alias_of': 'NARRATOR'}
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                with patch.object(route_contracts.voices, 'SCRIPT_PATH',
                                  str(root / 'annotated_script.json')), \
                        patch.object(route_contracts.voices, 'VOICE_CONFIG_PATH',
                                     manager.voice_config_path), \
                        patch.object(route_contracts.voices, 'CHUNKS_PATH', manager.chunks_path), \
                        patch.object(route_contracts.voices, 'project_manager', manager):
                    route_contracts.save_version('NARRATOR', 'A', dict(base))
                    route_contracts.save_version('NARRATOR', 'B', {})
                    route_contracts.select_at('NARRATOR', 'B', index=0)
                voices = json.loads(Path(manager.voice_config_path).read_text())
                self.assertEqual(voices['NARRATOR']['versions']['B']['narrator_strategy'],
                                 'gender')
                self.assertEqual(voices['NARRATOR']['version_timeline'][0]['from_uid'],
                                 row['uid'])
            else:
                voices = {'NARRATOR': {'type': 'custom', 'voice': 'Ryan', 'seed': 42}}
                if mode == 'numbered':
                    config = json.loads(Path(manager.config_path).read_text())
                    config['tts']['reproducible_takes'] = True
                    Path(manager.config_path).write_text(json.dumps(config))
                    manager.invalidate_config_cache()
                    row['take'] = 2
                    manager.save_chunks([row])
            Path(manager.voice_config_path).write_text(json.dumps(voices))
            manager.invalidate_config_cache()
            with patch.object(manager, 'get_engine', return_value=engine), \
                    patch('synthesis_recovery.capture_native_serving_identity',
                          side_effect=serving_identity), \
                    patch('synthesis_recovery_serving.capture_native_custom_serving_identity',
                          side_effect=serving_identity):
                ok, message = generate(manager)
                self.assertTrue(ok, message)
                origin = manager.load_chunks()[0]
                self.assertEqual(origin[KEY]['version'], 3)
                audio = (root / origin['audio_path']).read_bytes()
                first = calls[:]
                manager.update_chunk(0, {'take': 3})
                manager._publish_generated_chunk_audio(
                    manager.load_chunks()[0], str(fresh), 'NARRATOR')
                current = manager.load_chunks()[0]
                displaced = (root / current['audio_path']).read_bytes()
                self.assertNotEqual(audio, displaced)
                history = AudioTakeHistory(root)
                with history._locked() as document:
                    record = next(item for item in document['records']
                                  if item['audio_revision'] == origin['audio_revision'])
                request = SimpleNamespace(
                    expected_book_token=get_book_operation_token(root),
                    source_book_token=get_book_operation_token(root),
                    expected_uid=current['uid'],
                    expected_audio_revision=current['audio_revision'],
                    expected_audio_path=current['audio_path'],
                    expected_take=current['take'], version=record['version'],
                    sha256=record['sha256'])
                if mode in ('route', 'route_alias'):
                    voices['NARRATOR']['versions']['A']['voice'] = 'Serena'
                elif mode == 'route_active':
                    voices['NARRATOR']['versions']['B']['voice'] = 'Serena'
                elif mode == 'route_alias_target':
                    self.assertEqual(origin[KEY]['seed'], 7)
                    voices['NARRATOR']['versions']['M']['voice'] = 'Aiden'
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                manager.invalidate_config_cache()
                try:
                    restored = restore_editor_take(
                        manager, 0, request, capture_restore_target(manager, 0, request))
                    outcome = 'restored'
                    self.assertEqual((root / restored['audio_path']).read_bytes(), audio)
                except ValueError as error:
                    outcome = str(error)
                    self.assertEqual((root / current['audio_path']).read_bytes(), displaced)
                calls.clear()
                with case.fixture() as (other, next_manager, _, _), \
                        patch.object(next_manager, 'get_engine', return_value=engine):
                    next_manager.save_chunks([row])
                    Path(next_manager.config_path).write_bytes(
                        Path(manager.config_path).read_bytes())
                    Path(next_manager.voice_config_path).write_bytes(
                        Path(manager.voice_config_path).read_bytes())
                    next_manager.invalidate_config_cache()
                    ok, message = generate(next_manager)
                    self.assertTrue(ok, message)
                    second_row = next_manager.load_chunks()[0]
                    fresh_bytes = (other / second_row['audio_path']).read_bytes()
                return outcome, first, calls, audio, fresh_bytes

    def test_route_timeline_batch_ignores_inactive_version_and_keeps_bytes(self):
        outcome, first, second, audio, fresh = self.run_case('route', 'batch')
        self.assertEqual(outcome, 'restored')
        self.assertEqual([item[0] for item in first], ['Ryan'] * 3)
        self.assertEqual([item[0] for item in second], ['Ryan'] * 3)
        self.assertEqual(second, first)
        self.assertEqual(fresh, audio)

    def test_route_timeline_alias_batch_ignores_inactive_version(self):
        outcome, first, second, audio, fresh = self.run_case('route_alias', 'batch')
        self.assertEqual(outcome, 'restored')
        self.assertEqual([item[0] for item in second], ['Ryan'] * 3)
        self.assertEqual(second, first)
        self.assertEqual(fresh, audio)

    def test_route_timeline_single_keeps_explicit_selection(self):
        outcome, first, second, audio, fresh = self.run_case('route', 'single')
        self.assertEqual(outcome, 'restored')
        self.assertEqual([item[0] for item in second], ['Ryan'] * 3)
        self.assertEqual(second, first)
        self.assertEqual(fresh, audio)

    def test_route_active_version_change_is_refused(self):
        outcome, first, second, audio, fresh = self.run_case('route_active', 'batch')
        self.assertIn('voice or seed selection', outcome)
        self.assertEqual([item[0] for item in second], ['Serena'] * 3)
        self.assertNotEqual(fresh, audio)

    def test_numbered_take_single_keeps_bound_seed(self):
        outcome, first, second, audio, fresh = self.run_case('numbered', 'single')
        self.assertEqual(outcome, 'restored')
        self.assertEqual([item[2] for item in first], [2000048] * 3)
        self.assertEqual([item[2] for item in second], [2000048] * 3)
        self.assertEqual(second, first)
        self.assertEqual(fresh, audio)

    def test_numbered_take_batch_keeps_bound_seed(self):
        outcome, first, second, audio, fresh = self.run_case('numbered', 'batch')
        self.assertEqual(outcome, 'restored')
        self.assertEqual([item[2] for item in first], [42] * 3)
        self.assertEqual([item[2] for item in second], [42] * 3)
        self.assertEqual(second, first)
        self.assertEqual(fresh, audio)

    def test_windowed_timeline_alias_admits_the_target_and_refuses_changed_target(self):
        for dispatch in ('single', 'batch'):
            with self.subTest(dispatch=dispatch):
                outcome, first, second, audio, fresh = self.run_case('route_alias_target', dispatch)
                self.assertIn('voice or seed selection', outcome)
                self.assertEqual(first, [('Serena', 'steady', 7)] * 3)
                self.assertEqual(second, [('Aiden', 'steady', 7)] * 3)
                self.assertTrue(fresh != audio, 'Changing the target did not change the WAV')

    def test_unsplit_timeline_aliases_match_their_canonical_provider_and_wav(self):
        case = restoration_cases.TakeRestorationTests()

        def render(direction, strategy, speaker, batch):
            with case.fixture() as (root, manager, _, _), case.owner():
                row = {**manager.load_chunks()[0], 'speaker': speaker, 'narrator_gender': 'male'}
                manager.save_chunks([row])
                self.save_timeline_alias(root, manager, row, direction, strategy)
                engine = route_contracts.get_engine()
                engine._device = 'cpu'
                calls = []

                def synth(**kwargs):
                    text, voice, instruct = (kwargs[field] for field in ('text', 'speaker', 'instruct'))
                    if isinstance(text, list):
                        text, voice, instruct = text[0], voice[0], instruct[0]
                    seed = torch.initial_seed()
                    calls.append((text, voice, instruct, seed))
                    amplitude = {'Ryan': .05, 'Serena': .12, 'Vivian': .31}[voice] + seed / 1000
                    return [np.sin(np.arange(2400) * .1) * amplitude], 24000

                engine._local_custom_model = SimpleNamespace(generate_custom_voice=synth)
                engine._init_local_custom = lambda: engine._local_custom_model
                with patch.object(manager, 'get_engine', return_value=engine):
                    if batch:
                        result = manager.generate_chunks_batch([0], batch_size=2)
                        self.assertEqual((result['completed'], result['failed']), ([0], []), result)
                    else:
                        ok, detail = manager.generate_chunk_audio(0)
                        self.assertTrue(ok, detail)
                saved = manager.load_chunks()[0]
                return calls, (root / saved['audio_path']).read_bytes()

        reference, audio = render('incoming', 'gender', 'NARRATOR', False)
        self.assertEqual([call[1:] for call in reference], [('Serena', 'steady', 7)])
        cases = [('incoming', 'gender', 'NARRATOR'), ('incoming', 'gender', 'READER')]
        cases.extend(('outgoing', strategy, speaker) for strategy in ('gender', 'focus')
                     for speaker in ('NARRATOR', 'READER'))
        for direction, strategy, speaker in cases:
            for batch in (False, True):
                with self.subTest(direction=direction, strategy=strategy, speaker=speaker, batch=batch):
                    calls, result = render(direction, strategy, speaker, batch)
                    self.assertEqual(calls, reference)
                    self.assertTrue(result == audio, 'Published WAV differs from the canonical render')

    def test_unsplit_route_ensemble_ignores_inactive_version_for_direct_and_alias_rows(self):
        case = restoration_cases.TakeRestorationTests()
        calls = []

        class Model:
            def generate_custom_voice(self, **kwargs):
                calls.append((kwargs['speaker'], kwargs['instruct'], torch.initial_seed()))
                amplitude = {'Ryan': .05, 'Serena': .12, 'Aiden': .25, 'Vivian': .33}
                return [np.sin(np.arange(2400) * .1) * amplitude[kwargs['speaker']]], 24000

        def render(speaker, batch, inactive_voice):
            with case.fixture() as (root, manager, _, _), case.owner():
                row = {**manager.load_chunks()[0], 'speaker': speaker, 'narrator_gender': 'male'}
                manager.save_chunks([row])
                script = root / 'annotated_script.json'
                script.write_text(json.dumps([{**row, 'speaker': 'NARRATOR'}]))
                base = {'type': 'ensemble', 'members': ['X', 'Y'], 'seed': 42,
                        'gender': 'female', 'narrator_strategy': 'gender'}
                solo = {'type': 'custom', 'seed': 42, 'gender': 'female'}
                Path(manager.voice_config_path).write_text(json.dumps({
                    'NARRATOR': base, 'X': {**solo, 'voice': 'Ryan'},
                    'Y': {**solo, 'voice': 'Serena'}, 'READER': {'alias_of': 'NARRATOR'}}))
                routes = route_contracts.voices
                with patch.multiple(routes, SCRIPT_PATH=str(script),
                                    VOICE_CONFIG_PATH=manager.voice_config_path,
                                    CHUNKS_PATH=manager.chunks_path, project_manager=manager):
                    route_contracts.save_version('NARRATOR', 'A', {**solo, 'voice': 'Aiden'})
                    route_contracts.save_version('NARRATOR', 'B', {})
                    route_contracts.select_at('NARRATOR', 'B', index=0)
                    asyncio.run(routes.save_narrator_strategy(
                        routes.NarratorStrategyRequest(strategy='global')))
                    if inactive_voice != 'Aiden':
                        route_contracts.save_version('NARRATOR', 'A', {**solo, 'voice': inactive_voice})
                saved = json.loads(Path(manager.voice_config_path).read_text())['NARRATOR']
                self.assertEqual(saved['version_timeline'][0]['from_uid'], row['uid'])
                self.assertEqual(saved['versions']['B']['narrator_strategy'], 'gender')
                self.assertEqual(saved['narrator_strategy'], 'global')
                engine = case.engine()
                engine._synthesis_windows = None
                engine._local_custom_model = Model()
                Path(manager.config_path).write_text(json.dumps({'tts': engine.get_model_settings()}))
                manager.invalidate_config_cache()
                calls.clear()
                with patch.object(manager, 'get_engine', return_value=engine):
                    if batch:
                        result = manager.generate_chunks_batch([0], batch_size=2, batch_seed=42)
                        self.assertEqual((result['completed'], result['failed']), ([0], []), result)
                    else:
                        ok, detail = manager.generate_chunk_audio(0)
                        self.assertTrue(ok, detail)
                saved_row = manager.load_chunks()[0]
                return list(calls), (root / saved_row['audio_path']).read_bytes()

        for speaker in ('NARRATOR', 'READER'):
            single = render(speaker, False, 'Aiden')
            self.assertEqual(single[0], [('Ryan', 'neutral', 42), ('Serena', 'neutral', 42)])
            for batch, inactive in ((True, 'Aiden'), (True, 'Vivian'), (False, 'Vivian')):
                with self.subTest(speaker=speaker, batch=batch, inactive=inactive):
                    requests, audio = render(speaker, batch, inactive)
                    self.assertEqual(requests, single[0])
                    self.assertTrue(audio == single[1], 'Published WAV differs from single render')

    def test_unsplit_fallback_rows_keep_native_batches_and_matching_voice_stays_distinct(self):
        case = restoration_cases.TakeRestorationTests()
        for strategy in ('focus', 'gender'):
            with self.subTest(strategy=strategy), case.fixture() as (root, manager, _, _), case.owner():
                calls = []

                def generate_custom_voice(**kwargs):
                    text = kwargs['text']
                    calls.append((text, kwargs['speaker'], kwargs['instruct'], torch.initial_seed()))
                    size = len(text) if isinstance(text, list) else 1
                    return [np.full(2400, .2, dtype='float32') for _ in range(size)], 24000

                engine = route_contracts.get_engine()
                engine._device = 'cpu'
                engine._local_custom_model = SimpleNamespace(generate_custom_voice=generate_custom_voice)
                engine._init_local_custom = lambda: engine._local_custom_model
                base = {'type': 'custom', 'voice': 'Ryan', 'seed': 42}
                alternate = {**base, 'voice': 'Serena', 'gender': 'female', 'seed': 7}
                voices = {'NARRATOR': {**base, 'narrator_strategy': strategy,
                                       'versions': {'A': alternate}}, 'X': alternate}
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                rows = [{'text': f'Synthetic row {i}.', 'speaker': 'NARRATOR',
                         'instruct': 'warm', 'narrator_gender': 'male', 'status': 'pending'}
                        for i in range(5)]
                rows[4].update(focus_speaker='X', narrator_gender='female')
                manager.save_chunks(rows)
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = manager.generate_chunks_batch(list(range(5)), batch_size=5)
                self.assertEqual(sorted(result['completed']), list(range(5)), result)
                self.assertEqual(result['failed'], [])
                for row in manager.load_chunks():
                    audio, rate = sf.read(root / row['audio_path'])
                    self.assertEqual((len(audio), rate), (2400, 24000))
                self.assertEqual(calls, [
                    ('Synthetic row 4.', 'Serena', 'warm', 7),
                    (['Synthetic row 0.', 'Synthetic row 1.'], ['Ryan', 'Ryan'], ['warm', 'warm'], 42),
                    (['Synthetic row 2.', 'Synthetic row 3.'], ['Ryan', 'Ryan'], ['warm', 'warm'], 42),
                ])


if __name__ == '__main__':
    unittest.main()
