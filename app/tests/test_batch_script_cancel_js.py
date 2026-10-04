"""Actual batch preparation honors Cancel before and during start registration."""
from pathlib import Path
import os
import subprocess
import unittest

SOURCE = Path(os.environ.get('BATCH_CANCEL_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-core.js'))
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function client(phase){
 const fields={},requests=[],toasts=[],uploads=[],confirmation=[];let polls=0,gate=deferred();
 const el=id=>fields[id]||(fields[id]={value:id==='script-collision-policy'?'suffix':'',style:{},disabled:false,innerHTML:''});
 const preflight={books:[{scripts:['Latin'],exceeds_output_ceiling:false}],book_count:1,workers:1,loaded_parallel:1,per_slot_context:10000,worst_request_tokens:1000};
 const ctx={window:null,console:{log(){},error(){}},document:{getElementById:el},_batchPauseResume(){},_resetPauseBtn(){},_isStripFrontMatterChecked:()=>true,confirmIfRemote:async()=>true,
 showToast:(message,tone)=>toasts.push({message,tone}),showConfirm:async message=>{confirmation.push(message);return phase==='confirm'?await gate.promise:true;},_pollScriptBatchLogs:()=>polls++,API:{
 upload:async file=>{uploads.push(file.name);if(phase==='upload'){await gate.promise;}return {stored_filename:file.name};},
 post:async(url,data)=>{requests.push({url,data:JSON.parse(JSON.stringify(data))});if(url.endsWith('/preflight')){if(phase==='preflight'){await gate.promise;}return preflight;}if(url.endsWith('/start')&&phase==='start'){await gate.promise;}return{};}
 }};ctx.window=ctx;vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')),ctx);
 const admissionStart=source.indexOf('async function ensureScriptStartConfirmed(');if(admissionStart>=0){vm.runInContext(source.slice(admissionStart,source.indexOf('// navigator.clipboard',admissionStart)),ctx);}
 vm.runInContext('let scriptBatchQueue=[{file:{name:"one.txt"}}];'+source.slice(source.indexOf('async function cancelTask('),source.indexOf('// Same "unknown error" fallback'))+
 source.slice(source.includes('let scriptBatchStartOperation =')?source.indexOf('let scriptBatchStartOperation ='):source.indexOf('window.cancelBatchScript ='),source.indexOf('        loadExistingScriptUploads();',source.indexOf('async function _startBatchScript()'))),ctx);
 return {ctx,el,requests,toasts,uploads,confirmation,gate,getPolls:()=>polls,setPhase:value=>phase=value};
}
'''


class BatchScriptCancelJsTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});', str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_cost_admission_blocks_duplicate_batch_starts_and_recovers_decline(self):
        self.run_js(r"""
let completed=false;process.on('beforeExit',()=>assert(completed,'batch admission assertions must finish'));
for(const outcome of ['decline','error','approve']){
 const c=client('none'),gate=deferred();let admissions=0;
 c.ctx.confirmIfRemote=()=>{admissions++;return gate.promise;};
 const first=c.ctx._startBatchScript();assert.strictEqual(c.el('btn-gen-script').disabled,true);
 await c.ctx._startBatchScript();assert.strictEqual(admissions,1);assert.strictEqual(c.requests.length,0);
 if(outcome==='error'){gate.reject(Error('offline'));}else{gate.resolve(outcome==='approve');}
 await first;assert.strictEqual(c.requests.filter(r=>r.url.endsWith('/start')).length,outcome==='approve'?1:0);
 assert.strictEqual(c.el('btn-gen-script').disabled,outcome==='approve');
}
completed=true;
""")

    def test_cancel_during_upload_preflight_or_confirmation_never_starts_generation(self):
        self.run_js(r'''
for(const phase of ['upload','preflight','confirm']){
 const c=client(phase),starting=c.ctx._startBatchScript();await turn();assert.strictEqual(c.el('btn-gen-script').disabled,true);
 await c.ctx.cancelBatchScript();assert.strictEqual(c.requests.filter(r=>r.url.endsWith('/cancel')).length,0,'unregistered job must not receive ineffective server Cancel');assert.strictEqual(c.el('btn-pause-batch-script').style.display,'none');
 c.gate.resolve(true);await starting;assert.strictEqual(c.requests.filter(r=>r.url.endsWith('/start')).length,0);assert.strictEqual(c.getPolls(),0);assert.strictEqual(c.el('btn-gen-script').disabled,false);assert.strictEqual(c.el('btn-cancel-batch-script').style.display,'none');assert.match(c.el('script-batch-status-msg').innerHTML,/cancelled before generation started/);assert.strictEqual(c.toasts.length,0);
 if(phase==='upload'){assert.strictEqual(c.requests.length,0);assert.strictEqual(c.confirmation.length,0);}if(phase==='preflight'){assert.strictEqual(c.confirmation.length,0);}
 c.setPhase('none');await c.ctx._startBatchScript();assert.strictEqual(c.requests.filter(r=>r.url.endsWith('/start')).length,1,'fresh retry must get fresh cancellation state');assert.strictEqual(c.getPolls(),1);
}
''')

    def test_cancel_while_start_post_is_pending_waits_for_ack_then_cancels_worker(self):
        self.run_js(r'''
const c=client('start');const starting=c.ctx._startBatchScript();await turn();assert.strictEqual(c.requests.at(-1).url,'/api/generate_script/batch/start');await c.ctx.cancelBatchScript();assert.strictEqual(c.requests.filter(r=>r.url.endsWith('/cancel')).length,0);
await c.ctx._startBatchScript();assert.strictEqual(c.requests.filter(r=>r.url.endsWith('/start')).length,1);c.gate.resolve();await starting;
assert.deepStrictEqual(c.requests.map(r=>r.url),['/api/generate_script/batch/preflight','/api/generate_script/batch/start','/api/generate_script/batch/cancel']);assert.strictEqual(c.getPolls(),1);assert.strictEqual(c.el('btn-gen-script').disabled,true,'registered task owns controls until poll completion');assert.strictEqual(c.el('btn-pause-batch-script').style.display,'inline-block');
await c.ctx.cancelBatchScript();assert.strictEqual(c.requests.at(-1).url,'/api/generate_script/batch/cancel','later Cancel keeps normal worker path');
''')

    def test_cancelled_upload_failure_is_cancellation_and_normal_failure_releases_start(self):
        self.run_js(r'''
const c=client('upload');let starting=c.ctx._startBatchScript();await turn();await c.ctx.cancelBatchScript();c.gate.reject(Error('upload failed after cancel'));await starting;assert.strictEqual(c.toasts.length,0);assert.match(c.el('script-batch-status-msg').innerHTML,/cancelled/);
c.setPhase('none');c.ctx.API.upload=async()=>{throw Error('upload unavailable');};await c.ctx._startBatchScript();assert(c.toasts.at(-1).message.startsWith('Failed to start batch.'));assert(c.toasts.at(-1).message.includes('Test Connection in Setup'));assert(c.toasts.at(-1).message.includes('upload unavailable'));assert.strictEqual(c.el('btn-gen-script').disabled,false);
c.ctx.API.upload=async()=>({stored_filename:'one.txt'});await c.ctx._startBatchScript();assert.strictEqual(c.getPolls(),1);assert.strictEqual(c.requests.at(-1).data.strip_front_matter,true);
''')
