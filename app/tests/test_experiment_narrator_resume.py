"""Actual narrator-prior CLI retries unanswered rows without losing valid work."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments import pdnc_narrator_prior as runner
from experiments.manifest import ExperimentRecord, EnvironmentCaptureError


class NarratorResumeTests(unittest.TestCase):
    def test_failed_block_retries_after_transport_recovers_and_wrong_answers_stay_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = {'roster': ['ALICE', 'OTHER'], 'aliases': {}, 'entries': [
                {'id': i, 'line': f'line {i}', 'expected_speaker': 'ALICE',
                 'prev_context': 'before', 'next_context': 'after'} for i in range(25)]}
            calls, records = [], []
            def attribute(client, model, frozen, *args, **kwargs):
                calls.append(copy.deepcopy(frozen))
                if len(calls) == 1:
                    raise ConnectionError('synthetic outage')
                # A real but incorrect answer is still completed work.
                return [{'speaker': 'OTHER' if int(row['text'].split()[1]) % 2 else 'ALICE'}
                        for row in frozen]
            def make_record(*args, **kwargs):
                record = ExperimentRecord(*args, **kwargs)
                records.append(record)
                return record
            args = [runner.__file__, '--books', 'TheGambler', '--limit', '25',
                    '--arms', 'baseline', 'narrator', '--tag', 'resume-fixture']
            with patch.object(runner, 'REPO', tmp), patch.object(sys, 'argv', args), \
                    patch.object(runner, 'build_fixture', return_value=fixture), \
                    patch.object(runner, 'OpenAI', return_value=object()), \
                    patch.object(runner, 'get_llama_server_environment', return_value={
                        'loaded': True, 'context_length': 32768, 'parallel': 1}), \
                    patch.object(runner, 'ExperimentRecord', side_effect=make_record), \
                    patch.object(runner, 'attribute_batch', side_effect=attribute), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(EnvironmentCaptureError, 'baseline: 25 rows but not one prediction'):
                    runner.main()
                self.assertEqual(2, len(calls))
                successful = copy.deepcopy(records[0].rows[25:])
                checkpoint = root / 'ab_test_runtime/experiments/pdnc_narrator_prior__resume-fixture.json.ckpt'
                self.assertEqual(50, len(json.loads(checkpoint.read_text())['rows']))
                runner.main()
                self.assertEqual(3, len(calls), 'Only the unanswered 25-row block should be sent again')
                self.assertEqual([f'line {i}' for i in range(25)], [r['text'] for r in calls[2]])
                resumed = records[1]
                self.assertEqual(50, len(resumed.rows))
                self.assertEqual(successful, [r for r in resumed.rows if r['arm'] == 'narrator'])
                self.assertEqual(50, len({(r['arm'], r['id']) for r in resumed.rows}))
                artifact = root / 'ab_test_runtime/experiments/pdnc_narrator_prior__resume-fixture.json'
                published = json.loads(artifact.read_text())
                self.assertEqual(50, len(published['rows']))
                self.assertTrue(all(row['predicted'] for row in published['rows']))
                self.assertFalse(checkpoint.exists(), 'Successful publication retains normal checkpoint cleanup')

    def test_checkpoint_retry_policy_is_opt_in_and_retains_nonempty_wrong_predictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            gold = Path(tmp) / 'gold.json'
            gold.write_text('{"entries": []}')
            def record():
                return ExperimentRecord('fixture', tmp, 'model', 'http://fixture', str(gold), {},
                    environment={'loaded': True, 'context_length': 32768, 'parallel': 1})
            checkpoint = str(Path(tmp) / 'checkpoint.json')
            original = record()
            original.enable_checkpoint(checkpoint)
            for i, predicted in enumerate((None, '', ' \t', 'OTHER')):
                original.add('arm', str(i), 'Line.', 'ALICE', predicted, False)
            original._ckpt_save(force=True)
            legacy = record()
            legacy.enable_checkpoint(checkpoint)
            self.assertEqual(original.rows, legacy.rows)
            self.assertTrue(all(legacy.done('arm', str(i)) for i in range(4)))
            retry = record()
            retry.enable_checkpoint(checkpoint, retry_unanswered=True)
            self.assertEqual(original.rows[-1:], retry.rows)
            self.assertTrue(retry.done('arm', '3'))
            self.assertFalse(any(retry.done('arm', str(i)) for i in range(3)))
            self.assertEqual(3, retry.meta['checkpoint']['unanswered_retried'])
