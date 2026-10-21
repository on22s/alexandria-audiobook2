"""CPU providers simulate bounded VRAM; these are not hardware OOM tests."""
import contextlib
import gc
import io
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import weakref

import numpy as np
import soundfile as sf
import torch
import tts
from tests.test_clone_reference_cache import Prompt
from tests.test_lora_generation_cache import _write


class ModelLifecycleTests(unittest.TestCase):
    @contextlib.contextmanager
    def provider(self, engine, root, capacity=8e9, probe_error=False):
        live = weakref.WeakSet()
        loads, renders = [], []
        state = NS(live=live, loads=loads, renders=renders, entered=None, release=None,
                   attempts=[], fail_next=False)
        class Model:
            def __init__(self, family):
                self.family = family
                self.model = NS(talker=NS(eval=lambda: None))
            def create_voice_clone_prompt(self, **kwargs):
                return [Prompt(None, torch.ones(4), ref_text=kwargs['ref_text'])]
            def render(self, **kwargs):
                renders.append((self.family, kwargs))
                if state.entered is not None and self.family == 'CustomVoice':
                    state.entered.set()
                    if not state.release.wait(5):
                        raise RuntimeError('test render timed out')
                return [np.sin(np.arange(4000) / 30).astype(np.float32) * .1], 24000
            generate_custom_voice = render
            generate_voice_clone = render
            generate_voice_design = render
        class Provider:
            @staticmethod
            def from_pretrained(path, **kwargs):
                state.attempts.append(path)
                if state.fail_next:
                    state.fail_next = False
                    raise OSError('simulated incomplete cached snapshot')
                gc.collect()
                if (len(live) + 1) * 5e9 > capacity:
                    raise MemoryError('simulated family capacity exhausted')
                family = str(path).rsplit('-', 1)[-1]
                model = Model(family)
                live.add(model)
                loads.append((family, kwargs))
                return model
        def free(device):
            self.assertEqual('cuda:1', str(device))
            if probe_error:
                raise RuntimeError('simulated missing driver telemetry')
            return capacity - len(live) * 5e9, capacity
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {
                'qwen_tts': NS(Qwen3TTSModel=Provider),
                'peft': NS(PeftModel=NS(from_pretrained=lambda model, path: NS(merge_and_unload=lambda: model))),
            }))
            stack.enter_context(patch.object(engine, '_resolve_device', return_value='cuda:1'))
            stack.enter_context(patch.object(engine, '_enable_rocm_optimizations'))
            stack.enter_context(patch.object(engine, '_vram_snapshot', return_value={}))
            stack.enter_context(patch.object(tts.device_utils, 'compute_dtype', return_value=torch.bfloat16))
            stack.enter_context(patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value=None))
            stack.enter_context(patch('torch.cuda.mem_get_info', side_effect=free))
            stack.enter_context(patch('torch.cuda.memory_reserved', return_value=0))
            stack.enter_context(patch('torch.cuda.memory_allocated', return_value=0))
            stack.enter_context(patch('torch.cuda.empty_cache'))
            stack.enter_context(patch.object(tts, '_resolve_asset_path', side_effect=lambda rel: str(root / rel)))
            yield state

    def test_mixed_voice_render_releases_inactive_families_and_reuses_active_cache(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            sf.write(root / 'reference.wav', np.sin(np.arange(96000) / 30) * .1, 24000)
            engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:1'}})
            config = {'CUSTOM': {'type': 'custom', 'voice': 'Ryan', 'seed': -1},
                      'CLONE': {'type': 'clone', 'ref_audio': str(root / 'reference.wav'),
                                'ref_text': 'Synthetic reference', 'seed': -1}}
            with self.provider(engine, root) as state:
                for number, speaker in enumerate(('CUSTOM', 'CUSTOM', 'CLONE', 'CLONE')):
                    if number == 2:
                        engine._custom_warmup_needed = False
                    self.assertTrue(engine.generate_voice('Synthetic line', '', speaker, config, str(root / f'{number}.wav')))
                self.assertEqual(['CustomVoice', 'Base'], [family for family, _ in state.loads])
                self.assertIsNone(engine._local_custom_model)
                self.assertEqual(1, engine._clone_prompt_cache.snapshot()['entries'])
                design, _ = engine.generate_voice_design('Synthetic identity', 'Synthetic line')
                self.assertTrue(Path(design).exists())
                self.assertIsNone(engine._local_clone_model)
                self.assertEqual(0, engine._clone_prompt_cache.snapshot()['entries'])
                self.assertEqual(0, engine._prepared_reference_cache.snapshot()['charged_bytes'])
                self.assertTrue(engine.generate_voice('Synthetic line', '', 'CUSTOM', config, str(root / 'again.wav')))
                self.assertIsNone(engine._local_design_model)
                self.assertTrue(engine._custom_warmup_needed)
                self.assertEqual(['CustomVoice', 'Base', 'VoiceDesign', 'CustomVoice'], [f for f, _ in state.loads])
                self.assertEqual(1, len(state.live))
                self.assertTrue(all(kwargs['max_new_tokens'] == 2048 for _, kwargs in state.renders))
                self.assertTrue(all(kwargs['device_map'] == 'cuda:1' for _, kwargs in state.loads))
                for path in root.glob('*.wav'):
                    self.assertGreater(sf.info(path).frames, 0)

    def test_ample_capacity_retains_model_and_prompt_caches(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:1'}})
            with self.provider(engine, Path(tmp), capacity=32e9) as state:
                engine._init_local_custom()
                original = weakref.ref(engine._local_custom_model)
                engine._init_local_clone()
                reference = Path(tmp) / 'reference.wav'
                sf.write(reference, np.sin(np.arange(2400) / 30) * .1, 24000)
                config = {'CLONE': {'ref_audio': str(reference), 'ref_text': 'Synthetic reference'}}
                first = engine._get_clone_prompt('CLONE', config)
                engine._init_local_design()
                self.assertIs(original(), engine._init_local_custom())
                self.assertEqual(1, engine._clone_prompt_cache.snapshot()['entries'])
                self.assertTrue(torch.equal(first[0].ref_spk_embedding,
                    engine._get_clone_prompt('CLONE', config)[0].ref_spk_embedding))
                self.assertEqual(3, len(state.loads))

    def test_unknown_capacity_conservatively_releases_inactive_family(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:1'}})
            with self.provider(engine, Path(tmp), probe_error=True) as state:
                engine._init_local_custom()
                engine._init_local_clone()
                self.assertIsNone(engine._local_custom_model)
                self.assertEqual(1, len(state.live))

    def test_dtype_generation_window_and_cpu_keep_conservative_capacity(self):
        for device, dtype, tokens in (('cuda:1', torch.float32, 2048),
                                      ('cuda:1', torch.bfloat16, 8192),
                                      ('cpu', torch.float32, 2048)):
            with self.subTest(device=device, dtype=dtype, tokens=tokens), tempfile.TemporaryDirectory() as tmp:
                engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': device, 'max_new_tokens': tokens}})
                with self.provider(engine, Path(tmp), capacity=24e9) as state, \
                     patch.object(engine, '_resolve_device', return_value=device), \
                     patch.object(tts.device_utils, 'compute_dtype', return_value=dtype):
                    engine._init_local_custom()
                    engine._init_local_clone()
                    self.assertIsNone(engine._local_custom_model)
                    self.assertEqual(1, len(state.live))

    def test_cached_snapshot_retry_still_works_after_family_release(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:1'}})
            with self.provider(engine, Path(tmp)) as state:
                engine._init_local_custom()
                state.fail_next = True
                with patch.object(tts.TTSEngine, '_resolve_local_model_path', return_value='/synthetic/cache-Base'):
                    engine._init_local_clone()
                self.assertEqual('/synthetic/cache-Base', state.attempts[1])
                self.assertEqual('Qwen/Qwen3-TTS-12Hz-1.7B-Base', state.attempts[2])
                self.assertEqual(['CustomVoice', 'Base'], [f for f, _ in state.loads])
                self.assertIsNone(engine._local_custom_model)
                self.assertEqual(1, len(state.live))

    def test_lora_switch_releases_other_families_and_clears_generation_owned_cache(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp); adapter = root / 'adapter'; _write(adapter)
            engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:1'}})
            with self.provider(engine, root) as state:
                engine._init_local_custom()
                engine._init_local_lora(str(adapter))
                self.assertIsNone(engine._local_custom_model)
                engine._lora_prompt_cache['fixture'] = 'prompt'
                engine._init_local_design()
                self.assertIsNone(engine._local_lora_model)
                self.assertIsNone(engine._lora_adapter_path)
                self.assertIsNone(engine._lora_generation_sha256)
                self.assertEqual({}, engine._lora_prompt_cache)
                engine._init_local_lora(str(adapter))
                self.assertIsNone(engine._local_design_model)
                self.assertEqual(1, len(state.live))

    def test_switch_waits_until_inflight_render_releases_serialization_lock(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp); engine = tts.TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:1'}})
            config = {'A': {'type': 'custom', 'voice': 'Ryan', 'seed': -1}}
            with self.provider(engine, root) as state:
                state.entered, state.release = threading.Event(), threading.Event()
                second_started, second_done = threading.Event(), threading.Event()
                results, failures = [], []
                def render():
                    results.append(engine.generate_voice('Synthetic line', '', 'A', config, str(root / 'render.wav')))
                def switch():
                    second_started.set()
                    try:
                        engine._init_local_clone()
                    except Exception as error:
                        failures.append(error)
                    finally:
                        second_done.set()
                first = threading.Thread(target=render); first.start()
                self.assertTrue(state.entered.wait(5))
                second = threading.Thread(target=switch); second.start()
                try:
                    self.assertTrue(second_started.wait(5))
                    self.assertFalse(second_done.wait(.05))
                    self.assertIsNotNone(engine._local_custom_model)
                    self.assertEqual(1, len(state.loads))
                finally:
                    state.release.set(); first.join(5); second.join(5)
                self.assertFalse(first.is_alive()); self.assertFalse(second.is_alive())
                self.assertEqual([True], results); self.assertEqual([], failures)
                self.assertIsNone(engine._local_custom_model)
