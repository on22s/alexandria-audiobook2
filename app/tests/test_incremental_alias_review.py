import copy
import json
import sys
from pathlib import Path
import tempfile
import unittest
from experiments.incremental_alias_review import apply_reviews, get_candidates, get_checkpoint, validate_verdict


def row(text, speaker='Alpha'):
    return {'text': text, 'type': 'SPOKEN', 'speaker': speaker}


class IncrementalAliasReviewTests(unittest.TestCase):
    def setUp(self):
        self.source = [row('Hello.'), row('Goodbye.')]
        self.reference = [row('Hello.', 'Beta'), row('Goodbye.', 'Beta')]
        self.prediction = [dict(self.source[0], entry_index=0)]
        self.calls = []

    def review(self, candidate, mode, prompt):
        self.calls.append(mode)
        return {'same_identity': None, 'reason': 'Insufficient evidence.'}

    def candidates(self, prediction=None):
        return get_candidates(self.source, prediction or self.prediction, self.reference, True)

    def test_validation(self):
        with self.assertRaises(ValueError):
            get_candidates(self.source, self.prediction, self.reference)
        for bad in ([dict(self.prediction[0], entry_index=1)], self.prediction * 2,
                    [dict(self.prediction[0], text='Changed')],
                    [dict(self.prediction[0], type='NARRATOR')]):
            with self.assertRaises(ValueError):
                self.candidates(bad)
        full = [dict(self.source[i], entry_index=i) for i in (1, 0)]
        self.assertTrue(self.candidates(full)[2])

    def test_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'checkpoint'
            path.write_text(json.dumps(self.prediction[0]) + '\n{"entry')
            self.assertEqual(get_checkpoint(path, True), self.prediction)
            with self.assertRaises(ValueError):
                get_checkpoint(path)
            path.write_text('{bad}\n')
            with self.assertRaises(ValueError):
                get_checkpoint(path, True)

    def test_ambiguous(self):
        candidates, skipped, _ = get_candidates(self.source, self.prediction,
                                               self.reference + [self.reference[0]], True)
        self.assertEqual(candidates, [])
        self.assertEqual(skipped[0]['reason'], 'ambiguous')

    def test_restart_final_and_immutability(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.candidates()
            before = copy.deepcopy((self.source, self.prediction, self.reference))
            self.assertEqual(apply_reviews(tmp, *args, {}, self.review)['phase'], 'provisional')
            apply_reviews(tmp, *args, {}, self.review)
            self.assertEqual(len(self.calls), 2)
            full = self.prediction + [dict(self.source[1], entry_index=1)]
            report = apply_reviews(tmp, *self.candidates(full), {}, self.review)
            self.assertEqual(len(self.calls), 4)
            self.assertEqual(report['phase'], 'reconciled')
            self.assertEqual(len(report['reviews']), 2)
            self.assertEqual(before, (self.source, self.prediction, self.reference))
            apply_reviews(tmp, *self.candidates(full), {}, self.review)
            self.assertEqual(len(self.calls), 4)

    def test_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            def interrupt(candidate, mode, prompt):
                if mode == 'low':
                    raise KeyboardInterrupt()
                return self.review(candidate, mode, prompt)
            with self.assertRaises(KeyboardInterrupt):
                apply_reviews(tmp, *self.candidates(), {}, interrupt)
            report = apply_reviews(tmp, *self.candidates(), {}, self.review)
            self.assertEqual(self.calls, ['none', 'low'])
            self.assertEqual(report['attempts'], 3)

    def test_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidates, skipped, complete = self.candidates()
            apply_reviews(tmp, candidates, skipped, complete, {}, self.review)
            apply_reviews(tmp, candidates, skipped, complete, {'model': 'changed'}, self.review)
            apply_reviews(tmp, candidates, skipped, complete, {}, self.review, prompt='changed')
            for key in ('labels', 'context', 'reference_context'):
                changed = copy.deepcopy(candidates)
                changed[0][key] = ['changed']
                apply_reviews(tmp, changed, skipped, complete, {}, self.review)
            self.assertEqual(len(self.calls), 12)

    def test_failure_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(20):
                report = apply_reviews(tmp, *self.candidates(), {}, lambda *a: None)
            self.assertEqual(report['attempts'], 40)
            report = apply_reviews(tmp, *self.candidates(), {}, self.review)
            self.assertEqual(self.calls, [])
            self.assertTrue(all('error' in result for result in report['reviews']))
            self.assertEqual(json.loads((Path(tmp) / 'cache.json').read_text())['reviews'], {})

    def test_cap_and_verdict(self):
        source = [row(str(i), 'Alpha' + str(i)) for i in range(25)]
        prediction = [dict(value, entry_index=i) for i, value in enumerate(source)]
        reference = [dict(value, speaker='Beta' + str(i)) for i, value in enumerate(source)]
        self.assertEqual(len(get_candidates(source, prediction, reference)[0]), 20)
        for bad in (None, {}, {'same_identity': 1, 'reason': 'x'}, {'same_identity': False, 'reason': ''}):
            with self.assertRaises(ValueError):
                validate_verdict(bad)
        for value in (True, False, None):
            validate_verdict({'same_identity': value, 'reason': 'Evidence.'})

    def test_cli_opt_in_and_local_lock(self):
        from unittest.mock import patch, MagicMock
        from experiments import incremental_alias_review as runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, value in (('source', self.source), ('reference', self.reference),
                                ('config', {'llm_local': {'model_name': 'synthetic',
                                 'base_url': 'http://localhost:1234/v1', 'context_length': 4096}})):
                (root / name).write_text(json.dumps(value))
            prediction = root / 'prediction'
            prediction.write_text(json.dumps(self.prediction[0]) + '\n')
            argv = ['runner']
            for name in ('source', 'reference', 'config', 'prediction', 'output'):
                argv += ['--' + name, str(root / name)]
            with patch.object(sys, 'argv', argv), self.assertRaises(SystemExit):
                runner.main()
            self.assertFalse((root / 'output').exists())
            client = MagicMock()
            verdict = {'same_identity': False, 'reason': 'Different explicit identities.'}
            with patch.object(sys, 'argv', argv + ['--allow-network', '--allow-partial']), \
                    patch('llm_provider.make_llm_client', return_value=client), \
                    patch('generate_script.call_llm_for_object', return_value=verdict) as call, \
                    patch('experiments.gpu_guard.acquire_gpu_lock', return_value='lock') as acquire, \
                    patch('experiments.gpu_guard.release_gpu_lock') as release:
                self.assertEqual(runner.main(), 0)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(runner.main(), 0)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(acquire.call_count, 2)
                release.assert_called_with('lock')
                client.close.side_effect = RuntimeError('close failed')
                with self.assertRaises(RuntimeError):
                    runner.main()
                self.assertEqual(release.call_count, 3)
            self.assertEqual(json.loads((root / 'output/report.json').read_text())['reviews'][0]['verdict'], verdict)
            self.assertEqual(prediction.read_text(), json.dumps(self.prediction[0]) + '\n')


    def test_utf8_cli_real_request_budgets_and_cached_replay_on_non_utf8_locale(self):
        from types import SimpleNamespace as NS
        from unittest.mock import patch
        from experiments import incremental_alias_review as runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = [row('こんにちは。', 'アルファ')]
            reference = [row('こんにちは。', 'ベータ')]
            config = {'llm_local': {'model_name': 'モデル',
                      'base_url': 'http://localhost:1234/v1', 'context_length': 4096}}
            for name, value in (('source', source), ('reference', reference), ('config', config)):
                (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            (root / 'prediction').write_text(json.dumps(dict(source[0], entry_index=0),
                                            ensure_ascii=False) + '\n', encoding='utf-8')
            argv = ['runner', '--allow-network']
            for name in ('source', 'reference', 'config', 'prediction', 'output'):
                argv += ['--' + name, str(root / name)]
            from lmstudio_settings import get_active_llm_config
            previous_settings = {'profile_digest': runner.get_digest(get_active_llm_config(config)),
                                 'max_tokens': 512, 'temperature': 0,
                                 'reasoning_allowance': 512, 'structured_output': 'off'}
            previous_candidates = runner.get_candidates(source, [dict(source[0], entry_index=0)], reference)
            apply_reviews(root / 'output', *previous_candidates, previous_settings,
                          lambda *args: {'same_identity': False, 'reason': '旧ポリシーの結果。'})
            requests = []
            verdict = {'same_identity': None, 'reason': '証拠が足りません。'}

            def create(**kwargs):
                requests.append(kwargs)
                low = kwargs['extra_body']['reasoning_effort'] == 'low'
                exhausted = low and kwargs['max_tokens'] < 1024
                return NS(choices=[NS(message=NS(
                    content='' if exhausted else json.dumps(verdict, ensure_ascii=False),
                    reasoning_content='thinking' if exhausted else ''),
                    finish_reason='length' if exhausted else 'stop')],
                    usage=NS(prompt_tokens=100, completion_tokens=512 if exhausted else 30,
                             completion_tokens_details=None))

            client = NS(chat=NS(completions=NS(create=create)), close=lambda: None)
            original_open = Path.open

            def locale_open(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
                if 'b' not in mode and encoding is None:
                    encoding = 'cp1252'
                return original_open(path, mode, buffering, encoding, errors, newline)

            with patch.object(sys, 'argv', argv), patch.object(Path, 'open', locale_open), \
                    patch('llm_provider.make_llm_client', return_value=client), \
                    patch('experiments.gpu_guard.acquire_gpu_lock', return_value=None), \
                    patch('experiments.gpu_guard.release_gpu_lock'), \
                    patch('generate_script.get_response_log_path', return_value=str(root / 'responses.log')):
                self.assertEqual(runner.main(), 0)
                self.assertEqual([(r['extra_body']['reasoning_effort'], r['max_tokens'])
                                  for r in requests], [('none', 512), ('low', 1024)])
                self.assertTrue(all(r['model'] == 'モデル' for r in requests))
                report = json.loads((root / 'output/report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['candidates'][0]['context'][0]['text'], 'こんにちは。')
                self.assertEqual(report['candidates'][0]['labels'], ['アルファ', 'ベータ'])
                self.assertTrue(all(r['verdict'] == verdict for r in report['reviews']))
                self.assertEqual(runner.main(), 0)
                self.assertEqual(len(requests), 2)
                self.assertEqual(json.loads((root / 'output/cache.json').read_text(encoding='utf-8'))['attempts'], 4)

    def test_completion_budget_policy_invalidates_prior_cache(self):
        from experiments import incremental_alias_review as runner
        with tempfile.TemporaryDirectory() as tmp:
            previous = {'max_tokens': 512, 'reasoning_allowance': 512}
            apply_reviews(tmp, *self.candidates(), previous, self.review)
            current = dict(previous, completion_budgets=runner.COMPLETION_BUDGETS)
            report = apply_reviews(tmp, *self.candidates(), current, self.review)
            self.assertEqual(self.calls, ['none', 'low', 'none', 'low'])
            self.assertEqual(report['attempts'], 4)
            apply_reviews(tmp, *self.candidates(), current, self.review)
            self.assertEqual(len(self.calls), 4)
