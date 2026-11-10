"""#1660 (a, audit 5): the directory retirability preflight in
apply_book_state_locked looks like a discarded walk, but it is the one place
that refuses hard links (and symlinks, depth, entry count, Windows reparse
points) in a directory being retired; the content digest accepts them."""
import os
import tempfile
import unittest
from pathlib import Path

import book_state_transaction as books
from adapter_publication import get_adapter_bundle_sha256


class RetirePreflightTests(unittest.TestCase):
    def test_hardlinked_file_in_a_retired_directory_is_refused_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / 'bundle'
            bundle.mkdir()
            (bundle / 'a.bin').write_bytes(b'content')
            os.link(bundle / 'a.bin', bundle / 'b.bin')
            # The digest alone would accept this tree...
            self.assertRegex(get_adapter_bundle_sha256(str(bundle)), r'^[0-9a-f]{64}$')
            # ...so it is the preflight that refuses it, before any journal.
            with self.assertRaisesRegex(ValueError, 'unsafe artifact'):
                with books.ensure_book_state(root):
                    books.apply_book_state_locked(root, {}, ['bundle'])
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertEqual([], list(root.glob('.book-switch-*')))
            self.assertEqual(b'content', (bundle / 'b.bin').read_bytes())


if __name__ == '__main__':
    unittest.main()
