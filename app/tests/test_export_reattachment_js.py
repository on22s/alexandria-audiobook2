"""Actual shared export pollers retain completion/download/error contracts on reload."""
import unittest
from tests import test_training_ui_contract as ui


class ExportReattachmentTests(unittest.TestCase):
    def run_js(self, code):
        controls = r"""
const controlsSource=fs.readFileSync(process.argv[2],'utf8');
const controlsStart=controlsSource.indexOf('        const taskStartButtons =');
const controlsEnd=controlsSource.indexOf('        // --- API Helpers ---',controlsStart);
assert(controlsStart>=0&&controlsEnd>controlsStart);
vm.runInContext(controlsSource.slice(controlsStart,controlsEnd),ctx);
"""
        return ui.TrainingUiContractTests.run_js(self, controls + code)

    def test_shared_poller_all_exports_success_and_failure(self):
        self.run_js(r"""
const coreSource=fs.readFileSync(process.argv[2],'utf8');
vm.runInContext(coreSource.slice(coreSource.indexOf('        function isExportComplete('),coreSource.indexOf('        window.exportAudacity =')),ctx);
const polls={},downloads=[];let refreshed=0;
ctx._startPolling=(key,fetch,options)=>polls[key]={fetch,options};ctx.loadChapterExports=()=>refreshed++;
ctx.document.createElement=()=>({click(){downloads.push([this.href,this.download]);}});ctx.document.body={appendChild:()=>{},removeChild:()=>{}};ctx.setTimeout=()=>{};
for(const task of ['audacity_export','m4b_export','chapter_export']){
 ctx.pollExport(task);assert(!polls[task].options.doneCheck({running:true}));assert(polls[task].options.doneCheck({running:false}));
 polls[task].options.onTick({logs:['Writing chapter'],running:true});polls[task].options.onDone({logs:['No completion keyword in logs'],result:{status:'done',message:'3 chapters'},running:false});
}
assert.deepStrictEqual(downloads,[['/api/export_audacity?t=1700000000000','audacity_export.zip'],['/api/audiobook_m4b?t=1700000000000','audiobook.m4b']]);assert.strictEqual(refreshed,1);assert.strictEqual(element('chapter-cancel-btn').style.display,'none');
for(const [task,id] of [['audacity_export','audacity-status'],['m4b_export','m4b-status'],['chapter_export','chapter-status']]){
 ctx.pollExport(task);polls[task].options.onDone({logs:['Export complete: misleading earlier progress'],result:{status:'failed',message:'Export failed: <script>bad</script>'},running:false});assert(!element(id).innerHTML.includes('<script>'));assert(element(id).innerHTML.includes('Export failed'));}
for(const [task,id] of [['audacity_export','audacity-status'],['m4b_export','m4b-status'],['chapter_export','chapter-status']]){
 for(const text of ['Export incomplete: interrupted','incomplete export','Could not complete export']){
  ctx.pollExport(task);polls[task].options.onDone({logs:[text],running:false});assert(element(id).innerHTML.includes('text-danger'));assert.strictEqual(downloads.length,2);assert.strictEqual(refreshed,1);
 }
}
ctx.pollExport('chapter_export');polls.chapter_export.options.onDone({logs:['Export complete: older','Export failed: final error'],result:{status:'failed',message:'final error'},running:false});assert(element('chapter-status').innerHTML.includes('final error'));
for(const [task,id] of [['audacity_export','audacity-status'],['m4b_export','m4b-status'],['chapter_export','chapter-status']]){
 for(const result of [null,{}, {status:'done',message:7},{status:'unknown',message:'ambiguous'},{status:'cancelled',message:'Export cancelled'}]){
  ctx.pollExport(task);polls[task].options.onDone({logs:['Export complete: old marker'],result,running:false});assert(element(id).innerHTML.includes('text-danger'));assert.strictEqual(downloads.length,2);assert.strictEqual(refreshed,1);
 }
}
assert.strictEqual(downloads.length,2);assert.strictEqual(refreshed,1);assert.throws(()=>ctx.pollExport('unknown'));
""")

    def test_normal_starts_use_same_poller_after_successful_post_and_never_after_failure(self):
        self.run_js(r"""
const coreSource=fs.readFileSync(process.argv[2],'utf8');
vm.runInContext(coreSource.slice(coreSource.indexOf('        window.exportAudacity ='),coreSource.indexOf('        // --- Chapter-by-chapter export ---')),ctx);
vm.runInContext(coreSource.slice(coreSource.indexOf('        window.exportM4B ='),coreSource.indexOf('        // --- Polling Logic ---')),ctx);
const calls=[];ctx.pollExport=task=>{calls.push(task);ctx.releaseTaskStart(task);};ctx.API=vm.runInContext('API',ctx);ctx.API.post=async()=>({});
await ctx.exportAudacity();await ctx.exportM4B();assert.deepStrictEqual(calls,['audacity_export','m4b_export']);
ctx.API.post=async()=>{throw Error('admission refused');};await ctx.exportAudacity();await ctx.exportM4B();assert.strictEqual(calls.length,2);
assert(element('audacity-status').innerHTML.includes('admission refused'));assert(element('m4b-status').innerHTML.includes('admission refused'));
const chapter=coreSource.slice(coreSource.indexOf("document.getElementById('chapter-export-btn').addEventListener"),coreSource.indexOf("document.getElementById('chapter-cancel-btn').addEventListener"));
let handler;element('chapter-export-btn').addEventListener=(event,fn)=>handler=fn;ctx.chapterExportParams=()=>({format:'mp3'});vm.runInContext(chapter,ctx);
await handler();assert.strictEqual(calls.length,2);assert.strictEqual(element('chapter-cancel-btn').style.display,'none');
ctx.API.post=async()=>({});await handler();assert.strictEqual(calls.at(-1),'chapter_export');
""")

    def test_m4b_cancel_and_live_progress_keep_successful_download(self):
        self.run_js(r"""
const source=fs.readFileSync(process.argv[2],'utf8');
vm.runInContext(source.slice(source.indexOf('        function isExportComplete('),source.indexOf('        window.exportAudacity =')),ctx);
const calls=[],downloads=[];let poll;
ctx._startPolling=(key,fetch,options)=>{assert.strictEqual(key,'m4b_export');poll=options;};
ctx.cancelTask=async url=>{calls.push(url);return true;};
ctx.document.createElement=()=>({click(){downloads.push(this.download);}});
ctx.document.body={appendChild:()=>{},removeChild:()=>{}};ctx.setTimeout=()=>{};
vm.runInContext(source.slice(source.indexOf('        window.cancelM4B ='),source.indexOf('        window.exportM4B =')),ctx);
ctx.pollExport('m4b_export');assert.strictEqual(element('m4b-cancel-btn').style.display,'');
poll.onTick({logs:['Encoding M4B: 1.5/3.0 s; elapsed 0.2 s'],running:true});
assert.strictEqual(element('m4b-status').textContent,'Encoding M4B: 1.5/3.0 s; elapsed 0.2 s');
await ctx.cancelM4B();assert.deepStrictEqual(calls,['/api/merge_m4b/cancel']);
assert.strictEqual(element('m4b-cancel-btn').style.display,'');
poll.onDone({logs:[],result:{status:'cancelled',message:'Export cancelled'},running:false});
assert.strictEqual(element('m4b-cancel-btn').style.display,'none');assert.strictEqual(downloads.length,0);
ctx.pollExport('m4b_export');poll.onDone({logs:[],result:{status:'done',message:'audiobook.m4b'},running:false});
assert.deepStrictEqual(downloads,['audiobook.m4b']);assert.strictEqual(element('m4b-cancel-btn').style.display,'none');
""")
