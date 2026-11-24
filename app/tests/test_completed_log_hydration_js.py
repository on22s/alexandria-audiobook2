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
await ctx.reattachRunningPollers();assert.strictEqual(el('script-logs').innerText,'Earlier log history could not be loaded. Check the connection, then reload.');
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


MESSAGE = 'Earlier log history could not be loaded. Check the connection, then reload.'


class CompletedLogHydrationDiagnosticTests(unittest.TestCase):
    """A failed per-pane hydration reports itself instead of showing an older run."""

    run_js = fixture.TaskReattachmentJsTests.run_js

    def test_every_pane_failure_writes_the_localized_line_and_keeps_title_empty(self):
        self.run_js(r"""
const M='Earlier log history could not be loaded. Check the connection, then reload.';
statuses={};
ctx.API.get=async path=>{if(path==='/api/status'){return statuses;}throw Error('offline');};
await ctx.reattachRunningPollers();
for(const id of ['script-logs','voices-logs','audio-logs','voicelab-logs']){
  assert.strictEqual(el(id).innerText,M,id);
  assert.strictEqual(el(id).title,'',id);
}
""")

    def test_newest_failed_hydration_never_shows_an_older_run_log(self):
        self.run_js(r"""
const M='Earlier log history could not be loaded. Check the connection, then reload.';
statuses={review:{running:false,start_time:200},script:{running:false,start_time:100}};
ctx.API.get=async path=>{
  if(path==='/api/status'){return statuses;}
  if(path.endsWith('/review')){throw Error('503');}
  if(path.endsWith('/script')){return {running:false,start_time:100,logs:['older script log']};}
  return {running:false,logs:[]};
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,M);
assert.strictEqual(el('script-logs').title,'');
""")

    def test_older_failed_hydration_still_shows_the_newest_readable_run(self):
        self.run_js(r"""
statuses={script:{running:false,start_time:100},review:{running:false,start_time:200}};
ctx.API.get=async path=>{
  if(path==='/api/status'){return statuses;}
  if(path.endsWith('/script')){throw Error('503');}
  if(path.endsWith('/review')){return {running:false,start_time:200,logs:['newer review log']};}
  return {running:false,logs:[]};
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'newer review log');
assert.strictEqual(el('script-logs').title,'Restored review logs');
""")

    def test_failed_hydration_without_a_timestamp_reports_honestly(self):
        self.run_js(r"""
const M='Earlier log history could not be loaded. Check the connection, then reload.';
statuses={script:{running:false},review:{running:false}};
ctx.API.get=async path=>{
  if(path==='/api/status'){return statuses;}
  if(path.endsWith('/review')){throw Error('503');}
  if(path.endsWith('/script')){return {running:false,start_time:100,logs:['older script log']};}
  return {running:false,logs:[]};
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,M);
""")

    def test_successful_empty_hydration_does_not_write_the_diagnostic(self):
        self.run_js(r"""
statuses={};
ctx.API.get=async path=>path==='/api/status'?statuses:{running:false,start_time:100,logs:[]};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'');
assert.strictEqual(el('script-logs').children.length,0);
assert.strictEqual(el('script-logs').title,'');
""")

    def test_live_poll_generation_change_during_hydration_keeps_the_live_pane(self):
        self.run_js(r"""
statuses={};ctx._pollGen={};let release;
ctx.API.get=path=>{
  if(path==='/api/status'){return Promise.resolve(statuses);}
  if(path.endsWith('/script')){return new Promise((resolve,reject)=>{release=()=>reject(Error('503'));});}
  return Promise.resolve({running:false,logs:[]});
};
const restore=ctx.reattachRunningPollers();
for(let i=0;i<8;i++){await Promise.resolve();}
ctx._pollGen['logs:script']=1;
el('script-logs').innerText='live output';
release();
await restore;
assert.strictEqual(el('script-logs').innerText,'live output');
""")

    def test_a_renderer_started_later_replaces_the_diagnostic_on_its_first_tick(self):
        self.run_js(r"""
const M='Earlier log history could not be loaded. Check the connection, then reload.';
statuses={};
ctx.API.get=async path=>{if(path==='/api/status'){return statuses;}throw Error('offline');};
await ctx.reattachRunningPollers();
const pane=el('script-logs');
assert.strictEqual(pane.innerText,M);
assert.strictEqual(pane.children.length,1);
vm.runInContext(coreSource.slice(coreSource.indexOf('function getTaskLogUpdate('),coreSource.indexOf('// --- Setup Tab ---')),ctx);
const render=ctx.createTaskLogRenderer(pane);
render({running:true,start_time:7,logs:['live one','live two']});
assert.strictEqual(pane.innerText,'live one\nlive two');
assert.strictEqual(pane.children.length,0);
""")

    def test_every_locale_catalog_carries_the_diagnostic_key(self):
        import json
        from pathlib import Path
        i18n = Path(__file__).resolve().parent.parent / 'static' / 'i18n'
        for name in ('ui.en.json', 'ui.zh-CN.json', 'ui.ja.json', 'ui.es-419.json'):
            data = json.loads((i18n / name).read_text(encoding='utf-8'))
            self.assertIn('core.task.logs-history-failed', data, name)
            self.assertTrue(str(data['core.task.logs-history-failed']).strip(), name)

    def test_live_pane_text_is_not_replaced_when_only_failures_have_no_readable_log(self):
        self.run_js(r"""
statuses={};
ctx.API.get=async path=>{
  if(path==='/api/status'){return statuses;}
  if(path.endsWith('/review')){throw Error('503');}
  return {running:false,logs:[]};
};
el('script-logs').innerText='live line 1\nlive line 2';
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'live line 1\nlive line 2');
assert.strictEqual(el('script-logs').children.length,0);
""")

    def test_a_later_failed_newest_run_replaces_an_already_restored_older_log(self):
        self.run_js(r"""
const M='Earlier log history could not be loaded. Check the connection, then reload.';
statuses={review:{running:false,start_time:100}};
ctx.API.get=async path=>{
  if(path==='/api/status'){return statuses;}
  if(path.endsWith('/review')){return {running:false,start_time:100,logs:['older review log']};}
  return {running:false,logs:[]};
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'older review log');
statuses={review:{running:false,start_time:200}};
ctx.API.get=async path=>{
  if(path==='/api/status'){return statuses;}
  if(path.endsWith('/review')){throw Error('503');}
  return {running:false,logs:[]};
};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,M);
assert.strictEqual(el('script-logs').title,'');
""")

    def test_successful_empty_hydration_clears_our_earlier_diagnostic(self):
        self.run_js(r"""
const M='Earlier log history could not be loaded. Check the connection, then reload.';
statuses={};
ctx.API.get=async path=>{if(path==='/api/status'){return statuses;}throw Error('offline');};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,M);
ctx.API.get=async path=>path==='/api/status'?statuses:{running:false,start_time:100,logs:[]};
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,'');
assert.strictEqual(el('script-logs').children.length,0);
""")

    def test_failed_newest_hydration_reports_the_line_even_over_live_text(self):
        self.run_js(r"""
const M='Earlier log history could not be loaded. Check the connection, then reload.';
statuses={review:{running:false,start_time:200},script:{running:false,start_time:100}};
ctx.API.get=async path=>{
  if(path==='/api/status'){return statuses;}
  if(path.endsWith('/review')){throw Error('503');}
  if(path.endsWith('/script')){return {running:false,start_time:100,logs:['older script log']};}
  return {running:false,logs:[]};
};
el('script-logs').innerText='live text';
await ctx.reattachRunningPollers();
assert.strictEqual(el('script-logs').innerText,M);
assert.strictEqual(el('script-logs').title,'');
""")
