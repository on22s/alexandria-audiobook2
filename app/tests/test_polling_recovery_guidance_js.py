"""Native polling warnings name features while retries and ownership survive."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'

class PollingRecoveryGuidanceJsTests(unittest.TestCase):
    def test_native_warning_cadence_labels_recovery_and_stop(self):
        code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const timers=[],warnings=[],errors=[];let calls=0,failed=true,done=0;
const c={document:{hidden:false},console:{error:(...args)=>errors.push(args)},setTimeout:fn=>timers.push(fn),showToast:(...args)=>warnings.push(args)};vm.createContext(c);let a=s.indexOf('const TASK_LABELS =');vm.runInContext(s.slice(a,s.indexOf('// Notify the user',a)),c);a=s.indexOf('const _pollGen =');vm.runInContext(s.slice(a,s.indexOf('// A run can pause ITSELF',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));
(async()=>{for(const [key,label,explicit] of [['script','Script generation',null],['reattach:persona','Persona generation',null],['opaque_internal_123','Task',null],['opaque_internal_456','Voice check','Voice check']]){timers.length=warnings.length=0;failed=true;c._startPolling(key,async()=>{calls++;if(failed)throw Error('raw private transport detail');return{running:false};},{immediate:false,doneCheck:d=>!d.running,onDone:()=>done++,displayLabel:explicit});for(let i=0;i<6;i++){await timers.shift()();assert.strictEqual(warnings.length,Math.floor((i+1)/3));assert.strictEqual(timers.length,1);}const text=warnings[0][0];assert(text.includes(`for ${label} status updates`));assert(text.includes('still retrying'));assert(text.includes('has not cancelled'));assert(text.includes('check that Alexandria is still running'));assert(!text.includes(key==='script'?'opaque_internal':key));assert(!text.includes('raw private'));assert.strictEqual(warnings[0][1],'warning');failed=false;await timers.shift()();assert.strictEqual(timers.length,0);}
assert.strictEqual(done,4);assert.strictEqual(errors.length,24);assert(errors[0][1].message.includes('raw private'));const before=calls;const stop=c._startPolling('script',async()=>{calls++;return{};},{immediate:false,doneCheck:()=>false});stop();await timers.shift()();assert.strictEqual(calls,before);assert.strictEqual(timers.length,0);finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',code,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
