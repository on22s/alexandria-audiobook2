"""#1660 (a, c): the publish loop rechecks a row's own parent chain around its
move, all chains once before commit, and honors cancellation while doing so."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books
from audio_generation_owner import AudioGenerationCancelled


@unittest.skipUnless(os.name != 'nt' and os.open in os.supports_dir_fd,
                     'POSIX directory descriptors required')
class PublishLoopParentRecheckTests(unittest.TestCase):
    def book(self, root, count):
        replacements = {}
        for index in range(count):
            folder = root / f'part{index}'
            folder.mkdir()
            (folder / 'state.json').write_bytes(b'old')
            replacements[f'part{index}/state.json'] = b'new'
        return replacements

    def parent_walks(self, count):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replacements = self.book(root, count)
            walk, calls = books._require_target_parent_identities, [0]
            def counting(*args, **kwargs):
                calls[0] += 1
                return walk(*args, **kwargs)
            with patch.object(books, '_require_target_parent_identities', side_effect=counting):
                with books.ensure_book_state(root):
                    books.apply_book_state_locked(root, replacements, [])
            self.assertEqual(b'new', (root / 'part0/state.json').read_bytes())
            return calls[0]

    def test_parent_rechecks_grow_linearly_with_the_target_count(self):
        # Each row: its own chain before and after its move; then every chain
        # once before commit. Rechecking all chains per row is quadratic.
        self.assertEqual(3 * 4, self.parent_walks(8) - self.parent_walks(4))

    def test_no_parent_walk_starts_after_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replacements = self.book(root, 4)
            move, walk = books._move, books._require_target_parent_identities
            cancelled, after_cancel = [False], [0]
            def move_then_cancel(*args, **kwargs):
                result = move(*args, **kwargs)
                cancelled[0] = True   # cancel once the first target has moved
                return result
            def counting(*args, **kwargs):
                if cancelled[0]:
                    after_cancel[0] += 1
                return walk(*args, **kwargs)
            with patch.object(books, '_move', side_effect=move_then_cancel), \
                    patch.object(books, '_require_target_parent_identities', side_effect=counting), \
                    self.assertRaises(AudioGenerationCancelled):
                with books.ensure_book_state(root):
                    books.apply_book_state_locked(root, replacements, [],
                                                  cancel_check=lambda: cancelled[0])
            self.assertEqual(0, after_cancel[0])
            for index in range(4):   # rolled back
                self.assertEqual(b'old', (root / f'part{index}/state.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
