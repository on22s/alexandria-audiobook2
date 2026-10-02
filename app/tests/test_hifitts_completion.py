"""Native guard CLI, known PCM and PEFT files; copied chain with CPU workers."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import soundfile as sf
from unittest.mock import patch

from experiments import ljspeech_completion as completion
from tests.hifitts_fixture import write_hifitts_fixture

REPO=Path(__file__).resolve().parents[2]


class HiFiTTSCompletionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='hifitts fixture ');self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.corpus=self.root/'ab_test_runtime/corpora/hifitts/9017';self.work=self.root/'ab_test_runtime/hifitts_9017_eval'
        self.split,self.build=write_hifitts_fixture(self.root,self.corpus,self.work)
        (self.root/'run_chains/lib').mkdir(parents=True);(self.root/'app/experiments').mkdir(parents=True)
        for relative in ('run_chains/lib/stage.sh','run_chains/lib/server_cleanup.sh','app/experiments/ljspeech_completion.py','app/experiments/stop_gate_completion.py','app/adapter_artifacts.py'):shutil.copyfile(REPO/relative,self.root/relative)
        source=Path(os.environ.get('HIFITTS_SOURCE',str(REPO/'run_chains/hifitts_9017_20260913.sh')));shutil.copyfile(source,self.root/'run_chains/chain.sh')
        gate=self.work/'stop_check/verify_adapter_stops.json';gate.parent.mkdir();gate.write_text('{"passed":true}')
        queue=self.root/'gpu_job.sh';queue.write_text('#!/bin/bash\nshift\nexec "$@"\n');queue.chmod(0o755)
        worker='''import json,os,pathlib,sys
r=pathlib.Path(os.environ['FIXTURE_ROOT']);name=pathlib.Path(__file__).name
with (r/'calls').open('a') as f:f.write(name+'\\n')
if name in ('ljspeech_prepare.py','ljspeech_build.py','train_lora.py'):raise SystemExit(int(os.environ.get('FIXTURE_RC','7')))
p=pathlib.Path(sys.argv[sys.argv.index('--out')+1]);p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
'''
        for name in ('ljspeech_prepare.py','ljspeech_build.py','ljspeech_generate.py','prosody_fidelity.py','ljspeech_score.py','verify_adapter_stops.py'):(self.root/'app/experiments'/name).write_text(worker)
        (self.root/'app/train_lora.py').write_text(worker)
        config=self.root/'app/config.json';config.write_text('{}');self.env=dict(os.environ,FIXTURE_ROOT=str(self.root),PYTHON=sys.executable,CONFIG=str(config))
        for args in (('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.com'),('config','core.hooksPath',str(self.root/'no-hooks')),('add','-A'),('commit','-q','-m','fixture baseline')):subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)

    def run_chain(self,**env):
        result=subprocess.run(['bash',str(self.root/'run_chains/chain.sh')],cwd=self.root.parent,env=dict(self.env,**env),capture_output=True,text=True,timeout=30)
        marker=self.root/'calls';return result,marker.read_text().splitlines() if marker.exists() else []

    def test_valid_cache_reuse_preserves_split_build_adapter_and_dispatches_generation(self):
        paths=[self.corpus/'split.json',self.work/'build.json',self.work/'adapter/adapter_model.safetensors']
        before={p:p.read_bytes() for p in paths};result,calls=self.run_chain()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(['ljspeech_generate.py','prosody_fidelity.py','ljspeech_score.py'],calls)
        self.assertEqual(before,{p:p.read_bytes() for p in paths})

    def test_nonempty_partial_split_build_and_weights_never_bypass_required_workers(self):
        for kind,path in (('prepare',self.corpus/'split.json'),('build',self.work/'build.json'),('train',self.work/'adapter/adapter_model.safetensors')):
            with self.subTest(stage=kind):
                before=path.read_bytes();marker=self.root/'calls'
                if marker.exists():marker.unlink()
                try:
                    path.write_bytes(b'{}' if kind!='train' else b'truncated tensors')
                    result,calls=self.run_chain()
                    self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                    expected={'prepare':'ljspeech_prepare.py','build':'ljspeech_build.py','train':'train_lora.py'}[kind]
                    self.assertEqual([expected],calls)
                    self.assertNotIn('ljspeech_generate.py',calls)
                finally:path.write_bytes(before)

    def test_success_exit_with_incomplete_output_blocks_all_following_work(self):
        for kind,path in (('prepare',self.corpus/'split.json'),('build',self.work/'build.json'),('train',self.work/'adapter/adapter_model.safetensors')):
            with self.subTest(stage=kind):
                before=path.read_bytes();marker=self.root/'calls'
                if marker.exists():marker.unlink()
                try:
                    path.write_bytes(b'{}' if kind!='train' else b'truncated tensors')
                    result,calls=self.run_chain(FIXTURE_RC='0')
                    self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                    self.assertEqual([{'prepare':'ljspeech_prepare.py','build':'ljspeech_build.py','train':'train_lora.py'}[kind]],calls)
                    self.assertIn('did not publish a complete output',result.stdout)
                finally:path.write_bytes(before)

    def test_rows_metadata_and_audio_damage_are_rejected_without_mutating_inputs(self):
        completion.validate_split(self.corpus/'split.json',self.root,self.corpus,self.split['test_books'])
        completion.validate_build(self.work/'build.json',self.root,self.corpus/'split.json')
        path=self.work/'build.json';before=path.read_bytes()
        for document in (dict(self.build,test=self.build['test'][:1]),dict(self.build,ref_source_id='heldout'),dict(self.build,target_rate=22050)):
            with self.subTest(document=document):
                path.write_text(json.dumps(document));raw=path.read_bytes()
                with self.assertRaises(ValueError):completion.validate_build(path,self.root,self.corpus/'split.json')
                self.assertEqual(raw,path.read_bytes())
        path.write_bytes(before)
        wav=self.work/'human'/ (self.split['test'][0]['id']+'.wav');wav.write_bytes(b'truncated WAV')
        with self.assertRaises(sf.LibsndfileError):completion.validate_build(path,self.root,self.corpus/'split.json')

    def test_actual_prepare_and_build_producers_publish_artifacts_accepted_by_validator(self):
        for name,args in (('ljspeech_prepare',['--root',str(self.corpus),'--out',str(self.corpus/'split.json'),'--test-books',*self.split['test_books']]),('ljspeech_build',['--split',str(self.corpus/'split.json'),'--out',str(self.work)])):
            spec=importlib.util.spec_from_file_location('native_'+name,REPO/'app/experiments'/ (name+'.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            module.REPO=str(self.root)
            with patch.object(sys,'argv',[name,*args]):module.main()
        completion.validate_split(self.corpus/'split.json',self.root,self.corpus,self.split['test_books'])
        completion.validate_build(self.work/'build.json',self.root,self.corpus/'split.json')

    def test_missing_adapter_validator_dependency_refuses_before_training_dispatch(self):
        import shlex
        shim=self.root/'missing_dependency_python'
        checker=str(self.root/'app/experiments/ljspeech_completion.py')
        shim.write_text('#!/bin/sh\nif [ "$1" = '+shlex.quote(checker)+' ] && [ "$2" = train ]; then exec '+shlex.quote(sys.executable)+' -S "$@"; fi\nexec '+shlex.quote(sys.executable)+' "$@"\n')
        shim.chmod(0o755)
        result,calls=self.run_chain(PYTHON=str(shim))
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertEqual([],calls)
        self.assertIn('CANNOT VALIDATE',result.stderr)
        self.assertIn('REFUSING: cannot validate train cache',result.stdout)
