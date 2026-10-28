"""The #1650 MPS probe's pure parts; its MPS run is not testable here."""
import math
import unittest
from unittest.mock import patch

from experiments import mps_batch_padding_probe as probe


def result(ok, error='RuntimeError: nan', tokens=(9, 5)):
    return {'ok': ok, 'error': None if ok else error, 'outputs': [], 'token_lengths': list(tokens)}


class ProbeCaseTests(unittest.TestCase):
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


class ProbeReadingTests(unittest.TestCase):
    def results(self, sdpa_padded, eager_padded, singles=True, eager_loaded='eager',
                tokens=(9, 5), error='RuntimeError: nan'):
        def backend(padded, loaded):
            return {'loaded': {'<root>': loaded, 'talker': loaded},
                    'cases': {'padded_mixed': result(padded, error, tokens),
                              'equal_control': result(True, tokens=(7, 7)),
                              'single_1': result(singles, tokens=(5,))}}
        return {'sdpa': backend(sdpa_padded, 'sdpa'), 'eager': backend(eager_padded, eager_loaded)}

    def test_each_pre_registered_reading(self):
        self.assertEqual('cause_confirmed', probe.get_reading(self.results(False, True)))
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

    def test_loaded_backends_are_read_from_every_configured_module_once(self):
        from types import SimpleNamespace
        shared = SimpleNamespace(_attn_implementation='eager')
        modules = [('', SimpleNamespace(config=SimpleNamespace(_attn_implementation='eager'))),
                   ('talker', SimpleNamespace(config=shared)),
                   ('talker.layers.0', SimpleNamespace(config=shared)),
                   ('talker.code_predictor', SimpleNamespace(config=SimpleNamespace(_attn_implementation='sdpa'))),
                   ('codec', SimpleNamespace())]
        model = SimpleNamespace(model=SimpleNamespace(named_modules=lambda: iter(modules)))
        self.assertEqual({'<root>': 'eager', 'talker': 'eager', 'talker.code_predictor': 'sdpa'},
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
    def test_refuses_without_mps_before_loading_anything(self):
        import torch
        with patch('sys.argv', ['probe']), \
                patch.object(torch.backends.mps, 'is_available', return_value=False), \
                self.assertRaisesRegex(SystemExit, 'MPS is not available'):
            probe.main()


if __name__ == '__main__':
    unittest.main()
