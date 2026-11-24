"""Malformed pair rows must be indexed as failures that the report can read."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load_subject():
    spec = importlib.util.spec_from_file_location(
        'corpus_run_report_subject', ROOT / 'corpus_run_report.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CorpusRunReportMalformedPairTests(unittest.TestCase):
    def test_run_corpus_indexes_each_malformed_pair_and_publishes_the_report(self):
        module = load_subject()
        pairs = [
            {'audio': 'a.wav'},
            'not a mapping',
            {'audio': None, 'source': None},
            {'audio': 5, 'source': None},
            {'audio': '', 'source': None},
            {'audio': 'b.wav', 'source': None},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'output'
            output.mkdir()
            pairs_path = root / 'pairs.json'
            pairs_path.write_text(json.dumps(pairs))
            code = module.run_corpus(root, pairs_path, output, 'model', None)
            index = json.loads((output / 'corpus_attempts.json').read_text())
            report = (output / 'aggregated_report.md').read_text()
        self.assertEqual(1, code)
        self.assertEqual(len(pairs), len(index['rows']))
        for row in index['rows'][:-1]:
            self.assertTrue(row['status'].startswith('failed: '), row)
        self.assertEqual('no source match', index['rows'][-1]['status'])
        self.assertIn('| - | failed:', report)
        self.assertIn("'source'", report)
        self.assertIn('pair must be an object', report)
        self.assertIn('5 pair(s) failed', report)

    def test_report_reader_records_a_failed_row_without_an_audio_path(self):
        module = load_subject()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'output'
            run_dir = output / 'annotation_reports' / 'run1'
            run_dir.mkdir(parents=True)
            index = {'version': 1, 'run_id': 'run1', 'run_dir': str(run_dir),
                     'rows': [{'audio': None, 'source': None,
                               'status': 'failed: malformed pair'}]}
            code = module.save_corpus_report(index, output)
            report = (output / 'aggregated_report.md').read_text()
        self.assertEqual(1, code)
        self.assertIn('malformed pair', report)
        self.assertIn('| - |', report)
