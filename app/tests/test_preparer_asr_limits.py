import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import alexandria_preparer_rocm_compatible as preparer


class PreparerAsrLimitTests(unittest.TestCase):
    def test_primary_and_each_fallback_receive_same_bounded_audio(self):
        audio=np.ones(80*16000,dtype=np.float32)*.1; before=audio.copy()
        for selected in ('primary','cpp','whisperx'):
            with self.subTest(selected=selected):
                observed=[]
                def provider(name):
                    def transcribe(samples,*args,**kwargs):
                        observed.append((name,len(samples)))
                        if name!=selected:raise RuntimeError('fixture backend failed')
                        return ([{'word':'hello','start':0.0,'end':1.0}],'en')
                    return transcribe
                with patch.object(preparer,'TRANSFORMERS_WHISPER_AVAILABLE',True), \
                     patch.object(preparer,'WHISPER_CPP_AVAILABLE',True),patch.object(preparer,'WHISPERX_AVAILABLE',True), \
                     patch.object(preparer,'transcribe_with_wav2vec2',side_effect=provider('primary')), \
                     patch.object(preparer,'transcribe_with_whisper_cpp',side_effect=provider('cpp')), \
                     patch.object(preparer,'transcribe_with_whisperx_cpu',side_effect=provider('whisperx')):
                    words,language=preparer.choose_and_transcribe(audio,'cpu','en',limit=1)
                self.assertEqual('en',language);self.assertEqual('hello',words[0]['word'])
                self.assertTrue(observed)
                self.assertTrue(all(count==30*16000 for _name,count in observed),observed)
                self.assertTrue(np.array_equal(before,audio))

    def test_limits_use_existing_window_overlap_and_reject_invalid_values_before_loading(self):
        for limit,wanted in ((None,80),(1,30),(2,57),(3,80)):
            self.assertEqual(wanted*16000,preparer.get_asr_sample_count(80*16000,16000,limit))
        for limit in (0,-1,True,1.5):
            with self.subTest(limit=limit),patch.object(preparer,'transcribe_with_wav2vec2') as backend:
                with self.assertRaises(ValueError):preparer.choose_and_transcribe(np.zeros(16000),'cpu','en',limit)
                backend.assert_not_called()
            with self.subTest(direct=limit),self.assertRaises(ValueError):
                preparer.transcribe_with_wav2vec2(np.zeros(16000),'en',limit)

    def test_actual_asr_phase_bounds_detection_diarization_and_fallback_but_keeps_full_scratch(self):
        for mode in ('explicit','auto_multi','auto_single','unlimited'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);audio=root/'book.wav'
                sf.write(audio,np.ones(60*24000,dtype=np.float32)*.1,24000,subtype='PCM_16')
                original=audio.read_bytes(); paths=[]; spans=[]; calls=[]; lock_fds=[]
                def resample(data,orig_sr,target_sr):
                    indices=np.arange(round(len(data)*target_sr/orig_sr))*orig_sr/target_sr
                    return np.interp(indices,np.arange(len(data)),data).astype(np.float32)
                def detect(path,*args,**kwargs):
                    paths.append(str(Path(path).resolve()));spans.append(('detect',sf.info(path).duration,kwargs['duration_secs']))
                    return (mode=='auto_multi',[],None)
                def diarize(path,*args,**kwargs):
                    paths.append(str(Path(path).resolve()));spans.append(('diarize',sf.info(path).duration,None))
                    return [{'speaker':'speaker','start':0.0,'end':1.0}]
                def primary(samples,*args,**kwargs):
                    calls.append(('primary',len(samples)));raise RuntimeError('fixture primary failed')
                def fallback(samples,*args,**kwargs):
                    calls.append(('fallback',len(samples)))
                    return ([{'word':'hello','start':0.0,'end':1.0}],'en')
                original_acquire=preparer.acquire_run_lock
                def acquire(path):
                    fd=original_acquire(path);lock_fds.append(fd);return fd
                argv=['preparer','--audio',str(audio),'--phase','asr','--hf-token','fixture',
                      '--diarize' if mode in ('explicit','unlimited') else '--auto-detect-speakers']
                if mode!='unlimited':argv.extend(['--limit','1'])
                previous=Path.cwd()
                try:
                    os.chdir(root)
                    with patch.object(sys,'argv',argv),patch.object(preparer,'acquire_run_lock',side_effect=acquire), \
                         patch.object(preparer,'_lazy_import_torch'),patch.object(preparer,'resolve_cuda_device',return_value='cpu'), \
                         patch.object(preparer,'log_torch_info'),patch.object(preparer,'log_gpu_stats'), \
                         patch.object(preparer,'validate_inputs'),patch.object(preparer,'clear_vram'), \
                         patch.object(preparer,'_lazy_import_librosa',return_value=SimpleNamespace(
                             load=lambda path,**kwargs:sf.read(path,dtype='float32'),resample=resample)), \
                         patch.object(preparer,'detect_speaker_count',side_effect=detect), \
                         patch.object(preparer,'diarize_audio',side_effect=diarize), \
                         patch.object(preparer,'TRANSFORMERS_WHISPER_AVAILABLE',True), \
                         patch.object(preparer,'WHISPER_CPP_AVAILABLE',True), \
                         patch.object(preparer,'transcribe_with_wav2vec2',side_effect=primary), \
                         patch.object(preparer,'transcribe_with_whisper_cpp',side_effect=fallback), \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(0,preparer.main())
                finally:
                    os.chdir(previous)
                    for fd in lock_fds:os.close(fd)
                wanted=60 if mode=='unlimited' else 30
                self.assertTrue(spans)
                self.assertTrue(all(duration==wanted for _name,duration,_hint in spans),spans)
                self.assertTrue(all(hint in (None,wanted) for _name,_duration,hint in spans),spans)
                self.assertEqual([('primary',wanted*16000),('fallback',wanted*16000)],calls)
                self.assertEqual(60,sf.info(root/'dataset_temp/audio_24k_scratch.wav').duration)
                self.assertEqual(original,audio.read_bytes())
                self.assertTrue(all(Path(path).exists() == (mode=='unlimited') for path in paths))
                report=json.loads((root/'dataset_temp/asr_segments.json').read_text())
                self.assertEqual('hello',report['word_segments'][0]['word'])
                self.assertEqual(60,report['audio_duration'])
                self.assertEqual(mode!='auto_single',(root/'dataset_temp/diarization.json').exists())
