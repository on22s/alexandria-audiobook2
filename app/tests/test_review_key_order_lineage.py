"""Lineage-bound key-order publication tests (#2257).

These discriminate the round-3 publication paths: path aliases, split/merge
lineage on checkpoint resume and receipt reuse, late receipt evidence, read
errors, cancellation and atomic rename failure.
"""
import errno
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_script
import review_script as rev
from completed_review_receipt import get_review_progress_paths
from tests import test_review_source_spans as spans
from tests.test_pass_boundary_retry_regressions import client_for
from utils import atomic_json_write, file_lock


def dump(path, thing):
    Path(path).write_text(json.dumps(thing, ensure_ascii=False, indent=2), encoding='utf-8')


def read(path):
    return json.loads(Path(path).read_bytes())


def keys(rows):
    return [list(row) for row in rows]


def reply(text, speaker='MIRA', instruct='Reviewed.'):
    return {'text': text, 'speaker': speaker, 'instruct': instruct}


def reversed_row(row):
    return dict(reversed(list(row.items())))


def fixtures():
    layouts = [['text', 'witness', 'speaker', 'instruct'],
               ['speaker', 'instruct', 'witness', 'text'],
               ['witness', 'speaker', 'text', 'instruct'],
               ['instruct', 'witness', 'speaker', 'text']]
    texts = ['Rain fell. Wind rose.', 'The bell rang.', 'Ice melted.', 'Dawn broke.']
    rows = []
    for index, (layout, text) in enumerate(zip(layouts, texts)):
        values = {**reply(text, instruct=''),
                  'witness': {'origin': index, 'beta': 2, 'alpha': 1}}
        rows.append({key: values[key] for key in layout})
    return rows


class ReviewKeyOrderLineageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='aa2-order-lineage-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def cli(self, src, out, answers, name, *flags,
            during_provider=None, matcher=None):
        client, calls = client_for([(rows, 'stop') for rows in answers])
        if during_provider is not None:
            original = client.chat.completions.create
            def edit(**request):
                during_provider()
                return original(**request)
            client.chat.completions.create = edit
        argv = ['review', '--input', str(src), '--output', str(out), *flags]
        with spans.ReviewSourceSpanTests().cli(str(self.root), client) as output, \
                patch.object(rev.sys, 'argv', argv), \
                patch.object(generate_script, 'get_response_log_path',
                             return_value=str(self.root / 'responses.log')):
            if matcher is None:
                try:
                    rev.main()
                    error = None
                except BaseException as failure:
                    error = failure
            else:
                with patch.object(rev, 'get_reusable_review_completion', side_effect=matcher):
                    try:
                        rev.main()
                        error = None
                    except BaseException as failure:
                        error = failure
        (self.root / (name + '.log')).write_text(output.getvalue(), encoding='utf-8')
        return len(calls), error

    def seed(self, directory):
        directory.mkdir(exist_ok=True)
        rows = fixtures()[:1]
        src, out = directory / 'input.json', directory / 'output.json'
        dump(src, rows)
        self.assertEqual((1, None), self.cli(src, out, [[reply(rows[0]['text'])]], 'initial'))
        return rows, src, out, Path(get_review_progress_paths(str(out))[1])

    def test_receipt_reuse_preserves_split_and_merge_layouts(self):
        rows = fixtures()
        raw = '\n\n'.join(row['text'] for row in rows)
        for row in rows:
            start = raw.index(row['text'])
            row['source_span'] = [start, start + len(row['text'])]
        src, _, _ = spans.create_book(str(self.root), raw=raw, source=rows)
        out = self.root / 'separate.json'
        answers = [[reply('Rain fell.'), reply('Wind rose.'), reply(rows[1]['text'])],
                   [reply(row['text']) for row in rows[2:]]]
        self.assertEqual((2, None), self.cli(src, out, answers, 'initial'))
        expected = keys([rows[0], rows[0], rows[1], rows[2], rows[3]])
        self.assertEqual(expected, keys(read(out)))
        self.assertEqual((0, None), self.cli(src, out, [], 'reuse'))
        self.assertEqual(expected, keys(read(out)))
        self.assertEqual((2, None), self.cli(src, out, answers, 'forced', '--force-review'))
        self.assertEqual(expected, keys(read(out)))

    def test_equal_row_count_split_and_merge_reuse_is_stable(self):
        rows = fixtures()
        rows[3]['witness'] = dict(rows[2]['witness'])
        src, out = self.root / 'input.json', self.root / 'output.json'
        dump(src, rows)
        answers = [[reply('Rain fell.'), reply('Wind rose.'), reply(rows[1]['text'])],
                   [reply(rows[2]['text'] + ' ' + rows[3]['text'])]]
        self.assertEqual((2, None), self.cli(src, out, answers, 'initial'))
        expected = keys(read(out))
        self.assertEqual(len(rows), len(read(out)))
        self.assertEqual((0, None), self.cli(src, out, [], 'reuse'))
        self.assertEqual(expected, keys(read(out)))

    def test_checkpoint_resume_rebinds_each_split_child_to_its_parent(self):
        for reorder in (False, True):
            with self.subTest(reorder=reorder), tempfile.TemporaryDirectory(
                    prefix='aa2-order-ck-') as directory:
                original_root = self.root
                self.root = Path(directory)
                rows = fixtures()
                src = self.root / 'annotated_script.json'
                dump(src, rows)
                answers = [[reply('Rain fell.'), reply('Wind rose.'), reply(rows[1]['text'])],
                           [reply(row['text']) for row in rows[2:]]]
                bad = [reply('Unrelated reply.')]
                self.assertEqual((4, None), self.cli(src, src,
                                                     [answers[0], bad, bad, bad], 'failed'))
                if reorder:
                    rows = [reversed_row(row) for row in rows]
                    dump(src, rows)
                self.assertEqual((1, None), self.cli(src, src, [answers[1]], 'resume'))
                published = read(src)
                parents = [row['witness']['origin'] for row in published]
                self.assertEqual([0, 0, 1, 2, 3], parents)
                self.assertEqual(keys([rows[index] for index in parents]), keys(published))
                self.assertEqual(['Reviewed.'] * 5, [row['instruct'] for row in published])
                self.root = original_root

    def test_legacy_checkpoint_prefix_without_batch_lengths(self):
        later = [{**row, 'text': text} for row, text in zip(
            fixtures(), ['Stars faded.', 'Clouds lifted.'])]
        rows = fixtures() + later
        src = self.root / 'annotated_script.json'
        dump(src, rows)
        answers = [[reply(row['text']) for row in rows[index:index + 2]]
                   for index in (0, 2, 4)]
        save = rev.save_checkpoint
        class Interruption(Exception):
            pass
        def interrupt(*args, **kwargs):
            result = save(*args, **kwargs)
            if args[1] == 2:
                raise Interruption()
            return result
        with patch.object(rev, 'save_checkpoint', side_effect=interrupt):
            _, error = self.cli(src, src, answers, 'interrupted')
        self.assertIsInstance(error, Interruption)
        checkpoint = Path(get_review_progress_paths(str(src))[0])
        proof = rev.load_generation_checkpoint_document(str(checkpoint))
        for field in ('batch_lengths', 'review_accounting', 'review_batch_contributions'):
            proof.pop(field)
        dump(checkpoint, proof)
        reordered = [reversed_row(row) for row in rows]
        dump(src, reordered)
        self.assertEqual((1, None), self.cli(src, src, [answers[2]], 'resume'))
        published = read(src)
        self.assertEqual(keys(reordered), keys(published))
        self.assertEqual(['Reviewed.'] * len(rows), [row['instruct'] for row in published])

    def test_standalone_parent_alias_refuses_concurrent_edits(self):
        for value in (False, True):
            with self.subTest(value=value):
                directory = self.root / ('value' if value else 'order')
                directory.mkdir()
                real = directory / 'real-dir'
                real.mkdir()
                alias = directory / 'dir-link'
                alias.symlink_to(real, target_is_directory=True)
                src, out = real / 'book.json', alias / 'book.json'
                rows = fixtures()[:1]
                dump(src, rows)
                replacement = [reversed_row(rows[0])]
                if value:
                    replacement[0]['instruct'] = 'Editor saved this.'
                def writer():
                    with file_lock(str(src)):
                        atomic_json_write(replacement, str(src), allow_nonatomic_fallback=False)
                        (directory / 'editor-bytes.json').write_bytes(src.read_bytes())
                _, error = self.cli(src, out, [[reply(rows[0]['text'])]],
                                    'concurrent', during_provider=writer)
                self.assertIsInstance(error, ValueError)
                self.assertEqual(src.read_bytes(),
                                 (directory / 'editor-bytes.json').read_bytes())
                self.assertFalse(Path(get_review_progress_paths(str(out))[1]).exists())

    def test_receipt_reuse_revalidates_output_replaced_under_the_lock(self):
        rows, src, out, receipt = self.seed(self.root / 'replace')
        dump(src, [reversed_row(row) for row in rows])
        approved, edited = threading.Event(), threading.Event()
        captured = {}
        match = rev.get_reusable_review_completion
        def signal(*args, **kwargs):
            result = match(*args, **kwargs)
            self.assertTrue(result[0])
            approved.set()
            self.assertTrue(edited.wait(10))
            return result
        def writer():
            try:
                self.assertTrue(approved.wait(10))
                with file_lock(str(out)):
                    newer = read(out)
                    newer[0]['instruct'] = 'Newer editor version.'
                    atomic_json_write(newer, str(out), allow_nonatomic_fallback=False)
                    captured['bytes'] = out.read_bytes()
            finally:
                edited.set()
        thread = threading.Thread(target=writer)
        thread.start()
        _, error = self.cli(src, out, [], 'raced', matcher=signal)
        thread.join(12)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(error, ValueError)
        self.assertEqual(captured['bytes'], out.read_bytes())
        self.assertNotIn('Task review completed successfully.',
                         (self.root / 'raced.log').read_text())

    def test_receipt_reuse_read_error_and_missing_output_fail_loud(self):
        rows, src, out, receipt = self.seed(self.root / 'read-error')
        dump(src, [reversed_row(row) for row in rows])
        old, proof = out.read_bytes(), receipt.read_bytes()
        match = rev.get_reusable_review_completion
        matched = False
        def capture(*args, **kwargs):
            nonlocal matched
            result = match(*args, **kwargs)
            matched = bool(result[0])
            return result
        def fail_read(path, *args, **kwargs):
            if matched and str(path) == str(out):
                raise OSError(errno.EIO, 'synthetic second-read error')
            return open(path, *args, **kwargs)
        with patch.object(rev, 'get_reusable_review_completion', side_effect=capture), \
                patch.object(rev, 'open', side_effect=fail_read, create=True):
            _, error = self.cli(src, out, [], 'failed-read')
        self.assertTrue(matched)
        self.assertIsInstance(error, OSError)
        self.assertEqual(old, out.read_bytes())
        self.assertEqual(proof, receipt.read_bytes())
        self.assertNotIn('Task review completed successfully.',
                         (self.root / 'failed-read.log').read_text())
        self.assertEqual((0, None), self.cli(src, out, [], 'retry'))

    def test_receipt_reuse_honours_cancellation(self):
        rows, src, out, receipt = self.seed(self.root / 'cancel')
        dump(src, [reversed_row(row) for row in rows])
        old, proof = out.read_bytes(), receipt.read_bytes()
        cancel = self.root / 'cancel-file'
        snapshot = rev.get_coherent_output_snapshot
        def request_cancel(*args, **kwargs):
            cancel.touch()
            return snapshot(*args, **kwargs)
        with patch.dict(os.environ, {'ALEXANDRIA_REVIEW_CANCEL_FILE': str(cancel)}), \
                patch.object(rev, 'get_coherent_output_snapshot', side_effect=request_cancel):
            _, error = self.cli(src, out, [], 'cancelled')
        self.assertIsInstance(error, SystemExit)
        self.assertEqual(old, out.read_bytes())
        self.assertEqual(proof, receipt.read_bytes())

    def test_rebind_rename_failure_preserves_publication_and_retry_repairs(self):
        rows, src, out, receipt = self.seed(self.root / 'rename')
        reordered = [reversed_row(row) for row in rows]
        dump(src, reordered)
        old, proof = out.read_bytes(), receipt.read_bytes()
        replace = os.replace
        def cross_device(source, target, *args, **kwargs):
            if os.path.abspath(target) == str(out):
                raise OSError(errno.EXDEV, 'synthetic rename failure')
            return replace(source, target, *args, **kwargs)
        with patch.object(os, 'replace', side_effect=cross_device):
            _, error = self.cli(src, out, [], 'exdev')
        self.assertIsInstance(error, OSError)
        self.assertEqual(errno.EXDEV, error.errno)
        self.assertEqual(old, out.read_bytes())
        self.assertEqual(proof, receipt.read_bytes())
        self.assertEqual([], list(self.root.glob('.tmp_*.json')))
        self.assertEqual((0, None), self.cli(src, out, [], 'retry'))
        self.assertEqual(keys(reordered), keys(read(out)))


    def test_merge_last_parent_sharing_text_with_a_retained_row_is_stable(self):
        keysets = [['text', 'zeta', 'speaker', 'instruct'],
                   ['speaker', 'zeta', 'text', 'instruct'],
                   ['instruct', 'zeta', 'speaker', 'text']]
        def row(text, order, span):
            values = {'text': text, 'zeta': 'v', 'speaker': 'NARRATOR',
                      'instruct': 'original', 'source_span': span}
            return {key: values[key] for key in order}
        texts = ['Alpha.', 'Bravo.', 'bravo!']
        raw = '\n\n'.join(texts)
        offsets, source_spans = 0, []
        for text in texts:
            source_spans.append([offsets, offsets + len(text)])
            offsets += len(text) + 2
        rows = [row(text, keysets[index], source_spans[index])
                for index, text in enumerate(texts)]
        src, _, _ = spans.create_book(str(self.root), raw=raw, source=rows)
        out = self.root / 'separate.json'
        replies = [[reply('Alpha. Bravo.')], [reply('bravo!')]]
        self.assertEqual((2, None), self.cli(src, out, replies, 'initial'))
        first = out.read_bytes()
        self.assertEqual([keysets[1], keysets[2]], keys(read(out)))
        calls, error = self.cli(src, out, [], 'reuse')
        self.assertEqual((0, None), (calls, error))
        self.assertEqual(first, out.read_bytes())

    def test_checkpoint_merge_then_identical_retained_row(self):
        keysets = [['text', 'zeta', 'speaker', 'instruct'],
                   ['speaker', 'zeta', 'text', 'instruct'],
                   ['instruct', 'zeta', 'speaker', 'text'],
                   ['witness', 'zeta', 'speaker', 'text', 'instruct']]
        for reorder in (False, True):
            with self.subTest(reorder=reorder), tempfile.TemporaryDirectory(
                    prefix='aa2-order-merge-') as directory:
                original_root = self.root
                self.root = Path(directory)
                texts = ['Alpha.', 'Bravo.', 'bravo!', 'Delta.']
                raw = '\n\n'.join(texts)
                offsets, source_spans = 0, []
                for text in texts:
                    source_spans.append([offsets, offsets + len(text)])
                    offsets += len(text) + 2
                def row(text, order, span):
                    values = {'text': text, 'zeta': 'v', 'witness': 'w',
                              'speaker': 'NARRATOR', 'instruct': 'original',
                              'source_span': span}
                    return {key: values[key] for key in order}
                rows = [row(text, keysets[index], source_spans[index])
                        for index, text in enumerate(texts)]
                src = self.root / 'annotated_script.json'
                dump(src, rows)
                bad = [reply('Unrelated reply.')]
                self.assertEqual((4, None), self.cli(
                    src, src, [[reply('Alpha. Bravo.')], bad, bad, bad], 'failed'))
                if reorder:
                    rows = [reversed_row(row) for row in rows]
                    dump(src, rows)
                calls, error = self.cli(
                    src, src, [[reply('bravo!'), reply('Delta.')]], 'resume')
                self.assertIsNone(error)
                self.assertIn(calls, (1, 2))
                published = read(src)
                expected = [rows[1], rows[2], rows[3]]
                self.assertEqual(keys(expected), keys(published))
                self.assertEqual(['Reviewed.'] * 3,
                                 [row['instruct'] for row in published])
                self.root = original_root

    def test_ambiguous_repeated_text_lineage_fails_safe(self):
        def row(text, order):
            values = {'text': text, 'zeta': 'v', 'speaker': 'MIRA', 'instruct': ''}
            return {key: values[key] for key in order}
        first = ['text', 'zeta', 'speaker', 'instruct']
        second = ['speaker', 'zeta', 'text', 'instruct']
        third = ['instruct', 'zeta', 'speaker', 'text']
        self.assertIsNone(rev.get_review_source_order_templates(
            [row('a', first), row('b', second)],
            [row('a', first), row('b', second), row('b', third)]))
        self.assertEqual([first, second], rev.get_review_source_order_templates(
            [row('a', first), row('b', second)],
            [row('a', first), row('b', second)]))


if __name__ == '__main__':
    unittest.main()
