"""Shared decoding ownership stays independent of TTS policy and dependencies."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
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
        entered, release, expired = threading.Event(), threading.Event(), threading.Event()
        def consume(_block):
            entered.set()
            if not release.wait(15):
                raise RuntimeError('Test did not release its owned sink')
        # Reach the owned sink before expiring this operation's clock. Other
        # modules keep real time, and callback startup still has a bounded wait.
        clock_started = False
        def monotonic():
            nonlocal clock_started
            # The worker may first run after the entry wait has already failed.
            if not clock_started:
                clock_started = True
                return 0.0
            return float(expired.is_set())
        clock = SimpleNamespace(monotonic=monotonic)
        with patch('media_process.time', clock), ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(run_owned_media_check, [sys.executable, '-c',
                'import sys,time;sys.stdout.buffer.write(b"x"*65536);sys.stdout.flush();time.sleep(30)'],
                lambda: None, .2, output_limit=65536, consume_output=consume)
            try:
                self.assertTrue(entered.wait(5))
                expired.set()
                with self.assertRaises(FutureTimeoutError):
                    future.result(timeout=5)
            finally:
                expired.set()
                release.set()
            with self.assertRaises(subprocess.TimeoutExpired):
                future.result(timeout=5)

    def test_inherited_and_explicit_reports_cannot_escape_pipe_diagnostics(self):
        for explicit in (False,True):
            with self.subTest(explicit=explicit),tempfile.TemporaryDirectory() as tmp:
                report=Path(tmp)/'unexpected.log'
                report_name=str(report).replace('\\','\\\\').replace(':','\\:').replace("'","\\'")
                setting='file='+report_name+':level=48'
                original={**os.environ,'FFREPORT':setting,'ffreport':setting,'FfReport':setting,
                          'AA2_TEST_MARKER':'preserved'}
                command=['ffmpeg','-nostdin','-v','error','-f','lavfi',
                    '-i','anullsrc=r=24000:cl=mono','-t','0.01','-f','null','-']
                # Native integration prerequisite: FFmpeg with documented FFREPORT.
                control={key:value for key,value in original.items() if key not in ('ffreport','FfReport')}
                subprocess.run(command,env=control,cwd=tmp,check=True,
                               capture_output=True,timeout=10)
                self.assertTrue(report.is_file())
                self.assertGreater(report.stat().st_size,0)
                report.unlink()
                with patch.dict(os.environ,original,clear=True):
                    environment=original if explicit else None
                    result=run_owned_media_check([sys.executable,'-c',
                        'import os;print("present" if any(k.upper()=="FFREPORT" for k in os.environ) else "absent");'
                        'print(os.environ["AA2_TEST_MARKER"])'],lambda:None,10,
                        output_limit=1024,environment=environment)
                    self.assertEqual(b'absent\npreserved\n',result.replace(b'\r\n',b'\n'))
                    # Also exercise the actual FFmpeg report writer, rather than
                    # merely trusting that an absent variable suppresses it.
                    run_owned_media_check(['ffmpeg','-nostdin','-v','error','-f','lavfi',
                        '-i','anullsrc=r=24000:cl=mono','-t','0.01','-f','null','-'],
                        lambda:None,10,environment=environment)
                    self.assertFalse(report.exists())
                    self.assertEqual(original['FFREPORT'],os.environ['FFREPORT'])
                    self.assertEqual(setting,original['ffreport'])
                    self.assertIn('FFREPORT',original)
