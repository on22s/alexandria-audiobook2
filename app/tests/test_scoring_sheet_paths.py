import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build_scoring_sheet as sheet


class ScoringSheetPathTests(unittest.TestCase):
    def checkpoint(self, root, model, book, speaker='ALICE'):
        path = root / model / book / 'result.json.threepass_checkpoint.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'named': [{'text': 'Hello.', 'speaker': speaker}]}))
        return path

    def test_invalid_book_tags_fail_before_any_glob_or_read(self):
        with patch.object(sheet.glob, 'glob') as search, patch.object(sheet, 'load_named') as read:
            for book in ('', '.', '..', '../outside', 'a/b', 'a\\b', '/outside', 'book\x00', None):
                with self.subTest(book=repr(book)), self.assertRaises(ValueError):
                    sheet.find_model_runs('/fixture results', book)
            search.assert_not_called(); read.assert_not_called()

    def test_literal_glob_characters_in_root_and_book_select_only_exact_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'results [matrix]*'; root.mkdir()
            path = self.checkpoint(root, 'model A', 'book [one]* 猫')
            self.checkpoint(root, 'model B', 'book o 猫', 'BOB')
            original = path.read_bytes()
            self.assertEqual({'model A': [{'text': 'Hello.', 'speaker': 'ALICE'}]},
                             sheet.find_model_runs(str(root), 'book [one]* 猫'))
            self.assertEqual(original, path.read_bytes())

    def test_matching_symlink_cannot_read_outside_result_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); results = root / 'results'; results.mkdir()
            outside = root / 'outside'; outside.mkdir()
            source = self.checkpoint(outside, 'model', 'book')
            original = source.read_bytes()
            (results / 'external').symlink_to(outside / 'model', target_is_directory=True)
            with patch.object(sheet, 'load_named', wraps=sheet.load_named) as read:
                with self.assertRaisesRegex(ValueError, 'outside'):
                    sheet.find_model_runs(str(results), 'book')
                read.assert_not_called()
            self.assertEqual(original, source.read_bytes())
