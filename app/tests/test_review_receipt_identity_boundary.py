"""Receipt/identity evidence-boundary discriminators for #2222 residuals."""
import copy
import errno
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
from core import _extract_review_stats
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
            transform=None, publish_hook=None, use_real_dedupe=False):
        target = path or self.book
        output = out or target
        with ExitStack() as stack:
            log = stack.enter_context(redirect_stdout(io.StringIO()))
            patches = [
                ('get_runtime_data_dir', str(self.root)), ('load_app_config', self.config),
                ('get_active_llm_config', self.config['llm_remote']),
                ('ensure_ideal_settings', (False, {}, 'fixture')),
                ('make_run_client', self.client()), ('get_current_status', {'loaded': False}),
                ('ensure_cached_or_benchmarked_concurrency', 1),
                ('is_llm_on_this_gpu', True),
                ('ensure_review_not_cancelled', None)]
            if not use_real_dedupe:
                patches.append(('dedupe_speakers', ({}, 0, [])))
            for name, value in patches:
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
        for variation in ('same_path_new_bytes', 'other_path_original_bytes'):
            with self.subTest(variation=variation):
                self.setUp()
                source = self.root / 'one.json'
                source.write_text(json.dumps(self.rows))
                out = self.root / 'out.json'
                self.cli(path=source, out=out)
                self.cli(path=out, out=out, direction='backward')
                if variation == 'same_path_new_bytes':
                    source.write_text(json.dumps(
                        [{'speaker': 'X', 'text': 'Different source.'}]))
                    path = source
                else:
                    path = self.root / 'two.json'
                    path.write_text(json.dumps(self.rows))
                self.calls.clear()
                log = self.cli(path=path, out=out)
                self.assertTrue(self.calls, 'a forward request must reach the model')
                self.assertNotIn('verified backward pass consumed', log)


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
    def test_migrated_flat_reuse_reports_verified_separate_output_count(self):
        out = self.root / 'out.json'

        def split(rows, index):
            if index == 1:
                return [{**rows[0], 'text': 'Red'}, {**rows[0], 'text': 'cat.'}]
            return rows

        self.cli(out=out, transform=split)
        receipt_path = Path(receipts.get_review_receipt_path(str(out)))
        document = json.loads(receipt_path.read_text())
        document.pop('facts')
        receipt_path.write_text(json.dumps(document))
        self.calls.clear()
        for attempt in ('migration', 'subsequent reuse'):
            with self.subTest(attempt=attempt):
                log = self.cli(out=out)
                self.assertEqual([], self.calls)
                self.assertIn('Review complete: 4 -> 5 entries', log)
                stats = _extract_review_stats(log.splitlines())
                self.assertEqual((4, 5), (stats['entries_before'], stats['entries_after']))
                self.assertTrue(stats['accounting_unverified'])

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


