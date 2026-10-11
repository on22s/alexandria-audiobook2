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
            self.assertEqual(256,raw.return_value.chat.completions.create.call_args.kwargs['max_tokens'])

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

    def test_nickname_client_uses_post_heal_live_context(self):
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
                 patch('lmstudio_settings.get_current_status',side_effect=lambda *a,**kw:dict(runtime)) as status,patch('llm_provider.OpenAI') as raw, \
                 patch.object(fn,'get_cached_or_benchmarked_concurrency',return_value=1),patch.object(fn,'find_nicknames',side_effect=scan):
                fn.main()
                self.assertFalse(status.call_args.kwargs['use_cache'])
                self.assertEqual(2000,raw.return_value.chat.completions.create.call_args.kwargs['max_tokens'])
