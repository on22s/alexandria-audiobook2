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
    def test_prepared_source_normalizes_line_endings_before_publisher_cleanup(self):
        from generate_script import get_preprocessed_source
        paragraphs = ['Copyright © 2026 Synthetic Press'] + [
            f'The traveler watched the distant hills while the wind moved softly through the trees. Synthetic paragraph {i}.' for i in range(20)]
        baseline = None
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.txt'
            for separator in ('\n\n', '\r\n\r\n', '\r\r'):
                with self.subTest(separator=repr(separator)):
                    raw = separator.join(paragraphs).encode('utf-8'); path.write_bytes(raw)
                    single, _ = get_preprocessed_source(completion.get_generation_input(path)[0])
                    triple, _ = three_pass.get_prepared_source(str(path))
                    self.assertEqual(single, triple)
                    self.assertNotIn('Copyright', triple)
                    self.assertNotIn('\r', triple)
                    self.assertIn(paragraphs[-1], triple)
                    baseline = triple if baseline is None else baseline
                    self.assertEqual(baseline, triple)
                    self.assertEqual(raw, path.read_bytes())
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), completion.get_generation_input(path)[1])

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
                    # Real bytes through the confined capture seam: both
                    # readers decode the same file with one policy and the
                    # raw-byte digest is preserved.
                    single, digest = completion.get_generation_input(path)
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

    def test_prepared_source_without_expected_sha_refuses_alias_and_hardlink(self):
        import os
        import subprocess
        text = 'Alice walked home. "Hello," she said. The road was quiet and long.'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Valid control: no expected sha, ordinary CLI source outside
            # any book directory prepares normally, file unchanged.
            parent = root / 'parent'
            parent.mkdir()
            path = parent / 'book.txt'
            path.write_bytes(text.encode('utf-8'))
            prepared, _report = three_pass.get_prepared_source(str(path))
            self.assertIn('Alice walked home.', prepared)
            self.assertEqual(text.encode('utf-8'), path.read_bytes())
            # Hardlinked source (nlink != 1): refused without a digest,
            # exactly as the bound generation branch refuses it.
            alias = parent / 'alias.txt'
            os.link(path, alias)
            with self.assertRaises(ValueError):
                three_pass.get_prepared_source(str(alias))
            alias.unlink()
            # Aliased parent: refused on both hosts without a digest —
            # native directory junction on Windows, directory symlink on
            # POSIX (SourceParentSubstitutionTests pattern). Outside
            # sentinel bytes must never be returned.
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'book.txt').write_text('OUTSIDE SENTINEL. Must not be read.', encoding='utf-8')
            saved = root / 'parent-original'
            os.rename(parent, saved)
            aliased = root / 'parent'
            try:
                if os.name == 'nt':
                    subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J',
                                    str(aliased), str(outside)],
                                   capture_output=True, check=True)
                else:
                    os.symlink(outside, aliased, target_is_directory=True)
                self.assertTrue(os.path.lexists(aliased))
                with self.assertRaises(ValueError):
                    three_pass.get_prepared_source(str(aliased / 'book.txt'))
            finally:
                if os.path.islink(aliased):
                    os.unlink(aliased)
                elif os.path.lexists(aliased):
                    os.rmdir(aliased)
                if saved.exists():
                    os.rename(saved, parent)
            self.assertEqual(text.encode('utf-8'), path.read_bytes())
            # Expected-digest branch unchanged: match prepares, mismatch refuses.
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            prepared, _ = three_pass.get_prepared_source(str(path), expected_source_sha256=digest)
            self.assertIn('Alice walked home.', prepared)
            with self.assertRaisesRegex(ValueError, 'changed before it was read'):
                three_pass.get_prepared_source(str(path), expected_source_sha256='0' * 64)
