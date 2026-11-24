"""Malformed pair rows must become failed report rows, not abort the dry run."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load_subject():
    spec = importlib.util.spec_from_file_location(
        'corpus_alignment_prescan_subject', ROOT / 'corpus_alignment_prescan.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CorpusAlignmentPrescanMalformedPairTests(unittest.TestCase):
    def invoke(self, pairs):
        module = load_subject()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'output'
            audio = root / 'book.wav'
            audio.write_bytes(b'audio')
            source = root / 'book.txt'
            source.write_text('source')
            matched = {'audio': str(audio), 'source': str(source), 'output_name': 'book.zip'}
            pairs_path = root / 'pairs.json'
            pairs_path.write_text(json.dumps([*pairs, matched]))
            argv = ['prescan', '--repo', str(root), '--pairs', str(pairs_path),
                    '--output-dir', str(output)]
            with patch.object(sys, 'argv', argv), \
                 patch.object(module.subprocess, 'run',
                              return_value=SimpleNamespace(returncode=0)) as dispatch, \
                 patch.object(module, 'get_alignment_report_quality', return_value={
                     'average_ratio': 1.0, 'sampled': 30,
                     'below_60_percent': 0, 'review_needed': 0}):
                code = module.main()
            report = json.loads((output / 'dry_run_report.json').read_text())
            markdown = (output / 'dry_run_report.md').read_text()
            return code, report, markdown, dispatch

    def test_each_malformed_pair_is_a_failed_row_and_other_pairs_still_run(self):
        pairs = [
            {'audio': 'a.wav'},
            'not a mapping',
            {'audio': None, 'source': None},
            {'audio': 5, 'source': None},
            {'audio': '', 'source': None},
            {'audio': 'a.wav', 'source': 5},
        ]
        code, report, markdown, dispatch = self.invoke(pairs)
        self.assertEqual(1, code)
        self.assertEqual(len(pairs) + 1, len(report['rows']))
        for row in report['rows'][:-1]:
            self.assertTrue(row['status'].startswith('failed: '), row)
        self.assertEqual('a.wav', report['rows'][-2]['audio'])
        self.assertTrue(report['rows'][-2]['status'].startswith('failed: '))
        self.assertEqual('measured', report['rows'][-1]['status'])
        self.assertEqual(1, dispatch.call_count)
        self.assertIn('failed:', markdown)
        self.assertNotIn('None', markdown)

    def test_report_markdown_survives_a_failed_row_without_paths(self):
        module = load_subject()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.md'
            module.save_alignment_markdown(
                [{'audio': None, 'source': None, 'status': 'failed: malformed pair'}], path)
            text = path.read_text()
            self.assertIn('| - | - | failed: malformed pair |', text)
