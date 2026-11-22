"""Receipt/identity evidence-boundary discriminators for #2222 residuals."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import completed_review_receipt as receipts
import generation_checkpoint_deltas as gd
import review_script as review
import script_selection_binding as binding
from routers import script as script_router


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.book = self.root / 'book.json'
        self.rows = [{'speaker': 'JANE', 'text': text, 'instruct': ''}
                     for text in ('Red cat.', 'Blue cat.', 'White cat.', 'Black cat.')]
        self.book.write_text(json.dumps(self.rows))
        self.calls = []
        self.config = {'llm_mode': 'remote', 'llm_remote':
                       {'model_name': 'A', 'base_url': 'https://fixture.invalid/v1'},
                       'generation': {'review_batch_size': 1}}

    def client(self):
        def reply(**kwargs):
            content = kwargs['messages'][-1]['content']
            start = content.rindex('[\n  {')
            rows = json.JSONDecoder().raw_decode(content[start:])[0]
            self.calls.append([row['text'] for row in rows])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(rows)), finish_reason='stop')],
                usage=None)
        return SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=reply)), close=lambda: None)

    def cli(self, path=None, direction='forward', out=None, extra=(), gates=None,
            transform=None, publish_hook=None):
        target = path or self.book
        output = out or target
        with ExitStack() as stack:
            log = stack.enter_context(redirect_stdout(io.StringIO()))
            for name, value in (
                    ('get_runtime_data_dir', str(self.root)), ('load_app_config', self.config),
                    ('get_active_llm_config', self.config['llm_remote']),
                    ('ensure_ideal_settings', (False, {}, 'fixture')),
                    ('make_run_client', self.client()), ('get_current_status', {'loaded': False}),
                    ('ensure_cached_or_benchmarked_concurrency', 1),
                    ('is_llm_on_this_gpu', True),
                    ('dedupe_speakers', ({}, 0, [])),
                    ('ensure_review_not_cancelled', None)):
                stack.enter_context(patch.object(review, name, return_value=value))
            if gates is None:
                stack.enter_context(patch.object(review, 'wait_for_vram_headroom',
                                                 return_value=True))
            else:
                stack.enter_context(patch.object(review, 'wait_for_vram_headroom',
                                                 side_effect=gates))
            if transform is not None:
                def batch(_client, _model, rows, index, *_a, **_k):
                    self.calls.append([row['text'] for row in rows])
                    return transform(rows, index)
                stack.enter_context(patch.object(review, 'review_batch', side_effect=batch))
            stack.enter_context(patch.object(review.sys, 'argv',
                ['review', '--input', str(target), '--output', str(output),
                 '--review-pass', direction, *extra]))
            if publish_hook:
                stack.enter_context(publish_hook(stack))
            review.main()
        return log.getvalue()

    def receipt(self, path=None):
        return receipts.load_review_receipt_document(str(path or self.book))

    def fact(self, direction, path=None):
        return receipts.read_durable_review_fact(str(path or self.book), direction)


class ForwardFallbackTests(Harness):
    def test_changed_forward_input_is_not_authorized_by_backward_checkpoint(self):
        self.cli()
        self.cli(direction='backward', gates=[True, False])
        self.book.write_text(json.dumps([{'speaker': 'NEW', 'text': 'Totally unrelated.'}]))
        self.calls.clear()
        out = self.cli()
        self.assertTrue(self.calls)
        self.assertNotIn('verified backward checkpoint consumes', out)

    def test_separate_output_backward_cannot_certify_a_different_source(self):
        source = self.root / 'one.json'
        source.write_text(json.dumps(self.rows))
        out = self.root / 'out.json'
        self.cli(path=source, out=out)
        self.cli(path=out, out=out, direction='backward')
        other = self.root / 'two.json'
        other.write_text(json.dumps(self.rows))
        for path in (source, other):
            path.write_text(json.dumps([{'speaker': 'X', 'text': 'Different source.'}]))
            self.calls.clear()
            out_log = self.cli(path=path, out=out, direction='backward')
            self.assertTrue(self.calls)
            self.assertNotIn('verified backward pass consumed', out_log)


class CompanionProofTests(Harness):
    def _setup_companions(self, saved):
        target = (self.root / 'scripts' / 'book.json') if saved else self.root / 'annotated_script.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.rows))
        alias = self.root / 'character_aliases.json'
        alias.write_text('{"JANE":"ANN"}')
        voice = target.with_suffix('.voice_config.json') if saved else self.root / 'voice_config.json'
        voice.write_text('{"JANE":{"voice":"one"}}')
        return target, alias, voice

    def test_post_commit_companion_edit_is_not_authenticated(self):
        for saved in (True, False):
            for which in ('alias', 'voice'):
                with self.subTest(saved=saved, which=which):
                    self.setUp()
                    target, alias, voice = self._setup_companions(saved)
                    extra = ('--dedupe-speakers', '--alias-registry', str(alias),
                             '--remap-voice-config', str(voice))

                    def hook(stack):
                        name = 'publish_saved_script_edit' if saved else 'publish_controlled_script_edit'
                        original = getattr(binding, name)
                        def publish(*args, **kwargs):
                            moved = original(*args, **kwargs)
                            (alias if which == 'alias' else voice).write_text(
                                '{"ANN":{"voice":"outside"}}' if which == 'voice'
                                else '{"JANE":"BEA"}')
                            return moved
                        return patch.object(binding, name, side_effect=publish)
                    self.cli(path=target, extra=extra, publish_hook=hook)
                    companion = self.fact('forward', target)['companions'][
                        'aliases' if which == 'alias' else 'voice']
                    external = hashlib.sha256(
                        (alias if which == 'alias' else voice).read_bytes()).hexdigest()
                    self.assertNotEqual(external, companion['sha256'])
                    self.calls.clear()
                    self.cli(path=target, extra=extra)
                    self.assertTrue(self.calls, 'an external companion edit must force a rerun')

    def test_publisher_proof_captures_raw_entry_and_companion_hashes(self):
        self.setUp()
        target, alias, voice = self._setup_companions(True)
        original = target.read_bytes()
        edited = json.dumps([{**self.rows[0], 'text': 'Reviewed.'}]).encode()
        proof = binding.publish_saved_script_edit(
            str(self.root), str(target.parent), str(target), original, edited,
            voice_config_path=str(voice), voice_mapping={},
            remap_voice_config=None, alias_registry_path=str(alias))
        self.assertIsInstance(proof, int)
        self.assertEqual(hashlib.sha256(edited).hexdigest(), proof.output_raw_sha256)
        self.assertEqual(receipts.get_review_entries_fingerprint(json.loads(edited)),
                         proof.output_entries_sha256)
        self.assertEqual(hashlib.sha256(voice.read_bytes()).hexdigest(),
                         proof.companions['voice']['sha256'])
        self.assertEqual(hashlib.sha256(alias.read_bytes()).hexdigest(),
                         proof.companions['aliases']['sha256'])


class CheckpointEvidenceTests(Harness):
    def _complete_duplex(self):
        self.cli()
        self.cli(direction='backward')

    def _force_partial_backward(self, storage):
        original = gd.atomic_json_write
        def save(data, *args, **kwargs):
            if storage == 'delta' and data.get('output_sha256'):
                raise OSError(28, 'compact ENOSPC')
            return original(data, *args, **kwargs)
        with patch.object(gd, 'atomic_json_write', side_effect=save):
            return self.cli(direction='backward',
                            extra=('--force-review',), gates=[True, True, False])

    def test_failed_compaction_and_canonical_delta_replay_review_surviving_rows(self):
        for storage in ('delta', 'compact'):
            with self.subTest(storage=storage):
                self.setUp()
                self._complete_duplex()
                self._force_partial_backward(storage)
                path = Path(str(self.book) + '.review_checkpoint.json')
                self.assertTrue(path.exists())
                self.calls.clear()
                self.cli(direction='backward')
                self.assertEqual([['White cat.'], ['Black cat.']], self.calls)

    def test_superseded_backward_fact_is_retired_when_backward_work_starts(self):
        self._complete_duplex()
        self.assertTrue(self.fact('backward'))
        self.cli(direction='backward', extra=('--force-review',), gates=[True, False])
        document = self.receipt()
        self.assertNotIn('backward', document.get('facts', {}))
        self.assertIn('forward', document.get('facts', {}))


class FlatIdentityTests(Harness):
    def test_double_replacement_migrates_the_validated_digest(self):
        saved = self.root / 'scripts' / 'book.json'
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(json.dumps(self.rows))
        out = self.root / 'out.json'
        self.cli(path=saved, out=out)
        path = Path(receipts.get_review_receipt_path(str(out)))
        document = json.loads(path.read_text())
        document.pop('facts')
        path.write_text(json.dumps(document))
        original = out.read_bytes()
        external = json.dumps([{**self.rows[0], 'text': 'Unreviewed bytes.'}]).encode()
        out.write_bytes(external)
        real = review.read_validated_completed_review
        def swap(*args, **kwargs):
            out.write_bytes(original)
            result = real(*args, **kwargs)
            out.write_bytes(external)
            return result
        with patch.object(review, 'read_validated_completed_review', side_effect=swap):
            self.cli(path=saved, out=out)
        self.assertEqual(receipts.get_review_entries_fingerprint(json.loads(original)),
                         self.fact('forward', out)['output_sha256'])
        self.calls.clear()
        self.cli(path=saved, out=out)
        self.assertTrue(self.calls, 'the unreviewed replacement must be reviewed')

    def test_unsupported_version_facts_are_not_promoted_during_invalidation(self):
        self._complete = self.cli()
        self.cli(direction='backward')
        path = Path(receipts.get_review_receipt_path(str(self.book)))
        document = json.loads(path.read_text())
        document['version'] = 999
        path.write_text(json.dumps(document))
        self.cli(direction='backward', gates=[True, False])
        self.assertIsNone(self.fact('forward') if path.exists() else None)
        self.calls.clear()
        self.cli()
        self.assertTrue(self.calls)


class DirectionPairTests(Harness):
    def test_half_present_backward_parent_is_rejected_but_standalone_is_reusable(self):
        self.cli(direction='backward')
        self.calls.clear()
        self.cli(direction='backward')
        self.assertEqual([], self.calls, 'standalone backward with both parents absent reuses')
        path = Path(receipts.get_review_receipt_path(str(self.book)))
        document = json.loads(path.read_text())
        document['facts']['backward'].pop('consumed_forward_sha256')
        document['facts']['backward']['consumed_forward_fingerprint'] = 'a' * 64
        path.write_text(json.dumps(document))
        self.calls.clear()
        self.cli(direction='backward')
        self.assertTrue(self.calls, 'a half-present parent pair is never a valid fact')


class CoordinateAndBatchTests(Harness):
    def test_canonical_destination_deduplication(self):
        self.assertEqual(['book'], script_router.get_unique_batch_script_names(['book', '.book']))
        self.assertEqual(['book', 'other'],
                         script_router.get_unique_batch_script_names(['book', 'other']))
        self.assertEqual(['Book', 'book'] if __import__('os').name != 'nt' else ['Book'],
                         script_router.get_unique_batch_script_names(['Book', 'book']))

    def test_balanced_split_and_merge_rewinds_only_the_failed_batch(self):
        rows = [{'speaker': 'A', 'text': text} for text in
                ('one two', 'three', 'four', 'five six')]
        digest = receipts.get_review_entries_fingerprint(rows)
        stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
        accepted = [{'speaker': 'B', 'text': 'one'}, {'speaker': 'B', 'text': 'two'},
                    {'speaker': 'B', 'text': 'three'}, {'speaker': 'B', 'text': 'four'}]
        review.save_checkpoint(str(self.book), 3, 3, 1, 0, accepted, stats,
            accepted[-1:], [], [2], digest, digest, source_offsets=[1, 2, 4],
            execution_sha256='exec')
        loaded = review.load_checkpoint(str(self.book), 3, 1, 0, rows, execution_sha256='exec')
        self.assertIsNone(loaded, 'an ambiguous failed boundary must start fresh')


if __name__ == '__main__':
    unittest.main()
