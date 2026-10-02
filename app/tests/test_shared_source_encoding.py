"""Native source snapshots preserve text and original-byte publication identity."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import generation_completion as completion
from source_encoding import get_decoded_source_text, read_source_text
import three_pass_generate as three_pass


class SharedSourceEncodingTests(unittest.TestCase):
    def test_both_paths_decode_native_files_with_one_policy_and_raw_hash(self):
        cases = [('Alice’s café — “quiet.”', 'utf-8'),
                 ('Alice’s café — “quiet.”', 'cp1252'),
                 ('日本語 and café', 'utf-8'), ('Literal \ufffd damage.', 'utf-8')]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'book.txt'
            for text, encoding in cases:
                with self.subTest(encoding=encoding, text=text):
                    raw = text.encode(encoding)
                    path.write_bytes(raw)
                    with patch.object(Path, 'read_bytes', autospec=True, return_value=raw) as read:
                        single, digest = completion.get_generation_input(path)
                        self.assertEqual(1, read.call_count)
                    triple, detected = three_pass.read_source_text(path)
                    self.assertIs(three_pass.read_source_text, read_source_text)
                    self.assertEqual(text, single)
                    self.assertEqual(text, triple)
                    self.assertEqual(encoding, detected)
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                    self.assertEqual(raw, path.read_bytes())

    def test_cp1252_snapshot_keeps_existing_single_pass_newline_normalization(self):
        raw = 'Café’s\r\nsecond\rthird\n'.encode('cp1252')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'book.txt'
            path.write_bytes(raw)
            single, digest = completion.get_generation_input(path)
            self.assertEqual('Café’s\nsecond\nthird\n', single)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            self.assertEqual(('Café’s\r\nsecond\rthird\n', 'cp1252'), read_source_text(path))
            self.assertEqual(raw, path.read_bytes())

    def test_undefined_legacy_byte_remains_damage_and_trips_existing_gate(self):
        raw = b'\x81' * 50 + b'bad source'
        text, encoding = get_decoded_source_text(raw)
        self.assertEqual('utf-8/replace', encoding)
        self.assertEqual(50, text.count('\ufffd'))
        with self.assertRaises(ValueError):
            three_pass.prepare_source_text(text)

    def test_missing_file_still_fails_in_both_readers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'missing.txt'
            for reader in (read_source_text, completion.get_generation_input):
                with self.subTest(reader=reader.__name__), self.assertRaises(FileNotFoundError):
                    reader(path)
