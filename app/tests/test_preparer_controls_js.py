"""Actual Preparer callbacks: accepted task identity, shared submission and escaped status."""
import unittest
from tests import test_dataset_ui_ownership_js as ownership


SETUP = r"""
const apiStart=core.indexOf('const API = {'),apiEnd=core.indexOf('// --- Setup Tab ---',apiStart);
run(core.slice(apiStart,apiEnd));
const escapeStart=core.indexOf('function escapeHtml('),escapeEnd=core.indexOf('// Parse a numeric input',escapeStart);
run(core.slice(escapeStart,escapeEnd));
const prepStart=source.indexOf('let prepBatchQueue ='),prepEnd=source.indexOf('// List/download the dataset ZIPs',prepStart);
run(source.slice(prepStart,prepEnd));
const {File}=require('buffer');context.FormData=FormData;
context.getNumFieldValue=(_id,fallback)=>fallback;
context.document.createElement=()=>({innerHTML:'',textContent:''});
elements['prep-batch-queue-body']={innerHTML:'',appendChild(){}};
elements['preparer-logs']={style:{},innerText:'',scrollTop:0,clientHeight:0,appendChild(node){this.innerText+=node.textContent;},scrollHeight:0};
elements['prep-audio-file']={files:[new File(['single bytes'],'single.wav')]};
elements['prep-source-file']={files:[]};
elements['prep-batch-files']={files:[new File(['batch bytes'],'batch.wav')]};
elements['prep-batch-mode']={checked:false};
const toasts=[],cancels=[],polls=[],done=[];
context.showToast=(...args)=>toasts.push(args);
context.cancelTask=url=>{cancels.push(url);};
context._startPolling=(name,fetch,options)=>{polls.push({name,fetch,options});};
context.notifyJobDone=name=>done.push(name);
context.loadPreparerOutputs=()=>{};
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const flush=async()=>{for(let i=0;i<8;i++){await Promise.resolve();}};
"""


