"""#1660 (a): committed cleanup rechecks the row that owns each workspace entry,
not every target once per entry, so a target's hashing does not grow with
the number of targets (it was N+7 per target for bytes, N+8 for streams)."""
import os
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

    def test_each_entry_is_preceded_by_a_recheck_of_its_own_row(self):
        # Change row 1's target just before the per-entry recheck of row 1:
        # cleanup must refuse and keep that row's backup. A wrong entry-to-row
        # mapping would recheck another row and delete old-1.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(3):
                (root / f'f{index}.bin').write_bytes(b'old')
            validate, changed = books._validate_byte_cleanup_targets, []
            def change_row_one(rows, targets, phase, cancel_check=None, index=None):
                if index == 1 and not changed:
                    (root / 'f1.bin').write_bytes(b'writer edit')
                    changed.append(True)
                return validate(rows, targets, phase, cancel_check=cancel_check, index=index)
            with patch.object(books, '_validate_byte_cleanup_targets', side_effect=change_row_one), \
                    self.assertRaisesRegex(ValueError, 'changed'):
                with books.ensure_book_state(root):
                    books.apply_book_state_locked(
                        root, {f'f{index}.bin': b'new' for index in range(3)}, [])
            self.assertEqual([True], changed)
            # v4 rotates the slots: after publication new-1 holds row 1's
            # original, so that is the backup cleanup must preserve. Windows
            # (and an unsupported filesystem) keep the v3 old-1 slot.
            slot = 'old-1' if os.name == 'nt' else 'new-1'
            self.assertEqual(1, len(list(root.glob(f'.book-switch-*/{slot}'))))
            self.assertEqual(b'writer edit', (root / 'f1.bin').read_bytes())

    def test_entry_names_and_indexes_are_strict(self):
        for name in ('old-01', 'old--1', 'OLD-0', 'old-1-2', 'x', 'stage-1'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                books._get_workspace_entry_index(name)
        self.assertEqual(12, books._get_workspace_entry_index('discard-12'))
        rows, targets = [{'path': 'a'}], [Path('a')]
        for index in (1, -1, True):
            with self.subTest(index=index), self.assertRaises(ValueError):
                books._validate_byte_cleanup_targets(rows, targets, 'committed', index=index)

    def test_a_targets_hashing_does_not_grow_with_the_target_count(self):
        for kind in ('byte', 'stream'):
            with self.subTest(kind=kind):
                self.assertEqual(self.hashes_of_first_target(kind, 4),
                                 self.hashes_of_first_target(kind, 8))


if __name__ == '__main__':
    unittest.main()
