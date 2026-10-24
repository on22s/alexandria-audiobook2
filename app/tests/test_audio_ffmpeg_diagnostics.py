"""Real noisy child drains bounded diagnostics without a temporary log file."""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
import media_process
from audio_ffmpeg import run_audio_ffmpeg


class AudioFFmpegDiagnosticsTests(unittest.TestCase):
    def test_large_noisy_output_retains_final_record_and_environment_without_disk_spool(self):
        start = media_process.start_owned_subprocess
        children = []
        code = ('import os,sys; sys.stderr.write("x"*2000000); '
                'sys.stderr.write("\\nFINAL="+os.environ["AA2_TEST_MARKER"]+"\\n")')
        def launch(_command, **kwargs):
            self.assertEqual(subprocess.PIPE, kwargs['stderr'])
            child = start([sys.executable, '-c', code], **kwargs)
            children.append(child)
            return child
        with patch.object(media_process, 'start_owned_subprocess', side_effect=launch):
            text = run_audio_ffmpeg([], timeout=None,
                environment={**os.environ, 'AA2_TEST_MARKER':'complete'})
        self.assertLessEqual(len(text.encode()),65536)
        self.assertTrue(text.endswith('FINAL=complete\n'))
        self.assertEqual(0, children[0].poll())

    def test_failed_encoder_preserves_runtime_error_and_final_diagnostic(self):
        start = media_process.start_owned_subprocess
        def launch(_command, **kwargs):
            return start([sys.executable, '-c',
                'import sys;sys.stderr.write("x"*2000000+"\\nfinal failure\\n");sys.exit(7)'], **kwargs)
        with patch.object(media_process, 'start_owned_subprocess', side_effect=launch):
            with self.assertRaisesRegex(RuntimeError, 'final failure') as error:
                run_audio_ffmpeg([], timeout=10)
        self.assertLess(len(str(error.exception)),2100)
