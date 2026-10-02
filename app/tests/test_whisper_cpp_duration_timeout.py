"""Finite duration-aware deadlines; no model or multi-hour PCM allocation."""
from contextlib import ExitStack
import json
import math
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import soundfile as sf
import alexandria_preparer_rocm_compatible as preparer


class DurationOnlyAudio:
    def __init__(self, seconds): self.samples = round(seconds * 16000)
    def __len__(self): return self.samples


class WhisperCppDurationTimeoutTests(unittest.TestCase):
    def test_finite_deadline_scales_with_long_audio_and_short_pcm_is_unchanged(self):
        for seconds, expected in ((1,3600),(1800,3600),(1800.5,3601),(7200,14400),(27*3600,194400)):
            with self.subTest(seconds=seconds), ExitStack() as stack:
                paths=[]
                audio = np.zeros(16000,dtype=np.float32) if seconds==1 else DurationOnlyAudio(seconds)
                original_write = sf.write
                def write(path, value, samplerate):
                    self.assertIs(value,audio)
                    original_write(path, value if seconds==1 else np.zeros(16000), samplerate)
                def run(command, **kwargs):
                    paths.append(Path(command[command.index('--file')+1]))
                    self.assertEqual(expected,kwargs['timeout'])
                    self.assertTrue(math.isfinite(kwargs['timeout']))
                    self.assertEqual(16000,sf.info(paths[-1]).samplerate)
                    # A known 2-hour synthetic worker would fail under the old hour cap.
                    if seconds==27*3600 and kwargs['timeout'] < 7200:
                        raise subprocess.TimeoutExpired(command,kwargs['timeout'])
                    Path(command[command.index('--output-file')+1]+'.json').write_text(json.dumps({
                        'result':{'language':'en'},'transcription':[{'text':'hello','offsets':{'from':100,'to':400}}]}))
                    return SimpleNamespace(returncode=0,stdout='',stderr='')
                stack.enter_context(patch.object(preparer,'WHISPER_CPP_AVAILABLE',True))
                stack.enter_context(patch.object(preparer.sf,'write',side_effect=write))
                stack.enter_context(patch.object(preparer.subprocess,'run',side_effect=run))
                result=preparer.transcribe_with_whisper_cpp(audio,'en')
                self.assertEqual(([{'word':'hello','start':.1,'end':.4}],'en'),result)
                self.assertTrue(paths)
                self.assertTrue(all(not p.parent.exists() for p in paths))

    def test_timeout_propagates_and_removes_written_pcm_and_partial_output(self):
        paths=[]
        def timeout(command, **kwargs):
            path=Path(command[command.index('--file')+1]);paths.append(path)
            self.assertTrue(path.is_file())
            Path(command[command.index('--output-file')+1]+'.json').write_text('{partial')
            raise subprocess.TimeoutExpired(command,kwargs['timeout'])
        with patch.object(preparer,'WHISPER_CPP_AVAILABLE',True), \
                patch.object(preparer.subprocess,'run',side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired) as error:
                preparer.transcribe_with_whisper_cpp(np.zeros(16000,dtype=np.float32),'en')
        self.assertEqual(3600,error.exception.timeout)
        self.assertTrue(paths)
        self.assertTrue(all(not p.parent.exists() for p in paths))
