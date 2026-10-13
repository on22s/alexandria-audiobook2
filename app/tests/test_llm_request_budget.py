"""Profile ceilings bind planning and real dispatch without bypassing context."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from pydantic import ValidationError
from config_settings import LLMConfig, GenerationConfig
from generate_script import LLMGenParams
from llm_request_budget import get_inherited_profile_limits, get_request_limits, get_budgeted_request
from llm_provider import ConfiguredOpenAI, make_llm_client
from lmstudio_settings import get_active_llm_config, get_effective_max_tokens, TokenBudgetError
import three_pass_generate as tp


class RequestBudgetTests(unittest.TestCase):
    def test_existing_config_gets_no_output_cap_or_input_ceiling(self):
        config={'llm_mode':'remote','llm_remote':{'model_name':'fixture'},'generation':{'max_tokens':8192}}
        original=copy.deepcopy(config)
        active=get_active_llm_config(config)
        # The baseline stays a starting budget; a cap here truncated retries.
        self.assertIsNone(active.get('max_output_tokens'))
        self.assertIsNone(active.get('max_input_tokens'))
        self.assertEqual(original,config)
        self.assertIsNone(GenerationConfig().max_input_tokens)

    def test_explicit_profile_overrides_and_blank_inheritance(self):
        g={'max_tokens':4096,'max_input_tokens':2000}
        p={'max_output_tokens':9000,'max_input_tokens':1000,'context_length':16000}
        self.assertEqual(p,get_inherited_profile_limits(p,g))
        p['max_input_tokens']=None
        self.assertEqual(2000,get_inherited_profile_limits(p,g)['max_input_tokens'])

    def test_smaller_limit_wins_both_directions_unknown_uses_declaration(self):
        for declared,detected,want in [(8192,4096,4096),(4096,8192,4096),(1000000,None,1000000),(None,8192,8192),(None,None,None)]:
            self.assertEqual(want,get_request_limits({'context_length':declared},detected)['context_length'])

    def test_invalid_limits_never_become_unlimited(self):
        for field in ['context_length','max_input_tokens','max_output_tokens']:
            for value in [0,-1,True,1.5,float('inf'),'4096']:
                with self.subTest(field=field,value=value):
                    with self.assertRaises(ValueError):get_request_limits({field:value})
                    with self.assertRaises(ValidationError):LLMConfig(base_url='http://fixture',api_key='local',model_name='fixture',**{field:value})
        self.assertEqual(1000000,LLMConfig(base_url='http://fixture',api_key='local',model_name='fixture',context_length=1000000).context_length)

    def test_input_checks_apply_with_unknown_context_and_output_includes_reserve(self):
        with self.assertRaises(TokenBudgetError):get_effective_max_tokens(4096,None,[{'content':'x'*3001}],max_input_tokens=1000)
        self.assertEqual(3072,get_effective_max_tokens(4096,4096,[{'content':'x'*1536}],4096))
        with self.assertRaises(TokenBudgetError):get_effective_max_tokens(4096,4096,[{'content':'x'*12000}],4096)

    def test_extra_body_cannot_bypass_input_or_output_ceiling(self):
        limits=get_request_limits({'context_length':4096,'max_input_tokens':1000,'max_output_tokens':512})
        kwargs={'max_tokens':256,'messages':[{'content':'small'}],'extra_body':{'max_tokens':99999,'temperature':0.1}}
        original=copy.deepcopy(kwargs)
        result=get_budgeted_request(kwargs,limits)
        self.assertEqual(256,result['max_tokens'])
        self.assertEqual({'temperature':0.1},result['extra_body'])
        self.assertEqual(original,kwargs)
        kwargs['extra_body']['messages']=[{'content':'x'*3001}]
        self.assertEqual(kwargs['messages'],get_budgeted_request(kwargs,limits)['messages'])
        del kwargs['messages']
        with self.assertRaises(TokenBudgetError):get_budgeted_request(kwargs,limits)
        for value in [-1,0,True]:
            with self.assertRaises(ValueError):get_budgeted_request({'max_tokens':value},limits)

    def test_body_aliases_only_lower_checked_allowance_and_keep_finite_limits(self):
        for key in ('max_tokens', 'max_completion_tokens'):
            for body_budget, expected in ((20000, 128), (64, 64)):
                request = {'max_tokens': 128, 'messages': [{'content': 'short'}],
                           'extra_body': {key: body_budget}}
                self.assertEqual(expected, get_budgeted_request(request, {})['max_tokens'])
                self.assertEqual(32, get_budgeted_request(request, {'max_output_tokens': 32})['max_tokens'])
                self.assertEqual(8, get_budgeted_request(request, {'context_length': 522})['max_tokens'])
            for invalid in (0, -1, True, None):
                with self.assertRaises(ValueError):
                    get_budgeted_request({'max_tokens': 128, 'extra_body': {key: invalid}}, {})

    def test_timeout_variants_preserve_limits_and_block_before_dispatch(self):
        raw=Mock();raw.with_options.return_value=raw
        limits=get_request_limits({'context_length':4096,'max_input_tokens':100,'max_output_tokens':256})
        client=ConfiguredOpenAI(raw,{},limits=limits).with_options(timeout=1)
        with self.assertRaises(TokenBudgetError):client.chat.completions.create(messages=[{'content':'x'*301}],max_tokens=4096)
        raw.chat.completions.create.assert_not_called()
        client.chat.completions.create(messages=[{'content':'short'}],max_tokens=4096)
        self.assertEqual(256,raw.chat.completions.create.call_args.kwargs['max_tokens'])

    def test_http_client_enforces_declared_limits_without_probing_and_manual_does_not_reload(self):
        # Construction is pure (no status probe, no reload): a cached client
        # cannot hold a stale detected context. Detected context is applied per
        # call through params; the wrapper enforces the declaration.
        profile={'context_length':8192,'max_output_tokens':4096,'base_url':'http://fixture','api_key':'local','model_name':'fixture'}
        with patch('llm_provider.OpenAI') as raw, patch('lmstudio_settings.get_current_status') as status, patch('lmstudio_settings.ensure_ideal_settings') as reload:
            client=make_llm_client(profile,10)
            client.chat.completions.create(model='fixture',messages=[{'content':'x'*12288}],max_tokens=4096)
            # 4096 prompt + 512 reserve leave 3584 of the declared 8192.
            self.assertEqual(3584,raw.return_value.chat.completions.create.call_args.kwargs['max_tokens'])
            with self.assertRaises(TokenBudgetError):
                client.chat.completions.create(model='fixture',messages=[{'content':'x'*24000}],max_tokens=4096)
            self.assertEqual(1,raw.return_value.chat.completions.create.call_count)
            status.assert_not_called()
            reload.assert_not_called()
        with patch('llm_provider.ManualClient') as manual,patch('lmstudio_settings.get_current_status') as status:
            client=make_llm_client(dict(profile,transport='manual'),10)
            with self.assertRaises(TokenBudgetError):client.chat.completions.create(messages=[{'content':'x'*24000}],max_tokens=4096)
            manual.return_value.chat.completions.create.assert_not_called()
            status.assert_not_called()

    def test_native_params_and_preflight_share_limits(self):
        config={'llm':{'context_length':16000,'max_input_tokens':2000,'max_output_tokens':700},'generation':{'max_tokens':4096}}
        settings=tp.resolve_three_pass_generation_settings(config)
        params=tp.get_three_pass_run_params(config,{'context_length':8000},generation_settings=settings)
        self.assertEqual((8000,2000,700,700),(params.context_length,params.max_input_tokens,params.max_tokens,params.hard_max_tokens))
        self.assertFalse(tp.does_request_fit_context('','x'*6001,params,256))
        self.assertFalse(tp.does_request_fit_context('','small',params,701))
        settings["max_input_tokens"]=100  # Reject the actual fixed prompt, not fabricated context.
        with self.assertRaises(TokenBudgetError):
            tp.build_three_pass_request_preflight('She stood. "Yes."',settings,8000,1)
        settings['max_input_tokens']=10000
        report=tp.build_three_pass_request_preflight('She stood. "Yes."',settings,8000,1)
        self.assertEqual(8000,report['context_length'])
        self.assertEqual(700,report['output_ceiling'])

    def test_single_pass_with_input_limit_runs_real_chunk_path(self):
        import generate_script as gs
        source='The gardener watered the flowers beside the wall.'
        entries=[{'speaker':'NARRATOR','text':source,'instruct':'Neutral.'}]
        params=LLMGenParams(max_input_tokens=10000)
        with patch.object(gs,'call_llm_for_entries',return_value=entries):
            result=gs.process_chunk(None,'fixture',source,1,1,params)
        self.assertEqual(entries,result)

    def test_unknown_context_large_targets_rejected_before_pipeline_work(self):
        for controls in [dict(chunk_size=50000),dict(attribute_context_chars=20001),
                         dict(attribute_target_chars=1000),dict(instruct_target_chars=1000)]:
            with self.subTest(controls=controls),patch.object(tp,'get_run_model_binding') as binding:
                with self.assertRaisesRegex(TokenBudgetError,'Setup'):
                    tp.run_three_pass(None,'fixture','Text.',LLMGenParams(),
                                      controls.get('chunk_size',3000),
                                      **{k:v for k,v in controls.items() if k!='chunk_size'})
                binding.assert_not_called()

class ProfileBudgetAdmissionTests(unittest.TestCase):
    def test_unknown_context_refuses_before_claiming_a_job(self):
        import asyncio
        from fastapi import BackgroundTasks, HTTPException
        from routers import script
        config={'llm':{'model_name':'fixture','base_url':'http://unknown.invalid'},
                'generation':{'three_pass_chunk_size':50000,'three_pass_segmentation':'quotes'}}
        request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='sample.txt')])
        with patch.object(script,'check_global_gpu_lock'), patch.object(script,'_resolve_batch_script_input',return_value='/tmp/sample.txt'), \
             patch.object(script,'get_batch_script_source_identity',return_value=(1,)), \
             patch.object(script,'_read_and_validate_batch_script_source',return_value=('Narration. "Hello."',0)), \
             patch.object(script,'get_validated_batch_script_source',return_value=('Narration. "Hello."',0)), \
             patch.object(script,'load_app_config',return_value=config), \
             patch('lmstudio_settings.get_current_status',return_value={'context_length':None}), \
             patch.object(script,'claim_gpu_task') as claim:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(script.generate_script_batch_start(request,BackgroundTasks()))
            self.assertEqual(400,caught.exception.status_code)
            self.assertIn('Setup',caught.exception.detail)
            claim.assert_not_called()

    def test_declared_context_allows_unknown_manual_large_target_and_preserves_cache_capacity(self):
        from routers import script
        config={'llm':{'model_name':'fixture','transport':'manual','context_length':32000},
                'generation':{'three_pass_chunk_size':50000,'three_pass_segmentation':'quotes'}}
        settings=tp.resolve_three_pass_generation_settings(config)
        job={'filename':'sample.txt','prepared_source':{}}
        with patch.object(script,'load_app_config',return_value=config), \
             patch.object(script,'get_validated_batch_script_source',return_value=('Narration. "Hello."',0)), \
             patch('lmstudio_settings.get_current_status') as probe:
            self.assertIsNone(script.three_pass_refusal([job]))
            # Both callers size with the one planned-context answer.
            context=script.get_planned_request_context(config)['context']
            report=script.get_batch_script_source_preflight(job,'Narration. "Hello."',settings,context,None)
            job['prepared_source']['preflight']={'identity':script.get_batch_script_sizing_identity('Narration. "Hello."',settings,None,32000),'report':report}
            cached=script.get_batch_script_source_preflight(job,'Narration. "Hello."',settings,context,None)
            self.assertEqual(32000,cached['context_length'])
            probe.assert_not_called()

if __name__=='__main__':unittest.main()
