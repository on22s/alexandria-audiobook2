"""Native worker races with CPU model providers; no model or GPU is loaded."""
import concurrent.futures
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
import tts


class LocalTTSConcurrencyTests(unittest.TestCase):
    def test_two_engines_do_not_reseed_an_inflight_custom_render(self):
        for second_seed in (22,-1):
            with self.subTest(second_seed=second_seed),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);entered=threading.Event();second_started=threading.Event();reseeded=threading.Event()
                state={'seed':0};observed=[]
                def seed(value):
                    state['seed']=value
                    if value==22:reseeded.set()
                def first_render(**kwargs):
                    entered.set();self.assertTrue(second_started.wait(2))
                    reseeded.wait(.15)
                    observed.append(state['seed'])
                    return [np.full(240,state['seed']/100)],24000
                def second_render(**kwargs):
                    # An unseeded call still uses the process RNG and must wait.
                    state['seed']=99
                    return [np.full(240,.22)],24000
                first=tts.TTSEngine({'tts':{'mode':'local'}});second=tts.TTSEngine({'tts':{'mode':'local'}})
                first_model=types.SimpleNamespace(generate_custom_voice=first_render)
                second_model=types.SimpleNamespace(generate_custom_voice=second_render)
                torch=types.SimpleNamespace(manual_seed=seed)
                def run_second():
                    second_started.set()
                    return second.generate_custom_voice('second','', 'B',{'B':{'voice':'Ryan','seed':second_seed}},str(root/'second.wav'))
                with patch.dict('sys.modules',{'torch':torch}),patch.object(first,'_init_local_custom',return_value=first_model),patch.object(second,'_init_local_custom',return_value=second_model),concurrent.futures.ThreadPoolExecutor(2) as pool:
                    one=pool.submit(first.generate_custom_voice,'first','','A',{'A':{'voice':'Ryan','seed':11}},str(root/'first.wav'))
                    self.assertTrue(entered.wait(2));two=pool.submit(run_second)
                    self.assertTrue(one.result(3));self.assertTrue(two.result(3))
                self.assertEqual([11],observed)
                audio,rate=sf.read(root/'first.wav');self.assertEqual(24000,rate)
                np.testing.assert_allclose(audio,.11,atol=4e-5)

    def test_parallel_clone_prompt_requests_build_once_and_share_the_captured_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);ref=root/'ref.wav';sf.write(ref,np.zeros(240),24000)
            entered=threading.Event();second_started=threading.Event();second_build=threading.Event();calls=[]
            def create(**kwargs):
                calls.append(kwargs)
                if len(calls)==1:
                    entered.set();self.assertTrue(second_started.wait(2));second_build.wait(.15)
                else:second_build.set()
                return object()
            engine=tts.TTSEngine({'tts':{'mode':'local'}});config={'A':{'ref_audio':str(ref),'ref_text':'reference'}}
            def other():
                second_started.set();return engine._get_clone_prompt('A',config)
            with patch.object(engine,'_init_local_clone',return_value=types.SimpleNamespace(create_voice_clone_prompt=create)),concurrent.futures.ThreadPoolExecutor(2) as pool:
                one=pool.submit(engine._get_clone_prompt,'A',config);self.assertTrue(entered.wait(2));two=pool.submit(other)
                first=one.result(3);second=two.result(3)
            self.assertEqual(1,len(calls));self.assertIs(first,second)

    def test_failure_releases_admission_for_another_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);first=tts.TTSEngine({'tts':{'mode':'local'}});second=tts.TTSEngine({'tts':{'mode':'local'}})
            broken=types.SimpleNamespace(generate_custom_voice=lambda **kwargs:(_ for _ in ()).throw(RuntimeError('fixture failure')))
            healthy=types.SimpleNamespace(generate_custom_voice=lambda **kwargs:([np.full(240,.1)],24000))
            with patch.dict('sys.modules',{'torch':types.SimpleNamespace(manual_seed=lambda seed:None)}),patch.object(first,'_init_local_custom',return_value=broken),patch.object(second,'_init_local_custom',return_value=healthy),concurrent.futures.ThreadPoolExecutor(1) as pool:
                config={'A':{'voice':'Ryan','seed':3}}
                self.assertFalse(first.generate_custom_voice('first','','A',config,str(root/'bad.wav')))
                self.assertTrue(pool.submit(second.generate_custom_voice,'second','','A',config,str(root/'good.wav')).result(3))
            self.assertFalse((root/'bad.wav').exists());self.assertEqual(240,len(sf.read(root/'good.wav')[0]))


class GeneratedStereoFrameTests(unittest.TestCase):
    def test_stereo_preserves_frame_count_channels_and_each_channel_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'stereo.wav';values=np.column_stack((np.full(480,.25),np.full(480,-.5)))
            tts.TTSEngine._save_wav(values,24000,str(path))
            info=sf.info(path);self.assertEqual(480,info.frames);self.assertEqual(2,info.channels)
            audio,rate=sf.read(path);self.assertEqual(24000,rate);np.testing.assert_allclose(audio,values,atol=4e-5)

    def test_mono_and_one_channel_arrays_keep_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            for shape in ((480,),(480,1)):
                path=Path(tmp)/'mono.wav';tts.TTSEngine._save_wav(np.full(shape,.25),24000,str(path))
                info=sf.info(path);self.assertEqual(480,info.frames);self.assertEqual(1,info.channels)

    def test_invalid_channel_rank_fails_instead_of_inventing_a_long_mono_clip(self):
        from audio_validation import GeneratedAudioError
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'invalid.wav'
            for shape in ((2,3,4),(2,480)):
                with self.subTest(shape=shape),self.assertRaises((ValueError,GeneratedAudioError)):
                    tts.TTSEngine._save_wav(np.zeros(shape),24000,str(path))
                self.assertFalse(path.exists())
