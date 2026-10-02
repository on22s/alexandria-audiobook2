"""Actual CPU minibatch losses/gradients, unequal lengths, and durable training output."""
import copy
import gc
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref
import torch
import train_lora
from adapter_checkpoint_transaction import validate_adapter_checkpoint_generation
from tests.test_teacher_forcing_language import make_fixture
from tests.test_training_oom_recovery import TrainingFixture


def make_batch_fixture():
    sample,model,codec,text=make_fixture()
    groups=model.talker.code_predictor.get_input_embeddings()
    model.talker.code_predictor.get_input_embeddings=lambda:groups
    for embedding in (codec,text,*groups):embedding.requires_grad_(False)
    second={key:value.clone() for key,value in sample.items()}
    second['codec_ids']=torch.tensor([[7,8],[9,10]])
    second['text_ids']=second['text_ids'][:,:8]
    second['spk_embedding']=torch.ones(1,4)
    class Transformer(torch.nn.Linear):
        def __init__(self):
            super().__init__(4,4);self.calls=[];self.fail=False;self.failed=[]
        def forward(self,inputs_embeds,use_cache,attention_mask=None):
            self.calls.append((tuple(inputs_embeds.shape),None if attention_mask is None else attention_mask.clone()))
            if self.fail:
                self.failed.append(weakref.ref(inputs_embeds))
                raise RuntimeError('CUDA out of memory: CPU minibatch fixture')
            length=inputs_embeds.shape[1]
            scores=inputs_embeds @ inputs_embeds.transpose(1,2) / 2
            causal=torch.ones(length,length,dtype=torch.bool).triu(1)
            scores=scores.masked_fill(causal,float('-inf'))
            if attention_mask is not None:
                scores=scores.masked_fill(attention_mask[:,None,:]==0,float('-inf'))
            hidden=torch.softmax(scores,dim=-1) @ super().forward(inputs_embeds)
            return SimpleNamespace(last_hidden_state=hidden)
    transformer=Transformer();head=torch.nn.Linear(4,40);talker=model.talker
    talker.codec_head=head;talker.model=transformer;talker.frames=[]
    def sub_loss(codes,hidden):
        talker.frames.append((codes.detach().clone(),hidden.detach().clone()))
        return None,hidden.square().mean()
    talker.forward_sub_talker_finetune=sub_loss
    parameters=tuple(transformer.parameters())+tuple(head.parameters())
    return [sample,second],model,talker,transformer,parameters


