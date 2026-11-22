"""Durable bidirectional review resume in the single reserved receipt."""
import json
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import copy
import tempfile
import unittest

import completed_review_receipt as receipts
import core
import generate_script
import review_script as review


class DirectionalResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'book.json'
        self.rows = [{'speaker': 'ANN', 'text': 'Synthetic line.', 'instruct': 'Plain.'}]
        self.path.write_text(json.dumps(self.rows))
        self.aliases = self.root / 'aliases.json'
        self.voices = self.root / 'voices.json'
        self.aliases.write_text('{}')
        self.voices.write_text('{}')
        self.created = []

    def _client(self, transform=None):
        made = []

        def create(**_kwargs):
            rows = copy.deepcopy(self.rows)
            if transform:
                transform(rows)
            made.append(len(rows))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(rows)), finish_reason='stop')], usage=None)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                               close=Mock()), made

    def _run(self, client, direction='forward', force=False, companions=True, capture=None):
        argv = ['review', '--input', str(self.path)]
        if direction == 'backward':
            argv += ['--review-pass', 'backward']
        if force:
            argv += ['--force-review']
        if companions:
            argv += ['--dedupe-speakers', '--alias-registry', str(self.aliases),
                     '--remap-voice-config', str(self.voices)]
        with ExitStack() as stack:
            out = stack.enter_context(redirect_stdout(capture or StringIO()))
            values = {'load_app_config': {'generation': {'review_batch_size': 1}},
                      'get_active_llm_config': {}, 'ensure_ideal_settings': (False, {}, 'fixture'),
                      'get_current_status': {'loaded': False}, 'make_run_client': client,
                      'wait_for_vram_headroom': True, 'ensure_cached_or_benchmarked_concurrency': 1}
            for name, value in values.items():
                stack.enter_context(patch.object(review, name, return_value=value))
            stack.enter_context(patch.object(generate_script, 'get_response_log_path',
                                             return_value=str(self.root / 'responses.log')))
            stack.enter_context(patch.object(review, 'dedupe_speakers',
                                             return_value=({}, 0, [])))
            stack.enter_context(patch.object(review, 'merge_consecutive_narrators',
                                             side_effect=lambda rows, **kw: (rows, 0)))
            stack.enter_context(patch.object(review, 'ensure_review_not_cancelled', return_value=None))
            stack.enter_context(patch.object(__import__('sys'), 'argv', argv))
            review.main()
        return out.getvalue() if out is not None else ''

    def _receipt(self):
        return receipts.load_review_receipt_document(str(self.path))

    def test_forward_and_backward_facts_survive_and_third_run_reuses(self):
        client, made = self._client()
        self._run(client)
        self.assertEqual(1, len(made))
        client_b, made_b = self._client()
        self._run(client_b, 'backward')
        self.assertEqual(1, len(made_b))
        receipt = self._receipt()
        self.assertEqual({'forward', 'backward'}, set(receipt['facts']))
        for direction in ('forward', 'backward'):
            self.assertEqual(0, receipt['facts'][direction]['summary']['total_changes'])
        client2, made2 = self._client()
        self._run(client2)
        self.assertEqual([], made2, 'verified forward completion must make zero requests')
        client3, made3 = self._client()
        self._run(client3, 'backward')
        self.assertEqual([], made3, 'verified backward completion must make zero requests')

    def test_force_executes_forward_and_keeps_old_fact_until_success(self):
        client, made = self._client()
        self._run(client)
        forced, made_forced = self._client()
        out = self._run(forced, force=True, capture=StringIO())
        self.assertEqual(1, len(made_forced))
        self.assertIn('forced rerun', out)
        self.assertEqual('forward', self._receipt()['review_pass'])

    def test_changed_companion_or_input_invalidates_proof(self):
        client, made = self._client()
        self._run(client)
        client2, made2 = self._client()
        self._run(client2)
        self.assertEqual([], made2)
        self.voices.write_text('{"ANN":{"voice":"changed"}}')
        client3, made3 = self._client()
        self._run(client3)
        self.assertEqual([1], made3, 'post-publication companion change requires a rerun')
        self.path.write_text(json.dumps([{'speaker': 'ANN', 'text': 'Different line.', 'instruct': ''}]))
        self.rows = json.loads(self.path.read_text())
        client4, made4 = self._client()
        self._run(client4)
        self.assertEqual([1], made4, 'changed input requires a full match')

    def test_legacy_forward_receipt_resolves_with_unknown_counts(self):
        seed, made_seed = self._client()
        self._run(seed, companions=False)
        self.assertEqual([1], made_seed)
        receipt_path = Path(receipts.get_review_receipt_path(str(self.path)))
        flat = json.loads(receipt_path.read_text())
        flat.pop('facts', None)
        receipt_path.write_text(json.dumps(flat))
        client, made = self._client()
        out = self._run(client, companions=False, capture=StringIO())
        self.assertEqual([], made)
        self.assertIn('legacy proof', out)
        self.assertIn('unverified', out)

    def test_legacy_forward_proof_is_persisted_before_backward_runs(self):
        seed, made_seed = self._client()
        self._run(seed, companions=False)
        self.assertEqual([1], made_seed)
        receipt_path = Path(receipts.get_review_receipt_path(str(self.path)))
        flat = json.loads(receipt_path.read_text())
        flat.pop('facts', None)
        receipt_path.write_text(json.dumps(flat))
        reused, reused_made = self._client()
        self._run(reused, companions=False)
        self.assertEqual([], reused_made)
        self.assertIn('forward', self._receipt().get('facts', {}))

    def test_malformed_fact_never_yields_completion(self):
        client, made = self._client()
        self._run(client)
        receipt_path = Path(receipts.get_review_receipt_path(str(self.path)))
        receipt = json.loads(receipt_path.read_text())
        receipt['facts']['forward']['summary']['total_changes'] = 'bad'
        receipt_path.write_text(json.dumps(receipt))
        client2, made2 = self._client()
        self._run(client2)
        self.assertEqual([1], made2)


