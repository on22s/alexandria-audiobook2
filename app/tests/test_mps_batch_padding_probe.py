"""The #1650 MPS probe's pure parts; its MPS run is not testable here."""
import math
import json
import sys
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments import mps_batch_padding_probe as probe


def result(ok, error='RuntimeError: probability tensor contains either inf, nan or element < 0', tokens=(9, 5)):
    return {'ok': ok, 'error': None if ok else error, 'outputs': [], 'token_lengths': list(tokens)}


class ProbeCaseTests(unittest.TestCase):
    def test_model_revision_is_resolved_once_and_must_be_identifiable(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp) / ('a' * 40)
            snapshot.mkdir()
            calls = []
            def download(**kwargs):
                calls.append(kwargs)
                return str(snapshot)
            self.assertEqual((str(snapshot), 'a' * 40), probe.resolve_model_snapshot(download))
            self.assertEqual([{'repo_id': probe.MODEL_ID}], calls)
            with self.assertRaisesRegex(ValueError, 'identifiable hub revision'):
                probe.resolve_model_snapshot(lambda **kwargs: temp)

    def test_padded_cases_differ_and_the_control_does_not(self):
        # Token lengths, not characters, decide padding; the probe records them
        # at run time. Here: every padded case differs, and one differs so much
        # that no tokenizer could make it equal.
        cases = {name: (texts, speakers) for name, texts, speakers in probe.get_probe_cases()}
        for name in ('padded_mixed', 'padded_short', 'padded_long'):
            texts, speakers = cases[name]
            self.assertEqual(2, len(texts))
            self.assertNotEqual(texts[0], texts[1], name)
        short, long = cases['padded_long'][0]
        self.assertGreaterEqual(len(long.split()) - len(short.split()), 15)
        texts, speakers = cases['equal_control']
        self.assertEqual(texts[0], texts[1])
        self.assertNotEqual(speakers[0], speakers[1])

    def test_every_padded_line_is_also_rendered_alone_once(self):
        cases = probe.get_probe_cases()
        singles = [(texts[0], speakers[0]) for name, texts, speakers in cases if name.startswith('single')]
        padded = [(t, s) for name, texts, speakers in probe.CASES if name.startswith('padded')
                  for t, s in zip(texts, speakers)]
        self.assertEqual(sorted(padded), sorted(singles))
        self.assertTrue(all(len(texts) == 1 for name, texts, _ in cases if name.startswith('single')))


class ProbeRecordTests(unittest.TestCase):
    def test_non_finite_or_empty_audio_is_never_ok(self):
        self.assertTrue(probe.get_case_result([[0.0, 0.5, -0.5]], 24000)['ok'])
        for bad in ([[0.0, math.nan]], [[0.0, math.inf]], [[]], []):
            with self.subTest(bad=bad):
                self.assertFalse(probe.get_case_result(bad, 24000)['ok'])

    def test_an_exception_is_recorded_with_its_text(self):
        record = probe.get_case_result([], 0, RuntimeError(
            'probability tensor contains either `inf`, `nan` or element < 0'))
        self.assertFalse(record['ok'])
        self.assertIn('probability tensor', record['error'])

    def test_a_batch_must_return_every_requested_waveform_at_a_valid_rate(self):
        self.assertFalse(probe.get_case_result([[0.1]], 24000, expected_count=2)['ok'])
        self.assertFalse(probe.get_case_result([[0.1], [0.2]], 0, expected_count=2)['ok'])
        self.assertTrue(probe.get_case_result([[0.1], [0.2]], 24000, expected_count=2)['ok'])
        for rate in ('24000', True, None, -1, math.nan):
            with self.subTest(rate=rate):
                self.assertEqual('output contract: invalid sample rate',
                                 probe.get_case_result([[0.1]], rate)['error'])
        self.assertEqual('output contract: wrong waveform count',
                         probe.get_case_result(None, 24000, expected_count=2)['error'])
        self.assertEqual('output contract: malformed waveforms',
                         probe.get_case_result([None, None], 24000, expected_count=2)['error'])