class TrainingMinibatchTests(unittest.TestCase):
    def test_variable_length_batch_matches_frame_weighted_individual_losses_and_gradients(self):
        samples,model,talker,transformer,parameters=make_batch_fixture()
        before=copy.deepcopy(samples);oracle=[];losses=[];frames=[]
        for sample in samples:
            for parameter in parameters:parameter.grad=None
            result=train_lora.run_training_sample(sample,model,talker,transformer,parameters,'cpu',torch.float32,gradient_accumulation_steps=3)
            oracle.append([parameter.grad.clone() for parameter in parameters]);losses.append(result['loss']);frames.append(talker.frames[-1])
        for parameter in parameters:parameter.grad=None
        result=train_lora.run_training_batch(samples,model,talker,transformer,parameters,'cpu',torch.float32,gradient_accumulation_steps=3)
        weights=[sample['codec_ids'].shape[0]/5 for sample in samples]
        self.assertAlmostEqual(sum(loss*weight for loss,weight in zip(losses,weights)),result['loss'],places=5)
        for index,parameter in enumerate(parameters):
            expected=sum(gradients[index]*weight for gradients,weight in zip(oracle,weights))
            torch.testing.assert_close(expected,parameter.grad)
        shape,mask=transformer.calls[-1];self.assertEqual(2,shape[0]);self.assertEqual(3,len(transformer.calls))
        self.assertEqual(shape[:2],tuple(mask.shape));self.assertTrue(torch.all(mask[0]==1))
        self.assertEqual([1]*(shape[1]-3)+[0]*3,mask[1].tolist())
        torch.testing.assert_close(torch.cat([item[0] for item in frames]),talker.frames[-1][0])
        torch.testing.assert_close(torch.cat([item[1] for item in frames]),talker.frames[-1][1])
        for old,new in zip(before,samples):
            for key in old:torch.testing.assert_close(old[key],new[key])

    def test_failed_batch_restores_prior_gradient_objects_and_drops_failed_graph(self):
        samples,model,talker,transformer,parameters=make_batch_fixture()
        prior=[]
        for parameter in parameters:
            parameter.grad=torch.full_like(parameter,2);prior.append(parameter.grad)
        transformer.fail=True
        result=train_lora.run_training_batch(samples,model,talker,transformer,parameters,'cpu',torch.float32)
        self.assertIn('oom_error',result)
        for parameter,gradient in zip(parameters,prior):
            self.assertIs(gradient,parameter.grad);self.assertTrue(torch.all(parameter.grad==2))
        gc.collect();self.assertTrue(all(reference() is None for reference in transformer.failed))

    def test_backward_failure_restores_prior_gradients_after_other_parameters_received_gradients(self):
        samples,model,talker,transformer,parameters=make_batch_fixture()
        prior=[];attempts=[]
        for parameter in parameters:
            parameter.grad=torch.full_like(parameter,2);prior.append(parameter.grad)
        def observe(gradient):
            attempts.append(gradient.detach().clone());return gradient
        def fail(gradient):
            raise RuntimeError('CUDA out of memory: CPU minibatch backward fixture')
        observed=parameters[-1].register_hook(observe);failed=parameters[0].register_hook(fail)
        try:
            result=train_lora.run_training_batch(samples,model,talker,transformer,parameters,'cpu',torch.float32)
        finally:
            observed.remove();failed.remove()
        self.assertIn('oom_error',result);self.assertTrue(attempts)
        for parameter,gradient in zip(parameters,prior):
            self.assertIs(gradient,parameter.grad);self.assertTrue(torch.all(parameter.grad==2))

    def run_loop(self,modes,batch_size,accum=2):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=TrainingFixture(tmp,modes,grad_accum=accum);fixture.args.batch_size=batch_size
            original=fixture.talker.model.__call__;batches=[]
            def forward(inputs_embeds,use_cache,attention_mask=None):
                batches.append(inputs_embeds.shape[0]);return original(inputs_embeds,use_cache)
            # __call__ is looked up on the type, so use a concrete callable.
            class WrappedTransformer:
                def gradient_checkpointing_enable(self):pass
                def __call__(self,**kwargs):return forward(**kwargs)
            fixture.talker.model=WrappedTransformer()
            with fixture.patches():train_lora.train(fixture.args)
            metadata=validate_adapter_checkpoint_generation(fixture.output)
            return batches,len(fixture.steps),metadata,fixture.logs.getvalue()

    def test_loop_honors_batch_size_accumulation_tail_and_published_metadata(self):
        batches,steps,metadata,logs=self.run_loop(['success']*5,2)
        self.assertEqual([2,2,1],batches);self.assertEqual(2,steps)
        self.assertEqual(5,metadata['num_samples']);self.assertEqual(2,metadata['batch_size'])
        self.assertEqual(2,metadata['gradient_accumulation_steps']);self.assertEqual(0,metadata['oom_skips'])
        self.assertIn('effective batch: 4',logs);self.assertIn('step=5/5',logs)
        self.assertIn('sample presentations: 5',logs);self.assertIn('forward batches : 3',logs)
        batches,steps,metadata,logs=self.run_loop(['success']*8,4,accum=2)
        self.assertEqual([4,4],batches);self.assertEqual(1,steps);self.assertIn('effective batch: 8',logs)
        batches,steps,metadata,logs=self.run_loop(['success']*64,8,accum=8)
        self.assertEqual([8]*8,batches);self.assertEqual(1,steps)
        self.assertEqual(64,metadata['num_samples']);self.assertIn('effective batch: 64',logs)

    def test_batch_oom_counts_every_skipped_sample_and_flushes_successful_tail(self):
        batches,steps,metadata,logs=self.run_loop(['success','success','forward_oom','forward_oom','success'],2)
        self.assertEqual([2,2,1],batches);self.assertEqual(1,steps)
        self.assertEqual(2,metadata['oom_skips']);self.assertIn('skipping 2 sample(s)',logs)

    def test_batching_keeps_sample_coverage_and_consecutive_oom_limits(self):
        for modes,batch_size,message in ((['success']*2+['forward_oom']*6,2,'fewer than half.*requested=8 succeeded=2 skipped=6'),
                                         (['forward_oom']*16,8,'16 consecutive OOM-like.*requested=16 succeeded=0 skipped=16')):
            with self.subTest(batch_size=batch_size),tempfile.TemporaryDirectory() as tmp:
                fixture=TrainingFixture(tmp,modes);fixture.args.batch_size=batch_size
                class WrappedTransformer:
                    def gradient_checkpointing_enable(self):pass
                    def __call__(self,inputs_embeds,use_cache,attention_mask=None):
                        if fixture.mode=='forward_oom':raise RuntimeError('CUDA out of memory: CPU batch coverage fixture')
                        return SimpleNamespace(last_hidden_state=inputs_embeds+fixture.talker.a+fixture.talker.b)
                fixture.talker.model=WrappedTransformer()
                prior={name:b'prior checkpoint bytes' for name in ('adapter_model.safetensors','training_meta.json')}
                for name,data in prior.items():(fixture.output/name).write_bytes(data)
                with fixture.patches():
                    with self.assertRaisesRegex(RuntimeError,message):train_lora.train(fixture.args)
                self.assertEqual([],fixture.saved)
                self.assertEqual(prior,{name:(fixture.output/name).read_bytes() for name in prior})
