"""Discriminators for the #2222 checkpoint/disposition follow-up findings.

Every case drives production surfaces: review_script.main() for disposition and
resume, core's live/Markdown summaries for reporting. Fixtures are synthetic
source-bound rows, never text from a book.
"""
import errno
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import core
import generation_checkpoint_deltas as gd
import review_script as review
from tests.test_review_partial_retention import SOURCE


CONFIG = {
    'llm_mode': 'remote',
    'llm_remote': {'model_name': 'fixture', 'base_url': 'https://fixture.invalid/v1'},
    'generation': {
        'review_batch_size': 3, 'review_target_chars': 1, 'merge_narrators': False,
    },
}


def make_book(root):
    scripts = root / 'scripts'
    scripts.mkdir(exist_ok=True)
    path = scripts / 'book.json'
    path.write_text(json.dumps(SOURCE, indent=4))
    return path


def run_review(root, path, gates, *, ctx=1, mode='ok', fail=(), out=None, dedupe=False):
    """Run the real review CLI against a fixture and capture its markers."""
    args = ['review', '--input', str(path), '--output', str(out or path),
            '--context-window', str(ctx)] + (['--dedupe-speakers'] if dedupe else [])
    log = io.StringIO()
    calls = []
    original_write = gd.atomic_json_write

    def writer(data, *a, **kw):
        if mode == 'all' or (mode == 'compact' and data.get('output_sha256')):
            raise OSError(errno.ENOSPC, 'fixture')
        return original_write(data, *a, **kw)

    def batch(_client, _model, rows, index, *_a, **_kw):
        calls.append(index)
        if index in fail:
            return None
        return [{**row, 'speaker': 'ACCEPTED' + str(index)} for row in rows]

    with ExitStack() as stack:
        stack.enter_context(redirect_stdout(log))
        for name, value in [
            ('get_runtime_data_dir', str(root)), ('load_app_config', CONFIG),
            ('ensure_ideal_settings', (True, {}, 'fixture')),
            ('get_active_llm_config', CONFIG['llm_remote']),
            ('make_run_client', object()),
            ('ensure_cached_or_benchmarked_concurrency', 1),
            ('get_current_status', {'loaded': False}),
            ('is_llm_on_this_gpu', True),
        ]:
            stack.enter_context(patch.object(review, name, return_value=value))
        stack.enter_context(patch.object(review.sys, 'argv', args))
        stack.enter_context(patch.object(gd, 'atomic_json_write', writer))
        stack.enter_context(patch.object(review, 'wait_for_vram_headroom', side_effect=gates))
        stack.enter_context(patch.object(review, 'review_batch', side_effect=batch))
        stack.enter_context(patch.object(review, 'dedupe_speakers',
                                        return_value=({}, 0, [])))
        review.main()
    lines = log.getvalue().splitlines()

    def marker(name):
        prefix = name + ': '
        payloads = [json.loads(line[len(prefix):]) for line in lines if line.startswith(prefix)]
        return payloads[-1] if payloads else None

    return {
        'lines': lines, 'calls': calls,
        'disposition': marker('REVIEW_DISPOSITION_JSON'),
        'failed': marker('FAILED_SECTIONS_JSON'),
    }


def speakers(path):
    return [row['speaker'] for row in json.loads(path.read_text())]


def seed_legacy(path):
    """Legacy checkpoint: accepted batch 1 but no per-batch output lengths."""
    checkpoint = Path(str(path) + '.review_checkpoint.json')
    data = json.loads(checkpoint.read_text())
    for key in ('batch_lengths', 'review_accounting', 'review_batch_contributions'):
        data.pop(key, None)
    accepted = data['all_corrected'][:1]
    data.update(completed_batches=1, source_offsets=[1], accepted_source_offset=1,
                failed_batches=[], all_corrected=accepted, previous_tail=accepted)
    checkpoint.write_text(json.dumps(data))


class LegacyResumeTests(unittest.TestCase):
    def test_noncontextual_skip_rewinds_before_unresolved_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = make_book(root)
            run_review(root, path, [True, False], ctx=0)
            seed_legacy(path)
            abort = run_review(root, path, [False], ctx=0)
            self.assertEqual('vram_rows', abort['disposition']['abort_stage'])
            self.assertEqual(1, abort['disposition']['retry_offset'])
            self.assertEqual(2, abort['disposition']['retry_from_batch'])
            self.assertFalse(any('completed successfully' in line for line in abort['lines']))
            resume = run_review(root, path, [True] * 4, ctx=0)
            self.assertEqual([2, 3], resume['calls'])
            self.assertEqual(['ACCEPTED1', 'ACCEPTED2', 'ACCEPTED3'], speakers(path))
            self.assertFalse(resume['disposition']['incomplete'])
            self.assertTrue(any('Task review completed successfully.' in line
                                for line in resume['lines']))

    def test_contextual_skip_keeps_the_same_safe_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = make_book(root)
            run_review(root, path, [True, False], ctx=1)
            seed_legacy(path)
            abort = run_review(root, path, [False], ctx=1)
            self.assertEqual([], abort['calls'])
            self.assertEqual(1, abort['disposition']['retry_offset'])
            self.assertEqual(2, abort['disposition']['retry_from_batch'])
            resume = run_review(root, path, [True] * 4, ctx=1)
            self.assertEqual([2, 3], resume['calls'])
            self.assertEqual(['ACCEPTED1', 'ACCEPTED2', 'ACCEPTED3'], speakers(path))


