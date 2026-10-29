"""POSIX admission of administrator-owned ancestor aliases (#1659).

A symlinked ancestor is admitted only when no unprivileged user could create or
swap it. Fixtures cannot create root-owned links, so the trusted owners include
the test user and the system directories above each fixture are reported as
root-owned 0755; the fixture's own directories and links are real.
"""
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import direct_path_admission as admission


@unittest.skipIf(os.name == 'nt', 'POSIX alias policy; Windows uses reparse attributes')
class AdministratorAliasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = Path(os.path.realpath(self.tmp.name))
        self.base = self.fixture / 'base'
        self.target = self.fixture / 'target'
        self.base.mkdir(mode=0o755)
        self.target.mkdir(mode=0o755)
        (self.target / 'book').mkdir()
        os.chmod(self.fixture, 0o755)
        self.alias = self.base / 'alias'
        self.alias.symlink_to(self.target, target_is_directory=True)

    def tearDown(self):
        for directory in (self.fixture, self.base, self.target):
            os.chmod(directory, 0o755)
        self.tmp.cleanup()

    def trusted(self, owners=None):
        """Trust the test user; report system directories above the fixture as root 0755."""
        real = admission._stat_directory
        def stat_directory(path):
            if path == self.fixture or self.fixture in path.parents:
                return real(path)
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        owners = frozenset({0, os.getuid()}) if owners is None else owners
        return (patch.object(admission, 'TRUSTED_ALIAS_OWNERS', owners),
                patch.object(admission, '_stat_directory', side_effect=stat_directory))

    def check(self, path, owners=None):
        first, second = self.trusted(owners)
        with first, second:
            return admission.is_plain_path(path)

    def test_administrator_controlled_ancestor_alias_is_admitted(self):
        self.assertTrue(self.check(self.alias / 'book'))
        self.assertTrue(self.check(self.alias / 'book' / 'missing' / 'file.json'))
        first, second = self.trusted()
        with first, second:
            self.assertEqual(self.target / 'book', admission.get_canonical_plain_path(self.alias / 'book'))

    def test_final_component_alias_is_refused(self):
        self.assertFalse(self.check(self.alias))

    def test_alias_owned_by_an_untrusted_user_is_refused(self):
        self.assertFalse(self.check(self.alias / 'book', owners=frozenset({0})))
        first, second = self.trusted(frozenset({0}))
        with first, second, self.assertRaises(ValueError):
            admission.get_canonical_plain_path(self.alias / 'book')

    def test_writable_or_sticky_directory_above_the_alias_is_refused(self):
        for directory in (self.base, self.fixture):
            for mode in (0o775, 0o757, 0o1777):
                with self.subTest(directory=directory.name, mode=oct(mode)):
                    os.chmod(directory, mode)
                    try:
                        self.assertFalse(self.check(self.alias / 'book'))
                    finally:
                        os.chmod(directory, 0o755)

    def test_untrusted_alias_below_an_admitted_one_is_refused(self):
        # The inner link sits in a world-writable directory reached through the alias.
        os.chmod(self.target, 0o777)
        (self.target / 'inner').symlink_to(self.target / 'book', target_is_directory=True)
        self.assertFalse(self.check(self.alias / 'inner' / 'x'))

    def test_paths_without_aliases_keep_the_existing_rule(self):
        self.assertTrue(self.check(self.target / 'book'))
        self.assertTrue(self.check(self.target / 'book' / 'missing'))

    def test_book_root_under_an_administrator_alias_is_admitted(self):
        import book_state_transaction as books
        first, second = self.trusted()
        with first, second:
            root, identity = books._direct_book_root(self.alias / 'book')
        info = (self.target / 'book').stat()
        self.assertEqual((info.st_dev, info.st_ino), identity)
        first, second = self.trusted(frozenset({0}))
        with first, second, self.assertRaisesRegex(ValueError, 'aliased ancestors'):
            books._direct_book_root(self.alias / 'book')

    def test_real_host_system_alias_is_admitted_without_substitution(self):
        # macOS: /var, /tmp and /etc link into /private; Ubuntu: usr-merge /lib, /bin.
        candidates = [Path(name) for name in ('/var', '/tmp', '/etc', '/lib', '/bin', '/sbin')
                      if os.path.islink(name) and os.lstat(name).st_uid == 0]
        if not candidates:
            self.skipTest('host has no root-owned top-level alias')
        for alias in candidates:
            with self.subTest(alias=str(alias)):
                self.assertTrue(admission.is_plain_path(alias / 'aa2-1659-missing-child'))
                self.assertFalse(admission.is_plain_path(alias))


if __name__ == '__main__':
    unittest.main()
