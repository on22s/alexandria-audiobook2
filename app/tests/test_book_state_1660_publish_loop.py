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
        # Every chain once before the first move; each row its own chain
        # before and after its move; every chain once before commit: 4N.
        # Rechecking all chains per row is quadratic.
        self.assertEqual(4 * 4, self.parent_walks(8) - self.parent_walks(4))

    def test_no_parent_walk_starts_after_cancellation_before_the_first_move(self):
        # Cancel during the last pre-publication staged check. No target has
        # moved, so the failure takes the clean pre-publication path, which
        # walks no parent chain: every walk counted here is the publish loop
        # ignoring the cancel (the old loop walked all chains at row 0).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replacements = self.book(root, 4)
            staged, walk = books._validate_staged_replacement, books._require_target_parent_identities
            cancelled, checked, after_cancel = [False], [0], [0]
            def check_then_cancel(*args, **kwargs):
                result = staged(*args, **kwargs)
                checked[0] += 1
                if checked[0] == len(replacements):   # the last pre-loop check
                    cancelled[0] = True
                return result
            def counting(*args, **kwargs):
                if cancelled[0]:
                    after_cancel[0] += 1
                return walk(*args, **kwargs)
            with patch.object(books, '_validate_staged_replacement', side_effect=check_then_cancel), \
                    patch.object(books, '_require_target_parent_identities', side_effect=counting), \
                    self.assertRaises(AudioGenerationCancelled):
                with books.ensure_book_state(root):
                    books.apply_book_state_locked(root, replacements, [],
                                                  cancel_check=lambda: cancelled[0])
            self.assertEqual(0, after_cancel[0])
            self.assertFalse((root / books.JOURNAL).exists())   # clean abort
            for index in range(4):   # nothing moved
                self.assertEqual(b'old', (root / f'part{index}/state.json').read_bytes())

    def test_earlier_row_parent_swapped_later_is_caught_before_commit(self):
        # Rows check only their own chain, so a swap of an earlier row's parent
        # after its own checks is caught by the final all-chains pass: the
        # journal never becomes "committed" and recovery refuses.
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replacements = self.book(root, 4)
            move, save, moves, phases = books._move, books._save_journal, [0], []
            def move_then_swap(*args, **kwargs):
                result = move(*args, **kwargs)
                moves[0] += 1
                if moves[0] == 4:   # row 1 published; row 0's checks are done
                    os.rename(root / 'part0', root / 'part0-away')
                    (root / 'part0').mkdir()
                return result
            def recording(directory, document, **kwargs):
                phases.append(document['phase'])
                return save(directory, document, **kwargs)
            with patch.object(books, '_move', side_effect=move_then_swap), \
                    patch.object(books, '_save_journal', side_effect=recording), \
                    self.assertRaisesRegex(ValueError, 'parent changed'):
                with books.ensure_book_state(root):
                    books.apply_book_state_locked(root, replacements, [])
            self.assertNotIn('committed', phases)
            journal = json.loads((root / books.JOURNAL).read_text(encoding='utf-8'))
            self.assertEqual('publishing', journal['phase'])


if __name__ == '__main__':
    unittest.main()
