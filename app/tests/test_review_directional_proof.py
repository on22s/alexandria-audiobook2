"""Discriminators for the Astra #2222 directional-proof findings."""
import copy
import errno
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import completed_review_receipt as receipts
import core
import generation_checkpoint_deltas as gd
import review_script as review
import script_selection_binding as binding


class ProofHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.book = self.root / 'book.json'
        self.rows = [{'speaker': 'ORIGINAL', 'text': text, 'instruct': ''}
                     for text in ('Red cat.', 'Blue cat.', 'White cat.')]
        self.book.write_text(json.dumps(self.rows))
        self.model = 'A'
        self.calls = []

    def _client(self, transform=None):
        def reply(**kwargs):
            content = kwargs['messages'][-1]['content']
            start = content.rindex('[\n  {')
            rows = json.JSONDecoder().raw_decode(content[start:])[0]
            self.calls.append((self.model, [row['text'] for row in rows]))
            if transform:
                rows = transform(copy.deepcopy(rows))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(rows)), finish_reason='stop')], usage=None)
        return SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=reply)), close=lambda: None)

    def cli(self, direction='forward', *, path=None, extra=(), transform=None,
            publish_hook=None, batch_transform=None):
        target = path or self.book
        client = self._client(transform)
        config = {'llm_mode': 'remote', 'llm_remote':
                  {'model_name': self.model, 'base_url': 'https://fixture.invalid/v1'},
                  'generation': {'review_batch_size': 1}}
        with ExitStack() as stack:
            out = stack.enter_context(redirect_stdout(io.StringIO()))
            for name, value in (
                ('get_runtime_data_dir', str(self.root)), ('load_app_config', config),
                ('get_active_llm_config', config['llm_remote']),
                ('ensure_ideal_settings', (False, {}, 'fixture')),
                ('make_run_client', client), ('get_current_status', {'loaded': False}),
                ('ensure_cached_or_benchmarked_concurrency', 1),
                ('is_llm_on_this_gpu', True), ('dedupe_speakers', (lambda *a, **k: ({}, 0, []))),
            ):
                stack.enter_context(patch.object(review, name, return_value=value))
            stack.enter_context(patch.object(review, 'wait_for_vram_headroom', return_value=True))
            if batch_transform is not None:
                def batch(_client, _model, rows, index, *_a, **_k):
                    self.calls.append((self.model, [row['text'] for row in rows]))
                    return batch_transform(rows, index)
                stack.enter_context(patch.object(review, 'review_batch', side_effect=batch))
            stack.enter_context(patch.object(review, 'ensure_review_not_cancelled', return_value=None))
            stack.enter_context(patch.object(review.sys, 'argv',
                ['review', '--input', str(target), '--output', str(target),
                 '--review-pass', direction, *extra]))
            if publish_hook:
                stack.enter_context(publish_hook(stack))
            review.main()
        return out.getvalue()

    def fact(self, direction):
        return receipts.read_durable_review_fact(str(self.book), direction)


