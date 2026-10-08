"""Per-row style identity must survive real batch ordering and publication."""
import copy
import contextlib
from peft import LoraConfig  # Load real CPU configuration support before scoped torch providers.
from tests.test_support import write_test_adapter
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType,SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import numpy as np
import soundfile as sf
import tts
from project import ProjectManager


def get_style_config(kind='custom',adapter=None):
    return {'A':{'type':kind,'voice':'Ryan','description':'Base identity','adapter_path':str(adapter or ''),
        'character_style':'young','seed':0,'style_timeline':[{'from_index':4,'character_style':'aged'},{'from_index':8,'character_style':'elder'}]},
        'B':{'type':kind,'voice':'Aiden','description':'Other identity','adapter_path':str(adapter or ''),
        'character_style':'steady','seed':7,'style_timeline':[{'from_index':6,'character_style':'tired'}]}}


def get_chunks():
    return [{'speaker':'A','text':'middle long line','instruct':'warm','index':7},
            {'speaker':'A','text':'early','instruct':'soft','index':2},
            {'speaker':'A','text':'last longish line','instruct':'firm','index':9},
            {'speaker':'B','text':'other','instruct':'sad','index':10}]


def get_engine():
    engine=tts.TTSEngine.__new__(tts.TTSEngine)
    engine._mode='local';engine._compile_codec_enabled=False
    engine._language='English';engine._max_new_tokens=100
    engine._clear_gpu_cache=Mock();engine.ensure_custom_warmup=Mock()
    engine._estimate_max_batch_size=Mock(return_value=2)
    engine._build_sub_batches=lambda texts,max_items:[(start,min(start+max_items,len(texts))) for start in range(0,len(texts),max_items)]
    return engine


def get_torch():
    module=ModuleType('torch');module.manual_seed=Mock()
    module.cuda=SimpleNamespace(is_available=lambda:False)
    return module