class AcceptedBoundaryTests(unittest.TestCase):
    def test_split_source_never_invents_output_lengths_and_resumes_after_split(self):
        import review_script as review
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'script.json')
            source = [{'speaker': 'A', 'text': 'Please leave the blue cup.'},
                      {'speaker': 'B', 'text': 'The door swung gently.'},
                      {'speaker': 'C', 'text': 'A third line.'}]
            digest = receipts.get_review_entries_fingerprint(source)
            stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
            accepted = [{'speaker': 'B', 'text': 'Please leave'},
                        {'speaker': 'B', 'text': 'the blue cup.'}]
            review.save_checkpoint(str(output), 1, 3, 1, 0, accepted, stats,
                                   accepted[-1:], [], [], digest, digest,
                                   source_offsets=[1], execution_sha256='exec')
            path = Path(review._checkpoint_path(str(output)))
            data = json.loads(path.read_text())
            data.pop('batch_lengths')
            path.write_text(json.dumps(data))
            loaded = review.load_checkpoint(str(output), 3, 1, 0, source,
                                            execution_sha256='exec')
            self.assertEqual([], loaded['batch_lengths'],
                             'a split source must not fabricate per-batch output lengths')
            self.assertEqual(2, loaded['accepted_output_count'])
            offsets = review.get_review_resume_source_offsets(loaded, source, 1,
                                                              input_sha256=digest)
            self.assertEqual([1], offsets)
            self.assertEqual([1], review.get_review_resume_source_offsets(
                loaded, source, 1))

    def test_failed_batch_makes_aggregate_wording_independent_of_final_checks(self):
        import core
        totals = {**core._new_review_totals(), 'review_incomplete': True,
                  'post_processing_abort': True, 'batches_failed': 1}
        live = core._format_pass_summary('Overall', totals, [], False)
        self.assertNotIn('all rows reviewed', live)
        self.assertIn('1 section(s) failed', live)
        heads_up = core._markdown_heads_up_lines(totals)
        self.assertFalse(any('All entries were reviewed' in line for line in heads_up))
        self.assertTrue(any('1 section(s) ran into an error' in line for line in heads_up))


if __name__ == '__main__':
    unittest.main()
