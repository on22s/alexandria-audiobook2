import unittest
from types import SimpleNamespace
from unittest.mock import patch
from llm_request_budget import get_budgeted_request, get_request_limits
from lmstudio_settings import TokenBudgetError
import three_pass_generate as tp

class ReviewRegressionTests(unittest.TestCase):
    def test_core_inherits_global_limits_with_cached_client(self):
        import core
        cfg={'llm':{'base_url':'http://localhost:1234/v1','api_key':'local','model_name':'fixture'},'generation':{'max_tokens':256,'max_input_tokens':100}}
        with patch.object(core,'load_app_config',return_value=cfg),patch.object(core,'_llm_client_cache',{}),patch('llm_provider.OpenAI') as raw,patch('lmstudio_settings.get_current_status',return_value={}):
            client,_=core._make_llm_client(120)
            self.assertIs(client,core._make_llm_client(120)[0])
            with self.assertRaises(TokenBudgetError):client.chat.completions.create(messages=[{'content':'x'*301}],max_tokens=4000)
            raw.return_value.chat.completions.create.assert_not_called()
            client.chat.completions.create(messages=[{'content':'short'}],max_tokens=4000)
            # The global input ceiling is inherited; the 256 baseline is not a cap.
            self.assertEqual(4000,raw.return_value.chat.completions.create.call_args.kwargs['max_tokens'])

    def test_real_rescue_context_admitted_and_oversized_input_rejected(self):
        cfg={'llm':{'max_input_tokens':2000,'max_output_tokens':4096,'context_length':16000},'generation':{'three_pass_segmentation':'model','three_pass_chunk_size':500,'max_tokens':4096}}
        settings=tp.resolve_three_pass_generation_settings(cfg)
        report=tp.build_three_pass_request_preflight('The gardener watered flowers. '*8,settings,16000,1)
        self.assertTrue(any(r['stage']=='segment' for r in report['requests']))
        self.assertTrue(all(r['prompt_tokens']<=2000 for r in report['requests']))
        settings['max_input_tokens']=100
        with self.assertRaises(TokenBudgetError):tp.build_three_pass_request_preflight('The gardener watered flowers.',settings,16000,1)

    def test_count_attribution_splits_before_dispatch(self):
        source='\n'.join('"This is statement number %d with quite a few descriptive words."'%i for i in range(20))
        cfg={'llm':{'context_length':16000,'max_input_tokens':1200,'max_output_tokens':4096},'generation':{'max_tokens':4096,'three_pass_segmentation':'auto','three_pass_attribute_context_chars':0}}
        params=tp.get_three_pass_run_params(cfg,{})
        sizes=[]
        def attribute(c,m,b,p,**kw):
            sizes.append(len(b))
            return ([{'speaker':'Speaker','text':e['text']} for e in b],None)
        with patch.object(tp,'attribute_batch_voted',side_effect=attribute),patch.object(tp,'instruct_batch',side_effect=lambda c,m,b,p,**kw:[dict(e,instruct='Neutral.') for e in b]):
            entries=tp.run_three_pass(SimpleNamespace(),'fixture',source,params,3000,on_exhaustion='fallback',attribute_context_chars=0)
        self.assertGreater(len(sizes),1);self.assertEqual(20,sum(sizes));self.assertEqual(20,len(entries))

    def test_persona_recovery_splits_under_profile_limits(self):
        import generate_personas as gp
        profile={'context_length':4096,'max_input_tokens':100,'max_output_tokens':400}
        params=gp._persona_params('',8192,profile,400,0.3)
        self.assertEqual(4096,params.context_length)
        calls=[]
        def request(c,m,system,prompt,params,**kw):
            get_budgeted_request({'messages':[{'content':prompt}],'max_tokens':400},get_request_limits(profile))
            calls.append(prompt)
            return {'description':'ok','ref_text':'ok'}
        with patch.object(gp,'call_llm_for_object',side_effect=request):
            basic=gp.request_persona_with_evidence(SimpleNamespace(),'fixture','',lambda parts:''.join(v for k,v in parts),[('source','x'*200),('source','y'*200)],params,'BASIC',validate_object=lambda value:value)
            self.assertIsInstance(basic,dict)
            result=gp.request_persona_with_evidence(SimpleNamespace(),'fixture','',lambda parts:''.join(v for k,v in parts),[('source','x'*200),('source','y'*200)],params,'TEST',max_prompt_chars=10000,validate_object=lambda value:value)
        self.assertIsInstance(result,dict);self.assertIn('x'*200,calls);self.assertIn('y'*200,calls)

    def test_nickname_client_is_built_after_heal_with_profile_output_cap(self):
        import find_nicknames as fn
        import tempfile, json, sys
        from pathlib import Path
        runtime={'context_length':4096,'loaded':True}
        profile={'model_name':'fixture','base_url':'http://localhost:1234/v1','api_key':'local','max_output_tokens':2000}
        def heal(*a,**kw):
            runtime['context_length']=8192
            return False,dict(runtime),'healed'
        def scan(client,*a,**kw):
            client.chat.completions.create(messages=[{'content':'x'*12000}],max_tokens=2000)
            return {},{}
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp,'source.json');source.write_text(json.dumps([{'speaker':'Speaker','text':'Hello.'}]))
            with patch.object(sys,'argv',['find_nicknames.py','--input',str(source),'--aliases-file',str(Path(tmp,'aliases.json'))]), \
                 patch.object(fn,'load_app_config',return_value={'llm':profile}),patch.object(fn,'ensure_ideal_settings',side_effect=heal), \
                 patch('lmstudio_settings.get_current_status',side_effect=lambda *a,**kw:dict(runtime)),patch('llm_provider.OpenAI') as raw, \
                 patch.object(fn,'get_cached_or_benchmarked_concurrency',return_value=1),patch.object(fn,'find_nicknames',side_effect=scan):
                fn.main()
                self.assertEqual(2000,raw.return_value.chat.completions.create.call_args.kwargs['max_tokens'])


