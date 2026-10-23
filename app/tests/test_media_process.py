"""Shared decoding ownership stays independent of TTS policy and dependencies."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import subprocess
import sys
import threading
import unittest

from media_process import run_owned_media_check


class MediaProcessTests(unittest.TestCase):
    def test_import_needs_no_model_or_export_policy_modules(self):
        code = ('import sys; import media_process, audio_pcm_decode, audio_source_io; '
                'forbidden={"tts","torch","pydantic","export_quality"}; '
                'overlap=forbidden.intersection(sys.modules); '
                'sys.exit(str(sorted(overlap)) if overlap else 0)')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_callers_cancellation_type_survives_reader_cleanup(self):
        class SnapshotCancelled(RuntimeError):
            pass
        cancelled = SnapshotCancelled('Cancelled inside snapshot validation')
        def consume(_block):
            raise cancelled
        with self.assertRaises(SnapshotCancelled) as result:
            run_owned_media_check([sys.executable, '-c',
                'import sys,time;sys.stdout.buffer.write(b"x"*65536);sys.stdout.flush();time.sleep(30)'],
                lambda: None, 5, output_limit=65536, consume_output=consume,
                cancellation_errors=(SnapshotCancelled,))
        self.assertIs(cancelled, result.exception)

    def test_timeout_retains_callback_ownership_until_its_write_unwinds(self):
        entered, release = threading.Event(), threading.Event()
        def consume(_block):
            entered.set()
            if not release.wait(15):
                raise RuntimeError('Test did not release its owned sink')
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(run_owned_media_check, [sys.executable, '-c',
                'import sys,time;sys.stdout.buffer.write(b"x"*65536);sys.stdout.flush();time.sleep(30)'],
                lambda: None, .2, output_limit=65536, consume_output=consume)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(FutureTimeoutError):
                    future.result(timeout=5)
            finally:
                release.set()
            with self.assertRaises(subprocess.TimeoutExpired):
                future.result(timeout=5)
