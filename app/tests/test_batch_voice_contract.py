"""Batch paths must preserve the single-voice configuration and prompt contract."""

import copy
import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
import tts


class BatchVoiceContractTests(unittest.TestCase):
    def test_missing_voice_is_reported_without_default_custom_dispatch(self):
        for mode in ['local', 'external']:
            for missing in [None, {}]:
                with self.subTest(mode=mode, missing=missing):
                    engine = tts.TTSEngine.__new__(tts.TTSEngine)
                    engine._mode = mode
                    engine._compile_codec_enabled = False
                    engine._clear_gpu_cache = Mock()
                    batch = Mock(return_value={'completed':[2], 'failed':[]})
                    engine._local_batch_custom = batch
                    engine._external_batch = batch
                    chunks = [{'speaker':'MISSING', 'text':'hello', 'index':1},
                              {'speaker':'ASSIGNED', 'text':'world', 'index':2}]
                    config = {'ASSIGNED':{'type':'custom','voice':'Ryan'}}
                    if missing is not None:
                        config['MISSING'] = missing
                    original = copy.deepcopy((chunks, config))
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertFalse(engine.generate_voice('hello', '', 'MISSING', config, '/unused.wav'))
                        result = engine.generate_batch(chunks, config, '/unused')
                    self.assertEqual([2], result['completed'])
                    self.assertEqual([1], [i for i,_ in result['failed']])
                    self.assertIn('voice configuration', result['failed'][0][1].lower())
                    self.assertEqual(['ASSIGNED'], [r['speaker'] for r in batch.call_args.args[0]])
                    self.assertEqual(original, (chunks, config))

    def test_missing_voice_failure_survives_dynamic_narrator_dispatch(self):
        engine = tts.TTSEngine.__new__(tts.TTSEngine)
        engine._mode = 'local'
        engine._compile_codec_enabled = False
        engine._clear_gpu_cache = Mock()
        engine._local_batch_custom = Mock(return_value={'completed':[1], 'failed':[]})
        engine.generate_voice = Mock(return_value=True)
        config = {'NARRATOR':{'type':'custom','voice':'Ryan'}}
        resolved = {'NARRATOR':{'type':'custom','voice':'Aiden'}}
        chunks = [{'speaker':'MISSING','text':'missing','index':1},
                  {'speaker':'NARRATOR','text':'narration','index':2}]
        def resolve(speaker, _config, _chunk):
            return resolved if speaker == 'NARRATOR' else config
        with patch.object(tts, 'resolve_narrator_voice_config', side_effect=resolve):
            result = engine.generate_batch(chunks, config, '/unused')
        self.assertEqual([2], result['completed'])
        self.assertEqual([1], [i for i,_ in result['failed']])
        engine.generate_voice.assert_called_once()
        engine._local_batch_custom.assert_not_called()

    def test_native_custom_batch_uses_shared_anchor_order_and_writes_real_audio(self):
        engine = tts.TTSEngine.__new__(tts.TTSEngine)
        config = {'A':{'type':'custom','voice':'Ryan','character_style':'identity anchor'},
                  'B':{'type':'custom','voice':'Aiden','default_style':'legacy fallback'},
                  'C':{'type':'custom','voice':'Ryan'}}
        chunks = [{'speaker':'A','text':'first line','instruct':'angry','index':1},
                  {'speaker':'B','text':'longer second line','instruct':'','index':2},
                  {'speaker':'C','text':'short','instruct':'','index':3}]
        original = copy.deepcopy((chunks, config))
        samples = np.full(4000, 0.1, dtype='float32')
        model = SimpleNamespace(device="cuda:1", generate_custom_voice=Mock(
            side_effect=lambda **kw: ([samples.copy() for _ in kw['text']],16000)))
        engine._init_local_custom = lambda: model
        engine.ensure_custom_warmup = Mock()
        engine._clear_gpu_cache = Mock()
        engine._estimate_max_batch_size = Mock(return_value=3)
        engine._build_sub_batches = Mock(return_value=[(0,3)])
        engine._language = 'English'
        engine._max_new_tokens = 100
        fake_torch = ModuleType('torch')
        fake_torch.cuda = SimpleNamespace(is_available=lambda: True,
                                         reset_peak_memory_stats=lambda device: None,
                                         max_memory_allocated=lambda device: 0)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(sys.modules, {'torch':fake_torch}), \
             contextlib.redirect_stdout(io.StringIO()):
            result = engine._local_batch_custom(chunks, config, tmp)
            self.assertEqual([], result['failed'])
            self.assertEqual({1,2,3}, set(result['completed']))
            request = model.generate_custom_voice.call_args.kwargs
            by_text = dict(zip(request['text'],request['instruct']))
            for chunk in chunks:
                self.assertEqual(tts.anchored_instruct(config[chunk['speaker']],chunk['instruct']),
                                 by_text[chunk['text']])
                actual, rate = sf.read(Path(tmp, f"temp_batch_{chunk['index']}.wav"))
                self.assertEqual(16000, rate)
                np.testing.assert_allclose(samples, actual, atol=1e-4)
        self.assertEqual(original, (chunks, config))

    def test_dynamic_narrator_is_resolved_before_the_missing_config_check(self):
        engine = tts.TTSEngine.__new__(tts.TTSEngine)
        engine._mode = "local"
        engine._compile_codec_enabled = False
        engine.generate_voice = Mock(return_value=True)
        config = {'Narrator':{'type':'custom','narrator_strategy':'focus'},
                  'HERO':{'type':'custom','voice':'Aiden'}}
        result = engine.generate_batch([
            {'speaker':'NARRATOR','focus_speaker':'HERO','text':'narration','index':4}],
            config, '/unused')
        self.assertEqual({'completed':[4],'failed':[]}, result)
        engine.generate_voice.assert_called_once()
        self.assertEqual('Aiden', engine.generate_voice.call_args.args[3]['NARRATOR']['voice'])


