"""Actual TTS/Project paths preserve parent identity and prepare words once."""
import asyncio
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
import pronunciation
import tts
from synthesis_settings import get_synthesis_window_settings, get_synthesis_budget_keys
from tests.test_batch_style_timeline import get_engine, get_torch
from tests.test_occurrence_pronunciation_generation import rule
from tests import test_audio_generation_cancellation as cancellation_fixture
from fastapi import BackgroundTasks
from routers import editor


class SynthesisWindowDispatchTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, root, kind='custom', chars=8):
        with contextlib.ExitStack() as stack:
            engine = get_engine(); engine._device = 'cpu'
            engine._synthesis_windows = get_synthesis_window_settings({'enabled': True,
                'budgets': {key: {'max_chars': chars} for key in get_synthesis_budget_keys()}})
            seen = []
            def provider(**kwargs):
                seen.append(copy.deepcopy(kwargs))
                texts = kwargs['text'] if isinstance(kwargs['text'], list) else [kwargs['text']]
                return [np.full(2400, .2) for _ in texts], 24000
            model = SimpleNamespace(generate_custom_voice=provider, generate_voice_clone=provider,
                                    generate_voice_design=provider, _tokenize_texts=lambda values: values)
            prompt = [SimpleNamespace(ref_code=None, ref_text='Synthetic reference.')]
            for method in ('_init_local_custom', '_init_local_clone', '_init_local_design'):
                stack.enter_context(patch.object(engine, method, return_value=model))
            stack.enter_context(patch.object(engine, '_get_clone_prompt', return_value=prompt))
            stack.enter_context(patch.object(engine, '_ensure_local_lora_generation', return_value=(model, prompt)))
            stack.enter_context(patch.object(tts, '_resolve_asset_path', side_effect=lambda path: str(root/path)))
            stack.enter_context(patch.dict('sys.modules', {'torch': get_torch()}))
            voices = {'A': dict(type=kind, voice='Aiden', description='Calm', adapter_path=str(root/'adapter'), seed=0),
                      'B': dict(type='custom', voice='Ryan')}
            if kind == 'ensemble': voices['A']['members'] = ['B']
            yield engine, voices, seen, model

    def texts(self, seen):
        return [text for call in seen for text in (call['text'] if isinstance(call['text'], list) else [call['text']])]

    def test_all_builtin_voice_paths_split_canonical_occurrence_and_shared_words_once(self):
        for kind in ('custom', 'clone', 'design', 'lora', 'ensemble'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.fixture(root, kind) as (engine, voices, seen, model):
                    row = dict(uid='a', index=7, speaker='A', text='Ann Ann', pronunciation_rules=[rule(replacement='Ann Ann')])
                    before = copy.deepcopy((row, voices))
                    context = tts.get_pronunciation_render_context(voices, row, 7, engine.get_model_settings())
                    with pronunciation.ensure_pronunciation_audition({'Ann': 'Shared'}), \
                            pronunciation.ensure_occurrence_pronunciation_context(row, context):
                        result = engine.generate_voice_result(row['text'], '', 'A', voices, str(root/'single.wav'))
                        self.assertTrue(result.success, result.failure)
                        self.assertEqual(2, result.synthesis_receipt['window_count'])
                        self.assertEqual('Ann Ann Shared.', ''.join(self.texts(seen)))
                        self.assertEqual(4800, sf.info(root/'single.wav').frames)
                        seen.clear()
                        if kind == 'custom': direct = engine.generate_custom_voice(row['text'], '', 'A', voices, str(root/'direct.wav'))
                        elif kind == 'clone': direct = engine.generate_clone_voice(row['text'], 'A', voices, str(root/'direct.wav'))
                        elif kind == 'ensemble': direct = engine.generate_ensemble_voice(row['text'], '', voices['A'], voices, str(root/'direct.wav'))
                        else: direct = getattr(engine, f'generate_{kind}_voice')(row['text'], '', voices['A'], str(root/'direct.wav'))
                        self.assertTrue(direct); self.assertEqual('Ann Ann Shared.', ''.join(self.texts(seen)))
                        seen.clear(); batch = engine.generate_batch([row], voices, tmp, batch_seed=0)
                        self.assertEqual([7], batch['completed']); self.assertEqual([], batch['failed'])
                        self.assertEqual('Ann Ann Shared.', ''.join(self.texts(seen)))
                        self.assertEqual(2, batch['synthesis_receipts'][7]['window_count'])
                        self.assertEqual(4800, sf.info(root/'temp_batch_7.wav').frames)
                    self.assertEqual(before, (row, voices))

    def test_unsplit_native_rows_keep_list_batching_and_original_parent_style_indices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.fixture(root, chars=10) as (engine, voices, seen, model):
                voices['A'].update(character_style='young', style_timeline=[{'from_index': 4, 'character_style': 'aged'}])
                rows = [dict(index=7, speaker='A', text='One. Two. Three.', instruct='warm'),
                        dict(index=2, speaker='A', text='Short.', instruct='soft')]
                original = copy.deepcopy((rows, voices))
                result = engine.generate_batch(rows, voices, tmp, batch_seed=0)
                self.assertEqual({7, 2}, set(result['completed'])); self.assertEqual([], result['failed'])
                self.assertEqual({7}, set(result['synthesis_receipts']))
                native = [call for call in seen if isinstance(call['text'], list)]
                singles = [call for call in seen if isinstance(call['text'], str)]
                self.assertEqual([['Short.']], [call['text'] for call in native])
                self.assertEqual('One. Two. Three.', ''.join(call['text'] for call in singles))
                self.assertTrue(all(call['instruct'] == 'aged warm' for call in singles))
                self.assertEqual(['young soft'], native[0]['instruct'])
                self.assertTrue(all((call['speaker'] == 'Aiden' if isinstance(call['text'], str) else call['speaker'] == ['Aiden']) for call in seen))
                self.assertEqual(original, (rows, voices))
                self.assertFalse((root/'temp_batch_0.wav').exists()); self.assertFalse((root/'temp_batch_1.wav').exists())

    def test_project_journals_receipts_to_matching_revision_then_clears_on_unsplit_regeneration(self):
        fixture = cancellation_fixture.AudioGenerationCancellationTests()
        for mode in ('single', 'parallel', 'native'):
            with self.subTest(mode=mode), fixture.fixture(1) as (root, manager, before, state):
                manager._update_chunk_fields_by_uid('line-0', text='One. Two. Three.')
                with self.fixture(root, chars=10) as (engine, voices, seen, model):
                    manager.engine = engine
                    (root/'voice_config.json').write_text('{"NARRATOR":{"type":"custom","voice":"Aiden"}}')
                    if mode == 'single': result = manager.generate_chunk_audio(0); self.assertTrue(result[0], result)
                    elif mode == 'parallel': self.assertEqual([0], manager.generate_chunks_parallel([0], max_workers=1)['completed'])
                    else: self.assertEqual([0], manager.generate_chunks_batch([0], batch_size=1)['completed'])
                    saved = manager.load_chunks()[0]; receipt = saved['synthesis_receipt']
                    self.assertEqual(saved['audio_revision'], receipt['audio_revision'])
                    self.assertEqual('pre-encoding PCM', receipt['scope']); self.assertEqual(2, receipt['window_count'])
                    self.assertEqual('One. Two. Three.', saved['text'])
                    prior = (root/saved['audio_path']).read_bytes()
                    engine._synthesis_windows = None
                    self.assertTrue(manager.generate_chunk_audio(0)[0])
                    self.assertIsNone(manager.load_chunks()[0]['synthesis_receipt'])
                    self.assertEqual(prior, (root/saved['audio_path']).read_bytes())

    def test_owned_cancel_after_first_actual_window_stops_all_editor_modes(self):
        fixture = cancellation_fixture.AudioGenerationCancellationTests()
        for mode in ('single', 'parallel', 'native'):
            with self.subTest(mode=mode), fixture.fixture(1) as (root, manager, before, state):
                manager._update_chunk_fields_by_uid('line-0', text='One. Two. Three.')
                before = manager.load_chunks()
                with self.fixture(root, chars=10) as (engine, voices, seen, model):
                    def provider(**kwargs):
                        seen.append(kwargs)
                        self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
                        return [np.full(2400, .2)], 24000
                    model.generate_custom_voice = provider; manager.engine = engine
                    async def exercise():
                        tasks = BackgroundTasks(); await fixture.start(mode, tasks, [0]); await tasks()
                    asyncio.run(exercise())
                    self.assertEqual(1, len(seen)); self.assertEqual(before, manager.load_chunks())
                    self.assertFalse(list(root.glob('.synthesis-render-*')))
                    self.assertFalse(list(root.glob('chunk_*.wav'))); self.assertFalse(list(root.glob('temp_batch_*.wav')))

    def test_external_custom_clone_and_ensemble_preserve_prepared_words_and_parent_receipts(self):
        for kind in ('custom', 'clone'):
            for ensemble in (False, True):
                with self.subTest(kind=kind, ensemble=ensemble), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    with self.fixture(root) as (engine, voices, seen, model):
                        engine._mode = 'external'; engine._external_parallel_workers = 1
                        engine._external_urls = ['fixture']; engine._external_timeout = 5
                        self.addCleanup(engine.close_external)
                        for name in ('reference.wav', 'provider.wav'):
                            sf.write(root/name, np.full(2400, .2), 24000)
                        calls = []
                        def predict(*args, **kwargs):
                            calls.append(kwargs['text'] if kind == 'custom' else args[2])
                            return (str(root/'provider.wav'),)
                        engine._external_endpoint = lambda endpoint: (SimpleNamespace(predict=predict), threading.Lock())
                        voices = {'A': dict(type=kind, voice='Aiden', seed=0,
                            ref_audio=str(root/'reference.wav'), ref_text='Synthetic reference.'),
                            'GROUP': dict(type='ensemble', members=['A'])}
                        speaker = 'GROUP' if ensemble else 'A'
                        row = dict(uid='a', index=7, speaker=speaker, text='Ann Ann',
                            pronunciation_rules=[rule(replacement='Ann Ann')])
                        before = copy.deepcopy((row, voices))
                        context = tts.get_pronunciation_render_context(voices, row, 7, engine.get_model_settings())
                        with pronunciation.ensure_pronunciation_audition({'Ann': 'Shared'}), \
                                pronunciation.ensure_occurrence_pronunciation_context(row, context), \
                                patch('gradio_client.handle_file', side_effect=lambda path: path):
                            result = engine.generate_voice_result(row['text'], '', speaker, voices, str(root/'single.wav'))
                            self.assertTrue(result.success, result.failure)
                            self.assertEqual(2, result.synthesis_receipt['window_count'])
                            self.assertEqual('Ann Ann Shared.', ''.join(calls)); calls.clear()
                            result = engine.generate_batch([row], voices, tmp, batch_seed=0)
                            self.assertEqual([7], result['completed']); self.assertEqual([], result['failed'])
                            self.assertEqual(2, result['synthesis_receipts'][7]['window_count'])
                            self.assertEqual('Ann Ann Shared.', ''.join(calls))
                        self.assertEqual(4800, sf.info(root/'temp_batch_7.wav').frames)
                        self.assertEqual(before, (row, voices))
                        self.assertFalse(list(root.glob('.synthesis-render-*')))

    def test_owned_native_cancel_does_not_start_later_split_or_unsplit_parent(self):
        fixture = cancellation_fixture.AudioGenerationCancellationTests()
        for later_text in ('Short.', 'Four. Five. Six.'):
            with self.subTest(later_text=later_text), fixture.fixture(2) as (root, manager, before, state):
                for index, text in enumerate(('One. Two. Three.', later_text)):
                    manager._update_chunk_fields_by_uid(f'line-{index}', text=text)
                before = manager.load_chunks()
                config_path = Path(manager.config_path)
                config = json.loads(config_path.read_text()); config['tts']['parallel_workers'] = 2
                config_path.write_text(json.dumps(config)); manager.invalidate_config_cache()
                with self.fixture(root, chars=10) as (engine, voices, seen, model):
                    def provider(**kwargs):
                        seen.append(kwargs)
                        self.assertEqual({'status': 'cancelling'}, editor._apply_audio_cancel())
                        return [np.full(2400, .2)], 24000
                    model.generate_custom_voice = provider; manager.engine = engine
                    async def exercise():
                        tasks = BackgroundTasks(); await fixture.start('native', tasks, [0, 1]); await tasks()
                    asyncio.run(exercise())
                    self.assertEqual(1, len(seen)); self.assertEqual(before, manager.load_chunks())
                    self.assertFalse(list(root.glob('.synthesis-render-*')))
                    self.assertFalse(list(root.glob('temp_batch_*.wav')))
