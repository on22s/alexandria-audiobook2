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

    def test_timestamp_ties_missing_times_empty_logs_and_failed_request_have_fixed_selection(self):
        self.run_js(r"""
statuses={};let rows={script:{start_time:100,logs:['script']},review:{start_time:200,logs:['review']},nicknames:{start_time:300,logs:[]}};
ctx.API.get=async path=>path==='/api/status'?statuses:{running:false,logs:[],...(rows[path.split('/').at(-1)]||{})};
await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'review');
rows={script:{start_time:200,logs:['first']},review:{start_time:200,logs:['tie']}};await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'first');
rows={script:{logs:['missing']},review:{start_time:'999',logs:['not numeric']}};await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'missing');
rows={batch_script:{logs:['available']}};ctx.API.get=async path=>{if(path==='/api/status'){return statuses;}if(path.endsWith('/script')){throw Error('unavailable');}return {running:false,logs:[],...(rows[path.split('/').at(-1)]||{})};};
await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'available');
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
assert.strictEqual(requests.length,10);assert.strictEqual(new Set(requests).size,10);assert.strictEqual(requests[0],'/api/status');
assert.deepStrictEqual(requests.slice(1).sort(),['script','batch_script','review','batch_review','nicknames','cast_list','persona','audio','voicelab'].map(name=>'/api/status/'+name).sort());
""")
