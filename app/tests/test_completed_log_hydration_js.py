"""Actual reload log selection with reversed requests, live polling, and stale restores."""
import unittest
from tests import test_task_reattachment as fixture


class CompletedLogHydrationTests(unittest.TestCase):
    run_js=fixture.TaskReattachmentJsTests.run_js

    def test_response_order_does_not_select_shared_log_and_shared_pane_written_once(self):
        self.run_js(r"""
const names=['script','batch_script','review','batch_review','nicknames'];
let writes=0,text='';const pane=el('script-logs');Object.defineProperty(pane,'innerText',{get:()=>text,set:v=>{writes++;text=v;}});
for(const order of [names,names.slice().reverse()]){
 const pending={};writes=0;text='existing';statuses=Object.fromEntries(names.map(name=>[name,{running:false}]));
 ctx.API.get=path=>path==='/api/status'?Promise.resolve(statuses):new Promise(resolve=>{
  const name=path.split('/').at(-1);if(names.includes(name)){pending[name]=resolve;}else{resolve({running:false,logs:[]});}});
 const restore=ctx.reattachRunningPollers();for(let i=0;i<8;i++){await Promise.resolve();}assert.strictEqual(writes,0);
 for(const name of order){pending[name]({running:false,start_time:name==='script'?200:100,logs:[name+' output']});}
 await restore;assert.strictEqual(writes,1);assert.strictEqual(text,'script output');assert.strictEqual(pane.title,'Restored script logs');
}
""")

    def test_timestamp_ties_missing_times_empty_logs_and_failed_request_does_not_substitute(self):
        self.run_js(r"""
statuses={};let rows={script:{start_time:100,logs:['script']},review:{start_time:200,logs:['review']},nicknames:{start_time:300,logs:[]}};
ctx.API.get=async path=>path==='/api/status'?statuses:{running:false,logs:[],...(rows[path.split('/').at(-1)]||{})};
await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'review');
rows={script:{start_time:200,logs:['first']},review:{start_time:200,logs:['tie']}};await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'first');
rows={script:{logs:['missing']},review:{start_time:'999',logs:['not numeric']}};await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'missing');
// A failed detail read (script here) must not be replaced by an available
// older task's log; the pane reports the unavailable history instead (#2191).
rows={batch_script:{logs:['available']}};ctx.API.get=async path=>{if(path==='/api/status'){return statuses;}if(path.endsWith('/script')){throw Error('unavailable');}return {running:false,logs:[],...(rows[path.split('/').at(-1)]||{})};};
await ctx.reattachRunningPollers();assert(el('script-logs').innerText.includes('could not be loaded'));
assert.strictEqual(el('script-logs').title,'');
""")

    def test_newest_failed_read_never_shows_an_older_run_as_restored(self):
        self.run_js(r"""
statuses={script:{running:false},review:{running:false}};
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 const name=path.split('/').at(-1);
 if(name==='review'){throw Error('newest read down');}
 if(name==='script'){return {running:false,start_time:100,logs:['13:05 older script log']};}
 return {running:false,start_time:0,logs:[]};
};
await ctx.reattachRunningPollers();
assert(el('script-logs').innerText.includes('could not be loaded'));
assert(!el('script-logs').innerText.includes('13:05'));
assert.strictEqual(el('script-logs').title,'');
""")

    def test_all_failed_reads_on_fresh_load_report_unavailable_history(self):
        self.run_js(r"""
statuses={};ctx.API.get=async path=>{if(path==='/api/status'){return statuses;}throw Error('total outage');};
await ctx.reattachRunningPollers();
for(const id of ['script-logs','voices-logs','audio-logs','voicelab-logs']){
 assert(el(id).innerText.includes('could not be loaded'),id+' was left blank');
 assert.strictEqual(el(id).title,'');
}
""")

    def test_confirmed_empty_history_stays_blank_without_a_diagnostic(self):
        self.run_js(r"""
statuses={};ctx.API.get=async path=>path==='/api/status'?statuses:{running:false,logs:[]};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'');
assert(!el('script-logs').innerText.includes('could not'));
""")

    def test_recovery_replaces_diagnostic_then_confirms_empty_state(self):
        self.run_js(r"""
statuses={};let failing=true;
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 if(failing){throw Error('outage');}
 if(path.endsWith('/script')){return {running:false,start_time:200,logs:['recovered log']};}
 return {running:false,logs:[]};
};
await ctx.reattachRunningPollers();
assert(el('script-logs').innerText.includes('could not be loaded'));
failing=false;await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'recovered log');
assert.strictEqual(el('script-logs').title,'Restored script logs');
failing=true;await ctx.reattachRunningPollers();
assert(el('script-logs').innerText.includes('could not be loaded'));
assert.strictEqual(el('script-logs').title,'');
ctx.API.get=async path=>path==='/api/status'?statuses:{running:false,logs:[]};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'');
assert.strictEqual(el('script-logs').title,'');
""")

    def test_late_failed_restore_cannot_overwrite_newer_live_content(self):
        self.run_js(r"""
statuses={};let resolveScript,rejectScript;
ctx.API.get=path=>{
 if(path==='/api/status'){return Promise.resolve(statuses);}
 if(path.endsWith('/script')){return new Promise((res,rej)=>{resolveScript=res;rejectScript=rej;});}
 return Promise.resolve({running:false,logs:[]});
};
const restore=ctx.reattachRunningPollers();for(let i=0;i<8;i++){await Promise.resolve();}
el('script-logs').innerText='newer live output';
rejectScript(Error('late outage'));await restore;
assert.strictEqual(el('script-logs').innerText,'newer live output');
""")

    def test_book_switch_or_fresh_run_during_hydration_blocks_the_diagnostic(self):
        self.run_js(r"""
ctx.window=ctx;statuses={};let resolveScript;
ctx.API.get=path=>{
 if(path==='/api/status'){return Promise.resolve(statuses);}
 if(path.endsWith('/script')){return new Promise(r=>resolveScript=r);}
 return Promise.resolve({running:false,logs:[]});
};
for(const change of [
 ()=>{ctx._scriptRecoveryBookEpoch=1;},
 ()=>{ctx._selectedUploadIdentity={filename:'B.txt',token:'t'};},
 ()=>{ctx._scriptRecoveryAcceptedRun=1;},
 ()=>{ctx._scriptRecoveryTerminalToken=1;},
]){
 el('script-logs').innerText='';ctx._scriptRecoveryBookEpoch=0;ctx._selectedUploadIdentity=null;
 ctx._scriptRecoveryAcceptedRun=0;ctx._scriptRecoveryTerminalToken=0;
 const restore=ctx.reattachRunningPollers();for(let i=0;i<8;i++){await Promise.resolve();}
 change();resolveScript({running:false,logs:['stale'],start_time:100});
 await restore;assert.strictEqual(el('script-logs').innerText,'');
}
""")

    def test_active_shared_task_prevents_idle_hydration_and_newly_running_status_reattaches(self):
        self.run_js(r"""
statuses={review:{running:true}};el('script-logs').innerText='live review';
await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'live review');assert(calls.includes('review'));
statuses={review:{running:false}};calls.length=0;ctx.API.get=async path=>path==='/api/status'?statuses:{running:path.endsWith('/review'),logs:['older snapshot'],start_time:100};
await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'live review');assert(calls.includes('review'));
""")

    def test_poll_generation_change_preserves_new_live_content_in_only_its_pane(self):
        self.run_js(r"""
ctx._pollGen={};statuses={};let resolveScript;
ctx.API.get=path=>path==='/api/status'?Promise.resolve(statuses):path.endsWith('/script')?new Promise(r=>resolveScript=r):Promise.resolve({running:false,logs:path.endsWith('/persona')?['restored persona']:[],start_time:100});
const restore=ctx.reattachRunningPollers();for(let i=0;i<8;i++){await Promise.resolve();}
ctx._pollGen['logs:script']=1;el('script-logs').innerText='new live script';resolveScript({running:false,logs:['old script'],start_time:200});await restore;
assert.strictEqual(el('script-logs').innerText,'new live script');assert.strictEqual(el('voices-logs').innerText,'restored persona');
""")

    def test_older_restore_cannot_overwrite_newer_restore(self):
        self.run_js(r"""
statuses={};let resolveOld,count=0;
ctx.API.get=path=>path==='/api/status'?Promise.resolve(statuses):path.endsWith('/script')?(++count===1?new Promise(r=>resolveOld=r):Promise.resolve({running:false,logs:['new restore'],start_time:200})):Promise.resolve({running:false,logs:[]});
const old=ctx.reattachRunningPollers();for(let i=0;i<8;i++){await Promise.resolve();}
await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'new restore');
resolveOld({running:false,logs:['late old restore'],start_time:100});await old;assert.strictEqual(el('script-logs').innerText,'new restore');
""")

    def test_idle_discovery_fetches_each_full_task_status_once(self):
        self.run_js(r"""
statuses={};await ctx.reattachRunningPollers();
assert.strictEqual(requests.length,11);assert.strictEqual(new Set(requests).size,10);
assert.strictEqual(requests[0],'/api/status');assert.strictEqual(requests.at(-1),'/api/status');
assert.deepStrictEqual(requests.slice(1,-1).sort(),['script','batch_script','review','batch_review','nicknames','cast_list','persona','audio','voicelab'].map(name=>'/api/status/'+name).sort());
""")

    def test_shared_pane_skew_detects_newer_run_in_both_branches(self):
        self.run_js(r"""
const reg=(start)=>({running:false,ownership_unconfirmed:false,status:'idle',start_time:start});
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
for(const fail of [false,true]){
 let clock=100,reads=0,resolveNick;
 const gate=new Promise(r=>resolveNick=r);
 ctx.API.get=async path=>{
  if(path==='/api/status'){reads++;return {review:reg(clock),script:reg(200),nicknames:{...reg(null),has_history:false}};}
  if(path.endsWith('/script')){return detail(['older script'],200);}
  if(path.endsWith('/review')){return detail(['first review'],100);}
  if(path.endsWith('/nicknames')){await gate;if(fail){throw Error('unavailable');}return detail([],null);}
  return detail([],null);
 };
 el('script-logs').innerText='';el('script-logs').title='';
 const work=ctx.reattachRunningPollers();for(let i=0;i<10;i++){await Promise.resolve();}
 clock=300;resolveNick();await work;
 assert.strictEqual(reads,2);
 assert(el('script-logs').innerText.includes('could not be loaded'),'branch '+fail);
 assert.strictEqual(el('script-logs').title,'');
}
""")

    def test_unchanged_identity_restores_newest_and_preserves_empty_and_legacy(self):
        self.run_js(r"""
const reg=(start)=>({running:false,ownership_unconfirmed:false,status:'idle',start_time:start});
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
statuses={script:reg(100),review:reg(200)};
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 if(path.endsWith('/script')){return detail(['older'],100);}
 if(path.endsWith('/review')){return detail(['newest'],200);}
 return detail([],null);
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'newest');
assert.strictEqual(el('script-logs').title,'Restored review logs');
statuses={};ctx.API.get=async path=>path==='/api/status'?statuses:detail([],null);
el('script-logs').innerText='';el('script-logs').title='';
await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'');
// Legacy summary without identity fields still restores a successful read.
statuses={script:{running:false,status:'idle'}};
ctx.API.get=async path=>path==='/api/status'?statuses:path.endsWith('/script')?detail(['legacy'],100):detail([],null);
await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'legacy');
""")

    def test_retained_detail_cannot_clobber_a_newer_live_line(self):
        self.run_js(r"""
const held={running:false,ownership_unconfirmed:true,status:'failed',start_time:200,has_history:true};
ctx.window=ctx;ctx._scriptRecoveryBookEpoch=0;statuses={review:held};
let reviewGets=0,resolveDetail;const gate=new Promise(r=>resolveDetail=r);
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 if(path.endsWith('/review')){
  reviewGets++;
  return reviewGets===1?gate:{running:true,ownership_unconfirmed:false,status:'running',
    start_time:999,has_history:true,logs:['STALE RETAINED DETAIL']};
 }
 return {running:false,logs:[],start_time:null};
};
await ctx.reattachRunningPollers();
const pane=el('script-logs');
const renderLogs=ctx.createTaskLogRenderer(pane);
renderLogs({running:true,start_time:500,logs:['NEW SCRIPT LIVE LINE']});
resolveDetail();for(let i=0;i<8;i++){await Promise.resolve();}
assert.strictEqual(reviewGets,1);
assert.strictEqual(pane.innerText,'NEW SCRIPT LIVE LINE');
""")

    def test_retained_detail_cannot_write_after_book_transition(self):
        self.run_js(r"""
const held={running:false,ownership_unconfirmed:true,status:'failed',start_time:200,has_history:true};
ctx.window=ctx;ctx._scriptRecoveryBookEpoch=0;statuses={review:held};
let reviewGets=0,resolveDetail;const gate=new Promise(r=>resolveDetail=r);
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 if(path.endsWith('/review')){
  reviewGets++;
  return reviewGets===1?gate:{running:true,ownership_unconfirmed:false,status:'running',
    start_time:999,has_history:true,logs:['OLD BOOK STALE DETAIL']};
 }
 return {running:false,logs:[],start_time:null};
};
await ctx.reattachRunningPollers();
ctx._scriptRecoveryBookEpoch=1;
const pane=el('script-logs');
const renderLogs=ctx.createTaskLogRenderer(pane);
renderLogs({running:true,start_time:600,logs:['NEW BOOK OWN RESULT']});
resolveDetail();for(let i=0;i<8;i++){await Promise.resolve();}
assert.strictEqual(reviewGets,1);
assert.strictEqual(pane.innerText,'NEW BOOK OWN RESULT');
""")

    def test_retained_detail_cannot_write_when_book_or_selection_changes_without_a_render(self):
        self.run_js(r"""
const held={running:false,ownership_unconfirmed:true,status:'failed',start_time:200,has_history:true};
const reset=()=>{ctx.window=ctx;ctx._scriptRecoveryBookEpoch=0;ctx._selectedUploadIdentity=null;
 ctx._scriptRecoveryAcceptedRun=0;ctx._scriptRecoveryTerminalToken=0;};
for(const [name,change,stale] of [
 ['book',()=>{ctx._scriptRecoveryBookEpoch=1;},'OLD BOOK STALE DETAIL'],
 ['selection',()=>{ctx._selectedUploadIdentity={filename:'B.txt'};},'OLD SELECTION STALE DETAIL']]){
 reset();statuses={review:held};
 let reviewGets=0,resolveDetail;const gate=new Promise(r=>resolveDetail=r);
 ctx.API.get=path=>{
  if(path==='/api/status'){return Promise.resolve(statuses);}
  if(path.endsWith('/review')){reviewGets++;return reviewGets===1?gate:Promise.resolve({...held,logs:['old cleanup']});}
  return Promise.resolve({running:false,logs:[],start_time:null});
 };
 await ctx.reattachRunningPollers();
 const pane=el('script-logs');pane.title='';
 assert(pane.innerText.includes('still holding'),name+' opening warning missing: '+pane.innerText);
 change();
 resolveDetail({running:true,ownership_unconfirmed:false,status:'running',start_time:999,has_history:true,logs:[stale]});
 for(let i=0;i<8;i++){await Promise.resolve();}
 assert(!pane.innerText.includes(stale),name+' change let a stale detail through');
 assert(pane.innerText.includes('still holding'),name+' lost the retained warning without a renderer write');
}
""")

    def test_retired_warning_releases_authority_for_a_later_restore(self):
        self.run_js(r"""
const held={running:false,ownership_unconfirmed:true,status:'failed',start_time:200,has_history:true};
const stopped={running:false,ownership_unconfirmed:false,status:'failed',start_time:200,has_history:true};
const scriptHistory={running:false,ownership_unconfirmed:false,status:'failed',start_time:100,has_history:true};
ctx.window=ctx;ctx._scriptRecoveryBookEpoch=0;ctx._selectedUploadIdentity=null;
ctx._scriptRecoveryAcceptedRun=0;ctx._scriptRecoveryTerminalToken=0;
let reads=0,reviews=0;
ctx.API.get=path=>{
 if(path==='/api/status'){reads++;return Promise.resolve(reads===1?{review:held,script:scriptHistory}:{review:stopped,script:scriptHistory});}
 if(path.endsWith('/review')){reviews++;return reviews===1?new Promise(()=>{}):Promise.resolve({...held,logs:['old cleanup']});}
 if(path.endsWith('/script')){return Promise.reject(Error('script down'));}
 return Promise.resolve({running:false,logs:[],start_time:null});
};
await ctx.reattachRunningPollers();
const afterFirst=el('script-logs').innerText;
assert(afterFirst.includes('could not be loaded'),'retired warning left the pane at: '+JSON.stringify(afterFirst));
ctx.API.get=path=>{
 if(path==='/api/status'){return Promise.resolve({});}
 if(path.endsWith('/script')){return Promise.resolve({running:false,ownership_unconfirmed:false,status:'idle',start_time:100,has_history:true,logs:['RESTORED HISTORY LINE']});}
 return Promise.resolve({running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false,logs:[]});
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'RESTORED HISTORY LINE');
assert.strictEqual(el('script-logs').title,'Restored script logs');
""")

    def test_script_completing_during_restore_reports_an_honest_diagnostic(self):
        self.run_js(r"""
ctx.window=ctx;ctx._scriptRecoveryBookEpoch=0;ctx._selectedUploadIdentity=null;
ctx._scriptRecoveryAcceptedRun=0;ctx._scriptRecoveryTerminalToken=0;
const before={running:true,ownership_unconfirmed:false,status:'running',start_time:200,has_history:true};
const after={running:false,ownership_unconfirmed:false,status:'idle',start_time:200,has_history:true};
let reads=0;
ctx.API.get=async path=>{
 if(path==='/api/status'){reads++;return reads===1?{script:before}:{script:after};}
 if(path.endsWith('/script')){return {...after,logs:['Task script completed successfully.']};}
 return {running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false,logs:[]};
};
await ctx.reattachRunningPollers();
assert.strictEqual(reads,2);
assert(el('script-logs').innerText.includes('could not be loaded'),'blank pane: '+JSON.stringify(el('script-logs').innerText));
assert.strictEqual(el('script-logs').title,'');
""")

    def test_retained_warning_clears_default_but_preserves_foreign_live_text(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
const retained={running:false,ownership_unconfirmed:true,status:'failed',start_time:200,has_history:true,logs:['old cleanup']};
el('voices-logs').innerText='';
ctx.reattachTaskActivity('persona',['btn-gen-personas'],'voices-logs',null,retained);
assert(el('voices-logs').innerText.includes('still holding processes'));
el('voices-logs').innerText='Newer live persona output';
ctx.poll.options.onTick({...retained,logs:['obsolete cleanup']});
assert.strictEqual(el('voices-logs').innerText,'Newer live persona output');
""")

    def test_successor_gets_its_own_poller_after_predecessor_retires(self):
        self.run_js(r"""
const live={running:true,ownership_unconfirmed:false,status:'running',start_time:null,has_history:false};
const detail=(logs,state)=>({...state,logs});
let reads=0,resolveFinal;
const gate=new Promise(r=>resolveFinal=r);
ctx.API.get=path=>{
 if(path==='/api/status'){reads++;return Promise.resolve({word_timing:live,...(reads===2?{lora_training:{...live,start_time:5}}:{})});}
 if(path.endsWith('/word_timing'))return Promise.resolve(detail(['first terminal'],{...live,running:false}));
 return Promise.resolve(detail([],live));
};
const work=ctx.reattachRunningPollers();for(let i=0;i<200&&reads<2;i++){await Promise.resolve();}
assert.strictEqual(reads,2);
await ctx.poll.options.onDone({running:false,logs:['first terminal']});
resolveFinal();await work;
assert.strictEqual(calls.filter(name=>name==='reattach:word_timing').length,2);
""")

    def test_history_restore_writes_inner_text_in_browser_faithful_dom(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
statuses={};ctx.API.get=async path=>path==='/api/status'?statuses:path.endsWith('/review')?detail(['verified review row'],200):detail([],null);
el('script-logs').innerText='';
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'verified review row');
assert.strictEqual(el('script-logs').textContent,'verified review row');
""")

    def test_task_activity_replaces_authored_default_status_text(self):
        self.run_js(r"""
const defaults={
 'editor-word-status':'Choose a saved line with audio, then time its words or analyze its internal sentence gaps. Uses the installed English speech-recognition model; no automatic download.',
 'editor-line-speech-status':'Saved takes with recorded synthesis input can be rechecked. Takes without recorded input must be regenerated with speech checks enabled.',
 'lmstudio-status-badge':'Checking...'};
for(const [task,id,tick] of [['word_timing','editor-word-status','Timing captured line'],
 ['speech_verification','editor-line-speech-status','Recheck finished'],
 ['lmstudio_optimize','lmstudio-status-badge','Connected']]){
 el(id).innerText=defaults[id];
 ctx.reattachTaskActivity(task,[],id,null,{running:true,status:'running',ownership_unconfirmed:false,logs:[]});
 assert(el(id).innerText.includes('running...'),task+' did not replace the default: '+el(id).innerText);
 ctx.poll.options.onTick({running:true,logs:[tick]});
 assert.strictEqual(el(id).innerText,tick,task+' did not accept its live line');
}
""")

    def test_dispatch_token_changes_during_final_attach_block_stale_history(self):
        self.run_js(r"""
const reg=(start,status)=>({running:false,ownership_unconfirmed:false,status,start_time:start,has_history:start!==null});
for(const token of ['book','selection','terminal']){
 let reads=0,resolveDetail,detailRequested=false;
 const gate=new Promise(r=>resolveDetail=r);
 ctx.window=ctx;
 ctx._scriptRecoveryBookEpoch=0;ctx._selectedUploadIdentity=null;ctx._scriptRecoveryTerminalToken=0;
 ctx.API.get=async path=>{
  if(path==='/api/status'){reads++;return reads===1?{}:{review:reg(100,'done'),dataset_builder:{running:true,ownership_unconfirmed:false,status:'running',start_time:200,has_history:true}};}
  if(path.endsWith('/dataset_builder')){detailRequested=true;await gate;return {running:true,dataset_name:'B'};}
  if(path.endsWith('/review'))return {running:false,logs:['Completed review line'],start_time:100};
  return {running:false,logs:[],start_time:null};
 };
 el('script-logs').innerText='';el('script-logs').title='';
 const work=ctx.reattachRunningPollers();for(let i=0;i<200&&!detailRequested;i++){await Promise.resolve();}
 assert(detailRequested,'final-only detail was never awaited');
 if(token==='book'){ctx._scriptRecoveryBookEpoch=7;}
 else if(token==='selection'){ctx._selectedUploadIdentity={filename:'B'};}
 else {ctx._scriptRecoveryTerminalToken=9;}
 resolveDetail();await work;
 assert.strictEqual(el('script-logs').innerText,'',token+' let stale history through');
}
""")

    def test_live_write_during_first_registry_survives_failed_detail(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
let releaseFirst,reads=0;const gate=new Promise(r=>releaseFirst=r);
ctx.API.get=async path=>{
 if(path==='/api/status'){reads++;if(reads===1){await gate;}return {};}
 if(path.endsWith('/review')){throw Error('detail down');}
 return detail([],null);
};
const pane=el('script-logs');const work=ctx.reattachRunningPollers();
pane.innerText='Verified terminal review line';pane.title='existing tooltip';releaseFirst();await work;
assert.strictEqual(reads,2);
assert.strictEqual(pane.innerText,'Verified terminal review line');
assert.strictEqual(pane.title,'existing tooltip');
""")

    def test_final_registry_live_and_retained_owners_get_pollers(self):
        self.run_js(r"""
const base=(extra={})=>({running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false,...extra});
for(const mode of ['running','pending','retained']){
 const name=mode==='pending'?'script':'review';
 const owner=mode==='retained'?base({status:'failed',start_time:300,ownership_unconfirmed:true,has_history:true})
   :base({status:mode==='pending'?'starting':'running',start_time:mode==='pending'?null:300,
          running:true,has_history:mode!=='pending',worker_pending:mode==='pending'});
 let reads=0;
 ctx.API.get=async path=>{
  if(path==='/api/status'){reads++;return reads===1?{}:{[name]:owner};}
  if(path.endsWith('/'+name)){return {...owner,logs:[]};}
  return {running:false,ownership_unconfirmed:false,logs:[],start_time:null};
 };
 calls.length=0;el('script-logs').innerText='';el('script-logs').title='';
 await ctx.reattachRunningPollers();
 assert.strictEqual(reads,2);
 assert(calls.some(call=>call.includes(name)),'no poller for final '+mode+': '+JSON.stringify(calls));
}
""")

    def test_released_retained_owner_does_not_erase_newer_pane_output(self):
        self.run_js(r"""
let releaseRecheck,reads=0;const gate=new Promise(r=>releaseRecheck=r);
const retained={running:false,ownership_unconfirmed:true,status:'failed',start_time:200,has_history:true};
const released={running:false,ownership_unconfirmed:false,status:'failed',start_time:200,has_history:true};
ctx.API.get=async path=>{
 if(path==='/api/status'){reads++;if(reads===2){await gate;}return {review:reads===1?retained:released};}
 return {...retained,logs:['old cleanup info']};
};
const work=ctx.reattachRunningPollers();for(let i=0;i<10;i++){await Promise.resolve();}
el('script-logs').innerText='Current worker output';releaseRecheck();await work;
assert.strictEqual(el('script-logs').innerText,'Current worker output');
""")

    def test_node_replacement_at_every_await_blocks_history_write(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
let releaseFirst,releaseDetail,releaseRecheck,stage=0;
const gates=[new Promise(r=>releaseFirst=r),new Promise(r=>releaseDetail=r),new Promise(r=>releaseRecheck=r)];
ctx.API.get=async path=>{
 if(path==='/api/status'){return stage===0?gates[0]:(stage===2?gates[2]:{script:{running:false,status:'idle',start_time:200,has_history:true}});}
 if(path.endsWith('/script')){await gates[1];return detail(['stale restore'],200);}
 return detail([],null);
};
const original=el('script-logs');original.innerText='owned text';original.title='owner title';
const work=ctx.reattachRunningPollers();for(let i=0;i<10;i++){await Promise.resolve();}
const replacement={disabled:false,style:{display:'none'},_text:'owned text',_title:'owner title',
 get innerText(){return this._text;},set innerText(v){this._text=String(v);},
 get textContent(){return this._text;},set textContent(v){this._text=String(v);},
 get title(){return this._title;},set title(v){this._title=String(v);}};
elements['script-logs']=replacement;releaseFirst({});for(let i=0;i<10;i++){await Promise.resolve();}
stage=1;releaseDetail();for(let i=0;i<10;i++){await Promise.resolve();}
stage=2;releaseRecheck({});await work;
assert.strictEqual(original.innerText,'owned text');
assert.strictEqual(replacement.innerText,'owned text');
assert.strictEqual(replacement.title,'owner title');
""")

    def test_known_idle_failed_read_does_not_mask_a_valid_newest_log(self):
        self.run_js(r"""
const reg=(status,start,history)=>({running:false,ownership_unconfirmed:false,status,start_time:start,has_history:history});
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
statuses={script:reg('idle',1420,true),cast_list:reg('idle',null,false)};
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 const name=path.split('/').at(-1);
 if(name==='cast_list'){throw Error('never-started task detail down');}
 if(name==='script'){return detail(['valid newest log'],1420);}
 return detail([],0);
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'valid newest log');
assert.strictEqual(el('script-logs').title,'Restored script logs');
""")

    def test_older_failed_read_does_not_mask_a_valid_newest_log(self):
        self.run_js(r"""
const reg=(status,start)=>({running:false,ownership_unconfirmed:false,status,start_time:start});
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
statuses={script:reg('idle',100),review:reg('idle',200)};
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 const name=path.split('/').at(-1);
 if(name==='script'){throw Error('older task detail down');}
 if(name==='review'){return detail(['14:20 newest'],200);}
 return detail([],0);
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'14:20 newest');
assert.strictEqual(el('script-logs').title,'Restored review logs');
""")

    def test_newest_failed_read_with_real_start_time_stays_honest(self):
        self.run_js(r"""
const reg=(status,start)=>({running:false,ownership_unconfirmed:false,status,start_time:start});
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
statuses={script:reg('idle',1305),review:reg('idle',1420)};
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 const name=path.split('/').at(-1);
 if(name==='review'){throw Error('newest task detail down');}
 if(name==='script'){return detail(['13:05 older'],1305);}
 return detail([],0);
};
await ctx.reattachRunningPollers();
assert(el('script-logs').innerText.includes('could not be loaded'));
assert(!el('script-logs').innerText.includes('13:05'));
assert.strictEqual(el('script-logs').title,'');
""")

    def test_unknown_and_legacy_registry_failures_stay_honest(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
for(const registry of [{}, {script:{running:false,ownership_unconfirmed:false,status:'idle'}}]){
 statuses=registry;
 ctx.API.get=async path=>{
  if(path==='/api/status'){return statuses;}
  if(path.endsWith('/script')){throw Error('unknown detail down');}
  if(path.endsWith('/review')){return detail(['review log'],200);}
  return detail([],0);
 };
 el('script-logs').innerText='';el('script-logs').title='';
 await ctx.reattachRunningPollers();
 assert(el('script-logs').innerText.includes('could not be loaded'));
 assert(!el('script-logs').innerText.includes('review log'));
 assert.strictEqual(el('script-logs').title,'');
}
""")

    def test_fresh_and_active_claims_are_preserved(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
statuses={script:{running:true,ownership_unconfirmed:false,status:'running',start_time:10},
 review:{running:false,ownership_unconfirmed:true,status:'failed',start_time:20}};
el('script-logs').innerText='live claim';
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 if(path.endsWith('/script')){return {running:true,ownership_unconfirmed:false,logs:['live'],start_time:10};}
 return {running:false,ownership_unconfirmed:true,status:'failed',logs:['cleanup detail'],start_time:20};
};
calls.length=0;await ctx.reattachRunningPollers();
assert(calls.includes('script'));assert(calls.some(name=>name.includes('review')));
assert.strictEqual(el('script-logs').innerText,'live claim');
""")

    def test_null_start_time_without_history_flag_is_unknown_not_never_started(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
statuses={persona:{running:false,ownership_unconfirmed:false,status:'idle',start_time:null}};
ctx.API.get=async path=>{
 if(path==='/api/status'){return statuses;}
 if(path.endsWith('/persona')){throw Error('completed persona detail down');}
 return detail([],null);
};
await ctx.reattachRunningPollers();
assert(el('voices-logs').innerText.includes('could not be loaded'));
assert.strictEqual(el('voices-logs').title,'');
""")

    def test_registry_recheck_detects_newer_run_between_reads(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
const reg=(start,status)=>({running:false,ownership_unconfirmed:false,status,start_time:start,has_history:true});
let reviewStart=100,reads=0;
ctx.API.get=async path=>{
 if(path==='/api/status'){reads++;return {script:reg(1305,'idle'),review:reg(reviewStart,'idle')};}
 const name=path.split('/').at(-1);
 if(name==='review'){reviewStart=2000;throw Error('newer review finished before detail');}
 if(name==='script'){return detail(['older script'],1305);}
 return detail([],0);
};
await ctx.reattachRunningPollers();
assert.strictEqual(reads,2);
assert(el('script-logs').innerText.includes('could not be loaded'));
assert(!el('script-logs').innerText.includes('older script'));
""")

    def test_registry_recheck_detects_changed_status_and_failed_recheck(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
for(const mode of ['status','recheck']){
 let status='idle',reads=0;
 ctx.API.get=async path=>{
  if(path==='/api/status'){
   reads++;
   if(mode==='recheck'&&reads===2){throw Error('recheck down');}
   if(mode==='status'&&reads===2){status='done';}
   return {script:{running:false,ownership_unconfirmed:false,status,start_time:100,has_history:true},
           review:{running:false,ownership_unconfirmed:false,status:'idle',start_time:200,has_history:true}};
  }
  const name=path.split('/').at(-1);
  if(name==='script'){throw Error('older detail down');}
  if(name==='review'){return detail(['newest'],200);}
  return detail([],0);
 };
 el('script-logs').innerText='';el('script-logs').title='';
 await ctx.reattachRunningPollers();
 assert(el('script-logs').innerText.includes('could not be loaded'));
 assert(!el('script-logs').innerText.includes('newest'));
}
""")

    def test_recheck_await_preserves_a_newer_pane_owner(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
const reg=(start)=>({running:false,ownership_unconfirmed:false,status:'idle',start_time:start,has_history:true});
let resolveRecheck,first=true;
ctx.API.get=path=>{
 if(path==='/api/status'){
  if(first){first=false;return Promise.resolve({script:reg(100),review:reg(200)});}
  return new Promise(r=>resolveRecheck=r);
 }
 const name=path.split('/').at(-1);
 if(name==='script'){return Promise.reject(Error('older detail down'));}
 if(name==='review'){return Promise.resolve(detail(['newest'],200));}
 return Promise.resolve(detail([],0));
};
el('script-logs').innerText='owner text';el('script-logs').title='owner title';
const restore=ctx.reattachRunningPollers();for(let i=0;i<10;i++){await Promise.resolve();}
el('script-logs').innerText='newer owner';el('script-logs').title='newer owner title';
resolveRecheck({script:reg(100),review:reg(200)});await restore;
assert.strictEqual(el('script-logs').innerText,'newer owner');
assert.strictEqual(el('script-logs').title,'newer owner title');
""")

    def test_text_title_and_element_identity_writer_controls_block_restore(self):
        self.run_js(r"""
const detail=(logs,start)=>({running:false,ownership_unconfirmed:false,logs,start_time:start});
for(const change of ['text','title','element']){
 statuses={};let resolveScript;
 ctx.API.get=path=>path==='/api/status'?Promise.resolve(statuses):path.endsWith('/script')?new Promise(r=>resolveScript=r):Promise.resolve(detail([],0));
 const original=el('script-logs');original.innerText='owned text';original.title='owner A';
 const restore=ctx.reattachRunningPollers();for(let i=0;i<8;i++){await Promise.resolve();}
 let replacement=null;
 if(change==='text'){original.innerText='newer live output';}
 else if(change==='title'){original.title='owner B';}
 else {replacement={disabled:false,style:{display:'none'},innerText:'',title:''};elements['script-logs']=replacement;}
 resolveScript(detail(['stale restored'],2000));await restore;
 if(change==='element'){
  assert.strictEqual(original.innerText,'owned text');
  assert.strictEqual(original.title,'owner A');
  assert.strictEqual(replacement.innerText,'');assert.strictEqual(replacement.title,'');
 } else {
  assert.strictEqual(original.innerText,change==='text'?'newer live output':'owned text');
  assert.strictEqual(original.title,change==='text'?'owner A':'owner B');
 }
 elements['script-logs']=original;
}
""")

    # ── r10 findings: pane authority / owner protocol ──────────────
    def test_config_first_startup_rebaselines_once_and_restores_all_panes(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const done=n=>({...idle,status:'done',start_time:n,has_history:true});
let releaseOpening,reads=0;
const opening=new Promise(r=>releaseOpening=r);
const panes=['script','voices','audio','voicelab'];
const completed={script:done(100),persona:done(200),audio:done(300),voicelab:done(400)};
ctx.API.get=path=>{
 if(path==='/api/status'){reads++;return reads===1?opening:Promise.resolve(completed);}
 const name=path.split('/').at(-1);
 if(name==='script'){return Promise.reject(Error('script detail down'));}
 return Promise.resolve({...completed[name],logs:[name+'-history']});
};
el('script-logs').innerText='';el('voices-logs').innerText='';el('audio-logs').innerText='';el('voicelab-logs').innerText='';
const work=ctx.reattachRunningPollers();
// The initial configuration installs the saved book while the opening
// registry is still pending. This is not a user selection.
ctx._scriptRecoveryBookEpoch=1;
releaseOpening(completed);
await work;
assert.strictEqual(reads,2);
for(const [pane,task] of [['voices','persona'],['audio','audio'],['voicelab','voicelab']]){assert.strictEqual(el(pane+'-logs').innerText,task+'-history',pane);}
assert(el('script-logs').innerText.includes('could not be loaded'));
""")

    def test_genuine_user_selection_during_startup_still_fences_history(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const done=n=>({...idle,status:'done',start_time:n,has_history:true});
let releaseOpening;const opening=new Promise(r=>releaseOpening=r);let reads=0;
const completed={script:done(100),persona:done(200),audio:done(300),voicelab:done(400)};
ctx.API.get=path=>{
 if(path==='/api/status'){reads++;return reads===1?opening:Promise.resolve(completed);}
 const name=path.split('/').at(-1);
 return Promise.resolve({...completed[name],logs:[name+'-history']});
};
el('script-logs').innerText='';el('voices-logs').innerText='';el('audio-logs').innerText='';el('voicelab-logs').innerText='';
const work=ctx.reattachRunningPollers();
// A user picked another book before the opening registry settled.
ctx._bookSelectionEpoch=1;ctx._scriptRecoveryBookEpoch=1;
releaseOpening(completed);
await work;
for(const name of ['script','voices','audio','voicelab']){
 assert.strictEqual(el(name+'-logs').innerText,'',name+' history leaked across a user selection');
}
""")

    def test_config_late_triggers_exactly_one_fresh_history_pass(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const done=n=>({...idle,status:'done',start_time:n,has_history:true});
const completed={script:done(100),persona:done(200),audio:done(300),voicelab:done(400)};
let reads=0;
ctx.API.get=path=>{
 if(path==='/api/status'){reads++;return Promise.resolve(completed);}
 const name=path.split('/').at(-1);
 if(name==='script'){return Promise.reject(Error('script detail down'));}
 return Promise.resolve({...completed[name],logs:[name+'-history']});
};
ctx._initialConfigApplied=false;
let releaseConfig;ctx._initialConfigReady=new Promise(r=>releaseConfig=r);
await ctx.reattachRunningPollers();
const firstPassReads=reads;
assert(firstPassReads>=2);
releaseConfig();ctx._initialConfigApplied=true;
for(let i=0;i<20;i++){await Promise.resolve();}
assert.strictEqual(reads,firstPassReads+2,'config-late must reconcile exactly once');
for(const [pane,task] of [['voices','persona'],['audio','audio'],['voicelab','voicelab']]){assert.strictEqual(el(pane+'-logs').innerText,task+'-history',pane);}
assert(el('script-logs').innerText.includes('could not be loaded'));
""")

    def test_live_successor_with_new_finite_start_time_gets_poller_and_cancel(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const heldA={...idle,status:'failed',start_time:100,ownership_unconfirmed:true,has_history:true};
const liveB={...idle,status:'running',start_time:200,running:true,has_history:true};
let reads=0;
ctx.API.get=path=>{
 if(path==='/api/status'){reads++;return Promise.resolve(reads===1?{review:heldA}:{review:liveB});}
 if(path.endsWith('/review')){return new Promise(()=>{});}
 return Promise.resolve({running:false,logs:[],start_time:null});
};
assert.notStrictEqual(ctx.getRunIdentity('review',heldA),ctx.getRunIdentity('review',liveB));
ctx._showReviewControls=v=>{el('btn-cancel-review').style.display=v?'inline-block':'none';};
await ctx.reattachRunningPollers();
assert(calls.includes('review'),'successor live Review poll missing: '+JSON.stringify(calls));
assert.strictEqual(el('btn-cancel-review').style.display,'inline-block');
assert(el('script-logs').innerText==='' || el('script-logs').innerText.includes('still holding'));
""")

    def test_accepted_script_owner_fences_a_late_same_run_review_detail(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const held={...idle,status:'failed',start_time:100,ownership_unconfirmed:true,has_history:true};
for(const accepted of [true,false]){
 let reads=0,resolveDetail;
 const gate=new Promise(r=>resolveDetail=r);
 ctx.window=ctx;ctx._scriptRecoveryBookEpoch=0;ctx._selectedUploadIdentity=null;
 ctx._scriptRecoveryAcceptedRun=0;ctx._scriptRecoveryTerminalToken=0;
 ctx.API.get=path=>{
  if(path==='/api/status'){return Promise.resolve({review:held});}
  if(path.endsWith('/review')){reads++;return gate;}
  return Promise.resolve({running:false,logs:[],start_time:null});
 };
 await ctx.reattachRunningPollers();
 el('script-logs').innerText='';
 ctx._scriptRecoveryAcceptedRun=7;
 ctx.getScriptAdmissionByToken=token=>accepted&&token===7?{token:7,state:'accepted'}:null;
 resolveDetail({running:true,ownership_unconfirmed:false,status:'running',start_time:100,logs:['SAME RUN REVIEW']});
 for(let i=0;i<10;i++){await Promise.resolve();}
 if(accepted){assert(!el('script-logs').innerText.includes('SAME RUN REVIEW'));}
 else {assert(el('script-logs').innerText.includes('SAME RUN REVIEW'),'refused bookkeeping hid a legitimate detail');}
}
""")

    def test_retained_poll_obeys_book_fence_but_unchanged_book_still_paints(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const held={...idle,status:'failed',start_time:100,ownership_unconfirmed:true,has_history:true};
for(const change of [true,false]){
 ctx.window=ctx;ctx._scriptRecoveryBookEpoch=0;ctx._selectedUploadIdentity=null;
 ctx.API.get=path=>{
  if(path==='/api/status'){return Promise.resolve({review:held});}
  if(path.endsWith('/review')){return new Promise(()=>{});}
  return Promise.resolve({running:false,logs:[],start_time:null});
 };
 await ctx.reattachRunningPollers();
 assert.strictEqual(ctx.poll.key,'reattach:review');
 if(change){ctx._scriptRecoveryBookEpoch=1;}
 ctx.poll.options.onTick({running:true,ownership_unconfirmed:false,status:'running',start_time:200,logs:['POLL LINE']});
 if(change){assert(!el('script-logs').innerText.includes('POLL LINE'));}
 else {assert.strictEqual(el('script-logs').innerText,'POLL LINE');}
}
""")

    def test_opening_registry_cannot_revoke_a_newer_renderer(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const held={...idle,status:'failed',start_time:100,ownership_unconfirmed:true,has_history:true};
const terminal={...held,ownership_unconfirmed:false,running:false,status:'failed'};
let releaseOpening;const opening=new Promise(r=>releaseOpening=r);let reads=0;
ctx.API.get=path=>{
 if(path==='/api/status'){reads++;return reads===1?opening:Promise.resolve({review:terminal});}
 if(path.endsWith('/review')){return Promise.resolve({...terminal,logs:['Old Review cleanup finished']});}
 return Promise.resolve({running:false,logs:[],start_time:null});
};
const work=ctx.reattachRunningPollers();
ctx.createTaskLogRenderer(el('script-logs'),'script')({running:true,start_time:200,logs:['Newer Script finished']});
releaseOpening({review:held});
await work;
assert.strictEqual(el('script-logs').innerText,'Newer Script finished');
ctx.poll.options.onTick({...terminal,logs:['Old Review cleanup finished']});
assert.strictEqual(el('script-logs').innerText,'Newer Script finished');
""")

    def test_retained_failure_supersedes_restored_persona_success(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const stamp={...idle,status:'done',start_time:100,has_history:true};
const failure={...idle,status:'failed',start_time:200,ownership_unconfirmed:true,has_history:true};
let phase=0;
ctx.API.get=path=>{
 if(path==='/api/status'){return Promise.resolve({persona:phase?failure:stamp});}
 if(path.endsWith('/persona')){return Promise.resolve({...(phase?failure:stamp),logs:phase?['Error: worker still held']:['Previous persona completed successfully']});}
 return Promise.resolve({running:false,logs:[],start_time:null});
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('voices-logs').innerText,'Previous persona completed successfully');
assert.strictEqual(el('voices-logs').title,'Restored persona logs');
phase=1;await ctx.reattachRunningPollers();
for(let i=0;i<10;i++){await Promise.resolve();}
assert(el('voices-logs').innerText.includes('still holding'));
assert.notStrictEqual(el('voices-logs').title,'Restored persona logs');
assert(el('btn-gen-personas').disabled);
""")

    def test_unresolved_newest_failure_keeps_an_already_visible_renderer(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
for(const sibling of [false,true]){
 statuses={script:{...idle,status:'idle',start_time:900,has_history:true},
            review:sibling?{...idle,status:'done',start_time:100,has_history:true}:idle};
 ctx.API.get=path=>{
  if(path==='/api/status'){return Promise.resolve(statuses);}
  if(path.endsWith('/script')){return Promise.reject(Error('newest read down'));}
  if(path.endsWith('/review')){return Promise.resolve({logs:['older review'],start_time:100});}
  return Promise.resolve({running:false,logs:[],start_time:null});
 };
 el('script-logs').innerText='';el('script-logs').title='';
 ctx.createTaskLogRenderer(el('script-logs'))({running:false,start_time:900,logs:['Already visible final Script result']});
 await ctx.reattachRunningPollers();
 assert.strictEqual(el('script-logs').innerText,'Already visible final Script result','sibling '+sibling);
}
""")

    def test_identical_overlapping_restore_keeps_the_newer_commit(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const saved={...idle,status:'done',start_time:100,has_history:true};
let stage=1,releaseDataset,datasetRequested=false,registries=0;
const datasetGate=new Promise(r=>releaseDataset=r);
ctx.API.get=path=>{
 if(path==='/api/status'){
  registries++;
  // The final-only Dataset Builder attachment appears in the recheck only.
  return Promise.resolve(stage===1&&registries===2?{script:saved,dataset_builder:{...idle,running:true,status:'running',start_time:300,has_history:true}}:{script:saved});
 }
 if(path.endsWith('/script')){return Promise.resolve({...saved,logs:['Saved completion']});}
 if(path.endsWith('/dataset_builder')){datasetRequested=true;return datasetGate.then(()=>({running:false}));}
 return Promise.resolve({running:false,logs:[],start_time:null});
};
const first=ctx.reattachRunningPollers();
for(let i=0;i<200&&!datasetRequested;i++){await Promise.resolve();}
assert(datasetRequested,'first restore did not reach the final attachment');
assert.strictEqual(el('script-logs').innerText,'Saved completion');
stage=2;await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'Saved completion');
releaseDataset();
await first;
assert.strictEqual(el('script-logs').innerText,'Saved completion','older restore rolled back an identical newer history');
assert.strictEqual(el('script-logs').title,'Restored script logs');
""")

    def test_successive_restores_retire_the_old_observer_before_the_successor(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const held=n=>({...idle,status:'failed',start_time:n,ownership_unconfirmed:true,has_history:true});
const stops=[],keys=[];
ctx._pollGen={};
const originalStart=ctx._startPolling;
ctx._startPolling=(key,fetch,options)=>{
 keys.push(key);
 if(key.startsWith('reattach:')){return ()=>{stops.push(key);ctx._pollGen[key]=(ctx._pollGen[key]||0)+1;};}
 return ()=>{stops.push(key);};
};
let reads=0;
ctx.API.get=path=>{
 if(path==='/api/status'){reads++;return Promise.resolve({review:reads<=2?held(100):held(200)});}
 if(path.endsWith('/review')){return Promise.resolve({...held(100),logs:['cleanup']});}
 return Promise.resolve({running:false,logs:[],start_time:null});
};
await ctx.reattachRunningPollers();
assert(keys.includes('reattach:review'));
await ctx.reattachRunningPollers();
assert(stops.includes('reattach:review'),'old retained observer was not retired on the owner change');
""")

    def test_script_snapshot_button_follows_only_a_verified_script_owner(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const held={...idle,status:'failed',start_time:100,ownership_unconfirmed:true,has_history:true};
for(const task of ['review','persona','script']){
 let reads=0,resolveDetail;
 const gate=new Promise(r=>resolveDetail=r);
 ctx.window=ctx;ctx._scriptRecoveryBookEpoch=0;ctx._selectedUploadIdentity=null;
 ctx._scriptRecoveryAcceptedRun=0;ctx._scriptRecoveryTerminalToken=0;
 ctx.API.get=path=>{
  if(path==='/api/status'){return Promise.resolve({[task]:held});}
  if(path.endsWith('/'+task)){reads++;return gate;}
  return Promise.resolve({running:false,logs:[],start_time:null});
 };
 let snaps=0;ctx.syncSnapshotButton=()=>{snaps++;};
 await ctx.reattachRunningPollers();
 resolveDetail({running:true,ownership_unconfirmed:false,status:'running',start_time:200,logs:[task+' is active']});
 for(let i=0;i<10;i++){await Promise.resolve();}
 if(task==='script'){assert(snaps>=1,'script snapshot not synced');}
 else {assert.strictEqual(snaps,0,task+' show-cased the Script-only snapshot button');}
}
""")

    def test_live_renderer_clears_a_restored_history_tooltip(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const done={...idle,status:'done',start_time:100,has_history:true};
ctx.API.get=path=>{
 if(path==='/api/status'){return Promise.resolve({review:done});}
 if(path.endsWith('/review')){return Promise.resolve({...done,logs:['Review done']});}
 return Promise.resolve({running:false,logs:[],start_time:null});
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').title,'Restored review logs');
ctx.createTaskLogRenderer(el('script-logs'),'script')({running:true,start_time:200,logs:['Current Script output']});
assert.strictEqual(el('script-logs').innerText,'Current Script output');
assert.strictEqual(el('script-logs').title,'');
""")

    def test_dom_replacement_is_revalidated_at_the_detail_continuation(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const held={...idle,status:'failed',start_time:100,ownership_unconfirmed:true,has_history:true};
let reads=0,resolveDetail;
const gate=new Promise(r=>resolveDetail=r);
ctx.API.get=path=>{
 if(path==='/api/status'){return Promise.resolve({review:held});}
 if(path.endsWith('/review')){reads++;return gate;}
 return Promise.resolve({running:false,logs:[],start_time:null});
};
await ctx.reattachRunningPollers();
const original=el('script-logs');original.innerText='owned text';original.title='owner title';
const replacement={disabled:false,style:{display:'none'},_text:'replacement text',_title:'replacement title',
 get innerText(){return this._text;},set innerText(v){this._text=String(v);},
 get textContent(){return this._text;},set textContent(v){this._text=String(v);},
 get title(){return this._title;},set title(v){this._title=String(v);}};
elements['script-logs']=replacement;
resolveDetail({running:true,ownership_unconfirmed:false,status:'running',start_time:100,logs:['DETAIL INTO REPLACEMENT']});
for(let i=0;i<10;i++){await Promise.resolve();}
assert.strictEqual(replacement.innerText,'replacement text');
assert.strictEqual(replacement.title,'replacement title');
assert(!replacement.innerText.includes('DETAIL INTO REPLACEMENT'));
""")

    def test_retired_record_guard_blocks_a_same_run_detail(self):
        self.run_js(r"""
const idle={running:false,ownership_unconfirmed:false,status:'idle',start_time:null,has_history:false};
const held={...idle,status:'failed',start_time:100,ownership_unconfirmed:true,has_history:true};
const stopped={...held,ownership_unconfirmed:false,running:false,status:'failed'};
const sameRun={running:true,ownership_unconfirmed:false,status:'running',start_time:100,logs:['RETIRED FLAG STALE']};
let reads=0,resolveDetail;
const gate=new Promise(r=>resolveDetail=r);
ctx.API.get=path=>{
 if(path==='/api/status'){return Promise.resolve({review:held});}
 if(path.endsWith('/review')){reads++;return gate;}
 return Promise.resolve({running:false,logs:[],start_time:null});
};
await ctx.reattachRunningPollers();
ctx.poll.options.onTick({...stopped,start_time:100});
assert.strictEqual(ctx.poll.options.doneCheck({...stopped}),true);
resolveDetail(sameRun);
for(let i=0;i<10;i++){await Promise.resolve();}
assert(!el('script-logs').innerText.includes('RETIRED FLAG STALE'),'retired record still accepted a detail');
""")

    def test_batch_preparation_never_clears_the_shared_script_log(self):
        # S1: preparation used to wipe script-logs before /batch/preflight, so a
        # cancelled/early-returning preflight destroyed an active Review's lines.
        # The only admitted clear is the accepted batch renderer's reset path.
        source = (fixture.Path(__file__).resolve().parent.parent / 'static/js/app-core.js').read_text()
        start = source.index('async function _startBatchScript(')
        depth = 0
        body_end = None
        for index in range(start, len(source)):
            char = source[index]
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    body_end = index + 1
                    break
        self.assertIsNotNone(body_end)
        body = source[start:body_end]
        for clear in ("script-logs", "innerHTML = ''", "innerHTML=''", "textContent = ''",
                      "innerText = ''"):
            self.assertNotIn(clear, body, f'batch preparation clears the shared pane: {clear}')
        self.assertIn("createTaskLogRenderer(logEl, 'batch_script')", source)
