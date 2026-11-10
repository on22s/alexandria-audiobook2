"""#1660: atomic name exchange (renameat2 RENAME_EXCHANGE / renamex_np RENAME_SWAP)
and its capability probe. Nothing in the book transaction calls them yet."""
import ctypes
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import source_upload_publication as publication


class ExchangeMarshallingTests(unittest.TestCase):
    """Cross-platform: the exact native call each platform makes."""

    def call(self, platform, library, *args, **kwargs):
        with patch.object(publication, 'os', SimpleNamespace(
                name='posix', fsencode=os.fsencode, path=os.path)), \
                patch.object(publication.sys, 'platform', platform), \
                patch.object(ctypes, 'CDLL', return_value=library):
            publication.exchange_source_upload_names(*args, **kwargs)

    def test_native_exchange_signatures(self):
        for platform, symbol, kwargs, arguments in (
                ('linux', 'renameat2', {}, (-100, b'a', -100, b'b', 2)),
                ('linux', 'renameat2', {'source_dir_fd': 5, 'destination_dir_fd': 6},
                 (5, b'a', 6, b'b', 2)),
                ('darwin', 'renamex_np', {}, (b'a', b'b', 2)),
                ('darwin', 'renameatx_np', {'source_dir_fd': 5, 'destination_dir_fd': 6},
                 (5, b'a', 6, b'b', 2))):
            with self.subTest(platform=platform, symbol=symbol):
                operation = Mock(return_value=0)
                self.call(platform, SimpleNamespace(**{symbol: operation}), 'a', 'b', **kwargs)
                operation.assert_called_once_with(*arguments)
                self.assertEqual(ctypes.c_int, operation.restype)
                self.assertEqual(self.types(arguments), tuple(operation.argtypes))

    @staticmethod
    def types(arguments):
        return tuple(ctypes.c_char_p if isinstance(value, bytes)
                     else ctypes.c_uint if index == len(arguments) - 1 else ctypes.c_int
                     for index, value in enumerate(arguments))

    def test_exclusive_move_keeps_its_flags_through_the_shared_call(self):
        anchored = {'source_dir_fd': 5, 'destination_dir_fd': 6}
        for platform, symbol, kwargs, arguments in (
                ('linux', 'renameat2', {}, (-100, b'a', -100, b'b', 1)),
                ('linux', 'renameat2', anchored, (5, b'a', 6, b'b', 1)),
                ('darwin', 'renamex_np', {}, (b'a', b'b', 4)),
                ('darwin', 'renameatx_np', anchored, (5, b'a', 6, b'b', 4))):
            with self.subTest(platform=platform, symbol=symbol):
                operation = Mock(return_value=0)
                with patch.object(publication, 'os', SimpleNamespace(
                        name='posix', fsencode=os.fsencode, path=os.path)), \
                        patch.object(publication.sys, 'platform', platform), \
                        patch.object(ctypes, 'CDLL', return_value=SimpleNamespace(**{symbol: operation})):
                    publication.move_source_upload_exclusively('a', 'b', **kwargs)
                operation.assert_called_once_with(*arguments)
                self.assertEqual(self.types(arguments), tuple(operation.argtypes))

    def test_native_failure_carries_errno(self):
        operation = Mock(return_value=-1)
        with patch.object(ctypes, 'get_errno', return_value=errno.EINVAL), \
                self.assertRaises(OSError) as caught:
            self.call('linux', SimpleNamespace(renameat2=operation), 'a', 'b')
        self.assertEqual(errno.EINVAL, caught.exception.errno)

    def test_no_exchange_where_unavailable_and_never_a_plain_rename(self):
        for platform, library in (('linux', SimpleNamespace()), ('freebsd', SimpleNamespace())):
            with self.subTest(platform=platform), self.assertRaises(OSError) as caught:
                self.call(platform, library, 'a', 'b')
            self.assertEqual(errno.ENOTSUP, caught.exception.errno)
        with patch.object(publication, 'os', SimpleNamespace(name='nt', path=os.path)), \
                patch('os.rename') as rename, self.assertRaises(OSError) as caught:
            publication.exchange_source_upload_names('a', 'b')
        self.assertEqual(errno.ENOTSUP, caught.exception.errno)
        rename.assert_not_called()

    def test_anchored_names_are_validated(self):
        for args, kwargs in ((('a/b', 'c'), {'source_dir_fd': 3, 'destination_dir_fd': 3}),
                             (('a', '..'), {'source_dir_fd': 3, 'destination_dir_fd': 3}),
                             (('a', 'b'), {'source_dir_fd': -1, 'destination_dir_fd': 3})):
            with self.subTest(args=args), self.assertRaises(ValueError):
                publication.exchange_source_upload_names(*args, **kwargs)