class NewIntegrityTests(Harness):
    def test_corrupt_checkpoint_coordinates_never_truncate_or_duplicate(self):
        def split(rows, index):
            if index == 1:
                return [{**rows[0], 'text': 'Red'}, {**rows[0], 'text': 'cat.'}]
            return rows
        for field, value in (('batch_lengths', [99]), ('batch_lengths', [-1]),
                             ('source_offsets', [0])):
            with self.subTest(field=field, value=value):
                self.setUp()
                self.cli(gates=[True, False], transform=split)
                path = Path(str(self.book) + '.review_checkpoint.json')
                data = json.loads(path.read_text())
                data[field] = value
                path.write_text(json.dumps(data))
                self.calls.clear()
                self.cli()
                published = [row['text'] for row in json.loads(self.book.read_text())]
                expected = ['Red', 'cat.', 'Blue cat.', 'White cat.', 'Black cat.']
                self.assertEqual(expected, published, 'no truncation or duplication')
                self.assertEqual(len(expected), len(self.calls), 'all current rows reviewed')
                self.assertIsNotNone(self.fact('forward'))

    def test_zero_cardinality_and_missing_lengths_remain_loadable(self):
        digest = receipts.get_review_entries_fingerprint(self.rows)
        stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
        review.save_checkpoint(str(self.book), 0, 4, 1, 0, [], stats, None, [], [],
                               digest, execution_sha256='exec')
        loaded = review.load_checkpoint(str(self.book), 4, 1, 0, self.rows,
                                        execution_sha256='exec')
        self.assertIsNotNone(loaded)
        self.assertEqual([], loaded['batch_lengths'])

    def test_legacy_fingerprint_uses_the_captured_companion_snapshot(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        target = scripts / 'book.json'
        target.write_text(json.dumps(self.rows))
        alias = self.root / 'character_aliases.json'
        alias.write_text('{"JANE":"ANN"}')
        voice = target.with_suffix('.voice_config.json')
        voice.write_text('{}')
        extra = ('--alias-registry', str(alias), '--dedupe-speakers',
                 '--remap-voice-config', str(voice))
        captured = []
        real = review.get_legacy_review_fingerprint

        def capture(*args, **kwargs):
            result = real(*args, **kwargs)
            captured.append(result)
            return result

        with patch.object(review, 'get_legacy_review_fingerprint', side_effect=capture):
            self.cli(path=target, extra=extra, use_real_dedupe=True)
        self.assertTrue(captured[0])
        path = Path(receipts.get_review_receipt_path(str(target)))
        document = json.loads(path.read_text())
        document.pop('facts')
        document['fingerprint'] = captured[0]
        path.write_text(json.dumps(document))
        reviewed = alias.read_bytes()
        external = b'{"ANN":"BEA"}'
        alias.write_bytes(external)

        def aba(*args, **kwargs):
            alias.write_bytes(reviewed)
            try:
                return real(*args, **kwargs)
            finally:
                alias.write_bytes(external)

        self.calls.clear()
        with patch.object(review, 'get_legacy_review_fingerprint', side_effect=aba):
            self.cli(path=target, extra=extra, use_real_dedupe=True)
        self.assertEqual(4, len(self.calls))
        self.assertEqual('BEA', json.loads(target.read_text())[0]['speaker'])
        self.calls.clear()
        self.cli(path=target, extra=extra, use_real_dedupe=True)
        self.assertEqual([], self.calls)

    def test_standalone_companion_identity_comes_from_its_own_operation(self):
        for external_edit in (False, True):
            with self.subTest(external_edit=external_edit):
                self.setUp()
                target = self.root / 'solo.json'
                target.write_text(json.dumps(self.rows))
                alias = self.root / 'registry.json'
                alias.write_text('{"JANE":"ANN"}')
                voice = self.root / 'voice.json'
                voice.write_text('{"JANE":{"voice":"one"}}')
                extra = ('--dedupe-speakers', '--alias-registry', str(alias),
                         '--remap-voice-config', str(voice))
                original = review._remap_voice_config

                def remap(*args, **kwargs):
                    moved = original(*args, **kwargs)
                    if external_edit:
                        alias.write_text('{"JANE":"BEA"}')
                    return moved

                with patch.object(review, '_remap_voice_config', side_effect=remap):
                    self.cli(path=target, extra=extra, use_real_dedupe=True)
                fact = self.fact('forward', target)
                current = hashlib.sha256(alias.read_bytes()).hexdigest()
                self.assertEqual(external_edit, fact['companions']['aliases']['sha256'] != current)
                self.calls.clear()
                self.cli(path=target, extra=extra, use_real_dedupe=True)
                self.assertEqual(4 if external_edit else 0, len(self.calls))

    def test_standalone_own_alias_change_with_no_voice_move_reuses(self):
        target = self.root / 'solo.json'
        seeded = [self.rows[0], {**self.rows[0], 'speaker': 'BOB'}]
        target.write_text(json.dumps(seeded))
        alias = self.root / 'registry.json'
        alias.write_text('{}')
        voice = self.root / 'voice.json'
        voice.write_text('{}')
        extra = ('--dedupe-speakers', '--alias-registry', str(alias),
                 '--remap-voice-config', str(voice))
        with patch.object(review, 'call_llm_for_object', return_value={'JANE': 'BOB'}):
            self.cli(path=target, extra=extra, use_real_dedupe=True)
        self.assertEqual({'JANE': 'BOB'}, json.loads(alias.read_text()))
        self.assertEqual('{}', voice.read_text(), 'no voice key required a move')
        fact = self.fact('forward', target)
        self.assertEqual(hashlib.sha256(alias.read_bytes()).hexdigest(),
                         fact['companions']['aliases']['sha256'])
        self.calls.clear()
        self.cli(path=target, extra=extra, use_real_dedupe=True)
        self.assertEqual([], self.calls)

    def test_late_unsupported_receipt_cannot_be_promoted(self):
        for late in (False, True):
            with self.subTest(late=late):
                self.setUp()
                target = self.root / 'scripts' / 'book.json'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(self.rows))
                self.cli(path=target)
                path = Path(receipts.get_review_receipt_path(str(target)))
                good = path.read_bytes()
                bad = json.dumps({**json.loads(good), 'version': 999}).encode()
                original = binding.publish_saved_script_edit

                def publish(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if late:
                        path.write_bytes(bad)
                    return result

                with patch.object(binding, 'publish_saved_script_edit', side_effect=publish):
                    self.cli(path=target, direction='backward')
                document = json.loads(path.read_text())
                self.assertEqual({'backward', 'forward'} if not late else {'backward'},
                                 set(document['facts']))
                self.calls.clear()
                self.cli(path=target)
                self.assertEqual(0 if not late else 4, len(self.calls))

    def test_active_review_with_stable_missing_voice_publishes_and_reuses(self):
        target = self.root / 'annotated_script.json'
        target.write_text(json.dumps(self.rows))
        alias = self.root / 'character_aliases.json'
        alias.write_text('{}')
        voice = self.root / 'voice_config.json'
        extra = ('--dedupe-speakers', '--alias-registry', str(alias),
                 '--remap-voice-config', str(voice))
        self.cli(path=target, extra=extra, use_real_dedupe=True)
        fact = self.fact('forward', target)
        self.assertIsNotNone(fact)
        self.assertIsNone(fact['companions']['voice']['sha256'])
        self.calls.clear()
        self.cli(path=target, extra=extra, use_real_dedupe=True)
        self.assertEqual([], self.calls)
        with self.assertRaisesRegex(ValueError, 'disappeared'):
            binding._voice_remap_bytes(str(self.root), 'absent.json', {}, None,
                                       expected_sha256='a' * 64)

    def test_explicit_partial_backward_publication_resumes_on_retry(self):
        def edit(rows, index):
            return [{**row, 'speaker': 'BEA'} for row in rows]

        self.cli()
        self.cli(direction='backward', gates=[True, False], transform=edit)
        self.calls.clear()
        self.cli()
        self.assertEqual([], self.calls)
        self.calls.clear()
        self.cli(direction='backward', transform=edit)
        self.assertEqual([['Blue cat.'], ['White cat.'], ['Black cat.']], self.calls)

    def test_unrelated_output_after_partial_backward_is_a_miss(self):
        def edit(rows, index):
            return [{**row, 'speaker': 'BEA'} for row in rows]

        self.cli()
        self.cli(direction='backward', gates=[True, False], transform=edit)
        self.book.write_text(json.dumps([{'speaker': 'X', 'text': 'Unrelated.'}]))
        self.calls.clear()
        log = self.cli()
        self.assertTrue(self.calls)
        self.assertNotIn('verified backward checkpoint consumes', log)

    def test_migrated_flat_receipt_keeps_validated_output_count(self):
        for migrate in (False, True):
            with self.subTest(migrate=migrate):
                self.setUp()
                target = self.root / 'scripts' / 'book.json'
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(self.rows))
                self.cli(path=target)
                if migrate:
                    path = Path(receipts.get_review_receipt_path(str(target)))
                    document = json.loads(path.read_text())
                    document.pop('facts')
                    path.write_text(json.dumps(document))
                    self.calls.clear()
                    self.cli(path=target)
                    self.assertEqual([], self.calls)
                fact = self.fact('forward', target)
                self.assertEqual(4, fact['entries_after'])
                self.assertEqual(migrate, fact['summary']['accounting_unverified'])
                self.cli(path=target, direction='backward',
                         transform=lambda rows, index: [
                             {**row, 'speaker': 'BEA'} for row in rows])
                self.calls.clear()
                self.cli(path=target)
                self.assertEqual([], self.calls)