class ReviewFindingFixTests(unittest.TestCase):
    """PR #1097 review: each case failed on the reviewed head (66d55bb2)."""
    LOCAL={'model_name':'fixture','base_url':'http://localhost:1234/v1','api_key':'local'}

    def test_blank_profile_keeps_three_pass_escalation_ceiling(self):
        params=tp.get_three_pass_run_params({'llm':dict(self.LOCAL),'generation':{'max_tokens':4096}},{})
        self.assertEqual(4096,params.max_tokens)
        self.assertEqual(16384,params.hard_max_tokens)
        capped=tp.get_three_pass_run_params({'llm':dict(self.LOCAL,max_output_tokens=2000),'generation':{'max_tokens':4096}},{})
        self.assertEqual(2000,capped.hard_max_tokens)

    def test_blank_profile_http_client_does_not_clamp_to_baseline(self):
        from lmstudio_settings import get_active_llm_config
        from llm_provider import make_llm_client
        llm=get_active_llm_config({'llm':dict(self.LOCAL),'generation':{'max_tokens':4096}})
        with patch('llm_provider.OpenAI') as raw:
            make_llm_client(llm,10).chat.completions.create(messages=[{'content':'short'}],max_tokens=16000)
        self.assertEqual(16000,raw.return_value.chat.completions.create.call_args.kwargs['max_tokens'])

    def test_failover_with_blank_secondary_keeps_the_run_ceiling(self):
        from unittest.mock import Mock
        from llm_provider import FailoverClient
        from generate_script import LLMGenParams, ensure_run_request_params
        client=Mock(spec=FailoverClient)
        client.ensure_active_runtime_profile.return_value={'config':{},'context_length':8192}
        params=ensure_run_request_params(client,LLMGenParams(max_tokens=4096,hard_max_tokens=32768))
        self.assertEqual(32768,params.hard_max_tokens)
        self.assertEqual(8192,params.context_length)

    def test_rescue_fits_ample_context_and_respects_input_ceiling(self):
        from generate_script import LLMGenParams
        chunk='x'*9000
        # Output estimate (4564) exceeds max_tokens but fits the context.
        self.assertTrue(tp._rescue_prompt_fits(chunk,'b'*2000,'a'*2000,500,
                                               LLMGenParams(max_tokens=4096,context_length=131072)))
        self.assertFalse(tp._rescue_prompt_fits(chunk,'b'*2000,'a'*2000,500,
                                                LLMGenParams(max_tokens=4096,context_length=6000)))
        self.assertFalse(tp._rescue_prompt_fits(chunk,'b'*2000,'a'*2000,500,
                                                LLMGenParams(max_tokens=4096,max_input_tokens=1000)))
        self.assertTrue(tp._rescue_prompt_fits(chunk,'','',500,LLMGenParams(max_tokens=4096)))

    def test_setup_migration_does_not_bake_inherited_limits_into_profile(self):
        from routers.system import get_config_local_profile
        profile=get_config_local_profile({'llm':dict(self.LOCAL),
                                          'generation':{'max_tokens':4096,'max_input_tokens':2000}})
        self.assertEqual(dict(self.LOCAL),profile)

    def _refusal(self,llm,status=None,error=None):
        from routers import script
        config={'llm_mode':'local','llm':llm,
                'generation':{'three_pass_chunk_size':50000,'three_pass_segmentation':'quotes'}}
        job={'filename':'sample.txt','prepared_source':{}}
        probe=patch('lmstudio_settings.get_current_status',side_effect=error) if error else \
              patch('lmstudio_settings.get_current_status',return_value=status)
        with patch.object(script,'load_app_config',return_value=config), \
             patch.object(script,'get_validated_batch_script_source',return_value=('Narration. "Hello."',0)), probe:
            return script.three_pass_refusal([job])

    def test_admission_uses_planned_context_not_pre_heal_status(self):
        unloaded={'runtime':'lmstudio','management_verified':True,'available':True,'loaded':False,'context_length':None}
        unverified={'runtime':'unknown','management_verified':False,'available':False,'loaded':False,'context_length':None}
        # A managed model the run's heal will load is admitted.
        self.assertIsNone(self._refusal(dict(self.LOCAL),unloaded))
        # #1084: an endpoint that cannot report its context is refused up front...
        self.assertIn('Total context budget',self._refusal(dict(self.LOCAL),unverified))
        # ...unless the profile declares one.
        self.assertIsNone(self._refusal(dict(self.LOCAL,context_length=32000),unverified))
        # A probe failure defers to the run's own check instead of a 500.
        self.assertIsNone(self._refusal(dict(self.LOCAL),error=RuntimeError('ssh down')))

    def test_preflight_sizes_declared_context_against_planned_not_current(self):
        from lmstudio_settings import get_planned_request_context
        config={'llm_mode':'local','llm':dict(self.LOCAL,context_length=32768)}
        loaded_small={'runtime':'lmstudio','management_verified':True,'loaded':True,'context_length':8192,
                      'ideal_context_length':32768}
        with patch('lmstudio_settings.get_current_status',return_value=loaded_small) as probe:
            planned=get_planned_request_context(config)
        self.assertEqual({'context':32768,'known':True,'parallel':planned['parallel']},planned)
        self.assertEqual(1,probe.call_count)

