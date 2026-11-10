"""#1660 redesign step 2: _move's anchored setup is shared as _anchored_mutation,
and _exchange swaps two proven names, proves both, and undoes once."""
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_state_transaction as books
import source_upload_publication as uploads


def holds(path):
    """A journal-style predicate: the name must hold this exact object."""
    info = os.lstat(path)
    identity = (info.st_dev, info.st_ino)
    def predicate(dir_fd, name):
        try:
            now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return False
        return (now.st_dev, now.st_ino) == identity
    return predicate


@unittest.skipIf(os.name == 'nt', 'exchange is the POSIX protocol; Windows renames by handle')
class AnchoredExchangeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.identity = tuple((lambda i: (i.st_dev, i.st_ino))(self.root.lstat()))
        self.workspace = self.root / '.book-switch-ws'
        self.workspace.mkdir()
        self.target = self.root / 'script.json'
        self.staged = self.workspace / 'new-0'
        self.target.write_bytes(b'original')
        self.staged.write_bytes(b'replacement')
        try:
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                uploads.probe_exchange_support(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            self.skipTest(f'this filesystem cannot exchange: {error}')

    def exchange(self, **overrides):
        arguments = dict(anchor=self.root, anchor_identity=self.identity,
                         expect_source=holds(self.target), expect_destination=holds(self.staged))
        arguments.update(overrides)
        books._exchange(self.target, self.staged, **arguments)

    def test_a_proven_pair_is_swapped_in_one_step(self):
        original, replacement = os.lstat(self.target).st_ino, os.lstat(self.staged).st_ino
        self.exchange()
        self.assertEqual((b'replacement', b'original'), (self.target.read_bytes(), self.staged.read_bytes()))
        self.assertEqual((replacement, original), (os.lstat(self.target).st_ino, os.lstat(self.staged).st_ino))

    def test_a_name_that_no_longer_holds_its_object_is_refused_before_anything_moves(self):
        expect = holds(self.target)
        self.target.unlink()
        self.target.write_bytes(b'foreign')
        with self.assertRaisesRegex(ValueError, 'Book exchange refused'):
            self.exchange(expect_source=expect)
        self.assertEqual((b'foreign', b'replacement'), (self.target.read_bytes(), self.staged.read_bytes()))

    def test_a_substitution_after_the_check_is_swapped_back_and_nothing_is_lost(self):
        real = uploads.exchange_source_upload_names
        calls = []
        def writer_wins(*args, **kwargs):
            if not calls:   # between our proof and the syscall, a writer swaps the target
                os.rename(self.target, self.root / 'aside')
                (self.root / 'script.json').write_bytes(b'foreign')
            calls.append(1)
            return real(*args, **kwargs)
        with patch.object(uploads, 'exchange_source_upload_names', side_effect=writer_wins), \
                self.assertRaises(books.BookExchangeDiverged):
            self.exchange()
        self.assertEqual(2, len(calls))   # the swap and its single undo
        # Every object survives: the writer's file is back at its name, the
        # replacement back in the workspace, the original where the writer put it.
        self.assertEqual((b'foreign', b'replacement', b'original'),
                         (self.target.read_bytes(), self.staged.read_bytes(),
                          (self.root / 'aside').read_bytes()))

    def test_an_undo_that_cannot_be_proven_parks_without_touching_more(self):
        real = uploads.exchange_source_upload_names
        calls = []
        def fail_the_undo(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                os.rename(self.target, self.root / 'aside')
                (self.root / 'script.json').write_bytes(b'foreign')
                return real(*args, **kwargs)
            raise OSError(errno.EIO, 'undo failed')
        with patch.object(uploads, 'exchange_source_upload_names', side_effect=fail_the_undo), \
                self.assertRaises(books.BookExchangeParked):
            self.exchange()
        self.assertEqual(2, len(calls))
        # The swapped state is left for recovery: nothing overwritten or deleted.
        self.assertEqual((b'replacement', b'foreign', b'original'),
                         (self.target.read_bytes(), self.staged.read_bytes(),
                          (self.root / 'aside').read_bytes()))

    def test_a_name_changed_before_the_undo_parks_instead_of_swapping_again(self):
        expect, calls = holds(self.target), []
        def changes_on_the_proof(dir_fd, name):
            calls.append(name)
            if len(calls) == 2:   # the post-swap proof: a writer moves new-0 on
                self.staged.rename(self.workspace / 'moved-again')
                self.staged.write_bytes(b'third')
                return False
            return expect(dir_fd, name)
        with self.assertRaises(books.BookExchangeParked):
            self.exchange(expect_source=changes_on_the_proof)
        # The undo saw new-0 change and did not swap again.
        self.assertEqual((b'replacement', b'third', b'original'),
                         (self.target.read_bytes(), self.staged.read_bytes(),
                          (self.workspace / 'moved-again').read_bytes()))

    def test_unsupported_exchange_moves_nothing(self):
        with patch.object(uploads, 'exchange_source_upload_names',
                          side_effect=OSError(errno.ENOTSUP, 'no exchange')), \
                self.assertRaises(OSError):
            self.exchange()
        self.assertEqual((b'original', b'replacement'), (self.target.read_bytes(), self.staged.read_bytes()))

    def test_a_root_outside_the_anchor_or_swapped_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'Book move root changed'):
            self.exchange(anchor_identity=(self.identity[0], self.identity[1] + 1))
        self.assertEqual(b'original', self.target.read_bytes())

    def test_a_parent_change_after_a_proven_swap_is_reported_but_not_undone(self):
        named = []
        real = books._anchored_mutation
        def recording(*args, **kwargs):
            context = real(*args, **kwargs)
            class Wrapped:
                def __enter__(self_inner):
                    pair = context.__enter__()
                    def require_named_parents():
                        named.append(1)
                        if len(named) == 2:
                            raise ValueError('Book move parent changed; recovery evidence was preserved')
                        return pair.require_named_parents()
                    return books._AnchoredPair(pair.source_fd, pair.destination_fd, pair.source_name,
                                               pair.destination_name, pair.sync_parents,
                                               require_named_parents, pair.require_expected_parents)
                def __exit__(self_inner, *exc):
                    return context.__exit__(*exc)
            return Wrapped()
        with patch.object(books, '_anchored_mutation', side_effect=recording), \
                self.assertRaisesRegex(ValueError, 'parent changed'):
            self.exchange()
        self.assertEqual((b'replacement', b'original'), (self.target.read_bytes(), self.staged.read_bytes()))


class WindowsExchangeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows only')
    def test_windows_never_exchanges(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'a').write_bytes(b'a')
            (root / 'b').write_bytes(b'b')
            with self.assertRaises(OSError) as raised:
                books._exchange(root / 'a', root / 'b', anchor=root,
                                expect_source=lambda *a: True, expect_destination=lambda *a: True)
            self.assertEqual(errno.ENOTSUP, raised.exception.errno)
            self.assertEqual((b'a', b'b'), ((root / 'a').read_bytes(), (root / 'b').read_bytes()))


if __name__ == '__main__':
    unittest.main()
