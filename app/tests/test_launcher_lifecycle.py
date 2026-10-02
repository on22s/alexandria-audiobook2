"""Reset and update admit mutation only after their shared awaited shutdown."""
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
RESET_PATHS=['annotated_script.json','voices.json','voice_config.json','character_aliases.json','state.json','app/config.json','chunks.json','cloned_audiobook.mp3','voicelines']


def descriptor(name):
    return json.loads(subprocess.check_output(['node','-e','process.stdout.write(JSON.stringify(require(process.argv[1])))',str(ROOT/name)],text=True))


class LauncherLifecycleTests(unittest.TestCase):
    def test_reset_and_update_use_the_same_awaited_step_before_any_mutation(self):
        for name in ('reset.js','update.js'):
            with self.subTest(name=name):
                run=descriptor(name)['run']
                self.assertEqual('stop_writers',run[0]['method'])
                self.assertEqual('launcher_lifecycle.js',run[0]['uri'])
                self.assertNotIn('script.stop',[step['method'] for step in run])
                if name == 'update.js':
                    self.assertEqual('git pull',run[1]['params']['message'])
                    self.assertEqual('install.js',run[2]['params']['uri'])

    def test_reset_has_one_data_driven_boundary_and_preserves_its_exact_existing_scope(self):
        source=(ROOT/'reset.js').read_text()
        self.assertEqual(1,len(re.findall(r'method:\s*"fs.rm"',source)))
        run=descriptor('reset.js')['run']
        self.assertEqual(RESET_PATHS,[step['params']['path'] for step in run if step['method']=='fs.rm'])
        self.assertNotIn('scripts',RESET_PATHS);self.assertNotIn('voice_library.json',RESET_PATHS)

    def test_all_root_writer_scripts_are_registered_and_callers_are_excluded(self):
        self.assertTrue((ROOT/'launcher_lifecycle.js').is_file())
        script="""const fs=require('fs'),path=require('path'),root=process.argv[1],Lifecycle=require(path.join(root,'launcher_lifecycle.js'));const helper=new Lifecycle();const actual=fs.readdirSync(root).filter(n=>n.endsWith('.js')).filter(n=>{const d=require(path.join(root,n));return Array.isArray(d.run)});process.stdout.write(JSON.stringify({actual,reset:helper.get_writer_scripts('reset.js'),update:helper.get_writer_scripts('update.js')}));"""
        result=json.loads(subprocess.check_output(['node','-e',script,str(ROOT)],text=True))
        self.assertEqual(set(result['actual'])-{'reset.js'},set(result['reset']))
        self.assertEqual(set(result['actual'])-{'update.js'},set(result['update']))
        self.assertIn('run_stage4_checkpoint.js',result['reset']);self.assertIn('start_llm.js',result['reset']);self.assertIn('install.js',result['reset'])

    def run_native_case(self,case):
        result=subprocess.run(['node','-e',NATIVE_LIFECYCLE,str(ROOT),case],capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        return json.loads(result.stdout)

    def test_held_shutdown_is_awaited_and_each_success_log_follows_completion(self):
        result=self.run_native_case('held')
        self.assertFalse(result['finishedWhileHeld']);self.assertEqual(result['stops'],result['logs'])
        self.assertEqual(7,len(result['stops']));self.assertNotIn('reset.js',result['stops'])

    def test_shutdown_failure_prevents_reset_deletions_and_update_pull_or_install(self):
        result=self.run_native_case('failure')
        self.assertEqual(['reset.js','update.js'],result['failedCallers']);self.assertEqual([],result['mutations'])

    def test_real_writer_children_exit_before_reset_and_saved_libraries_remain_exact(self):
        result=self.run_native_case('writers')
        self.assertEqual(3,result['nativeChildrenStopped']);self.assertEqual(9,result['pathsRemoved'])
        self.assertTrue(result['savedLibraryPreserved']);self.assertFalse(result['recreatedChunks'])

    def test_foreign_missing_or_unmanaged_caller_cannot_stop_any_checkout_jobs(self):
        result=self.run_native_case('caller')
        self.assertEqual(3,result['rejected']);self.assertEqual([],result['stops'])


NATIVE_LIFECYCLE = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const root=process.argv[1],which=process.argv[2],Lifecycle=require(path.join(root,'launcher_lifecycle.js'));
const helper=new Lifecycle(),req=caller=>({parent:{path:path.join(root,caller)}});
const turn=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  if(which==='held'){
    let done=false;const queued=[],stops=[],logs=[];
    const kernel={api:{stop:r=>{stops.push(path.basename(r.params.uri));return new Promise(resolve=>queued.push(resolve))}}};
    const pending=helper.stop_writers(req('reset.js'),r=>logs.push(r.raw.match(/Stopped (.+)\r/)[1]),kernel).then(()=>{done=true});
    await turn();assert.equal(done,false);assert.deepEqual(logs,[]);assert.deepEqual(stops,['start.js']);
    for(let i=0;i<20&&!done;i++){const release=queued.shift();if(release){release()}await turn()}
    await pending;assert.equal(done,true);assert.deepEqual(stops,logs);
    console.log(JSON.stringify({finishedWhileHeld:false,stops,logs}));
  }else if(which==='failure'){
    const mutations=[],failedCallers=[];
    for(const caller of ['reset.js','update.js']){
      const steps=require(path.join(root,caller)).run;let calls=0;
      const kernel={api:{stop:async()=>{calls++;if(calls===2){throw new Error('known shutdown failure')}}}};
      try{
        for(const step of steps){
          if(step.uri==='launcher_lifecycle.js'){await helper[step.method](req(caller),()=>{},kernel)}
          else{mutations.push(step.method)}
        }
        assert.fail('shutdown failure was swallowed');
      }catch(error){assert.match(error.message,/known shutdown failure/);failedCallers.push(caller)}
    }
    assert.deepEqual(mutations,[]);console.log(JSON.stringify({failedCallers,mutations}));
  }else if(which==='caller'){
    const stops=[];let rejected=0;const kernel={api:{stop:async r=>stops.push(r.params.uri)}};
    for(const request of [{},{parent:{path:path.join(root,'start.js')}},{parent:{path:path.join(os.tmpdir(),'other-checkout','reset.js')}}]){
      try{await helper.stop_writers(request,()=>{},kernel);assert.fail('invalid caller accepted')}catch(error){assert.match(error.message,/shutdown/);rejected++}
    }
    assert.deepEqual(stops,[]);console.log(JSON.stringify({rejected,stops}));
  }else if(which==='writers'){
    const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'alexandria-lifecycle-')),children=new Map();
    try{
      const steps=require(path.join(root,'reset.js')).run;
      for(const step of steps.filter(s=>s.method==='fs.rm')){
        const target=path.join(tmp,step.params.path);fs.mkdirSync(path.dirname(target),{recursive:true});
        if(step.params.path==='voicelines'){fs.mkdirSync(target);fs.writeFileSync(path.join(target,'fixture.wav'),'fixture')}
        else{fs.writeFileSync(target,'active project fixture')}
      }
      fs.mkdirSync(path.join(tmp,'scripts'));fs.writeFileSync(path.join(tmp,'scripts/saved.json'),'exact saved book');fs.writeFileSync(path.join(tmp,'voice_library.json'),'exact reusable cast');
      const code=`const fs=require('fs');const file=process.env.FIXTURE_CHUNKS;let timer=setInterval(()=>fs.writeFileSync(file,'running fixture writer'),5);process.on('SIGTERM',()=>{clearInterval(timer);setTimeout(()=>{fs.writeFileSync(file,'last write during shutdown');process.exit(0)},30)});process.send('ready');`;
      await Promise.all(['start.js','start_llm.js','run_stage4_checkpoint.js'].map(name=>new Promise((resolve,reject)=>{
        const child=spawn(process.execPath,['-e',code],{env:{...process.env,FIXTURE_CHUNKS:path.join(tmp,'chunks.json')},stdio:['ignore','ignore','pipe','ipc']});children.set(name,child);child.once('error',reject);child.once('message',()=>resolve());
      })));
      const kernel={api:{stop:async r=>{
        const child=children.get(path.basename(r.params.uri));
        if(child&&child.exitCode===null&&child.signalCode===null){await new Promise((resolve,reject)=>{child.once('close',resolve);child.once('error',reject);child.kill('SIGTERM')})}
      }}};
      let removed=0;
      for(const step of steps){
        if(step.uri==='launcher_lifecycle.js'){await helper[step.method](req('reset.js'),()=>{},kernel)}
        else{
          assert.equal(step.method,'fs.rm');assert.ok([...children.values()].every(p=>p.exitCode!==null||p.signalCode!==null),'mutation started with a live writer');
          fs.rmSync(path.join(tmp,step.params.path),{recursive:true,force:true});removed++;
        }
      }
      assert.equal(fs.existsSync(path.join(tmp,'chunks.json')),false);assert.equal(fs.readFileSync(path.join(tmp,'scripts/saved.json'),'utf8'),'exact saved book');assert.equal(fs.readFileSync(path.join(tmp,'voice_library.json'),'utf8'),'exact reusable cast');
      console.log(JSON.stringify({nativeChildrenStopped:children.size,pathsRemoved:removed,savedLibraryPreserved:true,recreatedChunks:false}));
    }finally{
      for(const child of children.values()){if(child.exitCode===null&&child.signalCode===null){child.kill('SIGKILL');await new Promise(resolve=>child.once('close',resolve))}}
      fs.rmSync(tmp,{recursive:true,force:true});
    }
  }else{throw new Error('unknown fixture')}
})().catch(error=>{console.error(error);process.exitCode=1});
"""
