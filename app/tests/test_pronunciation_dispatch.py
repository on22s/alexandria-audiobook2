"""Once-only simultaneous respelling at real synthesis and publication boundaries."""
import copy
from contextlib import ExitStack
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
from tests.test_batch_style_timeline import get_engine, get_torch


class PronunciationDispatchTests(unittest.TestCase):
    def test_all_native_single_direct_and_batch_paths_match_preview(self):
        for source, rules, expected in (
                ('Reinhard arrives.', {'Reinhard': 'Rine-hard', 'Rine': 'Reen'}, 'Rine-hard arrives.'),
                ('Ann arrives.', {'Ann': 'Ann Ann'}, 'Ann Ann arrives.')):
            for kind in ('custom', 'clone', 'lora', 'design', 'ensemble'):
                with self.subTest(source=source, kind=kind), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    engine = get_engine()
                    seen = []
                    torch = get_torch()
                    def synth(**kwargs):
                        if kind == 'lora':
                            acquired = []
                            def contend():
                                admitted = tts._LOCAL_TTS_LOCK.acquire(blocking=False)
                                acquired.append(admitted)
                                if admitted:
                                    tts._LOCAL_TTS_LOCK.release()
                            thread = threading.Thread(target=contend)
                            thread.start()
                            thread.join(2)
                            self.assertEqual([False], acquired, 'LoRA model inference must retain serialization')
                        value = kwargs['text']
                        seen.extend(value if isinstance(value, list) else [value])
                        count = len(value) if isinstance(value, list) else 1
                        return [np.full(4000, .1, dtype=np.float32) for _ in range(count)], 16000
                    model = SimpleNamespace(generate_custom_voice=synth, generate_voice_clone=synth,
                                            generate_voice_design=synth, _tokenize_texts=lambda texts: texts)
                    prompt = [SimpleNamespace(ref_code=None, ref_text='Reference.')]
                    config = {'A': {'type': kind, 'voice': 'Ryan', 'description': 'Calm voice',
                                    'adapter_path': str(root / 'adapter'), 'seed': 0},
                              'B': {'type': 'custom', 'voice': 'Ryan'}}
                    if kind == 'ensemble':
                        config['A']['members'] = ['B']
                    chunks = [{'speaker': 'A', 'text': source, 'instruct': 'Calm.', 'index': 0}]
                    before = copy.deepcopy((config, chunks))
                    with ExitStack() as stack:
                        stack.enter_context(pronunciation.ensure_pronunciation_audition(rules))
                        stack.enter_context(patch.dict('sys.modules', {'torch': torch}))
                        for method in ('_init_local_custom', '_init_local_clone', '_init_local_design'):
                            stack.enter_context(patch.object(engine, method, return_value=model))
                        stack.enter_context(patch.object(engine, '_get_clone_prompt', return_value=prompt))
                        stack.enter_context(patch.object(engine, '_ensure_local_lora_generation', return_value=(model, prompt)))
                        stack.enter_context(patch.object(tts, '_resolve_asset_path', side_effect=lambda p: str(root / p)))
                        self.assertEqual(expected, tts.get_speech_preparation(source)['text'])
                        self.assertTrue(engine.generate_voice(source, 'Calm.', 'A', config, str(root / 'single.wav')))
                        if kind == 'custom':
                            success = engine.generate_custom_voice(source, 'Calm.', 'A', config, str(root / 'direct.wav'))
                        elif kind == 'clone':
                            success = engine.generate_clone_voice(source, 'A', config, str(root / 'direct.wav'))
                        elif kind == 'ensemble':
                            success = engine.generate_ensemble_voice(source, 'Calm.', config['A'], config, str(root / 'direct.wav'))
                        else:
                            success = getattr(engine, f'generate_{kind}_voice')(source, 'Calm.', config['A'], str(root / 'direct.wav'))
                        self.assertTrue(success)
                        result = engine.generate_batch(chunks, config, tmp)
                    self.assertEqual([0], result['completed'])
                    self.assertEqual([], result['failed'])
                    self.assertEqual([expected] * 3, seen)
                    if kind != 'ensemble':
                        seeded_calls = 3 if kind == 'design' else 2
                        self.assertEqual([((0,), {})] * seeded_calls, torch.manual_seed.call_args_list)
                    self.assertEqual(before, (config, chunks))
                    for name in ('single.wav', 'direct.wav', 'temp_batch_0.wav'):
                        self.assertGreater(sf.info(root / name).frames, 0)

    def test_dynamic_narrator_uses_prepared_input_and_preserves_public_overrides(self):
        engine = get_engine()
        config = {'NARRATOR': {'type': 'custom', 'voice': 'Ryan'}}
        resolved = {'NARRATOR': {'type': 'custom', 'voice': 'Aiden'}}
        chunk = {'speaker': 'NARRATOR', 'text': 'Ann arrives.', 'index': 0}
        seen = []
        with tempfile.TemporaryDirectory() as tmp, pronunciation.ensure_pronunciation_audition({'Ann': 'Ann Ann'}), \
                patch.object(tts, 'resolve_narrator_voice_config', side_effect=lambda s, c, *args: resolved), \
                patch.object(engine, '_local_generate_custom', side_effect=lambda text, *args: seen.append(text) or True):
            result = engine.generate_batch([chunk], config, tmp)
            self.assertEqual({'completed': [0], 'failed': []}, result)
            self.assertEqual(['Ann Ann arrives.'], seen)
            # Replacements of the public dispatcher must still be called.
            with patch.object(engine, 'generate_voice', return_value=True) as render:
                engine.generate_batch([chunk], config, tmp)
                self.assertEqual('Ann Ann arrives.', render.call_args.args[0])

    def test_external_single_batch_and_ensemble_send_once_prepared_text(self):
        for kind in ('custom', 'clone'):
            for group in (False, True):
                with self.subTest(kind=kind, group=group), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    sf.write(root / 'reference.wav', np.full(4000, .1), 16000)
                    sf.write(root / 'provider.wav', np.full(4000, .1), 16000)
                    engine = get_engine()
                    engine._mode = 'external'
                    engine._external_parallel_workers = 1
                    engine._external_urls = ['fixture']
                    engine._external_timeout = 5
                    engine._next_external_url = lambda: 'fixture'
                    seen = []
                    def predict(*args, **kwargs):
                        seen.append(kwargs['text'] if kind == 'custom' else args[2])
                        return (str(root / 'provider.wav'),)
                    engine._external_endpoint = lambda ep: (SimpleNamespace(predict=predict), threading.Lock())
                    config = {'A': {'type': kind, 'voice': 'Ryan', 'ref_audio': str(root / 'reference.wav'),
                                    'ref_text': 'Reference.', 'seed': 0},
                              'GROUP': {'type': 'ensemble', 'members': ['A']}}
                    speaker = 'GROUP' if group else 'A'
                    with pronunciation.ensure_pronunciation_audition({'Ann': 'Ann Ann'}), \
                            patch('gradio_client.handle_file', side_effect=lambda path: path):
                        self.assertTrue(engine.generate_voice('Ann arrives.', '', speaker, config, str(root / 'single.wav')))
                        result = engine.generate_batch([{'speaker': speaker, 'text': 'Ann arrives.', 'index': 0}], config, tmp)
                    self.assertEqual({'completed': [0], 'failed': []}, result)
                    self.assertEqual(['Ann Ann arrives.'] * 2, seen)