class SecondReviewRegressionTests(unittest.TestCase):
    def test_short_segment_ceiling_survives_blank_and_explicit_failover_limits(self):
        from unittest.mock import Mock
        from generate_script import ensure_run_request_params
        from llm_provider import FailoverClient
        params=tp.get_three_pass_run_params({'llm':{'max_output_tokens':2000},
                                           'generation':{'max_tokens':4096}},{})
        client=Mock(spec=FailoverClient)
        observed=[]
        def dispatch(c,m,system,user,bounded,**kw):
            serving=ensure_run_request_params(c,bounded)
            observed.append((serving.max_tokens,serving.hard_max_tokens))
            request=get_budgeted_request({'messages':[{'content':user}],
                                         'max_tokens':serving.hard_max_tokens},
                                        get_request_limits({'max_output_tokens':serving.hard_max_tokens},32768))
            self.assertLessEqual(request['max_tokens'],512)
            return [{'type':'NARRATOR','text':'Short source.'}]
        with patch.object(tp,'call_llm_for_entries',side_effect=dispatch):
            for profile in ({},{'max_output_tokens':8000},{'max_output_tokens':256}):
                client.ensure_active_runtime_profile.return_value={'config':profile,'context_length':32768}
                result=tp._call_segment(client,'fixture','Short source.','','Short source.',params,
                                        'SEGMENT',2,None)
                self.assertEqual('Short source.',result[0]['text'])
        self.assertEqual([(512,512),(512,512),(256,256)],observed)
        self.assertEqual((2000,2000),(params.max_tokens,params.hard_max_tokens))

    def test_task_policy_fingerprint_preserves_defaults_and_distinguishes_failover(self):
        from generate_script import LLMGenParams
        from llm_request_budget import get_task_output_fingerprint
        self.assertEqual({},get_task_output_fingerprint(LLMGenParams()))
        self.assertEqual({},get_task_output_fingerprint(LLMGenParams(task_max_tokens=4096,task_hard_max_tokens=16384)))
        self.assertEqual({'task_max_tokens':4096,'task_hard_max_tokens':16384},
                         get_task_output_fingerprint(LLMGenParams(max_tokens=512,hard_max_tokens=512,
                                                                 task_max_tokens=4096,task_hard_max_tokens=16384)))

    def test_declared_context_alone_subdivides_actual_attribution(self):
        from lmstudio_settings import get_effective_max_tokens
        source='\n'.join('"This is statement %d %s."'%(i,'word '*106) for i in range(20))
        cfg={'llm':{'context_length':4096},'generation':{'max_tokens':4096,'three_pass_segmentation':'auto','three_pass_attribute_context_chars':0}}
        params=tp.get_three_pass_run_params(cfg,{})
        sizes=[]
        def attribute(c,m,b,p,**kw):
            sizes.append(len(b))
            system,user=tp.get_attribute_sizing_request(b,p,kw.get('roster'),kw.get('neighbor_contexts'),kw.get('surround'))
            get_effective_max_tokens(p.max_tokens,p.context_length,[{'content':system},{'content':user}],p.hard_max_tokens,scale_to_context=False)
            return ([{'speaker':'Speaker','text':e['text']} for e in b],None)
        with patch.object(tp,'attribute_batch_voted',side_effect=attribute),patch.object(tp,'instruct_batch',side_effect=lambda c,m,b,p,**kw:[dict(e,instruct='Neutral.') for e in b]):
            entries=tp.run_three_pass(SimpleNamespace(),'fixture',source,params,3000,on_exhaustion='fail',attribute_context_chars=0)
        self.assertGreater(len(sizes),1)
        self.assertEqual(20,sum(sizes))
        self.assertEqual(20,len(entries))
        self.assertTrue(all(entry['text'] in source for entry in entries))

    def test_attribute_fit_allows_bounded_retry_growth(self):
        from generate_script import LLMGenParams
        params=LLMGenParams(max_tokens=256,hard_max_tokens=16384,context_length=16000)
        self.assertFalse(tp.does_request_fit_context('','short',params,480))
        self.assertTrue(tp.does_request_fit_context('','short',params,480,allow_retry_growth=True))
        sizes=[]
        source='\n'.join('"Short statement %d."'%i for i in range(20))
        params.segmentation='auto'
        def attribute(c,m,b,p,**kw):
            sizes.append(len(b))
            return ([{'speaker':'Speaker','text':e['text']} for e in b],None)
        with patch.object(tp,'attribute_batch_voted',side_effect=attribute),patch.object(tp,'instruct_batch',side_effect=lambda c,m,b,p,**kw:[dict(e,instruct='Neutral.') for e in b]):
            tp.run_three_pass(SimpleNamespace(),'fixture',source,params,3000,on_exhaustion='fail',attribute_context_chars=0)
        self.assertEqual([20],sizes)
        params.hard_max_tokens=400
        self.assertFalse(tp.does_request_fit_context('','short',params,480,allow_retry_growth=True))

    def test_capped_primary_does_not_cap_blank_failover(self):
        from unittest.mock import Mock
        from llm_provider import FailoverClient
        from generate_script import LLMGenParams, ensure_run_request_params
        primary=tp.get_three_pass_run_params({'llm':{'max_output_tokens':512},'generation':{'max_tokens':4096}},{})
        client=Mock(spec=FailoverClient)
        client.ensure_active_runtime_profile.return_value={'config':{},'context_length':32768}
        secondary=ensure_run_request_params(client,primary)
        self.assertEqual((4096,16384),(secondary.max_tokens,secondary.hard_max_tokens))
        review=LLMGenParams(max_tokens=512,hard_max_tokens=512,task_max_tokens=4096,task_hard_max_tokens=32768)
        self.assertEqual(32768,ensure_run_request_params(client,review).hard_max_tokens)
        client.ensure_active_runtime_profile.return_value={'config':{'max_output_tokens':2000},'context_length':32768}
        secondary=ensure_run_request_params(client,primary)
        self.assertEqual((2000,2000),(secondary.max_tokens,secondary.hard_max_tokens))
        self.assertEqual(512,primary.hard_max_tokens)

    def test_named_benchmark_profiles_inherit_global_input_limit(self):
        import benchmark_runner as br
        from llm_provider import make_llm_client
        profile={'model_name':'fixture','base_url':'http://localhost:1234/v1','api_key':'local','max_input_tokens':None}
        cfg={'llm_local':dict(profile),'llm_remote':dict(profile),'generation':{'max_input_tokens':100}}
        status={'available':True,'loaded':True,'context_length':8192}
        with patch.object(br,'get_lmstudio_status',return_value=status),patch.object(br,'get_remote_lmstudio_status',return_value=status),patch('llm_provider.OpenAI') as raw:
            for target in ('local','thunder'):
                selected,_=br._get_llm_benchmark_target(cfg,target)
                with self.assertRaises(TokenBudgetError):
                    make_llm_client(selected,10).chat.completions.create(messages=[{'content':'x'*600}],max_tokens=100)
                raw.return_value.chat.completions.create.assert_not_called()
            self.assertIsNone(cfg['llm_local']['max_input_tokens'])
            cfg['llm_local']['max_input_tokens']=300
            selected,_=br._get_llm_benchmark_target(cfg,'local')
            make_llm_client(selected,10).chat.completions.create(messages=[{'content':'x'*600}],max_tokens=100)
            self.assertEqual(1,raw.return_value.chat.completions.create.call_count)