class CoherentEvidenceTests(Harness):
    def _saved_failed_split(self):
        target = self.root / 'scripts' / 'book.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.rows))
        def answer(rows, index):
            if index == 2:
                return None
            if index == 1:
                return [{**rows[0], 'text': 'Red'}, {**rows[0], 'text': 'cat.'}, *rows[1:]]
            return rows
        self.cli(path=target, transform=answer)
        return target

    def test_corrupt_saved_checkpoint_evidence_restarts_without_certifying(self):
        for field, value in (('batch_lengths', [99]), ('batch_lengths', [0]),
                             ('batch_lengths', [1, 2, 1, 1]), ('failed_batches', [5])):
            with self.subTest(field=field, value=value):
                self.setUp()
                target = self._saved_failed_split()
                path = Path(str(target) + '.review_checkpoint.json')
                document = json.loads(path.read_text())
                document[field] = value
                path.write_text(json.dumps(document))
                self.calls.clear()
                self.cli(path=target)
                self.assertEqual([row['text'] for row in self.rows],
                                 [row['text'] for row in json.loads(target.read_text())])

    def test_in_range_source_offset_and_tampered_prefix_are_rejected(self):
        for field, value in (('source_offsets', [4]), ('all_corrected', None)):
            with self.subTest(field=field):
                self.setUp()
                target = self.root / 'book.json'
                target.write_text(json.dumps(self.rows))
                def split(rows, index):
                    if index == 1:
                        return [{**rows[0], 'text': 'Red'}, {**rows[0], 'text': 'cat.'}, *rows[1:]]
                    return rows
                self.cli(path=target, gates=[True, False], transform=split)
                path = Path(str(target) + '.review_checkpoint.json')
                document = json.loads(path.read_text())
                if field == 'source_offsets':
                    document[field] = value
                else:
                    document['all_corrected'][0]['text'] = 'Wrong text.'
                path.write_text(json.dumps(document))
                self.calls.clear()
                self.cli(path=target)
                self.assertNotIn('Wrong text.',
                                 [row['text'] for row in json.loads(target.read_text())])

    def test_legacy_split_second_interruption_resumes_remaining_rows(self):
        self.config['generation'] = {'review_batch_size': 3, 'review_target_chars': 9}
        def split(rows, index):
            if index == 1:
                return [{**rows[0], 'text': 'Red'}, {**rows[0], 'text': 'cat.'}, *rows[1:]]
            return rows
        self.cli(gates=[True, True, False], transform=split)
        path = Path(str(self.book) + '.review_checkpoint.json')
        document = json.loads(path.read_text())
        document.pop('batch_lengths')
        path.write_text(json.dumps(document))
        self.cli(gates=[True, False])
        self.cli()
        published = [row['text'] for row in json.loads(self.book.read_text())]
        self.assertEqual(['Red', 'cat.', 'Blue cat.', 'White cat.', 'Black cat.'], published)
        self.assertEqual(5, len(published), 'a split must not be lost on migration')

    def test_nonnumeric_length_is_a_miss_in_normal_and_forced_runs(self):
        def edit(rows, index):
            return [{**row, 'speaker': 'BEA'} for row in rows]
        self.cli()
        self.cli(direction='backward', gates=[True, False], transform=edit)
        path = Path(str(self.book) + '.review_checkpoint.json')
        document = json.loads(path.read_text())
        document['batch_lengths'] = ['1']
        path.write_text(json.dumps(document))
        for extra in ((), ('--force-review',)):
            with self.subTest(extra=extra):
                self.calls.clear()
                self.cli(extra=extra)
                self.assertTrue(self.calls)

    def test_parent_requires_original_path_and_companions(self):
        for variation in ('other_input', 'registry_edited'):
            with self.subTest(variation=variation):
                self.setUp()
                source = self.root / 'one.json'
                source.write_text(json.dumps(self.rows))
                alias = self.root / 'registry.json'
                alias.write_text('{"JANE":"ANN"}')
                extra = ('--alias-registry', str(alias), '--dedupe-speakers')
                self.cli(path=source, extra=extra, use_real_dedupe=True)
                if variation == 'other_input':
                    other = self.root / 'two.json'
                    other.write_bytes(source.read_bytes())
                    backward_input, out = other, source
                else:
                    alias.write_text('{"ANN":"BEA"}')
                    backward_input, out = source, source
                self.cli(path=backward_input, out=out, direction='backward', extra=extra,
                         transform=lambda rows, index: [
                             {**row, 'instruct': 'softly'} for row in rows])
                self.calls.clear()
                self.cli(path=source, extra=extra, use_real_dedupe=True)
                self.assertEqual(4, len(self.calls))

    def test_changed_in_place_input_refuses_reuse_before_publishing(self):
        def edit(rows, index):
            return [{**row, 'speaker': 'BEA'} for row in rows]

        for target in (self.book, self.root / 'scripts' / 'book.json'):
            with self.subTest(target=target.parent.name):
                target.parent.mkdir(exist_ok=True)
                target.write_text(json.dumps(self.rows))
                self.cli(path=target)
                self.cli(path=target, direction='backward', gates=[True, False], transform=edit)
                checkpoint_path, receipt_path = map(
                    Path, receipts.get_review_progress_paths(str(target)))
                progress = checkpoint_path.read_bytes(), receipt_path.read_bytes()
                replacement = json.dumps([{'speaker': 'X', 'text': 'Replacement.'}]).encode()
                original = review.get_coherent_output_snapshot

                def swap(*args, **kwargs):
                    target.write_bytes(replacement)
                    return original(*args, **kwargs)

                self.calls.clear()
                with patch.object(review, 'get_coherent_output_snapshot', side_effect=swap):
                    with self.assertRaisesRegex(ValueError, 'review input changed'):
                        self.cli(path=target)
                self.assertEqual([], self.calls)
                self.assertEqual(replacement, target.read_bytes())
                self.assertEqual(progress, (checkpoint_path.read_bytes(), receipt_path.read_bytes()))

    def test_published_partial_keeps_parent_through_repeat_and_completion(self):
        def edit(rows, index):
            return [{**row, 'speaker': 'BEA'} for row in rows]
        self.cli()
        self.cli(direction='backward', gates=[True, False], transform=edit)
        for gates in ([True, False], None):
            self.calls.clear()
            self.cli(direction='backward', gates=gates, transform=edit)
            fact = self.fact('backward') if gates is None else None
            if fact is not None:
                forward = self.fact('forward')
                self.assertEqual(forward['output_sha256'], fact['consumed_forward_sha256'])
                self.assertEqual(forward['fingerprint'], fact['consumed_forward_fingerprint'])
        self.calls.clear()
        self.cli()
        self.assertEqual([], self.calls)

    def test_required_voice_remap_failure_refuses_publication(self):
        for failure in ('write', 'lock'):
            with self.subTest(failure=failure):
                self.setUp()
                target = self.root / 'solo.json'
                target.write_text(json.dumps(self.rows))
                alias = self.root / 'registry.json'
                alias.write_text('{"JANE":"ANN"}')
                voice = self.root / 'voice.json'
                voice.write_text('{"JANE":{"voice":"one"}}')
                original_bytes = voice.read_bytes()
                extra = ('--dedupe-speakers', '--alias-registry', str(alias),
                         '--remap-voice-config', str(voice))
                original_write = review.atomic_json_write
                original_lock = review.file_lock
                def write(data, path, *args, **kwargs):
                    if failure == 'write' and path == str(voice):
                        raise OSError(errno.ENOSPC, 'fixture')
                    return original_write(data, path, *args, **kwargs)
                def lock(path, *args, **kwargs):
                    if failure == 'lock' and path == str(voice):
                        raise TimeoutError('fixture')
                    return original_lock(path, *args, **kwargs)
                with patch.object(review, 'atomic_json_write', side_effect=write), \
                        patch.object(review, 'file_lock', side_effect=lock):
                    with self.assertRaises(OSError):
                        self.cli(path=target, extra=extra, use_real_dedupe=True)
                self.assertEqual(original_bytes, voice.read_bytes())
                self.assertEqual('JANE', json.loads(target.read_text())[0]['speaker'])
                self.assertIsNone(self.fact('forward', target))

    def test_stale_invalidation_cannot_restore_a_newer_forward_fact(self):
        target = self.root / 'scripts' / 'book.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.rows))
        self.cli(path=target)
        older = self.fact('forward', target)
        path = Path(receipts.get_review_receipt_path(str(target)))
        load = receipts.load_review_receipt_document
        def interleave(*args, **kwargs):
            result = load(*args, **kwargs)
            with patch.object(receipts, 'load_review_receipt_document', load):
                self.cli(path=target, extra=('--force-review',),
                         transform=lambda rows, index: [
                             {**row, 'speaker': 'BEA'} for row in rows])
            return result
        with patch.object(receipts, 'load_review_receipt_document', interleave):
            try:
                self.cli(path=target, direction='backward')
            except ValueError:
                pass
        current = self.fact('forward', target)
        self.assertNotEqual(older['output_sha256'], current['output_sha256'])
        self.calls.clear()
        self.cli(path=target)
        self.assertEqual([], self.calls)


