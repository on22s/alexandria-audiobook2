"""Run the closed-set harness with isolated CPU provider and corpus fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'experiments/closed_set.py'


class ClosedSetHarnessTests(unittest.TestCase):
    def test_actual_harness_starts_scores_all_arms_and_writes_verified_rows(self):
        import openai
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'app/experiments/closed_set.py'
            script.parent.mkdir(parents=True)
            script.write_bytes(SOURCE.read_bytes())
            names = ['ANNA', 'BOB', 'CLARA', 'DAVID', 'ELLA', 'FRANK', 'GRACE', 'HENRY']
            narration = ' '.join((' '.join(names) + '.') for _ in range(3))
            line = 'Tell me who is there.'
            source = narration + '\n“' + line + '”'
            inputs = root / 'ab_test_runtime/results/matrix_20260725-115148/inputs'
            inputs.mkdir(parents=True)
            source_path = inputs / 'mini.txt'
            source_path.write_text(source)
            checkpoint = inputs.parent / 'qwen3.5-9b-uncensored-hauhaucs-aggressive/mini/result.json.threepass_checkpoint.json'
            checkpoint.parent.mkdir(parents=True)
            segmented = [{'type': 'NARRATOR', 'text': narration}, {'type': 'SPOKEN', 'text': line}]
            checkpoint.write_text(json.dumps({'segmented': segmented,
                'named': [{'speaker': name, 'text': line} for name in names]}))
            gold = root / 'app/fixtures/attribution_gold_mini.json'
            gold.parent.mkdir(parents=True)
            gold.write_text(json.dumps({'entries': [{'id': 'quote1', 'line': line, 'expected_speaker': 'ANNA'}], 'aliases': []}))
            experiments = root / 'ab_test_runtime/experiments'
            experiments.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), 'add', '-A'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture',
                '-c', 'user.email=fixture@example.com', 'commit', '-q', '-m', 'CPU fixture'], check=True)
            original = {p: p.read_bytes() for p in (source_path, checkpoint, gold)}
            calls = []
            def create(**request):
                calls.append(request)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='UNKNOWN'))])
            provider = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            env = {'EXPERIMENT_MODEL': 'cpu/model', 'EXPERIMENT_BOOK': 'mini',
                'EXPERIMENT_GOLD': 'fixtures/attribution_gold_mini.json', 'EXPERIMENT_TAG': 'cpu-fixture',
                'EXPERIMENT_BASE_URL': 'http://cpu-fixture.invalid/v1',
                'EXPERIMENT_ENV': json.dumps({'loaded': True, 'context_length': 4096, 'parallel': 1,
                                             'fixture': 'CPU provider; no model inference'})}
            with patch.dict(os.environ, env), patch.object(openai, 'OpenAI', return_value=provider) as construct, \
                 patch.object(sys, 'path', list(sys.path)), contextlib.redirect_stdout(io.StringIO()) as output:
                namespace = runpy.run_path(str(script), run_name='__main__')
            construct.assert_called_once_with(base_url='http://cpu-fixture.invalid/v1', api_key='local')
            self.assertEqual(str(root), namespace['REPO'])
            self.assertEqual(3, len(calls))
            expected_candidates = [names, names[:6], namespace['record'].rows[2]['candidates']]
            for request, candidates in zip(calls, expected_candidates):
                prompt = request['messages'][1]['content']
                self.assertIn('The speaker is one of: ' + ', '.join(candidates + ['UNKNOWN']), prompt)
                self.assertIn(line, prompt)
                self.assertEqual(0.0, request['temperature'])
            artifact = experiments / 'closed_set__mini__cpu__model__cpu-fixture.json'
            written = json.loads(artifact.read_text())
            self.assertEqual(['open', 'closed-6', 'closed-oracle'], [r['arm'] for r in written['rows']])
            self.assertEqual(['quote1'] * 3, [r['id'] for r in written['rows']])
            self.assertTrue(all(r['predicted'] == 'UNKNOWN' for r in written['rows']))
            self.assertEqual(namespace['record'].rows, written['rows'])
            self.assertIn('wrote', output.getvalue())
            for path, content in original.items():
                self.assertEqual(content, path.read_bytes())
