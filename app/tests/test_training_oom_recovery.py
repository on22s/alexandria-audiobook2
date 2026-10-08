"""Real CPU gradients, failed graph lifetimes and actual training publication."""
import contextlib
import copy
import gc
import hashlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import numpy as np
import soundfile as sf
import torch
import torch._dynamo  # Load native registrations before scoped provider module patches.
from safetensors.torch import save_file,load_file
import train_lora
from peft import LoraConfig, PeftConfig

class TrainingFixture:
    """Tiny CPU model and provider replacements; train() itself is real."""
    def __init__(self,root,modes,epochs=1,target_loss=None,grad_accum=8,device='cpu',sub_weight=0):
        self.root=Path(root);self.output=self.root/'output';self.data=self.root/'data';self.data.mkdir();self.output.mkdir()
        self.ref=self.data/'ref.wav';sf.write(self.ref,np.full(1600,0.1,dtype='float32'),16000)
        self.samples=[{'text':f'Sample {i}.','audio_path':str(self.ref),'mode':mode} for i,mode in enumerate(modes)]
        (self.data/'metadata.jsonl').write_text(''.join(json.dumps({'audio':'ref.wav','text':s['text']})+'\n' for s in self.samples))
        self.before=copy.deepcopy(self.samples);self.mode='success';self.device=device;self.failed_refs=[];self.all_refs=[];self.reclaimed=[];self.steps=[];self.saved=[];self.sub_weight=sub_weight
        fixture=self
        class Transformer:
            def gradient_checkpointing_enable(self):pass
            def __call__(self,inputs_embeds,use_cache):
                hidden=inputs_embeds+fixture.talker.a+fixture.talker.b
                fixture.track(hidden)
                if fixture.mode=='forward_oom':raise RuntimeError('CUDA out of memory: CPU forward fixture')
                if fixture.mode=='other_error':raise RuntimeError('provider configuration invalid')
                return SimpleNamespace(last_hidden_state=hidden)
        class Talker(torch.nn.Module):
            def __init__(self):
                super().__init__();self.a=torch.nn.Parameter(torch.tensor(0.1));self.b=torch.nn.Parameter(torch.tensor(0.2));self.model=Transformer()
            def codec_head(self,hidden):return torch.stack((hidden[...,0],-hidden[...,1]),dim=-1)
            def forward_sub_talker_finetune(self,codes,hidden):return None,fixture.sub_weight*(4*self.a+6*self.b)
        self.talker=Talker()
        def fail_backward(gradient):
            if fixture.mode=='backward_oom':raise RuntimeError('CUDA out of memory: CPU backward hook')
            return gradient
        self.talker.a.register_hook(fail_backward)
        class Adapter(torch.nn.Module):
            def __init__(self):
                super().__init__();self.talker=fixture.talker;self.base_model=SimpleNamespace(model=fixture.talker)
            def enable_input_require_grads(self):pass
            def save_pretrained(self,path):
                path=Path(path);path.mkdir(parents=True,exist_ok=True);fixture.saved.append(str(path))
                save_file({name:parameter.detach().clone() for name,parameter in fixture.talker.named_parameters()},str(path/'adapter_model.safetensors'))
                LoraConfig(r=2,lora_alpha=4,target_modules=['q_proj']).save_pretrained(path)
        self.adapter=Adapter();self.hf=SimpleNamespace(talker=self.talker)
        self.args=SimpleNamespace(data_dir=str(self.data),output_dir=str(self.output),model_name='private CPU fixture',epochs=epochs,lr=0.01,
            lora_r=2,lora_alpha=4,gradient_accumulation_steps=grad_accum,batch_size=1,device=device,language='english',max_audio_seconds=30,
            target_loss=target_loss,seed=None,candidate_checkpoints=2)
        self.logs=io.StringIO();self.original_adamw=torch.optim.AdamW
    def track(self,tensor):
        self.all_refs.append(weakref.ref(tensor))
        if self.mode.endswith('_oom'):self.failed_refs.append(weakref.ref(tensor))
    def build(self,sample,*args,**kwargs):
        self.mode=sample['mode'];full=torch.ones((1,4,2));self.track(full)
        labels=torch.tensor([[-100,0,1,0]]);codes=torch.zeros((2,2),dtype=torch.long)
        return full,labels,codes,2
    def loss(self,*args,**kwargs):
        a,b=(7,11) if self.mode=='backward_oom' else (2,3)
        return 5+a*self.talker.a+b*self.talker.b
    def optimizer(self,parameters,**kwargs):
        optimizer=self.original_adamw(parameters,**kwargs);step=optimizer.step
        def record_step(*args,**kwargs):
            self.steps.append([parameter.grad.detach().clone() if parameter.grad is not None else None for parameter in self.talker.parameters()]);return step(*args,**kwargs)
        optimizer.step=record_step;return optimizer
    def reclaim(self,phase):
        self.reclaimed.append((phase,sum(ref() is not None for ref in self.failed_refs)))
    @contextlib.contextmanager
    def patches(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules,{'qwen_tts':SimpleNamespace(Qwen3TTSModel=SimpleNamespace(from_pretrained=lambda *a,**k:SimpleNamespace(processor=object(),model=self.hf))),
                'peft':SimpleNamespace(LoraConfig=LoraConfig,PeftConfig=PeftConfig,get_peft_model=lambda *args:self.adapter)}))
            stack.enter_context(patch.object(train_lora,'resolve_device',return_value=self.device));stack.enter_context(patch.object(train_lora,'enable_rocm_optimizations'))
            stack.enter_context(patch.object(train_lora,'load_dataset',return_value=(self.samples,str(self.ref))))
            stack.enter_context(patch.object(train_lora,'build_teacher_forcing_input',side_effect=self.build))
            stack.enter_context(patch.object(torch.nn.functional,'cross_entropy',side_effect=self.loss))
            stack.enter_context(patch.object(torch.optim,'AdamW',side_effect=self.optimizer))
            stack.enter_context(patch.object(random,'shuffle'))
            stack.enter_context(patch.object(train_lora,'get_code_lineage',return_value={'code_commit':'CPU fixture','code_dirty':False}))
            stack.enter_context(patch.object(torch.cuda,'is_available',return_value=False))
            stack.enter_context(patch.object(torch.cuda,'is_current_stream_capturing',return_value=False))
            stack.enter_context(patch.object(torch.cuda,'empty_cache',side_effect=lambda:self.reclaim('cuda')))
            original_collect=gc.collect
            def collect():self.reclaim('gc');return original_collect()
            stack.enter_context(patch.object(gc,'collect',side_effect=collect));stack.enter_context(contextlib.redirect_stdout(self.logs))
            yield

