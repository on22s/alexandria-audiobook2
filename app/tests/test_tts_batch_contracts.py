"""Synthetic endpoint-to-provider regressions for TTS batch correctness."""
import asyncio
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
import tts
from project import ProjectManager
from routers import voices
from tests.test_batch_style_timeline import get_engine
from tests import test_lora_generation_cache as lora_cache
from adapter_checkpoint_transaction import save_adapter_checkpoint, get_adapter_generation_sha256


def save_version(speaker, version_id, config):
    asyncio.run(voices.save_voice_version(speaker, voices.VoiceVersionRequest(version_id=version_id, config=config)))


def select_at(speaker, version_id, index=5):
    asyncio.run(voices.save_version_timeline(speaker, voices.VersionTimelineRequest(points=[voices.VoiceVersionPoint(from_index=index, version_id=version_id)])))


def write_audio(path):
    sf.write(path, np.full(4000, .1, dtype=np.float32), 16000)


class BatchContractsTests(unittest.TestCase):
    def test_public_ensemble_versions_resolve_new_members_and_invalidate_audio(self):
        for base in ({'type': 'custom', 'voice': 'Ryan'}, {'type': 'ensemble', 'members': ['OLD']}):
            with self.subTest(base=base), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); manager = ProjectManager(tmp)
                config = {'GROUP': base, 'OLD': {'type': 'custom', 'voice': 'Ryan'},
                          'B': {'type': 'custom', 'voice': 'Ryan', 'style_timeline': [{'from_index': 5, 'character_style': 'aged'}]},
                          'NICK': {'alias_of': 'B', 'voice': 'Ryan'}}
                script = root / 'annotated_script.json'
                script.write_text(json.dumps([{'speaker': name, 'text': 'Synthetic words'} for name in config]))
                Path(manager.voice_config_path).write_text(json.dumps(config))
                manager.save_chunks([{'speaker': 'GROUP', 'text': 'Together', 'status': 'pending'} for _ in range(6)])
                with patch.object(voices, 'SCRIPT_PATH', str(script)), patch.object(voices, 'VOICE_CONFIG_PATH', manager.voice_config_path), patch.object(voices, 'CHUNKS_PATH', manager.chunks_path), patch.object(voices, 'project_manager', manager):
                    save_version('GROUP', 'chorus', {'type': 'ensemble', 'members': ['NICK']})
                    save_version('B', 'adult', {'type': 'custom', 'voice': 'Aiden'})
                    select_at('GROUP', 'chorus'); select_at('B', 'adult')
                    saved = json.loads(Path(manager.voice_config_path).read_text()); before = copy.deepcopy(saved)
                    resolved = tts.voice_config_for_chunk(saved, 'GROUP', 5)
                    self.assertEqual(tts.voice_config_for_chunk(saved, 'B', 5)['B'], resolved['B'])
                    self.assertEqual(('Aiden', 'aged'), (resolved['B']['voice'], resolved['B']['character_style']))
                    engine = get_engine(); received = []
                    def render(text, instruct, speaker, voice_config, out):
                        received.append((speaker, voice_config[speaker])); write_audio(out); return True
                    engine.generate_custom_voice = render
                    result = engine.generate_batch([{'speaker': 'GROUP', 'text': 'Together', 'index': 5}], saved, tmp)
                    self.assertEqual({'completed': [5], 'failed': []}, result)
                    self.assertEqual('B', received[0][0]); self.assertEqual('Aiden', received[0][1]['voice']); self.assertEqual(before, saved)
                    chunks = manager.load_chunks(); chunks[5].update(status='done', audio_path='chorus.wav')
                    write_audio(root / 'chorus.wav'); manager.save_chunks(chunks)
                    save_version('B', 'adult', {'type': 'custom', 'voice': 'Serena'})
                    self.assertEqual('pending', manager.load_chunks()[5]['status']); self.assertIsNone(manager.load_chunks()[5]['audio_path'])

    def test_alias_members_dispatch_to_canonical_custom_clone_and_lora(self):
        for kind in ('custom', 'clone', 'lora'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                engine = get_engine(); received = []
                config = {'GROUP': {'type': 'ensemble', 'members': ['DIRECT', 'CHAIN']},
                          'DIRECT': {'alias_of': 'CANON', 'voice': 'Ryan'}, 'CHAIN': {'alias': 'DIRECT'},
                          'CANON': {'type': kind, 'voice': 'Aiden', 'adapter_path': 'fixture', 'ref_audio': 'fixture', 'ref_text': 'Synthetic reference'}}
                def custom(text, instruct, speaker, data, out):
                    received.append((speaker, data[speaker])); write_audio(out); return True
                def clone(text, speaker, data, out): return custom(text, '', speaker, data, out)
                def lora(text, instruct, data, out):
                    received.append(('CANON', data)); write_audio(out); return True
                engine.generate_custom_voice = custom; engine.generate_clone_voice = clone; engine.generate_lora_voice = lora
                self.assertTrue(engine.generate_voice('Together', '', 'GROUP', config, str(Path(tmp) / 'mix.wav')))
                self.assertEqual([('CANON', config['CANON'])] * 2, received)
                received.clear(); before = copy.deepcopy(config)
                result = engine.generate_batch([{'speaker': 'GROUP', 'text': 'Together', 'index': 0}], config, tmp, batch_seed=42)
                self.assertEqual({'completed': [0], 'failed': []}, result)
                self.assertEqual([('CANON', {**config['CANON'], 'seed': 42})] * 2, received)
                self.assertEqual(before, config)
                self.assertEqual('CANON', ProjectManager(tmp)._resolve_alias('CHAIN', config))
                inputs = ProjectManager(tmp).get_chunk_voice_inputs(config, {'speaker': 'GROUP'}, 0)
                self.assertIn('CANON', inputs); self.assertNotIn('CHAIN', inputs)
                for target in ({'alias_of': 'DIRECT'}, {'type': 'ensemble', 'members': ['DIRECT']}):
                    config['CANON'] = target; received.clear()
                    with self.assertRaisesRegex(ValueError, 'cycle|nested'):
                        engine.generate_voice('Together', '', 'GROUP', config, str(Path(tmp) / 'bad.wav'))
                    self.assertEqual([], received); self.assertFalse((Path(tmp) / 'bad.wav').exists())

    def test_batch_seed_reaches_design_and_ensemble_after_version_selection(self):
        for seed in (0, 42, -1):
            for speaker in ('DESIGN', 'GROUP'):
                with self.subTest(seed=seed, speaker=speaker), tempfile.TemporaryDirectory() as tmp:
                    engine = get_engine(); calls = []
                    config = {'DESIGN': {'type': 'design', 'description': 'Synthetic identity', 'seed': 17,
                              'versions': {'selected': {'type': 'design', 'seed': 23}}, 'version_timeline': [{'from_index': 5, 'version_id': 'selected'}]},
                              'NICK': {'alias_of': 'DESIGN'}, 'OTHER': {'type': 'design', 'description': 'Other identity', 'seed': 29},
                              'GROUP': {'type': 'ensemble', 'members': ['NICK', 'OTHER']}}
                    before = copy.deepcopy(config)
                    def preview(description, sample_text, seed=-1):
                        calls.append(seed); path = Path(tmp) / 'preview.wav'; write_audio(path); return str(path), 16000
                    engine.generate_voice_design = preview
                    result = engine.generate_batch([{'speaker': speaker, 'text': 'Synthetic line', 'index': 5}], config, tmp, batch_seed=seed)
                    self.assertEqual({'completed': [5], 'failed': []}, result)
                    expected = [seed if seed >= 0 else 23]
                    if speaker == 'GROUP': expected.append(seed if seed >= 0 else 29)
                    self.assertEqual(expected, calls); self.assertEqual(before, config)

    def test_external_predict_receives_batch_seed_for_custom_clone_and_ensemble(self):
        for kind in ('custom', 'clone'):
            for seed in (0, 42, -1):
                for speaker in ('VOICE', 'GROUP'):
                    with self.subTest(kind=kind, seed=seed, speaker=speaker), tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp); write_audio(root / 'reference.wav'); write_audio(root / 'provider.wav')
                        engine = get_engine(); engine._mode = 'external'; engine._external_parallel_workers = 1
                        engine._external_urls = ['fixture']; engine._external_timeout = 5; engine._next_external_url = lambda: 'fixture'; calls = []
                        def predict(*args, **kwargs):
                            calls.append(kwargs['seed'] if kind == 'custom' else args[8]); return (str(root / 'provider.wav'),)
                        engine._external_endpoint = lambda ep: (SimpleNamespace(predict=predict), threading.Lock())
                        config = {'VOICE': {'type': kind, 'voice': 'Aiden', 'seed': 17, 'ref_audio': str(root / 'reference.wav'), 'ref_text': 'Synthetic reference'},
                                  'GROUP': {'type': 'ensemble', 'members': ['VOICE']}}
                        before = copy.deepcopy(config)
                        with patch('gradio_client.handle_file', side_effect=lambda path: path):
                            result = engine.generate_batch([{'speaker': speaker, 'text': 'Synthetic line', 'index': 5}], config, tmp, batch_seed=seed)
                        self.assertEqual({'completed': [5], 'failed': []}, result)
                        self.assertEqual([seed if seed >= 0 else 17], calls); self.assertEqual(before, config)
                        self.assertTrue((root / 'temp_batch_5.wav').exists())

    def test_public_lora_pin_refuses_promoted_generation_before_loading_and_groups_separate_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); adapter = root / 'adapter'; lora_cache._write(adapter); original = get_adapter_generation_sha256(adapter)
            manager = ProjectManager(tmp); script = root / 'annotated_script.json'
            script.write_text('[{"speaker":"PINNED","text":"Synthetic line"}]')
            Path(manager.voice_config_path).write_text(json.dumps({'PINNED': {'type': 'lora', 'adapter_path': str(adapter)}}))
            with patch.object(voices, 'SCRIPT_PATH', str(script)), patch.object(voices, 'VOICE_CONFIG_PATH', manager.voice_config_path), patch.object(voices, 'CHUNKS_PATH', manager.chunks_path), patch.object(voices, 'project_manager', manager):
                save_version('PINNED', 'old', {'type': 'lora', 'adapter_path': str(adapter), 'adapter_generation_sha256': original}); select_at('PINNED', 'old', 0)
            save_adapter_checkpoint(adapter, lambda stage: lora_cache._write(stage, 2))
            config = json.loads(Path(manager.voice_config_path).read_text()); before = copy.deepcopy(config)
            engine = tts.TTSEngine({'tts': {'mode': 'local'}}); loads, prompts = [], []; providers = lora_cache.LoraGenerationCacheTests()
            with providers.providers(engine, loads, prompts), patch.object(engine, '_estimate_max_batch_size', return_value=1), patch.object(engine, '_build_sub_batches', return_value=[(0, 1)]):
                self.assertFalse(engine.generate_voice('Synthetic line', '', 'PINNED', tts.voice_config_for_chunk(config, 'PINNED', 0), str(root / 'single.wav')))
                result = engine.generate_batch([{'speaker': 'PINNED', 'text': 'Synthetic line', 'index': 0}], config, tmp)
                self.assertEqual([], result['completed']); self.assertEqual(0, result['failed'][0][0]); self.assertIn('generation changed', result['failed'][0][1])
                self.assertEqual([], loads); self.assertFalse((root / 'single.wav').exists()); self.assertFalse((root / 'temp_batch_0.wav').exists())
                config['CURRENT'] = {'type': 'lora', 'adapter_path': str(adapter), 'adapter_generation_sha256': get_adapter_generation_sha256(adapter)}
                result = engine.generate_batch([{'speaker': 'CURRENT', 'text': 'Synthetic current', 'index': 1}, {'speaker': 'PINNED', 'text': 'Synthetic old', 'index': 0}], config, tmp)
                self.assertEqual([1], result['completed']); self.assertEqual([0], [i for i, _ in result['failed']]); self.assertEqual(1, len(loads))
                self.assertTrue((root / 'temp_batch_1.wav').exists()); self.assertFalse((root / 'temp_batch_0.wav').exists())
            self.assertEqual(before['PINNED'], config['PINNED'])

    def test_lora_batch_releases_previous_group_model_before_bounded_next_load(self):
        import gc
        import weakref
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); adapters = [root / 'first', root / 'second']
            for value, adapter in enumerate(adapters, 1): lora_cache._write(adapter, value)
            live = weakref.WeakSet(); loads, renders, prompts = [], [], []
            class Model:
                def __init__(self):
                    self.model = SimpleNamespace(talker=lora_cache.BaseTalker())
                def create_voice_clone_prompt(self, **kwargs):
                    prompts.append(kwargs['ref_text'])
                    return [SimpleNamespace(ref_code=None, ref_text=kwargs['ref_text'])]
                def _tokenize_texts(self, texts): return texts
                def generate_voice_clone(self, **kwargs):
                    renders.append(kwargs['max_new_tokens'])
                    weight = float(self.model.talker.layer.q_proj.weight.detach().mean())
                    return [np.full(2400, weight / 100, dtype=np.float32) for _ in kwargs['text']], 24000
            def load(*args):
                gc.collect()
                # CPU providers simulate one 5 GB family within an 8 GB budget.
                if (len(live) + 1) * 5e9 > 8e9:
                    raise MemoryError('simulated family capacity exhausted')
                model = Model(); live.add(model); loads.append('loaded'); return model
            engine = tts.TTSEngine({'tts': {'mode': 'local', 'max_new_tokens': 100}})
            config = {name: {'type': 'lora', 'adapter_path': str(adapter),
                            'adapter_generation_sha256': get_adapter_generation_sha256(adapter)}
                      for name, adapter in zip(('A', 'B'), adapters)}
            before = copy.deepcopy(config)
            chunks = [{'speaker': name, 'text': 'Synthetic bounded line', 'index': index}
                      for index, name in enumerate(('A', 'A', 'B', 'B'))]
            # A call-recording estimator Mock would itself retain the model.
            with patch.dict(lora_cache.sys.modules, {'qwen_tts': SimpleNamespace(Qwen3TTSModel=object())}), \
                 patch.object(engine, '_load_model', side_effect=load), \
                 patch.object(engine, '_resolve_device', return_value='cpu'), \
                 patch.object(engine, '_enable_rocm_optimizations'), \
                 patch.object(engine, '_clear_gpu_cache'), \
                 patch.object(engine, '_estimate_max_batch_size', new=lambda *args, **kwargs: 1), \
                 patch.object(lora_cache.torch.cuda, 'is_available', return_value=False), \
                 patch.object(lora_cache.peft_lora_model, 'is_bnb_available', return_value=False), \
                 patch.object(lora_cache.peft_lora_model, 'is_bnb_4bit_available', return_value=False):
                result = engine.generate_batch(chunks, config, tmp)
                self.assertEqual({'completed': [0, 1, 2, 3], 'failed': []}, result)
                self.assertEqual(2, len(loads)); self.assertEqual(1, len(live))
                # The admitted active generation still reuses its model/prompt.
                result = engine.generate_batch([{'speaker': 'B', 'text': 'Synthetic cached line', 'index': 4}], config, tmp)
                self.assertEqual({'completed': [4], 'failed': []}, result)
                self.assertEqual(2, len(loads)); self.assertEqual(1, len(live))
                self.assertEqual(['reference generation 1', 'reference generation 2'], prompts)
            for index in range(5):
                audio, rate = sf.read(root / f'temp_batch_{index}.wav')
                self.assertEqual((2400, 24000), (len(audio), rate))
                self.assertAlmostEqual(.0425 if index < 2 else .1625, float(audio.mean()), places=4)
            self.assertEqual([100] * 5, renders); self.assertEqual(before, config)
