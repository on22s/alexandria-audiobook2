"""Actual report handlers: explicit dispatch, ownership-bound polling and stale views."""
from pathlib import Path
import subprocess
import unittest

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-reports.js'
SETUP=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),elements={},posts=[],gets=[],toasts=[];let poll;
const el=id=>elements[id]||(elements[id]={style:{},disabled:false,innerHTML:'',textContent:''});
const bodies={'first.md':'First report','second.md':'Second report'};
const ctx={document:{getElementById:el,querySelectorAll:()=>[]},escapeHtml:x=>String(x),marked:{parse:x=>x},DOMPurify:{sanitize:x=>'sanitized:'+x},confirmIfRemote:async()=>true,showToast:(...x)=>toasts.push(x),fetch:async url=>({ok:true,text:async()=>bodies[decodeURIComponent(url.split('/').at(-1))]}),API:{get:async url=>{gets.push(url);return [{filename:'first.md',can_explain:true},{filename:'second.md',can_explain:false}];},post:async(url,body)=>{posts.push({url,body});return {status:'started',run_id:'run1'};}},_startPolling:(name,fetch,options)=>{poll={name,fetch,options};}};
vm.createContext(ctx);{const guidanceCore=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInContext(guidanceCore.slice(guidanceCore.indexOf('function showActionError('),guidanceCore.indexOf('function showConfirm(')),ctx);}vm.runInContext(source.slice(0,source.indexOf('// Last script:')),ctx);
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
'''

class ReportExplanationJsTests(unittest.TestCase):
    def test_report_list_rejects_stale_results_and_errors_and_updates_eligibility(self):
        self.run_js(r'''
const pending=[];ctx.API.get=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
const first=ctx.loadReports(),second=ctx.loadReports();pending[1].resolve([{filename:'second.md',can_explain:true}]);await second;const newer=el('reports-list').innerHTML;pending[0].resolve([{filename:'first.md',can_explain:true}]);await first;assert.strictEqual(el('reports-list').innerHTML,newer);
await ctx.viewReport('first.md');assert.strictEqual(el('btn-report-explain').style.display,'none');await ctx.viewReport('second.md');assert.strictEqual(el('btn-report-explain').style.display,'');
const stale=ctx.loadReports(),current=ctx.loadReports();pending[3].resolve([]);await current;const empty=el('reports-list').innerHTML;pending[2].reject(Error('stale failure'));await stale;assert.strictEqual(el('reports-list').innerHTML,empty);assert(empty.includes('No reports yet'));await ctx.viewReport('second.md');assert.strictEqual(el('btn-report-explain').style.display,'none');
const failed=ctx.loadReports();pending[4].reject(Error('current failure'));await failed;assert(el('reports-list').innerHTML.includes('current failure'));
''')

    def run_js(self,code):
        result=subprocess.run(['node','-e',SETUP+'\n(async()=>{'+code+'\n})().catch(e=>{console.error(e);process.exitCode=1;});',str(SOURCE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_action_only_dispatches_after_explicit_choice_and_binds_cancel_and_poll(self):
        self.run_js(r'''
await ctx.loadReports();await ctx.viewReport('first.md');assert.strictEqual(posts.length,0);
ctx.confirmIfRemote=async()=>false;await ctx.onExplainReport();assert.strictEqual(posts.length,0);assert.strictEqual(el('btn-report-explain').disabled,false);
ctx.confirmIfRemote=async()=>true;await ctx.onExplainReport();await ctx.onExplainReport();assert.strictEqual(posts.length,1);assert.strictEqual(posts[0].url,'/api/reports/first.md/explain');assert.strictEqual(poll.name,'report_explanation');
assert.strictEqual(poll.options.doneCheck({running:true,run_id:'run1'}),false);
await poll.fetch();assert.strictEqual(gets.at(-1),'/api/status/report_explanation');
await ctx.onCancelReportExplanation();assert.strictEqual(posts[1].url,'/api/reports/explanation/cancel');assert.strictEqual(posts[1].body.run_id,'run1');assert.strictEqual(el('btn-report-explain').disabled,true);
bodies['first.md']='Explained first report';await poll.options.onDone({running:false,run_id:'run1',status:'done'});assert.strictEqual(el('report-view-content').innerHTML,'sanitized:Explained first report');assert.strictEqual(el('btn-report-explain').disabled,false);
''')

    def test_other_report_or_run_never_receives_false_success_or_stale_content(self):
        self.run_js(r'''
await ctx.loadReports();await ctx.viewReport('first.md');await ctx.onExplainReport();await ctx.viewReport('second.md');await poll.options.onDone({running:false,run_id:'run1',status:'done'});
assert.strictEqual(el('report-view-title').textContent,'second.md');assert.strictEqual(el('report-view-content').innerHTML,'sanitized:Second report');assert.strictEqual(el('btn-report-explain').style.display,'none');
await ctx.viewReport('first.md');await ctx.onExplainReport();assert.strictEqual(poll.options.doneCheck({running:true,run_id:'other'}),true);await poll.options.onDone({running:false,run_id:'other',status:'done'});assert.match(toasts.at(-1)[0],/status changed/);assert.strictEqual(toasts.at(-1)[1],'warning');
const late=deferred();ctx.fetch=async url=>({ok:true,text:()=>url.endsWith('first.md')?late.promise:Promise.resolve('Newest second report')});const pending=ctx.viewReport('first.md');await ctx.viewReport('second.md');late.resolve('Stale first report');await pending;assert.strictEqual(el('report-view-content').innerHTML,'sanitized:Newest second report');
''')

    def test_confirmation_switch_and_malformed_start_do_not_start_unbound_poll(self):
        self.run_js(r'''
await ctx.loadReports();await ctx.viewReport('first.md');const choice=deferred();ctx.confirmIfRemote=()=>choice.promise;const pending=ctx.onExplainReport();await ctx.viewReport('second.md');choice.resolve(true);await pending;assert.strictEqual(posts.length,0);assert.strictEqual(poll,undefined);
await ctx.viewReport('first.md');ctx.confirmIfRemote=async()=>true;ctx.API.post=async()=>({status:'started'});await ctx.onExplainReport();assert.strictEqual(poll,undefined);assert.match(toasts.at(-1)[0],/run identifier/);assert.strictEqual(el('btn-report-explain').disabled,false);
''')
