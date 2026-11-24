"""Row-aware narrator resolution when the narrator is an ensemble member (#2170).

The synthetic provider is request-sensitive: it records the exact voice,
instruction and seed each native call receives, and returns PCM whose amplitude
encodes the requested voice. No real model, GPU, or app data is involved.
"""
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

from routers import voices as routes
from tts import (get_chunk_render_voice_selection, get_custom_voice_request,
                 resolve_narrator_voice_config)
from tests import test_take_restoration as restoration

AMPLITUDE = {'Ryan': .05, 'Serena': .12, 'Aiden': .25, 'Vivian': .33}


class NarratorEnsembleMemberTests(unittest.TestCase):
    def setUp(self):
        self.case = restoration.TakeRestorationTests()
        api = FastAPI()
        api.include_router(routes.router)
        self.client = TestClient(api)

    def request(self, path, **body):
        response = self.client.post(path, json=body)
        self.assertEqual(200, response.status_code, (path, response.text))
        return response.json()

    def install(self, root, manager, *, member='READER', active='B',
                narrator_strategy='gender', inactive_voice='Aiden'):
        row = dict(manager.load_chunks()[0], speaker='GROUP', narrator_gender='male')
        manager.save_chunks([row])
        script = root / 'annotated_script.json'
        script.write_text(json.dumps([dict(row, speaker='NARRATOR')]))
        solo = dict(type='custom', voice='Ryan', seed=42, gender='female',
                    narrator_strategy=narrator_strategy)
        config = {
            'GROUP': dict(type='ensemble', members=[member, 'Y'], seed=42),
            'NARRATOR': solo,
            'Y': dict(type='custom', voice='Serena', seed=7),
        }
        if member == 'READER':
            config['READER'] = {'alias_of': 'NARRATOR'}
        voice_path = Path(manager.voice_config_path)
        voice_path.write_text(json.dumps(config))
        with patch.multiple(routes, SCRIPT_PATH=str(script), VOICE_CONFIG_PATH=str(voice_path),
                            CHUNKS_PATH=manager.chunks_path, project_manager=manager):
            self.request('/api/voices/NARRATOR/versions', version_id='A',
                         config=dict(type='custom', voice='Aiden', gender='female', seed=7))
            self.request('/api/voices/NARRATOR/versions', version_id='B', config={})
            self.request('/api/voices/NARRATOR/version_timeline',
                         points=[dict(from_index=0, version_id=active)])
            self.request('/api/narrator/strategy', strategy='global')
            if inactive_voice != 'Aiden':
                self.request('/api/voices/NARRATOR/versions', version_id='A',
                             config=dict(type='custom', voice=inactive_voice,
                                         gender='female', seed=7))
        saved = json.loads(voice_path.read_text())
        self.assertEqual(row['uid'], saved['NARRATOR']['version_timeline'][0]['from_uid'])
        self.assertEqual('gender', saved['NARRATOR']['versions']['B']['narrator_strategy'])
        self.assertEqual('global', saved['NARRATOR']['narrator_strategy'])
        return voice_path

    def install_dynamic(self, root, manager, strategy, member):
        row = dict(manager.load_chunks()[0], speaker='GROUP', narrator_gender='male',
                   focus_speaker='LEAD', narrator_version='male_role')
        manager.save_chunks([row])
        (root / 'annotated_script.json').write_text(json.dumps([
            row, dict(row, speaker='NARRATOR'), dict(row, speaker='LEAD')]))
        config = {
            'NARRATOR': dict(type='custom', voice='Ryan', seed=42),
            'GROUP': dict(type='ensemble', members=[member, 'Y'], seed=42),
            'Y': dict(type='custom', voice='Serena', seed=13),
            'LEAD': dict(type='custom', voice='Vivian', seed=23),
        }
        if member == 'READER':
            config['READER'] = {'alias_of': 'NARRATOR'}
        voice_path = Path(manager.voice_config_path)
        voice_path.write_text(json.dumps(config))
        male = dict(type='custom', voice='Aiden', seed=7, gender='male')
        female = dict(type='custom', voice='Vivian', seed=23, gender='female')
        with self.routed(root, manager, voice_path):
            # An unordered engine fallback picks the first saved version. Gender
            # saves the female version first so that fallback is observably wrong.
            for version_id, config in ((('female_role', female), ('male_role', male))
                                       if strategy == 'gender' else
                                       (('male_role', male), ('female_role', female))):
                self.request('/api/voices/NARRATOR/versions', version_id=version_id, config=config)
            self.request('/api/narrator/strategy', strategy=strategy)
        return voice_path

    def render(self, root, manager, dispatch, *, batch_seed=-1, windows=None):
        engine = self.case.engine()
        if windows is None:
            engine._synthesis_windows = None
        engine._device = 'cpu'
        engine._custom_warmup_needed = False
        calls = []

        def synth(**kwargs):
            voice = kwargs['speaker']
            if isinstance(voice, list):
                voice = voice[0]
            calls.append((voice, kwargs['instruct'], torch.initial_seed()))
            return [np.sin(np.arange(2400) * .1) * AMPLITUDE[voice]], 24000

        engine._local_custom_model = SimpleNamespace(generate_custom_voice=synth)
        engine._init_local_custom = lambda: engine._local_custom_model
        Path(manager.config_path).write_text(json.dumps({'tts': engine.get_model_settings()}))
        manager.invalidate_config_cache()
        with patch.object(manager, 'get_engine', return_value=engine):
            if dispatch == 'batch':
                result = manager.generate_chunks_batch([0], batch_size=2, batch_seed=batch_seed)
                self.assertEqual(([0], []), (result['completed'], result['failed']), result)
            else:
                ok, detail = manager.generate_chunk_audio(0)
                self.assertTrue(ok, detail)
        published = manager.load_chunks()[0]
        path = root / published['audio_path']
        self.assertEqual((2400, 24000), (sf.info(path).frames, sf.info(path).samplerate))
        return calls, path.read_bytes()

    def change_inactive(self, root, manager, voice_path, voice):
        with self.routed(root, manager, voice_path):
            self.request('/api/voices/NARRATOR/versions', version_id='A',
                         config=dict(type='custom', voice=voice, gender='female', seed=7))

    def set_active(self, root, manager, voice_path, version):
        with self.routed(root, manager, voice_path):
            self.request('/api/voices/NARRATOR/version_timeline',
                         points=[dict(from_index=0, version_id=version)])

    def routed(self, root, manager, voice_path):
        return patch.multiple(routes, SCRIPT_PATH=str(root / 'annotated_script.json'),
                              VOICE_CONFIG_PATH=str(voice_path),
                              CHUNKS_PATH=manager.chunks_path, project_manager=manager)

    def test_alias_member_uses_active_version_and_ignores_inactive_change(self):
        expected = [('Ryan', 'neutral', 42), ('Serena', 'neutral', 7)]
        for dispatch in ('single', 'batch'):
            with self.subTest(dispatch=dispatch), self.case.fixture() as (root, manager, _, _), self.case.owner():
                voice_path = self.install(root, manager, member='READER', active='B')
                first, wav = self.render(root, manager, dispatch)
                self.assertEqual(expected, first)
                self.change_inactive(root, manager, voice_path, 'Vivian')
                second, again = self.render(root, manager, dispatch)
                self.assertEqual(expected, second)
                self.assertEqual(wav, again, 'Inactive narrator version changed the ensemble WAV')

    def test_direct_narrator_member_matches_alias_member(self):
        expected = [('Ryan', 'neutral', 42), ('Serena', 'neutral', 7)]
        for dispatch in ('single', 'batch'):
            with self.subTest(dispatch=dispatch), self.case.fixture() as (root, manager, _, _), self.case.owner():
                voice_path = self.install(root, manager, member='NARRATOR', active='B')
                first, wav = self.render(root, manager, dispatch)
                self.assertEqual(expected, first)
                self.change_inactive(root, manager, voice_path, 'Vivian')
                second, again = self.render(root, manager, dispatch)
                self.assertEqual(expected, second)
                self.assertEqual(wav, again)

    def test_active_version_controls_effective_voice_and_seed(self):
        for dispatch in ('single', 'batch'):
            with self.subTest(dispatch=dispatch), self.case.fixture() as (root, manager, _, _), self.case.owner():
                voice_path = self.install(root, manager, member='READER', active='A')
                calls, wav = self.render(root, manager, dispatch)
                self.assertEqual([('Aiden', 'neutral', 7), ('Serena', 'neutral', 7)], calls)
                self.set_active(root, manager, voice_path, 'B')
                baseline, baseline_wav = self.render(root, manager, dispatch)
                self.assertEqual([('Ryan', 'neutral', 42), ('Serena', 'neutral', 7)], baseline)
                self.assertNotEqual(baseline_wav, wav)

    def test_dynamic_strategy_matches_direct_narration_for_every_member(self):
        expected = {'gender': ('Aiden', 7), 'chapter': ('Aiden', 7), 'focus': ('Vivian', 23)}
        for strategy, (voice, seed) in expected.items():
            for member in ('NARRATOR', 'READER'):
                for dispatch in ('single', 'batch'):
                    with self.subTest(strategy=strategy, member=member, dispatch=dispatch), \
                            self.case.fixture() as (root, manager, _, _), self.case.owner():
                        self.install_dynamic(root, manager, strategy, member)
                        group, _ = self.render(root, manager, dispatch)
                        self.assertEqual((voice, 'neutral', seed), group[0])
                        manager.save_chunks([dict(manager.load_chunks()[0], speaker='NARRATOR')])
                        solo, _ = self.render(root, manager, dispatch)
                        self.assertEqual([(voice, 'neutral', seed)], solo)
                        self.assertEqual(group[0], solo[0],
                                         'Ensemble member lost the row narrator context')

    def test_focus_cycle_in_ensemble_member_is_refused_and_batch_settles(self):
        from contextlib import nullcontext
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                rows = [dict(uid=f'u-{i}', index=i, speaker=speaker,
                             text=f'Synthetic line {i}.', status='pending', focus_speaker='READER')
                        for i, speaker in enumerate(['READER', 'GROUP', 'STATIC_CYCLE', 'HEALTHY'])]
                manager.save_chunks(rows)
                (root / 'annotated_script.json').write_text(json.dumps([
                    dict(row, speaker='NARRATOR') for row in rows]))
                Path(manager.voice_config_path).write_text(json.dumps({
                    'NARRATOR': dict(type='custom', voice='Ryan', seed=42,
                                     narrator_strategy='focus'),
                    'READER': {'alias_of': 'NARRATOR'},
                    'GROUP': dict(type='ensemble', members=['READER', 'Y'], seed=42),
                    'STATIC_CYCLE': dict(type='ensemble', members=['A'], seed=42),
                    'HEALTHY': dict(type='ensemble', members=['Y'], seed=42),
                    'A': {'alias_of': 'B'}, 'B': {'alias_of': 'A'},
                    'Y': dict(type='custom', voice='Serena', seed=7)}))
                engine = self.case.engine()
                engine._synthesis_windows = None
                engine._device = 'cpu'

                def synth(**kwargs):
                    voices = kwargs['speaker']
                    count = len(voices) if isinstance(voices, list) else 1
                    return [np.full(2400, .2)] * count, 24000

                engine._local_custom_model = SimpleNamespace(generate_custom_voice=synth)
                engine._init_local_custom = lambda: engine._local_custom_model
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = manager.generate_chunks_batch([0, 1, 2, 3], batch_size=4,
                                                           batch_group_by_type=grouped)
                self.assertEqual([3], result['completed'])
                self.assertEqual(3, len(result['failed']))
                self.assertTrue(all('Alias cycle detected' in detail
                                    for _, detail in result['failed']))
                self.assertEqual(0, result['cancelled'])
                bad, member_row, static, good = manager.load_chunks()
                self.assertEqual(('error', None), (bad['status'], bad.get('audio_path')))
                self.assertEqual(('error', None), (member_row['status'], member_row.get('audio_path')))
                self.assertEqual(('error', None), (static['status'], static.get('audio_path')))
                self.assertEqual('done', good['status'])

    def test_real_take_receipt_stability_and_restore(self):
        from audio_take_history import AudioTakeHistory
        from book_state_transaction import get_book_operation_token
        from take_publication import KEY, validate_take_publication
        from take_restoration import capture_restore_target, restore_editor_take
        from tests.test_narrator_dispatch_identity import serving_identity

        for dispatch in ('single', 'batch'):
            for speaker in ('GROUP', 'READER'):
                with self.subTest(dispatch=dispatch, speaker=speaker), \
                        self.case.fixture() as (root, manager, _, fresh), self.case.owner():
                    voice_path = self.install(root, manager)
                    group_row = {**manager.load_chunks()[0], 'speaker': 'GROUP'}
                    voices = json.loads(voice_path.read_text())
                    _, materialized, _ = get_chunk_render_voice_selection(voices, 'GROUP', group_row, 0)
                    engine_view = resolve_narrator_voice_config('NARRATOR', materialized)
                    self.assertEqual(materialized['NARRATOR'], engine_view['NARRATOR'],
                                     'Engine re-resolved the narrator without the row context')
                    request = get_custom_voice_request(engine_view, 'NARRATOR', 'neutral')
                    self.assertEqual(('Ryan', 42), (request.voice, request.seed))
                    manager.save_chunks([dict(group_row, speaker=speaker)])
                    engine = self.case.engine()
                    engine._device = 'cpu'
                    calls = []

                    def native(**kwargs):
                        names = kwargs['speaker']
                        if not isinstance(names, list):
                            names = [names]
                        calls.extend(names)
                        return [np.sin(np.arange(2400) * .1) * AMPLITUDE[name] for name in names], 24000

                    engine._local_custom_model = SimpleNamespace(generate_custom_voice=native)
                    Path(manager.config_path).write_text(json.dumps({'tts': {
                        **engine.get_model_settings(),
                        'synthesis_recovery': engine._synthesis_recovery}}))
                    manager.invalidate_config_cache()

                    def generate():
                        calls.clear()
                        if dispatch == 'single':
                            ok, why = manager.generate_chunk_audio(0)
                            self.assertTrue(ok, why)
                        else:
                            result = manager.generate_chunks_batch([0], batch_size=2, batch_seed=-1)
                            self.assertEqual(([0], []), (result['completed'], result['failed']), result)
                        live = manager.load_chunks()[0]
                        return live, validate_take_publication(live), (root / live['audio_path']).read_bytes()

                    patches = (patch.object(manager, 'get_engine', return_value=engine),
                               patch('synthesis_recovery.capture_native_serving_identity',
                                     side_effect=serving_identity),
                               patch('synthesis_recovery_serving.capture_native_custom_serving_identity',
                                     side_effect=serving_identity))
                    with patches[0], patches[1], patches[2]:
                        live, proof, wav = generate()
                        if speaker == 'GROUP':
                            self.assertIsNone(proof, 'GROUP takes are audition-only')
                        else:
                            self.assertEqual(3, proof['version'])
                            digest = proof['dependencies']['voice_sha256']
                        manager.update_chunk(0, {'take': 3})
                        manager._publish_generated_chunk_audio(manager.load_chunks()[0], str(fresh), speaker)
                        current = manager.load_chunks()[0]
                        displaced = (root / current['audio_path']).read_bytes()
                        self.assertNotEqual(wav, displaced)
                        with AudioTakeHistory(root)._locked() as document:
                            saved = next(row for row in document['records']
                                         if row['audio_revision'] == live['audio_revision'])
                        self.assertEqual(saved['captured'].get(KEY), live.get(KEY))
                        token = get_book_operation_token(root)
                        request = SimpleNamespace(
                            expected_book_token=token, source_book_token=token,
                            expected_uid=current['uid'], expected_audio_revision=current['audio_revision'],
                            expected_audio_path=current['audio_path'], expected_take=current['take'],
                            version=saved['version'], sha256=saved['sha256'])
                        self.change_inactive(root, manager, voice_path, 'Vivian')
                        if speaker == 'GROUP':
                            with self.assertRaisesRegex(ValueError, 'no proven generation dependencies'):
                                restore_editor_take(manager, 0, request,
                                                    capture_restore_target(manager, 0, request))
                            self.assertEqual(displaced, (root / current['audio_path']).read_bytes())
                        else:
                            restored = restore_editor_take(
                                manager, 0, request, capture_restore_target(manager, 0, request))
                            self.assertEqual(wav, (root / restored['audio_path']).read_bytes())
                        _, after, after_wav = generate()
                        self.assertEqual(wav, after_wav)
                        if speaker == 'READER':
                            self.assertEqual(digest, after['dependencies']['voice_sha256'])
                            for voice, seed in (('Aiden', 42), ('Aiden', 43)):
                                with self.routed(root, manager, voice_path):
                                    self.request('/api/voices/NARRATOR/versions', version_id='B',
                                                 config=dict(type='custom', voice=voice, seed=seed,
                                                             gender='female', narrator_strategy='gender'))
                                _, changed, _ = generate()
                                if voice == 'Aiden' and seed == 42:
                                    voice_digest = changed['dependencies']['voice_sha256']
                                    self.assertNotEqual(digest, voice_digest)
                                else:
                                    self.assertNotEqual(voice_digest,
                                                        changed['dependencies']['voice_sha256'])

    def test_batching_keeps_native_grouping_for_unsplit_rows(self):
        with self.case.fixture() as (root, manager, _, _), self.case.owner():
            rows = [dict(manager.load_chunks()[0], index=index, uid=f'alias-{index}',
                         speaker='READER', narrator_gender='male', text=f'Unique line {index}.')
                    for index in range(4)]
            from tests.test_batch_style_timeline import get_engine as get_batch_engine
            self.install(root, manager, member='READER', active='B')
            manager.save_chunks(rows)
            engine = get_batch_engine()
            engine._synthesis_windows = None
            engine._device = 'cpu'
            Path(manager.config_path).write_text(json.dumps({'tts': engine.get_model_settings()}))
            manager.invalidate_config_cache()
            batches = []

            def synth(**kwargs):
                voices = kwargs['speaker']
                if not isinstance(voices, list):
                    voices = [voices]
                batches.append((len(voices), list(voices)))
                return [np.full(2400, .1)] * len(voices), 24000

            engine._local_custom_model = SimpleNamespace(generate_custom_voice=synth)
            engine._init_local_custom = lambda: engine._local_custom_model
            with patch.object(manager, 'get_engine', return_value=engine):
                result = manager.generate_chunks_batch([0, 1, 2, 3], batch_size=2, batch_seed=42)
                self.assertEqual([0, 1, 2, 3], sorted(result['completed']), result)
            self.assertEqual([2, 2], [size for size, _ in batches], batches)
            for _, voices in batches:
                self.assertEqual(['Ryan', 'Ryan'], voices)

    def test_cycle_still_settles_bad_row_without_aborting_healthy_row(self):
        from contextlib import nullcontext
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                reader = dict(uid='reader', speaker='READER', text='Synthetic reader.',
                              focus_speaker='READER', status='pending')
                healthy = dict(uid='group', speaker='GROUP', text='Healthy line.', status='pending')
                manager.save_chunks([reader, healthy])
                (root / 'annotated_script.json').write_text(json.dumps([
                    reader, healthy, dict(healthy, speaker='NARRATOR')]))
                voice = dict(type='custom', voice='Ryan', seed=42)
                Path(manager.voice_config_path).write_text(json.dumps({
                    'NARRATOR': {**voice, 'narrator_strategy': 'focus'},
                    'READER': {**voice, 'versions': {'B': {'type': 'custom', 'alias_of': 'NARRATOR'}},
                               'version_timeline': [{'from_index': 0, 'from_uid': 'reader', 'version_id': 'B'}]},
                    'GROUP': dict(type='ensemble', members=['Y', 'Y2'], seed=42),
                    'Y': voice, 'Y2': voice}))
                engine = self.case.engine()
                engine._device = 'cpu'
                engine._synthesis_windows = None
                calls = []

                def synth(**kwargs):
                    calls.append(kwargs['text'])
                    speaker = kwargs['speaker']
                    count = len(speaker) if isinstance(speaker, list) else 1
                    return [np.full(2400, .2)] * count, 24000

                engine._local_custom_model = SimpleNamespace(generate_custom_voice=synth)
                engine._init_local_custom = lambda: engine._local_custom_model
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = manager.generate_chunks_batch([0, 1], batch_size=2,
                                                           batch_group_by_type=grouped)
                self.assertEqual([1], result['completed'])
                self.assertEqual(1, len(result['failed']))
                self.assertEqual(0, result['failed'][0][0])
                self.assertIn('Alias cycle detected', result['failed'][0][1])
                self.assertEqual(0, result['cancelled'])
                bad, good = manager.load_chunks()
                self.assertEqual(('error', None), (bad['status'], bad.get('audio_path')))
                self.assertEqual('done', good['status'])
                self.assertTrue(calls)

    def _synthetic_engine(self, manager, calls):
        engine = self.case.engine()
        engine._device = 'cpu'
        engine._custom_warmup_needed = False

        def synth(**kwargs):
            names = kwargs['speaker']
            if not isinstance(names, list):
                names = [names]
            calls.extend((name, torch.initial_seed()) for name in names)
            return [np.sin(np.arange(2400) * .1) * AMPLITUDE[name] for name in names], 24000

        engine._local_custom_model = SimpleNamespace(generate_custom_voice=synth)
        Path(manager.config_path).write_text(json.dumps({'tts': {
            **engine.get_model_settings(), 'synthesis_recovery': engine._synthesis_recovery}}))
        manager.invalidate_config_cache()
        return engine

    def _generate_batch(self, manager, grouped, indices):
        return manager.generate_chunks_batch(indices, batch_size=max(2, len(indices)),
                                             batch_group_by_type=grouped)

    def test_mixed_narrator_spelling_binds_rendering_and_receipt(self):
        from audio_take_history import AudioTakeHistory
        from book_state_transaction import get_book_operation_token
        from take_publication import get_receipt_voice_digest, validate_take_publication
        from take_restoration import capture_restore_target, restore_editor_take
        from tests.test_narrator_dispatch_identity import serving_identity
        from tts import get_chunk_voice_inputs

        for dispatch in ('single', 'batch'):
            for speaker in ('READER', 'GROUP'):
                with self.subTest(dispatch=dispatch, speaker=speaker), \
                        self.case.fixture() as (root, manager, _, fresh), self.case.owner():
                    row = dict(manager.load_chunks()[0], speaker=speaker, narrator_gender='male')
                    manager.save_chunks([row])
                    basic = dict(type='custom', voice='Ryan', seed=42)
                    voices = {'NARRATOR': basic, 'Narrator': basic,
                              'READER': {'alias_of': 'Narrator'},
                              'Y': dict(type='custom', voice='Serena', seed=13),
                              'GROUP': dict(type='ensemble', members=['READER', 'Y'])}
                    voice_path = Path(manager.voice_config_path)
                    voice_path.write_text(json.dumps(voices))
                    (root / 'annotated_script.json').write_text(
                        json.dumps([dict(row, speaker=key) for key in voices]))
                    with self.routed(root, manager, voice_path):
                        self.request('/api/voices/NARRATOR/versions', version_id='F',
                                     config=dict(type='custom', voice='Vivian',
                                                 gender='female', seed=23))
                        if speaker == 'GROUP':
                            self.request('/api/voices/NARRATOR/versions', version_id='M',
                                         config=dict(type='custom', voice='Aiden',
                                                     gender='male', seed=7))
                        self.request('/api/narrator/strategy', strategy='gender')
                    expected = ('Aiden', 7) if speaker == 'GROUP' else ('Ryan', 42)
                    calls = []
                    engine = self._synthetic_engine(manager, calls)

                    def generate():
                        calls.clear()
                        if dispatch == 'single':
                            ok, why = manager.generate_chunk_audio(0)
                            self.assertTrue(ok, why)
                        else:
                            result = manager.generate_chunks_batch([0], batch_size=2, batch_seed=-1)
                            self.assertEqual(([0], []),
                                             (result['completed'], result['failed']), result)
                        live = manager.load_chunks()[0]
                        full = json.loads(voice_path.read_bytes())
                        selected = manager.get_chunk_render_voice_config(full, speaker, live, 0)['Narrator']
                        projected = get_chunk_voice_inputs(full, live, 0)['Narrator']
                        return (live, validate_take_publication(live),
                                (root / live['audio_path']).read_bytes(), selected, projected)

                    patches = (patch.object(manager, 'get_engine', return_value=engine),
                               patch('synthesis_recovery.capture_native_serving_identity',
                                     side_effect=serving_identity),
                               patch('synthesis_recovery_serving.capture_native_custom_serving_identity',
                                     side_effect=serving_identity))
                    with patches[0], patches[1], patches[2]:
                        live, proof, wav, selected, projected = generate()
                        self.assertEqual((expected[0], expected[1]),
                                         (projected['voice'], projected['seed']),
                                         'Projection disagrees with the selected narrator voice')
                        native = [(name, seed) for name, seed in calls if name == 'Narrator' or name == expected[0]]
                        self.assertTrue(native, calls)
                        self.assertTrue(all(seed == expected[1] for _, seed in native),
                                        f'Native request seed drifted from the receipt: {calls}')
                        self.assertIn(expected[0], [name for name, _ in calls])
                        self.assertNotIn('Vivian', [name for name, _ in calls],
                                         'Mixed spelling fell back to the first saved version')
                        if speaker == 'GROUP':
                            self.assertIsNone(proof, 'GROUP takes are audition-only')
                            continue
                        self.assertEqual(3, proof['version'])
                        digest = proof['dependencies']['voice_sha256']
                        self.assertEqual(get_receipt_voice_digest(3, selected, proof['seed']), digest,
                                         'Receipt was not bound to the rendered narrator identity')
                        manager.update_chunk(0, {'take': 3})
                        manager._publish_generated_chunk_audio(manager.load_chunks()[0], str(fresh), speaker)
                        current = manager.load_chunks()[0]
                        with AudioTakeHistory(root)._locked() as document:
                            saved = next(row for row in document['records']
                                         if row['audio_revision'] == live['audio_revision'])
                        token = get_book_operation_token(root)
                        request = SimpleNamespace(
                            expected_book_token=token, source_book_token=token,
                            expected_uid=current['uid'], expected_audio_revision=current['audio_revision'],
                            expected_audio_path=current['audio_path'], expected_take=current['take'],
                            version=saved['version'], sha256=saved['sha256'])
                        with self.routed(root, manager, voice_path):
                            self.request('/api/voices/NARRATOR/versions', version_id='F',
                                         config=dict(type='custom', voice='Serena',
                                                     gender='female', seed=23))
                        restored = restore_editor_take(manager, 0, request,
                                                       capture_restore_target(manager, 0, request))
                        self.assertEqual(wav, (root / restored['audio_path']).read_bytes(),
                                         'Unchanged-config restore disagreed with the rendered take')
                        _, after, after_wav, _, _ = generate()
                        self.assertEqual(wav, after_wav, 'Inactive spelling edit changed the take')

    def test_unconfigured_focus_under_character_gender_does_not_abort_rows(self):
        from contextlib import nullcontext
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                common = dict(type='custom', voice='Ryan', seed=42)
                voices = {'NARRATOR': dict(common),
                          'Y': {**common, 'voice': 'Serena', 'seed': 13},
                          'READER': {'alias_of': 'NARRATOR'},
                          'GROUP': {'type': 'ensemble', 'members': ['READER', 'Y']},
                          'HEALTHY': {'type': 'ensemble', 'members': ['Y']},
                          'FOCUS': None}
                voice_path = Path(manager.voice_config_path)
                voice_path.write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([{'speaker': key, 'text': 'Synthetic.'} for key in voices]))
                manager.save_chunks([])
                with self.routed(root, manager, voice_path):
                    self.request('/api/narrator/strategy', strategy='character_gender')
                rows = [dict(uid='u0', index=0, speaker='GROUP', status='pending',
                             text='Group line.', focus_speaker='FOCUS', narrator_gender='male'),
                        dict(uid='u1', index=1, speaker='HEALTHY', status='pending',
                             text='Healthy line.', focus_speaker='FOCUS', narrator_gender='male')]
                manager.save_chunks(rows)
                calls = []
                engine = self._synthetic_engine(manager, calls)
                engine._synthesis_windows = None
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = self._generate_batch(manager, grouped, [0, 1])
                self.assertEqual([0, 1], sorted(result['completed']), result)
                self.assertEqual([], result['failed'])
                self.assertEqual([('done', True), ('done', True)],
                                 [(row['status'], bool(row.get('audio_path')))
                                  for row in manager.load_chunks()])
                self.assertIn('Ryan', [name for name, _ in calls],
                              'Unconfigured focus did not fall back to the narrator voice')

    def test_dynamic_focus_does_not_override_saved_narrator_alias(self):
        for dispatch in ('single', 'batch'):
            for speaker in ('GROUP', 'NARRATOR', 'READER'):
                with self.subTest(dispatch=dispatch, speaker=speaker), \
                        self.case.fixture() as (root, manager, _, _), self.case.owner():
                    row = dict(manager.load_chunks()[0], speaker=speaker, focus_speaker='MASK',
                               narrator_gender='male')
                    manager.save_chunks([row])
                    voices = {'NARRATOR': dict(type='custom', voice='Ryan', seed=42),
                              'MASK': dict(type='custom', voice='Vivian', seed=23),
                              'Y': dict(type='custom', voice='Serena', seed=13),
                              'READER': {'alias_of': 'NARRATOR'},
                              'GROUP': {'type': 'ensemble', 'members': ['READER', 'Y']}}
                    voice_path = Path(manager.voice_config_path)
                    voice_path.write_text(json.dumps(voices))
                    (root / 'annotated_script.json').write_text(
                        json.dumps([dict(row, speaker=key) for key in voices]))
                    with self.routed(root, manager, voice_path):
                        snapshot = self.client.get('/api/voice_config/snapshot').json()
                        self.request('/api/voice_config/save', revision=snapshot['revision'],
                                     book_token=snapshot['book_token'],
                                     voices={'NARRATOR': {'alias_of': 'Y'}})
                        self.request('/api/narrator/strategy', strategy='focus')
                    calls, _ = self.render(root, manager, dispatch)
                    self.assertTrue(calls)
                    self.assertEqual({('Serena', 13)},
                                     {(name, seed) for name, _, seed in calls},
                                     f'{speaker} requested the focus voice instead of the saved alias')

    def test_focus_alias_target_timeline_matches_direct_and_group(self):
        for dispatch in ('single', 'batch'):
            for speaker in ('GROUP', 'NARRATOR', 'READER'):
                with self.subTest(dispatch=dispatch, speaker=speaker), \
                        self.case.fixture() as (root, manager, _, _), self.case.owner():
                    row = dict(manager.load_chunks()[0], speaker=speaker, focus_speaker='MASK',
                               narrator_gender='male')
                    manager.save_chunks([row])
                    voices = {'NARRATOR': dict(type='custom', voice='Ryan', seed=42),
                              'MASK': {'alias_of': 'TARGET'},
                              'TARGET': dict(type='custom', voice='Ryan', seed=42),
                              'READER': {'alias_of': 'NARRATOR'},
                              'GROUP': {'type': 'ensemble', 'members': ['READER', 'Y']},
                              'Y': dict(type='custom', voice='Serena', seed=13)}
                    voice_path = Path(manager.voice_config_path)
                    voice_path.write_text(json.dumps(voices))
                    (root / 'annotated_script.json').write_text(
                        json.dumps([dict(row, speaker=key) for key in voices]))
                    with self.routed(root, manager, voice_path):
                        self.request('/api/voices/TARGET/versions', version_id='late',
                                     config=dict(type='custom', voice='Aiden', seed=7))
                        self.request('/api/voices/TARGET/version_timeline',
                                     points=[dict(from_index=0, version_id='late')])
                        self.request('/api/narrator/strategy', strategy='focus')
                    calls, _ = self.render(root, manager, dispatch)
                    self.assertIn(('Aiden', 7), [(name, seed) for name, _, seed in calls],
                                  f'{speaker} lost the focus alias target timeline')
                    self.assertNotIn('Ryan', [name for name, _, _ in calls],
                                     f'{speaker} rendered the unversioned alias target')

    def test_aliased_entry_does_not_inspect_masked_members(self):
        from contextlib import nullcontext
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                row = dict(uid='u0', speaker='Y', text='First synthetic line.', status='pending')
                other = dict(uid='u1', speaker='HEALTHY', text='Second synthetic line.', status='pending')
                manager.save_chunks([row, other])
                voice_path = Path(manager.voice_config_path)
                voice = dict(type='custom', voice='Serena', seed=13)
                voice_path.write_text(json.dumps({
                    'MASKED': dict(type='custom', voice='Ryan', seed=42),
                    'Y': voice,
                    'HEALTHY': dict(type='ensemble', members=['Y'])}))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker='MASKED'), other]))
                with self.routed(root, manager, voice_path):
                    self.request('/api/voices/MASKED/versions', version_id='V',
                                 config={'alias_of': 'Y', 'type': 'ensemble', 'members': 1})
                    self.request('/api/voices/MASKED/version_timeline',
                                 points=[dict(from_index=0, version_id='V')])
                manager.save_chunks([dict(manager.load_chunks()[0], speaker='MASKED'),
                                     manager.load_chunks()[1]])
                calls = []
                engine = self._synthetic_engine(manager, calls)
                engine._synthesis_windows = None
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = self._generate_batch(manager, grouped, [0, 1])
                self.assertEqual([0, 1], sorted(result['completed']), result)
                self.assertEqual([('done', True), ('done', True)],
                                 [(row['status'], bool(row.get('audio_path')))
                                  for row in manager.load_chunks()])
                self.assertNotIn('Ryan', [name for name, _ in calls])

    def test_alias_selected_voice_skips_masked_narrator_member(self):
        from contextlib import nullcontext
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                row = dict(manager.load_chunks()[0], speaker='GROUP', focus_speaker='FOCUS',
                           narrator_gender='male')
                manager.save_chunks([row])
                voices = {'NARRATOR': dict(type='custom', voice='Ryan', seed=42,
                                           narrator_strategy='character_gender'),
                          'LEAD': dict(type='custom', voice='Aiden', seed=7),
                          'FOCUS': None,
                          'GROUP': {'type': 'ensemble', 'alias_of': 'LEAD',
                                    'members': ['NARRATOR']}}
                voice_path = Path(manager.voice_config_path)
                voice_path.write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for key in voices]))
                calls = []
                engine = self._synthetic_engine(manager, calls)
                engine._synthesis_windows = None
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = self._generate_batch(manager, grouped, [0])
                self.assertEqual([0], result['completed'], result)
                self.assertEqual([], result['failed'])
                self.assertEqual([('Aiden', 7)], [(name, seed) for name, seed in calls],
                                 'A masked narrator member was inspected or dispatched')

    def _mixed_spelling_voices(self):
        basic = dict(type='custom', voice='Ryan', seed=42)
        return {'NARRATOR': basic, 'Narrator': basic,
                'READER': {'alias_of': 'Narrator'},
                'MASK': dict(type='custom', voice='Vivian', seed=23),
                'Y': dict(type='custom', voice='Serena', seed=13),
                'GROUP': {'type': 'ensemble', 'members': ['READER', 'Y']}}

    def _save_mixed_alias(self, root, manager, voice_path, strategy):
        with self.routed(root, manager, voice_path):
            snapshot = self.client.get('/api/voice_config/snapshot').json()
            self.request('/api/voice_config/save', revision=snapshot['revision'],
                         book_token=snapshot['book_token'],
                         voices={'NARRATOR': {'alias_of': 'Y'}})
            self.request('/api/voices/NARRATOR/versions', version_id='female_role',
                         config=dict(type='custom', voice='Vivian', gender='female', seed=23))
            self.request('/api/voices/NARRATOR/versions', version_id='male_role',
                         config=dict(type='custom', voice='Aiden', gender='male', seed=7))
            self.request('/api/narrator/strategy', strategy=strategy)

    def test_mixed_spelling_saved_alias_precedence_across_dispatch(self):
        from tts import get_chunk_voice_inputs

        for strategy in ('gender', 'chapter', 'focus'):
            for speaker in ('NARRATOR', 'READER', 'GROUP'):
                for dispatch in ('single', 'batch'):
                    with self.subTest(strategy=strategy, speaker=speaker, dispatch=dispatch), \
                            self.case.fixture() as (root, manager, _, _), self.case.owner():
                        row = dict(manager.load_chunks()[0], speaker=speaker,
                                   narrator_gender='male', narrator_version='male_role',
                                   focus_speaker='MASK')
                        manager.save_chunks([row])
                        voices = self._mixed_spelling_voices()
                        voice_path = Path(manager.voice_config_path)
                        voice_path.write_text(json.dumps(voices))
                        (root / 'annotated_script.json').write_text(
                            json.dumps([dict(row, speaker=key) for key in voices]))
                        self._save_mixed_alias(root, manager, voice_path, strategy)
                        before = json.loads(voice_path.read_bytes())
                        calls, wav = self.render(root, manager, dispatch)
                        self.assertTrue(calls, calls)
                        self.assertEqual({('Serena', 13)},
                                         {(name, seed) for name, _, seed in calls},
                                         f'{speaker}/{strategy} ignored the saved narrator alias')
                        self.assertNotIn('Aiden', [name for name, _, _ in calls])
                        self.assertNotIn('Vivian', [name for name, _, _ in calls])
                        live = manager.load_chunks()[0]
                        full = json.loads(voice_path.read_bytes())
                        canonical, selected, _ = get_chunk_render_voice_selection(
                            full, speaker, live, 0)
                        projected = get_chunk_voice_inputs(full, live, 0)
                        if speaker == 'NARRATOR':
                            # The explicit alias keeps its canonical target rather
                            # than being flattened onto the spelling (#2170 R5).
                            self.assertEqual('Y', canonical)
                            self.assertEqual('Y', selected['NARRATOR']['alias_of'])
                        else:
                            self.assertEqual('Serena', selected['Narrator']['voice'])
                        self.assertEqual({('Serena', 13)},
                                         {(value.get('voice'), value.get('seed'))
                                          for value in projected.values()
                                          if value.get('voice')},
                                         'Projection disagrees with the rendered narrator alias')
                        self.assertEqual(before, json.loads(voice_path.read_bytes()),
                                         'Resolution mutated the saved voice config')
                        again, again_wav = self.render(root, manager, dispatch)
                        self.assertEqual(calls, again)
                        self.assertEqual(wav, again_wav, 'Re-resolution was not idempotent')

    def test_mixed_spelling_saved_alias_receipt_and_restore(self):
        from audio_take_history import AudioTakeHistory
        from book_state_transaction import get_book_operation_token
        from take_publication import get_receipt_voice_digest, validate_take_publication
        from take_restoration import capture_restore_target, restore_editor_take
        from tests.test_narrator_dispatch_identity import serving_identity
        from tts import get_chunk_voice_inputs

        for dispatch in ('single', 'batch'):
            with self.subTest(dispatch=dispatch), \
                    self.case.fixture() as (root, manager, _, fresh), self.case.owner():
                row = dict(manager.load_chunks()[0], speaker='READER', narrator_gender='male')
                manager.save_chunks([row])
                voices = self._mixed_spelling_voices()
                voice_path = Path(manager.voice_config_path)
                voice_path.write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for key in voices]))
                self._save_mixed_alias(root, manager, voice_path, 'gender')
                calls = []
                engine = self._synthetic_engine(manager, calls)

                def generate():
                    calls.clear()
                    if dispatch == 'single':
                        ok, why = manager.generate_chunk_audio(0)
                        self.assertTrue(ok, why)
                    else:
                        result = manager.generate_chunks_batch([0], batch_size=2, batch_seed=-1)
                        self.assertEqual(([0], []),
                                         (result['completed'], result['failed']), result)
                    live = manager.load_chunks()[0]
                    full = json.loads(voice_path.read_bytes())
                    selected = manager.get_chunk_render_voice_config(full, 'READER', live, 0)['Narrator']
                    projected = get_chunk_voice_inputs(full, live, 0)['Narrator']
                    return (live, validate_take_publication(live),
                            (root / live['audio_path']).read_bytes(), selected, projected)

                patches = (patch.object(manager, 'get_engine', return_value=engine),
                           patch('synthesis_recovery.capture_native_serving_identity',
                                 side_effect=serving_identity),
                           patch('synthesis_recovery_serving.capture_native_custom_serving_identity',
                                 side_effect=serving_identity))
                with patches[0], patches[1], patches[2]:
                    live, proof, wav, selected, projected = generate()
                    self.assertEqual(('Serena', 13), (projected['voice'], projected['seed']),
                                     'Projection disagrees with the saved alias target')
                    self.assertEqual({('Serena', 13)},
                                     {(name, seed) for name, seed in calls})
                    self.assertEqual(3, proof['version'])
                    digest = proof['dependencies']['voice_sha256']
                    self.assertEqual(get_receipt_voice_digest(3, selected, proof['seed']), digest,
                                     'Receipt was not bound to the aliased narrator identity')
                    manager.update_chunk(0, {'take': 3})
                    manager._publish_generated_chunk_audio(
                        manager.load_chunks()[0], str(fresh), 'READER')
                    current = manager.load_chunks()[0]
                    with AudioTakeHistory(root)._locked() as document:
                        saved = next(row for row in document['records']
                                     if row['audio_revision'] == live['audio_revision'])
                    token = get_book_operation_token(root)
                    request = SimpleNamespace(
                        expected_book_token=token, source_book_token=token,
                        expected_uid=current['uid'], expected_audio_revision=current['audio_revision'],
                        expected_audio_path=current['audio_path'], expected_take=current['take'],
                        version=saved['version'], sha256=saved['sha256'])
                    with self.routed(root, manager, voice_path):
                        self.request('/api/voices/NARRATOR/versions', version_id='female_role',
                                     config=dict(type='custom', voice='Vivian',
                                                 gender='female', seed=23))
                    restored = restore_editor_take(
                        manager, 0, request, capture_restore_target(manager, 0, request))
                    self.assertEqual(wav, (root / restored['audio_path']).read_bytes(),
                                     'Unchanged-config restore disagreed with the rendered take')
                    _, after, after_wav, _, _ = generate()
                    self.assertEqual(wav, after_wav, 'Inactive spelling edit changed the take')

    def test_saved_alias_target_timeline_across_reached_spellings(self):
        with self.case.fixture() as (root, manager, _, _), self.case.owner():
            rows = [dict(manager.load_chunks()[0], uid=f'u{index}', index=index,
                         speaker=speaker, text=f'Timeline line {index}.', status='pending')
                    for index, speaker in enumerate(('READER', 'GROUP', 'Narrator'))]
            manager.save_chunks(rows)
            basic = dict(type='custom', voice='Ryan', seed=42)
            voices = {'NARRATOR': basic, 'Narrator': basic,
                      'READER': {'alias_of': 'Narrator'},
                      'nArRaToR': dict(type='custom', voice='Ryan', seed=99),
                      'Y': dict(type='custom', voice='Serena', seed=13),
                      'GROUP': {'type': 'ensemble', 'members': ['READER', 'Y']}}
            voice_path = Path(manager.voice_config_path)
            voice_path.write_text(json.dumps(voices))
            (root / 'annotated_script.json').write_text(
                json.dumps([dict(row, speaker=key) for row in rows for key in voices]))
            with self.routed(root, manager, voice_path):
                snapshot = self.client.get('/api/voice_config/snapshot').json()
                self.request('/api/voice_config/save', revision=snapshot['revision'],
                             book_token=snapshot['book_token'],
                             voices={'NARRATOR': {'alias_of': 'Y'}})
                self.request('/api/voices/Y/versions', version_id='late',
                             config=dict(type='custom', voice='Aiden', seed=7))
                self.request('/api/voices/Y/version_timeline',
                             points=[dict(from_index=1, version_id='late')])
            expected = {
                'READER': {('Serena', 13)},
                'GROUP': {('Aiden', 7)},
                'Narrator': {('Aiden', 7)},
            }
            for index, speaker in enumerate(('READER', 'GROUP', 'Narrator')):
                calls, _ = self.render(root, manager, 'single') if index == 0 else self._render_index(
                    root, manager, index)
                names = {(name, seed) for name, _, seed in calls}
                self.assertEqual(expected[speaker], names,
                                 f'Timeline boundary or spelling disagreed at index {index}')

    def _render_index(self, root, manager, index):
        engine = self.case.engine()
        engine._synthesis_windows = None
        engine._device = 'cpu'
        engine._custom_warmup_needed = False
        calls = []

        def synth(**kwargs):
            voice = kwargs['speaker']
            if isinstance(voice, list):
                voice = voice[0]
            calls.append((voice, kwargs['instruct'], torch.initial_seed()))
            return [np.sin(np.arange(2400) * .1) * AMPLITUDE[voice]], 24000

        engine._local_custom_model = SimpleNamespace(generate_custom_voice=synth)
        engine._init_local_custom = lambda: engine._local_custom_model
        Path(manager.config_path).write_text(json.dumps({'tts': engine.get_model_settings()}))
        manager.invalidate_config_cache()
        with patch.object(manager, 'get_engine', return_value=engine):
            ok, detail = manager.generate_chunk_audio(index)
            self.assertTrue(ok, detail)
        return calls, (root / manager.load_chunks()[index]['audio_path']).read_bytes()

    def test_saved_alias_precedence_survives_recovery_settlement(self):
        for dispatch in ('single', 'batch'):
            with self.subTest(dispatch=dispatch), \
                    self.case.fixture() as (root, manager, _, _), self.case.owner():
                rows = [dict(uid='good', index=0, speaker='READER',
                             text='Good alias row.', status='pending'),
                        dict(uid='cycle', index=1, speaker='STATIC_CYCLE',
                             text='Broken cycle row.', status='pending')]
                manager.save_chunks(rows)
                basic = dict(type='custom', voice='Ryan', seed=42)
                voices = {'NARRATOR': basic, 'Narrator': basic,
                          'READER': {'alias_of': 'Narrator'},
                          'Y': dict(type='custom', voice='Serena', seed=13),
                          'GROUP': {'type': 'ensemble', 'members': ['READER', 'Y']},
                          'STATIC_CYCLE': {'type': 'ensemble', 'members': ['A']},
                          'A': {'alias_of': 'B'}, 'B': {'alias_of': 'A'}}
                voice_path = Path(manager.voice_config_path)
                seed_voices = {key: value for key, value in voices.items()
                               if key not in ('STATIC_CYCLE', 'A', 'B')}
                voice_path.write_text(json.dumps(seed_voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for row in rows for key in voices]))
                with self.routed(root, manager, voice_path):
                    snapshot = self.client.get('/api/voice_config/snapshot').json()
                    self.request('/api/voice_config/save', revision=snapshot['revision'],
                                 book_token=snapshot['book_token'],
                                 voices={'NARRATOR': {'alias_of': 'Y'}})
                saved_voices = json.loads(voice_path.read_bytes())
                saved_voices.update({'STATIC_CYCLE': voices['STATIC_CYCLE'],
                                     'A': voices['A'], 'B': voices['B']})
                voice_path.write_text(json.dumps(saved_voices))
                calls = []
                engine = self._synthetic_engine(manager, calls)
                with patch.object(manager, 'get_engine', return_value=engine):
                    if dispatch == 'single':
                        ok, why = manager.generate_chunk_audio(0)
                        self.assertTrue(ok, why)
                        bad, detail = manager.generate_chunk_audio(1)
                        self.assertFalse(bad)
                        self.assertIn('Alias cycle detected', str(detail))
                    else:
                        result = manager.generate_chunks_batch([0, 1], batch_size=2,
                                                               batch_seed=-1)
                        self.assertEqual([0], result['completed'])
                        self.assertEqual(1, len(result['failed']))
                        self.assertIn('Alias cycle detected', result['failed'][0][1])
                self.assertEqual({('Serena', 13)}, {(name, seed) for name, seed in calls})
                self.assertEqual(('done', 'error'),
                                 (manager.load_chunks()[0]['status'],
                                  manager.load_chunks()[1]['status']))

    def test_dict_members_version_resolves_narrator_like_direct(self):
        for dispatch in ('single', 'batch'):
            with self.subTest(dispatch=dispatch), \
                    self.case.fixture() as (root, manager, _, _), self.case.owner():
                row = dict(manager.load_chunks()[0], speaker='GROUP', narrator_gender='male')
                manager.save_chunks([row])
                basic = dict(type='custom', voice='Ryan', seed=42)
                voices = {'NARRATOR': basic, 'READER': {'alias_of': 'NARRATOR'},
                          'Y': dict(type='custom', voice='Serena', seed=13),
                          'GROUP': dict(type='custom', voice='Ryan', seed=42)}
                voice_path = Path(manager.voice_config_path)
                voice_path.write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for key in voices]))
                with self.routed(root, manager, voice_path):
                    self.request('/api/voices/GROUP/versions', version_id='V',
                                 config={'type': 'ensemble', 'members': {'READER': 1, 'Y': 2}})
                    self.request('/api/voices/GROUP/version_timeline',
                                 points=[dict(from_index=0, version_id='V')])
                    for version_id, config in (('female_role',
                                                dict(type='custom', voice='Vivian',
                                                     gender='female', seed=23)),
                                               ('male_role',
                                                dict(type='custom', voice='Aiden',
                                                     gender='male', seed=7))):
                        self.request('/api/voices/NARRATOR/versions',
                                     version_id=version_id, config=config)
                    self.request('/api/narrator/strategy', strategy='gender')
                saved = json.loads(voice_path.read_bytes())
                self.assertEqual({'READER': 1, 'Y': 2}, saved['GROUP']['versions']['V']['members'])
                group_calls, _ = self.render(root, manager, dispatch)
                self.assertEqual([('Aiden', 7), ('Serena', 13)],
                                 [(name, seed) for name, _, seed in group_calls])
                full = json.loads(voice_path.read_bytes())
                live = manager.load_chunks()[0]
                from tts import get_chunk_voice_inputs
                projected = get_chunk_voice_inputs(full, live, 0)
                self.assertEqual(('Aiden', 7),
                                 (projected['NARRATOR']['voice'], projected['NARRATOR']['seed']),
                                 'Projection did not follow the dict members dispatch accepts')
                self.assertEqual('NARRATOR', ''.join(
                    key for key in projected if key.upper() == 'NARRATOR'))
                self.assertEqual([(projected['NARRATOR']['voice'], projected['NARRATOR']['seed']),
                                  (projected['Y']['voice'], projected['Y']['seed'])],
                                 [(name, seed) for name, _, seed in group_calls],
                                 'Projection disagreed with the dispatched provider requests')
                manager.save_chunks([dict(live, speaker='NARRATOR')])
                direct_calls, _ = self.render(root, manager, dispatch)
                self.assertEqual([('Aiden', 7)], [(name, seed) for name, _, seed in direct_calls])
                self.assertEqual(direct_calls[0][:1] + direct_calls[0][2:],
                                 group_calls[0][:1] + group_calls[0][2:],
                                 'GROUP member disagreed with direct narration')

    def test_malformed_scalar_members_settle_as_configuration_error(self):
        from contextlib import nullcontext
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                rows = [dict(uid='bad', index=0, speaker='GROUP', text='Bad members.',
                             status='pending'),
                        dict(uid='good', index=1, speaker='HEALTHY', text='Healthy.',
                             status='pending')]
                manager.save_chunks(rows)
                voices = {'NARRATOR': dict(type='custom', voice='Ryan', seed=42),
                          'Y': dict(type='custom', voice='Serena', seed=13),
                          'HEALTHY': {'type': 'ensemble', 'members': ['Y']},
                          'GROUP': {'type': 'ensemble', 'members': 1}}
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for row in rows for key in voices]))
                calls = []
                engine = self._synthetic_engine(manager, calls)
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = manager.generate_chunks_batch([0, 1], batch_size=2,
                                                           batch_group_by_type=grouped)
                self.assertEqual([1], result['completed'], result)
                self.assertEqual(1, len(result['failed']))
                self.assertIn('no members configured', result['failed'][0][1])
                self.assertEqual(('error', 'done'),
                                 (manager.load_chunks()[0]['status'],
                                  manager.load_chunks()[1]['status']))
                self.assertEqual({('Serena', 13)}, {(name, seed) for name, seed in calls})

    def test_nested_ensemble_is_not_inspected(self):
        import tts
        from contextlib import nullcontext
        voices = {'GROUP': {'type': 'ensemble', 'members': ['DIRECT']},
                  'DIRECT': {'type': 'ensemble', 'members': ['NARRATOR', 'Narrator']},
                  'NARRATOR': dict(type='custom', voice='Ryan', seed=42,
                                   narrator_strategy='focus'),
                  'Narrator': dict(type='custom', voice='Ryan', seed=42,
                                   narrator_strategy='focus'),
                  'Y': dict(type='custom', voice='Serena', seed=13),
                  'HEALTHY': {'type': 'ensemble', 'members': ['Y']}}
        row = {'speaker': 'GROUP', 'text': 'Nested line.', 'focus_speaker': 'Y'}
        effective = tts.voice_config_for_chunk(voices, 'GROUP', 0)
        with patch.object(tts, 'resolve_narrator_voice_config',
                          wraps=tts.resolve_narrator_voice_config) as inspect:
            resolved, reached = tts.resolve_row_narrators(
                voices, effective, 'GROUP', row, 0)
        self.assertEqual(set(), set(reached),
                         'A narrator behind a rejected nested ensemble was reached')
        self.assertEqual(0, inspect.call_count,
                         'A descendant dispatch refuses before rendering was inspected')
        self.assertIs(effective, resolved)
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                manager.save_chunks([
                    dict(uid='nested', index=0, speaker='GROUP',
                         text='Nested line.', status='pending', focus_speaker='Y'),
                    dict(uid='good', index=1, speaker='HEALTHY',
                         text='Healthy line.', status='pending')])
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for row in manager.load_chunks()
                                for key in voices]))
                calls = []
                engine = self._synthetic_engine(manager, calls)
                original = tts.resolve_narrator_voice_config
                consulted = []

                def track(speaker, voice_config, chunk=None):
                    consulted.append(speaker)
                    return original(speaker, voice_config, chunk)

                with patch.object(tts, 'resolve_narrator_voice_config', side_effect=track), \
                        patch.object(manager, 'get_engine', return_value=engine):
                    result = manager.generate_chunks_batch([0, 1], batch_size=2,
                                                           batch_group_by_type=grouped)
                self.assertEqual([1], result['completed'], result)
                self.assertEqual(1, len(result['failed']))
                self.assertIn("is itself an ensemble", result['failed'][0][1])
                self.assertEqual([], [name for name in consulted
                                      if isinstance(name, str) and name.upper() == 'NARRATOR'],
                                 'Batch inspected a nested descendant it refuses to render')
                self.assertEqual(('error', 'done'),
                                 (manager.load_chunks()[0]['status'],
                                  manager.load_chunks()[1]['status']))

    def _default_alias_voices(self):
        basic = dict(type='custom', voice='Ryan', seed=42)
        return {'NARRATOR': dict(basic), 'Narrator': dict(basic),
                'READER': {'alias_of': 'Narrator'},
                'Y': dict(type='custom', voice='Serena', seed=13),
                'GROUP': {'type': 'ensemble', 'members': ['READER', 'Y']}}

    def test_default_global_saved_alias_native_batch_matches_single_and_projection(self):
        """F1: default/global must not leave native batch on the saved config."""
        from tts import get_chunk_voice_inputs

        for windows in (None, 'default'):
            for speaker in ('Narrator', 'READER', 'NARRATOR', 'GROUP'):
                outcomes = {}
                for dispatch in ('single', 'batch'):
                    with self.subTest(windows=windows, speaker=speaker, dispatch=dispatch), \
                            self.case.fixture() as (root, manager, _, _), self.case.owner():
                        row = dict(manager.load_chunks()[0], speaker=speaker,
                                   narrator_gender='female', text='Short line.')
                        manager.save_chunks([row])
                        voices = self._default_alias_voices()
                        voice_path = Path(manager.voice_config_path)
                        voice_path.write_text(json.dumps(voices))
                        (root / 'annotated_script.json').write_text(
                            json.dumps([dict(row, speaker=key) for key in voices]))
                        with self.routed(root, manager, voice_path):
                            snapshot = self.client.get('/api/voice_config/snapshot').json()
                            self.request('/api/voice_config/save',
                                         revision=snapshot['revision'],
                                         book_token=snapshot['book_token'],
                                         voices={'NARRATOR': {'alias_of': 'Y'}})
                        data = json.loads(voice_path.read_bytes())
                        canonical, resolved, native = get_chunk_render_voice_selection(
                            data, speaker, row, 0)
                        projected = get_chunk_voice_inputs(data, row, 0)
                        if speaker == 'NARRATOR':
                            # The configured alias keeps its canonical target, so
                            # the saved-config re-read is still this row (#2170 R5).
                            self.assertEqual('Y', canonical)
                            self.assertTrue(native,
                                            'Static narrator alias lost safe native batching')
                            self.assertEqual('Y', resolved['NARRATOR']['alias_of'])
                        elif speaker in ('Narrator', 'READER'):
                            # The sibling fallback renames the reached spelling, so
                            # its saved-config re-read is not this row.
                            self.assertEqual('Narrator', canonical)
                            self.assertFalse(native)
                            self.assertEqual('Serena', resolved['Narrator']['voice'])
                        self.assertEqual({('Serena', 13)},
                                         {(value.get('voice'), value.get('seed'))
                                          for value in projected.values()
                                          if value.get('voice')})
                        calls, wav = self.render(root, manager, dispatch, windows=windows)
                        self.assertEqual({'Serena'}, {name for name, _, _ in calls})
                        self.assertEqual({13}, {seed for _, _, seed in calls})
                        outcomes[dispatch] = ([(name, seed) for name, _, seed in calls], wav)
                with self.subTest(windows=windows, speaker=speaker, comparison=True):
                    self.assertEqual({'single', 'batch'}, set(outcomes),
                                     'A dispatch failed before the identity comparison')
                    self.assertEqual(outcomes['single'], outcomes['batch'],
                                     'Single and batch rendered different narrator identities')

    def test_unreached_configured_narrator_cycle_spares_healthy_rows_projection_and_saves(self):
        """F2: a cycle on the unreached configured spelling must not leak out."""
        from tts import VoiceAliasResolutionError, get_chunk_voice_inputs

        basic = dict(type='custom', voice='Ryan', seed=42)
        voices = {'NARRATOR': {'alias_of': 'A'}, 'A': {'alias_of': 'NARRATOR'},
                  'Narrator': dict(basic),
                  'Y': dict(type='custom', voice='Serena', seed=13),
                  'GROUP': {'type': 'ensemble', 'members': ['Narrator']},
                  'HEALTHY': {'type': 'ensemble', 'members': ['Y']}}
        for grouped in (True, False):
            with self.subTest(grouped=grouped), \
                    self.case.fixture() as (root, manager, _, _), self.case.owner():
                rows = [dict(uid='group', index=0, speaker='GROUP',
                             text='Group line.', status='pending'),
                        dict(uid='healthy', index=1, speaker='HEALTHY',
                             text='Healthy line.', status='pending')]
                manager.save_chunks(rows)
                voice_path = Path(manager.voice_config_path)
                voice_path.write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for row in rows for key in voices]))
                group_row = manager.load_chunks()[0]
                projected = get_chunk_voice_inputs(voices, group_row, 0)
                self.assertEqual(('Ryan', 42),
                                 (projected['Narrator']['voice'],
                                  projected['Narrator']['seed']))
                self.assertNotIn('NARRATOR', projected,
                                 'Unreached configured spelling was inspected')
                before_bytes = voice_path.read_bytes()
                with self.routed(root, manager, voice_path):
                    self.request('/api/voices/Y/versions', version_id='V',
                                 config=dict(type='custom', voice='Vivian', seed=23))
                saved = json.loads(voice_path.read_bytes())
                self.assertEqual({'alias_of': 'A'}, saved['NARRATOR'],
                                 'Unrelated save rewrote the malformed cycle')
                self.assertEqual({'alias_of': 'NARRATOR'}, saved['A'])
                self.assertEqual('Vivian', saved['Y']['versions']['V']['voice'])
                self.assertNotEqual(before_bytes, voice_path.read_bytes())
                calls = []
                engine = self._synthetic_engine(manager, calls)
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = self._generate_batch(manager, grouped, [0, 1])
                self.assertEqual([0, 1], sorted(result['completed']), result)
                self.assertEqual([], result['failed'])
                self.assertIn(('Ryan', 42), calls)
                self.assertIn(('Serena', 13), calls)
                self.assertEqual([('done', True), ('done', True)],
                                 [(row['status'], bool(row.get('audio_path')))
                                  for row in manager.load_chunks()])
        # A genuinely reached cycle is still attributed to its own row.
        with self.case.fixture() as (root, manager, _, _), self.case.owner():
            row = dict(manager.load_chunks()[0], speaker='NARRATOR')
            manager.save_chunks([row, dict(manager.load_chunks()[0], index=1, uid='good',
                                           speaker='HEALTHY', text='Healthy line.',
                                           status='pending')])
            voice_path = Path(manager.voice_config_path)
            voice_path.write_text(json.dumps(voices))
            (root / 'annotated_script.json').write_text(
                json.dumps([dict(row, speaker=key) for key in voices]))
            with self.assertRaisesRegex(VoiceAliasResolutionError, "NARRATOR"):
                get_chunk_voice_inputs(voices, manager.load_chunks()[0], 0)
            calls = []
            engine = self._synthetic_engine(manager, calls)
            with patch.object(manager, 'get_engine', return_value=engine):
                result = self._generate_batch(manager, True, [0, 1])
            self.assertEqual([1], result['completed'])
            self.assertEqual(1, len(result['failed']))
            self.assertIn("NARRATOR", result['failed'][0][1])
            self.assertEqual(('error', 'done'),
                             (manager.load_chunks()[0]['status'],
                              manager.load_chunks()[1]['status']))

    def test_unreached_configured_narrator_cycle_spares_each_reached_spelling_and_recovers(self):
        """F2: direct and aliased reached spellings stay healthy; healing works."""
        from tts import get_chunk_voice_inputs

        basic = dict(type='custom', voice='Ryan', seed=42)
        for speaker in ('Narrator', 'READER'):
            for dispatch in ('single', 'batch'):
                with self.subTest(speaker=speaker, dispatch=dispatch), \
                        self.case.fixture() as (root, manager, _, _), self.case.owner():
                    row = dict(manager.load_chunks()[0], speaker=speaker)
                    manager.save_chunks([row])
                    voices = {'NARRATOR': {'alias_of': 'A'}, 'A': {'alias_of': 'NARRATOR'},
                              'Narrator': dict(basic),
                              'READER': {'alias_of': 'Narrator'},
                              'Y': dict(type='custom', voice='Serena', seed=13)}
                    voice_path = Path(manager.voice_config_path)
                    voice_path.write_text(json.dumps(voices))
                    (root / 'annotated_script.json').write_text(
                        json.dumps([dict(row, speaker=key) for key in voices]))
                    live = manager.load_chunks()[0]
                    projected = get_chunk_voice_inputs(voices, live, 0)
                    reached = 'Narrator'
                    self.assertEqual(('Ryan', 42), (projected[reached]['voice'],
                                                    projected[reached]['seed']))
                    self.assertNotIn('NARRATOR', projected)
                    calls = []
                    engine = self._synthetic_engine(manager, calls)
                    with patch.object(manager, 'get_engine', return_value=engine):
                        if dispatch == 'single':
                            ok, why = manager.generate_chunk_audio(0)
                            self.assertTrue(ok, why)
                        else:
                            result = manager.generate_chunks_batch([0], batch_size=2,
                                                                   batch_seed=-1)
                            self.assertEqual(([0], []),
                                             (result['completed'], result['failed']), result)
                    self.assertIn(('Ryan', 42), calls)
                    # Recovery: a normal save that removes the cycle is accepted
                    # and the healed alias target is what the row now renders.
                    with self.routed(root, manager, voice_path):
                        snapshot = self.client.get('/api/voice_config/snapshot').json()
                        self.request('/api/voice_config/save',
                                     revision=snapshot['revision'],
                                     book_token=snapshot['book_token'],
                                     voices={'NARRATOR': {'alias_of': 'Y'}})
                    healed = json.loads(voice_path.read_bytes())
                    self.assertEqual({'alias_of': 'Y'}, healed['NARRATOR'])
                    live = manager.load_chunks()[0]
                    self.assertEqual(('Serena', 13),
                                     (get_chunk_voice_inputs(healed, live, 0)['Narrator']['voice'],
                                      get_chunk_voice_inputs(healed, live, 0)['Narrator']['seed']))
                    calls.clear()
                    with patch.object(manager, 'get_engine', return_value=engine):
                        if dispatch == 'single':
                            ok, why = manager.generate_chunk_audio(0)
                            self.assertTrue(ok, why)
                        else:
                            result = manager.generate_chunks_batch([0], batch_size=2,
                                                                   batch_seed=-1)
                            self.assertEqual(([0], []),
                                             (result['completed'], result['failed']), result)
                    self.assertEqual({('Serena', 13)},
                                     {(name, seed) for name, seed in calls})

    def test_default_global_alias_receipt_and_restore(self):
        """F1: the default/global windowed path binds and restores its receipt."""
        from audio_take_history import AudioTakeHistory
        from book_state_transaction import get_book_operation_token
        from take_publication import get_receipt_voice_digest, validate_take_publication
        from take_restoration import capture_restore_target, restore_editor_take
        from tests.test_narrator_dispatch_identity import serving_identity
        from tts import get_chunk_voice_inputs

        for dispatch in ('single', 'batch'):
            with self.subTest(dispatch=dispatch), \
                    self.case.fixture() as (root, manager, _, fresh), self.case.owner():
                row = dict(manager.load_chunks()[0], speaker='READER',
                           narrator_gender='female')
                manager.save_chunks([row])
                voices = self._default_alias_voices()
                voice_path = Path(manager.voice_config_path)
                voice_path.write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for key in voices]))
                with self.routed(root, manager, voice_path):
                    snapshot = self.client.get('/api/voice_config/snapshot').json()
                    self.request('/api/voice_config/save',
                                 revision=snapshot['revision'],
                                 book_token=snapshot['book_token'],
                                 voices={'NARRATOR': {'alias_of': 'Y'}})
                calls = []
                engine = self._synthetic_engine(manager, calls)

                def generate():
                    calls.clear()
                    if dispatch == 'single':
                        ok, why = manager.generate_chunk_audio(0)
                        self.assertTrue(ok, why)
                    else:
                        result = manager.generate_chunks_batch([0], batch_size=2,
                                                               batch_seed=-1)
                        self.assertEqual(([0], []),
                                         (result['completed'], result['failed']), result)
                    live = manager.load_chunks()[0]
                    return live, validate_take_publication(live), \
                        (root / live['audio_path']).read_bytes()

                patches = (patch.object(manager, 'get_engine', return_value=engine),
                           patch('synthesis_recovery.capture_native_serving_identity',
                                 side_effect=serving_identity),
                           patch('synthesis_recovery_serving.capture_native_custom_serving_identity',
                                 side_effect=serving_identity))
                with patches[0], patches[1], patches[2]:
                    live, proof, wav = generate()
                    full = json.loads(voice_path.read_bytes())
                    projected = get_chunk_voice_inputs(full, live, 0)
                    self.assertEqual(('Serena', 13),
                                     (projected['Narrator']['voice'],
                                      projected['Narrator']['seed']))
                    self.assertEqual({('Serena', 13)}, set(calls))
                    self.assertEqual(3, proof['version'])
                    _, selected, _ = get_chunk_render_voice_selection(
                        full, 'READER', live, 0)
                    self.assertEqual(
                        get_receipt_voice_digest(3, selected['Narrator'], proof['seed']),
                        proof['dependencies']['voice_sha256'])
                    manager.update_chunk(0, {'take': 3})
                    manager._publish_generated_chunk_audio(
                        manager.load_chunks()[0], str(fresh), 'READER')
                    current = manager.load_chunks()[0]
                    displaced = (root / current['audio_path']).read_bytes()
                    with AudioTakeHistory(root)._locked() as document:
                        saved = next(row for row in document['records']
                                     if row['audio_revision'] == live['audio_revision'])
                    token = get_book_operation_token(root)
                    request = SimpleNamespace(
                        expected_book_token=token, source_book_token=token,
                        expected_uid=current['uid'],
                        expected_audio_revision=current['audio_revision'],
                        expected_audio_path=current['audio_path'],
                        expected_take=current['take'],
                        version=saved['version'], sha256=saved['sha256'])
                    with self.routed(root, manager, voice_path):
                        self.request('/api/voices/NARRATOR/versions', version_id='I',
                                     config=dict(type='custom', voice='Vivian', seed=23,
                                                 gender='female'))
                    restored = restore_editor_take(
                        manager, 0, request, capture_restore_target(manager, 0, request))
                    self.assertEqual(wav, (root / restored['audio_path']).read_bytes(),
                                     'Default/global restore disagreed with the take')
                    self.assertNotEqual(wav, displaced)

    def test_distinct_narrator_spelling_aliases_are_not_silently_overwritten(self):
        """F3: a reached spelling keeps its own saved alias; NARRATOR keeps its."""
        from take_publication import get_receipt_voice_digest, validate_take_publication
        from tests.test_narrator_dispatch_identity import serving_identity
        from tts import get_chunk_voice_inputs

        voices = {'NARRATOR': {'alias_of': 'Y'},
                  'Narrator': {'alias_of': 'Z'},
                  'Y': dict(type='custom', voice='Serena', seed=13),
                  'Z': dict(type='custom', voice='Vivian', seed=23),
                  'Y2': dict(type='custom', voice='Aiden', seed=7),
                  'GROUP': {'type': 'ensemble', 'members': ['Narrator']}}
        expected = {'Narrator': ('Vivian', 23), 'GROUP': ('Vivian', 23),
                    'NARRATOR': ('Serena', 13)}
        for speaker in ('Narrator', 'GROUP', 'NARRATOR'):
            for dispatch in ('single', 'batch'):
                with self.subTest(speaker=speaker, dispatch=dispatch), \
                        self.case.fixture() as (root, manager, _, _), self.case.owner():
                    row = dict(manager.load_chunks()[0], speaker=speaker)
                    manager.save_chunks([row])
                    voice_path = Path(manager.voice_config_path)
                    voice_path.write_text(json.dumps(voices))
                    (root / 'annotated_script.json').write_text(
                        json.dumps([dict(row, speaker=key) for key in voices]))
                    before_bytes = voice_path.read_bytes()
                    calls = []
                    engine = self._synthetic_engine(manager, calls)

                    def generate():
                        calls.clear()
                        if dispatch == 'single':
                            ok, why = manager.generate_chunk_audio(0)
                            self.assertTrue(ok, why)
                        else:
                            result = manager.generate_chunks_batch(
                                [0], batch_size=2, batch_seed=-1)
                            self.assertEqual(([0], []),
                                             (result['completed'], result['failed']), result)
                        live = manager.load_chunks()[0]
                        return live, validate_take_publication(live), \
                            (root / live['audio_path']).read_bytes()

                    patches = (patch.object(manager, 'get_engine', return_value=engine),
                               patch('synthesis_recovery.capture_native_serving_identity',
                                     side_effect=serving_identity),
                               patch('synthesis_recovery_serving.capture_native_custom_serving_identity',
                                     side_effect=serving_identity))
                    with patches[0], patches[1], patches[2]:
                        live, proof, wav = generate()
                        projected = get_chunk_voice_inputs(
                            json.loads(voice_path.read_bytes()), live, 0)
                        self.assertEqual({expected[speaker]},
                                         {(value.get('voice'), value.get('seed'))
                                          for value in projected.values()
                                          if isinstance(value, dict) and value.get('voice')})
                        self.assertEqual({expected[speaker]},
                                         {(name, seed) for name, seed in calls})
                        self.assertEqual(before_bytes, voice_path.read_bytes(),
                                         'Resolution mutated the distinct saved aliases')
                        if speaker != 'GROUP':
                            self.assertEqual(3, proof['version'])
                            canonical, selected, _ = get_chunk_render_voice_selection(
                                json.loads(voice_path.read_bytes()), speaker, live, 0)
                            self.assertEqual(
                                get_receipt_voice_digest(3, selected[canonical], proof['seed']),
                                proof['dependencies']['voice_sha256'],
                                'Receipt did not bind the spelling own alias')
                        other = 'Narrator' if speaker == 'NARRATOR' else 'NARRATOR'
                        replacement = {'NARRATOR': {'alias_of': 'Y2'},
                                       'Narrator': {'alias_of': 'Y2'}}[other]
                        with self.routed(root, manager, voice_path):
                            snapshot = self.client.get('/api/voice_config/snapshot').json()
                            self.request('/api/voice_config/save',
                                         revision=snapshot['revision'],
                                         book_token=snapshot['book_token'],
                                         voices={other: replacement})
                        _, _, after_wav = generate()
                        self.assertEqual(wav, after_wav,
                                         'Editing the unreached spelling changed the take')

    def test_ensemble_member_shape_policy_is_shared_by_projection_and_dispatch(self):
        """F4: one member policy for list/dict/tuple/string/scalar and mixed."""
        import tts

        shapes = {
            'list': {'members': ['Y', 'NARRATOR']},
            'dict': {'members': {'READER': 1, 'Y': 2}},
            'tuple': {'members': ('Y', 'NARRATOR')},
            'str': {'members': 'Y'},
            'int': {'members': 1},
            'none': {'members': None},
            'empty': {'members': []},
            'set': {'members': {'Y'}},
            'mixed': {'members': ['Y', 1, None, 'NARRATOR']},
        }
        expected = {'list': ['Y', 'NARRATOR'], 'dict': ['READER', 'Y'],
                    'tuple': ['Y', 'NARRATOR'], 'str': ['Y'], 'int': [], 'none': [],
                    'empty': [], 'set': [], 'mixed': ['Y', 'NARRATOR']}
        for name, shape in shapes.items():
            with self.subTest(shape=name):
                self.assertEqual(expected[name], tts.get_ensemble_member_names(shape))
        basic = dict(type='custom', voice='Ryan', seed=42)
        for shape in ('list', 'dict', 'mixed'):
            with self.subTest(dispatch_shape=shape), \
                    self.case.fixture() as (root, manager, _, _), self.case.owner():
                rows = [dict(uid='group', index=0, speaker='GROUP',
                             text='Group line.', status='pending'),
                        dict(uid='healthy', index=1, speaker='HEALTHY',
                             text='Healthy line.', status='pending')]
                manager.save_chunks(rows)
                voices = {'NARRATOR': basic,
                          'READER': {'alias_of': 'NARRATOR'},
                          'Y': dict(type='custom', voice='Serena', seed=13),
                          'HEALTHY': {'type': 'ensemble', 'members': ['Y']},
                          'GROUP': {'type': 'ensemble', **shapes[shape]}}
                voice_path = Path(manager.voice_config_path)
                voice_path.write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for row in rows for key in voices]))
                from tts import get_chunk_voice_inputs
                group_row = manager.load_chunks()[0]
                members = tts.get_ensemble_member_names(voices['GROUP'])
                projected = get_chunk_voice_inputs(voices, group_row, 0)
                resolved_members = []
                for member in members:
                    resolved_members.append(tts.get_resolved_speaker(member, voices))
                self.assertEqual(set(resolved_members) | {'GROUP'}, set(projected),
                                 'Projection and the shared member policy disagree')
                calls = []
                engine = self._synthetic_engine(manager, calls)
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = self._generate_batch(manager, True, [0, 1])
                self.assertEqual([0, 1], sorted(result['completed']), result)
                self.assertEqual([], result['failed'])
                self.assertEqual({('Ryan', 42), ('Serena', 13)},
                                 {(name, seed) for name, seed in calls})

    def test_new_ensemble_member_entries_reject_partial_collections(self):
        """F4: new writes refuse a collection that runtime would truncate."""
        import tts

        validate_ensemble_members = getattr(tts, 'validate_ensemble_members', None)
        self.assertIsNotNone(validate_ensemble_members,
                             'No shared ensemble-member write validator')
        with self.assertRaises(ValueError):
            validate_ensemble_members({'Y': 1, 1: 'NARRATOR'})
        for bad in (['Y', 1],):
            with self.subTest(bad=bad), self.case.fixture() as (root, manager, _, _), \
                    self.case.owner():
                voices = {'Y': dict(type='custom', voice='Serena', seed=13),
                          'NARRATOR': dict(type='custom', voice='Ryan', seed=42),
                          'GROUP': {'type': 'ensemble', 'members': ['Y']}}
                voice_path = Path(manager.voice_config_path)
                voice_path.write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([{'speaker': 'GROUP', 'text': 'Synthetic.'}]))
                manager.save_chunks([])
                before = voice_path.read_bytes()
                with self.routed(root, manager, voice_path):
                    response = self.client.post(
                        '/api/voices/GROUP/versions',
                        json={'version_id': 'V', 'config': {'type': 'ensemble',
                                                            'members': bad}})
                    self.assertEqual(422, response.status_code, response.text)
                    self.assertIn('member', response.text.lower())
                    snapshot = self.client.get('/api/voice_config/snapshot').json()
                    response = self.client.post(
                        '/api/voice_config/save',
                        json={'revision': snapshot['revision'],
                              'book_token': snapshot['book_token'],
                              'voices': {'GROUP': {'type': 'ensemble',
                                                   'members': bad}}})
                    self.assertEqual(422, response.status_code, response.text)
                self.assertEqual(before, voice_path.read_bytes(),
                                 'A refused member collection must preserve bytes')
        with self.case.fixture() as (root, manager, _, _), self.case.owner():
            voices = {'Y': dict(type='custom', voice='Serena', seed=13),
                      'GROUP': {'type': 'ensemble', 'members': ['Y']}}
            voice_path = Path(manager.voice_config_path)
            voice_path.write_text(json.dumps(voices))
            (root / 'annotated_script.json').write_text(
                json.dumps([{'speaker': 'GROUP', 'text': 'Synthetic.'}]))
            manager.save_chunks([])
            with self.routed(root, manager, voice_path):
                self.request('/api/voices/GROUP/versions', version_id='V',
                             config={'type': 'ensemble',
                                     'members': {'Y': 1}})
                saved = json.loads(voice_path.read_bytes())
                self.assertEqual({'Y': 1}, saved['GROUP']['versions']['V']['members'])

    def test_legacy_non_dict_version_can_be_repaired_by_both_save_routes(self):
        """F5: member validation must not dereference a legacy version value."""
        repaired = {'type': 'custom', 'voice': 'Vivian', 'seed': 23}
        for legacy in ('legacy', ['x']):
            for route in ('version', 'full'):
                with self.subTest(legacy=legacy, route=route), \
                        self.case.fixture() as (root, manager, _, _), self.case.owner():
                    voices = {'Hero': {'type': 'custom', 'voice': 'Ryan',
                                       'versions': {'OLD': legacy}}}
                    voice_path = Path(manager.voice_config_path)
                    voice_path.write_text(json.dumps(voices))
                    (root / 'annotated_script.json').write_text(
                        json.dumps([{'speaker': 'Hero', 'text': 'Synthetic.'}]))
                    manager.save_chunks([])
                    with self.routed(root, manager, voice_path):
                        if route == 'version':
                            response = self.client.post(
                                '/api/voices/Hero/versions',
                                json={'version_id': 'OLD', 'config': repaired})
                        else:
                            snapshot = self.client.get('/api/voice_config/snapshot').json()
                            response = self.client.post(
                                '/api/voice_config/save',
                                json={'revision': snapshot['revision'],
                                      'book_token': snapshot['book_token'],
                                      'voices': {'Hero': {'versions': {'OLD': repaired}}}})
                    self.assertEqual(200, response.status_code, response.text)
                    saved = json.loads(voice_path.read_bytes())
                    stored = saved['Hero']['versions']['OLD']
                    self.assertIsInstance(stored, dict)
                    self.assertEqual(
                        repaired,
                        {key: stored.get(key) for key in ('type', 'voice', 'seed')})

    def test_narrator_derived_identities_and_audio_invalidation(self):
        """R3.6: no traversal cache exists; derived identities move only when used."""
        from tts import get_chunk_voice_inputs, get_pronunciation_render_context

        basic = dict(type='custom', voice='Ryan', seed=42)
        for grouped in (True, False):
            with self.subTest(grouped=grouped), \
                    self.case.fixture() as (root, manager, _, _), self.case.owner():
                rows = [dict(uid='group', index=0, speaker='GROUP',
                             text='Group line.', status='done',
                             audio_path='group.wav', voice_revision='rev-group'),
                        dict(uid='healthy', index=1, speaker='HEALTHY',
                             text='Healthy line.', status='done',
                             audio_path='healthy.wav', voice_revision='rev-healthy')]
                manager.save_chunks(rows)
                for audio in ('group.wav', 'healthy.wav'):
                    sf.write(root / audio, np.zeros(10), 24000)
                voices = {'NARRATOR': dict(basic), 'Narrator': dict(basic),
                          'READER': {'alias_of': 'Narrator'},
                          'Y': dict(type='custom', voice='Serena', seed=13),
                          'GROUP': {'type': 'ensemble', 'members': ['READER', 'Y']},
                          'HEALTHY': {'type': 'ensemble', 'members': ['Y']}}
                voice_path = Path(manager.voice_config_path)
                voice_path.write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for row in rows for key in voices]))
                before_group = get_pronunciation_render_context(
                    voices, rows[0], 0, {'mode': 'local', 'language': 'en'})
                before_healthy = get_pronunciation_render_context(
                    voices, rows[1], 1, {'mode': 'local', 'language': 'en'})
                with self.routed(root, manager, voice_path):
                    snapshot = self.client.get('/api/voice_config/snapshot').json()
                    self.request('/api/voice_config/save',
                                 revision=snapshot['revision'],
                                 book_token=snapshot['book_token'],
                                 voices={'NARRATOR': {'alias_of': 'Y'}})
                saved = json.loads(voice_path.read_bytes())
                after_group = get_pronunciation_render_context(
                    saved, rows[0], 0, {'mode': 'local', 'language': 'en'})
                after_healthy = get_pronunciation_render_context(
                    saved, rows[1], 1, {'mode': 'local', 'language': 'en'})
                self.assertNotEqual(before_group['voice_sha256'],
                                    after_group['voice_sha256'])
                self.assertEqual(before_healthy,
                                 after_healthy,
                                 'Unrelated ensemble derived identity drifted')
                group_after, healthy_after = manager.load_chunks()
                self.assertEqual(('pending', None), (group_after['status'],
                                                     group_after['audio_path']))
                self.assertNotEqual('rev-group', group_after['voice_revision'])
                self.assertEqual(('done', 'healthy.wav', 'rev-healthy'),
                                 (healthy_after['status'], healthy_after['audio_path'],
                                  healthy_after['voice_revision']))
                # Shape-only member edits leave the derived identity alone.
                list_shape = get_chunk_voice_inputs(
                    {**saved, 'GROUP': {'type': 'ensemble',
                                        'members': ['READER', 'Y']}}, rows[0], 0)
                dict_shape = get_chunk_voice_inputs(
                    {**saved, 'GROUP': {'type': 'ensemble',
                                        'members': {'READER': 1, 'Y': 2}}}, rows[0], 0)
                self.assertEqual({key: value for key, value in list_shape.items()
                                  if key != 'GROUP'},
                                 {key: value for key, value in dict_shape.items()
                                  if key != 'GROUP'},
                                 'Member-shape edit changed reachable member inputs')
                # Inactive version edits invalidate nothing; active ones do.
                with self.routed(root, manager, voice_path):
                    snapshot = self.client.get('/api/voice_config/snapshot').json()
                    self.request('/api/voice_config/save',
                                 revision=snapshot['revision'],
                                 book_token=snapshot['book_token'],
                                 voices={'NARRATOR': {'alias_of': None}})
                def mark_done(prefix):
                    for audio in ('group.wav', 'healthy.wav'):
                        sf.write(root / audio, np.zeros(10), 24000)
                    manager.save_chunks([dict(row, status='done',
                                              audio_path=f'{row["uid"]}.wav',
                                              voice_revision=f'{prefix}-{row["uid"]}')
                                         for row in manager.load_chunks()])
                mark_done('rev2')
                with self.routed(root, manager, voice_path):
                    self.request('/api/voices/Narrator/versions', version_id='A',
                                 config=dict(type='custom', voice='Aiden', seed=7))
                    self.request('/api/voices/Narrator/versions', version_id='B',
                                 config=dict(type='custom', voice='Vivian', seed=23))
                    self.request('/api/voices/Narrator/version_timeline',
                                 points=[dict(from_index=0, version_id='A')])
                mark_done('rev3')
                with self.routed(root, manager, voice_path):
                    self.request('/api/voices/Narrator/versions', version_id='B',
                                 config=dict(type='custom', voice='Serena', seed=13))
                self.assertEqual(['done', 'done'],
                                 [row['status'] for row in manager.load_chunks()],
                                 'Inactive narrator version edit invalidated audio')
                with self.routed(root, manager, voice_path):
                    self.request('/api/voices/Narrator/versions', version_id='A',
                                 config=dict(type='custom', voice='Vivian', seed=23))
                group_timeline, healthy_timeline = manager.load_chunks()
                self.assertEqual(('pending', None),
                                 (group_timeline['status'], group_timeline['audio_path']))
                self.assertEqual('done', healthy_timeline['status'])
                self.assertNotEqual('rev3-group', group_timeline['voice_revision'])

    def test_timeline_walk_skips_masked_and_nested_members(self):
        """R1: an alias mask or a rejected nested ensemble hides its members."""
        from tts import get_chunk_voice_inputs, voice_config_for_chunk

        bad = {'type': 'custom', 'voice': 'Ryan', 'seed': 42,
               'versions': {'bad': {'character_style': 5}},
               'version_timeline': [{'from_index': 0, 'version_id': 'bad'}]}
        y = dict(type='custom', voice='Serena', seed=13)
        masked = {'ROOT': {'type': 'ensemble', 'alias_of': 'Y', 'members': {'META': 1}},
                  'Y': y, 'META': bad}
        effective = voice_config_for_chunk(masked, 'ROOT', 0)
        self.assertNotEqual(5, effective.get('META', {}).get('character_style'),
                            'The walker materialized a member hidden behind an alias')
        self.assertEqual({'Y'}, set(get_chunk_voice_inputs(
            masked, {'speaker': 'ROOT', 'text': 'x'}, 0)))
        nested = {'ROOT': {'type': 'ensemble', 'members': ['INNER']},
                  'INNER': {'type': 'ensemble', 'members': {'META': 1}},
                  'META': bad, 'Y': y}
        effective = voice_config_for_chunk(nested, 'ROOT', 0)
        self.assertNotEqual(5, effective.get('META', {}).get('character_style'),
                            'The walker descended into a rejected nested ensemble')
        self.assertEqual({'ROOT', 'INNER'}, set(get_chunk_voice_inputs(
            nested, {'speaker': 'ROOT', 'text': 'x'}, 0)))

    def test_malformed_optional_sibling_target_does_not_abort_healthy_rows(self):
        """R2: a malformed optional fallback is unavailable, not fatal."""
        from contextlib import nullcontext
        from tts import (get_chunk_render_voice_selection, get_resolved_speaker,
                         VoiceAliasResolutionError)

        voices = {'NARRATOR': {'alias_of': 'LEGACY'}, 'LEGACY': 'old',
                  'Narrator': dict(type='custom', voice='Aiden', seed=7),
                  'READER': {'alias_of': 'Narrator'},
                  'GROUP': {'type': 'ensemble', 'members': ['READER']},
                  'Y': dict(type='custom', voice='Serena', seed=13),
                  'HEALTHY': {'type': 'ensemble', 'members': ['Y']}}
        row = {'speaker': 'GROUP', 'text': 'Group line.', 'index': 0}
        canonical, resolved, _ = get_chunk_render_voice_selection(voices, 'GROUP', row, 0)
        self.assertEqual('GROUP', canonical)
        self.assertEqual(('Aiden', 7),
                         (resolved['Narrator']['voice'], resolved['Narrator']['seed']),
                         'A malformed unreached sibling aborted a healthy group')
        with self.assertRaises(VoiceAliasResolutionError):
            get_resolved_speaker('LEGACY', voices)
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                manager.save_chunks([
                    dict(uid='group', index=0, speaker='GROUP', text='Group line.', status='pending'),
                    dict(uid='good', index=1, speaker='HEALTHY', text='Healthy line.', status='pending')])
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([dict(row, speaker=key) for key in voices]))
                calls = []
                engine = self._synthetic_engine(manager, calls)
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = self._generate_batch(manager, grouped, [0, 1])
                self.assertEqual([0, 1], sorted(result['completed']), result)
                self.assertEqual([], result['failed'])

    def test_active_version_alias_precedes_sibling_fallback(self):
        """R3: the reached spelling's active-version alias wins, and its cycle fails."""
        from tts import (get_chunk_render_voice_selection, get_custom_voice_request,
                         VoiceAliasResolutionError)

        voices = {'NARRATOR': {'alias_of': 'Y'},
                  'Y': dict(type='custom', voice='Serena', seed=13),
                  'Narrator': {'type': 'custom', 'voice': 'Ryan', 'seed': 42,
                               'versions': {'V': {'type': 'custom', 'alias_of': 'Z'}},
                               'version_timeline': [{'from_index': 0, 'version_id': 'V'}]},
                  'Z': dict(type='custom', voice='Vivian', seed=23),
                  'READER': {'alias_of': 'Narrator'},
                  'GROUP': {'type': 'ensemble', 'members': ['READER']}}
        for speaker in ('Narrator', 'READER', 'GROUP'):
            row = {'speaker': speaker, 'text': 'Line.', 'index': 0}
            canonical, resolved, _ = get_chunk_render_voice_selection(voices, speaker, row, 0)
            key = 'Z' if speaker == 'GROUP' else canonical
            request = get_custom_voice_request(resolved, key, 'neutral')
            self.assertEqual(('Vivian', 23), (request.voice, request.seed), speaker)
        canonical, resolved, _ = get_chunk_render_voice_selection(
            voices, 'NARRATOR', {'speaker': 'NARRATOR', 'text': 'Line.', 'index': 0}, 0)
        self.assertEqual('Y', canonical)
        self.assertEqual('Serena', resolved['Y']['voice'])
        cycled = json.loads(json.dumps(voices))
        cycled['Z'] = {'alias_of': 'Narrator'}
        with self.assertRaises(VoiceAliasResolutionError):
            get_chunk_render_voice_selection(
                cycled, 'Narrator', {'speaker': 'Narrator', 'text': 'Line.', 'index': 0}, 0)

    def test_native_eligibility_distinguishes_absent_and_null_voice(self):
        """R4: absent and explicit null are different provider requests."""
        from tts import get_chunk_render_voice_selection, get_custom_voice_request, voice_config_for_chunk

        voices = {'NARRATOR': {'alias_of': 'Y'},
                  'Y': {'type': 'custom', 'seed': '13',
                        'versions': {'V': {'type': 'custom', 'seed': '13'}},
                        'version_timeline': [{'from_index': 0, 'version_id': 'V'}]},
                  'Narrator': {'type': 'custom', 'seed': '13',
                               'versions': {'V': {'type': 'custom', 'voice': None, 'seed': '13'}},
                               'version_timeline': [{'from_index': 0, 'version_id': 'V'}]}}
        row = {'speaker': 'Narrator', 'text': 'Line.', 'index': 0}
        canonical, resolved, native = get_chunk_render_voice_selection(voices, 'Narrator', row, 0)
        self.assertFalse(native, 'Native re-read would send a different request')
        request = get_custom_voice_request(resolved, canonical, 'neutral')
        self.assertEqual(('Ryan', 13), (request.voice, request.seed))
        saved = voice_config_for_chunk(voices, 'Narrator', 0)['Narrator']
        self.assertIn('voice', saved)
        self.assertIsNone(saved['voice'])

    def test_explicit_alias_keeps_canonical_pronunciation_target(self):
        """R5: an explicit alias keeps its target identity and safe native batch."""
        from tts import get_chunk_render_voice_selection, get_chunk_voice_inputs

        voices = {'NARRATOR': {'type': 'custom', 'voice': 'Ryan', 'seed': 42,
                               'alias_of': 'Y'},
                  'Y': dict(type='custom', voice='Serena', seed=13)}
        row = {'speaker': 'NARRATOR', 'text': 'Ann.', 'index': 0}
        canonical, resolved, native = get_chunk_render_voice_selection(voices, 'NARRATOR', row, 0)
        self.assertEqual('Y', canonical)
        self.assertTrue(native, 'A static explicit alias lost native batching')
        self.assertEqual('Y', resolved['NARRATOR']['alias_of'])
        self.assertEqual({'Y'}, set(get_chunk_voice_inputs(voices, row, 0)),
                         'Projection was re-keyed away from the canonical target')

    def test_fallback_ensemble_materializes_member_overlays(self):
        """R6: a fallback onto an ensemble keeps its members' active overlays."""
        from tts import get_chunk_render_voice_selection

        voices = {'NARRATOR': {'alias_of': 'K'},
                  'Narrator': dict(type='custom', voice='Ryan', seed=42),
                  'READER': {'alias_of': 'Narrator'},
                  'K': {'type': 'ensemble', 'members': ['Z']},
                  'Z': {'type': 'custom', 'voice': 'Ryan', 'seed': 42,
                        'versions': {'V': {'type': 'custom', 'voice': 'Aiden', 'seed': 7}},
                        'version_timeline': [{'from_index': 0, 'version_id': 'V'}]}}
        for speaker in ('Narrator', 'READER', 'NARRATOR', 'K'):
            row = {'speaker': speaker, 'text': 'Line.', 'index': 0}
            canonical, resolved, _ = get_chunk_render_voice_selection(voices, speaker, row, 0)
            self.assertEqual(('Aiden', 7),
                             (resolved['Z']['voice'], resolved['Z']['seed']), speaker)
        group = json.loads(json.dumps(voices))
        group['GROUP'] = {'type': 'ensemble', 'members': ['Narrator']}
        canonical, resolved, _ = get_chunk_render_voice_selection(
            group, 'GROUP', {'speaker': 'GROUP', 'text': 'Line.', 'index': 0}, 0)
        self.assertEqual('custom', resolved['Narrator'].get('type'),
                         'A flat cast member was converted into a nested ensemble')

    def test_reached_spellings_resolve_deterministically(self):
        """R7: member order and process hash order cannot change the decision."""
        from tts import get_chunk_render_voice_selection

        cast = {'NARRATOR': {'type': 'custom', 'voice': 'Ryan', 'seed': 42,
                             'narrator_strategy': 'focus'},
                'Narrator': {'type': 'custom', 'voice': 'Ryan', 'seed': 42,
                             'versions': {'V': {'type': 'custom', 'voice': 'Vivian', 'seed': 23}},
                             'version_timeline': [{'from_index': 0, 'version_id': 'V'}]},
                'FOCUS': {'alias_of': 'Narrator'},
                'GROUP': {'type': 'ensemble', 'members': ['NARRATOR', 'Narrator']}}
        row = {'speaker': 'GROUP', 'text': 'Line.', 'index': 0, 'focus_speaker': 'FOCUS'}
        decisions = []
        for members in (['NARRATOR', 'Narrator'], ['Narrator', 'NARRATOR']):
            variant = json.loads(json.dumps(cast))
            variant['GROUP']['members'] = members
            canonical, resolved, _ = get_chunk_render_voice_selection(variant, 'GROUP', row, 0)
            decisions.append((canonical,
                              resolved['NARRATOR'].get('voice'),
                              resolved['Narrator'].get('voice')))
        self.assertEqual(decisions[0], decisions[1])
        self.assertEqual('Vivian', decisions[0][1])
        self.assertEqual('Vivian', decisions[0][2])

    def test_dynamic_narrator_member_without_own_entry_matches_direct(self):
        """R8: a dynamic narrator spelling need not have a separate saved entry."""
        expected = {'gender': ('Aiden', 7), 'chapter': ('Aiden', 7),
                    'focus': ('Vivian', 23)}
        for strategy, (voice, seed) in expected.items():
            for dispatch in ('single', 'batch'):
                with self.subTest(strategy=strategy, dispatch=dispatch), \
                        self.case.fixture() as (root, manager, _, _), self.case.owner():
                    self.install_dynamic(root, manager, strategy, 'Narrator')
                    saved = json.loads(Path(manager.voice_config_path).read_text())
                    self.assertNotIn('Narrator', saved)
                    group, _ = self.render(root, manager, dispatch)
                    self.assertEqual((voice, 'neutral', seed), group[0])
                    manager.save_chunks([dict(manager.load_chunks()[0], speaker='Narrator')])
                    solo, _ = self.render(root, manager, dispatch)
                    self.assertEqual([(voice, 'neutral', seed)], solo)
                    self.assertEqual(group[0], solo[0],
                                     'Member spelling without an entry lost the row context')

    def test_malformed_configured_narrator_is_an_unavailable_fallback(self):
        """N1: every saved-config read is fuzzed with wrong types.

        A sibling row that only reaches 'Narrator' must not abort on a
        malformed configured 'NARRATOR', and the healthy member keeps its own
        voice. Each row of the table is one persisted field shape.
        """
        from tts import get_chunk_render_voice_selection, get_chunk_voice_inputs

        healthy = dict(type='custom', voice='Aiden', seed=7)
        shapes = {
            'scalar_int': (5, {}),
            'scalar_str': ('old', {}),
            'scalar_bool': (True, {}),
            'list': (['x'], {}),
            'null': (None, {}),
            'empty_str': ('', {}),
            'dict': ({'x': 1}, {}),
            'strategy_int': ({**healthy, 'narrator_strategy': 7}, {}),
            'strategy_bool': ({**healthy, 'narrator_strategy': True}, {}),
            'strategy_list': ({**healthy, 'narrator_strategy': ['focus']}, {}),
            'strategy_dict': ({**healthy, 'narrator_strategy': {'x': 1}}, {}),
            'strategy_null': ({**healthy, 'narrator_strategy': None}, {}),
            'versions_int': ({**healthy, 'narrator_strategy': 'chapter', 'versions': 5},
                             {'narrator_version': 'V'}),
            'versions_list': ({**healthy, 'narrator_strategy': 'chapter', 'versions': ['x']},
                              {'narrator_version': 'V'}),
            'versions_str': ({**healthy, 'narrator_strategy': 'chapter', 'versions': 'x'},
                             {'narrator_version': 'V'}),
            'gender_versions_list': ({**healthy, 'narrator_strategy': 'gender',
                                      'versions': ['x']}, {'narrator_gender': 'male'}),
            'versions_dict_bad': ({**healthy, 'narrator_strategy': 'chapter',
                                   'versions': {'V': 'x'}}, {'narrator_version': 'V'}),
            'versions_dict_nested': ({**healthy, 'narrator_strategy': 'chapter',
                                      'versions': {'V': ['x']}}, {'narrator_version': 'V'}),
            'nested_strategy': ({**healthy, 'narrator_strategy': {'x': ['y']}}, {}),
            'focus_list': ({**healthy, 'narrator_strategy': 'focus'},
                           {'focus_speaker': ['Y']}),
            'focus_dict': ({**healthy, 'narrator_strategy': 'focus'},
                           {'focus_speaker': {'Y': 1}}),
            'focus_nested': ({**healthy, 'narrator_strategy': 'focus'},
                             {'focus_speaker': {'x': ['y']}}),
            'alias_int': ({**healthy, 'alias_of': 5}, {}),
            'alias_list': ({**healthy, 'alias_of': ['Narrator']}, {}),
            'alias_dict': ({**healthy, 'alias_of': {'a': 1}}, {}),
            'alias_bool': ({**healthy, 'alias_of': True}, {}),
            'members_int': ({**healthy, 'type': 'ensemble', 'members': 5}, {}),
            'members_list_bad': ({**healthy, 'type': 'ensemble', 'members': [5, 'Narrator']}, {}),
            'members_str': ({**healthy, 'type': 'ensemble', 'members': 'Narrator'}, {}),
            'members_dict': ({**healthy, 'type': 'ensemble', 'members': {'Narrator': 1}}, {}),
            'members_nested': ({**healthy, 'type': 'ensemble',
                                'members': {'Narrator': {'x': ['y']}}}, {}),
            'version_timeline_int': ({**healthy, 'version_timeline': 5}, {}),
            'version_timeline_bool': ({**healthy, 'version_timeline': True}, {}),
            'version_timeline_str': ({**healthy, 'version_timeline': 'V'}, {}),
            'version_timeline_dict': ({**healthy, 'version_timeline': {'a': 1}}, {}),
            'version_timeline_bad_points': ({**healthy, 'version_timeline': [5, None, 'x']}, {}),
            'style_timeline_int': ({**healthy, 'style_timeline': 5}, {}),
        }
        for label, (narrator, extra) in shapes.items():
            with self.subTest(label=label):
                voices = {'NARRATOR': narrator, 'Narrator': dict(healthy),
                          'READER': {'alias_of': 'Narrator'},
                          'GROUP': {'type': 'ensemble', 'members': ['READER']},
                          'Y': dict(type='custom', voice='Serena', seed=13)}
                row = {'speaker': 'GROUP', 'text': 'Line.', 'index': 0, **extra}
                canonical, resolved, _ = get_chunk_render_voice_selection(
                    voices, 'GROUP', row, 0)
                self.assertEqual('GROUP', canonical)
                self.assertEqual({'GROUP', 'Narrator'},
                                 set(get_chunk_voice_inputs(voices, row, 0)))
                self.assertEqual(('Aiden', 7), (resolved['Narrator']['voice'],
                                                resolved['Narrator']['seed']))

    def test_malformed_configured_narrator_spares_healthy_rows_and_saves(self):
        """N1: a malformed fallback cannot strand a batch or 500 an unrelated save."""
        from contextlib import nullcontext
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                voices = {'NARRATOR': 'old',
                          'Narrator': dict(type='custom', voice='Aiden', seed=7),
                          'READER': {'alias_of': 'Narrator'},
                          'GROUP': {'type': 'ensemble', 'members': ['READER']},
                          'Y': dict(type='custom', voice='Serena', seed=13)}
                manager.save_chunks([
                    dict(uid='group', index=0, speaker='GROUP', text='Group line.', status='pending'),
                    dict(uid='good', index=1, speaker='Y', text='Healthy line.', status='pending')])
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([{'speaker': key} for key in voices]))
                calls = []
                engine = self._synthetic_engine(manager, calls)
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = self._generate_batch(manager, grouped, [0, 1])
                self.assertEqual([0, 1], sorted(result['completed']), result)
                self.assertEqual([], result['failed'])
                self.assertEqual(['done', 'done'],
                                 [row['status'] for row in manager.load_chunks()])
                with self.routed(root, manager, Path(manager.voice_config_path)):
                    self.request('/api/voices/Y/versions', version_id='inactive',
                                 config=dict(type='custom', voice='Vivian', seed=23))

    def test_malformed_configured_narrator_reached_by_its_own_row_errors_there(self):
        """N1: a malformed entry the row actually renders fails that row only."""
        from contextlib import nullcontext
        for grouped in (True, False):
            owner = nullcontext() if grouped else self.case.owner()
            with self.subTest(grouped=grouped), self.case.fixture() as (root, manager, _, _), owner:
                voices = {'NARRATOR': 5,
                          'Y': dict(type='custom', voice='Serena', seed=13)}
                manager.save_chunks([
                    dict(uid='narr', index=0, speaker='NARRATOR', text='Narration.', status='pending'),
                    dict(uid='good', index=1, speaker='Y', text='Healthy.', status='pending')])
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                (root / 'annotated_script.json').write_text(
                    json.dumps([{'speaker': key} for key in voices]))
                calls = []
                engine = self._synthetic_engine(manager, calls)
                with patch.object(manager, 'get_engine', return_value=engine):
                    result = self._generate_batch(manager, grouped, [0, 1])
                self.assertEqual([1], result['completed'], result)
                self.assertEqual([0], [index for index, _ in result['failed']], result)
                self.assertEqual(('error', 'done'),
                                 tuple(row['status'] for row in manager.load_chunks()))

    def test_self_referential_fallback_ensemble_keeps_own_voice(self):
        """N2: an ensemble listing the replaced spelling must not nest itself."""
        from tts import get_chunk_render_voice_selection, voice_category

        voices = {'NARRATOR': {'alias_of': 'K'},
                  'Narrator': dict(type='custom', voice='Ryan', seed=42),
                  'K': {'type': 'ensemble', 'members': ['Narrator']},
                  'READER': {'alias_of': 'Narrator'},
                  'Y': dict(type='custom', voice='Serena', seed=13)}
        for speaker in ('Narrator', 'READER'):
            with self.subTest(speaker=speaker):
                row = {'speaker': speaker, 'text': 'Line.', 'index': 0}
                canonical, resolved, _ = get_chunk_render_voice_selection(
                    voices, speaker, row, 0)
                self.assertEqual('custom', voice_category(resolved.get('Narrator')),
                                 'The fallback replaced the spelling with its own ensemble')
                self.assertEqual(('Ryan', 42), (resolved['Narrator']['voice'],
                                                resolved['Narrator']['seed']))

    def test_export_readiness_uses_the_generation_row_seam(self):
        """N3: export readiness agrees with a renderable dynamic narrator member."""
        from routers import editor as editor_routes

        with self.case.fixture() as (root, manager, _, _):
            voices = {'NARRATOR': dict(type='custom', voice='Ryan', seed=42,
                                       narrator_strategy='gender', ready=True,
                                       versions={'M': dict(type='custom', voice='Aiden',
                                                           seed=7, gender='male', ready=True)}),
                      'GROUP': {'type': 'ensemble', 'members': ['Narrator'], 'ready': True},
                      'Y': dict(type='custom', voice='Serena', seed=13, ready=True)}
            rows = [{'speaker': 'GROUP', 'text': 'Line.', 'index': 0,
                     'uid': 'a', 'narrator_gender': 'male'}]
            Path(manager.voice_config_path).write_text(json.dumps(voices))
            (root / 'annotated_script.json').write_text(json.dumps(rows))
            with patch.multiple(editor_routes,
                                SCRIPT_PATH=str(root / 'annotated_script.json'),
                                DATA_DIR=str(root)):
                self.assertEqual([], editor_routes.get_unready_export_speakers(rows))

if __name__ == '__main__':
    unittest.main()