if __name__ == '__main__':
    unittest.main()


class CpuBatchPeakTelemetryTests(unittest.TestCase):
    def test_cpu_and_gpu_telemetry_preserve_custom_and_clone_sub_batches_and_real_wavs(self):
        for family in ('custom','clone'):
            for available in (False,True):
                with self.subTest(family=family,available=available),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp)
                    reference=root/'reference.wav'
                    sf.write(reference,np.full(4000,0.1,dtype='float32'),16000)
                    reference_before=reference.read_bytes()
                    chunks=[{'speaker':'A','text':'first','instruct':'warm','index':7},
                            {'speaker':'A','text':'a longer second line','instruct':'','index':2},
                            {'speaker':'A','text':'last third line','instruct':'','index':9}]
                    config={'A':{'type':family,'voice':'Ryan','ref_audio':str(reference),'ref_text':'reference line'}}
                    before=copy.deepcopy((chunks,config))
                    samples={chunk['text']:np.full(4000,(i+1)/10,dtype='float32') for i,chunk in enumerate(chunks)}
                    events=[]
                    peaks=iter((1e9,3.1e9,2e9))
                    def reset(device):
                        self.assertEqual("cuda:1", device)
                        events.append('reset')
                        if not available:raise RuntimeError('CPU has no peak-memory backend')
                    def peak(device):
                        self.assertEqual("cuda:1", device)
                        events.append('peak')
                        if not available:raise RuntimeError('CPU has no peak-memory backend')
                        return next(peaks)
                    def render(**kwargs):
                        events.append('render')
                        return [samples[text].copy() for text in kwargs['text']],16000
                    fake_torch=ModuleType('torch')
                    availability=Mock(return_value=available)
                    fake_torch.cuda=SimpleNamespace(is_available=availability,
                        reset_peak_memory_stats=Mock(side_effect=reset),max_memory_allocated=Mock(side_effect=peak))
                    fake_torch.manual_seed=Mock(side_effect=lambda seed:events.append('seed:'+str(seed)))
                    model=SimpleNamespace(device="cuda:1", generate_custom_voice=Mock(side_effect=render),
                                          generate_voice_clone=Mock(side_effect=render))
                    engine=tts.TTSEngine.__new__(tts.TTSEngine)
                    engine._init_local_custom=Mock(return_value=model)
                    engine._init_local_clone=Mock(return_value=model)
                    engine.ensure_custom_warmup=Mock()
                    engine._get_clone_prompt=Mock(return_value=[SimpleNamespace(ref_code=np.zeros((4,)),ref_text='reference line')])
                    engine._clear_gpu_cache=Mock(side_effect=lambda:events.append('clear'))
                    engine._estimate_max_batch_size=Mock(return_value=1)
                    engine._build_sub_batches=Mock(return_value=[(0,1),(1,2),(2,3)])
                    engine._language='English';engine._max_new_tokens=100
                    logs=io.StringIO()
                    method=engine._local_batch_custom if family=='custom' else engine._local_batch_clone
                    with patch.dict(sys.modules,{'torch':fake_torch}),contextlib.redirect_stdout(logs):
                        result=method(chunks,config,tmp,batch_seed=41)
                    self.assertEqual([],result['failed'],logs.getvalue())
                    self.assertEqual([7,9,2],result['completed'])
                    self.assertEqual(3.1 if available else 0,result['peak_vram_gb'])
                    availability.assert_called_once_with()
                    for chunk in chunks:
                        decoded,rate=sf.read(root/f"temp_batch_{chunk['index']}.wav")
                        self.assertEqual(16000,rate)
                        np.testing.assert_allclose(samples[chunk['text']],decoded,atol=1e-4)
                    block=['seed:41']+(['reset'] if available else [])+['render']+(['peak'] if available else [])+['clear']
                    self.assertEqual(['clear']+block*3,events)
                    self.assertEqual(3 if available else 0,fake_torch.cuda.reset_peak_memory_stats.call_count)
                    self.assertEqual(3 if available else 0,fake_torch.cuda.max_memory_allocated.call_count)
                    self.assertEqual(4,engine._clear_gpu_cache.call_count)
                    self.assertEqual(3,fake_torch.manual_seed.call_count)
                    engine._estimate_max_batch_size.assert_called_once()
                    self.assertEqual(available,'Peak VRAM' in logs.getvalue())
                    self.assertEqual(before,(chunks,config))
                    self.assertEqual(reference_before,reference.read_bytes())
