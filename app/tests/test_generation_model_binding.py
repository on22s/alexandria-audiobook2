"""Selected models reach both CLIs and remain honest across configured failover."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import config_settings as cs
import generate_script as gs
import three_pass_generate as tp
import llm_provider as lp
from lmstudio_settings import get_active_llm_config
from experiments import three_pass_vs_single as harness

ROOT=Path(__file__).resolve().parents[2]
TEXT='The patient gardener carefully watered the tall flowers beside the old stone wall.'
MODEL='qwen3-14b'

def config(mode='local'):
    return {'llm_mode':mode,'llm_failover':True,
        'llm':{'model_name':'stale','base_url':'http://stale/v1','api_key':'k'},
        'llm_local':{'model_name':'configured-local','base_url':'http://local/v1','api_key':'k',
            'provider_headers':{'X-Fixture':'local'},'request_interval_seconds':.25,'api_retry_limit':0},
        'llm_remote':{'model_name':'configured-remote','base_url':'http://remote/v1','api_key':'k',
            'provider_extra_body':{'reasoning_effort':'none'},'on_api_exhaustion':'pause'},
        'generation':{'chunk_size':3000,'three_pass_segmentation':'quotes','max_tokens':4096}}

class GenerationModelBindingTests(unittest.TestCase):
    def test_shared_override_selects_active_profile_without_mutating_config_or_failover(self):
        helper=getattr(cs,'get_generation_config',None)
        self.assertIsNotNone(helper,'shared validated override is missing')
        for mode in ('local','remote'):
            cfg=config(mode);before=copy.deepcopy(cfg);out=helper(cfg,MODEL)
            self.assertEqual(before,cfg)
            self.assertEqual(MODEL,get_active_llm_config(out)['model_name'])
            other='remote' if mode=='local' else 'local'
            self.assertEqual(cfg['llm_'+other],out['llm_'+other])
            self.assertEqual({**cfg['llm_'+mode],'model_name':MODEL},out['llm_'+mode])
            self.assertEqual(get_active_llm_config(out),out['llm'])
            out['llm_'+mode]['api_key']='fixture edit'
            self.assertEqual(before,cfg)
        for bad in ('','  ','bad\nmodel',17,False):
            with self.subTest(bad=bad),self.assertRaises(ValueError):helper(config(),bad)
        self.assertEqual('configured-local',get_active_llm_config(helper(config()))['model_name'])
        self.assertEqual(MODEL,get_active_llm_config(helper({'llm':config()['llm_local']},MODEL))['model_name'])

    def run_cli(self,module,root,override=True,failover=False):
        source=root/'book.txt';source.write_text(TEXT)
        cfg=config();cfg_path=root/'config.json';cfg_path.write_text(json.dumps(cfg));before=cfg_path.read_bytes()
        output=root/(module.__name__+'.json');seen=[];requests=[]
        def create_client(profile,timeout):
            seen.append(copy.deepcopy(profile))
            def create(**kwargs):
                requests.append(kwargs['model'])
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content=json.dumps([{'speaker':'NARRATOR','text':TEXT,'instruct':'Neutral.'}])),finish_reason='stop')],usage=None)
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        def single_work(client,model,chunk,*args,**kwargs):
            if failover:client.failover({'category':'server_error','status_code':503})
            client.chat.completions.create(model=model)
            return [{'speaker':'NARRATOR','text':chunk,'instruct':'Neutral.'}],False
        def instruct_work(client,model,rows,params,**kwargs):
            if failover:client.failover({'category':'server_error','status_code':503})
            client.chat.completions.create(model=model)
            return [{**r,'instruct':'Neutral.'} for r in rows]
        argv=[module.__name__,str(source),'--output',str(output)]
        if override:argv+=['--model',MODEL]
        with patch.dict(os.environ,{'ALEXANDRIA_DATA_DIR':str(root)}),patch.object(sys,'argv',argv), \
             patch.object(module,'ensure_ideal_settings',return_value=(False,{'context_length':8192,'parallel':1},'CPU fixture')) as heal, \
             patch('llm_provider.make_llm_client',side_effect=create_client), \
             patch.object(gs,'process_chunk_adaptively',side_effect=single_work), \
             patch.object(tp,'instruct_batch',side_effect=instruct_work), \
             patch('attribution_adapter.check_adapter',return_value=(True,'')), \
             contextlib.redirect_stdout(io.StringIO()):
            module.main()
            self.assertEqual('k', heal.call_args.kwargs['api_key'])
        self.assertEqual(before,cfg_path.read_bytes())
        self.assertEqual(MODEL if override else 'configured-local',seen[0]['model_name'])
        self.assertEqual(cfg['llm_remote'],seen[1])
        self.assertEqual(cfg['llm_local']['provider_headers'],seen[0]['provider_headers'])
        self.assertTrue(output.is_file());self.assertEqual(TEXT,json.loads(output.read_text())[0]['text'])
        suffix='.generation_quality.json' if module is gs else '.threepass_manifest.json'
        record=json.loads(Path(str(output)+suffix).read_text())
        self.assertEqual('complete',record['status'])
        expected=MODEL if override else 'configured-local'
        self.assertIsInstance(record.get('model_binding'),dict,'published model binding is missing')
        self.assertEqual(expected,record['fingerprint']['model_binding']['primary_model'])
        self.assertEqual(expected,record['model_name'] if module is gs else record['fingerprint']['model_name'])
        self.assertEqual({'primary_model':expected,'failover_model':'configured-remote','failover_used':failover},record['model_binding'])
        self.assertEqual(['configured-remote' if failover else expected],requests)
        self.assertEqual(not failover,harness._is_reusable(str(output),expected))
        return output,record

    def test_both_actual_cli_entrypoints_publish_selected_model_and_keep_profile_settings(self):
        for module in (gs,tp):
            for override in (False,True):
                with self.subTest(module=module.__name__,override=override),tempfile.TemporaryDirectory() as tmp:
                    self.run_cli(module,Path(tmp),override)

    def test_both_cli_artifacts_expose_configured_failover_and_reject_mixed_model_reuse(self):
        for module in (gs,tp):
            with self.subTest(module=module.__name__),tempfile.TemporaryDirectory() as tmp:
                self.run_cli(module,Path(tmp),True,True)

    def test_harness_forwards_selected_model_to_both_actual_arm_argv_and_reuse_checks(self):
        for override,mixed in ((False,False),(True,False),(True,True)):
            with self.subTest(override=override,mixed=mixed),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);app=root/'app';app.mkdir();inputs=root/'inputs';inputs.mkdir()
                (inputs/'fixture.txt').write_text(TEXT);(app/'config.json').write_text(json.dumps(config()))
                out=root/'result.json';seen=[];reuse=[];selected=MODEL if override else 'configured-local'
                reusable=harness._is_reusable
                def run(cmd,**kwargs):
                    seen.append(cmd);path=Path(cmd[cmd.index('--output')+1]);path.write_text(json.dumps([{'speaker':'NARRATOR','text':TEXT}]))
                    Path(str(path)+'.generation_quality.json').write_text(json.dumps({'status':'complete','total_chunks':1,
                        'accepted_chunk_count':1,'model_name':selected,'model_binding':{'primary_model':selected,
                            'failover_model':'other','failover_used':mixed}}))
                    return SimpleNamespace(returncode=0)
                argv=['harness','--books','fixture','--inputs',str(inputs),'--work',str(root/'work'),'--out',str(out),'--reuse-complete']
                if override:argv+=['--model',MODEL]
                with patch.dict(os.environ,{'ALEXANDRIA_DATA_DIR':str(root)}),patch.object(harness,'REPO',str(root)), \
                     patch.object(harness,'APP',str(app)),patch.object(sys,'argv',argv), \
                     patch.object(harness,'load_gold',return_value=({'one':{'id':'one','text':TEXT,'expected_speaker':'NARRATOR'}},())), \
                     patch.object(harness,'_is_reusable',side_effect=lambda path,model:reuse.append(model) or reusable(path,model)), \
                     patch.object(harness.subprocess,'run',side_effect=run), \
                     patch('experiments.provenance.provenance',return_value={'fixture':'CPU'}),contextlib.redirect_stdout(io.StringIO()):
                    if mixed:
                        with self.assertRaises(SystemExit) as caught:harness.main()
                        self.assertEqual(3,caught.exception.code)
                    else:harness.main()
                self.assertEqual([selected]*(2 if mixed else 4),reuse)
                self.assertEqual(1 if mixed else 2,len(seen))
                for cmd in seen:self.assertIn('--model',cmd);self.assertEqual(selected,cmd[cmd.index('--model')+1])
                result=json.loads(out.read_text());self.assertEqual(selected,result['model_name'])
                if mixed:self.assertEqual([],result['results']);self.assertIn('selected model',result['failures'][0]['error'])

    def test_reuse_refuses_malformed_wrong_unbound_and_mixed_model_records(self):
        binding={'primary_model':MODEL,'failover_model':'other','failover_used':False}
        for suffix,base in (('.generation_quality.json',{'status':'complete','total_chunks':1,'accepted_chunk_count':1,'model_name':MODEL}),
            ('.threepass_manifest.json',{'status':'complete','progress':{'chunks_total':1,'chunks_completed':1},'fingerprint':{'model_name':MODEL},'diagnostic_failures':[]})):
            with tempfile.TemporaryDirectory() as tmp:
                out=Path(tmp)/'book.json';out.write_text('[]');record=Path(str(out)+suffix)
                for invalid in (None,[],42,{**base},{**base,'model_binding':[]},
                    {**base,'model_binding':{**binding,'primary_model':'wrong'}},
                    {**base,'model_binding':{**binding,'failover_used':True}},
                    {**base,'model_binding':{**binding,'failover_used':'false'}}):
                    with self.subTest(suffix=suffix,invalid=invalid):
                        record.write_text(json.dumps(invalid));self.assertFalse(harness._is_reusable(str(out),MODEL))
                record.write_text(json.dumps({**base,'model_binding':binding}));self.assertTrue(harness._is_reusable(str(out),MODEL))
                record.write_text(json.dumps({**base,'model_binding':{**binding,'failover_model':MODEL,'failover_used':True}}));self.assertTrue(harness._is_reusable(str(out),MODEL))

    def test_actual_overnight_chain_requires_exact_json_model_and_never_restores_stale_backup(self):
        replies=[({'data':[{'id':MODEL}]},0),({'data':[{'id':'qwen3-8b'}]},1),
            ({'data':[{'id':'qwen/qwen3-14b'}]},1),({'data':[{'not_id':MODEL}]},1),
            ({'data':MODEL},1),([MODEL],1),('not-json',1)]
        for payload,expected in replies:
            with self.subTest(payload=payload),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);app=root/'app';(app/'env/bin').mkdir(parents=True);(app/'env/bin/python').symlink_to(sys.executable)
                logs=root/'ab_test_runtime/logs';logs.mkdir(parents=True)
                (app/'config.json').write_text('current user config');(logs/'config.json.pre_qwen3_backup').write_text('stale backup')
                bin_dir=root/'bin';bin_dir.mkdir();reply=root/'reply';reply.write_text(payload if isinstance(payload,str) else json.dumps(payload))
                curl=bin_dir/'curl';curl.write_text('#!/bin/bash\ncat "$FIXTURE_REPLY"\n');curl.chmod(0o755)
                wrapper=root/'gpu_job.sh';wrapper.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >> "$FIXTURE_CALLS"\n');wrapper.chmod(0o755)
                source=(ROOT/'run_chains/overnight_2026_08_09b.sh').read_text().replace('REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git','REPO='+str(root))
                script=root/'chain.sh';script.write_text(source)
                result=subprocess.run(['bash',str(script)],capture_output=True,text=True,timeout=5,
                    env={**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'],'FIXTURE_REPLY':str(reply),'FIXTURE_CALLS':str(root/'calls')})
                self.assertEqual(expected,result.returncode,result.stdout+result.stderr)
                self.assertEqual('current user config',(app/'config.json').read_text())
                self.assertEqual('stale backup',(logs/'config.json.pre_qwen3_backup').read_text())
                if expected:self.assertFalse((root/'calls').exists())
                else:
                    calls=(root/'calls').read_text().splitlines();self.assertIn('--model',calls);self.assertEqual(MODEL,calls[calls.index('--model')+1])

    def test_model_binding_survives_real_checkpoint_resume_without_another_failover(self):
        for module in (gs,tp):
            with self.subTest(module=module.__name__),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source=root/'book.txt';source.write_text(TEXT)
                (root/'config.json').write_text(json.dumps(config()));out=root/'resume.json'
                binding={'primary_model':MODEL,'failover_model':'configured-remote','failover_used':True}
                if module is gs:
                    load=gs.load_generation_checkpoint
                    def seeded_load(path,fingerprint):
                        rows=[{'chunk_number':1,'source_sha256':fingerprint['chunk_sha256'][0],
                            'entries':[{'speaker':'NARRATOR','text':TEXT,'instruct':'Neutral.'}],
                            'quality':gs.validate_chunk_quality(TEXT,[{'speaker':'NARRATOR','text':TEXT,'instruct':'Neutral.'}]),
                            'model_binding':binding}]
                        gs.save_generation_checkpoint(path,fingerprint,rows)
                        return load(path,fingerprint)
                    patch_target='load_generation_checkpoint'
                else:
                    load=tp._load_three_pass_checkpoint
                    def seeded_load(path,fingerprint,chunk_count=None):
                        row={'speaker':'NARRATOR','text':TEXT,'instruct':'Neutral.'}
                        tp._save_three_pass_checkpoint(path,fingerprint,'instruct',
                            [{'type':'NARRATOR','text':TEXT}],1,[row],[row],['quotes'],model_binding=binding)
                        return load(path,fingerprint,chunk_count)
                    patch_target='_load_three_pass_checkpoint'
                def provider(profile,timeout):return SimpleNamespace()
                with patch.dict(os.environ,{'ALEXANDRIA_DATA_DIR':str(root)}), \
                    patch.object(sys,'argv',['cli',str(source),'--output',str(out),'--model',MODEL]), \
                    patch.object(module,'ensure_ideal_settings',return_value=(False,{'context_length':8192,'parallel':1},'CPU fixture')) as heal, \
                    patch('llm_provider.make_llm_client',side_effect=provider), \
                    patch.object(module,patch_target,side_effect=seeded_load), \
                    patch('attribution_adapter.check_adapter',return_value=(True,'')),contextlib.redirect_stdout(io.StringIO()):
                    module.main()
                suffix='.generation_quality.json' if module is gs else '.threepass_manifest.json'
                record=json.loads(Path(str(out)+suffix).read_text())
                self.assertEqual('complete',record['status']);self.assertEqual(binding,record['model_binding'])
                self.assertEqual(TEXT,json.loads(out.read_text())[0]['text'])
                self.assertFalse(harness._is_reusable(str(out),MODEL))

    def test_real_child_cli_runs_provider_requests_and_publishes_matching_artifacts(self):
        driver = """