class RetryDispositionTests(unittest.TestCase):
    def test_retry_batch_uses_window_ordinal_not_batch_size_division(self):
        windows = [(0, 1), (1, 2), (2, 3)]
        self.assertEqual(2, review.get_review_retry_batch(windows, 1))
        self.assertEqual(3, review.get_review_retry_batch(windows, 2))
        self.assertEqual(2, review.get_review_retry_batch(
            windows, None, [{'batch': 2}]))

    def test_compact_failure_keeps_same_input_deltas_but_not_changed_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = make_book(root)
            first = run_review(root, path, [True, True, False], ctx=1, mode='compact')
            self.assertEqual(2, first['disposition']['retry_offset'])
            self.assertEqual(3, first['disposition']['retry_from_batch'])
            resume = run_review(root, path, [True] * 4, ctx=1)
            self.assertEqual([3], resume['calls'])
            self.assertEqual(resume['calls'][0], first['disposition']['retry_from_batch'])
            path.write_text(path.read_text().replace('blue', 'red'))
            stale = run_review(root, path, [True, False], ctx=1, mode='all')
            self.assertIsNone(stale['disposition']['retry_offset'])
            self.assertFalse(stale['disposition']['resumable_checkpoint'])

    def test_all_writes_fail_never_claims_a_verified_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = make_book(root)
            result = run_review(root, path, [True, False], ctx=1, mode='all')
            self.assertIsNone(result['disposition']['retry_offset'])
            self.assertFalse(result['disposition']['resumable_checkpoint'])
            self.assertFalse(any('the verified checkpoint holds the completed work' in line
                                 for line in result['lines']))
            self.assertTrue(any('no verified checkpoint holds the work' in line
                                for line in result['lines']))

    def test_failed_section_marker_ordinal_matches_next_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = make_book(root)
            failed = run_review(root, path, [True] * 3 + [False], fail=[2], dedupe=True)
            self.assertEqual(2, failed['failed']['retry_from_batch'])
            self.assertEqual(2, failed['disposition']['retry_from_batch'])
            resume = run_review(root, path, [True] * 4, dedupe=True)
            self.assertEqual(failed['disposition']['retry_from_batch'], resume['calls'][0])
            self.assertEqual(['ACCEPTED1', 'ACCEPTED2', 'ACCEPTED3'], speakers(path))


class DispositionTextTests(unittest.TestCase):
    def test_batch_failure_plus_headroom_abort_never_claims_all_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = make_book(root)
            result = run_review(root, path, [True] * 3 + [False], fail=[2], dedupe=True)
            self.assertNotIn('final_checks', result['disposition']['abort_stage'])
            notes = '\n'.join(result['lines'])
            self.assertNotIn('All 3 entries were reviewed', notes)
            self.assertNotIn('all rows were reviewed', notes)
            self.assertTrue(any('1 unresolved batch(es)' in line for line in result['lines']))

    def test_live_and_markdown_mixed_summary_keeps_both_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            final_path = make_book(root)
            final = run_review(root, final_path, [True] * 3 + [False], dedupe=True)
            rows_path = root / 'scripts' / 'other.json'
            rows_path.write_text(json.dumps(SOURCE, indent=4))
            rows = run_review(root, rows_path, [True, False], ctx=0)
            combined = core._combine_pass_stats(
                core._extract_review_stats(final['lines']),
                core._extract_review_stats(rows['lines']))
            combined['books_done'] = 2
            summary = core._format_pass_summary('Overall', combined, [], False)
            self.assertIn('Final checks stopped for 1 book(s); unreviewed rows: 2', summary)
            self.assertNotIn('all rows reviewed', summary)
            prose = core._get_deterministic_review_summary(combined, True)
            self.assertIn('1 book(s) stopped in final checks and 2 entries were left unreviewed', prose)
            self.assertNotIn('all entries were reviewed', prose)
            heads = core._markdown_heads_up_lines(combined)
            self.assertTrue(any('Final checks stopped on some book(s), and 2 entries were '
                                'left unreviewed' in line for line in heads))
            self.assertFalse(any('All entries were reviewed' in line for line in heads))

    def test_saved_report_respects_explicit_complete_and_keeps_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(core, 'REPORTS_DIR', str(Path(tmp, 'reports'))), \
                    patch.object(core, '_llm_summarize_report', return_value=None):
                complete = core._extract_review_stats([
                    'Review complete: 3 -> 3 entries', 'Batches failed: 1',
                    'Batches skipped (low GPU VRAM): 1'])
                text = Path(core._write_single_review_report(complete)).read_text()
                self.assertNotIn('partially reviewed', text)
                self.assertNotIn('Re-run', text)
                incomplete = core._extract_review_stats([
                    'Review incomplete: 3 -> 3 entries',
                    'Batches skipped (low GPU VRAM): 1'])
                text = Path(core._write_single_review_report(incomplete)).read_text()
                self.assertIn('partially reviewed', text)
                counters = core._new_review_totals()
                counters.update(batches_failed=1, entries_before=3, entries_after=3)
                text = Path(core._write_single_review_report(counters)).read_text()
                self.assertIn('partially reviewed', text)


if __name__ == '__main__':
    unittest.main()