@unittest.skipUnless(sys.platform in ('linux', 'darwin') and os.open in os.supports_dir_fd,
                     'native name exchange')
class NativeExchangeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)

    def test_files_and_directories_swap_and_nothing_is_overwritten(self):
        (self.root / 'a').write_bytes(b'first')
        (self.root / 'b').write_bytes(b'second')
        (self.root / 'd1').mkdir(); (self.root / 'd1' / 'x').write_text('one')
        (self.root / 'd2').mkdir()
        publication.exchange_source_upload_names('a', 'b', source_dir_fd=self.fd, destination_dir_fd=self.fd)
        publication.exchange_source_upload_names(str(self.root / 'd1'), str(self.root / 'd2'))
        self.assertEqual((b'second', b'first'), ((self.root / 'a').read_bytes(), (self.root / 'b').read_bytes()))
        self.assertEqual('one', (self.root / 'd2' / 'x').read_text())
        self.assertEqual([], list((self.root / 'd1').iterdir()))

    def test_missing_side_is_refused_without_moving_anything(self):
        (self.root / 'a').write_bytes(b'only')
        with self.assertRaises(FileNotFoundError):
            publication.exchange_source_upload_names('a', 'b', source_dir_fd=self.fd, destination_dir_fd=self.fd)
        self.assertEqual(b'only', (self.root / 'a').read_bytes())
        self.assertFalse((self.root / 'b').exists())

    def test_probe_reports_support_and_leaves_nothing_behind(self):
        self.assertTrue(publication.probe_exchange_support(self.fd))
        self.assertEqual([], list(self.root.iterdir()))

    def test_probe_reports_an_unsupported_filesystem_and_cleans_up(self):
        for code in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EXDEV, errno.EPERM, errno.EACCES):
            with self.subTest(errno=errno.errorcode[code]), patch.object(
                    publication, 'exchange_source_upload_names',
                    side_effect=OSError(code, 'unsupported')):
                self.assertFalse(publication.probe_exchange_support(self.fd))
            self.assertEqual([], list(self.root.iterdir()))

    def test_probe_rejects_an_exchange_that_reports_success_without_swapping(self):
        # The security-critical check: success is proven by identity and bytes,
        # never taken from the call's return value.
        with patch.object(publication, 'exchange_source_upload_names', return_value=None):
            self.assertFalse(publication.probe_exchange_support(self.fd))
        self.assertEqual([], list(self.root.iterdir()))

    def test_probe_half_success_is_unsupported_and_cleaned_up(self):
        exchange, calls = publication.exchange_source_upload_names, [0]
        def files_then_reject(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return exchange(*args, **kwargs)   # the files really swap
            raise OSError(errno.EINVAL, 'directories unsupported')
        with patch.object(publication, 'exchange_source_upload_names', side_effect=files_then_reject):
            self.assertFalse(publication.probe_exchange_support(self.fd))
        self.assertEqual(2, calls[0])
        self.assertEqual([], list(self.root.iterdir()))

    def test_probe_never_reports_tampering_as_unsupported(self):
        # Its own names vanishing is not a capability verdict a writer may forge.
        for code in (errno.ENOENT, errno.ENOTDIR, errno.EISDIR):
            with self.subTest(errno=errno.errorcode[code]), patch.object(
                    publication, 'exchange_source_upload_names', side_effect=OSError(code, 'tampered')), \
                    self.assertRaises(OSError) as caught:
                publication.probe_exchange_support(self.fd)
            self.assertEqual(code, caught.exception.errno)
            self.assertEqual([], list(self.root.iterdir()))

    def test_probe_propagates_resource_errors(self):
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=OSError(errno.EMFILE, 'too many files')), \
                self.assertRaises(OSError) as caught:
            publication.probe_exchange_support(self.fd)
        self.assertEqual(errno.EMFILE, caught.exception.errno)
        self.assertEqual([], list(self.root.iterdir()))


if __name__ == '__main__':
    unittest.main()