class PreparerControlsJsTests(unittest.TestCase):
    def test_cpu_fallback_choice_reaches_actual_preparer_request(self):
        self.run_scenario(SETUP + r"""
for (const allowed of [false,true]) {
 elements['prep-allow-cpu-fallback']={checked:allowed};
 let captured;
 context.fetch=async(url,request)=>{captured=JSON.parse(request.body.get('config_json'));return {ok:false,status:400,statusText:'fixture refusal',text:async()=>''};};
 await context.startPreparer();
 assert.strictEqual(captured.allow_cpu_fallback,allowed);
}
""")

    run_scenario = ownership.DatasetUiOwnershipJsTests.run_scenario

    def test_pending_and_accepted_runs_cancel_the_captured_mode(self):
        self.run_scenario(SETUP + r"""
for(const batch of [false,true]){
 elements['prep-batch-mode'].checked=batch;if(batch){context.onPrepBatchFilesChange();}
 const pending=deferred();let requests=0;
 context.fetch=(url,req)=>{requests++;assert.strictEqual(url,batch?'/api/preparer/batch/upload_start':'/api/preparer/start');
  assert(req.body instanceof FormData);return pending.promise;};
 const start=context.startPreparer();await flush();
 assert.strictEqual(elements['btn-prep-start'].disabled,true);assert.strictEqual(elements['btn-prep-cancel'].style.display,'none');
 const before=cancels.length;context.cancelPreparer();assert.strictEqual(cancels.length,before,'pending upload has no registered controls');
 await context.startPreparer();assert.strictEqual(requests,1,'duplicate submission blocked');
 pending.resolve(new Response('{}',{status:200}));await start;
 assert.strictEqual(elements['btn-prep-cancel'].style.display,'inline-block');
 elements['prep-batch-mode'].checked=!batch;context.cancelPreparer();
 assert.strictEqual(cancels.at(-1),batch?'/api/preparer/batch/cancel':'/api/preparer/cancel');
 assert.strictEqual(polls.at(-1).name,batch?'batch_preparer':'preparer');
 polls.at(-1).options.onDone({running:false,status:'done',logs:[]});
 assert.strictEqual(elements['btn-prep-start'].disabled,false);assert.strictEqual(elements['btn-prep-cancel'].style.display,'none');
 context.cancelPreparer();assert.strictEqual(cancels.length,before+1,'idle controls do not target a checkbox');
}
""")

    def test_accepted_task_identity_survives_mode_edits(self):
        self.run_scenario(SETUP + r"""
context.fetch=async()=>new Response('{}',{status:200});
for(const batch of [false,true]){
 elements['prep-batch-mode'].checked=batch;if(batch){context.onPrepBatchFilesChange();}
 await context.startPreparer();elements['prep-batch-mode'].checked=!batch;
 context.cancelPreparer();assert.strictEqual(cancels.at(-1),batch?'/api/preparer/batch/cancel':'/api/preparer/cancel');
 polls.at(-1).options.onDone({running:false,status:'done',logs:[]});
}
""")

    def test_both_transports_use_shared_submission_and_restore_failure_controls(self):
        self.run_scenario(SETUP + r"""
const original=context._submitPreparer;assert.strictEqual(typeof original,'function');const dispatch=[];
context._submitPreparer=(task,fd)=>{dispatch.push(task);return original(task,fd);};
for(const batch of [false,true]){
 elements['prep-batch-mode'].checked=batch;if(batch){context.onPrepBatchFilesChange();}
 for(const failure of ['http','network']){
 context.fetch=async(url,request)=>{
  assert.strictEqual(request.method,'POST');const key=batch?'audio_files':'audio_file';
  const files=request.body.getAll(key);assert.strictEqual(files.length,1);
  assert.strictEqual(await files[0].text(),batch?'batch bytes':'single bytes');
  const cfg=JSON.parse(request.body.get('config_json'));assert.strictEqual(batch?cfg.tasks[0].audio_filename:cfg.audio_filename,batch?'batch.wav':'single.wav');
  if(failure==='network'){throw Error('offline');}return new Response('proxy failed',{status:503,statusText:'Service Unavailable'});};
 await context.startPreparer();assert.strictEqual(elements['btn-prep-start'].disabled,false);
 assert.strictEqual(elements['btn-prep-cancel'].style.display,'none');assert.strictEqual(polls.length,0);
 assert(toasts.at(-1)[0].startsWith(batch?'Batch preparer start is unconfirmed':'Preparer start is unconfirmed'));
 }
}
assert.deepStrictEqual(dispatch,['preparer','preparer','batch_preparer','batch_preparer']);
""")

    def test_status_markup_is_escaped_and_colors_are_constrained(self):
        self.run_scenario(SETUP + r"""
context._pollPreparerLogs('batch_preparer');
const attack='<img src=x onerror="boom()">';
for(const status of [attack,'constructor','done']){
 polls.at(-1).options.onTick({running:true,logs:[],tasks:[{status}]});
 const html=elements['prep-batch-status-0'].innerHTML;
 assert(!html.includes('<img'));assert(html.includes(status==='done'?'bg-success':'bg-secondary'));
 if(status===attack){assert(html.includes('&lt;img'));assert(html.includes('&quot;'));}
}
polls.at(-1).options.onDone({running:false,logs:[],status:'done'});
context._pollPreparerLogs('preparer');polls.at(-1).options.onDone({running:false,logs:[],status:attack});
assert(!elements['prep-status-msg'].innerHTML.includes('<img'));assert(elements['prep-status-msg'].innerHTML.includes('Preparation finished'));
""")

    def test_logs_keep_reader_position_and_show_rotating_ring(self):
        self.run_scenario(SETUP + r"""
const el=elements['preparer-logs'];el.style={};el.clientHeight=100;el.scrollHeight=1000;el.scrollTop=0;el.innerText='';el.appendChild=node=>{el.innerText+=node.textContent;};
context._pollPreparerLogs('preparer');const tick=polls.at(-1).options.onTick;
tick({run_id:'one',running:true,logs:['a','b','c']});assert.strictEqual(el.innerText,'a\nb\nc');assert.strictEqual(el.scrollTop,1000);
el.scrollTop=200;tick({run_id:'one',running:true,logs:['b','c','d']});assert.strictEqual(el.innerText,'b\nc\nd','same-length rotation must show new lines');assert.strictEqual(el.scrollTop,200);
tick({run_id:'one',running:true,logs:['b','c','d','e']});assert.strictEqual(el.innerText,'b\nc\nd\ne');assert.strictEqual(el.scrollTop,200);
context._pollPreparerLogs('batch_preparer');tick({run_id:'one',running:true,logs:['stale']});assert.strictEqual(el.innerText,'b\nc\nd\ne','previous task cannot overwrite current task logs');
""")

    def test_completion_feedback_reports_failed_cancelled_and_mixed_batches(self):
        self.run_scenario(SETUP + r"""
for(const task of ['preparer','batch_preparer']){for(const [state,tone,text] of [[{status:'done',logs:[]},'text-success','finished'],[{status:'failed',logs:[]},'text-danger','failed'],[{status:'cancelled',logs:[]},'text-warning','cancelled'],[{status:'done',logs:[],tasks:[{status:'failed'}]},'text-danger','failed'],[{status:'done',logs:[],tasks:[{status:'cancelled'}]},'text-warning','cancelled']]){
context._pollPreparerLogs(task);polls.at(-1).options.onDone({...state,running:false});const html=elements['prep-status-msg'].innerHTML;assert(html.includes(tone));assert(html.includes(text));assert(html.includes(task==='preparer'?'Preparation':'Batch preparation'));if(text==='failed'){assert(html.includes('Review the activity log before retrying'));}assert(!elements['btn-prep-start'].disabled);
}}
""")

    def test_older_poll_completion_cannot_clear_new_task_controls(self):
        self.run_scenario(SETUP + r"""
context._pollPreparerLogs('preparer');const old=polls.at(-1);
context._pollPreparerLogs('batch_preparer');const current=polls.at(-1);
old.options.onDone({running:false,status:'done',logs:[]});
assert.strictEqual(elements['btn-prep-start'].disabled,true);assert.strictEqual(elements['btn-prep-cancel'].style.display,'inline-block');assert.strictEqual(done.length,0);
elements['prep-batch-mode'].checked=false;context.cancelPreparer();assert.strictEqual(cancels.at(-1),'/api/preparer/batch/cancel');
current.options.onDone({running:false,status:'done',logs:[]});assert.deepStrictEqual(done,['batch_preparer']);
assert.strictEqual(elements['btn-prep-start'].disabled,false);
""")
