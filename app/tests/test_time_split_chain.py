"""Copied time-split driver, PCM/PEFT caches and CPU producers under private Git."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from experiments import ljspeech_completion as completion
from tests.test_support import write_test_adapter

REPO=Path(__file__).resolve().parents[2]


class TimeSplitChainTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='time split fixture ');self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.work=self.root/'ab_test_runtime/time_split/warm_baritone_30s_m_1';self.data=self.work/'data'
        for relative in ('train','val'):(self.data/relative).mkdir(parents=True)
        audio=np.sin(np.arange(48000)*.03).astype(np.float32)*.1
        for relative in ('ref.wav','train/train.wav','val/held.wav'):sf.write(self.data/relative,audio,24000)
        (self.data/'ref_text.txt').write_text('Reference text.')
        for split,name in (('train','train'),('val','held')):(self.data/split/'metadata.jsonl').write_text(json.dumps({'audio_filepath':split+'/'+name+'.wav','text':name+' text.','duration':2})+'\n')
        write_test_adapter(self.work/'adapter')
        self.build=self.work/'build.json'
        self.build_doc={'target_rate':24000,'ref_sample':str((self.data/'ref.wav').relative_to(self.root)),'ref_text':'Reference text.','ref_source_id':'ref','test_books':['warm_baritone_30s_m_1'],
            'test':[{'id':'held','book':'warm_baritone_30s_m_1','text':'held text.','seconds':2,'human_wav':str((self.data/'val/held.wav').relative_to(self.root))}]}
        self.build.write_text(json.dumps(self.build_doc));(self.root/'build-template.json').write_text(json.dumps(self.build_doc))
        self.gate=self.work/'stop_check/verify_adapter_stops.json';self.gate.parent.mkdir();self.gate.write_text('{"passed":true}')
        self.generated=self.root/'ab_test_runtime/experiments/time_split__warm_baritone_30s_m_1_generate.json';self.generated.parent.mkdir(parents=True)
        row=dict(self.build_doc['test'][0]);row['human_seconds']=row.pop('seconds')
        for arm in ('lora','clone'):
            wav=self.work/('held__'+arm+'.wav');sf.write(wav,audio,24000);row[arm+'_wav']=str(wav.relative_to(self.root))
        self.gen_doc={'arms':['lora','clone'],'seed':1234,'rows':[row],'failures':[]};self.generated.write_text(json.dumps(self.gen_doc));(self.root/'generation-template.json').write_text(json.dumps(self.gen_doc))
        (self.root/'run_chains/lib').mkdir(parents=True);(self.root/'app/experiments').mkdir(parents=True)
        for relative in ('run_chains/lib/stage.sh','run_chains/lib/server_cleanup.sh','run_chains/lib/queue.sh','app/experiments/ljspeech_completion.py','app/experiments/stop_gate_completion.py','app/adapter_artifacts.py'):shutil.copyfile(REPO/relative,self.root/relative)
        source=Path(os.environ.get('TIME_SPLIT_SOURCE',str(REPO/'run_chains/private_narrator_time_split_20260913.sh')));shutil.copyfile(source,self.root/'run_chains/chain.sh')
        config=self.root/'app/config.json';config.write_text('{"fixture":true}')
        queue=self.root/'gpu_job.sh';queue.write_text('#!/bin/bash\nshift\nexec "$@"\n');queue.chmod(0o755)
        worker='''import json,os,pathlib,sys
r=pathlib.Path(os.environ['FIXTURE_ROOT']);name=pathlib.Path(__file__).name
with (r/'calls').open('a') as f:f.write(name+'\\n')
if name!='prosody_fidelity.py':raise SystemExit(int(os.environ.get('FIXTURE_RC','7')))
(r/'prosody_args.json').write_text(json.dumps(sys.argv))
p=pathlib.Path(sys.argv[sys.argv.index('--out')+1]);p.write_text('{}')
'''
        (self.root/'app/train_lora.py').write_text(worker)
        for name in ('library_eval_build.py','verify_adapter_stops.py','ljspeech_generate.py','prosody_fidelity.py'):(self.root/'app/experiments'/name).write_text(worker)
        self.env=dict(os.environ,FIXTURE_ROOT=str(self.root),PYTHON=sys.executable,CONFIG=str(config),PYTHONPATH=str(REPO/'app'))
        for args in (('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.com'),('config','core.hooksPath',str(self.root/'no-hooks')),('add','-A'),('commit','-q','-m','fixture baseline')):subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)

    def run_chain(self,**env):
        configured=dict(self.env,**env)
        configured={key:value for key,value in configured.items() if value is not None}
        result=subprocess.run(['bash',str(self.root/'run_chains/chain.sh')],cwd=self.root.parent,env=configured,capture_output=True,text=True,timeout=30)
        marker=self.root/'calls';return result,marker.read_text().splitlines() if marker.exists() else []

    def test_bad_explicit_environment_refuses_before_any_worker(self):
        for env in ({'PYTHON':str(self.root/'missing-python')},{'PYTHON':''},{'CONFIG':str(self.root/'missing-config')},{'CONFIG':''}):
            with self.subTest(env=env):
                result,calls=self.run_chain(**env)
                self.assertEqual(1,result.returncode,result.stdout+result.stderr);self.assertEqual([],calls)
                self.assertIn('REFUSING: configured',result.stderr)

    def test_unset_environment_uses_checkout_defaults(self):
        import shlex
        interpreter=self.root/'app/env/bin/python';interpreter.parent.mkdir(parents=True)
        interpreter.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n');interpreter.chmod(0o755)
        result,calls=self.run_chain(PYTHON=None,CONFIG=None)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(['prosody_fidelity.py'],calls)

    def test_valid_all_stage_caches_remain_unchanged_and_skip_to_prosody(self):
        paths=[self.build,self.gate,self.generated,self.work/'adapter/adapter_model.safetensors'];before={p:p.read_bytes() for p in paths}
        result,calls=self.run_chain();self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(['prosody_fidelity.py'],calls);self.assertEqual(before,{p:p.read_bytes() for p in paths})

    def test_partial_nonempty_caches_and_zero_exit_incomplete_outputs_do_not_reach_later_work(self):
        cases=((self.work/'adapter/adapter_model.safetensors',b'broken weights','train_lora.py'),(self.build,b'{}','library_eval_build.py'),(self.gate,b'{"passed":false}','verify_adapter_stops.py'),(self.generated,b'{}','ljspeech_generate.py'))
        for path,damage,worker in cases:
            for rc in ('7','0'):
                with self.subTest(stage=worker,rc=rc):
                    before=path.read_bytes();marker=self.root/'calls'
                    if marker.exists():marker.unlink()
                    try:
                        path.write_bytes(damage);result,calls=self.run_chain(FIXTURE_RC=rc)
                        self.assertEqual(1,result.returncode,result.stdout+result.stderr);self.assertEqual([worker],calls)
                        self.assertEqual(damage,path.read_bytes())
                    finally:path.write_bytes(before)

    def test_actual_library_builder_artifact_is_accepted_and_short_generation_is_rejected(self):
        spec=importlib.util.spec_from_file_location('native_library_build',REPO/'app/experiments/library_eval_build.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.REPO=str(self.root)
        with patch.object(sys,'argv',['library_eval_build','--dataset',str(self.data),'--out',str(self.build)]):module.main()
        completion.validate_library_build(self.build,self.root,self.data)
        completion.validate_generation(self.generated,self.root,self.build)
        wave=self.data/'val/held.wav';before_wave=wave.read_bytes();wave.unlink()
        with self.assertRaisesRegex(ValueError,'missing held-out audio'):
            completion.validate_library_build(self.build,self.root,self.data)
        wave.write_bytes(before_wave)
        for doc in (dict(self.gen_doc,rows=[]),dict(self.gen_doc,failures=[{'id':'held'}]),dict(self.gen_doc,arms=['lora'])):
            with self.subTest(doc=doc):
                self.generated.write_text(json.dumps(doc));before=self.generated.read_bytes()
                with self.assertRaises(ValueError):completion.validate_generation(self.generated,self.root,self.build)
                self.assertEqual(before,self.generated.read_bytes())


class ValidatedStageDependencyTests(unittest.TestCase):
    def test_failed_prerequisite_prevents_even_cached_checker_and_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);checker=root/'checker';worker=root/'worker'
            body='source "$1"\nSTAGE_LOG_DIR="$2/logs"\nSTAGE_RESULT[earlier]=failed:7\nrun_validated_cached_stage later 0 bash -c \'touch "$1"\' check "$2/checker" -- --requires-ok earlier -- bash -c \'touch "$1"\' worker "$2/worker"\n'
            result=subprocess.run(['bash','-c',body,'fixture',str(REPO/'run_chains/lib/stage.sh'),str(root)],capture_output=True,text=True,timeout=5)
            self.assertEqual(1,result.returncode,result.stdout+result.stderr);self.assertFalse(checker.exists());self.assertFalse(worker.exists())
