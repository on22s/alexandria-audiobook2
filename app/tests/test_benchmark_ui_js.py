"""Execute actual benchmark form and status handlers without running GPU work."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / "static/js/app-reports.js"


class BenchmarkUiJsTests(unittest.TestCase):
    def run_js(self, script):
        setup = "const sharedSource = require('fs').readFileSync(process.argv[2], 'utf8'); const sharedPollingCode = 'function restoreTab() {}\\n' + sharedSource.slice(sharedSource.indexOf('function showActionError('),sharedSource.indexOf('function showConfirm(')) + sharedSource.slice(sharedSource.indexOf('const _pollGen ='), sharedSource.indexOf('// A run can pause ITSELF'));\n"
        script = setup + script.replace('vm.runInNewContext(code,context)', 'vm.runInNewContext(sharedPollingCode + code,context)').replace("vm.runInContext(source.slice(source.indexOf('let _benchmarkPreflightId =')),ctx)", "vm.runInContext(sharedPollingCode + source.slice(source.indexOf('let _benchmarkPreflightId =')),ctx)")
        result = subprocess.run(["node", "-e", script, str(SOURCE), str(SOURCE.parent / 'app-core.js')], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_manifest_object_contract_blocks_arrays_before_api_requests(self):
        self.run_js(r"""
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('let _benchmarkPreflightId ='), source.indexOf('async function onBenchmarkStart()'));
const field = {value:''}, button = {disabled:true}, out = {}, posts = [], toasts = [];
const context = {document:{getElementById:id=>({'benchmark-manifest':field,'btn-benchmark-start':button,'benchmark-output':out}[id])},
 API:{post:async(path,data)=>{posts.push({path,data});return {preflight_id:'valid'};}},
 showToast(message,kind){toasts.push({message,kind});}};