class BatchStyleTimelineTests(unittest.TestCase):
    def assert_outputs(self,root,indices):
        for index in indices:
            audio,rate=sf.read(Path(root)/f'temp_batch_{index}.wav')
            self.assertEqual((4000,16000),(len(audio),rate));self.assertAlmostEqual(0.1,float(audio.mean()),places=3)

    def test_native_custom_sorted_sub_batches_use_original_indices_and_project_publishes_audio(self):
        requests=[];samples=np.full(4000,0.1,dtype='float32')
        def render(**kwargs):
            requests.append(kwargs);return [samples.copy() for _ in kwargs['text']],16000
        engine=get_engine();engine._init_local_custom=Mock(return_value=SimpleNamespace(generate_custom_voice=render))
        config=get_style_config();chunks=get_chunks();before=copy.deepcopy((chunks,config))
        with tempfile.TemporaryDirectory() as tmp,patch.dict(sys.modules,{'torch':get_torch()}),contextlib.redirect_stdout(io.StringIO()):
            result=engine.generate_batch(chunks,config,tmp,batch_seed=0)
            self.assertEqual([],result['failed']);self.assertEqual({2,7,9,10},set(result['completed']))
            self.assert_outputs(tmp,[2,7,9,10])
        instructions={text.rstrip('.'):inst for request in requests for text,inst in zip(request['text'],request['instruct'])}
        self.assertEqual({'middle long line':'aged warm','early':'young soft','last longish line':'elder firm','other':'tired sad'},instructions)
        self.assertEqual(2,len(requests));self.assertTrue(all(len(request['text'])==2 for request in requests))
        self.assertEqual(before,(chunks,config))
        requests.clear()
        with tempfile.TemporaryDirectory() as tmp,patch.dict(sys.modules,{'torch':get_torch()}),contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);manager=ProjectManager(tmp)
            stored=[{'text':f'unused {i}','speaker':'A','instruct':'neutral','status':'pending'} for i in range(11)]
            for chunk in chunks:stored[chunk['index']].update({key:value for key,value in chunk.items() if key!='index'})
            manager.save_chunks(stored);Path(manager.voice_config_path).write_text(json.dumps(config));config_bytes=Path(manager.voice_config_path).read_bytes()
            with patch.object(manager,'get_engine',return_value=engine):
                result=manager.generate_chunks_batch([7,2,9,10],batch_size=4,batch_seed=0,batch_group_by_type=False)
            self.assertEqual([],result['failed']);self.assertEqual({2,7,9,10},set(result['completed']))
            saved=manager.load_chunks()
            for index in (7,2,9,10):
                self.assertEqual('done',saved[index]['status'])
                path=root/saved[index]['audio_path'];decoded,rate=sf.read(path)
                self.assertEqual((4000,16000),(len(decoded),rate))
            self.assertEqual(config_bytes,Path(manager.voice_config_path).read_bytes())
            instructions={text.rstrip('.'):inst for request in requests for text,inst in zip(request['text'],request['instruct'])}
            self.assertEqual({'middle long line':'aged warm','early':'young soft','last longish line':'elder firm','other':'tired sad'},instructions)

    def test_native_lora_shared_adapter_keeps_each_sorted_row_style_and_batching(self):
        requests=[];tokenized=[];samples=np.full(4000,0.1,dtype='float32')
        def tokenize(texts):tokenized.extend(texts);return texts
        def render(**kwargs):requests.append(kwargs);return [samples.copy() for _ in kwargs['text']],16000
        model=SimpleNamespace(_tokenize_texts=tokenize,generate_voice_clone=render)
        engine=get_engine();engine._init_local_lora=Mock(return_value=model)
        engine._ensure_lora_prompt=Mock(return_value=[SimpleNamespace(ref_code=None,ref_text='Reference words.')])
        with tempfile.TemporaryDirectory() as tmp,patch.dict(sys.modules,{'torch':get_torch()}),contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);adapter=root/'adapter';write_test_adapter(adapter)
            sf.write(adapter/'ref_sample.wav',samples,16000);(adapter/'training_meta.json').write_text('{"ref_sample_text":"Reference words."}')
            config=get_style_config('lora',adapter);chunks=get_chunks();before=copy.deepcopy((chunks,config))
            result=engine.generate_batch(chunks,config,tmp,batch_seed=0)
            self.assertEqual([],result['failed']);self.assert_outputs(tmp,[2,7,9,10])
            observed={text.rstrip('.'):prompt for request in requests for text,prompt in zip(request['text'],request['instruct_ids'])}
            self.assertEqual({'middle long line':'<|im_start|>user\nwarm aged<|im_end|>\n','early':'<|im_start|>user\nsoft young<|im_end|>\n','last longish line':'<|im_start|>user\nfirm elder<|im_end|>\n','other':'<|im_start|>user\nsad tired<|im_end|>\n'},observed)
            self.assertEqual(2,len(requests));engine._init_local_lora.assert_called_once();self.assertEqual(before,(chunks,config))

    def test_external_custom_predict_uses_the_same_per_row_styles_and_preserves_seed_zero(self):
        engine=get_engine();engine._mode='external';engine._external_parallel_workers=2;engine._external_urls=['fixture'];engine._external_timeout=5
        engine._next_external_url=lambda:'fixture';calls=[];lock=threading.Lock()
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp)
            def predict(**kwargs):
                calls.append(kwargs);path=root/(str(len(calls))+'.wav');sf.write(path,np.full(4000,0.1,dtype='float32'),16000);return (str(path),)
            engine._external_endpoint=lambda endpoint:(SimpleNamespace(predict=predict),lock)
            config=get_style_config();chunks=get_chunks();before=copy.deepcopy((chunks,config))
            result=engine.generate_batch(chunks,config,tmp)
            self.assertEqual([],result['failed']);self.assert_outputs(tmp,[2,7,9,10])
            self.assertEqual({'middle long line':'aged warm','early':'young soft','last longish line':'elder firm','other':'tired sad'},{call['text'].rstrip('.'):call['instruct'] for call in calls})
            self.assertEqual({0},{call['seed'] for call in calls if call['speaker']=='Ryan'})
            self.assertEqual(before,(chunks,config))

    def test_design_dynamic_narrator_and_ensemble_receive_actual_resolved_style_prompts(self):
        samples=np.full(4000,0.1,dtype='float32')
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()),patch.dict(sys.modules,{'torch':get_torch()}):
            root=Path(tmp);engine=get_engine();config=get_style_config('design');chunks=get_chunks();before=copy.deepcopy((chunks,config));design=[]
            def preview(description,sample_text,seed=-1):
                design.append((sample_text,description,seed));path=root/(str(len(design))+'_preview.wav');sf.write(path,samples,16000);return str(path),16000
            engine.generate_voice_design=preview
            result=engine.generate_batch(chunks,config,tmp)
            self.assertEqual([],result['failed']);self.assert_outputs(tmp,[2,7,9,10])
            self.assertEqual({'middle long line':'Base identity, aged warm','early':'Base identity, young soft','last longish line':'Base identity, elder firm','other':'Other identity, tired sad'},{text.rstrip('.'):desc for text,desc,seed in design})
            self.assertEqual({'middle long line':0,'early':0,'last longish line':0,'other':7},
                             {text.rstrip('.'):seed for text,desc,seed in design})
            self.assertEqual(before,(chunks,config))
            config=get_style_config();config['NARRATOR']={'type':'custom','voice':'Ryan','narrator_strategy':'focus'}
            calls=[]
            def custom(**kwargs):calls.append(kwargs);return [samples.copy()],16000
            engine._init_local_custom=lambda:SimpleNamespace(generate_custom_voice=custom)
            result=engine.generate_batch([{'speaker':'NARRATOR','focus_speaker':'A','text':'Narration','instruct':'warm','index':9}],config,tmp)
            self.assertEqual([],result['failed']);self.assertEqual('elder warm',calls[0]['instruct'])
            config=get_style_config();config['GROUP']={'type':'ensemble','members':['A','B']}
            calls.clear();snapshot=copy.deepcopy(config)
            result=engine.generate_batch([{'speaker':'GROUP','text':'Together','instruct':'firm','index':9}],config,tmp)
            self.assertEqual([],result['failed']);self.assertEqual(['elder firm','tired firm'],[call['instruct'] for call in calls])
            self.assertEqual(snapshot,config)
            audio,rate=sf.read(root/'temp_batch_9.wav');self.assertEqual((4000,16000),(len(audio),rate))

    def test_malformed_style_indices_raise_value_error_before_any_generation(self):
        for value in (None,[],{},'bad','',True,-1,1.5,float('inf')):
            with self.subTest(value=value):
                engine=get_engine();engine._local_batch_custom=Mock(side_effect=AssertionError('invalid timeline reached model work'))
                config=get_style_config();config['A']['style_timeline']=[{'from_index':value,'character_style':'bad'}];before=copy.deepcopy(config)
                with self.assertRaisesRegex(ValueError,'style_timeline.*from_index'):
                    tts.active_character_style(config['A'],2)
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaisesRegex(ValueError,'style_timeline.*from_index'):
                        engine.generate_batch(get_chunks(),config,tmp)
                    self.assertEqual([],list(Path(tmp).iterdir()))
                engine._local_batch_custom.assert_not_called();self.assertEqual(before,config)

    def test_external_custom_and_local_lora_keep_original_indices(self):
        samples=np.full(4000,0.1,dtype='float32');engine=get_engine();calls=[]
        engine._mode='external';engine._external_parallel_workers=2;engine._external_urls=['fixture'];engine._external_timeout=5
        engine._next_external_url=lambda:'fixture';lock=threading.Lock()
        with tempfile.TemporaryDirectory() as tmp,patch.dict(sys.modules,{'torch':get_torch()}),contextlib.redirect_stdout(io.StringIO()):
            source=Path(tmp)/'source.wav';sf.write(source,samples,16000)
            def predict(**kwargs):
                calls.append(kwargs);return (str(source),)
            engine._external_endpoint=lambda endpoint:(SimpleNamespace(predict=predict),lock)
            config=get_style_config();before=copy.deepcopy(config)
            result=engine.generate_batch(get_chunks(),config,tmp)
            self.assertEqual([],result['failed']);self.assert_outputs(tmp,[2,7,9,10])
            self.assertEqual({'middle long line':'aged warm','early':'young soft','last longish line':'elder firm','other':'tired sad'},
                {call['text'].rstrip('.'):call['instruct'] for call in calls})
            self.assertEqual(before,config)
            adapter=Path(tmp)/'adapter';write_test_adapter(adapter)
            sf.write(adapter/'ref_sample.wav',samples,16000)
            (adapter/'training_meta.json').write_text('{"ref_sample_text":"Reference words."}')
            lora_calls=[]
            def lora(**kwargs):
                lora_calls.append(kwargs);return [samples.copy() for _ in kwargs['text']],16000
            engine._mode='local'
            engine._init_local_lora=Mock(return_value=SimpleNamespace(_tokenize_texts=lambda texts:texts,generate_voice_clone=lora))
            engine._ensure_lora_prompt=Mock(return_value=[SimpleNamespace(ref_code=None,ref_text='Reference words.')])
            config=get_style_config('lora',adapter);before=copy.deepcopy(config)
            result=engine.generate_batch(get_chunks(),config,tmp)
            self.assertEqual([],result['failed']);self.assert_outputs(tmp,[2,7,9,10])
            self.assertEqual({'middle long line':'<|im_start|>user\nwarm aged<|im_end|>\n',
                'early':'<|im_start|>user\nsoft young<|im_end|>\n',
                'last longish line':'<|im_start|>user\nfirm elder<|im_end|>\n',
                'other':'<|im_start|>user\nsad tired<|im_end|>\n'},
                {text.rstrip('.'):instruct for call in lora_calls for text,instruct in zip(call['text'],call['instruct_ids'])})
            self.assertEqual(before,config)

    def test_timeline_compatibility_and_late_invalid_narrator_refuse_before_render(self):
        for value in (0,0.0,'0',' 0 '):
            self.assertEqual('changed',tts.active_character_style({'style_timeline':[{'from_index':value,'character_style':'changed'}]},0))
        self.assertEqual('changed',tts.active_character_style({'style_timeline':[{'character_style':'changed'}]},0))
        self.assertEqual('base',tts.active_character_style({'character_style':'base','style_timeline':[{'from_index':1,'character_style':'later'}]},0))
        config=get_style_config();config['NARRATOR']={'type':'custom','voice':'Ryan','narrator_strategy':'focus'}
        config['B']['style_timeline']=[{'from_index':None,'character_style':'bad'}]
        engine=get_engine();engine.generate_voice=Mock(side_effect=AssertionError('render before validation'))
        engine._local_batch_custom=Mock(side_effect=AssertionError('render before validation'))
        chunks=[{'speaker':'NARRATOR','focus_speaker':'A','text':'first','index':9},
                {'speaker':'NARRATOR','focus_speaker':'B','text':'bad later','index':10}]
        before=copy.deepcopy((chunks,config))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'style_timeline.*from_index'):
                engine.generate_batch(chunks,config,tmp)
            self.assertEqual([],list(Path(tmp).iterdir()))
        engine.generate_voice.assert_not_called();engine._local_batch_custom.assert_not_called()
        self.assertEqual(before,(chunks,config))
