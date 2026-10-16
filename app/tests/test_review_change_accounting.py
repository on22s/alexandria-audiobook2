"""Native CLI/delta artifacts distinguish replayed edits from inherited history."""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import core
import review_script as review
from generation_checkpoint_deltas import GenerationCheckpointDeltas, load_generation_checkpoint_document


def get_stats(**values):
    return {**{key: 0 for key in review.REVIEW_EDIT_COUNTERS},
            'batches_failed': 0, 'batches_skipped_vram': 0, **values}


def get_rows(count=6):
    return [{'speaker': 'BOB', 'text': f'Original sentence number {i}.', 'instruct': ''}
            for i in range(count)]


class ReviewAccountingTests(unittest.TestCase):
    def run_cli(self, directory, source, output, respond, *, context=0, concurrency=1,
                benchmark=None, remove=None):
        log = io.StringIO()
        client = SimpleNamespace(close=lambda: None)
        config = {'llm_mode': 'remote', 'llm_remote': {'model_name': 'fixture'},
                  'generation': {'review_batch_size': 3, 'merge_narrators': False}}
        args = ['review', '--input', str(source), '--output', str(output),
                '--context-window', str(context)]
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(log))
            stack.enter_context(patch.object(review.sys, 'argv', args))
            for name, value in [('get_runtime_data_dir', directory), ('load_app_config', config),
                                ('get_active_llm_config', config['llm_remote']),
                                ('ensure_ideal_settings', (True, {}, 'fixture')),
                                ('make_run_client', client), ('get_completed_review_fingerprint', 'fixture'),
                                ('get_current_status', {'loaded': False}), ('is_llm_on_this_gpu', False)]:
                stack.enter_context(patch.object(review, name, return_value=value))
            stack.enter_context(patch.object(review, 'get_cached_or_benchmarked_concurrency',
                side_effect=benchmark if benchmark else lambda *a, **kw: concurrency))
            stack.enter_context(patch.object(review, 'review_batch', side_effect=respond))
            if remove is not None:
                stack.enter_context(patch.object(review, 'remove_completed_review_receipt', side_effect=remove))
            review.main()
        return log.getvalue()

    def checkpoint(self, output):
        return load_generation_checkpoint_document(review._checkpoint_path(str(output)))

    def save_owned(self, output, source, batches, *, failed=(), output_hash=None):
        stats = get_stats(batches_failed=len(failed))
        digest = review._entries_fingerprint(source)
        state = review.get_review_runtime_accounting(None, digest, len(source), get_stats(), [], [], 0)
        corrected, offsets, lengths = [], [], []
        consumed = 0
        for i, (original, changed) in enumerate(batches, 1):
            if i not in failed:
                delta = review.diff_entries(original, changed)
                for key in review.REVIEW_EDIT_COUNTERS:
                    stats[key] += delta[key]
            corrected.extend(copy.deepcopy(changed))
            consumed += len(original)
            offsets.append(consumed); lengths.append(len(changed))
            state = review.save_checkpoint(str(output), i, len(batches), 3, 0, corrected,
                stats, corrected[-2:], lengths, list(failed), digest,
                output_sha256=output_hash if i == len(batches) else None,
                source_offsets=offsets, execution_sha256='fixture', accounting=state)
        return state

    def test_actual_separate_input_repeated_rewind_counts_one_edit_once_in_both_modes(self):
        for context, concurrency in ((0, 1), (0, 2), (1, 1)):
            with self.subTest(context=context, concurrency=concurrency), tempfile.TemporaryDirectory() as tmp:
                original = get_rows(); source = Path(tmp, 'source.json'); output = Path(tmp, 'out.json')
                source.write_text(json.dumps(original))
                fail_first = True
                calls = []
                def respond(client, model, batch, number, *args, **kwargs):
                    calls.append(number)
                    if number == 1 and fail_first:
                        return None
                    changed = copy.deepcopy(batch)
                    if number == 2:
                        changed[0]['speaker'] = 'ALICE'
                    return changed
                for _ in range(3):
                    log = self.run_cli(tmp, source, output, respond, context=context, concurrency=concurrency)
                    self.assertEqual(1, core._extract_review_stats(log.splitlines())['speaker_changed'])
                    saved = self.checkpoint(output)
                    self.assertEqual([0, 1], [row['speaker_changed'] for row in saved['review_batch_contributions']])
                    self.assertFalse(saved['review_accounting']['inherited_unverified'])
                fail_first = False
                log = self.run_cli(tmp, source, output, respond, context=context, concurrency=concurrency)
                self.assertEqual(1, core._extract_review_stats(log.splitlines())['speaker_changed'])
                self.assertEqual(original, json.loads(source.read_text()))
                self.assertEqual('ALICE', json.loads(output.read_text())[3]['speaker'])
                self.assertFalse(Path(review._checkpoint_path(str(output))).exists())
                self.assertEqual(8, len(calls), 'accounting must not add model work')

    def test_successful_prefix_partial_middle_and_redone_suffix_have_distinct_contributions(self):
        for context in (0, 1):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp, 'source.json'); output = Path(tmp, 'out.json')
                source.write_text(json.dumps(get_rows(9)))
                partial = True
                def respond(client, model, batch, number, *args, **kwargs):
                    changed = copy.deepcopy(batch); changed[0]['speaker'] = 'ALICE'
                    if number == 2 and partial:
                        kwargs['attempt_observer']({'event': review.REVIEW_PARTIAL_EVENT})
                    return changed
                self.run_cli(tmp, source, output, respond, context=context)
                self.assertEqual([1, 0, 1], [r['speaker_changed'] for r in self.checkpoint(output)['review_batch_contributions']])
                partial = False
                log = self.run_cli(tmp, source, output, respond, context=context)
                self.assertEqual(3, core._extract_review_stats(log.splitlines())['speaker_changed'])

    def test_in_place_promotes_previous_edits_and_equal_hash_resume_keeps_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, 'source.json'); source.write_text(json.dumps(get_rows()))
            fail = True
            def respond(client, model, batch, number, *args, **kwargs):
                if number == 1 and fail:
                    return None
                changed = copy.deepcopy(batch)
                changed[0]['speaker'] = 'ALICE'
                return changed
            self.run_cli(tmp, source, source, respond)
            for _ in range(2):
                self.run_cli(tmp, source, source, respond)
                saved = self.checkpoint(source)
                self.assertEqual(saved['source_sha256'], saved['output_sha256'])
                self.assertEqual(1, saved['review_accounting']['inherited']['speaker_changed'])
                self.assertEqual([0, 0], [r['speaker_changed'] for r in saved['review_batch_contributions']])
            fail = False
            log = self.run_cli(tmp, source, source, respond)
            self.assertEqual(2, core._extract_review_stats(log.splitlines())['speaker_changed'])

    def test_output_resume_without_rewind_rebases_merged_source_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'out.json'); original = get_rows(4)
            merged = [{**original[0], 'text': ' '.join(row['text'] for row in original[:3])}]
            state = self.save_owned(output, original, [(original[:3], merged)])
            published = merged + original[3:]
            data = self.checkpoint(output); data['output_sha256'] = review._entries_fingerprint(published)
            Path(review._checkpoint_path(str(output))).write_text(json.dumps(data))
            loaded = review.load_checkpoint(str(output), 2, 3, 0, published, execution_sha256='fixture')
            self.assertEqual(4, state['metadata']['source_entry_count'])
            self.assertEqual(2, loaded['review_accounting']['source_entry_count'])
            self.assertEqual(state['totals'], loaded['review_accounting']['inherited'])
            self.assertEqual(1, loaded['review_batch_contributions'][0]['source_end'])
            self.assertEqual(0, loaded['review_batch_contributions'][0]['entries_removed'])
            self.assertEqual([1], review.get_review_resume_source_offsets(loaded, published, 3))

    def test_repeated_text_split_merge_counts_remain_per_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = [{'speaker': 'BOB', 'text': text} for text in ('Echo.', 'Other.', 'Echo.', 'Other.')]
            merged = [{'speaker': 'BOB', 'text': 'Echo. Other.'}]
            output = Path(tmp, 'out.json')
            state = self.save_owned(output, original,
                [(original[:2], merged), (original[2:], original[2:])], failed=(2,))
            loaded = review.load_checkpoint(str(output), 2, 3, 0, original, execution_sha256='fixture')
            self.assertEqual([2], loaded['source_offsets'])
            self.assertEqual([1], loaded['batch_lengths'])
            self.assertEqual(1, loaded['total_stats']['text_changed'])
            self.assertEqual(1, loaded['total_stats']['entries_removed'])
            self.assertEqual(state['totals'], {k: loaded['total_stats'][k] for k in review.REVIEW_EDIT_COUNTERS})
            global_diff = review.diff_entries(original, merged + original[2:])
            self.assertNotEqual(state['totals'], {key: global_diff[key] for key in review.REVIEW_EDIT_COUNTERS})

    def test_failed_save_advances_runtime_then_native_delta_replay_has_two_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = get_rows(); output = Path(tmp, 'out.json'); digest = review._entries_fingerprint(original)
            changed = copy.deepcopy(original); changed[0]['speaker'] = 'ALICE'
            stats = get_stats(speaker_changed=1, entries_changed=1)
            native = review.diff_entries(original, changed)
            self.assertEqual({key: stats[key] for key in review.REVIEW_EDIT_COUNTERS},
                             {key: native[key] for key in review.REVIEW_EDIT_COUNTERS})
            state = review.get_review_runtime_accounting(None, digest, 6, get_stats(), [], [], 0)
            writer = GenerationCheckpointDeltas(review._checkpoint_path(str(output)))
            with patch('generation_checkpoint_deltas.atomic_json_write', side_effect=OSError('fixture disk failure')):
                state = review.save_checkpoint(str(output), 1, 2, 3, 0, changed[:3], stats,
                    changed[1:3], [3], [], digest, writer=writer, source_offsets=[3], accounting=state)
            self.assertEqual(1, len(state['contributions']))
            state = review.save_checkpoint(str(output), 2, 2, 3, 0, changed, stats,
                changed[-2:], [3, 3], [], digest, writer=writer, source_offsets=[3, 6], accounting=state)
            self.assertEqual([1, 0], [r['speaker_changed'] for r in self.checkpoint(output)['review_batch_contributions']])
            state = review.save_checkpoint(str(output), 2, 2, 3, 0, changed, stats,
                changed[-2:], [3, 3], [], digest, output_sha256=review._entries_fingerprint(changed), writer=writer,
                source_offsets=[3, 6], accounting=state)
            self.assertEqual(2, len(state['contributions']))

    def test_unreadable_accepted_counts_stop_before_benchmark_and_preserve_all_artifacts(self):
        for value in (True, -1, 'bad'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                original = get_rows(); source = Path(tmp, 'source.json'); output = Path(tmp, 'out.json')
                source.write_text(json.dumps(original))
                self.save_owned(output, original, [(original[:3], original[:3])])
                path = Path(review._checkpoint_path(str(output))); data = self.checkpoint(output)
                data['total_stats']['speaker_changed'] = value; path.write_text(json.dumps(data))
                before = path.read_bytes()
                stop = lambda *a, **kw: self.fail('No benchmark, inference or receipt removal allowed')
                with self.assertRaisesRegex(ValueError, 'checkpoint preserved'):
                    self.run_cli(tmp, source, output, stop, benchmark=stop, remove=stop)
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(original, json.loads(source.read_text()))

    def test_stale_malformed_counter_checkpoint_keeps_native_ignore_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, 'source.json'); output = Path(tmp, 'out.json'); original = get_rows()
            source.write_text(json.dumps(original)); self.save_owned(output, original, [(original, original)])
            path = Path(review._checkpoint_path(str(output))); data = self.checkpoint(output)
            data['execution_sha256'] = 'stale'; data['total_stats']['speaker_changed'] = True
            path.write_text(json.dumps(data))
            log = self.run_cli(tmp, source, output, lambda c, m, batch, *a, **kw: copy.deepcopy(batch))
            self.assertEqual(0, core._extract_review_stats(log.splitlines())['speaker_changed'])

    def test_legacy_missing_lengths_stays_unowned_and_warning_survives_completion_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, 'source.json'); output = Path(tmp, 'out.json'); original = get_rows()
            source.write_text(json.dumps(original)); self.save_owned(output, original, [(original[:3], original[:3])])
            path = Path(review._checkpoint_path(str(output))); data = self.checkpoint(output)
            data.pop('batch_lengths'); data.pop('review_accounting'); data.pop('review_batch_contributions')
            data['total_stats']['speaker_changed'] = 7; path.write_text(json.dumps(data))
            log = self.run_cli(tmp, source, output, lambda c, m, batch, *a, **kw: copy.deepcopy(batch))
            stats = core._extract_review_stats(log.splitlines())
            self.assertEqual(7, stats['speaker_changed']); self.assertIs(True, stats['accounting_unverified'])
            self.assertEqual([], core._markdown_change_density_lines({**stats, 'entries_before': 100, 'entries_changed': 90}))
            with patch.object(core, 'REPORTS_DIR', str(Path(tmp, 'reports'))):
                report = Path(core._write_single_review_report(stats))
            self.assertIn('Historical review change counts are unverified', report.read_text())
            self.assertFalse(Path(review._checkpoint_path(str(output))).exists())

    def test_report_and_per_book_pass_merger_keep_uncertainty_boolean(self):
        totals = core._new_review_totals()
        for _ in range(3):
            combined = core._combine_pass_stats(totals, {'speaker_changed': 2, 'accounting_unverified': True})
            combined['books_done'] = totals['books_done'] + 1
            totals.update(combined)
        self.assertEqual(6, totals['speaker_changed']); self.assertIs(True, totals['accounting_unverified'])
        self.assertEqual(core.get_review_accounting_warning(totals), totals['accounting_warning'])
        overall = core._combine_pass_totals({'totals_fwd': totals, 'totals_bwd': {**totals, 'books_done': 1}})
        self.assertEqual(3, overall['books_done']); self.assertIs(True, overall['accounting_unverified'])
        self.assertTrue(core._combine_pass_stats({'partial': True}, {})['partial'])

    def test_invalid_ledger_history_stays_warned_across_source_and_output_resumes(self):
        mutations = [lambda d: d.pop('review_accounting'),
                     lambda d: d['review_accounting'].update(version=True),
                     lambda d: d['review_accounting'].update(source_entry_count=True),
                     lambda d: d['review_batch_contributions'][0].update(source_end=True),
                     lambda d: d['review_batch_contributions'].pop(),
                     lambda d: d['review_accounting']['inherited'].update(speaker_changed=4)]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as tmp:
                original = get_rows(); source = Path(tmp, 'source.json'); output = Path(tmp, 'out.json')
                source.write_text(json.dumps(original))
                changed = copy.deepcopy(original[3:]); changed[0]['speaker'] = 'ALICE'
                self.save_owned(output, original,
                    [(original[:3], original[:3]), (original[3:], changed)], failed=(1,))
                path = Path(review._checkpoint_path(str(output))); data = self.checkpoint(output)
                mutate(data); path.write_text(json.dumps(data))
                fail = True
                def respond(client, model, batch, number, *a, **kw):
                    if number == 1 and fail:
                        return None
                    rows = copy.deepcopy(batch); rows[0]['speaker'] = 'ALICE'; return rows
                for incoming in (source, source, output, output):
                    log = self.run_cli(tmp, incoming, output, respond)
                    stats = core._extract_review_stats(log.splitlines())
                    self.assertEqual(2, stats['speaker_changed'])
                    self.assertIs(True, stats['accounting_unverified'])
                fail = False
                log = self.run_cli(tmp, output, output, respond)
                self.assertEqual(3, core._extract_review_stats(log.splitlines())['speaker_changed'])
                self.assertIn('historical counts are unverified', log)

    def test_unknown_multi_batch_delta_is_not_split_into_invented_rows_or_mutated(self):
        original = get_rows(); digest = review._entries_fingerprint(original)
        state = review.get_review_runtime_accounting(None, digest, 6, get_stats(), [], [], 0)
        data = {'source_sha256': digest, 'completed_batches': 2, 'source_offsets': [3, 6],
                'batch_lengths': [3, 3], 'all_corrected': original,
                'total_stats': get_stats(speaker_changed=1), 'failed_batches': []}
        before = copy.deepcopy((state, data))
        updated = review.get_saved_review_accounting(state, data)
        self.assertEqual(before, (state, data))
        self.assertEqual('unowned', updated['metadata']['mode'])
        self.assertEqual([], updated['contributions'])
        self.assertEqual(1, updated['totals']['speaker_changed'])
        self.assertTrue(updated['metadata']['inherited_unverified'])

    def test_native_missing_entries_changed_migration_preserves_zero_but_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = get_rows(); output = Path(tmp, 'out.json')
            self.save_owned(output, original, [(original[:3], original[:3])])
            path = Path(review._checkpoint_path(str(output))); data = self.checkpoint(output)
            data['total_stats'].pop('entries_changed'); path.write_text(json.dumps(data))
            before = path.read_bytes()
            loaded = review.load_checkpoint(str(output), 2, 3, 0, original, execution_sha256='fixture')
            self.assertEqual(0, loaded['total_stats']['entries_changed'])
            self.assertTrue(loaded['review_accounting']['inherited_unverified'])
            self.assertEqual(before, path.read_bytes())

    def test_invalid_structured_summary_is_not_reported_as_verified_zero(self):
        base = ['Review complete: 6 -> 6 entries', 'Total changes: 3']
        for value in ('not JSON', '[]', '{"inherited_unverified": 0}', '{}'):
            with self.subTest(value=value):
                stats = core._extract_review_stats(base + ['REVIEW_ACCOUNTING_JSON: ' + value])
                self.assertEqual(3, stats['total_changes'])
                self.assertIs(True, stats['accounting_unverified'])
                self.assertEqual(core.get_review_accounting_warning(stats), stats['accounting_warning'])

    def test_actual_frontend_displays_supplied_warning_and_escapes_live_totals(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        stats = core._combine_pass_stats({'total_changes': 3, 'text_changed': 1,
            'speaker_changed': 2, 'accounting_unverified': True})
        stats['books_done'] = 1
        code = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const first = source.indexOf('function _formatBookStats(');
const last = source.indexOf('function _showTaskRecoveryPanel(', first);
assert(first >= 0 && last > first, 'actual formatters must be loaded');
const totals = {style: {}, innerHTML: ''};
const ctx = {document: {getElementById: () => totals}, escapeHtml: s => String(s)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')};
vm.createContext(ctx); vm.runInContext(source.slice(first, last), ctx);
const stats = JSON.parse(process.argv[2]);
assert(ctx._formatBookStats(stats).includes(stats.accounting_warning));
assert(ctx._formatTotalsLine('Totals', stats).includes(stats.accounting_warning));
const verified = {...stats, accounting_unverified: false, accounting_warning: ''};
assert.strictEqual(ctx._formatBookStats(verified), '3 changes: 1 text, 2 speaker, 0 instruct, +0/-0 entries');
assert.strictEqual(ctx._formatTotalsLine('Totals', verified), 'Totals: 1 book(s), 3 total change(s) (1 text, 2 speaker, 0 instruct, +0/-0 entries)');
assert.strictEqual(ctx._formatTotalsLine('Totals', null), 'Totals: no books finished yet');
const partial = {...stats, partial: true, batches_failed: 1, batches_skipped_vram: 2};
assert(ctx._formatBookStats(partial).startsWith('(partial'));
assert(ctx._formatBookStats(partial).includes('2 batch(es) skipped (VRAM)'));
assert(ctx._formatTotalsLine('Totals', partial).includes('1 batch(es) failed'));
const supplied = {...stats, accounting_warning: '<unsafe & warning>'};
ctx._updateReviewBatchTotals({totals_fwd: supplied});
assert(totals.innerHTML.includes('&lt;unsafe &amp; warning&gt;'));
assert(!totals.innerHTML.includes('<unsafe'));
ctx._updateReviewBatchTotals({bidirectional: true, totals_fwd: stats, totals_bwd: verified});
assert(totals.innerHTML.includes('Forward pass: 1 book(s)'));
assert(totals.innerHTML.includes('Backward pass (hindsight): 1 book(s)'));
assert.strictEqual(totals.innerHTML.split(stats.accounting_warning).length - 1, 1);
'''
        result = subprocess.run(['node', '-e', code, str(source), json.dumps(stats)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_impossible_accounting_rewind_keeps_readable_counts_unowned(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = get_rows(); output = Path(tmp, 'out.json')
            self.save_owned(output, original, [(original[:3], original[:3])])
            data = self.checkpoint(output); before = copy.deepcopy(data)
            metadata, rows, counts = review.get_review_checkpoint_accounting(data, original, 99)
            self.assertEqual(before, data)
            self.assertEqual('unowned', metadata['mode'])
            self.assertEqual([], rows)
            self.assertEqual(get_stats()['speaker_changed'], counts['speaker_changed'])


if __name__ == '__main__':
    unittest.main()
