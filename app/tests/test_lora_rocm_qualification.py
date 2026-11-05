import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'fork-adoption-evidence/1710/rocm-20261105'


def load_evidence_module(name, filename):
    specification = importlib.util.spec_from_file_location(name, EVIDENCE / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


reducer = load_evidence_module('rocm_numeric_reducer', 'reduce-numeric.py')
metrics = load_evidence_module('rocm_qualification_metrics', 'qualification_metrics.py')


class RocmNumericReplayTests(unittest.TestCase):
    def make_values(self):
        values = {precision + '_' + arm: np.zeros((6, 3072), dtype=np.float32)
                  for precision in ('fp32', 'bf16') for arm in ('base', 'merged', 'unmerged')}
        values['fp32_merged'][:] = 1
        values['fp32_unmerged'][:] = 1
        values['bf16_merged'][:] = 1.2
        values['bf16_unmerged'][:] = 1.05
        return values

    def test_known_effects_use_fp32_merged_minus_base_reference(self):
        result = reducer.get_numeric_comparison(self.make_values())
        self.assertTrue(result['qualified'])
        self.assertEqual(12, len(result['records']))
        self.assertAlmostEqual(.2, result['median_relative_l2_error']['merged'], places=5)
        self.assertAlmostEqual(.05, result['median_relative_l2_error']['unmerged'], places=5)
        self.assertAlmostEqual(1.05, result['records'][1]['retention'], places=5)
        self.assertAlmostEqual(1, result['records'][1]['cosine'], places=5)

    def test_swapped_arms_fail_the_lower_median_gate(self):
        values = self.make_values()
        values['bf16_merged'], values['bf16_unmerged'] = values['bf16_unmerged'], values['bf16_merged']
        result = reducer.get_numeric_comparison(values)
        self.assertFalse(result['qualified'])
        self.assertFalse(result['gates']['lower_median_relative_error'])

    def test_one_worse_line_refuses_even_with_lower_median(self):
        values = self.make_values()
        values['bf16_unmerged'][0] = 1.3
        result = reducer.get_numeric_comparison(values)
        self.assertTrue(result['gates']['lower_median_relative_error'])
        self.assertFalse(result['gates']['no_line_more_than_20_percent_worse'])
        self.assertFalse(result['qualified'])

    def test_missing_nonfinite_wrong_shape_and_absent_effect_refuse(self):
        for mutation in ('missing', 'nonfinite', 'shape', 'zero_reference', 'zero_candidate'):
            with self.subTest(mutation=mutation):
                values = self.make_values()
                if mutation == 'missing':
                    del values['bf16_base']
                elif mutation == 'nonfinite':
                    values['bf16_base'][0, 0] = np.nan
                elif mutation == 'shape':
                    values['bf16_base'] = values['bf16_base'][:5]
                elif mutation == 'zero_reference':
                    values['fp32_merged'][:] = 0
                else:
                    values['bf16_unmerged'][:] = 0
                with self.assertRaises(ValueError):
                    reducer.get_numeric_comparison(values)

    def test_committed_public_logits_reproduce_the_numeric_gate_on_cpu(self):
        result = reducer.get_archived_comparison(EVIDENCE)
        self.assertTrue(result['qualified'])
        self.assertEqual(12, len(result['records']))

    def test_portable_source_identity_records_both_historical_digests(self):
        record = json.loads((EVIDENCE / 'results.json').read_text(encoding='utf-8'))
        identity = record['measurement_source_identity']
        self.assertEqual('sha256-lf-v1', identity['scheme'])
        self.assertEqual('app/tts.py', identity['path'])
        self.assertEqual(record['measurement_source_sha256'], identity['captured_bytes_sha256'])
        self.assertRegex(identity['canonical_sha256'], r'^[0-9a-f]{64}$')
        self.assertNotEqual(identity['canonical_sha256'], identity['captured_bytes_sha256'])
        canonical = b'first line\nsecond line\n'
        for representation in (canonical, canonical.replace(b'\n', b'\r\n')):
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), hashlib.sha256(
                representation.replace(b'\r\n', b'\n')).hexdigest())

    def test_identical_embedding_conditions_cannot_qualify_separation(self):
        arms = {arm: {'samples': [{'text_id': text_id, 'seed': seed,
                                  'speaker_embedding': [1, 0]}
                                 for text_id in range(6) for seed in (1710, 2710)]}
                for arm in ('no_lora', 'unmerged')}
        result = metrics.get_identity_separation(arms)
        self.assertEqual(24, result['trials'])
        self.assertEqual(.5, result['balanced_accuracy'])
        self.assertEqual(0, result['mean_signed_margin'])
        self.assertFalse(result['qualified_to_separate_serving_conditions'])