class ProbeReadingTests(unittest.TestCase):
    def test_load_and_measurement_failures_cannot_claim_model_verdicts(self):
        self.assertEqual('model_load_failure', probe.get_reading({
            'sdpa': {'load_error': 'RuntimeError: failed to load', 'cases': {}}}))
        self.assertEqual('inconclusive_memory', probe.get_reading({
            'eager': {'load_error': 'RuntimeError: out of memory', 'cases': {}}}))
        observed = self.results(False, False)
        for backend in probe.BACKENDS:
            observed[backend]['cases']['padded_mixed']['instrument_error'] = True
        self.assertEqual('instrument_failure', probe.get_reading(observed))

    def test_equal_tokens_in_one_case_do_not_discard_other_padding_evidence(self):
        observed = self.results(False, True)
        for backend in probe.BACKENDS:
            observed[backend]['cases']['padded_short'] = result(True, tokens=(6, 6))
        self.assertEqual('padding_backend_association', probe.get_reading(observed))
        observed = self.results(True, True)
        observed['sdpa']['cases']['padded_short'] = result(False, tokens=(6, 6))
        observed['eager']['cases']['padded_short'] = result(True, tokens=(6, 6))
        self.assertEqual('control_failure', probe.get_reading(observed))

    def results(self, sdpa_padded, eager_padded, singles=True, eager_loaded='eager',
                tokens=(9, 5), error='RuntimeError: probability tensor contains either inf, nan or element < 0'):
        def backend(padded, loaded):
            cases = {}
            for name, _, _ in probe.get_probe_cases():
                if name.startswith('padded'):
                    cases[name] = result(padded, error, tokens)
                elif name == 'equal_control':
                    cases[name] = result(True, tokens=(7, 7))
                else:
                    cases[name] = result(singles, tokens=(5,))
            return {'loaded': {'Qwen3TTSTalkerAttention:talker': loaded,
                               'Qwen3TTSAttention:predictor': loaded},
                    'cases': cases}
        return {'sdpa': backend(sdpa_padded, 'sdpa'), 'eager': backend(eager_padded, eager_loaded)}

    def test_each_pre_registered_reading(self):
        self.assertEqual('padding_backend_association', probe.get_reading(self.results(False, True)))
        self.assertEqual('padding_not_whole_story', probe.get_reading(self.results(False, False)))
        self.assertEqual('not_reproduced', probe.get_reading(self.results(True, True)))
        self.assertEqual('suspect_probe', probe.get_reading(self.results(True, False)))
        # A single-line failure outranks the padded readings: the cap alone would not be enough.
        self.assertEqual('single_line_failure', probe.get_reading(self.results(False, True, singles=False)))

    def test_the_instrument_rejects_runs_that_did_not_test_the_hypothesis(self):
        # Each of these would otherwise read as evidence about padding.
        self.assertEqual('backend_not_applied',
                         probe.get_reading(self.results(False, False, eager_loaded='sdpa')))
        self.assertEqual('padding_not_exercised',
                         probe.get_reading(self.results(True, True, tokens=(6, 6))))
        self.assertEqual('inconclusive_memory', probe.get_reading(
            self.results(False, True, error='RuntimeError: MPS backend out of memory')))

    def test_incomplete_or_misidentified_runs_cannot_claim_an_association(self):
        for loaded in ({}, {'Qwen3TTSTalkerAttention:talker': None},
                       {'Qwen3TTSTalkerAttention:talker': 'sdpa',
                        'Qwen3TTSAttention:predictor': None},
                       {'Qwen3TTSTalkerAttention:talker': 'eager',
                        'Qwen3TTSAttention:predictor': 'eager'}):
            with self.subTest(loaded=loaded):
                observed = self.results(False, True)
                observed['sdpa']['loaded'] = loaded
                self.assertEqual('backend_not_applied', probe.get_reading(observed))
        observed = self.results(False, True)
        del observed['eager']['cases']['padded_short']
        self.assertEqual('incomplete_cases', probe.get_reading(observed))
        self.assertEqual('unexpected_failure', probe.get_reading(
            self.results(False, True, error='RuntimeError: unrelated error')))
        broken = self.results(False, True)
        broken['sdpa']['cases']['padded_mixed']['token_lengths'] = None
        self.assertEqual('instrument_failure', probe.get_reading(broken))
        broken = self.results(False, True)
        broken['sdpa']['cases']['padded_mixed']['error'] = 'output contract: wrong waveform count'
        self.assertEqual('output_contract_failure', probe.get_reading(broken))
        for control in (result(False, tokens=(7, 7)), result(True, tokens=(7, 8))):
            with self.subTest(control=control):
                observed = self.results(False, True)
                observed['sdpa']['cases']['equal_control'] = control
                self.assertEqual('control_failure', probe.get_reading(observed))

    def test_loaded_backends_are_read_from_every_configured_module_once(self):
        from types import SimpleNamespace
        talker = type('Qwen3TTSTalkerAttention', (), {})
        predictor = type('Qwen3TTSAttention', (), {})
        first, second = talker(), predictor()
        first.config = SimpleNamespace(_attn_implementation='eager')
        second.config = SimpleNamespace(_attn_implementation='sdpa')
        modules = [('talker.layer', first), ('talker.code_predictor', second),
                   ('codec', SimpleNamespace())]
        model = SimpleNamespace(model=SimpleNamespace(named_modules=lambda: iter(modules)))
        self.assertEqual({'Qwen3TTSTalkerAttention:talker.layer': 'eager',
                          'Qwen3TTSAttention:talker.code_predictor': 'sdpa'},
                         probe.get_loaded_backends(model))

    def test_token_lengths_use_the_same_prompt_qwen_tts_builds(self):
        from types import SimpleNamespace
        seen = []
        def tokenize(prompts):
            seen.extend(prompts)
            return [SimpleNamespace(shape=(1, len(text.split()))) for text in prompts]
        model = SimpleNamespace(_build_assistant_text=lambda text: f'<a> {text} </a>',
                                _tokenize_texts=tokenize)
        self.assertEqual([3, 5], probe.get_token_lengths(model, ['one', 'one two three']))
        self.assertEqual(['<a> one </a>', '<a> one two three </a>'], seen)


