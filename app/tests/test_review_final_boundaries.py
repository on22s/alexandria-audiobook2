"""Discriminators for the final Astra resume/reporting boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core
import review_script as review
from completed_review_receipt import get_review_entries_fingerprint
from tests.test_review_partial_retention import SOURCE, IncompletePublicationTests


class LegacyBoundaryTests(unittest.TestCase):
    def test_three_invocations_keep_accepted_batch_without_inventing_lengths(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'script.json')
            rows = [{'speaker': 'A', 'text': 'first line.'},
                    {'speaker': 'B', 'text': 'second line.'},
                    {'speaker': 'C', 'text': 'third line.'}]
            digest = get_review_entries_fingerprint(rows)
            stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
            review.save_checkpoint(str(output), 1, 3, 3, 0, rows[:1], stats, rows[:1],
                                   [], [], digest, source_offsets=[1], execution_sha256='exec')
            path = Path(str(output) + '.review_checkpoint.json')
            data = json.loads(path.read_text())
            del data['batch_lengths']
            data.pop('review_accounting', None)
            data.pop('review_batch_contributions', None)
            path.write_text(json.dumps(data))
            loaded = review._load_resume_state(str(output), 3, 3, 0, rows, [], dict(stats),
                                               execution_sha256='exec')
            self.assertEqual(1, loaded[3])
            self.assertEqual([1], review.get_review_resume_source_offsets(
                loaded[-1], rows, 3, input_sha256=digest))
            # Second invocation aborts before the next model batch; the final
            # save uses the validated accepted boundary, not optional lengths.
            review.save_checkpoint(str(output), len([1]), 3, 3, 0, rows[:1], dict(stats),
                                   rows[:1], [], [], digest, source_offsets=[1],
                                   execution_sha256='exec')
            again = review._load_resume_state(str(output), 3, 3, 0, rows, [], dict(stats),
                                              execution_sha256='exec')
            self.assertEqual(1, again[3])
            self.assertEqual([1], review.get_review_resume_source_offsets(
                again[-1], rows, 3, input_sha256=digest))

    def test_verified_retry_offset_uses_real_loader_and_rejects_stale_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'script.json')
            rows = [{'speaker': 'A', 'text': 'first line.'},
                    {'speaker': 'B', 'text': 'second line.'},
                    {'speaker': 'C', 'text': 'third line.'}]
            digest = get_review_entries_fingerprint(rows)
            stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
            windows = [(0, 1), (1, 2), (2, 3)]
            review.save_checkpoint(str(output), 1, 3, 3, 0, rows[:1], stats, rows[:1],
                                   [1], [], digest, source_offsets=[1], execution_sha256='exec')
            self.assertEqual(1, review.get_verified_retry_offset(
                str(output), 3, 3, 0, rows, digest, rows, 'exec', review_windows=windows))
            self.assertIsNone(review.get_verified_retry_offset(
                str(output), 3, 3, 0, rows, digest, rows, 'other', review_windows=windows))
            Path(str(output) + '.review_checkpoint.json').write_text('{broken')
            self.assertIsNone(review.get_verified_retry_offset(
                str(output), 3, 3, 0, rows, digest, rows, 'exec', review_windows=windows))


class StageAndReportTests(unittest.TestCase):
    def test_contextual_abort_counts_all_untouched_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_path = root / 'annotated_script.json'
            script_path.write_text(json.dumps(SOURCE))
            log = IncompletePublicationTests().run_review(
                root, script_path, vram_ok=False, dedupe=True)
            marker = json.loads(next(line.split(': ', 1)[1] for line in log.splitlines()
                                     if line.startswith('REVIEW_DISPOSITION_JSON: ')))
            self.assertEqual(len(SOURCE), marker['unreviewed_entries'])
            self.assertEqual('vram_rows', marker['abort_stage'])
            self.assertFalse(marker['post_processing_abort'])
            self.assertEqual('vram_rows', core._extract_review_stats(log.splitlines())['abort_stage'])

    def test_explicit_complete_suppresses_historical_warnings(self):
        stats = core._extract_review_stats([
            'Review complete: 2 -> 2 entries', 'Batches failed: 1'])
        self.assertEqual([], core._markdown_heads_up_lines(stats))
        stats['review_complete'] = False
        self.assertTrue(core._markdown_heads_up_lines(stats))

    def test_mixed_aggregate_reason_is_not_blanket_all_reviewed(self):
        stats = core._new_review_totals()
        stats.update(review_incomplete=True, final_check_aborted=1, unreviewed_entries=3)
        summary = core._get_deterministic_review_summary(stats, True)
        self.assertIn('1 book(s) stopped in final checks and 3 entries were left unreviewed', summary)
        self.assertNotIn('all entries were reviewed', summary)


if __name__ == '__main__':
    unittest.main()
