"""Cross-platform policy for the shared synthesis storage capability source."""
from contextlib import contextmanager
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import synthesis_recovery_cache as cache
import synthesis_storage_capability as capability
import synthesis_workspace as workspace


_PRIMITIVES = ('open', 'stat', 'unlink', 'rename', 'mkdir', 'rmdir')


class _Primitive:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('%s must not run while checking capability' % self.name)


class _Surface:
    def __init__(self, *, directory=True, nofollow=True,
                 supported=_PRIMITIVES, present=_PRIMITIVES):
        self.primitives = {}
        self.supports_dir_fd = set()
        for name in _PRIMITIVES:
            primitive = _Primitive(name)
            self.primitives[name] = primitive
            if name in present:
                setattr(self, name, primitive)
            if name in supported and name in present:
                self.supports_dir_fd.add(primitive)
        if directory:
            self.O_DIRECTORY = object()
        if nofollow:
            self.O_NOFOLLOW = object()


@contextmanager
def _platform(platform, surface):
    with patch.object(capability, 'sys', SimpleNamespace(platform=platform)), \
            patch.object(capability, 'os', surface):
        yield


class SynthesisStorageCapabilityTests(unittest.TestCase):
    def test_linux_and_darwin_with_every_primitive_are_capable(self):
        for platform in ('linux', 'darwin'):
            with self.subTest(platform=platform):
                with _platform(platform, _Surface()):
                    self.assertTrue(capability.is_synthesis_storage_capable())

    def test_windows_and_unknown_platforms_are_never_capable(self):
        for platform in ('win32', 'cygwin', 'aix', 'freebsd13', 'linux2', ''):
            with self.subTest(platform=platform):
                with _platform(platform, _Surface()):
                    self.assertFalse(capability.is_synthesis_storage_capable())

    def test_each_missing_directory_flag_is_incapable(self):
        for platform in ('linux', 'darwin'):
            for absent, surface in (('O_DIRECTORY', _Surface(directory=False)),
                                    ('O_NOFOLLOW', _Surface(nofollow=False))):
                with self.subTest(platform=platform, absent=absent):
                    with _platform(platform, surface):
                        self.assertFalse(capability.is_synthesis_storage_capable())

    def test_each_primitive_outside_dir_fd_support_is_incapable(self):
        for platform in ('linux', 'darwin'):
            for name in _PRIMITIVES:
                supported = tuple(item for item in _PRIMITIVES if item != name)
                with self.subTest(platform=platform, primitive=name):
                    with _platform(platform, _Surface(supported=supported)):
                        self.assertFalse(capability.is_synthesis_storage_capable())

    def test_absent_primitive_attribute_is_incapable(self):
        present = tuple(item for item in _PRIMITIVES if item != 'mkdir')
        with _platform('linux', _Surface(present=present)):
            self.assertFalse(capability.is_synthesis_storage_capable())

    def test_predicate_invokes_no_filesystem_primitive(self):
        surface = _Surface()
        with _platform('darwin', surface):
            self.assertTrue(capability.is_synthesis_storage_capable())
        self.assertEqual({name: 0 for name in _PRIMITIVES},
                         {name: primitive.calls
                          for name, primitive in surface.primitives.items()})

    def test_public_wrappers_report_the_shared_capability(self):
        cases = (('linux', _Surface(), True),
                 ('darwin', _Surface(), True),
                 ('win32', _Surface(), False),
                 ('darwin', _Surface(supported=('open',)), False),
                 ('linux', _Surface(nofollow=False), False))
        for platform, surface, expected in cases:
            with self.subTest(platform=platform, expected=expected):
                with _platform(platform, surface):
                    self.assertIs(expected, capability.is_synthesis_storage_capable())
                    self.assertIs(expected, cache.is_recovery_storage_platform_supported())
                    self.assertIs(expected, workspace.is_synthesis_workspace_supported())

    def test_public_wrappers_delegate_to_the_capability_source(self):
        for expected in (True, False):
            with self.subTest(expected=expected), \
                    patch.object(capability, 'is_synthesis_storage_capable',
                                 return_value=expected) as source:
                self.assertIs(expected, cache.is_recovery_storage_platform_supported())
                self.assertIs(expected, workspace.is_synthesis_workspace_supported())
                self.assertEqual(2, source.call_count)

    def test_real_interpreter_agrees_with_both_wrappers(self):
        shared = capability.is_synthesis_storage_capable()
        self.assertIs(shared, cache.is_recovery_storage_platform_supported())
        self.assertIs(shared, workspace.is_synthesis_workspace_supported())
        if sys.platform == 'win32':
            self.assertFalse(shared)


if __name__ == '__main__':
    unittest.main()
