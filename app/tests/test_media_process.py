"""Shared decoding ownership stays independent of TTS policy and dependencies."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
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

    def test_inherited_report_cannot_escape_pipe_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/'unexpected.log'
            report_name=str(report).replace('\\','\\\\').replace(':','\\:').replace("'","\\'")
            original={**os.environ,'FFREPORT':'file='+report_name+':level=48',
                      'AA2_TEST_MARKER':'preserved'}
            command=['ffmpeg','-nostdin','-v','error','-f','lavfi',
                '-i','anullsrc=r=24000:cl=mono','-t','0.01','-f','null','-']
            # Native integration prerequisite: FFmpeg with documented FFREPORT.
            subprocess.run(command,env=original,cwd=tmp,check=True,
                           capture_output=True,timeout=10)
            self.assertTrue(report.is_file())
            self.assertGreater(report.stat().st_size,0)
            report.unlink()
            with patch.dict(os.environ,original,clear=True):
                result=run_owned_media_check([sys.executable,'-c',
                    'import os;print(os.environ.get("FFREPORT","absent"));'
                    'print(os.environ["AA2_TEST_MARKER"])'],lambda:None,10,output_limit=1024)
                self.assertEqual(b'absent\npreserved\n',result.replace(b'\r\n',b'\n'))
                # Exercise FFmpeg's actual report writer, not only environment
                # forwarding. The external path must remain absent.
                run_owned_media_check(['ffmpeg','-nostdin','-v','error','-f','lavfi',
                    '-i','anullsrc=r=24000:cl=mono','-t','0.01','-f','null','-'],lambda:None,10)
                self.assertFalse(report.exists())
                self.assertEqual(original['FFREPORT'],os.environ['FFREPORT'])
                self.assertIn('FFREPORT',original)
