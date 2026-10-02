"""Controlled concurrent book readers must use their own respelling maps."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pronunciation


class PronunciationConcurrencyTests(unittest.TestCase):
    def test_other_book_loading_between_load_and_snapshot_cannot_change_output(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = (Path(directory) / name for name in ('a.json', 'b.json'))
            a.write_text(json.dumps({'Subaru': 'First'}))
            b.write_text(json.dumps({'Subaru': 'Second'}))
            ready, proceed, b_started, b_finished = [threading.Event() for _ in range(4)]
            actual_load = pronunciation.load_lexicon

            def load(path=None, force=False):
                result = actual_load(path, force)
                if path == str(a):
                    ready.set()
                    if not proceed.wait(3):
                        raise AssertionError('fixture did not release first book')
                return result

            def apply_b():
                b_started.set()
                result = pronunciation.apply_pronunciation('Subaru', str(b))
                b_finished.set()
                return result

            with patch.object(pronunciation, 'load_lexicon', side_effect=load), ThreadPoolExecutor(2) as pool:
                first = pool.submit(pronunciation.apply_pronunciation, 'Subaru', str(a))
                try:
                    self.assertTrue(ready.wait(3))
                    second = pool.submit(apply_b)
                    self.assertTrue(b_started.wait(3))
                    # Old code allows B to finish before A reads the global cache.
                    b_finished.wait(0.2)
                finally:
                    proceed.set()
                self.assertEqual(('First', [{'name': 'Subaru', 'spoken': 'First'}]), first.result(3))
                self.assertEqual(('Second', [{'name': 'Subaru', 'spoken': 'Second'}]), second.result(3))

    def test_concurrent_valid_missing_and_malformed_books_keep_results_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid, malformed, missing = [root / name for name in ('valid.json', 'bad.json', 'missing.json')]
            valid.write_text(json.dumps({'Subaru': 'Soo-bah-roo', 'Natsuki Subaru': 'Full name'}))
            malformed.write_text('{broken')
            cases = [(str(valid), ('Full name met Soo-bah-roo', [
                {'name': 'Natsuki Subaru', 'spoken': 'Full name'},
                {'name': 'Subaru', 'spoken': 'Soo-bah-roo'}])),
                (str(malformed), ('Natsuki Subaru met Subaru', [])),
                (str(missing), ('Natsuki Subaru met Subaru', []))] * 40
            with ThreadPoolExecutor(6) as pool:
                results = list(pool.map(lambda case: pronunciation.apply_pronunciation(
                    'Natsuki Subaru met Subaru', case[0]), cases))
            self.assertEqual([case[1] for case in cases], results)
