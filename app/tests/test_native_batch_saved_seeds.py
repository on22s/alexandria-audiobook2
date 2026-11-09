"""Effective seeds partition native inference calls without losing published rows."""
import contextlib
import copy
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
import tts
from tests.test_batch_style_timeline import get_engine, get_torch


class NativeBatchSavedSeedTests(unittest.TestCase):
    def render(self, family, seed=777, *, versions=None, batch_seed=-1,
               public=False, capped=False, ratio=False, fail_seed=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = root/'adapter'
            adapter.mkdir()
            chunks = [{'index': index, 'speaker': 'A', 'text': text, 'instruct': ''}
                      for index, text in ((7, 'seven'), (2, 'two'), (9, 'n'), (0, 'initial'))]
            config = {'A': {'type': family, 'voice': 'Ryan', 'seed': seed,
                            'adapter_path': str(adapter), 'ref_audio': 'synthetic.wav',
                            'ref_text': 'Synthetic reference'}}
            if versions is not None:
                config['A']['versions'] = {
                    'middle': {'type': family, 'seed': versions[0]},
                    'late': {'type': family, 'seed': versions[1]}}
                config['A']['version_timeline'] = [
                    {'from_index': 4, 'version_id': 'middle'},
                    {'from_index': 8, 'version_id': 'late'}]
            before = copy.deepcopy((chunks, config))
            calls, pending_seed = [], []
            torch = get_torch()
            torch.manual_seed.side_effect = pending_seed.append
            values = {chunk['text']: (chunk['index']+1)/32 for chunk in chunks}

            def generate(**arguments):
                selected_seed = pending_seed.pop() if pending_seed else None
                texts = list(arguments['text'])
                calls.append((selected_seed, texts))
                if selected_seed == fail_seed and fail_seed is not None:
                    raise RuntimeError('Synthetic seeded model failure')
                return [np.full(240, values[text.rstrip('.')], dtype=np.float32)
                        for text in texts], 24000

            model = SimpleNamespace(generate_custom_voice=generate,
                                    generate_voice_clone=generate,
                                    _tokenize_texts=lambda texts: texts)
            prompt = [SimpleNamespace(ref_code=None, ref_text='Synthetic reference')]
            engine = get_engine()
            engine._init_local_custom = Mock(return_value=model)
            engine._init_local_clone = Mock(return_value=model)
            engine._get_clone_prompt = Mock(return_value=prompt)
            engine._ensure_local_lora_generation = Mock(return_value=(model, prompt))
            if capped:
                engine._sub_batch_enabled = False
                engine._sub_batch_max_items = 2
                engine._build_sub_batches = tts.TTSEngine._build_sub_batches.__get__(engine)
                engine._estimate_max_batch_size.return_value = 4
            if ratio:
                engine._sub_batch_enabled = True
                engine._sub_batch_max_items = 0
                engine._sub_batch_min_size = 1
                engine._sub_batch_ratio = 2
                engine._build_sub_batches = tts.TTSEngine._build_sub_batches.__get__(engine)
                engine._estimate_max_batch_size.return_value = 4
            for chunk in chunks:
                (root/f"temp_batch_{chunk['index']}.wav").write_bytes(b'prior output')
            with patch.dict('sys.modules', {'torch': torch}), \
                    contextlib.redirect_stdout(io.StringIO()):
                if public:
                    result = engine.generate_batch(chunks, config, str(root), batch_seed)
                else:
                    result = getattr(engine, '_local_batch_'+family)(
                        chunks, config, str(root), batch_seed)
            self.assertEqual(before, (chunks, config))
            self.assertEqual([], pending_seed)
            completed = set(result['completed'])
            failed = {index for index, _ in result['failed']}
            self.assertEqual({0, 2, 7, 9}, completed | failed)
            self.assertFalse(completed & failed)
            for chunk in chunks:
                path = root/f"temp_batch_{chunk['index']}.wav"
                if chunk['index'] in completed:
                    audio, rate = sf.read(path)
                    self.assertEqual((240, 24000), (len(audio), rate))
                    np.testing.assert_allclose(audio, values[chunk['text']])
                else:
                    self.assertEqual(b'prior output', path.read_bytes())
            self.assertEqual([], list(root.glob('*.pending.*')))
            return calls, result

    def test_homogeneous_saved_seeds_keep_native_sub_batching(self):
        for family in ('custom', 'clone', 'lora'):
            for seed in (777, '17', 0, -1):
                with self.subTest(family=family, seed=seed):
                    calls, result = self.render(family, seed)
                    self.assertEqual([], result['failed'])
                    expected = int(seed) if int(seed) >= 0 else None
                    self.assertEqual([(expected, ['n', 'two']),
                                      (expected, ['seven', 'initial'])], calls)

    def test_version_selected_seeds_partition_sorted_original_chunks(self):
        for family in ('custom', 'clone', 'lora'):
            for public in (False, True):
                with self.subTest(family=family, public=public):
                    calls, result = self.render(family, versions=(23, 0), public=public)
                    self.assertEqual([], result['failed'])
                    self.assertEqual([(0, ['n'+('.' if public else '')]),
                                      (777, ['two'+('.' if public else '')]),
                                      (23, ['seven'+('.' if public else '')]),
                                      (777, ['initial'+('.' if public else '')])], calls)

    def test_random_versions_are_not_forced_to_a_pinned_neighbour_seed(self):
        for family in ('custom', 'clone', 'lora'):
            with self.subTest(family=family):
                calls, result = self.render(family, versions=(-1, -1))
                self.assertEqual([], result['failed'])
                self.assertEqual([(None, ['n']), (777, ['two']),
                                  (None, ['seven']), (777, ['initial'])], calls)

    def test_explicit_batch_override_preserves_original_sub_batches(self):
        for family in ('custom', 'clone', 'lora'):
            for override in (0, 42):
                with self.subTest(family=family, override=override):
                    calls, result = self.render(family, versions=(23, -1), batch_seed=override)
                    self.assertEqual([], result['failed'])
                    self.assertEqual([(override, ['n', 'two']),
                                      (override, ['seven', 'initial'])], calls)

    def test_disabled_ratio_splitting_still_honors_manual_item_cap(self):
        for family in ('custom', 'clone', 'lora'):
            with self.subTest(family=family):
                calls, result = self.render(family, capped=True)
                self.assertEqual([], result['failed'])
                self.assertEqual([2, 2], [len(texts) for _, texts in calls])
                self.assertEqual([777, 777], [seed for seed, _ in calls])

    def test_seed_specific_failure_preserves_prior_output_and_other_results(self):
        for family in ('custom', 'clone', 'lora'):
            with self.subTest(family=family):
                _, result = self.render(family, versions=(23, 0), fail_seed=23)
                self.assertEqual({0, 2, 9}, set(result['completed']))
                self.assertEqual([7], [index for index, _ in result['failed']])
                self.assertIn('Synthetic seeded model failure', result['failed'][0][1])

    def test_malformed_saved_seed_retains_unseeded_fallback_without_aborting_other_rows(self):
        for family in ('custom', 'clone', 'lora'):
            for seed in ('bad', None, {}, float('inf')):
                for public in (False, True):
                    with self.subTest(family=family, seed=seed, public=public):
                        calls, result = self.render(family, versions=(seed, 0), public=public)
                        self.assertEqual([], result['failed'])
                        suffix = '.' if public else ''
                        self.assertEqual([(0, ['n'+suffix]), (777, ['two'+suffix]),
                                          (None, ['seven'+suffix]), (777, ['initial'+suffix])], calls)

    def test_real_length_partitions_are_only_refined_at_seed_changes(self):
        for family in ('custom', 'clone', 'lora'):
            for versions, override, first_seed, later_seed in (
                    (None, -1, 777, 777), ((777, 0), -1, 0, 777),
                    ((23, 0), 42, 42, 42)):
                with self.subTest(family=family, versions=versions, override=override):
                    calls, result = self.render(family, versions=versions,
                                                batch_seed=override, ratio=True)
                    self.assertEqual([], result['failed'])
                    self.assertEqual([(first_seed, ['n']), (later_seed, ['two', 'seven']),
                                      (later_seed, ['initial'])], calls)