import contextlib,json,os,runpy,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import lmstudio_settings, llm_provider, attribution_adapter, generate_script
root=Path(os.environ['ALEXANDRIA_DATA_DIR'])
text=(root/'book.txt').read_text()
script=sys.argv[1]
def factory(profile,timeout):
    def create(**kwargs):
        with (root/'requests.jsonl').open('a') as handle:handle.write(json.dumps({'model':kwargs['model'],'profile':profile})+'\\n')
        rows=([{'n':0,'instruct':'Neutral.'}] if script.endswith('three_pass_generate.py')
              else [{'speaker':'NARRATOR','text':text,'instruct':'Neutral.'}])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(rows)),finish_reason='stop')],usage=None)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
sys.argv=sys.argv[1:]
with patch.object(lmstudio_settings,'ensure_ideal_settings',return_value=(False,{'context_length':8192,'parallel':1},'CPU fixture')), \
     patch.object(llm_provider,'make_llm_client',side_effect=factory), \
     patch.object(attribution_adapter,'check_adapter',return_value=(True,'')), \
     patch.object(generate_script,'get_response_log_path',side_effect=lambda name:str(root/name)):
    runpy.run_path(script,run_name='__main__')
"""
        for name in ('generate_script.py','three_pass_generate.py'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'book.txt').write_text(TEXT);cfg=root/'config.json';cfg.write_text(json.dumps(config()));before=cfg.read_bytes()
                output=root/'result.json'
                result=subprocess.run([sys.executable,'-c',driver,str(ROOT/'app'/name),str(root/'book.txt'),
                    '--output',str(output),'--model',MODEL],cwd=ROOT,capture_output=True,text=True,timeout=15,
                    env={**os.environ,'PYTHONPATH':str(ROOT/'app')+os.pathsep+str(ROOT),'ALEXANDRIA_DATA_DIR':str(root)})
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                requests=[json.loads(row) for row in (root/'requests.jsonl').read_text().splitlines()]
                self.assertEqual([MODEL],[row['model'] for row in requests])
                self.assertEqual(MODEL,requests[0]['profile']['model_name'])
                self.assertEqual(before,cfg.read_bytes());self.assertEqual(TEXT,json.loads(output.read_text())[0]['text'])
                self.assertTrue(harness._is_reusable(str(output),MODEL))
