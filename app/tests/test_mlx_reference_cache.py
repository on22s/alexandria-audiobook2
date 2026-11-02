"""MLX worker clone-reference caches (#1438), on CPU with stand-in mlx modules.

The fake model copies mlx-audio 0.6.1's access pattern exactly: it keys
`_icl_cache` by (transcript, (sample count, sample sum)), tests `in` before
reading, stores only after a miss, and calls `self.extract_speaker_embedding`
with the same array object it was handed.
"""
from pathlib import Path
import importlib
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np
import soundfile as sf

APP_DIR = Path(__file__).resolve().parent.parent


class FakeResult:
    def __init__(self, audio, sample_rate):
        self.audio, self.sample_rate = audio, sample_rate


class FakeQwen:
    sample_rate = 24000

    def __init__(self):
        self._icl_cache = {}
        self.encodes = self.embeds = 0
        self.used_codes = []
        self.fail = False

    def extract_speaker_embedding(self, audio, sr=24000):
        self.embeds += 1
        return np.array([float(audio[0])], np.float32)

    def generate(self, text, ref_audio, ref_text, lang_code, max_tokens):
        key = (ref_text, (ref_audio.size, float(ref_audio.sum())))
        codes = None
        if key in self._icl_cache:
            codes = self._icl_cache[key]
        if codes is None:
            self.encodes += 1
            codes = (np.array(ref_audio, np.float32), np.array([len(ref_text)], np.int32))
        if self.fail:
            raise RuntimeError('decode failed')
        if key not in self._icl_cache:
            self._icl_cache[key] = codes
        speaker = self.extract_speaker_embedding(ref_audio)
        self.used_codes.append(codes[0])
        yield FakeResult(np.full(240, 0.05 + 0.01 * float(speaker[0]), np.float32), 24000)


class ReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.loads = []
        self.models = []
        core = types.SimpleNamespace(random=types.SimpleNamespace(seed=lambda value: None),
                                     clear_cache=lambda: None, eval=lambda *values: None)
        mlx = types.ModuleType('mlx'); mlx.core = core

        def load_model(path):
            self.models.append(FakeQwen())
            return self.models[-1]

        def load_audio(path, sample_rate=24000):
            self.loads.append((path, sample_rate))
            return np.frombuffer(Path(path).read_bytes(), np.uint8).astype(np.float32)
        modules = {'mlx': mlx, 'mlx.core': core,
                   'mlx_audio': types.ModuleType('mlx_audio'),
                   'mlx_audio.tts': types.ModuleType('mlx_audio.tts'),
                   'mlx_audio.tts.utils': types.SimpleNamespace(load_model=load_model),
                   'mlx_audio.utils': types.SimpleNamespace(load_audio=load_audio)}
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start(); self.addCleanup(patcher.stop)
        sys.modules.pop('mlx_tts_worker', None)
        if str(APP_DIR) not in sys.path:
            sys.path.insert(0, str(APP_DIR))
        stdout = sys.stdout
        self.worker = importlib.import_module('mlx_tts_worker')
        self.assertIs(sys.stdout, stdout, 'importing the worker must not take over stdout')
        self.addCleanup(sys.modules.pop, 'mlx_tts_worker', None)
        self.count = 0

    def ref(self, name, data):
        path = self.tmp / name
        path.write_bytes(bytes(data))
        return str(path)

    def clone(self, ref_audio, ref_text='A synthetic reference line.', model='base'):
        self.count += 1
        output = self.tmp / f'out-{self.count}.wav'
        reply = self.worker.generate({'family': 'clone', 'model': model, 'text': 'Hello there.',
                                      'language': 'English', 'ref_audio': ref_audio, 'ref_text': ref_text,
                                      'seed': 1, 'max_tokens': 64, 'output': str(output)})
        return reply, sf.read(str(output), dtype='float32')[0]

    def test_identical_bytes_at_another_path_reuse_every_stage_and_render_the_same(self):
        first, audio_a = self.clone(self.ref('a.wav', [10, 20, 30, 40]))
        second, audio_b = self.clone(self.ref('copy-for-another-speaker.wav', [10, 20, 30, 40]))
        model = self.models[0]
        self.assertEqual(len(self.loads), 1)
        self.assertEqual((model.encodes, model.embeds), (1, 1))
        cache = second['reference_cache']
        self.assertEqual((cache['prepared_audio']['hits'], cache['prepared_audio']['misses']), (1, 1))
        self.assertEqual((cache['reference_codes']['hits'], cache['reference_codes']['misses']), (1, 1))
        self.assertEqual((cache['speaker_embedding']['hits'], cache['speaker_embedding']['misses']), (1, 1))
        np.testing.assert_array_equal(audio_a, audio_b)

    def test_transcript_change_reencodes_but_keeps_prepared_audio_and_embedding(self):
        path = self.ref('a.wav', [10, 20, 30, 40])
        self.clone(path)
        reply, _ = self.clone(path, ref_text='A synthetic reference line. ')
        self.assertEqual(self.models[0].encodes, 2)
        self.assertEqual(self.models[0].embeds, 1)
        self.assertEqual(reply['reference_cache']['reference_codes']['entries'], 2)

    def test_clips_that_collide_under_mlx_audio_key_get_their_own_codes(self):
        a, b = [10, 20, 30, 40], [40, 30, 20, 10]  # same length and same sum, different audio
        self.clone(self.ref('a.wav', a))
        self.clone(self.ref('b.wav', b))
        model = self.models[0]
        self.assertEqual(model.encodes, 2)
        np.testing.assert_array_equal(model.used_codes[1], np.array(b, np.float32))

    def test_known_bad_control_unscoped_mlx_cache_hands_b_the_codes_of_a(self):
        model = FakeQwen()  # not attached to the worker caches: mlx-audio's own dict
        for data in ([10, 20, 30, 40], [40, 30, 20, 10]):
            list(model.generate('x', np.array(data, np.float32), 'same', 'english', 8))
        self.assertEqual(model.encodes, 1)
        np.testing.assert_array_equal(model.used_codes[1], np.array([10, 20, 30, 40], np.float32))

    def test_sample_rate_is_part_of_the_prepared_key(self):
        caches = self.worker.ReferenceCaches()
        path = self.ref('a.wav', [1, 2, 3])
        load = mock.Mock(side_effect=lambda p, rate: np.zeros(rate // 1000, np.float32))
        caches.prepare(path, 24000, load); caches.prepare(path, 24000, load); caches.prepare(path, 16000, load)
        self.assertEqual(load.call_count, 2)

    def test_model_switch_clears_every_cache(self):
        path = self.ref('a.wav', [1, 2, 3, 4])
        self.clone(path, model='base')
        reply, _ = self.clone(path, model='other')
        stats = reply['reference_cache']
        self.assertEqual(len(self.loads), 2)
        self.assertEqual((self.models[1].encodes, self.models[1].embeds), (1, 1))
        self.assertEqual([stats[k]['entries'] for k in stats], [1, 1, 1])

    def test_failure_mid_render_leaves_no_codes_and_ends_the_scope(self):
        path = self.ref('a.wav', [1, 2, 3, 4])
        self.worker.get_model('base').fail = True
        with self.assertRaises(RuntimeError):
            self.clone(path)
        caches = self.worker._caches
        self.assertEqual(caches.icl.stats()['entries'], 0)
        self.assertIsNone(caches.icl_view.scope)
        self.assertIsNone(caches._current)
        self.assertFalse(list(self.tmp.glob('out-*')))
        self.models[0].fail = False
        self.clone(path)
        self.assertEqual(self.models[0].encodes, 2)

    def test_embedding_is_only_cached_for_the_array_the_worker_prepared(self):
        caches = self.worker.ReferenceCaches()
        model = FakeQwen(); caches.attach(model)
        prepared = np.array([5, 6], np.float32)
        caches.begin('digest', prepared)
        model.extract_speaker_embedding(np.array([5, 6], np.float32))  # equal values, other object
        model.extract_speaker_embedding(prepared); model.extract_speaker_embedding(prepared)
        caches.end()
        model.extract_speaker_embedding(prepared)  # outside a request
        self.assertEqual(model.embeds, 3)

    def test_count_and_byte_bounds_evict_oldest_and_skip_oversized(self):
        cache = self.worker.BoundedCache(2, 100)
        for key in 'abc':
            cache.put(key, np.zeros(10, np.uint8))
        self.assertEqual(cache.stats()['entries'], 2); self.assertNotIn('a', cache)
        cache.put('d', np.zeros(95, np.uint8))
        self.assertEqual(cache.stats()['charged_bytes'], 95); self.assertEqual(cache.stats()['evictions'], 3)
        cache.put('huge', np.zeros(101, np.uint8))
        self.assertNotIn('huge', cache); self.assertIn('d', cache)
        cache.clear()
        self.assertEqual((cache.stats()['entries'], cache.stats()['charged_bytes']), (0, 0))


if __name__ == '__main__':
    unittest.main()
