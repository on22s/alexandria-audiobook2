"""Synthetic paragraph-boundary and source-coverage regressions for #1945."""
import hashlib
import random
import tempfile
import unittest
from pathlib import Path

import cast_list
from tests.test_incremental_cast import ALICE, BOB, Client
from tests import test_incremental_cast as fixtures


class CastParagraphWindowTests(unittest.TestCase):
    def test_windows_end_at_complete_paragraphs_instead_of_inside_words(self):
        paragraphs = ['ALICE spoke calmly.\n\n', 'BORIN replied softly.\n\n', 'The narrator paused.']
        text = ''.join(paragraphs)
        windows = list(cast_list.iter_source_windows(text, 35))
        self.assertEqual(paragraphs, [part for _, _, part in windows])
        self.assertEqual(text, ''.join(part for _, _, part in windows))

    def test_generated_windows_preserve_bounded_paragraphs_and_exact_offsets(self):
        rng = random.Random(1945)
        for case in range(250):
            paragraphs = []
            for index in range(rng.randrange(1, 20)):
                body = ''.join(rng.choice('abc界😀 ') for _ in range(rng.randrange(1, 40))) + '.'
                if rng.randrange(2):
                    body += '\nWrapped text.'  # A single line break is not a paragraph break.
                paragraphs.append(body + rng.choice(['\n\n', '\n \t\n', '\r\n\r\n', '\r\r',
                                                     '\n\u00a0\n', '\n\u3000\n',
                                                     '\u2028\u2028', '\u2029', '\u2029\u2029']))
            if rng.randrange(2):
                paragraphs.append('Final paragraph.')
            text = ''.join(paragraphs)
            limit = rng.randrange(max(map(len, paragraphs)), max(map(len, paragraphs)) + 100)
            ends, position = [], 0
            for paragraph in paragraphs:
                position += len(paragraph)
                ends.append(position)
            with self.subTest(case=case, limit=limit):
                windows = list(cast_list.iter_source_windows(text, limit))
                self.assertEqual(text, ''.join(part for _, _, part in windows))
                cursor = 0
                for start, end, part in windows:
                    self.assertEqual(cursor, start)
                    self.assertEqual(text[start:end], part)
                    self.assertTrue(0 < len(part) <= limit)
                    self.assertIn(end, ends)
                    if end != len(text):
                        self.assertGreater(ends[ends.index(end) + 1] - start, limit)
                    cursor = end
                self.assertEqual(len(text), cursor)

    def test_single_newline_and_crlf_do_not_split_a_wrapped_paragraph(self):
        for newline in ('\n', '\r\n', '\r', '\u2028'):
            text = 'A sentence' + newline + 'continues here.'
            with self.subTest(newline=repr(newline)), self.assertRaisesRegex(ValueError, 'paragraph|Paragraph'):
                list(cast_list.iter_source_windows(text, 20))

    def test_empty_exact_limit_tail_and_separator_boundaries(self):
        for text, limit, expected in (
                ('', 1, []), ('Tiny.', 5, ['Tiny.']),
                ('A.\n\nB.', 4, ['A.\n\n', 'B.']),
                ('A.\n\u3000\nB.', 5, ['A.\n\u3000\n', 'B.']),
                ('A.\n\u00a0\nB.', 5, ['A.\n\u00a0\n', 'B.']),
                ('A.\u2028\u2028B.', 4, ['A.\u2028\u2028', 'B.']),
                ('A.\u2029B.', 3, ['A.\u2029', 'B.']),
                ('A.\u2029\u2029B.', 4, ['A.\u2029\u2029', 'B.']),
                ('ALPHA.\r\n\r\nBETA.', 10, ['ALPHA.\r\n\r\n', 'BETA.']),
                (' A.\n \t\n  B. ', 7, [' A.\n \t\n', '  B. '])):
            with self.subTest(text=repr(text)):
                self.assertEqual(expected, [part for _, _, part in cast_list.iter_source_windows(text, limit)])
        with self.assertRaisesRegex(ValueError, 'including its separator'):
            list(cast_list.iter_source_windows('A.\n\n', 3))

    def test_late_oversized_paragraph_refuses_before_any_model_request(self):
        client = Client([ALICE] * 20)
        with self.assertRaisesRegex(ValueError, 'Paragraph.*Increase Cast source characters.*add blank-line paragraph breaks'):
            cast_list.request_incremental_cast(client, 'fixture', 'Short.\n\n' + 'x' * 36, 35, 8000)
        self.assertEqual([], client.requests)

    def test_cli_preserves_unicode_blank_line_paragraphs(self):
        for separator in ('\n\u3000\n', '\u2028\u2028', '\u2029', '\u2029\u2029'):
            with self.subTest(separator=repr(separator)), tempfile.TemporaryDirectory() as directory:
                source, out = Path(directory) / 'source.txt', Path(directory) / 'cast.json'
                source.write_text('ALICE spoke.' + separator + 'BORIN replied.', encoding='utf-8')
                client = Client([ALICE, BOB])
                self.assertEqual(0, fixtures.IncrementalCastTests.run_main(
                    self, source, out, client, '--incremental', '--source-chars', '20'))
                self.assertEqual(2, len(client.requests))
                self.assertIn(separator, client.requests[0]['messages'][0]['content'])

    def test_actual_progress_roster_and_provenance_follow_packed_windows(self):
        paragraphs = ['ALICE spoke calmly.\n\n', 'BORIN replied softly.\n\n', 'The narrator paused.']
        client, progress = Client([ALICE, [], BOB]), []
        cast, windows, _ = cast_list.request_incremental_cast(
            client, 'fixture', ''.join(paragraphs), 35, 8000, report=progress.append)
        self.assertEqual(ALICE + BOB, cast)
        self.assertEqual(3, len(windows))
        self.assertIn('Cast window 3/3:', progress[-1])
        for index, request in enumerate(client.requests):
            prompt = request['messages'][0]['content']
            self.assertTrue(prompt.endswith(paragraphs[index]))
            self.assertEqual(hashlib.sha256(paragraphs[index].encode()).hexdigest(), windows[index]['source_sha256'])
            if index:
                self.assertIn('THE PILOT', prompt)

    def test_oversized_paragraph_keeps_existing_cast_and_reports_action(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'source.txt', Path(directory) / 'cast.json'
            source.write_text('Short.\n\n' + 'x' * 36, encoding='utf-8')
            out.write_bytes(b'previous complete cast')
            client = Client([ALICE] * 20)
            result = fixtures.IncrementalCastTests.run_main(self, source, out, client,
                                                           '--incremental', '--source-chars', '35')
            self.assertEqual(1, result)
            self.assertEqual([], client.requests)
            self.assertEqual(b'previous complete cast', out.read_bytes())