class TrainingOOMLoopTests(unittest.TestCase):
    def test_less_than_half_success_refuses_all_adapter_and_metadata_publication(self):
        for modes in (['success','forward_oom','forward_oom'],['success','forward_oom','success','forward_oom','forward_oom','forward_oom']):
            with self.subTest(modes=modes),tempfile.TemporaryDirectory() as tmp:
                fixture=TrainingFixture(tmp,modes);prior={name:b'prior adapter bytes' for name in ('adapter_model.safetensors','training_meta.json')}
                for name,data in prior.items():(fixture.output/name).write_bytes(data)
                with fixture.patches():
                    with self.assertRaisesRegex(RuntimeError,'fewer than half.*requested=.*succeeded=.*skipped='):train_lora.train(fixture.args)
                self.assertEqual([],fixture.saved);self.assertEqual(fixture.before,fixture.samples)
                for name,data in prior.items():self.assertEqual(data,(fixture.output/name).read_bytes())
                self.assertFalse((fixture.output/'candidates').exists())
    def test_existing_zero_success_and_consecutive_oom_caps_stay_fail_loud(self):
        for modes,message in ((['forward_oom'],'zero successful'),(['forward_oom']*11,'11 consecutive OOM-like')):
            with self.subTest(message=message),tempfile.TemporaryDirectory() as tmp:
                fixture=TrainingFixture(tmp,modes)
                with fixture.patches():
                    with self.assertRaisesRegex(RuntimeError,message):train_lora.train(fixture.args)
                self.assertEqual([],fixture.saved);self.assertEqual([],list(fixture.output.iterdir()));self.assertEqual(fixture.before,fixture.samples)
    def test_failed_backward_contributes_nothing_and_partial_window_is_flushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,['success','backward_oom','success','forward_oom'])
            with fixture.patches():train_lora.train(fixture.args)
            self.assertEqual(1,len(fixture.steps));torch.testing.assert_close(torch.stack(fixture.steps[0]),torch.tensor([2.0,3.0]) / torch.tensor([2.0,3.0]).norm())
            self.assertEqual(fixture.before,fixture.samples);meta=json.loads((fixture.output/'training_meta.json').read_text())
            self.assertEqual(2,meta['oom_skips']);self.assertEqual(4,meta['num_samples']);self.assertEqual(1,meta['epochs'])
            self.assertEqual(hashlib.sha256((fixture.output/'adapter_model.safetensors').read_bytes()).hexdigest(),meta['checkpoint_sha256'])
            self.assertEqual(fixture.ref.read_bytes(),(fixture.output/'ref_sample.wav').read_bytes());self.assertTrue(all(torch.isfinite(value).all() for value in load_file(str(fixture.output/'adapter_model.safetensors')).values()))
    def test_failed_intermediates_are_dead_before_cache_and_gc_reclamation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,['success','forward_oom','success','backward_oom'],device='cuda')
            with fixture.patches():train_lora.train(fixture.args)
            self.assertTrue(fixture.failed_refs);self.assertTrue(fixture.reclaimed);self.assertEqual({'cuda','gc'},{phase for phase,_ in fixture.reclaimed})
            self.assertTrue(all(alive==0 for _,alive in fixture.reclaimed),fixture.reclaimed)
    def test_boundary_coverage_normal_training_and_actual_early_stop_metadata(self):
        for modes,epochs,target,actual in ((['success','forward_oom'],1,None,1),(['success','success'],2,None,2),(['success'],5,6.0,1)):
            with self.subTest(modes=modes,epochs=epochs),tempfile.TemporaryDirectory() as tmp:
                fixture=TrainingFixture(tmp,modes,epochs=epochs,target_loss=target)
                with fixture.patches():train_lora.train(fixture.args)
                meta=json.loads((fixture.output/'training_meta.json').read_text());self.assertEqual(actual,meta['epochs']);self.assertEqual(epochs,meta.get('requested_epochs'))
                self.assertIn(f'requested={len(modes)} succeeded={modes.count("success")} skipped={len(modes)-modes.count("success")}',fixture.logs.getvalue())
                self.assertEqual(fixture.before,fixture.samples);self.assertTrue(load_file(str(fixture.output/'adapter_model.safetensors')))
    def test_non_oom_error_aborts_without_checkpoint_or_reclamation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,['other_error'])
            with fixture.patches():
                with self.assertRaisesRegex(RuntimeError,'configuration invalid'):train_lora.train(fixture.args)
            self.assertEqual([],fixture.saved);self.assertEqual([],fixture.reclaimed);self.assertEqual([],list(fixture.output.iterdir()))


    def test_interrupted_second_checkpoint_preserves_complete_first_safe_generation(self):
        from adapter_checkpoint_transaction import validate_adapter_checkpoint_generation
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,['success'],epochs=2);original=fixture.adapter.save_pretrained;calls=0;published={}
            def interrupted(path):
                nonlocal calls
                calls+=1
                if calls==3:
                    self.assertTrue((fixture.output/'training_meta.json').exists(),'safe epoch must publish metadata before finalization')
                    validate_adapter_checkpoint_generation(fixture.output)
                    validate_adapter_checkpoint_generation(fixture.output/'candidates'/'epoch_001')
                    published.update({str(p.relative_to(fixture.output)):p.read_bytes() for p in fixture.output.rglob('*') if p.is_file()})
                    (Path(path)/'adapter_model.safetensors').write_bytes(b'interrupted replacement weights')
                    raise RuntimeError('checkpoint interrupted after partial write')
                return original(path)
            with fixture.patches(),patch.object(fixture.adapter,'save_pretrained',side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError,'checkpoint interrupted'):train_lora.train(fixture.args)
            self.assertEqual(3,calls);self.assertTrue(published)
            self.assertEqual(published,{str(p.relative_to(fixture.output)):p.read_bytes() for p in fixture.output.rglob('*') if p.is_file()})
            meta=validate_adapter_checkpoint_generation(fixture.output);self.assertEqual(1,meta['epochs']);self.assertEqual(2,meta['requested_epochs'])
            self.assertEqual(fixture.ref.read_bytes(),(fixture.output/'ref_sample.wav').read_bytes())

    def test_final_regression_keeps_best_weights_while_publishing_final_run_metadata(self):
        from adapter_checkpoint_transaction import validate_adapter_checkpoint_generation
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,['success'],epochs=2);loss_calls=0;first_weights=None
            def loss(*args,**kwargs):
                nonlocal loss_calls,first_weights
                loss_calls+=1
                if loss_calls==2:
                    first_weights=(fixture.output/'adapter_model.safetensors').read_bytes()
                    self.assertEqual(1,validate_adapter_checkpoint_generation(fixture.output)['epochs'])
                return (5 if loss_calls==1 else 10)+2*fixture.talker.a+3*fixture.talker.b
            fixture.loss=loss
            with fixture.patches():train_lora.train(fixture.args)
            self.assertEqual(2,loss_calls);self.assertIsNotNone(first_weights);self.assertEqual(first_weights,(fixture.output/'adapter_model.safetensors').read_bytes())
            meta=validate_adapter_checkpoint_generation(fixture.output);self.assertEqual(2,meta['epochs']);self.assertGreater(meta['final_loss'],meta['best_loss'])