class ProbeRefusalTests(unittest.TestCase):
    def test_load_failure_writes_final_artifact_for_either_arm(self):
        for failing_backend in (*probe.BACKENDS, 'measurement', 'backend_inspection', 'seed'):
            with self.subTest(backend=failing_backend), tempfile.TemporaryDirectory() as temp:
                out = Path(temp) / 'result.json'
                model = SimpleNamespace(generate_custom_voice=lambda **kwargs: ([[0.1]] * len(kwargs['text']), 24000))
                def load(*args, **kwargs):
                    if kwargs['attn_implementation'] == failing_backend:
                        raise RuntimeError('model load broke')
                    return model
                torch = SimpleNamespace(__version__='test', float32='float32',
                    backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: True)),
                    manual_seed=lambda seed: None, mps=SimpleNamespace(empty_cache=lambda: None))
                if failing_backend == 'seed':
                    def fail_seed(seed):
                        raise RuntimeError('seed setup broke')
                    torch.manual_seed = fail_seed
                def write(value, path):
                    Path(path).write_text(json.dumps(value))
                modules = {'torch': torch, 'qwen_tts': SimpleNamespace(Qwen3TTSModel=SimpleNamespace(from_pretrained=load)),
                    'huggingface_hub': SimpleNamespace(snapshot_download=lambda **kwargs: temp),
                    'transformers': SimpleNamespace(__version__='test'),
                    'experiments.provenance': SimpleNamespace(provenance=lambda *args: {}),
                    'utils': SimpleNamespace(atomic_json_write=write)}
                with patch.dict(sys.modules, modules), patch('sys.argv', ['probe', '--out', str(out)]), \
                     patch.object(probe, 'resolve_model_snapshot', return_value=(temp, 'a' * 40)), \
                     patch.object(probe, 'get_loaded_backends', side_effect=
                         RuntimeError('backend inspection broke') if failing_backend == 'backend_inspection'
                         else lambda model: {
                         'Qwen3TTSTalkerAttention:talker': 'sdpa',
                         'Qwen3TTSAttention:predictor': 'sdpa'}), \
                     patch.object(probe, 'get_token_lengths',
                         side_effect=RuntimeError('token measurement broke')
                         if failing_backend == 'measurement' else None,
                         return_value=[9, 5]):
                    probe.main()
                artifact = json.loads(out.read_text())
                self.assertEqual('complete', artifact['status'])
                if failing_backend in ('measurement', 'seed'):
                    self.assertEqual('instrument_failure', artifact['reading'])
                    for backend in probe.BACKENDS:
                        records = artifact['results'][backend]['cases']
                        self.assertEqual(len(probe.get_probe_cases()), len(records))
                        self.assertTrue(all(record['instrument_error'] for record in records.values()))
                elif failing_backend == 'backend_inspection':
                    self.assertEqual('instrument_failure', artifact['reading'])
                    self.assertIn('backend inspection broke', artifact['results']['sdpa']['instrument_error'])
                else:
                    self.assertEqual('model_load_failure', artifact['reading'])
                    self.assertIn('model load broke', artifact['results'][failing_backend]['load_error'])

    def test_refuses_without_mps_before_loading_anything(self):
        torch = SimpleNamespace(backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)))
        with patch('sys.argv', ['probe']), \
                patch.dict(sys.modules, {'torch': torch}), \
                self.assertRaisesRegex(SystemExit, 'MPS is not available'):
            probe.main()


if __name__ == '__main__':
    unittest.main()
