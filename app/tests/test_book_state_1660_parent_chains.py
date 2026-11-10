"""#1660: parent chains captured by path (the Windows scheme) and the journal's
scheme marker. The Windows integration checks run in the native runner."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import book_state_transaction as books


class PathParentChainTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def identity(self, path):
        info = os.lstat(path)
        return [info.st_dev, info.st_ino]

    def test_chain_records_each_parent_and_a_missing_suffix(self):
        (self.root / 'sub' / 'deeper').mkdir(parents=True)
        capture = books._capture_target_parent_identities_path
        self.assertEqual([self.identity(self.root)], capture(self.root, 'state.json'))
        self.assertEqual([self.identity(self.root), self.identity(self.root / 'sub'),
                          self.identity(self.root / 'sub' / 'deeper')],
                         capture(self.root, 'sub/deeper/file.json'))
        self.assertEqual([self.identity(self.root), self.identity(self.root / 'sub'), None, None],
                         capture(self.root, 'sub/absent/again/file.json'))

    def test_a_parent_that_is_not_a_plain_directory_is_refused(self):
        (self.root / 'sub').write_bytes(b'a file where a folder belongs')
        with self.assertRaisesRegex(ValueError, 'not a plain directory'):
            books._capture_target_parent_identities_path(self.root, 'sub/file.json')
        for bad in ('/abs/file.json', '../escape.json', ''):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                books._capture_target_parent_identities_path(self.root, bad)

    def test_chains_are_rechecked_only_where_they_were_captured(self):
        chains = [[[1, 2]]]
        check = books._validate_parent_identity_scheme
        posix, windows = {'target_parent_identities': chains}, {
            'target_parent_identities': chains, 'target_parent_identity_scheme': books.WINDOWS_PARENT_SCHEME}
        check(posix, platform='posix')
        check(windows, platform='nt')
        check({}, platform='nt')
        check({}, platform='posix')
        for data, platform in ((windows, 'posix'), (posix, 'nt')):
            with self.subTest(platform=platform), self.assertRaisesRegex(
                    ValueError, 'cannot be rechecked on this platform'):
                check(data, platform=platform)
        for data in ({'target_parent_identity_scheme': books.WINDOWS_PARENT_SCHEME},
                     {**posix, 'target_parent_identity_scheme': 'bogus'}):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, 'parent identity evidence'):
                check(data, platform='nt')

    def test_an_unknown_parent_identity_scheme_is_refused(self):
        workspace = self.root / ('.book-switch-' + '0' * 32)
        workspace.mkdir()
        (self.root / 'state.json').write_bytes(b'old')
        journal = {'version': 3, 'phase': 'publishing', 'workspace': workspace.name,
                   'workspace_identity': self.identity(workspace),
                   'entries': [{'path': 'state.json', 'existed': True,
                                'old_sha256': '0' * 64, 'new_sha256': None}],
                   'target_parent_identities': [[self.identity(self.root)]],
                   'target_parent_identity_scheme': 'bogus'}
        (self.root / books.JOURNAL).write_text(json.dumps(journal), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'parent identity evidence'):
            with books.ensure_book_state(self.root):
                pass
        self.assertTrue((self.root / books.JOURNAL).exists())


if __name__ == '__main__':
    unittest.main()