class DirectionalProofTests(ProofHarness):
    def test_unrelated_backward_cannot_certify_a_forward_pass(self):
        self.calls.clear()
        self.cli('forward')
        self.book.write_text(json.dumps([{'speaker': 'NEW', 'text': 'Replacement book.'}]))
        self.model = 'B'
        self.calls.clear()
        self.cli('backward')
        self.calls.clear()
        out = self.cli('forward')
        self.assertTrue(self.calls, 'an unrelated forward pass must reach the model')
        self.assertNotIn('No changes since the completed review', out)

    def test_post_publication_replacement_receives_no_completion_fact(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        target = scripts / 'book.json'
        target.write_text(json.dumps(self.rows))

        def replace_after_publish(stack):
            original = binding.publish_saved_script_edit

            def publish(*args, **kwargs):
                result = original(*args, **kwargs)
                target.write_text(json.dumps([{'speaker': 'NEW', 'text': 'Interleaved.'}]))
                return result
            return patch.object(binding, 'publish_saved_script_edit', side_effect=publish)

        self.cli('forward', path=target, publish_hook=replace_after_publish)
        receipt = json.loads(Path(receipts.get_review_receipt_path(str(target))).read_text())
        self.assertEqual(receipt['output_sha256'],
                         receipt['facts']['forward']['output_sha256'])
        self.calls.clear()
        self.cli('forward', path=target)
        self.assertTrue(self.calls, 'a post-publication replacement must be reviewed')

    def test_unknown_version_and_malformed_path_are_cache_misses_even_when_forced(self):
        self.cli('forward')
        path = Path(receipts.get_review_receipt_path(str(self.book)))
        document = json.loads(path.read_text())
        document['version'] = 999
        path.write_text(json.dumps(document))
        self.calls.clear()
        out = self.cli('forward')
        self.assertTrue(self.calls)
        self.assertNotIn('No changes since the completed review', out)
        document = json.loads(path.read_text())
        document['version'] = 1
        document['facts']['forward']['input_path'] = 123
        path.write_text(json.dumps(document))
        self.calls.clear()
        self.cli('forward', extra=('--force-review',))
        self.assertTrue(self.calls, 'malformed paths must not stop a forced rerun')

    def test_balanced_split_and_merge_never_infer_batch_lengths(self):
        source = [{'speaker': 'A', 'text': text} for text in
                  ('one two', 'three', 'four', 'five six')]
        digest = receipts.get_review_entries_fingerprint(source)
        accepted = [{'speaker': 'B', 'text': 'one'}, {'speaker': 'B', 'text': 'two'},
                    {'speaker': 'B', 'text': 'three'}, {'speaker': 'B', 'text': 'four'}]
        stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
        review.save_checkpoint(str(self.book), 3, 3, 1, 0, accepted, stats,
            accepted[-1:], [], [2], digest, digest, source_offsets=[1, 2, 4],
            execution_sha256='exec')
        loaded = review.load_checkpoint(str(self.book), 3, 1, 0, source,
                                        execution_sha256='exec')
        self.assertEqual([], loaded['batch_lengths'])
        self.assertEqual(4, loaded['accepted_output_count'])

    def test_reused_report_keeps_published_entry_counts(self):
        from types import SimpleNamespace
        import generate_script
        config = {'generation': {'review_batch_size': 1}, 'llm_mode': 'local'}
        args = SimpleNamespace(context_window=0, dedupe_speakers=False,
                               alias_registry=None, remap_voice_config=None,
                               review_pass='forward')
        params = generate_script.LLMGenParams('system', '{batch}')
        fingerprint = review.get_completed_review_fingerprint(
            config, {}, params, args, object(), 'model')
        settings = review.get_review_settings_fingerprint(
            config, {}, params, args, object(), 'model', None, self.rows)
        grown = self.rows + [{'speaker': 'ANN', 'text': 'added line.', 'instruct': ''}]
        output_sha = receipts.get_review_entries_fingerprint(grown)
        summary = {key: 0 for key in receipts.REVIEW_SUMMARY_FIELDS}
        summary.update(accounting_unverified=False, total_changes=1, entries_added=1)
        fact = receipts.build_review_fact(str(self.book), str(self.book),
            receipts.get_review_entries_fingerprint(self.rows), output_sha, fingerprint,
            {'aliases': None, 'voice': None}, summary, settings_fingerprint=settings,
            entries_before=3, entries_after=4)
        self.book.write_text(json.dumps(grown))
        receipts.save_durable_review_fact(str(self.book), 'forward', fact,
            top_level={'version': 1, 'review_pass': 'forward',
                       'fingerprint': fingerprint, 'output_sha256': output_sha})
        matched, _reason, reused, unverified, _legacy = review.get_reusable_review_completion(
            str(self.book), str(self.book), output_sha, output_sha, fingerprint, (),
            'forward', {'aliases': None, 'voice': None}, settings_fingerprint=settings)
        self.assertTrue(matched)
        self.assertFalse(unverified)
        self.assertEqual((3, 4), (reused['entries_before'], reused['entries_after']))


class ReportAndRouteTests(unittest.TestCase):
    def test_mixed_report_keeps_final_check_warning_independently(self):
        stats = {**core._new_review_totals(), 'review_incomplete': True,
                 'batches_failed': 1, 'final_check_aborted': 1}
        summary = core._get_deterministic_review_summary(stats, True)
        self.assertIn('section(s) failed', summary)
        self.assertIn('book(s) stopped in final checks', summary)
        notes = core._markdown_heads_up_lines(stats)
        self.assertTrue(any('ran into an error' in line for line in notes))
        self.assertTrue(any('Final checks also stopped' in line for line in notes))

    def test_all_failed_checkpoint_writes_do_not_promise_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            from tests.test_review_checkpoint_disposition import make_book, run_review
            path = make_book(root)
            outcome = run_review(root, path, [True, True, True], ctx=0,
                                 mode='all', fail=(2,))
            self.assertEqual(False, outcome['disposition']['resumable_checkpoint'])
            self.assertIsNone(outcome['disposition']['retry_from_batch'])
            self.assertFalse(any('retry on resume' in line for line in outcome['lines']))


if __name__ == '__main__':
    unittest.main()
