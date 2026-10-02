"""Launcher descriptors execute their real prerequisite expressions in Node."""
import json
import hashlib
import importlib.util
import os
import shutil
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def get_descriptor(path):
    result=subprocess.run(['node','-e','process.stdout.write(JSON.stringify(require(process.argv[1])))',str(path)],capture_output=True,text=True,check=True)
    return json.loads(result.stdout)


class LauncherLlamaAssetTests(unittest.TestCase):
    def test_fresh_app_install_provisions_the_same_backend_setup_as_preparer(self):
        install=get_descriptor(ROOT/'install.js')
        calls=[step for step in install['run'] if step['method']=='script.start' and step['params']['uri']=='llama_cpp.js']
        self.assertEqual(2,len(calls),'app and AMD preparer must share one llama_cpp installer')
        app=next(step for step in calls if step['params']['params'].get('path')=='app')
        self.assertEqual('env',app['params']['params']['venv']);self.assertNotIn('when',app)
        prep=next(step for step in calls if step is not app)
        self.assertEqual('preparer_env',prep['params']['params']['venv']);self.assertIn("gpu === 'amd'",prep['when'])
        child=get_descriptor(ROOT/'llama_cpp.js')
        for platform,gpu in (('linux','amd'),('linux','nvidia'),('win32','nvidia'),('darwin','apple'),('linux','cpu'),('win32','amd')):
            script="""const d=require(process.argv[1]);const platform=process.argv[2],gpu=process.argv[3];const out=d.run.filter(s=>!s.when||eval(s.when.slice(2,-2)));process.stdout.write(JSON.stringify(out));"""
            run=subprocess.run(['node','-e',script,str(ROOT/'llama_cpp.js'),platform,gpu],capture_output=True,text=True,check=True)
            active=json.loads(run.stdout);self.assertEqual(1,len(active),(platform,gpu))
            self.assertIn('llama-cpp-python[server]==0.3.23',active[0]['params']['message'])
            self.assertIn('--no-binary llama-cpp-python',active[0]['params']['message'])
            intended='HIP' if (platform,gpu)==('linux','amd') else 'CUDA' if gpu=='nvidia' else 'METAL' if platform=='darwin' else None
            flags=str(active[0]['params'].get('env',{}))+active[0]['params']['message']
            if intended:self.assertIn('GGML_'+intended+'=ON',flags)

    def test_normal_install_without_sibling_passes_and_missing_prerequisites_still_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'app/env').mkdir(parents=True);(root/'model.gguf').write_bytes(b'fixture')
            script="""const fs=require('fs'),path=require('path'),d=require(process.argv[1]);const root=process.argv[2],args={model:process.argv[3]};const exists=p=>fs.existsSync(path.resolve(root,p));const out=d.run.filter(s=>s.method==='script.return').map(s=>eval(s.when.slice(2,-2)));process.stdout.write(JSON.stringify(out));"""
            def abort(model):
                result=subprocess.run(['node','-e',script,str(ROOT/'start_llm.js'),str(root),model],capture_output=True,text=True,check=True)
                return json.loads(result.stdout)[0]
            self.assertFalse(abort('model.gguf'));self.assertTrue(abort('missing.gguf'))
            (root/'app/env').rmdir();self.assertTrue(abort('model.gguf'))
        shell=next(step for step in get_descriptor(ROOT/'start_llm.js')['run'] if step['method']=='shell.run')
        self.assertEqual('app/env',shell['params']['venv'])

    def test_model_value_is_an_argument_in_the_documented_structured_command(self):
        shell=next(step for step in get_descriptor(ROOT/'start_llm.js')['run'] if step['method']=='shell.run')
        command=shell['params']['message'];self.assertIsInstance(command,dict)
        self.assertEqual(['python','-m','llama_cpp.server'],command['_'][:3])
        self.assertIn('--model={{args.model}}',command['_'])
        self.assertIn('--port',command['_']);self.assertIn('{{local.port}}',command['_'])
        self.assertNotIn('args.model',str(shell['params'].get('env',{})))

    def test_whisper_pins_are_shared_and_checked_before_any_build_or_success_notice(self):
        self.assertTrue((ROOT/'whisper_assets.json').is_file(),'source and model require immutable pins')
        assets=json.loads((ROOT/'whisper_assets.json').read_text())
        self.assertEqual(40,len(assets['source_commit']));self.assertEqual(40,len(assets['model_revision']));self.assertEqual(64,len(assets['model_sha256']))
        steps=get_descriptor(ROOT/'install.js')['run']
        download=next(step for step in steps if step['method']=='fs.download')
        self.assertIn('/resolve/'+assets['model_revision']+'/',download['params']['url']);self.assertNotIn('/main/',download['params']['url'])
        verify=next(i for i,s in enumerate(steps) if 'verify_whisper_assets.py' in str(s.get('params',{})))
        builds=[i for i,s in enumerate(steps) if 'cmake' in str(s.get('params',{}))]
        self.assertTrue(builds);self.assertTrue(all(verify<i for i in builds))
        self.assertLess(verify,next(i for i,s in enumerate(steps) if s['method']=='notify'))

    def test_selected_build_commands_reach_uv_with_backend_and_constraints_in_native_bash(self):
        for platform,gpu,flag in (('linux','amd','HIP'),('linux','nvidia','CUDA'),('darwin','apple','METAL'),('linux','cpu',None)):
            with self.subTest(platform=platform,gpu=gpu),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);binary=root/'bin';binary.mkdir()
                uv=binary/'uv';uv.write_text('#!'+sys.executable+'\nimport json,os,sys;print(json.dumps({"args":sys.argv[1:],"cmake":os.environ.get("CMAKE_ARGS"),"uv_constraint":os.environ.get("UV_CONSTRAINT"),"pip_constraint":os.environ.get("PIP_CONSTRAINT")}))\n');uv.chmod(0o700)
                rocminfo=binary/'rocminfo';rocminfo.write_text('#!/bin/sh\nprintf "Name: gfx1201\\nName: gfx1100\\n"\n');rocminfo.chmod(0o700)
                script="const d=require(process.argv[1]),platform=process.argv[2],gpu=process.argv[3];process.stdout.write(JSON.stringify(d.run.find(s=>eval(s.when.slice(2,-2))).params));"
                selected=json.loads(subprocess.check_output(['node','-e',script,str(ROOT/'llama_cpp.js'),platform,gpu],text=True))
                env=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH'])
                env.update({k:v.replace('{{args.constraints}}','retained-constraints.txt') for k,v in selected['env'].items()})
                result=subprocess.run(['bash','-c',selected['message']],cwd=root,env=env,capture_output=True,text=True,check=True)
                captured=json.loads(result.stdout)
                self.assertEqual(['pip','install','llama-cpp-python[server]==0.3.23','--no-binary','llama-cpp-python'],captured['args'])
                self.assertEqual('retained-constraints.txt',captured['uv_constraint']);self.assertEqual(captured['uv_constraint'],captured['pip_constraint'])
                if flag:self.assertIn('GGML_'+flag+'=ON',captured['cmake'])
                else:self.assertEqual('-DGGML_HIP=OFF -DGGML_CUDA=OFF -DGGML_METAL=OFF',captured['cmake'])
                if flag=='HIP':self.assertIn('AMDGPU_TARGETS=gfx1201',captured['cmake']);self.assertNotIn('gfx1100',captured['cmake'])


class WhisperAssetVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('tested_whisper_verifier',ROOT/'tools/verify_whisper_assets.py')
        cls.verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.verifier)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source=self.root/'whisper.cpp';self.source.mkdir();self.tracked=self.source/'source.c';self.tracked.write_bytes(b'known tracked source bytes\n')
        subprocess.run(['git','init','-q',str(self.source)],check=True)
        subprocess.run(['git','-C',str(self.source),'add','source.c'],check=True)
        subprocess.run(['git','-C',str(self.source),'-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-qm','fixture'],check=True)
        self.commit=subprocess.check_output(['git','-C',str(self.source),'rev-parse','HEAD'],text=True).strip()
        self.model=self.root/'models/whisper.cpp/ggml-small.en.bin';self.model.parent.mkdir(parents=True)
        self.content=b'known model fixture bytes'*(1024*100);self.model.write_bytes(self.content)
        self.assets={'source_commit':self.commit,'model_filename':self.model.name,'model_sha256':hashlib.sha256(self.content).hexdigest()}
        (self.root/'whisper_assets.json').write_text(json.dumps(self.assets))

    def test_valid_full_streamed_bytes_and_tracked_source_are_verified_without_mutation(self):
        before=self.model.read_bytes();result=self.verifier.verify_whisper_assets(self.root)
        self.assertEqual(self.assets['source_commit'],result['source_commit']);self.assertEqual(self.assets['model_sha256'],result['model_sha256'])
        self.assertEqual(before,self.model.read_bytes());self.assertEqual(b'known tracked source bytes\n',self.tracked.read_bytes())

    def test_wrong_commit_and_dirty_tracked_source_are_rejected_before_success(self):
        self.assets['source_commit']='0'*40;(self.root/'whisper_assets.json').write_text(json.dumps(self.assets))
        with self.assertRaisesRegex(ValueError,'commit differs'):self.verifier.verify_whisper_assets(self.root)
        self.assets['source_commit']=self.commit;(self.root/'whisper_assets.json').write_text(json.dumps(self.assets));self.tracked.write_bytes(b'altered tracked source')
        with self.assertRaisesRegex(ValueError,'tracked changes'):self.verifier.verify_whisper_assets(self.root)

    def test_empty_truncated_and_tail_corrupted_model_bytes_are_rejected_and_preserved(self):
        for content in (b'',self.content[:-1],self.content[:-1]+b'!'):
            with self.subTest(length=len(content)):
                self.model.write_bytes(content)
                with self.assertRaisesRegex(ValueError,'checksum mismatch'):self.verifier.verify_whisper_assets(self.root)
                self.assertEqual(content,self.model.read_bytes(),'validation must not delete or silently replace user bytes')

    def test_missing_model_fails_and_cli_exits_nonzero_on_corruption(self):
        self.model.unlink()
        with self.assertRaises(FileNotFoundError):self.verifier.verify_whisper_assets(self.root)
        helper=self.root/'tools/verify_whisper_assets.py';helper.parent.mkdir();shutil.copy2(ROOT/'tools/verify_whisper_assets.py',helper)
        self.model.write_bytes(self.content)
        result=subprocess.run([sys.executable,str(helper)],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertEqual(self.assets['model_sha256'],json.loads(result.stdout)['model_sha256'])
        self.model.write_bytes(b'corrupt')
        result=subprocess.run([sys.executable,str(helper)],capture_output=True,text=True)
        self.assertNotEqual(0,result.returncode);self.assertIn('checksum mismatch',result.stderr);self.assertEqual('',result.stdout)
