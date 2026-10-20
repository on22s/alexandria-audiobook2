"""Shipping conclusions must come from one explicitly paired serving run."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments import shipping_readiness as runner


class ShippingCohortTests(unittest.TestCase):
    def artifact(self, model='stack-a', arms=('base', 'lora')):
        return {'meta': {'experiment': 'lora_serving_eval', 'model': model, 'validation': 'ok',
                         'decoding': {'arms': list(arms)}, 'gold_files': {'fixture': 'known hash'}},
                'rows': [{'arm': arm, 'id': 'grimgar03:0', 'line': 'A line.', 'expected': 'ALICE',
                          'predicted': 'BOB', 'correct': False} for arm in arms]}

    def run_cli(self, ledger, out, artifact=None):
        argv = [runner.__file__, '--out', str(out)]
        if artifact is not None:
            argv += ['--artifact', str(artifact)]
        with patch.object(runner, 'LEDGER', str(ledger)), patch.object(sys, 'argv', argv), \
                patch.object(runner, 'leads_for', return_value=set()), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runner.main()

    def test_conflicting_complete_and_partial_runs_require_explicit_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); out = root / 'readiness.json'; out.write_text('previous result')
            paired = root / 'lora_serving_eval__a.json'; paired.write_text(json.dumps(self.artifact()))
            other = self.artifact('stack-b', ('base',)); other['rows'][0]['correct'] = True
            (root / 'lora_serving_eval__b.json').write_text(json.dumps(other))
            with self.assertRaises(SystemExit):
                self.run_cli(root, out)
            self.assertEqual('previous result', out.read_text())
            self.run_cli(root, out, paired)
            result = json.loads(out.read_text())
            self.assertEqual({'base': 0., 'lora': 0., 'gained': 0, 'lost': 0, 'p': 1.}, result['pooled'])
            self.assertEqual([], result['blockers'])
            self.assertEqual('stack-a', result['source_artifact']['model'])
            self.assertEqual(str(paired.resolve()), result['source_artifact']['path'])

    def test_incomplete_duplicate_or_unmatched_rows_refuse_without_overwriting_output(self):
        for change in ('partial_arm', 'missing_row', 'duplicate', 'expected', 'line', 'prompt',
                       'wrong_type', 'unanswered', 'bad_validation', 'missing_model', 'unknown_arm'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); out = root / 'readiness.json'; out.write_text('previous result')
                doc = self.artifact()
                if change == 'partial_arm':
                    doc = self.artifact(arms=('base',))
                elif change == 'missing_row':
                    doc['rows'][1]['id'] = 'grimgar03:1'
                elif change == 'duplicate':
                    doc['rows'].append(copy.deepcopy(doc['rows'][0]))
                elif change in ('expected', 'line'):
                    doc['rows'][1][change] = 'Different'
                elif change == 'prompt':
                    doc['rows'][0]['prompt_sha256'] = 'a'; doc['rows'][1]['prompt_sha256'] = 'b'
                elif change == 'wrong_type':
                    doc['rows'][0]['correct'] = 'false'
                elif change == 'unanswered':
                    doc['rows'][0]['predicted'] = None
                elif change == 'bad_validation':
                    doc['meta']['validation'] = ['unverified environment']
                elif change == 'missing_model':
                    doc['meta'].pop('model')
                else:
                    doc['rows'][1]['arm'] = 'unrecognized'
                artifact = root / 'lora_serving_eval__input.json'; artifact.write_text(json.dumps(doc))
                with self.assertRaises(SystemExit):
                    self.run_cli(root, out, artifact)
                self.assertEqual('previous result', out.read_text())

    def test_one_complete_run_remains_usable_without_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'lora_serving_eval__input.json'
            source.write_text(json.dumps(self.artifact()))
            self.run_cli(root, root / 'result.json')
            self.assertEqual(0, json.loads((root / 'result.json').read_text())['pooled']['lost'])
