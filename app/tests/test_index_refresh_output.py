"""Native CPU index workers with large, newline-free diagnostics."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('refresh_output_test', ROOT / 'refresh_indexes.py')
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)


class IndexRefreshOutputTests(unittest.TestCase):
    def test_large_native_streams_keep_failure_tail_without_output_sized_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = Path(tmp, 'worker.py')
            worker.write_text("import os,sys\n"
                              "for _ in range(128):\n"
                              " os.write(1,b'A'*65536);os.write(2,b'B'*65536)\n"
                              "os.write(2,'\\n最終 diagnostic\\n'.encode())\n"
                              "sys.exit(7)\n", encoding='utf-8')
            tracemalloc.start()
            try:
                with patch.object(refresh, 'REPO', tmp):
                    ok, output = refresh.run('worker.py', False, sys.executable)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            print(f'16 MiB native output: parent traced peak {peak} bytes; returned {len(output)} characters')
            self.assertFalse(ok)
            self.assertTrue(output.endswith('最終 diagnostic'), output[-100:])
            self.assertLessEqual(len(output), 32768)
            self.assertLess(peak, 1024 * 1024, f'parent traced allocation peak: {peak}')

    def test_small_output_order_newlines_and_check_flag_remain_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'worker.py').write_text(
                "import os,sys\nos.write(2,b' stderr\\r\\n')\n"
                "os.write(1,b' stdout\\r\\n')\n"
                "sys.exit(0 if '--check' in sys.argv else 3)\n")
            with patch.object(refresh, 'REPO', tmp):
                self.assertEqual((True, 'stdout\n stderr'),
                                 refresh.run('worker.py', True, sys.executable))
                self.assertEqual((False, 'stdout\n stderr'),
                                 refresh.run('worker.py', False, sys.executable))

    def test_launch_failure_closes_both_temporary_streams_and_propagates(self):
        streams = []
        actual_temp = tempfile.TemporaryFile
        def create_stream(*args, **kwargs):
            stream = actual_temp(*args, **kwargs)
            streams.append(stream)
            return stream
        with patch.object(refresh.tempfile, 'TemporaryFile', side_effect=create_stream):
            with self.assertRaises(FileNotFoundError):
                refresh.run('unused.py', False, '/nonexistent/alexandria-python')
        self.assertEqual(2, len(streams))
        self.assertTrue(all(stream.closed for stream in streams))
