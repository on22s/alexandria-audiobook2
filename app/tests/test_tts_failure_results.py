"""Native chunk persistence and external workers retain independent failure causes."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
import tts
from tests import test_chunk_generation_identity as identity

class TTSFailureResultTests(unittest.TestCase):
    def get_result(self, engine, root, config=None, speaker='A'):
        if config is None:
            config={'A':{'type':'custom','voice':'Ryan'}}
        return engine.generate_voice_result('Known words','',speaker,config,str(root/'out.wav'))

    def test_native_chunk_json_preserves_previously_lost_causes(self):
        for kind, expected in (('configuration','No voice configuration'),('backend','local TTS mode'),('model','model download unavailable')):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                root=Path(tmp);manager=identity.ChunkGenerationIdentityTests().get_manager(root)
                engine=tts.TTSEngine({'tts':{'mode':'external' if kind=='backend' else 'local'}})
                if kind=='configuration':
                    Path(manager.voice_config_path).write_text('{}')
                elif kind=='backend':
                    Path(manager.voice_config_path).write_text(json.dumps({'A':{'type':'lora'}}))
                with patch.object(manager,'get_engine',return_value=engine),patch.object(engine,'_init_local_custom',side_effect=OSError('model download unavailable')):
                    success,message=manager.generate_chunk_audio(0)
                self.assertFalse(success);self.assertIn(expected,message);self.assertIn('Select local TTS' if kind=='backend' else 'before retrying',message)
                row=manager.load_chunks()[0];self.assertEqual(message,row['error']);self.assertEqual('error',row['status'])
                self.assertEqual([],list(root.glob('chunk_*.wav')))

    def test_oom_reference_and_empty_audio_have_specific_causes(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            import torch
            root=Path(tmp);engine=tts.TTSEngine({'tts':{'mode':'local'}})
            with patch.object(engine,'_init_local_custom',side_effect=torch.OutOfMemoryError('GPU allocation failed')):
                result=self.get_result(engine,root)
            self.assertEqual('out_of_memory',result.failure.category);self.assertIn('Free GPU memory',result.failure.next_action)
            with patch.object(engine,'_get_clone_prompt',side_effect=FileNotFoundError('reference.wav missing')):
                result=self.get_result(engine,root,{'A':{'type':'clone','ref_audio':'reference.wav','ref_text':'words'}})
            self.assertEqual('missing_asset',result.failure.category);self.assertIn('reference.wav',result.failure.detail)
            with patch.object(engine,'_init_local_custom',return_value=SimpleNamespace(generate_custom_voice=lambda **kw:([],24000))):
                result=self.get_result(engine,root)
            self.assertEqual('invalid_audio',result.failure.category)

    def test_concurrent_external_requests_keep_causes_isolated_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            root=Path(tmp);engine=tts.TTSEngine({'tts':{'mode':'external'}});barrier=threading.Barrier(2);results={}
            class Client:
                def predict(self,**kwargs):
                    barrier.wait(timeout=3)
                    raise OSError(kwargs['text']+' token=private-value')
            def run(name):
                results[name]=engine.generate_voice_result(name,'','A',{'A':{'type':'custom'}},str(root/(name+'.wav')))
            with patch.object(engine,'_external_endpoint',side_effect=lambda endpoint=None:(Client(),threading.Lock())):
                threads=[threading.Thread(target=run,args=(name,)) for name in ('first','second')]
                for thread in threads:thread.start()
                for thread in threads:
                    thread.join(timeout=5);self.assertFalse(thread.is_alive())
            for name in ('first','second'):
                self.assertFalse(results[name].success);self.assertEqual(name+'. token=[REDACTED]',results[name].failure.detail)
            self.assertIsNone(tts._TTS_FAILURES.get())

    def test_timeout_late_failure_cannot_contaminate_next_success(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);engine=tts.TTSEngine({'tts':{'mode':'external'}});engine._external_timeout=.05
            release=threading.Event();late_done=threading.Event()
            def delayed(text,instruct,speaker,config,path,cancelled=None):
                release.wait(timeout=3);tts.apply_tts_failure(OSError('late previous failure'));late_done.set();return False
            with patch.object(engine,'_external_generate_custom',side_effect=delayed):result=self.get_result(engine,root)
            self.assertEqual('timeout',result.failure.category);release.set();self.assertTrue(late_done.wait(timeout=3))
            with patch.object(engine,'generate_voice',return_value=True):next_result=self.get_result(engine,root)
            self.assertTrue(next_result.success);self.assertIsNone(next_result.failure);self.assertEqual('timeout',result.failure.category)

    def test_success_discards_failure_and_ensemble_retains_member_cause(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            root=Path(tmp);engine=tts.TTSEngine({'tts':{'mode':'local'}})
            def succeeds(*args):tts.apply_tts_failure(OSError('earlier attempt'));return True
            with patch.object(engine,'generate_voice',side_effect=succeeds):result=self.get_result(engine,root)
            self.assertTrue(result.success);self.assertIsNone(result.failure)
            config={'GROUP':{'type':'ensemble','members':['A']},'A':{'type':'custom'}}
            with patch.object(engine,'_init_local_custom',side_effect=OSError('member model unavailable')):result=self.get_result(engine,root,config,'GROUP')
            self.assertEqual('member model unavailable',result.failure.detail)

    def test_external_invalid_audio_preserves_prior_output(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            root=Path(tmp);broken=root/'broken.wav';broken.write_bytes(b'not audio');output=root/'out.wav';sf.write(output,np.zeros(1200),24000);prior=output.read_bytes()
            engine=tts.TTSEngine({'tts':{'mode':'external'}});client=SimpleNamespace(predict=lambda **kw:(str(broken),))
            with patch.object(engine,'_external_endpoint',return_value=(client,threading.Lock())):result=self.get_result(engine,root)
            self.assertFalse(result.success);self.assertIsNotNone(result.failure);self.assertEqual(prior,output.read_bytes());self.assertEqual([],list(root.glob('out.wav.pending.*')))

    def test_stale_input_refuses_failure_and_legacy_false_remains_compatible(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            root=Path(tmp);manager=identity.ChunkGenerationIdentityTests().get_manager(root);engine=tts.TTSEngine({'tts':{'mode':'local'}});snapshot=[]
            def changes_input():
                manager.update_chunk(0,{'text':'edited words'});snapshot.extend(manager.load_chunks());raise OSError('old input failed')
            with patch.object(manager,'get_engine',return_value=engine),patch.object(engine,'_init_local_custom',side_effect=changes_input):result=manager.generate_chunk_audio(0)
            self.assertFalse(result[0]);self.assertIn('generation inputs',result[1]);self.assertEqual(snapshot,manager.load_chunks())
            manager.engine=SimpleNamespace(generate_voice=lambda *args:False);result=manager.generate_chunk_audio(0)
            self.assertEqual((False,'Generation returned False'),result);self.assertEqual('Generation returned False',manager.load_chunks()[0]['error'])

    def test_legacy_boolean_and_unknown_false_have_no_stale_failure(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);engine=tts.TTSEngine({'tts':{'mode':'local'}})
            self.assertIs(False,engine.generate_voice('words','','A',{},str(root/'out.wav')));self.assertIsNone(tts._TTS_FAILURES.get())
            with patch.object(engine,'generate_voice',return_value=False):result=self.get_result(engine,root)
            self.assertFalse(result.success);self.assertIsNone(result.failure)
