"""Serving-generation cache identity with real CPU PEFT merges and PCM artifacts."""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
import torch
import peft.tuners.lora.model as peft_lora_model
import tts
from adapter_checkpoint_transaction import save_adapter_checkpoint, get_adapter_generation_sha256
from lora_evidence import get_file_sha256
from tests.test_support import write_test_adapter


def _write(path,value=1):
    path=Path(path);write_test_adapter(path,value=value)
    sf.write(path/'ref_sample.wav',np.full(2400,.05*value,dtype=np.float32),24000)
    (path/'training_meta.json').write_text(json.dumps({'ref_sample_text':f'reference generation {value}',
        'checkpoint_sha256':get_file_sha256(str(path/'adapter_model.safetensors')),
        'reference_audio_sha256':get_file_sha256(str(path/'ref_sample.wav'))}))


class BaseTalker(torch.nn.Module):
    def __init__(self):
        super().__init__();self.layer=torch.nn.Module();self.layer.q_proj=torch.nn.Linear(3,4,bias=False)
        with torch.no_grad():self.layer.q_proj.weight.fill_(.25)
        self.config={'tie_word_embeddings':False}


class LoraGenerationCacheTests(unittest.TestCase):
    @contextlib.contextmanager
    def providers(self,engine,loads,prompts,load_gate=None):
        def load(*args):
            if load_gate:load_gate()
            model=SimpleNamespace(model=SimpleNamespace(talker=BaseTalker()))
            def prompt(**kwargs):
                prompts.append((kwargs['ref_text'],float(np.mean(kwargs['ref_audio'][0]))))
                return [SimpleNamespace(ref_code=None,ref_text=kwargs['ref_text'])]
            def generate(**kwargs):
                weight=float(model.model.talker.layer.q_proj.weight.detach().mean())
                count=len(kwargs['text']) if isinstance(kwargs['text'],list) else 1
                return [np.full(2400,weight/100,dtype=np.float32) for _ in range(count)],24000
            model.create_voice_clone_prompt=prompt;model.generate_voice_clone=generate;model._tokenize_texts=lambda texts:texts
            loads.append(model);return model
        with patch.dict(sys.modules,{'qwen_tts':SimpleNamespace(Qwen3TTSModel=object())}),patch.object(engine,'_load_model',side_effect=load),patch.object(engine,'_resolve_device',return_value='cpu'),patch.object(engine,'_enable_rocm_optimizations'),patch.object(engine,'_clear_gpu_cache'),patch.object(torch.cuda,'is_available',return_value=False),patch.object(peft_lora_model,'is_bnb_available',return_value=False),patch.object(peft_lora_model,'is_bnb_4bit_available',return_value=False):
            yield

    def test_same_path_publication_reloads_weights_and_prompt_but_unchanged_generation_reuses_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);adapter=root/'voice';_write(adapter);engine=tts.TTSEngine({'tts':{'mode':'local'}});loads=[];prompts=[];config={'adapter_path':str(adapter),'seed':0}
            with self.providers(engine,loads,prompts):
                self.assertTrue(engine.generate_lora_voice('known line','',config,str(root/'first.wav')))
                self.assertTrue(engine.generate_lora_voice('known line','',config,str(root/'same.wav')))
                self.assertEqual(1,len(loads));self.assertEqual(1,len(prompts));self.assertEqual((root/'first.wav').read_bytes(),(root/'same.wav').read_bytes())
                first=get_adapter_generation_sha256(adapter);save_adapter_checkpoint(adapter,lambda stage:_write(stage,2));self.assertNotEqual(first,get_adapter_generation_sha256(adapter))
                self.assertTrue(engine.generate_lora_voice('known line','',config,str(root/'new.wav')))
                self.assertTrue((root/'first.wav').read_bytes() != (root/'new.wav').read_bytes(),'unchanged-path publication left the rendered WAV byte-identical')
                self.assertEqual(2,len(loads));self.assertEqual(2,len(prompts));self.assertEqual(['reference generation 1','reference generation 2'],[row[0] for row in prompts]);self.assertAlmostEqual(.05,prompts[0][1],places=3);self.assertAlmostEqual(.1,prompts[1][1],places=3)
                a,rate=sf.read(root/'first.wav');b,rate2=sf.read(root/'new.wav');self.assertEqual((24000,24000),(rate,rate2));np.testing.assert_allclose(a,.0425,atol=4e-5);np.testing.assert_allclose(b,.1625,atol=4e-5)
                self.assertNotEqual((root/'first.wav').read_bytes(),(root/'new.wav').read_bytes())

    def test_batch_uses_the_same_generation_reader_and_rejects_mismatched_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);adapter=root/'voice';_write(adapter);engine=tts.TTSEngine({'tts':{'mode':'local'}});loads=[];prompts=[]
            for name in ('first','second','rejected'):(root/name).mkdir()
            chunks=[{'index':7,'speaker':'ANN','text':'known line'}];config={'ANN':{'type':'lora','adapter_path':str(adapter),'seed':0}}
            with self.providers(engine,loads,prompts),patch.object(engine,'_estimate_max_batch_size',return_value=1),patch.object(engine,'_build_sub_batches',return_value=[(0,1)]):
                first=engine.generate_batch(chunks,config,str(root/'first'));self.assertEqual([],first['failed']);self.assertEqual(1,len(loads))
                save_adapter_checkpoint(adapter,lambda stage:_write(stage,2))
                second=engine.generate_batch(chunks,config,str(root/'second'));self.assertEqual([],second['failed']);self.assertEqual(2,len(loads));self.assertEqual(2,len(prompts))
                meta=json.loads((adapter/'training_meta.json').read_text());meta['checkpoint_sha256']='0'*64;(adapter/'training_meta.json').write_text(json.dumps(meta))
                rejected=engine.generate_batch(chunks,config,str(root/'rejected'));self.assertEqual(7,rejected['failed'][0][0]);self.assertIn('metadata does not match',rejected['failed'][0][1]);self.assertEqual(2,len(loads));self.assertFalse((root/'rejected'/'7.wav').exists())

    def test_publication_proceeds_during_slow_model_loading_but_reader_keeps_its_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);adapter=root/'voice';_write(adapter);engine=tts.TTSEngine({'tts':{'mode':'local'}});loads=[];prompts=[];arrived=threading.Event();release=threading.Event();published=threading.Event();errors=[];result=[]
            def gate():arrived.set();self.assertTrue(release.wait(5))
            def read():
                try:result.append(engine.generate_lora_voice('known line','',{'adapter_path':str(adapter)},str(root/'old.wav')))
                except Exception as error:errors.append(error)
            def publish():
                try:save_adapter_checkpoint(adapter,lambda stage:_write(stage,2));published.set()
                except Exception as error:errors.append(error)
            with self.providers(engine,loads,prompts,gate):
                reader=threading.Thread(target=read);reader.start();self.assertTrue(arrived.wait(5));writer=threading.Thread(target=publish);writer.start()
                try:
                    self.assertTrue(published.wait(5));self.assertTrue(reader.is_alive())
                finally:release.set();reader.join(5);writer.join(5)
                self.assertFalse(reader.is_alive());self.assertFalse(writer.is_alive());self.assertEqual([],errors);self.assertEqual([True],result);self.assertTrue(published.is_set());self.assertEqual('reference generation 1',prompts[0][0]);pcm,rate=sf.read(root/'old.wav');np.testing.assert_allclose(pcm,.0425,atol=4e-5)

    def test_pinned_generation_refuses_new_publication_and_loading_mutation_does_not_leave_a_reusable_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);adapter=root/'voice';_write(adapter);engine=tts.TTSEngine({'tts':{'mode':'local'}});loads=[];prompts=[];original=get_adapter_generation_sha256(adapter)
            save_adapter_checkpoint(adapter,lambda stage:_write(stage,2))
            with self.providers(engine,loads,prompts):
                self.assertFalse(engine.generate_lora_voice('known line','',{'adapter_path':str(adapter),'adapter_generation_sha256':original},str(root/'wrong.wav')));self.assertEqual([],loads);self.assertFalse((root/'wrong.wav').exists())
            original_load=engine._init_local_lora
            def mutate(snapshot,**kwargs):
                meta=json.loads((Path(snapshot)/'training_meta.json').read_text());meta['ref_sample_text']='provider changed private metadata during load';(Path(snapshot)/'training_meta.json').write_text(json.dumps(meta))
                return original_load(snapshot,**kwargs)
            with self.providers(engine,loads,prompts),patch.object(engine,'_init_local_lora',side_effect=mutate):
                self.assertFalse(engine.generate_lora_voice('known line','',{'adapter_path':str(adapter)},str(root/'mixed.wav')));self.assertIsNone(engine._lora_generation_sha256);self.assertFalse((root/'mixed.wav').exists())
            with self.providers(engine,loads,prompts):
                self.assertTrue(engine.generate_lora_voice('known line','',{'adapter_path':str(adapter)},str(root/'retry.wav')));self.assertEqual(2,len(loads));self.assertEqual('reference generation 2',prompts[-1][0])

    def test_pending_publication_blocks_a_warm_cache_and_malformed_declared_hash_is_not_legacy(self):
        from adapter_publication import CLI_PUBLICATION_JOURNAL,NAMING_PUBLICATION_JOURNAL
        from adapter_checkpoint_transaction import get_adapter_checkpoint_journal
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);adapter=root/'voice';_write(adapter);engine=tts.TTSEngine({'tts':{'mode':'local'}});loads=[];prompts=[]
            with self.providers(engine,loads,prompts):
                self.assertTrue(engine.generate_lora_voice('known line','',{'adapter_path':str(adapter)},str(root/'warm.wav')))
                for marker in (root/CLI_PUBLICATION_JOURNAL,root/NAMING_PUBLICATION_JOURNAL,adapter/'.checkpoint_swap.json',get_adapter_checkpoint_journal(adapter)):
                    with self.subTest(marker=marker.name):
                        marker.write_text('{broken')
                        try:
                            self.assertFalse(engine.generate_lora_voice('known line','',{'adapter_path':str(adapter)},str(root/'blocked.wav')))
                            self.assertFalse((root/'blocked.wav').exists());self.assertEqual(1,len(loads))
                        finally:marker.unlink()
                meta=json.loads((adapter/'training_meta.json').read_text());meta.pop('checkpoint_sha256');meta.pop('reference_audio_sha256');(adapter/'training_meta.json').write_text(json.dumps(meta))
                self.assertTrue(engine.generate_lora_voice('known line','',{'adapter_path':str(adapter)},str(root/'legacy.wav')))
                meta['checkpoint_sha256']='';(adapter/'training_meta.json').write_text(json.dumps(meta))
                self.assertFalse(engine.generate_lora_voice('known line','',{'adapter_path':str(adapter)},str(root/'invalid.wav')));self.assertFalse((root/'invalid.wav').exists());self.assertEqual(2,len(loads))

    def test_native_rename_after_path_lookup_keeps_saved_single_and_batch_voices_renderable(self):
        import copy
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        from voice_manifest import get_resolved_adapter_path
        for mode in ('single','batch'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
                _write(models/'raw_a');old=models/'raw_a'
                config={'ANN':{'type':'lora','adapter_path':str(old),'seed':0}};before=copy.deepcopy(config)
                engine=tts.TTSEngine({'tts':{'mode':'local'}});loads=[];prompts=[];resolved=[]
                def rename_after_lookup(path):
                    result=get_resolved_adapter_path(path)
                    self.assertEqual(0,_invoke(models,'--apply'))
                    self.assertFalse(Path(result).exists())
                    resolved.append(result)
                    return result
                output=root/'out';output.mkdir()
                with self.providers(engine,loads,prompts),patch.object(tts,'get_resolved_adapter_path',side_effect=rename_after_lookup),patch.object(engine,'_estimate_max_batch_size',return_value=1),patch.object(engine,'_build_sub_batches',return_value=[(0,1)]):
                    if mode=='single':
                        path=output/'single.wav'
                        self.assertTrue(engine.generate_lora_voice('known line','',config['ANN'],str(path)))
                    else:
                        result=engine.generate_batch([{'index':7,'speaker':'ANN','text':'known line'}],config,str(output))
                        self.assertEqual([],result['failed']);self.assertEqual([7],result['completed'])
                        path=output/'temp_batch_7.wav'
                self.assertEqual([str(old)],resolved)
                self.assertEqual(before,config)
                self.assertEqual(1,len(loads));self.assertEqual('reference generation 1',prompts[0][0])
                audio,rate=sf.read(path);self.assertEqual(24000,rate)
                np.testing.assert_allclose(audio,.0425,atol=4e-5)
