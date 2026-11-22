"""Native polling warnings name features while retries and ownership survive."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'

class PollingRecoveryGuidanceJsTests(unittest.TestCase):
    def test_native_warning_cadence_labels_recovery_and_stop(self):
        code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const timers=[],warnings=[],errors=[];let calls=0,failed=true,done=0;
const c={document:{hidden:false},console:{error:(...args)=>errors.push(args)},setTimeout:fn=>timers.push(fn),showToast:(...args)=>warnings.push(args)};vm.createContext(c),c.t=c.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english))),c.formatUiSafetyText=c.formatUiSafetyText||((key,english,values=Object())=>c.t(key,english,values)),c.tSafety=c.tSafety||((key,english,values=Object())=>Object.fromEntries([['primary',c.t(key,english,values)],['secondary',null]]));let a=s.indexOf('const TASK_LABELS =');vm.runInContext(s.slice(a,s.indexOf('// Notify the user',a)),c);a=s.indexOf('const _pollGen =');vm.runInContext(s.slice(a,s.indexOf('// A run can pause ITSELF',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));
(async()=>{for(const [key,label,explicit] of [['script','Script generation',null],['reattach:persona','Persona generation',null],['opaque_internal_123','Task',null],['opaque_internal_456','Voice check','Voice check']]){timers.length=warnings.length=0;failed=true;c._startPolling(key,async()=>{calls++;if(failed)throw Error('raw private transport detail');return{running:false};},{immediate:false,doneCheck:d=>!d.running,onDone:()=>done++,displayLabel:explicit});for(let i=0;i<6;i++){await timers.shift()();assert.strictEqual(warnings.length,Math.floor((i+1)/3));assert.strictEqual(timers.length,1);}const text=warnings[0][0];assert(text.includes(`for ${label} status updates`));assert(text.includes('still retrying'));assert(text.includes('has not cancelled'));assert(text.includes('check that Alexandria is still running'));assert(!text.includes(key==='script'?'opaque_internal':key));assert(!text.includes('raw private'));assert.strictEqual(warnings[0][1],'warning');failed=false;await timers.shift()();assert.strictEqual(timers.length,0);}
assert.strictEqual(done,4);assert.strictEqual(errors.length,24);assert(errors[0][1].message.includes('raw private'));const before=calls;const stop=c._startPolling('script',async()=>{calls++;return{};},{immediate:false,doneCheck:()=>false});stop();await timers.shift()();assert.strictEqual(calls,before);assert.strictEqual(timers.length,0);finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',code,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_completion_is_final_and_handler_errors_are_reported_once(self):
        code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');
const timers=[],warnings=[],errors=[];
const c={document:{hidden:false},console:{error:(...a)=>errors.push(a)},setTimeout:fn=>timers.push(fn),showToast:(...a)=>warnings.push(a),TASK_LABELS:{},getTaskLabel:k=>k};
c.t=(key,english,values={})=>Object.keys(values).reduce((text,name)=>text.split('{'+name+'}').join(String(values[name])),String(english));
vm.createContext(c);const a=s.indexOf('const _pollGen =');vm.runInContext(s.slice(a,s.indexOf('// A run can pause ITSELF',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));
const reset=()=>{timers.length=warnings.length=errors.length=0;};
(async()=>{
reset();let fetches=0,dones=0;
c._startPolling('ok',async()=>{fetches++;return{done:true};},{immediate:false,doneCheck:d=>d.done,onDone:()=>dones++});
await timers.shift()();assert.strictEqual(fetches,1);assert.strictEqual(dones,1);assert.strictEqual(timers.length,0);assert.strictEqual(errors.length,0);
reset();fetches=dones=0;
c._startPolling('sync',async()=>{fetches++;return{done:true};},{immediate:false,doneCheck:d=>d.done,onDone:()=>{dones++;throw Error('sync boom');}});
await timers.shift()();assert.strictEqual(fetches,1);assert.strictEqual(dones,1);assert.strictEqual(timers.length,0,'a throwing onDone must not re-arm');assert.strictEqual(errors.length,1,'completion error reported once');assert(errors[0][0].includes('onDone'));assert(errors[0][1].message.includes('sync boom'));assert.strictEqual(warnings.length,0,'handler fault is not a connectivity warning');
reset();fetches=dones=0;let unhandled=0;const onUnhandled=()=>{unhandled++;};process.on('unhandledRejection',onUnhandled);
c._startPolling('async',async()=>{fetches++;return{done:true};},{immediate:false,doneCheck:d=>d.done,onDone:async()=>{dones++;throw Error('async boom');}});
await timers.shift()();await new Promise(r=>setTimeout(r,10));process.removeListener('unhandledRejection',onUnhandled);
assert.strictEqual(fetches,1);assert.strictEqual(dones,1);assert.strictEqual(timers.length,0);assert.strictEqual(errors.length,1);assert(errors[0][0].includes('onDone'));assert.strictEqual(unhandled,0,'async completion failure must be consumed');
reset();let ticks=0,doneFromTick=0;
c._startPolling('logs',async()=>({done:false}),{immediate:false,doneCheck:d=>d.done,onTick:()=>{ticks++;throw Error('render boom');},onDone:()=>doneFromTick++});
await timers.shift()();assert.strictEqual(ticks,1);assert.strictEqual(doneFromTick,0);assert.strictEqual(timers.length,1,'onTick fault keeps polling');assert.strictEqual(warnings.length,0);assert.strictEqual(errors.length,1);assert(errors[0][0].includes('onTick'));
reset();let tries=0;
c._startPolling('script',async()=>{tries++;throw Error('offline');},{immediate:false,doneCheck:()=>false});
for(let i=0;i<3;i++){await timers.shift()();assert.strictEqual(timers.length,1);}
assert.strictEqual(tries,3);assert.strictEqual(warnings.length,1);assert.strictEqual(errors.length,3);assert(errors[0][0].includes('Poll error'));
reset();let checked=0,doneOwnership=0;
c._startPolling('script',async()=>({ownership_unconfirmed:true,done:true}),{immediate:false,doneCheck:()=>{checked++;return true;},onDone:()=>doneOwnership++});
await timers.shift()();assert.strictEqual(checked,0);assert.strictEqual(doneOwnership,0);assert.strictEqual(timers.length,1);
reset();
let checkedRace=0,doneRace=0,release,entered;
const gate=new Promise(r=>release=r),tickEntered=new Promise(r=>entered=r);
const stop=c._startPolling('race',async()=>({done:true}),{immediate:false,doneCheck:()=>{checkedRace++;return true;},onDone:()=>doneRace++,onTick:()=>{entered();return gate;}});
const running=timers.shift()();
await tickEntered;
stop();
release();
await running;
assert.strictEqual(checkedRace,0);assert.strictEqual(doneRace,0);assert.strictEqual(timers.length,0);
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',code,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
