"""Submission deadlines and actual copy-failure cleanup for external TTS."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf
import tts


class ExternalBatchDeadlineTests(unittest.TestCase):
    def engine(self):
        return tts.TTSEngine({'tts':{'mode':'external','url':'http://fixture','parallel_workers':2}})

    def test_later_request_cannot_complete_after_its_submission_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);engine=self.engine();engine._external_timeout=.15
            release=threading.Event();done=[threading.Event(),threading.Event()]
            old=b'prior approved output'
            for index in (0,1):(root/f'temp_batch_{index}.wav').write_bytes(old)
            def render(text,instruct,speaker,config,path,endpoint=None,cancelled=None):
                index=int(text)
                try:
                    if index==0:release.wait(2)
                    else:time.sleep(.24)
                    sf.write(path,np.full(240,.1),24000,format='WAV')
                    return True
                finally:done[index].set()
            chunks=[{'index':i,'speaker':'A','text':str(i)} for i in (0,1)]
            with patch.object(engine,'_external_generate_custom',side_effect=render):
                started=time.monotonic()
                try:result=engine._external_batch(chunks,{'A':{'voice':'Ryan'}},str(root),'custom')
                finally:
                    elapsed=time.monotonic()-started;release.set()
                    self.assertTrue(all(event.wait(2) for event in done))
            # Let worker finally cleanup finish after its completion signal.
            limit=time.monotonic()+1
            while list(root.glob('*.pending.*')) and time.monotonic()<limit:time.sleep(.01)
            self.assertEqual([],result['completed'])
            self.assertEqual([0,1],[index for index,_ in result['failed']])
            self.assertLess(elapsed,.23)
            for index in (0,1):self.assertEqual(old,(root/f'temp_batch_{index}.wav').read_bytes())
            self.assertEqual([],list(root.glob('*.pending.*')))

    def test_completed_in_time_later_request_survives_an_earlier_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);engine=self.engine();engine._external_timeout=.15
            release=threading.Event();done=threading.Event()
            def render(text,instruct,speaker,config,path,endpoint=None,cancelled=None):
                try:
                    if text=='0':release.wait(2)
                    sf.write(path,np.full(240,.1),24000,format='WAV');return True
                finally:
                    if text=='0':done.set()
            chunks=[{'index':i,'speaker':'A','text':str(i)} for i in (0,1)]
            with patch.object(engine,'_external_generate_custom',side_effect=render):
                try:result=engine._external_batch(chunks,{'A':{'voice':'Ryan'}},str(root),'custom')
                finally:release.set();self.assertTrue(done.wait(2))
            self.assertEqual([1],result['completed'],result);self.assertEqual([0],[i for i,_ in result['failed']])
            audio,rate=sf.read(root/'temp_batch_1.wav');self.assertEqual(24000,rate);self.assertEqual(240,len(audio))

    def test_completion_timestamp_rejects_a_late_success_even_if_its_future_is_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);engine=self.engine();engine._external_timeout=.15
            def render(text,instruct,speaker,config,path,endpoint=None,cancelled=None):
                if text=='1':time.sleep(.21)
                sf.write(path,np.full(240,.1),24000,format='WAV');return True
            publish=tts.publish_audio_output
            def slow_first_publish(staging,path,cancelled=None):
                if path.endswith('temp_batch_0.wav'):time.sleep(.26)
                return publish(staging,path,cancelled)
            with patch.object(engine,'_external_generate_custom',side_effect=render),patch.object(tts,'publish_audio_output',side_effect=slow_first_publish):
                result=engine._external_batch([{'index':i,'speaker':'A','text':str(i)} for i in (0,1)],{'A':{'voice':'Ryan'}},str(root),'custom')
            self.assertEqual([0],result['completed']);self.assertEqual([1],[i for i,_ in result['failed']])
            self.assertTrue((root/'temp_batch_0.wav').exists());self.assertFalse((root/'temp_batch_1.wav').exists())
            self.assertEqual([],list(root.glob('*.pending.*')))

    def test_actual_partial_copy_failure_cleans_all_private_files_and_preserves_published_audio(self):
        for kind in ('custom','clone'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=root/'source.wav';sf.write(source,np.full(240,.2),24000)
                engine=self.engine();output=root/'temp_batch_0.wav';output.write_bytes(b'prior output')
                lock=threading.Lock();provider=SimpleNamespace(predict=lambda *args,**kwargs:(str(source),))
                copied=[]
                def fail_copy(source_path,destination,*args,**kwargs):
                    copied.append(str(destination));Path(destination).write_bytes(b'partial bytes')
                    raise OSError('fixture disk full')
                config={'A':{'voice':'Ryan','ref_audio':str(source),'ref_text':'Reference'}}
                with patch.object(engine,'_external_endpoint',return_value=(provider,lock)),patch.dict('sys.modules',{'gradio_client':SimpleNamespace(handle_file=lambda path:path)}),patch.object(tts.shutil,'copy',side_effect=fail_copy):
                    result=engine._external_batch([{'index':0,'speaker':'A','text':'known'}],config,str(root),kind)
                self.assertTrue(copied);self.assertEqual([],result['completed']);self.assertEqual([0],[i for i,_ in result['failed']])
                self.assertEqual(b'prior output',output.read_bytes())
                self.assertEqual([],list(root.glob('*.pending.*')))
                self.assertEqual([],list(root.glob('*.tmp')))
