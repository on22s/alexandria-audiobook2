"""Actual shared poller and benchmark handlers with controlled timers/HTTP."""
import os
from pathlib import Path
import subprocess
import unittest

JS = Path(__file__).resolve().parents[1] / 'static/js'


class BenchmarkSharedPollingTests(unittest.TestCase):
    def test_full_reports_script_restores_after_benchmark_state_initializes(self):
        script = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const status={},out={},cancel={style:{}},button={disabled:false},help={};
let calls=0,restores=0,pending;
const context={document:{hidden:false,getElementById:id=>({
 'benchmark-status':status,'benchmark-output':out,'btn-benchmark-cancel':cancel,
 'btn-benchmark-start':button,'benchmark-start-help':help}[id])||null},
 API:{get:async path=>{assert.strictEqual(path,'/api/benchmark/status');calls++;return {running:false,status:'idle',logs:[]};}},
 restoreTab(){restores++;pending=context.refreshBenchmarkStatus();}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{await pending;assert.strictEqual(restores,1);assert.strictEqual(calls,1);
 assert.strictEqual(status.textContent,'idle');assert.strictEqual(button.disabled,true);
 assert.strictEqual(cancel.style.display,'none');})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script,
            os.environ.get('REPORT_BOOT_SOURCE', str(JS / 'app-reports.js'))],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def run_js(self, body):
        setup = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const core=fs.readFileSync(process.argv[1],'utf8'),reports=fs.readFileSync(process.argv[2],'utf8');
const code='function restoreTab() {}\n'+core.slice(core.indexOf('const _pollGen ='),core.indexOf('// A run can pause ITSELF'))+
 reports.slice(reports.indexOf('let _benchmarkPreflightId ='));
const status={},out={textContent:'previous'},cancel={style:{}},timers=[],toasts=[];
let calls=0,response={running:true,tasks:[{status:'done'}],logs:['working']},failure=false;
const context={document:{hidden:false,getElementById:id=>({'benchmark-status':status,'benchmark-output':out,'btn-benchmark-cancel':cancel}[id])},
 API:{get:async()=>{calls++;if(failure){throw new Error('offline');}return response;}},
 setTimeout:(fn,ms)=>{assert.strictEqual(ms,3000);timers.push(fn);return timers.length;},
 setInterval:(fn,ms)=>{timers.push(fn);return timers.length;},clearInterval:()=>{},
 showToast:(...args)=>toasts.push(args),console:{error:()=>{}}};
vm.createContext(context);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),context);vm.runInContext(code,context);
async function next(){assert.strictEqual(timers.length,1,'one scheduled callback');await timers.shift()();}
let finished=false;process.on('beforeExit',()=>{if(!finished){console.error('unfinished polling fixture');process.exitCode=1;}});
(async()=>{
'''
        script = setup + body + r'''
finished=true;})().catch(e=>{finished=true;console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script,
            os.environ.get('BENCHMARK_POLL_CORE', str(JS / 'app-core.js')),
            os.environ.get('BENCHMARK_POLL_REPORTS', str(JS / 'app-reports.js'))],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_hidden_ticks_do_not_fetch_then_visible_tick_observes_completion(self):
        self.run_js(r'''
context.document.hidden=true;await context.refreshBenchmarkStatus();assert.strictEqual(calls,0);
context.document.hidden=false;await context.refreshBenchmarkStatus();assert.strictEqual(calls,1);
assert.strictEqual(vm.runInContext('_pollGen.benchmark',context),1,'benchmark uses shared engine');
context.document.hidden=true;
for(let i=0;i<8;i++){await next();await context.refreshBenchmarkStatus();}
assert.strictEqual(calls,1);assert.strictEqual(out.textContent,'working');
response={running:false,status:'complete',tasks:[{status:'done'}],logs:['finished']};
context.document.hidden=false;await next();
assert.strictEqual(calls,2);assert.strictEqual(status.textContent,'complete');
assert.strictEqual(out.textContent,'finished');assert.strictEqual(cancel.style.display,'none');
assert.strictEqual(timers.length,0);assert.strictEqual(vm.runInContext('_benchmarkPoll',context),null);
''')

    def test_shared_retry_warning_and_pending_guard_survive_visibility_changes(self):
        self.run_js(r'''
failure=true;await context.refreshBenchmarkStatus();assert.strictEqual(calls,1);
await next();await next();assert.strictEqual(toasts.length,0);
context.document.hidden=true;await next();assert.strictEqual(calls,3);
context.document.hidden=false;await next();assert.strictEqual(calls,4);
assert.strictEqual(toasts.length,1);assert.strictEqual(toasts[0][1],'warning');
let resolve;context.API.get=()=>{calls++;return new Promise(r=>{resolve=r;});};
const tick=timers.shift();const pending=tick();assert.strictEqual(calls,5);
await tick();await context.refreshBenchmarkStatus();assert.strictEqual(calls,5);
resolve({running:true,tasks:[],logs:['recovered']});await pending;
assert.strictEqual(timers.length,1);assert.strictEqual(out.textContent,'recovered');
context.API.get=async()=>{calls++;return {running:false,status:'cancelled'};};
await next();assert.strictEqual(status.textContent,'cancelled');assert.strictEqual(timers.length,0);
''')

    def test_other_shared_pollers_keep_default_hidden_document_behavior(self):
        self.run_js(r'''
context.document.hidden=true;
let ticks=0;
vm.runInContext("_startPolling('existing', async()=>({running:false}), {doneCheck:s=>!s.running,onTick:()=>{globalThis.existingTick=true;}})",context);
for(let i=0;i<8;i++){await Promise.resolve();}
assert.strictEqual(context.existingTick,true);assert.strictEqual(timers.length,0);
''')
