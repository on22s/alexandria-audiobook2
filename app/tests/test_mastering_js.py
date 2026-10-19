"""Measured results cannot survive an export start or a stale async reply."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC=Path(__file__).resolve().parent.parent/'static'
SETUP=r'''
Element.prototype.replaceChildren=function(...nodes){for(const node of [...this.children]){node.remove();}for(const node of nodes){this.appendChild(node);}};
for(const id of ['export-mastering-enabled','export-mastering-loudness','export-mastering-peak','export-mastering-reports','export-mastering-status','m4b-status','m4b-per-chunk','m4b-title','m4b-author','m4b-narrator','m4b-year','m4b-description','m4b-require-ready','m4b-cancel-btn','chapter-status','chapter-cancel-btn']){body.appendChild(new Element('div',{id}));}
document.getElementById('export-mastering-loudness').value='-18';document.getElementById('export-mastering-peak').value='-2';
context.window=context;context.chunkSnapshotBookToken='b'.repeat(64);context.chunkSnapshotBook='fixture';context.currentBookFilename='fixture';const errors=[],posts=[];
context.showActionError=(...v)=>errors.push(v);context.escapeHtml=v=>v;context.getActionErrorMessage=(...v)=>v.join(' ');context.claimTaskStart=()=>true;context.releaseTaskStart=()=>{};context.pollExport=()=>{};
const report={file:'audiobook.m4b',before:{integrated_lufs:-25,true_peak_dbtp:-10,range_lu:1},after:{integrated_lufs:-18,true_peak_dbtp:-2,range_lu:1},settings:{loudness:-18,true_peak:-2},mode:'linear',sha256:'old-sha'};
context.API={get:async()=>({book_token:'b'.repeat(64),reports:[report],unavailable:0}),post:async(url,data)=>{posts.push({url,data});return{};}};
load();let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish'));(async()=>{
'''

class MasteringJsTests(unittest.TestCase):
    def run_js(self,code):
        result=subprocess.run(['node','-e',DOM_FIXTURE+SETUP+code+'\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});',str(STATIC/'js/app-mastering.js'),str(STATIC/'js/app-core.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_off_bounds_and_export_start_invalidate_old_and_pending_measurements(self):
        self.run_js(r'''
assert.equal(context.getExportMasteringOptions(),null);document.getElementById('export-mastering-enabled').checked=true;document.getElementById('export-mastering-peak').value='Infinity';assert.throws(()=>context.getExportMasteringOptions());document.getElementById('export-mastering-peak').value='-2';
const core=fs.readFileSync(process.argv[2],'utf8');const start=core.indexOf('window.exportM4B =');vm.runInContext(core.slice(start,core.indexOf('\n        };',start)+12),context);
await context.loadExportMasteringReports();assert.equal(document.getElementById('export-mastering-reports').children.length,1);await context.exportM4B();assert.equal(posts.length,1);assert.equal(document.getElementById('export-mastering-reports').children.length,0);
let resolve;context.API.get=()=>new Promise(r=>resolve=r);const pending=context.loadExportMasteringReports();await context.exportM4B();resolve({book_token:'b'.repeat(64),reports:[report],unavailable:0});await pending;assert.equal(document.getElementById('export-mastering-reports').children.length,0);
for(const endpoint of ['/api/merge','/api/export_chapters','/api/merge_m4b']){const at=core.indexOf(`await API.post('${endpoint}',`);assert(at>0);assert(core.slice(at-150,at).includes('resetExportMasteringView'));}
''')

    def test_failed_terminal_export_also_invalidates_report_epoch(self):
        self.run_js(r'''
const core=fs.readFileSync(process.argv[2],'utf8');context.exportStatusGenerations=new Map();let poll;context._startPolling=(name,fetch,options)=>poll=options;vm.runInContext(core.slice(core.indexOf('function isExportComplete('),core.indexOf('window.exportAudacity =')),context);
for(const task of ['m4b_export','chapter_export']){await context.loadExportMasteringReports();assert.equal(document.getElementById('export-mastering-reports').children.length,1);context.pollExport(task);poll.onDone({running:false,result:{status:'failed',message:'synthetic failure'}});assert.equal(document.getElementById('export-mastering-reports').children.length,0);}
''')
