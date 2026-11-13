"""Synthetic follow-ups for publisher boundaries and whitespace cast requests."""
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

import cast_list
import source_normalization as normalization
from tests.test_incremental_cast import ALICE, Client
from tests import test_incremental_cast as fixtures

SEPARATORS = ('\n\n', '\r\n\r\n', '\r\r', '\n\u3000\n',
              '\n\u00a0\n', '\u2028\u2028', '\u2029')


class ParagraphFollowupTests(unittest.TestCase):
    def test_publisher_stripping_preserves_exact_story_for_every_separator(self):
        for separator in SEPARATORS:
            front = separator.join(('Copyright', 'All rights reserved.',
                                    'Translation by Synthetic Translator')) + separator
            story = separator.join(f'“Alice opened synthetic door {i},” Bob said.'
                                   for i in range(12)) + separator
            back = separator.join(('Copyright', 'All rights reserved.',
                                   'ISBN 978-1-234-56789-0'))
            with self.subTest(separator=repr(separator)):
                result, report = normalization.strip_publisher_matter(front + story + back)
                self.assertEqual(story, result)
                self.assertEqual({'front_paragraphs': 3, 'back_paragraphs': 3}, report)

    def test_story_mentions_and_all_metadata_remain_untouched(self):
        for separator in SEPARATORS:
            story = separator.join(('“Read the copyright page,” Alice said.',
                                    *('Alice walked home.' for _ in range(12)),
                                    '“All rights reserved,” Bob read aloud.'))
            metadata = separator.join(('Copyright', 'All rights reserved.') * 8)
            for source in (story, metadata, separator + story + separator):
                with self.subTest(separator=repr(separator), source=source):
                    self.assertEqual((source, {'front_paragraphs': 0, 'back_paragraphs': 0}),
                                     normalization.strip_publisher_matter(source))

    def test_shared_paragraph_ranges_are_lossless_and_match_constructed_units(self):
        rng = random.Random(1945)
        for _ in range(150):
            units = [f'Paragraph {i}: ' + ''.join(rng.choice('abc 界😀') for _ in range(rng.randrange(10)))
                     + rng.choice(SEPARATORS) for i in range(rng.randrange(1, 20))]
            # Non-whitespace content prevents adjacent separators coalescing.
            source = ''.join(units) + 'Tail.'
            spans = list(normalization.iter_source_paragraph_ranges(source))
            self.assertEqual(units + ['Tail.'], [source[start:end] for start, end in spans])
            self.assertEqual(source, ''.join(source[start:end] for start, end in spans))
            self.assertEqual(0, spans[0][0])
            self.assertEqual(len(source), spans[-1][1])

    def test_whitespace_window_records_coverage_without_a_model_request(self):
        for whitespace in ('\n' * 20, '\u2029' * 20, '\n\u3000' * 9 + '\n\n'):
            source = whitespace + 'ALICE spoke.'
            client, progress = Client([ALICE]), []
            with self.subTest(whitespace=repr(whitespace)):
                cast, windows, dropped = cast_list.request_incremental_cast(
                    client, 'fixture', source, 20, 8000, report=progress.append)
                self.assertEqual(ALICE, cast)
                self.assertEqual([], dropped)
                self.assertEqual(1, len(client.requests))
                self.assertEqual([(0, 20), (20, len(source))], [(w['start'], w['end']) for w in windows])
                self.assertEqual('skipped_whitespace', windows[0]['status'])
                self.assertEqual(0, windows[0]['attempts'])
                self.assertIsNone(windows[0]['request_sha256'])
                self.assertEqual({'prompt_tokens': 0, 'completion_tokens': 0}, windows[0]['usage'])
                self.assertEqual(hashlib.sha256(whitespace.encode()).hexdigest(), windows[0]['source_sha256'])
                self.assertIn('no model request', progress[0])
                self.assertIn('Cast window 2/2:', progress[-1])

    def test_whitespace_only_source_preserves_prior_cast_without_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
            source.write_text('\n' * 20, encoding='utf8')
            out.write_bytes(b'previous complete cast')
            client = Client([ALICE])
            result = fixtures.IncrementalCastTests.run_main(self, source, out, client,
                                                  '--incremental', '--source-chars', '20')
            self.assertEqual(1, result)
            self.assertEqual([], client.requests)
            self.assertEqual(b'previous complete cast', out.read_bytes())

    def test_cli_audits_skipped_window_and_real_request_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
            source.write_text('\n' * 20 + 'ALICE spoke.', encoding='utf8')
            client = Client([ALICE])
            self.assertEqual(0, fixtures.IncrementalCastTests.run_main(self, source, out, client,
                             '--incremental', '--source-chars', '20'))
            provenance = json.loads(out.read_text(encoding='utf8'))['provenance']
            self.assertEqual(1, len(client.requests))
            self.assertEqual(1, provenance['attempts'])
            self.assertEqual('skip-whitespace-v1', provenance['empty_window_policy'])
            self.assertTrue(provenance['complete'])
            self.assertEqual('skipped_whitespace', provenance['windows'][0]['status'])
            self.assertIsNone(provenance['usage']['prompt_tokens'])

    def test_generation_preparation_and_cast_cli_share_unicode_stripping(self):
        from generate_script import get_preprocessed_source
        separator = '\n\u3000\n'
        front = separator.join(('Copyright', 'All rights reserved.',
                                'Translation by Synthetic Translator')) + separator
        story = separator.join(f'“Alice opened synthetic door {i},” Bob said.'
                               for i in range(12)) + separator
        back = separator.join(('Copyright', 'All rights reserved.', 'ISBN 978-1-234-56789-0'))
        raw = front + story + back
        self.assertEqual(story, get_preprocessed_source(raw)[0])
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
            source.write_text(raw, encoding='utf8')
            before = source.read_bytes()
            client = Client([ALICE] * 20)
            self.assertEqual(0, fixtures.IncrementalCastTests.run_main(self, source, out, client,
                             '--incremental', '--source-chars', '100'))
            payload = json.loads(out.read_text(encoding='utf8'))
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(hashlib.sha256(story.encode()).hexdigest(),
                             payload['provenance']['prepared_source_sha256'])
            self.assertGreater(len(client.requests), 1)
            for request in client.requests:
                self.assertNotIn('Copyright', request['messages'][0]['content'])

    def test_cancel_after_skipped_whitespace_preserves_old_cast(self):
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'book.txt', Path(directory) / 'cast.json'
            source.write_text('\n' * 20 + 'ALICE spoke.\n\nBOB spoke.', encoding='utf8')
            out.write_bytes(b'previous complete cast')
            client = Client([ALICE, KeyboardInterrupt()])
            self.assertEqual(1, fixtures.IncrementalCastTests.run_main(self, source, out, client,
                             '--incremental', '--source-chars', '20'))
            self.assertEqual(2, len(client.requests))
            self.assertEqual(b'previous complete cast', out.read_bytes())