vm.runInNewContext(code,context);
(async()=>{
 for(const value of [[],[{}],null,false,0,'a']) {
  field.value=JSON.stringify(value);posts.length=0;toasts.length=0;
  await context.onBenchmarkPreflight();
  assert.strictEqual(posts.length,0,`Invalid manifest ${field.value} must not call API`);
  assert.strictEqual(toasts.length,1);assert.strictEqual(toasts[0].kind,'error');
  assert(toasts[0].message.includes('must be a JSON object'));assert.strictEqual(button.disabled,true);
 }
 for(const value of [{},{fixtures:[{id:'fixture'}]}]) {
  field.value=JSON.stringify(value);posts.length=0;toasts.length=0;
  await context.onBenchmarkPreflight();
  assert.strictEqual(posts.length,1);assert.deepStrictEqual(JSON.parse(JSON.stringify(posts[0].data.manifest)),value);
  assert.strictEqual(posts[0].path,'/api/benchmark/preflight');assert.strictEqual(button.disabled,false);
  assert.deepStrictEqual(toasts,[]);
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_fixture_progress_counts_only_done_and_keeps_existing_poll_lifecycle(self):
        self.run_js(r"""
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('let _benchmarkPreflightId ='));
const status = {}, output = {}, cancel = {style:{}}, intervals = [], cleared = [];
let response;
const context = {_benchmarkPoll:null,_benchmarkStatusPending:false,document:{getElementById:id=>({'benchmark-status':status,'benchmark-output':output,'btn-benchmark-cancel':cancel}[id])},
 API:{get:async path=>{assert.strictEqual(path,'/api/benchmark/status');return response;}},
 setTimeout(fn,ms){assert.strictEqual(ms,3000);if (!intervals.length) { intervals.push(99); } return 99;},
 clearTimeout(id){cleared.push(id);}};
vm.runInNewContext(code,context);
(async()=>{
 response={running:true,tasks:[{status:'done'},{status:'failed'},{status:'pending'},{status:'running'},{status:'cancelled'},{status:null},{}],logs:['started']};
 await context.refreshBenchmarkStatus();
 assert.strictEqual(status.textContent,'running · 1/7 fixtures');assert.strictEqual(cancel.style.display,'');
 assert.strictEqual(output.textContent,'started');assert.deepStrictEqual(intervals,[99]);
 response={running:true,tasks:[{status:'done'},{status:'done'}]};await context.refreshBenchmarkStatus();
 assert.strictEqual(status.textContent,'running · 2/2 fixtures');assert.deepStrictEqual(intervals,[99]);
 response={running:false,status:'failed',tasks:[]};await context.refreshBenchmarkStatus();
 assert.strictEqual(status.textContent,'failed');assert.strictEqual(cancel.style.display,'none');assert.deepStrictEqual(cleared,[]);
 assert.strictEqual(vm.runInNewContext('_benchmarkPoll',context),null);
 response={running:true,tasks:[]};await context.refreshBenchmarkStatus();assert.strictEqual(status.textContent,'running · 0/0 fixtures');
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


    def test_initial_status_failure_retries_without_duplicate_timers_and_stops_on_idle(self):
        self.run_js(r"""
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('let _benchmarkPreflightId ='));
const status = {}, output = {textContent:'previous logs'}, cancel = {style:{display:''}}, callbacks = [], cleared = [];
let failure = true, calls = 0, running = true;
const context = {_benchmarkPoll:null,_benchmarkStatusPending:false, document:{getElementById:id=>({'benchmark-status':status,'benchmark-output':output,'btn-benchmark-cancel':cancel}[id])},
 API:{get:async()=>{calls++;if(failure){throw new Error('HTTP 503');}return {running,status:'idle',tasks:[{status:'done'}]};}},
 setTimeout(fn,ms){assert.strictEqual(ms,3000);callbacks[0] = fn;return 99;}, clearTimeout(id){cleared.push(id);}};
vm.runInNewContext(code,context);
(async()=>{
 await context.refreshBenchmarkStatus();
 assert.strictEqual(calls,1);assert.strictEqual(callbacks.length,1,'First failure must schedule retry');
 assert(status.textContent.includes('unavailable'));assert(status.textContent.includes('HTTP 503'));
 assert.strictEqual(output.textContent,'previous logs');assert.strictEqual(cancel.style.display,'');
 await callbacks[0]();assert.strictEqual(callbacks.length,1,'Repeated failure must not duplicate retry timer');
 failure=false;await callbacks[0]();
 assert.strictEqual(status.textContent,'running · 1/1 fixtures');assert.strictEqual(callbacks.length,1);
 running=false;await callbacks[0]();
 assert.strictEqual(status.textContent,'idle');assert.deepStrictEqual(cleared,[]);assert.strictEqual(vm.runInNewContext('_benchmarkPoll',context),null);
 assert.strictEqual(cancel.style.display,'none');assert.strictEqual(calls,4);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")


    def test_pending_status_request_suppresses_interval_overlap_and_releases_after_error(self):
        self.run_js(r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const code=source.slice(source.indexOf('let _benchmarkPreflightId ='));
const status={},out={textContent:'previous'},cancel={style:{display:'none'}};
const requests=[],timers=[],cleared=[];
const context={document:{getElementById:id=>({'benchmark-status':status,'benchmark-output':out,'btn-benchmark-cancel':cancel}[id])},
 API:{get:path=>{assert.strictEqual(path,'/api/benchmark/status');return new Promise((resolve,reject)=>requests.push({resolve,reject}));}},
 setTimeout(fn,ms){assert.strictEqual(ms,3000);timers[0] = fn;return 99;},
 clearTimeout(id){cleared.push(id);}};
vm.runInNewContext(code,context);
let finished=false;
process.on('beforeExit',()=>{if(!finished){console.error('Status fixture did not reach its final assertions');process.exitCode=1;}});
(async()=>{
 const first=context.refreshBenchmarkStatus();
 assert.strictEqual(requests.length,1);
 const duplicate=context.refreshBenchmarkStatus(),duplicate2=context.refreshBenchmarkStatus();
 assert.strictEqual(requests.length,1,'pending manual/interval ticks must share one status request');
 assert.strictEqual(out.textContent,'previous');
 requests[0].resolve({running:true,tasks:[{status:'done'},{status:'running'}],logs:['working']});await Promise.all([first,duplicate,duplicate2]);
 assert.strictEqual(status.textContent,'running · 1/2 fixtures');assert.strictEqual(out.textContent,'working');
 assert.strictEqual(timers.length,1);assert.strictEqual(cancel.style.display,'');
 const second=timers[0]();assert.strictEqual(requests.length,2);
 const skipped=timers[0](),skipped2=context.refreshBenchmarkStatus();assert.strictEqual(requests.length,2);
 requests[1].reject(new Error('HTTP 503'));await Promise.all([second,skipped,skipped2]);
 assert(status.textContent.includes('Status polling will retry'));assert(status.textContent.includes('Details: HTTP 503'));assert.strictEqual(timers.length,1);
 const third=timers[0]();assert.strictEqual(requests.length,3,'rejection must release the in-flight guard');
 const skipped3=timers[0]();assert.strictEqual(requests.length,3);
 requests[2].resolve({running:false,status:'completed',tasks:[{status:'done'}],logs:['finished']});await Promise.all([third,skipped3]);
 assert.strictEqual(status.textContent,'completed');assert.strictEqual(out.textContent,'finished');
 assert.strictEqual(cancel.style.display,'none');assert.deepStrictEqual(cleared,[]);
 const fourth=context.refreshBenchmarkStatus();assert.strictEqual(requests.length,4,'terminal response must release guard too');
 requests[3].resolve({running:false,status:'idle',tasks:[]});await fourth;
 assert.strictEqual(status.textContent,'idle');
 context.document.getElementById=()=>null;
 await context.refreshBenchmarkStatus();assert.strictEqual(requests.length,4,'absent status UI must not start a poll');
 finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
