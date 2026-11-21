"""Focused regressions for the #2222 Astra follow-ups."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
import review_script as review
from completed_review_receipt import get_review_entries_fingerprint, get_review_receipt_path
from routers import script
from tests import test_review_partial_retention as partial_retention

CLEAN = ['Review complete: 1 -> 1 entries', 'Total changes: 0']
INCOMPLETE = ['Review incomplete: 1 -> 1 entries', 'Total changes: 0']


class BatchPassCheckpointTests(unittest.TestCase):
    def run_batch(self, lines, *, bidirectional=True, start_over=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / 'scripts'; scripts.mkdir()
            reports = root / 'reports'
            book = scripts / 'book.json'; book.write_text('[]')
            checkpoint = Path(str(book) + '.review_checkpoint.json')
            checkpoint.write_text(json.dumps({'completed_batches': 1, 'source_sha256': 'legacy'}))
            checkpoint_bytes = checkpoint.read_bytes()
            state = {'batch_review': {'running': False, 'cancel': False}}
            calls = []
            def stream(command, _cwd, current, **kwargs):
                nickname = any(str(arg).endswith('find_nicknames.py') for arg in command)
                calls.append(('nickname' if nickname else 'review', current.get('current_pass')))
                return (0, []) if nickname else (0, lines)
            app = FastAPI(); app.include_router(script.router)
            from contextlib import ExitStack
            patchers = [
                patch.object(script, 'SCRIPTS_DIR', str(scripts)),
                patch.object(script, 'CHARACTER_ALIASES_PATH', str(root / 'aliases.json')),
                patch.object(script, 'REPORTS_DIR', str(reports)),
                patch.object(script, 'ROOT_DIR', str(root)),
                patch.object(script, 'process_state', state),
                patch.object(core, 'process_state', state),
                patch.object(core, '_task_claims', {}),
                patch.object(core, '_gpu_leases', {}),
                patch.object(core, 'acquire_gpu_lock', return_value=None),
                patch.object(core, 'llm_is_on_this_gpu', return_value=True),
                patch.object(script, 'check_global_gpu_lock'),
                patch.object(script, '_require_review_restart_book'),
                patch.object(core, 'claim_gpu_task', wraps=core.claim_gpu_task),
                patch.object(script, '_init_task_log', return_value=str(root / 'task.log')),
                patch.object(script, '_stream_subprocess_to_logs', side_effect=stream),
                patch.object(script, '_insert_llm_summary', side_effect=lambda text, *_a, **_k: text),
                patch.object(script, 'discard_review_progress'),
                patch.object(script, '_run_claimed_background_task', side_effect=lambda name, work: work()),
                TestClient(app),
            ]
            with ExitStack() as stack:
                entered = [stack.enter_context(item) for item in patchers]
                discard, client = entered[-3], entered[-1]
                response = client.post('/api/review_script/batch/start', json={
                    'script_names': ['book'], 'dedupe_speakers': True,
                    'find_nicknames': True, 'bidirectional': bidirectional,
                    'start_over': start_over})
            return (response, calls, checkpoint_bytes == checkpoint.read_bytes(),
                    state['batch_review'], discard.call_count,
                    [task.get('status') for task in state['batch_review'].get('tasks', [])],
                    [item.read_text() for item in reports.glob('*.md')], str(book))

    def test_incomplete_forward_keeps_checkpoint_and_skips_hindsight(self):
        response, calls, kept, state, discard, statuses, reports, book = self.run_batch(INCOMPLETE)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(['review'], [kind for kind, _ in calls if kind == 'review'])
        self.assertEqual('fwd', next(phase for kind, phase in calls if kind == 'review'))
        self.assertTrue(kept)
        self.assertEqual(0, discard)
        self.assertEqual(['incomplete'], statuses)
        self.assertFalse(Path(get_review_receipt_path(book)).exists())
        self.assertIn('Skipping hindsight pass', '\n'.join(state['logs']))
        self.assertIn('only partially reviewed', reports[0])

    def test_clean_forward_keeps_checkpoint_and_runs_hindsight(self):
        response, calls, kept, state, discard, statuses, reports, _book = self.run_batch(CLEAN)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(['review', 'review'], [kind for kind, _ in calls if kind == 'review'])
        self.assertEqual(['fwd', 'bwd'], [phase for kind, phase in calls if kind == 'review'])
        self.assertTrue(kept)
        self.assertEqual(0, discard)
        self.assertEqual(['done'], statuses)

    def test_explicit_restart_discards_progress(self):
        response, _calls, _kept, state, discard, _statuses, _reports, _book = self.run_batch(CLEAN, start_over=True)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(2, discard)


class InheritedSpanResumeTests(unittest.TestCase):
    def case(self, output):
        original = [{'speaker': 'ANN', 'text': 'A short line.'},
                    {'speaker': 'BOB', 'text': 'Another short line.'}]
        normalized = [{**row, 'source_span': [index * 20, index * 20 + len(row['text'])]}
                      for index, row in enumerate(original)]
        digest = get_review_entries_fingerprint(original)
        stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
        stats['batches_skipped_vram'] = 2
        review.save_checkpoint(str(output), 1, 2, 2, 0, original[:1], stats, original[:1],
                               [len(original[0]['text'])], [], digest,
                               output_sha256=get_review_entries_fingerprint(original),
                               source_offsets=[1], execution_sha256='exec')
        return original, normalized, digest

    def test_normalized_rows_resume_only_with_original_input_identity(self):
        for name in ('annotated_script.json', 'book.json'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output = root / name
                original, normalized, digest = self.case(output)
                resumed = review.load_checkpoint(str(output), 2, 2, 0, normalized,
                    execution_sha256='exec', input_sha256=digest, input_entries=original)
                self.assertIsNotNone(resumed)
                self.assertEqual(1, resumed['completed_batches'])
                self.assertEqual([1],
                    review.get_review_resume_source_offsets(resumed, normalized, 2, input_sha256=digest))
                self.assertIsNone(review.load_checkpoint(str(output), 2, 2, 0, normalized,
                    execution_sha256='exec'), 'normalized rows must not silently rebind the checkpoint')
                loaded = review._load_resume_state(str(output), 2, 2, 0, normalized, [], dict(
                    {key: 0 for key in review.REVIEW_EDIT_COUNTERS}),
                    execution_sha256='exec', input_sha256=digest, input_entries=original)
                self.assertEqual(0, loaded[1]['batches_skipped_vram'],
                                 'historical skip counts must not survive into a successful resume')

    def test_legacy_prefix_without_batch_lengths_still_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'script.json')
            original, normalized, digest = self.case(output)
            path = Path(str(output) + '.review_checkpoint.json')
            data = json.loads(path.read_text())
            data.pop('batch_lengths'); data['batch_lengths'] = []
            data['source_offsets'] = [1]
            data.pop('review_accounting', None)
            data.pop('review_batch_contributions', None)
            path.write_text(json.dumps(data))
            resumed = review.load_checkpoint(str(output), 2, 2, 0, normalized,
                execution_sha256='exec', input_sha256=digest, input_entries=original)
            self.assertIsNotNone(resumed)
            self.assertEqual([1], review.get_review_resume_source_offsets(
                resumed, normalized, 2, input_sha256=digest))


class DispositionTests(unittest.TestCase):
    def stats(self, *, stage='post', withheld=True, verified=True, legacy=False):
        lines = list(INCOMPLETE)
        if not legacy:
            lines.append('REVIEW_DISPOSITION_JSON: ' + json.dumps({
                'incomplete': True, 'unreviewed_entries': 0 if stage == 'post' else 3,
                'post_processing_abort': stage == 'post',
                'publication_withheld': withheld, 'resumable_checkpoint': verified}))
        stats = core._extract_review_stats(lines)
        self.assertIsNotNone(stats)
        self.assertTrue(core._review_is_incomplete(stats))
        return stats

    def run_parent(self, lines):
        state = {'running': False, 'logs': [], 'status': '', 'return_code': None}
        with tempfile.TemporaryDirectory() as tmp:
            def stream(command, cwd, actual_state, **kwargs):
                actual_state['logs'].extend(lines)
                return 0, ''
            with patch.dict(core.process_state, {'review': state}), \
                 patch.object(core, 'REPORTS_DIR', str(Path(tmp, 'reports'))), \
                 patch.object(core, '_init_task_log', return_value=None), \
                 patch.object(core, '_stream_subprocess_to_logs', side_effect=stream), \
                 patch.object(core, '_llm_summarize_report', side_effect=AssertionError('no model')):
                core.run_process(['synthetic-review'], 'review')
        return state

    def test_post_processing_abort_reports_all_rows_and_withheld_publication(self):
        stats = self.stats()
        self.assertTrue(stats['post_processing_abort'])
        self.assertEqual(0, stats['unreviewed_entries'])
        state = self.run_parent(INCOMPLETE + ['REVIEW_DISPOSITION_JSON: ' + json.dumps({
            'incomplete': True, 'unreviewed_entries': 0, 'post_processing_abort': True,
            'publication_withheld': True, 'resumable_checkpoint': True})])
        self.assertEqual('incomplete', state['status'])
        notes = '\n'.join(state['logs'])
        self.assertIn('all entries were reviewed; final checks did not finish', notes)
        self.assertIn('left unchanged', notes)
        self.assertIn('verified checkpoint', notes)
        self.assertIn('all rows reviewed; final checks stopped',
                      core._format_pass_summary('Review', {**core._new_review_totals(),
                          **stats, 'books_done': 1}, [], False))

    def test_skipped_rows_and_explicit_output_keep_distinct_messages(self):
        state = self.run_parent(INCOMPLETE + ['REVIEW_DISPOSITION_JSON: ' + json.dumps({
            'incomplete': True, 'unreviewed_entries': 3, 'post_processing_abort': False,
            'publication_withheld': False, 'resumable_checkpoint': False})])
        notes = '\n'.join(state['logs'])
        self.assertIn('3 entries were left unreviewed', notes)
        self.assertIn('assembled entries were written to the output', notes)
        self.assertIn('No resumable checkpoint was verified', notes)
        self.assertIn('3 entries were left unreviewed',
                      '\n'.join(core._markdown_heads_up_lines(self.stats(stage='skipped'))))

    def test_legacy_stats_fall_back_to_counters(self):
        stats = self.stats(legacy=True)
        self.assertFalse(stats['post_processing_abort'])
        self.assertFalse(stats['publication_withheld'])
        self.assertTrue(core._review_is_incomplete({'batches_failed': 0,
            'batches_skipped_vram': 1}))
        self.assertFalse(core._review_is_incomplete({'batches_failed': 0,
            'batches_skipped_vram': 0}))


class VerifiedCheckpointTests(unittest.TestCase):
    def test_only_current_bound_checkpoint_promises_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'script.json')
            entries = [{'speaker': 'ANN', 'text': 'A line.'}]
            digest = get_review_entries_fingerprint(entries)
            stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
            review.save_checkpoint(str(output), 1, 1, 1, 0, entries, stats, entries, [1], [],
                                   digest, output_sha256=digest, execution_sha256='current')
            self.assertIsNotNone(review.get_verified_checkpoint(str(output), 'current', digest, digest))
            self.assertIsNone(review.get_verified_checkpoint(str(output), 'obsolete', digest, digest))
            self.assertIsNone(review.get_verified_checkpoint(str(output), 'current', 'other', digest))
            path = Path(str(output) + '.review_checkpoint.json')
            path.write_text('{not json')
            self.assertIsNone(review.get_verified_checkpoint(str(output), 'current', digest, digest))

    def test_cli_does_not_promise_resume_for_obsolete_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_path = root / 'annotated_script.json'
            script_path.write_text(json.dumps(partial_retention.SOURCE))
            legacy = Path(str(script_path) + '.review_checkpoint.json')
            legacy.write_text(json.dumps({'completed_batches': 0, 'execution_sha256': 'obsolete'}))
            with patch.object(review, 'save_checkpoint', side_effect=lambda *a, **kw: kw.get('accounting')):
                log = partial_retention.IncompletePublicationTests().run_review(
                    root, script_path, vram_ok=False)
            self.assertIn('No resumable checkpoint was saved', log)
            self.assertNotIn('Resume the review', log)


class RunHistoryOutcomeTests(unittest.TestCase):
    def test_helper_derives_terminal_and_child_outcomes(self):
        cases = (
            ({'status': 'done'}, 'completed'),
            ({'status': 'incomplete'}, 'incomplete'),
            ({'status': 'partial'}, 'incomplete'),
            ({'status': 'done', 'tasks': [{'status': 'done'}, {'status': 'incomplete'}]}, 'incomplete'),
            ({'status': 'done', 'tasks': [{'status': 'failed'}]}, 'failed'),
            ({'status': 'done', 'cancel': True}, 'cancelled'),
        )
        for state, expected in cases:
            with self.subTest(state=state):
                self.assertEqual(expected, core._run_history_final_status(state, 'batch_review', None))
        self.assertEqual('failed', core._run_history_final_status({'status': 'done'}, 'review', 'boom'))

    def test_background_task_persists_derived_status(self):
        state = {'running': True, 'status': 'done', 'logs': [],
                 'tasks': [{'status': 'done'}, {'status': 'incomplete'}]}
        with patch.dict(core.process_state, {'batch_review': state}), \
             patch.dict(core._task_claims, {}, clear=True), \
             patch.object(core, 'prune_runs'), \
             patch.object(core, 'start_run', return_value='run_fixture'), \
             patch.object(core, 'finish_run') as finish:
            core._run_claimed_background_task('batch_review', lambda: None)
        finish.assert_called_once()
        self.assertEqual('incomplete', finish.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
