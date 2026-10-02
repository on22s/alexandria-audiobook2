"""Private checkout, real package/Git operations, CPU workers; no inference."""
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[2]


class PruneChainTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='prune chain ');self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        for folder in ('run_chains/lib','app/env/bin','app/experiments','tools/voice_lab','lora_models','ab_test_runtime/experiments'):(self.root/folder).mkdir(parents=True)
        for name in ('stage.sh','server_cleanup.sh','queue.sh'):shutil.copyfile(REPO/'run_chains/lib'/name,self.root/'run_chains/lib'/name)
        source=Path(os.environ.get('PRUNE_CHAIN_SOURCE',str(REPO/'run_chains/prune_retrain_20260913.sh'))).read_text()
        if os.environ.get('PRUNE_ISOLATE_BASELINE'):
            literal=re.search(r'(?m)^MAIN=(["\x27]?)([^$\n]+)\1$',source)
            self.assertIsNotNone(literal, 'baseline must expose its actual MAIN directory')
            source=source.replace(literal.group(2),str(self.root))
            source=source.replace('MAIN='+str(self.root), 'MAIN='+shlex.quote(str(self.root)))
        (self.root/'run_chains/prune_retrain_20260913.sh').write_text(source)
        archive=self.root/'source.zip';archive.write_bytes(b'CPU fixture control archive')
        (self.root/'lora_models/manifest.json').write_text(json.dumps([{'id':'voice','zip_source':str(archive)}]))
        self.write('gpu_job.sh','#!/bin/bash\necho "$1" >> "$FIXTURE_ROOT/jobs"\nshift\nexec "$@"\n')
        self.write('app/env/bin/python','#!/bin/sh\nif [ "$1" = - ]; then shift; exec '+shlex.quote(sys.executable)+' "$FIXTURE_ROOT/read_manifest.py" "$@"; fi\nexec '+shlex.quote(sys.executable)+' "$@"\n')
        self.write('read_manifest.py', '''import os,pathlib,sys
root=pathlib.Path(os.environ['FIXTURE_ROOT'])
def inspect(event,args):
 if event=='open' and isinstance(args[0],str) and args[0].endswith('lora_models/manifest.json'):
  with (root/'manifest_read').open('a') as f:f.write(args[0]+'\\n')
  if pathlib.Path(args[0]).resolve()!=root/'lora_models/manifest.json':raise PermissionError('refusing another checkout manifest')
sys.addaudithook(inspect)
sys.argv=['-',*sys.argv[1:]]
exec(compile(sys.stdin.read(),'<native chain manifest reader>','exec'))
''')
        self.write('app/experiments/prune_prosodic_clips.py', '''import json,os,pathlib,sys
r=pathlib.Path(os.environ['FIXTURE_ROOT']);args=sys.argv
(r/'prune_args.json').write_text(json.dumps(args))
if os.environ.get('FIXTURE_FAIL')=='prune':raise SystemExit(7)
p=pathlib.Path(args[args.index('--out')+1]);p.mkdir(parents=True,exist_ok=True)
for split in ('train','val'):
 (p/split).mkdir(exist_ok=True);(p/split/'metadata.jsonl').write_text('{}\\n');(p/split/'clip.wav').write_bytes(b'CPU fixture')
(p/'metadata.jsonl').write_text('{}\\n');(p/'prune_report.json').write_text('{}')
''')
        self.write('tools/voice_lab/batch_train_lora.py', '''import os,pathlib,sys
r=pathlib.Path(os.environ['FIXTURE_ROOT']);(r/'trained').touch();a=sys.argv;p=pathlib.Path(a[a.index('--manifest')+1]);p.write_text('[]')
raise SystemExit(8 if os.environ.get('FIXTURE_FAIL')=='train' else 0)
''')
        self.write('app/experiments/library_voice_fidelity.py', '''import os,pathlib,sys
r=pathlib.Path(os.environ['FIXTURE_ROOT']);(r/'scored').touch();a=sys.argv;pathlib.Path(a[a.index('--out')+1]).write_text('{"CPU_fixture":true}')
''')
        for args in (('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.com'),('config','core.hooksPath',str(self.root/'no-hooks')),('add','-A'),('commit','-q','-m','base')):subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
        self.work=self.root/'ab_test_runtime/prune_retrain_20260913/voice'
        self.env=dict(os.environ,FIXTURE_ROOT=str(self.root),ADAPTER='voice')
        self.env.pop('PYTHON',None)

    def write(self,name,source):
        p=self.root/name;p.write_text(source);p.chmod(0o755)

    def run_chain(self,**env):
        return subprocess.run(['bash',str(self.root/'run_chains/prune_retrain_20260913.sh')],cwd=self.root.parent,env=dict(self.env,**env),capture_output=True,text=True,timeout=15)

    def test_selected_checkout_manifest_and_whisper_paths_are_used(self):
        result=self.run_chain();self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(str(self.root/'lora_models/manifest.json'),(self.root/'manifest_read').read_text().strip())
        args=json.loads((self.root/'prune_args.json').read_text())
        for key,relative in (('--whisper-cpp-bin','whisper.cpp/build/bin/whisper-cli'),('--whisper-cpp-model','whisper.cpp/models/ggml-base.en.bin')):self.assertEqual(str(self.root/relative),args[args.index(key)+1])
        self.assertTrue((self.root/'trained').exists());self.assertTrue((self.root/'scored').exists())

    def test_failed_prune_blocks_packaging_training_and_fidelity(self):
        result=self.run_chain(FIXTURE_FAIL='prune');self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertFalse((self.work/'zips/source_pruned.zip').exists());self.assertFalse((self.root/'trained').exists());self.assertFalse((self.root/'scored').exists())
        self.assertIn('package_pruned = skipped',result.stdout)

    def test_failed_training_does_not_score_even_with_new_partial_manifest(self):
        result=self.run_chain(FIXTURE_FAIL='train');self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertTrue((self.work/'models/manifest.json').exists());self.assertFalse((self.root/'scored').exists());self.assertIn('fidelity = skipped',result.stdout)

    def test_cached_report_without_metadata_fails_package_and_blocks_training(self):
        p=self.work/'pruned';p.mkdir(parents=True);(p/'prune_report.json').write_text('{}')
        result=self.run_chain();self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertFalse((self.root/'trained').exists());self.assertFalse((self.root/'scored').exists());self.assertIn('package_pruned = failed:',result.stdout)

    def test_invalid_explicit_interpreter_refuses_instead_of_using_live_checkout(self):
        result=self.run_chain(PYTHON=str(self.root/'missing-python'));self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertIn('configured PYTHON is not executable',result.stderr);self.assertFalse((self.root/'manifest_read').exists())

    def test_repeat_preserves_existing_prune_and_package_caches(self):
        first=self.run_chain();self.assertEqual(0,first.returncode,first.stdout+first.stderr)
        paths=[self.root/'prune_args.json',self.work/'zips/source_control.zip',self.work/'zips/source_pruned.zip']
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in paths}
        second=self.run_chain();self.assertEqual(0,second.returncode,second.stdout+second.stderr)
        self.assertIn('SKIP prune (cached report)',second.stdout)
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in paths})

    def test_control_copy_failure_blocks_training_even_when_pruned_package_succeeds(self):
        folder=self.root/'bin';folder.mkdir()
        self.write('bin/cp','#!/bin/sh\nexit 9\n')
        result=self.run_chain(PATH=str(folder)+os.pathsep+os.environ['PATH'])
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertIn('package_control = failed:9',result.stdout)
        self.assertTrue((self.work/'zips/source_pruned.zip').exists())
        self.assertFalse((self.root/'trained').exists());self.assertFalse((self.root/'scored').exists())
