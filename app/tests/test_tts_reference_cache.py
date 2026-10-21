"""Real reference capture/decode with a small CPU prompt-provider fixture."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import gc
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import weakref

import numpy as np
import soundfile as sf
import torch

import clone_reference_cache as cache
import tts
from audio_generation_owner import (AudioGenerationCancelled, AudioGenerationOwner,
                                    ensure_audio_generation_owner)
from tests.test_clone_reference_cache import Prompt


class Model:
    def __init__(self):
        self.calls = []
        self.fail = self.mutate = False
        self.entered = self.release = self.cancel = None
        self.observe = None

    def create_voice_clone_prompt(self, *, ref_audio, ref_text):
        audio, rate = ref_audio
        self.calls.append((audio.copy(), rate, ref_text))
        if self.observe is not None:
            self.observe()
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(3):
                raise RuntimeError('CPU provider fixture timed out')
        if self.fail:
            raise ValueError('Synthetic prompt construction failure')
        embedding = torch.tensor([float(audio.mean()), float(rate)], dtype=torch.float64)
        if self.mutate:
            audio.fill(99)
        if self.cancel is not None:
            self.cancel.set()
        return [Prompt(torch.arange(8).reshape(2, 4), embedding, ref_text=ref_text)]


class TtsReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.reference = self.root / 'reference.wav'
        sf.write(self.reference, np.linspace(-.1, .1, 2400), 24000, subtype='FLOAT')
        self.engine = tts.TTSEngine({'tts': {'mode': 'local'}})
        self.model = Model()
        self.voices = {name: {'ref_audio': str(self.reference), 'ref_text': 'Synthetic reference'}
                       for name in ('A', 'B')}
        controlled = patch.object(self.engine, '_init_local_clone', side_effect=lambda: self.model)
        controlled.start()
        self.addCleanup(controlled.stop)

    def test_identical_labels_and_path_aliases_share_work_but_capture_every_hit(self):
        alias = self.root / 'copied.wav'
        shutil.copyfile(self.reference, alias)
        self.voices['B']['ref_audio'] = str(alias)
        with patch.object(tts, 'get_adapter_asset_snapshot', wraps=tts.get_adapter_asset_snapshot) as capture, \
                patch.object(tts.sf, 'read', wraps=sf.read) as decode:
            first = self.engine._get_clone_prompt('A', self.voices)
            second = self.engine._get_clone_prompt('B', self.voices)
            self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual((3, 1, 1), (capture.call_count, decode.call_count, len(self.model.calls)))
        self.assertIsNot(first, second)
        first[0].ref_spk_embedding.fill_(42)
        second[0].ref_code.zero_()
        third = self.engine._get_clone_prompt('A', self.voices)
        self.assertTrue(torch.equal(third[0].ref_code, torch.arange(8).reshape(2, 4)))
        self.assertEqual(24000, third[0].ref_spk_embedding[1].item())

    def test_transcript_variant_reuses_pristine_float64_preparation(self):
        stereo = np.column_stack((np.linspace(-.17, .19, 2400), np.linspace(.12, -.13, 2400)))
        sf.write(self.reference, stereo, 24000, subtype='DOUBLE')
        expected = stereo.mean(axis=1)
        self.model.mutate = True
        self.voices['B']['ref_text'] = 'Different exact transcript'
        with patch.object(tts.sf, 'read', wraps=sf.read) as decode:
            self.engine._get_clone_prompt('A', self.voices)
            second = self.engine._get_clone_prompt('B', self.voices)
        self.assertEqual((1, 2), (decode.call_count, len(self.model.calls)))
        for audio, rate, _ in self.model.calls:
            self.assertEqual(np.dtype('float64'), audio.dtype)
            np.testing.assert_array_equal(expected, audio)
            self.assertEqual(24000, rate)
        self.assertEqual(self.voices['B']['ref_text'], second[0].ref_text)

    def test_sample_rate_preprocessing_and_actual_model_change_invalidate(self):
        self.engine._get_clone_prompt('A', self.voices)
        sf.write(self.reference, np.linspace(-.1, .1, 2400), 16000, subtype='FLOAT')
        changed = self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual(16000, changed[0].ref_spk_embedding[1].item())
        with patch.object(cache, 'PREPROCESSING_VERSION', cache.PREPROCESSING_VERSION + 1):
            self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual(3, len(self.model.calls))
        previous = weakref.ref(self.model)
        self.model = Model()
        gc.collect()
        self.assertIsNone(previous())  # Cache ownership must not retain a model.
        with patch.object(tts.sf, 'read', wraps=sf.read) as decode:
            self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual((1, 1), (decode.call_count, len(self.model.calls)))
        self.assertEqual(1, self.engine._clone_prompt_cache.snapshot()['entries'])

    def test_failed_new_prompt_and_cache_copy_memory_error_leave_no_partial_retention(self):
        self.model.fail = True
        with self.assertRaisesRegex(ValueError, 'Synthetic prompt'):
            self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual(0, self.engine._prepared_reference_cache.snapshot()['entries'])
        self.assertEqual(0, self.engine._clone_prompt_cache.snapshot()['entries'])
        self.model.fail = False
        with patch.object(cache, 'copy_clone_prompt', side_effect=MemoryError('Synthetic cache pressure')):
            prompt = self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual(24000, prompt[0].ref_spk_embedding[1].item())
        self.assertEqual(0, self.engine._prepared_reference_cache.snapshot()['charged_bytes'])
        self.assertEqual(0, self.engine._clone_prompt_cache.snapshot()['charged_bytes'])
        self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual(3, len(self.model.calls))

    def test_unknown_provider_prompt_stays_uncached_without_blocking_generation(self):
        with patch.object(self.model, 'create_voice_clone_prompt', side_effect=lambda **_kwargs: object()) as create:
            first = self.engine._get_clone_prompt('A', self.voices)
            second = self.engine._get_clone_prompt('B', self.voices)
        self.assertIsNot(first, second)
        self.assertEqual(2, create.call_count)
        self.assertEqual(0, self.engine._clone_prompt_cache.snapshot()['entries'])
        self.assertEqual(2, self.engine._clone_prompt_cache.snapshot()['bypasses'])
        self.assertEqual(1, self.engine._prepared_reference_cache.snapshot()['hits'])

    def test_warm_hit_copy_memory_error_rebuilds_and_retention_recovers(self):
        first = self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual(1, self.engine._clone_prompt_cache.snapshot()['entries'])
        retained = weakref.ref(next(iter(self.engine._clone_prompt_cache._entries.values()))[0][0].ref_spk_embedding)
        self.model.observe = lambda: self.assertIsNone(retained())
        def fail_copy(*_args, **_kwargs):
            raise MemoryError('Synthetic warm-copy pressure')
        try:
            with patch.object(cache, 'copy_clone_prompt', new=fail_copy):
                rebuilt = self.engine._get_clone_prompt('B', self.voices)
        finally:
            self.model.observe = None
        self.assertTrue(torch.equal(first[0].ref_spk_embedding, rebuilt[0].ref_spk_embedding))
        self.assertEqual(2, len(self.model.calls))
        self.assertEqual(0, self.engine._clone_prompt_cache.snapshot()['charged_bytes'])
        self.assertEqual(0, self.engine._prepared_reference_cache.snapshot()['charged_bytes'])
        recovered = self.engine._get_clone_prompt('A', self.voices)
        self.assertTrue(torch.equal(first[0].ref_code, recovered[0].ref_code))
        self.assertEqual(3, len(self.model.calls))
        self.assertEqual(1, self.engine._clone_prompt_cache.snapshot()['entries'])

    def test_prepared_copy_memory_error_releases_old_array_before_provider_retry(self):
        self.engine._get_clone_prompt('A', self.voices)
        class CopyPressure(np.ndarray):
            def copy(self, order='C'):
                raise MemoryError('Synthetic prepared-copy pressure')
        key, (value, charge) = next(iter(self.engine._prepared_reference_cache._entries.items()))
        view = value[0].view(CopyPressure)
        retained = weakref.ref(view)
        self.engine._prepared_reference_cache._entries[key] = ((view, value[1]), charge)
        del view, value
        self.voices['B']['ref_text'] = 'Different exact transcript'
        self.model.observe = lambda: self.assertIsNone(retained())
        try:
            result = self.engine._get_clone_prompt('B', self.voices)
        finally:
            self.model.observe = None
        self.assertEqual(2, len(self.model.calls))
        self.assertEqual('Different exact transcript', result[0].ref_text)
        self.assertEqual(1, self.engine._prepared_reference_cache.snapshot()['entries'])

    def test_non_scalar_public_provider_transcript_bypasses_prompt_keying(self):
        self.voices['A']['ref_text'] = ['Synthetic reference']
        for _ in range(2):
            result = self.engine._get_clone_prompt('A', self.voices)
            self.assertEqual(['Synthetic reference'], result[0].ref_text)
        self.assertEqual(2, len(self.model.calls))
        self.assertEqual(0, self.engine._clone_prompt_cache.snapshot()['entries'])
        self.assertEqual(2, self.engine._clone_prompt_cache.snapshot()['bypasses'])

    def test_loaded_code_map_includes_cache_and_is_stable_across_warm_hit_and_clear(self):
        from synthesis_recovery_serving import _get_aa2_loaded_code
        before = _get_aa2_loaded_code()
        self.assertEqual(cache._LOADED_RECOVERY_CODE_SHA256, before['clone_reference_cache'])
        self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual(before, _get_aa2_loaded_code())
        self.engine._get_clone_prompt('B', self.voices)
        self.assertEqual(before, _get_aa2_loaded_code())
        self.engine._clear_clone_reference_caches()
        self.assertEqual(before, _get_aa2_loaded_code())

    def test_both_retained_tiers_obey_actual_small_byte_and_count_budgets(self):
        self.engine._prepared_reference_cache = cache.ReferenceLRU(8, 60000)
        self.engine._clone_prompt_cache = cache.ReferenceLRU(2, 10000)
        for number in range(8):
            sf.write(self.reference, np.full(2400, number / 100), 24000, subtype='FLOAT')
            self.engine._get_clone_prompt('A', self.voices)
        prepared = self.engine._prepared_reference_cache.snapshot()
        prompts = self.engine._clone_prompt_cache.snapshot()
        self.assertLessEqual(prepared['charged_bytes'], 60000)
        self.assertLessEqual(prepared['entries'], 1)  # Byte bound precedes count8.
        self.assertGreater(prepared['evictions'], 0)
        self.assertLessEqual(prompts['entries'], 2)
        self.assertLessEqual(prompts['charged_bytes'], 10000)
        self.assertGreater(prompts['evictions'], 0)
        self.engine._clear_clone_reference_caches()
        self.assertEqual(0, self.engine._prepared_reference_cache.snapshot()['charged_bytes'])
        self.assertEqual(0, self.engine._clone_prompt_cache.snapshot()['charged_bytes'])

    def test_concurrent_labels_admit_only_one_shared_provider_preparation(self):
        self.model.entered, self.model.release = threading.Event(), threading.Event()
        started = threading.Event()
        def second():
            started.set()
            return self.engine._get_clone_prompt('B', self.voices)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.engine._get_clone_prompt, 'A', self.voices)
            try:
                self.assertTrue(self.model.entered.wait(3))
                other = executor.submit(second)
                self.assertTrue(started.wait(3))
                self.assertFalse(other.done())
            finally:
                self.model.release.set()
            self.assertTrue(torch.equal(first.result(3)[0].ref_code, other.result(3)[0].ref_code))
        self.assertEqual(1, len(self.model.calls))

    def test_cancelled_hit_and_cancellation_after_provider_cannot_skip_admission(self):
        self.engine._get_clone_prompt('A', self.voices)
        cancelled = threading.Event()
        @contextmanager
        def admit(**_kwargs):
            if cancelled.is_set():
                raise AudioGenerationCancelled()
            yield
        owner = AudioGenerationOwner('a' * 32, admit)
        with ensure_audio_generation_owner(owner):
            cancelled.set()
            with patch.object(tts, 'get_adapter_asset_snapshot', side_effect=AssertionError('cancelled disk read')):
                with self.assertRaises(AudioGenerationCancelled):
                    self.engine._get_clone_prompt('A', self.voices)
            cancelled.clear()
            self.engine._clear_clone_reference_caches()
            self.model.cancel = cancelled
            with self.assertRaises(AudioGenerationCancelled):
                self.engine._get_clone_prompt('A', self.voices)
        self.assertEqual(0, self.engine._prepared_reference_cache.snapshot()['entries'])
        self.assertEqual(0, self.engine._clone_prompt_cache.snapshot()['entries'])
