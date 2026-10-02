"""Native corpus dispatch and stored artifacts discriminate ownership and failure cases."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_corpus_dispatch_identity as dispatch_fixture


class CorpusOwnedSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture = dispatch_fixture.CorpusDispatchIdentityTests()
        self.root = self.fixture.create_repo(self.base)
        self.output = self.root / 'output'

    def invoke(self, mode='--run', worker='valid'):
        with patch.dict(os.environ, CORPUS_FIXTURE_MODE=worker):
            return self.fixture.invoke(self.root, self.output, mode, self.base)

    def index(self):
        return json.loads((self.output / 'corpus_attempts.json').read_text())

    def test_unrelated_logs_do_not_affect_metrics_and_resume_totals_are_not_added_twice(self):
        for name in ('old', 'concurrent'):
            (self.root / 'logs' / ('alexandria_preparer_' + name + '.log')).write_text(
                'Annotation complete: 999 total segments (999 new this run)\nTotal audio in dataset : 999.0s\n')
        result = self.invoke()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        report = (self.output / 'aggregated_report.md').read_text()
        self.assertIn('Total segments emitted: **6**', report)
        self.assertIn('Total dataset audio:    **24s**', report)
        self.assertIn('2/1', report)
        self.assertIn('Realign events total: **4**', report)
        self.assertIn('Re-anchor events total: **2**', report)
        self.assertNotIn('999', report)
        before = (self.output / 'corpus_attempts.json').read_bytes()
        (self.root / 'logs').rename(self.root / 'logs-no-longer-readable')
        result = self.invoke('--aggregate')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(report, (self.output / 'aggregated_report.md').read_text())
        self.assertEqual(before, (self.output / 'corpus_attempts.json').read_bytes())

    def test_invalid_or_unexported_summaries_fail_and_independent_pair_still_completes(self):
        for mode in ('missing', 'stale', 'wrong-settings', 'nan', 'invalid', 'bad-resume', 'bad-version', 'export-failed'):
            with self.subTest(mode=mode):
                result = self.invoke(worker=mode)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                rows = self.index()['rows']
                self.assertEqual(1, sum(row['status'] == 'completed' for row in rows))
                self.assertEqual(1, sum(row['status'].startswith('failed:') for row in rows))
                report = (self.output / 'aggregated_report.md').read_text()
                self.assertIn('Total segments emitted: **3**', report)
                self.assertIn('1 pair(s) failed', report)
                self.assertNotIn('Done. Report', result.stdout)
                # In particular, the export-failed worker writes a plausible annotation summary
                # and may leave an earlier ZIP, neither of which can imply export success.
                result = self.invoke('--aggregate')
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)

    def test_fresh_attempt_cannot_reuse_a_previous_summary(self):
        result = self.invoke()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        old = self.index()
        saved = {Path(row['summary_path']): Path(row['summary_path']).read_bytes() for row in old['rows']}
        result = self.invoke(worker='missing')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertNotEqual(old['run_id'], self.index()['run_id'])
        for path, data in saved.items():
            self.assertEqual(data, path.read_bytes())

    def test_stored_dataset_summary_or_inputs_changed_are_explicit_failures(self):
        for artifact in ('summary_path', 'output', 'audio', 'source'):
            with self.subTest(artifact=artifact):
                result = self.invoke()
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                row = self.index()['rows'][0]
                path = Path(row[artifact])
                data = path.read_bytes()
                path.write_bytes(data + b'changed')
                result = self.invoke('--aggregate')
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn('failed:', (self.output / 'aggregated_report.md').read_text())
                path.write_bytes(data)

    def test_pending_interrupted_pair_is_not_silently_omitted(self):
        self.assertEqual(0, self.invoke().returncode)
        index = self.index()
        index['rows'][0]['status'] = 'pending'
        (self.output / 'corpus_attempts.json').write_text(json.dumps(index))
        result = self.invoke('--aggregate')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        report = (self.output / 'aggregated_report.md').read_text()
        self.assertIn('failed: pending', report)
        self.assertIn('Total segments emitted: **3**', report)

    def test_cross_corpus_index_refuses_and_concurrent_writer_cannot_replace_index(self):
        self.assertEqual(0, self.invoke().returncode)
        path = self.output / 'corpus_attempts.json'
        before = path.read_bytes()
        with (self.output / '.corpus.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.invoke()
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertEqual(before, path.read_bytes())
        index = self.index()
        index['run_dir'] = str(self.root)
        path.write_text(json.dumps(index))
        report = (self.output / 'aggregated_report.md').read_bytes()
        result = self.invoke('--aggregate')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('another corpus', result.stderr)
        self.assertEqual(report, (self.output / 'aggregated_report.md').read_bytes())