class EvidenceBoundaryTests(Harness):
    def _failed_split(self, saved=True, delta=False):
        target = self.root / 'scripts' / 'book.json' if saved else self.book
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.rows))
        def answer(rows, index):
            if index == 2:
                return None
            if index == 1:
                return [{**rows[0], 'text': 'Red'}, {**rows[0], 'text': 'cat.'}, *rows[1:]]
            return rows
        self.cli(path=target, transform=answer)
        path = Path(str(target) + '.review_checkpoint.json')
        document = json.loads(path.read_text())
        if delta:
            writer = gd.GenerationCheckpointDeltas(str(path), reset=True)
            writer.save_checkpoint(document)
        return target, path, document

    def _write_checkpoint(self, path, document, writer=None):
        if writer is not None:
            writer.save_checkpoint(document)
        else:
            path.write_text(json.dumps(document))

    def test_unowned_interior_lengths_and_failure_list_must_be_proven(self):
        for field, value in (('batch_lengths', [99, 1, 1, 1]),
                             ('batch_lengths', [1, 2, 1, 1]),
                             ('failed_batches', []), ('failed_batches', [3])):
            with self.subTest(field=field, value=value):
                self.setUp()
                target, path, document = self._failed_split()
                document.pop('review_batch_contributions')
                document.pop('review_accounting')
                document[field] = value
                self._write_checkpoint(path, document)
                self.calls.clear()
                self.cli(path=target, transform=lambda rows, index: [
                    {**row, 'speaker': 'CORRECTED'} if row['text'] == 'Blue cat.' else row
                    for row in rows])
                published = [row['text'] for row in json.loads(target.read_text())]
                self.assertEqual([row['text'] for row in self.rows], published)
                self.assertIn('CORRECTED',
                              [row['speaker'] for row in json.loads(target.read_text())])

    def test_retained_words_are_validated_in_compact_and_delta(self):
        for delta in (False, True):
            with self.subTest(delta=delta):
                self.setUp()
                target, path, document = self._failed_split(delta=delta)
                document['all_corrected'][0]['text'] = 'Wrong text.'
                writer = gd.GenerationCheckpointDeltas(str(path), reset=True) if delta else None
                self._write_checkpoint(path, document, writer)
                self.cli(path=target)
                self.assertNotIn('Wrong text.',
                                 [row['text'] for row in json.loads(target.read_text())])

    def test_forward_edit_and_split_keep_parent_across_two_interruptions(self):
        def forward(rows, index):
            return [{**row, 'speaker': 'ANN'} for row in rows]
        def backward(rows, index):
            return ([{**rows[0], 'text': 'Red'}, {**rows[0], 'text': 'cat.'}, *rows[1:]]
                    if index == 1 else [{**row, 'instruct': 'quiet'} for row in rows])
        self.cli(transform=forward)
        self.cli(direction='backward', gates=[True, False], transform=backward)
        counts = []
        for direction, gates in (('forward', None), ('backward', [True, False]),
                                 ('forward', None), ('backward', None), ('forward', None)):
            self.calls.clear()
            self.cli(direction=direction, gates=gates, transform=backward)
            counts.append(len(self.calls))
        self.assertEqual([0, 1, 0, 2, 0], counts)
        fact = self.fact('backward')
        forward_fact = self.fact('forward')
        self.assertEqual(forward_fact['output_sha256'], fact['consumed_forward_sha256'])
        self.assertEqual(forward_fact['entries_after'], fact['parent_entry_count'])

    def test_strict_voice_read_failures_preserve_publication(self):
        for failure in ('eio', 'malformed', 'removed'):
            with self.subTest(failure=failure):
                self.setUp()
                target = self.root / 'solo.json'
                target.write_text(json.dumps(self.rows))
                alias = self.root / 'registry.json'
                alias.write_text('{"JANE":"ANN"}')
                voice = self.root / 'voice.json'
                voice.write_text('{"JANE":{"voice":"one"}}')
                original_script, original_voice = target.read_bytes(), voice.read_bytes()
                extra = ('--alias-registry', str(alias), '--remap-voice-config', str(voice),
                         '--dedupe-speakers')
                original = review._remap_voice_config
                if failure == 'eio':
                    import builtins
                    real_open = builtins.open
                    def inject_open(path, mode='r', *args, **kwargs):
                        if str(path) == str(voice) and 'r' in mode:
                            raise OSError(errno.EIO, 'fixture')
                        return real_open(path, mode, *args, **kwargs)
                    patcher = patch('builtins.open', side_effect=inject_open)
                else:
                    def inject(*args, **kwargs):
                        if failure == 'malformed':
                            voice.write_text('{"JANE":')
                        if failure == 'removed':
                            voice.unlink()
                        return original(*args, **kwargs)
                    patcher = patch.object(review, '_remap_voice_config', side_effect=inject)
                with patcher:
                    with self.assertRaises(Exception):
                        self.cli(path=target, extra=extra, use_real_dedupe=True)
                self.assertEqual(original_script, target.read_bytes())
                self.assertIsNone(self.fact('forward', target))
                voice.write_bytes(original_voice)
                self.cli(path=target, extra=extra, use_real_dedupe=True)
                self.assertEqual('ANN', json.loads(target.read_text())[0]['speaker'])
                self.assertEqual({'ANN': {'voice': 'one'}}, json.loads(voice.read_text()))
                self.calls.clear()
                self.cli(path=target, extra=extra, use_real_dedupe=True)
                self.assertEqual([], self.calls)

    def test_in_place_replacement_guard_keeps_replacement_and_progress(self):
        def edit(rows, index):
            return [{**row, 'speaker': 'BEA'} for row in rows]
        for target in (self.book, self.root / 'scripts' / 'book.json'):
            with self.subTest(target=target.parent.name):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(self.rows))
                self.cli(path=target)
                self.cli(path=target, direction='backward', gates=[True, False], transform=edit)
                paths = list(receipts.get_review_progress_paths(str(target)))
                progress = [Path(path).read_bytes() for path in paths]
                replacement = json.dumps([{'speaker': 'X', 'text': 'Replacement.'}]).encode()
                original = review.get_coherent_output_snapshot
                def swap(*args, **kwargs):
                    target.write_bytes(replacement)
                    return original(*args, **kwargs)
                self.calls.clear()
                with patch.object(review, 'get_coherent_output_snapshot', side_effect=swap):
                    with self.assertRaisesRegex(ValueError, 'review input changed'):
                        self.cli(path=target)
                self.assertEqual([], self.calls)
                self.assertEqual(replacement, target.read_bytes())
                self.assertEqual(progress, [Path(path).read_bytes() for path in paths])


if __name__ == '__main__':
    unittest.main()
