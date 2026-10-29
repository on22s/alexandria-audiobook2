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


_real_lstat = Path.lstat


def _root_owned(path, link):
    """lstat, reporting `link` as root-owned as only an administrator could make it."""
    info = _real_lstat(path)
    if path != link:
        return info
    return SimpleNamespace(st_mode=info.st_mode, st_uid=0)


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

    def test_alias_target_is_checked_as_if_spelled_directly(self):
        # A user-swappable link behind the alias is not on the lexical path.
        os.chmod(self.target, 0o777)
        (self.target / 'hop').symlink_to(self.target / 'book', target_is_directory=True)
        (self.base / 'to-hop').symlink_to(self.target / 'hop', target_is_directory=True)
        (self.target / 'book' / 'sub').mkdir()
        (self.base / 'through-hop').symlink_to(self.target / 'hop' / 'sub', target_is_directory=True)
        self.assertFalse(self.check(self.base / 'to-hop' / 'x'))
        self.assertFalse(self.check(self.base / 'through-hop' / 'x'))

    def test_dot_dot_resolves_from_the_physical_directory_behind_an_alias(self):
        # deep -> nest/inner, so deep/.. is nest physically, not base.
        nest = self.target / 'nest'
        (nest / 'inner').mkdir(parents=True)
        os.chmod(nest, 0o777)
        (nest / 'evil').symlink_to(self.target / 'book', target_is_directory=True)
        (nest / 'inner' / 'link').symlink_to('../evil', target_is_directory=True)
        (self.base / 'deep').symlink_to(nest / 'inner', target_is_directory=True)
        self.assertFalse(self.check(self.base / 'deep' / 'link' / 'file'))
        (self.target / 'up').mkdir()
        self.assertFalse(self.check(self.alias / 'up' / '..' / 'book'))
        self.assertTrue(self.check(self.target / 'up' / '..' / 'book'))

    def test_administrator_alias_chain_is_followed(self):
        (self.base / 'chain').symlink_to(self.alias, target_is_directory=True)
        self.assertTrue(self.check(self.base / 'chain' / 'book'))
        self.assertFalse(self.check(self.base / 'chain'))

    def test_non_directory_component_or_loop_under_an_alias_is_refused(self):
        (self.target / 'file').write_text('not a directory')
        (self.target / 'loop').symlink_to('loop')
        self.assertFalse(self.check(self.alias / 'file' / 'child'))
        self.assertFalse(self.check(self.alias / 'loop' / 'child'))
        (self.base / 'self').symlink_to(self.base / 'self' / 'x')
        self.assertFalse(self.check(self.base / 'self' / 'y'))

    def test_alias_in_a_parent_owned_by_an_untrusted_user_is_refused(self):
        # The alias and every other directory are root's; only `base` varies.
        for owner, admitted in ((os.getuid(), False), (0, True)):
            def stat_directory(path):
                uid = owner if path == self.base else 0
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=uid)
            with self.subTest(base_owner=owner), \
                 patch.object(admission, 'TRUSTED_ALIAS_OWNERS', frozenset({0})), \
                 patch.object(admission, '_stat_directory', side_effect=stat_directory), \
                 patch.object(Path, 'lstat', autospec=True,
                              side_effect=lambda p: _root_owned(p, self.alias)):
                self.assertEqual(admitted, admission.is_plain_path(self.alias / 'book'))

    def test_admitted_alias_and_resolved_spellings_share_book_and_take_scope(self):
        import audio_take_history
        import book_state_transaction as books
        first, second = self.trusted()
        with first, second:
            with books.ensure_book_state(self.alias / 'book', recover=False):
                self.assertTrue(books.is_book_state_admitted(self.alias / 'book'))
                self.assertTrue(books.is_book_state_admitted(self.target / 'book'))
            self.assertEqual(self.target / 'book' / 'take.wav', audio_take_history._ensure_plain_path(
                self.alias / 'book' / 'take.wav', self.target / 'book'))
            with self.assertRaisesRegex(ValueError, 'inside the project'):
                audio_take_history._ensure_plain_path(self.target / 'take.wav', self.alias / 'book')
        # An unadmitted root cannot lend its resolved form to containment.
        (self.fixture / 'user-root').symlink_to(self.target / 'book', target_is_directory=True)
        with first, second, self.assertRaisesRegex(ValueError, 'aliases'):
            audio_take_history._ensure_plain_path(self.target / 'book' / 'take.wav', self.fixture / 'user-root')

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
