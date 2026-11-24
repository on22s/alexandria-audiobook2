"""Publication-boundary regressions for the preserved key order (#2257).

These cover paths that do not go through the per-row review builders: literal
policy retention, checkpoint resume, completed-receipt reuse and the standalone
publisher's late-source fence.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import generate_script
import review_script as review
from completed_review_receipt import get_review_progress_paths, get_review_receipt_path
from literal_policy import SCRIPT_POLICY_KEY, bind_literal_policy
from tests.test_pass_boundary_retry_regressions import client_for
from tests import test_review_source_spans as spans
from utils import atomic_json_write, file_lock


def write(path, thing):
    Path(path).write_text(json.dumps(thing, indent=2, ensure_ascii=False), encoding='utf-8')


def read(path):
    return json.loads(Path(path).read_bytes())


def keys(rows):
    return [list(row) for row in rows]


class ReviewKeyOrderPublicationTests(unittest.TestCase):
    def invoke(self, root, src, out, replies, *flags):
        client, calls = client_for([] if callable(replies)
                                   else [(rows, 'stop') for rows in replies])
        if callable(replies):
            def answer(**request):
                calls.append(request)
                prompt = request['messages'][-1]['content']
                current, _ = json.JSONDecoder().raw_decode(prompt[prompt.rindex('[\n  {'):])
                return client_for([(replies(current), 'stop')])[0].chat.completions.create(**request)
            client.chat.completions.create = answer
        argv = ['review_script.py', '--input', str(src), '--output', str(out), *flags]
        with spans.ReviewSourceSpanTests().cli(str(root), client, script=src), \
                patch.object(review.sys, 'argv', argv), \
                patch.object(generate_script, 'get_response_log_path',
                             return_value=str(Path(root) / 'responses.log')), \
                redirect_stdout(io.StringIO()):
            review.main()
        return len(calls)

    def test_literal_marker_keeps_position_when_row_is_unchanged(self):
        for labels in (False, True):
            with self.subTest(labels=labels), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                raw = 'A copper bell rang.'
                source = bind_literal_policy(raw, [
                    {'text': raw, 'speaker': 'MIRA', 'instruct': 'Even.'}])
                source[0]['legacy_note'] = {'z': 'first', 'a': 'last'}
                src, _, _ = spans.create_book(str(root), raw=raw, source=source)
                reply = {'speaker': 'MIRA', 'instruct': 'Even.'}
                reply.update({'n': 0} if labels else {'text': raw})
                flags = ('--labels-only',) if labels else ()
                calls = self.invoke(root, src, src, [[reply]], *flags)
                published = read(src)
                self.assertEqual(1, calls)
                self.assertEqual(keys(source), keys(published))
                self.assertEqual(list(source[0][SCRIPT_POLICY_KEY]),
                                 list(published[0][SCRIPT_POLICY_KEY]))

    def test_literal_marker_is_not_displaced_by_a_new_annotation(self):
        raw = 'A copper bell rang.'
        source = bind_literal_policy(raw, [
            {'text': raw, 'speaker': 'MIRA', 'instruct': 'Even.'}])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, _, _ = spans.create_book(str(root), raw=raw, source=source)
            calls = self.invoke(root, src, src,
                                [[{'text': raw, 'speaker': 'MIRA',
                                   'instruct': 'Even.', 'spoken': True}]])
            published = read(src)
            self.assertEqual(1, calls)
            self.assertEqual(list(source[0]) + ['spoken'], list(published[0]))

    def test_checkpoint_resume_rebinds_retained_rows_to_current_source_order(self):
        rows = [
            {'text': text, 'speaker': 'MIRA', 'custom': {'z': 1, 'a': 2},
             'instruct': '', 'spoken': True}
            for text in ('The bell rang.', 'The cat slept.',
                         'The door opened.', 'The rain stopped.')
        ]
        def model_rows(source):
            return [{'instruct': 'Edited.', 'speaker': row['speaker'], 'text': row['text']}
                    for row in source]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'annotated_script.json'
            write(src, rows)
            before = src.read_bytes()
            bad = [{'speaker': 'MIRA', 'text': 'Unrelated reply.', 'instruct': ''}]
            self.assertEqual(4, self.invoke(root, src, src,
                                            [model_rows(rows[:2]), bad, bad, bad]))
            self.assertEqual(before, src.read_bytes())
            checkpoint = Path(get_review_progress_paths(str(src))[0])
            proof = review.load_generation_checkpoint_document(str(checkpoint))
            self.assertEqual([2], proof['failed_batches'])
            proof['all_corrected'] = [dict(reversed(list(row.items())))
                                      for row in proof['all_corrected']]
            write(checkpoint, proof)
            self.invoke(root, src, src, model_rows)
            published = read(src)
            self.assertEqual(keys(rows), keys(published))
            self.assertEqual(['Edited.'] * len(rows),
                             [row['instruct'] for row in published])

    def test_receipt_reuse_rebinds_output_after_order_only_input_edit(self):
        source = [{'text': 'Rain fell.', 'speaker': 'ADA',
                   'custom': {'beta': 2, 'alpha': 1}, 'instruct': ''}]
        replies = [[{'text': source[0]['text'], 'speaker': 'ADA', 'instruct': 'Edited.'}]]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, out = root / 'input.json', root / 'output.json'
            write(src, source)
            self.assertEqual(1, self.invoke(root, src, out, replies))
            self.assertEqual(0, self.invoke(root, src, out, replies))
            reordered = [dict(reversed(list(row.items()))) for row in source]
            write(src, reordered)
            self.assertEqual(0, self.invoke(root, src, out, replies))
            self.assertEqual(keys(reordered), keys(read(out)))
            self.assertEqual('Edited.', read(out)[0]['instruct'])

    def test_receipt_reuse_upgrades_a_prior_out_of_order_publication(self):
        source = [{'text': 'Rain fell.', 'speaker': 'ADA',
                   'custom': {'beta': 2, 'alpha': 1}, 'instruct': ''}]
        replies = [[{'text': source[0]['text'], 'speaker': 'ADA', 'instruct': 'Edited.'}]]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, out = root / 'input.json', root / 'output.json'
            write(src, source)
            self.assertEqual(1, self.invoke(root, src, out, replies))
            write(out, [dict(reversed(list(row.items()))) for row in read(out)])
            self.assertEqual(0, self.invoke(root, src, out, replies))
            self.assertEqual(keys(source), keys(read(out)))

    def test_standalone_publication_refuses_a_concurrent_key_reorder(self):
        row = {'text': 'A bell rang.', 'speaker': 'MIRA', 'instruct': '', 'spoken': True}
        concurrent = dict(reversed(list(row.items())))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'book.json'
            write(src, [row])
            client, calls = client_for([
                ([{'text': row['text'], 'speaker': 'MIRA', 'instruct': 'Reviewed.'}], 'stop')])
            original = client.chat.completions.create
            def writer(**request):
                with file_lock(str(src)):
                    atomic_json_write([concurrent], str(src), allow_nonatomic_fallback=False)
                return original(**request)
            client.chat.completions.create = writer
            with spans.ReviewSourceSpanTests().cli(str(root), client, script=src), \
                    patch.object(generate_script, 'get_response_log_path',
                                 return_value=str(root / 'responses.log')), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    review.main()
            self.assertEqual(1, len(calls))
            self.assertEqual([concurrent], read(src))
            self.assertEqual([list(concurrent)], keys(read(src)))
            self.assertFalse(Path(get_review_receipt_path(str(src))).exists())


if __name__ == '__main__':
    unittest.main()
