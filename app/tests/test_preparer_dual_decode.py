import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
from tests import test_preparer_run_state as support

p = support.preparer


class PreparerDualDecodeTests(unittest.TestCase):
    def test_independent_streams_match_old_two_decodes_sample_exactly(self):
        for rate, channels in ((32000, 1), (44100, 2)):
            with self.subTest(rate=rate, channels=channels), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                timeline = np.arange(rate * 2 + 123) / rate
                audio = .3 * np.sin(timeline * 997 * 2 * np.pi)
                if channels == 2:
                    audio = np.column_stack((audio, .2 * np.cos(timeline * 331 * 2 * np.pi)))
                source = root / 'source.wav'
                sf.write(source, audio, rate, subtype='PCM_24')
                source_bytes = source.read_bytes()
                p._ffmpeg_decode_to_wav(str(source), str(root / 'old.wav'), 24000)
                old = p.decode_audio_to_memmap(str(source), 16000, str(root / 'old.f32'))
                try:
                    current = p.decode_audio_to_asr_streams(str(source), str(root / 'new.wav'), str(root / 'new.f32'))
                    try:
                        self.assertIsInstance(current, np.memmap)
                        np.testing.assert_array_equal(old, current)
                        old_pcm, old_rate = sf.read(root / 'old.wav', dtype='int16')
                        new_pcm, new_rate = sf.read(root / 'new.wav', dtype='int16')
                        self.assertEqual(24000, new_rate)
                        self.assertEqual(old_rate, new_rate)
                        np.testing.assert_array_equal(old_pcm, new_pcm)
                    finally:
                        current._mmap.close()
                finally:
                    old._mmap.close()
                self.assertEqual(source_bytes, source.read_bytes())

    def test_actual_asr_phase_decodes_once_keeps_scratch_and_cleans_mapping(self):
        commands = []
        run = subprocess.run
        def record(command, **kwargs):
            commands.append(command)
            return run(command, **kwargs)
        with patch.object(p.subprocess, 'run', side_effect=record):
            result = unittest.TestResult()
            support.PreparerRunStateTests('test_long_audio_asr_uses_disk_backed_samples_and_cleans_them').run(result)
            self.assertEqual(1, result.testsRun)
            self.assertEqual([], result.errors)
            self.assertEqual([], result.failures)
            self.assertEqual([], result.skipped)
        decoded = [command for command in commands if command[0] == 'ffmpeg' and '-i' in command]
        self.assertEqual(1, len(decoded))
        self.assertIn('pcm_s16le', decoded[0])
        self.assertIn('pcm_f32le', decoded[0])

    def test_decoder_failure_removes_temporary_asr_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path.cwd()
            os.chdir(tmp)
            try:
                sf.write('source.wav', np.ones(16000, dtype='float32') * .1, 16000)
                def fail(command, **kwargs):
                    output = command[-1]
                    Path(output).write_bytes(b'partial raw output')
                    raise subprocess.CalledProcessError(1, command)
                with patch.object(sys, 'argv', ['preparer', '--audio', 'source.wav', '--phase', 'asr']), patch.object(p, 'acquire_run_lock', return_value=1), patch.object(p, 'get_run_identity', return_value={}), patch.object(p, 'ensure_run_manifest'), patch.object(p, 'validate_scratch_path'), patch.object(p, '_wav_overflow_info', return_value=(True, 601, 1)), patch.object(p, '_lazy_import_torch'), patch.object(p, 'resolve_cuda_device', return_value='cpu'), patch.object(p, 'log_torch_info'), patch.object(p, 'mark_artifact_complete') as receipt, patch.object(p.subprocess, 'run', side_effect=fail), patch.object(p, 'choose_and_transcribe') as transcribe:
                    with self.assertLogs('alexandria', 'ERROR') as logs:
                        self.assertEqual(1, p.main())
                    self.assertIn('returned non-zero exit status 1', '\n'.join(logs.output))
                transcribe.assert_not_called()
                receipt.assert_not_called()
                self.assertEqual([], list(Path('dataset_temp').glob('alexandria_asr_*')))
            finally:
                os.chdir(old)
