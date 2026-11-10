"""#1660 (a): committed cleanup rechecks the row that owns each workspace entry,
not every target once per entry, so a target's hashing does not grow with
the number of targets (it was N+7 per target for bytes, N+8 for streams)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books
from tests.test_book_state_streaming import stream


class CleanupRecheckCostTests(unittest.TestCase):
    def hashes_of_first_target(self, kind, count):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(count):
                (root / f'f{index}.bin').write_bytes(b'old' * 1024)
            name = '_get_digest' if kind == 'byte' else '_bounded_book_file_digest'
            digest, calls = getattr(books, name), [0]
            def counting(path, *args, **kwargs):
                if Path(path).name == 'f0.bin':
                    calls[0] += 1
                return digest(path, *args, **kwargs)
            with patch.object(books, name, side_effect=counting):
                with books.ensure_book_state(root):
                    if kind == 'byte':
                        books.apply_book_state_locked(
                            root, {f'f{index}.bin': b'new' * 1024 for index in range(count)}, [])
                    else:
                        books.apply_book_state_locked(
                            root, {f'f{index}.bin': stream(b'new' * 1024) for index in range(count)}, [],
                            stream_byte_limit=count * 4096)
            self.assertEqual(b'new' * 1024, (root / 'f0.bin').read_bytes())
            self.assertEqual([], list(root.glob('.book-switch-*')))
            return calls[0]

    def test_a_targets_hashing_does_not_grow_with_the_target_count(self):
        for kind in ('byte', 'stream'):
            with self.subTest(kind=kind):
                self.assertEqual(self.hashes_of_first_target(kind, 4),
                                 self.hashes_of_first_target(kind, 8))


if __name__ == '__main__':
    unittest.main()
