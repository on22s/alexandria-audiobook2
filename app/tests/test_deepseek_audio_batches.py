import contextlib
import copy
import io
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import numpy as np
import soundfile as sf
import torch
import train_lora
import tts_vram_benchmark as vram
from tts import TTSEngine
from tests.test_training_oom_recovery import TrainingFixture

class AudioBatchTests(unittest.TestCase):
    def test_real_training_partial_and_oom_windows_match_full_mean_gradient(self):
        for modes in (['success','success'],['success'],['success','forward_oom']):
            with self.subTest(modes=modes),tempfile.TemporaryDirectory() as tmp:
                fixture=TrainingFixture(tmp,modes,grad_accum=2)
                fixture.loss=lambda *a,**k: 5+.02*fixture.talker.a+.03*fixture.talker.b
                with fixture.patches(): train_lora.train(fixture.args)
                self.assertEqual(1,len(fixture.steps))
                torch.testing.assert_close(torch.stack(fixture.steps[0]),torch.tensor([.02,.03]))
                self.assertTrue((fixture.output/'adapter_model.safetensors').exists())

    def test_clone_timeline_batches_use_the_effective_reference(self):
        config={'S':{'type':'clone','ref_audio':'base.wav','ref_text':'base',
            'version_timeline':[{'from_index':2,'version_id':'v2'}],
            'versions':{'v2':{'ref_audio':'new.wav','ref_text':'new'}}}}
        original=copy.deepcopy(config)
        engine=TTSEngine({'tts':{'mode':'local'}});seen=[]
        def prompt(speaker,selected):
            seen.append((speaker,selected[speaker]['ref_audio'],selected[speaker]['ref_text']))
            raise RuntimeError('capture before inference')
        chunks=[{'index':i,'speaker':'S','text':'hello','instruct':''} for i in (0,2,3)]
        with tempfile.TemporaryDirectory() as tmp,patch.object(engine,'_init_local_clone',return_value=None),patch.object(engine,'_get_clone_prompt',side_effect=prompt):
            result=engine.generate_batch(chunks,config,tmp)
        self.assertEqual([('S','base.wav','base'),('S','new.wav','new')],seen)
        self.assertEqual([0,2,3],[i for i,_ in result['failed']])
        self.assertEqual(original,config)

    def test_dynamic_narrator_batch_seed_override_and_disabled_control(self):
        config={'NARRATOR':{'type':'custom','voice':'Ryan','narrator_strategy':'focus'},
                'HERO':{'type':'custom','voice':'Ryan','seed':91}}
        original=copy.deepcopy(config)
        for seed,expected in ((42,42),(-1,91)):
            engine=TTSEngine({'tts':{'mode':'local'}});seen=[]
            def render(text,instruct,name,selected,path):
                seen.append(selected[name]['seed']);sf.write(path,np.full(240,.2),24000);return True
            with tempfile.TemporaryDirectory() as tmp,patch.object(engine,'generate_voice',side_effect=render):
                result=engine.generate_batch([{'index':0,'text':'hi','speaker':'NARRATOR','focus_speaker':'HERO'}],config,tmp,batch_seed=seed)
                self.assertTrue(Path(tmp,'temp_batch_0.wav').exists())
            self.assertEqual([expected],seen);self.assertEqual([0],result['completed'])
            self.assertEqual(original,config)

    def test_malformed_timeline_fails_one_chunk_without_base_fallback(self):
        config={'BAD':{'type':'custom','voice':'Ryan','style_timeline':[{'from_index':'bad','character_style':'quiet'}]},
                'GOOD':{'type':'custom','voice':'Ryan'}}
        engine=TTSEngine({'tts':{'mode':'local'}})
        with tempfile.TemporaryDirectory() as tmp,patch.object(engine,'_local_batch_custom',return_value={'completed':[1],'failed':[]}) as worker:
            result=engine.generate_batch([{'index':0,'speaker':'BAD','text':'one'},{'index':1,'speaker':'GOOD','text':'two'}],config,tmp)
        self.assertEqual([1],result['completed']);self.assertEqual(0,result['failed'][0][0]);self.assertIn('from_index',result['failed'][0][1])
        self.assertEqual([1],[c['index'] for c in worker.call_args.args[0]])

    def test_vram_recommendations_show_compiled_and_uncompiled_measurements(self):
        baseline=[{'sub_batch_max_items':4,'peak_vram_gb':12.,'rtf':1.,'failed':0}]
        for peak,risk in ((20.,True),(13.,False)):
            output=io.StringIO()
            with patch.object(vram,'gpu_name',return_value='fixture'),contextlib.redirect_stdout(output):
                vram.print_summary(baseline,[{**baseline[0],'peak_vram_gb':peak}],10.,16.)
            text=output.getvalue().split('Tier table recommendation')[-1]
            self.assertIn('uncompiled',text);self.assertIn('compiled',text)
            self.assertIn(f'peak={peak:.2f}GB',text);self.assertEqual(risk,'OOM-RISK' in text)
        output=io.StringIO()
        with patch.object(vram,'gpu_name',return_value='fixture'),contextlib.redirect_stdout(output): vram.print_summary(baseline,None,10.,16.)
        self.assertNotIn('OOM-RISK',output.getvalue())
