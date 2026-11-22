"""MP3 encoding uses the shared finite, duration-derived FFmpeg deadline."""
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydub.generators import Sine

import audio_ffmpeg
import audio_export
import pcm_worker_contract
from audio_streaming import write_combined_pcm
from tts import ExportCancelled


class Mp3EncodeTimeoutTests(unittest.TestCase):
    def source(self):
        return Sine(317, sample_rate=24000).to_audio_segment(duration=300)

    def settings(self, frames, rate=24000, channels=1, width=1):
        return dict(frames=frames, frame_rate=rate, channels=channels, sample_width=width)

    def staged_pcm(self, root, name, frames):
        raw = root / name
        with raw.open('wb') as stream:
            stream.truncate(frames)
        return raw

    def test_timeout_uses_shared_duration_contract_and_scales(self):
        seen = []
        def worker(arguments, cancel_check=None, timeout=None, **kwargs):
            seen.append(timeout)
            Path(arguments[-1]).write_bytes(b'encoded mp3')
            return ''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(audio_ffmpeg, 'run_audio_ffmpeg', side_effect=worker):
                for seconds in (400, 800):
                    frames = 24000 * seconds
                    raw = self.staged_pcm(root, f'source-{seconds}.pcm', frames)
                    audio_export.export_pcm_mp3(
                        raw, root / f'out-{seconds}.mp3', self.settings(frames), lambda: False)
        self.assertEqual([pcm_worker_contract.get_duration_encode_timeout(400),
                          pcm_worker_contract.get_duration_encode_timeout(800)], seen)
        self.assertTrue(all(timeout is not None and math.isfinite(timeout) for timeout in seen))
        self.assertLess(seen[0], seen[1])

    def test_timeout_exit_reaps_encoder_and_keeps_prior_output(self):
        import media_process
        real_start = media_process.start_owned_subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / 'input.pcm'
            segment = self.source()
            settings = write_combined_pcm([segment], ['A'], raw, 0, 0)
            output = root / 'out.mp3'
            output.write_bytes(b'previous verified MP3')
            processes = []
            program = "import sys,time;open(sys.argv[1],'wb').write(b'partial');time.sleep(60)"
            def start(command, **kwargs):
                process = real_start([sys.executable, '-c', program, command[-1]], **kwargs)
                processes.append(process)
                return process
            with patch.object(media_process, 'start_owned_subprocess', side_effect=start), \
                    patch.object(pcm_worker_contract, 'get_duration_encode_timeout', return_value=.5):
                with self.assertRaises(subprocess.TimeoutExpired):
                    audio_export.export_pcm_mp3(raw, output, settings, lambda: False)
            self.assertEqual(b'previous verified MP3', output.read_bytes())
            self.assertEqual(1, len(processes))
            self.assertIsNotNone(processes[0].poll())
            self.assertEqual([], list(root.glob('.pcm-encode-*')))

    def test_cancel_still_stops_mp3_encode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            segment = self.source()
            settings = write_combined_pcm([segment], ['A'], root / 'input.pcm', 0, 0)
            output = root / 'out.mp3'
            output.write_bytes(b'previous verified MP3')
            with self.assertRaises(ExportCancelled):
                audio_export.export_pcm_mp3(root / 'input.pcm', output, settings, lambda: True)
            self.assertEqual(b'previous verified MP3', output.read_bytes())
            self.assertEqual([], list(root.glob('.pcm-encode-*')))

    def test_successful_encode_still_publishes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            segment = self.source()
            settings = write_combined_pcm([segment], ['A'], root / 'input.pcm', 0, 0)
            output = root / 'out.mp3'
            audio_export.export_pcm_mp3(root / 'input.pcm', output, settings, lambda: False)
            self.assertGreater(output.stat().st_size, 0)
            self.assertEqual([], list(root.glob('.pcm-encode-*')))
