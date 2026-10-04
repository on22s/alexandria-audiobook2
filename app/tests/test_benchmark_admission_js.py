"""Actual benchmark handlers under delayed/reversed HTTP responses."""
import unittest
import json
import re
from pathlib import Path
from tests import test_benchmark_ui_js as existing


SETUP = r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const field={value:'{"fixture":"A"}'},button={disabled:true},out={textContent:''},status={},cancel={style:{}},help={textContent:''};
const timers=[],cleared=[],toasts=[];
const ctx={document:{getElementById:id=>({'benchmark-manifest':field,'btn-benchmark-start':button,'benchmark-output':out,'benchmark-status':status,'btn-benchmark-cancel':cancel,'benchmark-start-help':help}[id])},
 API:{},showToast:(...args)=>toasts.push(args),setTimeout:(fn,ms)=>{assert.strictEqual(ms,3000);timers.push(fn);return timers.length;},clearTimeout:id=>cleared.push(id)};
vm.createContext(ctx);{const guidanceCore=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInContext(guidanceCore.slice(guidanceCore.indexOf('function showActionError('),guidanceCore.indexOf('function showConfirm(')),ctx);}vm.runInContext(source.slice(source.indexOf('let _benchmarkPreflightId =')),ctx);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const flush=async()=>{for(let i=0;i<12;i++){await Promise.resolve();}};
let finished=false;process.on('beforeExit',()=>{if(!finished){console.error('Benchmark fixture unfinished');process.exitCode=1;}});
(async()=>{
"""


class BenchmarkAdmissionJsTests(unittest.TestCase):
    def run_case(self, code):
        existing.BenchmarkUiJsTests.run_js(self, SETUP + code + "\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});")

    def test_textarea_input_hook_invalidates_the_visible_approval(self):
        html = (Path(__file__).resolve().parent.parent / 'static/index.html').read_text()
        tag = re.search(r'<textarea[^>]*id="benchmark-manifest"[^>]*>', html).group(0)
        action = re.search(r'oninput="([^"]+)"', tag).group(1)
        self.run_case("ctx.API.post=async()=>({preflight_id:'approved'});await ctx.onBenchmarkPreflight();"
                      "assert.strictEqual(button.disabled,false);field.value='{}';"
                      + "vm.runInContext(" + json.dumps(action) + ",ctx);"
                      + "assert.strictEqual(button.disabled,true);")

    def test_manifest_edits_and_programmatic_changes_require_new_approval(self):
        self.run_case(r"""
let starts=0;ctx.API.post=async path=>{if(path.endsWith('/preflight')){return {preflight_id:'A'};}starts++;return {};};
ctx.API.get=async()=>({running:false,status:'idle'});
await ctx.onBenchmarkPreflight();assert.strictEqual(button.disabled,false);
field.value='{"fixture":"B"}';ctx.onBenchmarkManifestChange();assert.strictEqual(button.disabled,true);
await ctx.onBenchmarkStart();assert.strictEqual(starts,0);
await ctx.onBenchmarkPreflight();assert.strictEqual(button.disabled,false);
field.value='{"fixture":"C"}';await ctx.onBenchmarkStart();assert.strictEqual(starts,0);assert.strictEqual(button.disabled,true);
field.value='{"fixture":"D"}';await ctx.onBenchmarkPreflight();field.value='not JSON';await ctx.onBenchmarkPreflight();
assert.strictEqual(button.disabled,true);assert.strictEqual(toasts.length,1);
""")

    def test_start_checks_current_manifest_even_without_an_input_event(self):
        self.run_case(r"""
let starts=0;ctx.API.post=async path=>{if(path.endsWith('/preflight')){return {preflight_id:'A'};}starts++;return {};};
ctx.API.get=async()=>({running:false,status:'idle'});
await ctx.onBenchmarkPreflight();field.value='{"fixture":"unchecked-B"}';
await ctx.onBenchmarkStart();assert.strictEqual(starts,0,'unchecked manifest must not be posted with old token');assert.strictEqual(button.disabled,true);
""")

    def test_older_preflight_response_cannot_replace_latest_token_or_output(self):
        self.run_case(r"""
const a=deferred(),b=deferred();const posted=[];
ctx.API.post=(path,body)=>{posted.push({path,body});if(path.endsWith('/start')){return Promise.resolve({});}return body.manifest.fixture==='A'?a.promise:b.promise;};
ctx.API.get=async()=>({running:true,tasks:[]});
const first=ctx.onBenchmarkPreflight();field.value='{"fixture":"B"}';const second=ctx.onBenchmarkPreflight();
b.resolve({preflight_id:'token-B',checked:'B'});await second;
a.resolve({preflight_id:'token-A',checked:'A'});await first;
assert(out.textContent.includes('token-B'));assert(!out.textContent.includes('token-A'));
await ctx.onBenchmarkStart();const start=posted.find(p=>p.path.endsWith('/start'));
assert.strictEqual(start.body.preflight_id,'token-B');assert.strictEqual(start.body.manifest.fixture,'B');
assert.strictEqual(posted[0].body.manifest.fixture,'A');assert.strictEqual(button.disabled,true);
""")

    def test_stale_errors_and_edit_away_then_back_do_not_restore_approval(self):
        self.run_case(r"""
const a=deferred(),b=deferred();let n=0;ctx.API.post=()=>++n===1?a.promise:b.promise;
const first=ctx.onBenchmarkPreflight();field.value='{"fixture":"B"}';const second=ctx.onBenchmarkPreflight();
b.resolve({preflight_id:'token-B'});await second;const previous=out.textContent;
a.reject(Error('old failure'));await first;assert.strictEqual(out.textContent,previous);assert.strictEqual(button.disabled,false);
const changed=deferred();ctx.API.post=()=>changed.promise;const pending=ctx.onBenchmarkPreflight();
field.value='{"fixture":"C"}';ctx.onBenchmarkManifestChange();field.value='{"fixture":"B"}';ctx.onBenchmarkManifestChange();
changed.resolve({preflight_id:'stale-B'});await pending;assert.strictEqual(button.disabled,true);
assert.strictEqual(vm.runInContext('_benchmarkPreflightId',ctx),null);
""")

    def test_duplicate_start_and_active_run_are_guarded_and_failure_needs_new_check(self):
        self.run_case(r"""
let starts=0,checks=0;const start=deferred();
ctx.API.post=(path)=>{if(path.endsWith('/preflight')){checks++;return Promise.resolve({preflight_id:'approved'});}starts++;return start.promise;};
let running=true;ctx.API.get=async()=>({running,status:running?'running':'completed',tasks:[]});
await ctx.onBenchmarkPreflight();const pending=ctx.onBenchmarkStart();assert.strictEqual(button.disabled,true);
await ctx.onBenchmarkStart();assert.strictEqual(starts,1);await ctx.onBenchmarkPreflight();assert.strictEqual(checks,1);
start.resolve({});await pending;await flush();await ctx.onBenchmarkStart();await ctx.onBenchmarkPreflight();assert.strictEqual(starts,1);assert.strictEqual(checks,1);assert.strictEqual(button.disabled,true);
running=false;await ctx.refreshBenchmarkStatus();assert.strictEqual(button.disabled,true,'accepted token was consumed');
await ctx.onBenchmarkPreflight();assert.strictEqual(button.disabled,false);
ctx.API.post=async()=>{throw Error('not accepted');};await ctx.onBenchmarkStart();assert.strictEqual(button.disabled,true);assert.strictEqual(toasts.length,1);
assert.strictEqual(vm.runInContext('_benchmarkStarting',ctx),false);
ctx.API.post=async()=>({preflight_id:'fresh'});await ctx.onBenchmarkPreflight();assert.strictEqual(button.disabled,false);
""")

    def test_old_idle_status_cannot_undo_newly_accepted_run(self):
        self.run_case(r"""
const old=deferred();let statusCalls=0;ctx.API.get=()=>{statusCalls++;return statusCalls===1?old.promise:Promise.resolve({running:true,tasks:[]});};
ctx.API.post=async()=>({preflight_id:'approved'});
await ctx.onBenchmarkPreflight();const pendingStatus=ctx.refreshBenchmarkStatus();
await ctx.onBenchmarkStart();assert.strictEqual(timers.length,1);
old.resolve({running:false,status:'idle',tasks:[]});await pendingStatus;
assert.strictEqual(vm.runInContext('_benchmarkRunning',ctx),true);assert.strictEqual(button.disabled,true);assert.strictEqual(timers.length,1);assert.strictEqual(cleared.length,0);
await timers[0]();assert.strictEqual(statusCalls,2);assert(status.textContent.includes('running'));
""")

    def test_start_help_tracks_approval_failure_and_running_state(self):
        self.run_case(r"""
ctx._applyBenchmarkStartButton();assert.match(help.textContent,/successful preflight/);ctx.API.post=async()=>({preflight_id:'approved'});await ctx.onBenchmarkPreflight();assert.strictEqual(button.disabled,false);assert.match(help.textContent,/Preflight passed/);field.value='{"changed":true}';ctx.onBenchmarkManifestChange();assert(button.disabled);assert.match(help.textContent,/after editing/);ctx.API.post=async()=>{throw Error('failed');};await ctx.onBenchmarkPreflight();assert(button.disabled);assert.match(help.textContent,/successful preflight/);vm.runInContext('_benchmarkRunning=true;',ctx);ctx._applyBenchmarkStartButton();assert.match(help.textContent,/benchmark is running/);assert(button.disabled);
""")
