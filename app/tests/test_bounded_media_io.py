"""Real files distinguish pre-write enforcement from a final size check."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from bounded_media_io import BoundedMediaOutput


class BoundedMediaIOTests(unittest.TestCase):
    def test_exact_cap_overwrite_and_readonly_reopen_use_one_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with BoundedMediaOutput(root, 'output.m4b', 8) as output:
                self.assertFalse(hasattr(output, 'fileno'))
                output.write(b'abcdefgh'); output.seek(-4, os.SEEK_END); output.write(b'WXYZ')
                reader = output.reopen(output.name, 1, {})
                self.assertFalse(reader.writable()); self.assertFalse(hasattr(reader, 'write'))
                self.assertEqual(b'abcdWXYZ', reader.read(8))
                self.assertEqual(8, output.tell())
                self.assertEqual(8, output.highwater)
                output.flush()
            self.assertTrue(reader._file.closed)
            self.assertEqual({'output.m4b'}, {p.name for p in root.iterdir()})
            self.assertEqual(b'abcdWXYZ', (root/'output.m4b').read_bytes())

    def test_large_read_requests_return_bounded_chunks_without_false_eof(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw=b'a'*(1024**2)+b'b'*31
            with BoundedMediaOutput(tmp,'output.m4b',len(raw)) as output:
                output.write(raw)
                reader=output.reopen(output.name,1,{})
                self.assertEqual(raw[:1024**2],reader.read(2**31-1))
                self.assertEqual(raw[1024**2:],reader.read(2**31-1))
                self.assertEqual(b'',reader.read(2**31-1))
                with self.assertRaises(ValueError):reader.read(-1)
                with self.assertRaises(ValueError):output.check()

    def test_write_seek_and_truncate_refuse_before_growing_and_latch(self):
        for operation in ('write', 'seek', 'truncate', 'negative_seek', 'bad_whence'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                with BoundedMediaOutput(tmp, 'output.m4b', 8) as output:
                    output.write(b'1234')
                    action = {'write':lambda: output.write(b'56789'), 'seek':lambda: output.seek(9),
                        'truncate':lambda: output.truncate(9), 'negative_seek':lambda: output.seek(-1),
                        'bad_whence':lambda: output.seek(0, 123)}[operation]
                    with self.assertRaises(ValueError): action()
                    self.assertEqual(b'1234', output.path.read_bytes())
                    with self.assertRaises(ValueError): output.write(b'x')
                    with self.assertRaises(ValueError): output.reopen(output.name, 1, {})

    def test_reopen_rejects_arbitrary_urls_writers_and_options(self):
        for url, flags, options in (('/tmp/other', 1, {}), (BoundedMediaOutput.name, 2, {}),
                (BoundedMediaOutput.name, True, {}), (BoundedMediaOutput.name, 1, {'x':1})):
            with self.subTest(url=url,flags=flags), tempfile.TemporaryDirectory() as tmp:
                with BoundedMediaOutput(tmp, 'output.m4b', 8) as output:
                    with self.assertRaises(ValueError): output.reopen(url, flags, options)
                    self.assertEqual(0, output.path.stat().st_size)

    def test_external_replacement_and_growth_refuse(self):
        for operation in ('replace', 'grow'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                with BoundedMediaOutput(tmp, 'output.m4b', 8) as output:
                    output.write(b'old')
                    if operation == 'replace':
                        replacement = Path(tmp)/'new'; replacement.write_bytes(b'other')
                        # Windows refuses replacement of an open destination.
                        try: os.replace(replacement, output.path)
                        except PermissionError: continue
                    else:
                        with output.path.open('ab') as external: external.write(b'x')
                    before = output.path.read_bytes()
                    with self.assertRaisesRegex(ValueError, 'changed outside'): output.write(b'!')
                    self.assertEqual(before, output.path.read_bytes())

    def test_cancellation_and_short_writes_are_sticky(self):
        class Cancelled(RuntimeError): pass
        with tempfile.TemporaryDirectory() as tmp:
            checks = Mock()
            with BoundedMediaOutput(tmp, 'cancel', 8, checks) as output:
                output.write(b'old'); checks.side_effect = Cancelled('fixture cancel')
                with self.assertRaises(Cancelled): output.write(b'new')
                checks.side_effect = None
                with self.assertRaises(Cancelled): output.write(b'new')
                self.assertEqual(b'old', output.path.read_bytes())
            with BoundedMediaOutput(tmp, 'short', 8) as output:
                original = output._file
                proxy = Mock(wraps=original)
                proxy.write.side_effect = lambda raw: original.write(raw[:1])
                output._file = proxy
                with self.assertRaisesRegex(ValueError, 'Incomplete'): output.write(b'abc')
                with self.assertRaises(ValueError): output.check()
                self.assertEqual(b'a', output.path.read_bytes())

    def test_input_validation_exclusive_creation_and_shrink(self):
        with tempfile.TemporaryDirectory() as tmp:
            for filename, limit in (('../escape',8), ('/escape',8), ('output',True), ('output',0), ('output',2**63)):
                with self.subTest(filename=filename,limit=limit), self.assertRaises(ValueError):
                    BoundedMediaOutput(tmp,filename,limit)
            path = Path(tmp)/'output';path.write_bytes(b'preserved')
            with self.assertRaises(FileExistsError): BoundedMediaOutput(tmp,'output',8)
            self.assertEqual(b'preserved',path.read_bytes())
            with BoundedMediaOutput(tmp,'new',8) as output:
                output.write(b'abcdefgh'); output.truncate(4); output.seek(0)
                self.assertEqual(8,output.highwater)
                self.assertEqual(b'abcd',output.path.read_bytes())

    def test_close_error_still_closes_remaining_readers_and_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=BoundedMediaOutput(tmp,'output.m4b',8)
            output.write(b'abc')
            first=output.reopen(output.name,1,{})
            second=output.reopen(output.name,1,{})
            first.close()
            first.close=Mock(side_effect=OSError('fixture close error'))
            with self.assertRaisesRegex(OSError,'fixture close error'):output.close()
            self.assertTrue(second._file.closed)
            self.assertTrue(output._file.closed)

    def test_cleanup_oserror_preserves_primary_failure_but_programming_error_propagates(self):
        for cleanup in (OSError('fixture close'),RuntimeError('fixture programming error')):
            with self.subTest(cleanup=type(cleanup).__name__),tempfile.TemporaryDirectory() as tmp:
                expected=ValueError if isinstance(cleanup,OSError) else RuntimeError
                with self.assertRaises(expected):
                    with BoundedMediaOutput(tmp,'output.m4b',8) as output:
                        output._readers.append(Mock(close=Mock(side_effect=cleanup)))
                        raise ValueError('primary mux failure')
                self.assertTrue(output._file.closed)