class TrainingSampleIsolationTests(unittest.TestCase):
    def set_prior(self,fixture):
        fixture.talker.a.grad=torch.tensor(2.0);fixture.talker.b.grad=torch.tensor(3.0)
        return [parameter.grad for parameter in fixture.talker.parameters()]
    def assert_prior(self,fixture,prior):
        for parameter,gradient,value in zip(fixture.talker.parameters(),prior,(2.0,3.0)):
            self.assertIs(gradient,parameter.grad);self.assertEqual(value,gradient.item())
    def run_sample(self,fixture,mode='success',parameters=None,accum=1):
        return train_lora.run_training_sample({'mode':mode},fixture.hf,fixture.talker,fixture.talker.model,
            tuple(parameters or fixture.talker.parameters()),'cpu',torch.float32,gradient_accumulation_steps=accum)
    def test_partial_backward_restores_references_and_next_optimizer_update_excludes_failed_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,['success']);prior=self.set_prior(fixture);attempt=[]
            fixture.talker.b.register_post_accumulate_grad_hook(lambda parameter:attempt.append(parameter.grad.item()))
            with fixture.patches():
                result=self.run_sample(fixture,'backward_oom');self.assertEqual([11.0],attempt);self.assertIsInstance(result['oom_error'],str)
                self.assert_prior(fixture,prior);self.assertTrue(all(ref() is None for ref in fixture.failed_refs))
                success=self.run_sample(fixture);self.assertNotIn('oom_error',success)
                torch.testing.assert_close(torch.stack([parameter.grad for parameter in fixture.talker.parameters()]),torch.tensor([4.0,6.0]))
                optimizer=torch.optim.SGD(fixture.talker.parameters(),lr=0.1);optimizer.step()
            torch.testing.assert_close(torch.stack([parameter.detach() for parameter in fixture.talker.parameters()]),torch.tensor([-0.3,-0.4]))
            self.assertEqual([2.0,3.0],[gradient.item() for gradient in prior])
    def test_failure_before_backward_restores_exact_previous_gradients(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,['success']);prior=self.set_prior(fixture)
            with fixture.patches():
                result=self.run_sample(fixture,'forward_oom');self.assertIn('oom_error',result);self.assert_prior(fixture,prior)
                self.assertTrue(all(ref() is None for ref in fixture.failed_refs))
    def test_loss_read_failure_after_backward_is_not_committed_and_non_oom_errors_propagate(self):
        for error_type,message in ((RuntimeError,'CUDA out of memory: loss read'),(RuntimeError,'loss read configuration invalid'),(ValueError,'bad loss scalar')):
            with self.subTest(message=message),tempfile.TemporaryDirectory() as tmp:
                fixture=TrainingFixture(tmp,['success']);prior=self.set_prior(fixture);reads=[];original_item=torch.Tensor.item
                def item(tensor,*args,**kwargs):
                    reads.append(True)
                    if len(reads)==3:raise error_type(message)
                    return original_item(tensor,*args,**kwargs)
                with fixture.patches(),patch.object(torch.Tensor,'item',item):
                    if 'out of memory' in message:self.assertIn('oom_error',self.run_sample(fixture))
                    else:
                        with self.assertRaisesRegex(error_type,message):self.run_sample(fixture)
                self.assert_prior(fixture,prior);self.assertEqual(3,len(reads));self.assertTrue(all(ref() is None for ref in fixture.all_refs))
    def test_failed_merge_cannot_mutate_prior_gradients_even_after_first_attempt_is_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,['success']);prior=self.set_prior(fixture);adds=[];original_add=torch.Tensor.add_
            def add(tensor,*args,**kwargs):
                adds.append(True)
                if len(adds)==2:raise RuntimeError('CUDA out of memory: merge fixture')
                return original_add(tensor,*args,**kwargs)
            with fixture.patches(),patch.object(torch.Tensor,'add_',add):self.assertIn('oom_error',self.run_sample(fixture))
            self.assertEqual(2,len(adds));self.assert_prior(fixture,prior)
    def test_success_preserves_scaling_sub_loss_weight_and_unused_prior_gradient(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,['success'],sub_weight=1);prior=self.set_prior(fixture)
            unused=torch.nn.Parameter(torch.tensor(0.3));unused.grad=torch.tensor(13.0);old_unused=unused.grad
            with fixture.patches():result=self.run_sample(fixture,parameters=(*fixture.talker.parameters(),unused),accum=2)
            torch.testing.assert_close(torch.stack([parameter.grad for parameter in fixture.talker.parameters()]),torch.tensor([3.6,5.4]))
            self.assertIs(old_unused,unused.grad);self.assertEqual(13.0,unused.grad.item());self.assertEqual([2.0,3.0],[gradient.item() for gradient in prior])
            self.assertAlmostEqual(6.28,result['loss'],places=5);self.assertAlmostEqual(5.8,result['talker_loss'],places=5);self.assertAlmostEqual(1.6,result['sub_loss'],places=5)
            self.assertTrue(all(isinstance(value,float) for value in result.values()))
    def test_cpu_allocation_records_one_extra_trainable_gradient_set(self):
        a=torch.nn.Parameter(torch.ones(1024));b=torch.nn.Parameter(torch.ones(2048));parameters=(a,b)
        a.grad=torch.full_like(a,2);b.grad=torch.full_like(b,3);prior=(a.grad,b.grad);measurements=[];original_item=torch.Tensor.item
        def item(tensor,*args,**kwargs):
            measurements.append((sum(gradient.numel()*gradient.element_size() for gradient in prior),
                sum(parameter.grad.numel()*parameter.grad.element_size() for parameter in parameters if parameter.grad is not None)))
            return original_item(tensor,*args,**kwargs)
        base=SimpleNamespace(codec_head=lambda hidden:torch.zeros((1,4,2)),forward_sub_talker_finetune=lambda *args:(None,0*a.sum()))
        transformer=lambda **kwargs:SimpleNamespace(last_hidden_state=kwargs['inputs_embeds'])
        with patch.object(train_lora,'build_teacher_forcing_input',return_value=(torch.ones((1,4,2)),torch.tensor([[-100,0,1,0]]),torch.zeros((2,2),dtype=torch.long),2)), \
             patch.object(torch.nn.functional,'cross_entropy',side_effect=lambda *args,**kwargs:7*a.sum()+11*b.sum()),patch.object(torch.Tensor,'item',item):
            result=train_lora.run_training_sample({},object(),base,transformer,parameters,'cpu',torch.float32)
        self.assertNotIn('oom_error',result);self.assertEqual([(12288,12288)]*3,measurements)
        self.assertNotEqual(prior[0].data_ptr(),a.grad.data_ptr());self.assertNotEqual(prior[1].data_ptr(),b.grad.data_ptr())
        print(json.dumps({'measurement':'CPU float32 parameter gradients','prior_gradient_bytes':measurements[0][0],
            'attempt_gradient_bytes':measurements[0][1],'additional_gradient_bytes':measurements[0][1],'combined_gradient_bytes':sum(measurements[0])}))


class TrainingNumericalRefusalTests(unittest.TestCase):
    def test_nonfinite_loss_refuses_before_optimizer_and_preserves_prior_output(self):
        real_ce = torch.nn.functional.cross_entropy
        for loss in (float('nan'), float('inf')):
            with self.subTest(loss=loss), tempfile.TemporaryDirectory() as tmp:
                fixture = TrainingFixture(tmp, ['success'], sub_weight=loss)
                prior = fixture.output / 'adapter_model.safetensors'
                prior.write_bytes(b'prior completed adapter')
                before = [p.detach().clone() for p in fixture.talker.parameters()]
                with fixture.patches(), patch.object(torch.nn.functional, 'cross_entropy', real_ce):
                    with self.assertRaisesRegex(RuntimeError, 'non-finite'):
                        train_lora.train(fixture.args)
                self.assertEqual([], fixture.steps)
                self.assertEqual([], fixture.saved)
                self.assertEqual(b'prior completed adapter', prior.read_bytes())
                for parameter, original in zip(fixture.talker.parameters(), before):
                    torch.testing.assert_close(parameter, original)
                self.assertNotIn('[DONE]', fixture.logs.getvalue())

    def test_nonfinite_backward_restores_exact_previous_gradients(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = TrainingFixture(tmp, ['success'])
            parameters = tuple(fixture.talker.parameters())
            previous = [torch.full_like(p, 3) for p in parameters]
            for parameter, gradient in zip(parameters, previous):
                parameter.grad = gradient
            hook = parameters[0].register_hook(lambda grad: torch.full_like(grad, float('nan')))
            try:
                with fixture.patches(), self.assertRaisesRegex(RuntimeError, 'non-finite'):
                    train_lora.run_training_sample({'mode': 'success'}, fixture.hf, fixture.talker,
                        fixture.talker.model, parameters, 'cpu', torch.float32)
            finally:
                hook.remove()
            for parameter, gradient in zip(parameters, previous):
                self.assertIs(gradient, parameter.grad)
                torch.testing.assert_close(parameter.grad, torch.full_like(parameter, 3))

    def test_nonfinite_optimizer_weights_never_publish_a_successful_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = TrainingFixture(tmp, ['success'])
            optimizer = fixture.optimizer
            def corrupt(parameters, **kwargs):
                result = optimizer(parameters, **kwargs)
                step = result.step
                def corrupted_step(*args, **options):
                    value = step(*args, **options)
                    with torch.no_grad():
                        fixture.talker.a.fill_(float('nan'))
                    return value
                result.step = corrupted_step
                return result
            with fixture.patches(), patch.object(torch.optim, 'AdamW', side_effect=corrupt):
                with self.assertRaisesRegex(RuntimeError, 'non-finite'):
                    train_lora.train(fixture.args)
            self.assertEqual([], fixture.saved)
            self.assertFalse((fixture.output / 'adapter_model.safetensors').exists())
            self.assertNotIn('[DONE]', fixture.logs.getvalue())
