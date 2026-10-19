"""Selection uses real shared render barriers; preview controls never start a job."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from tests.test_editor_render_barrier import HARNESS
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent/'static'
UI_SETUP = r'''
Element.prototype.querySelector=function(s){return this.querySelectorAll(s)[0]||null;};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}this.textContent='';for(const child of children){this.appendChild(child);}};
Element.prototype.add=function(child){this.appendChild(child);};
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(v){if(v){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Object.defineProperty(Element.prototype,'selectedOptions',{get(){return this.children.filter(x=>x.selected);}});
context.Option=function(text,value){const e=new Element('option');e.textContent=text;e.value=value;return e;};
for(const id of ['editor-repair-start','editor-repair-lines','editor-repair-status','editor-repair-chapter','editor-repair-speakers','editor-repair-note']){body.appendChild(new Element('div',{id}));}
document.getElementById('editor-repair-chapter').value='';
context.chunkSnapshotBookToken='b'.repeat(64);context.chunkSnapshotBook='A';context.currentBookFilename='A';context.isRenderingAll=false;
const errors=[],starts=[],posts=[];context.showActionError=(...v)=>errors.push(v);context.ensureEditorRenderSnapshot=async()=>[];context.startRender=async(...v)=>starts.push(v);
const row={id:2,uid:'u2',speaker:'<img onerror=x>',text:'Exact words',status:'done'};
context.API={get:async()=>({book_token:context.chunkSnapshotBookToken,options_revision:'f'.repeat(64),chapters:[{index:0,title:'Repeated title'}],speakers:[row.speaker],grouping_note:'Detected chapters'}),post:async(url,data)=>{posts.push({url,data});return {book_token:context.chunkSnapshotBookToken,repair_revision:'c'.repeat(64),rows:[row],rendered_count:1};}};
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish'));
(async()=>{
'''

class EditorRepairJsTests(unittest.TestCase):
    def run_ui(self, code):
        result = subprocess.run(['node', '-e', DOM_FIXTURE+UI_SETUP+code+'\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});', str(STATIC/'js/app-editor-repair.js')], capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_preview_no_job_safe_text_and_exact_selected_start(self):
        self.run_ui(r'''
await context.loadEditorRepairOptions();assert.equal(starts.length,0);const speakers=document.getElementById('editor-repair-speakers');speakers.children[0].selected=true;
await context.previewEditorRepair();assert.equal(starts.length,0);assert.equal(posts.length,1);assert(document.getElementById('editor-repair-lines').children[0].textContent.includes('<img onerror=x>'));assert.equal(body.querySelectorAll('img').length,0);
const selected=context.getEditorRepairSelection([row,{id:3,uid:'u3',text:'Unselected'}],vm.runInContext('editorRepairPreview',context));assert.equal(selected.length,1);assert.equal(selected[0].uid,'u2');
await context.generateEditorRepair();assert.equal(starts.length,1);assert.equal(starts[0][0],true);assert.equal(starts[0][1].rows[0].uid,'u2');assert(document.getElementById('editor-repair-start').disabled);
''')

    def test_empty_selection_late_book_and_changed_filters_cannot_start(self):
        self.run_ui(r'''
await context.loadEditorRepairOptions();await context.previewEditorRepair();assert.equal(posts.length,0);assert.equal(errors.length,1);
document.getElementById('editor-repair-speakers').children[0].selected=true;let release;context.API.post=()=>new Promise(resolve=>release=resolve);const first=context.previewEditorRepair();await tick();const duplicate=context.previewEditorRepair();context.chunkSnapshotBookToken='d'.repeat(64);release({book_token:'b'.repeat(64),rows:[row]});await Promise.all([first,duplicate]);await context.generateEditorRepair();assert.equal(starts.length,0);
context.chunkSnapshotBookToken='b'.repeat(64);context.API.post=async()=>({book_token:'b'.repeat(64),rows:[row],repair_revision:'c'.repeat(64),rendered_count:1});await context.previewEditorRepair();document.getElementById('editor-repair-chapter').value='0';assert.throws(()=>context.getEditorRepairSelection([row],vm.runInContext('editorRepairPreview',context)),/selection changed/);assert.equal(starts.length,0);
''')

    def run_render(self, code):
        with tempfile.TemporaryDirectory() as root:
            source = (STATIC/'js/app-editor-repair.js').read_text(); helper=source[source.index('function getEditorRepairSelection('):source.index('async function generateEditorRepair(')]
            extra="\nconst helper="+__import__('json').dumps(helper)+";\n"+code
            result=subprocess.run(['node','-e',HARNESS+extra,str(STATIC/'js/app-core.js'),str(Path(root)/'rows.json')],capture_output=True,text=True,timeout=15)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_batch_selection_confirmation_cancellation_and_change(self):
        self.run_render(r'''
(async()=>{for(const button of ['renderAll','renderBatchFast']){for(const mode of ['success','cancel','edit']){
const a=client();a.setHold(false);const rows=await a.context.ensureEditorRenderSnapshot();const filters={chapter:null,speakers:['NEW SPEAKER']};a.context.getEditorRepairFilters=()=>filters;vm.runInContext(helper,a.context);const preview={book_token:'book-token',repair_revision:'e'.repeat(64),filters,rows};
a.context.showConfirm=async()=>{if(mode==='edit'){a.controls.text='Changed during confirmation';}return mode!=='cancel';};
await a.context[button](true,preview);assert.equal(a.batches.length,mode==='success'?1:0);if(mode==='success'){assert.deepEqual(a.batches[0].data.indices,[7]);assert.equal(a.batches[0].data.expected_repair_revision,'e'.repeat(64));}if(mode==='edit'){assert(a.errors.some(e=>e.includes('selected line changed')));}
}}finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_terminal_subset_polling_counts_and_cancellation_reset(self):
        self.run_render(r'''
(async()=>{for(const button of ['renderAll','renderBatchFast']){for(const terminal of ['done','failed','cancelled','missing']){
const a=client();a.setHold(false);const rows=await a.context.ensureEditorRenderSnapshot();const filters={chapter:null,speakers:['NEW SPEAKER']};a.context.getEditorRepairFilters=()=>filters;vm.runInContext(helper,a.context);
vm.runInContext(source.slice(source.indexOf('function getBatchOutcome('),source.indexOf('function pollReviewBatch(')),a.context);
vm.runInContext(source.slice(source.indexOf('window.cancelRender ='),source.indexOf('window.startRender =')),a.context);
let poll,fetchState;const drifts=[];a.context._startPolling=(name,fetch,options)=>{assert.equal(name,'render_batch');fetchState=fetch;poll=options;};a.context.runDriftCheck=ids=>drifts.push(Array.from(ids));
const originalGet=a.context.API.get;a.context.API.get=async url=>url==='/api/status/audio'?{running:false}:originalGet(url);
a.context.ensureChunkRefresh=async()=>[...(terminal==='missing'?[]:[{...rows[0],status:terminal}]),{id:8,uid:'unselected',status:'done'},{id:7,uid:'replacement-uid',status:'done'}];
const preview={book_token:'book-token',repair_revision:'e'.repeat(64),filters,rows};await a.context[button](true,preview);assert(poll);assert.equal(vm.runInContext('isRenderingAll',a.context),true);
const updated=await fetchState();assert(poll.doneCheck(updated));await poll.onDone(updated);assert.equal(vm.runInContext('isRenderingAll',a.context),false);
const expected=terminal==='done'?'1 succeeded, 0 failed, 0 cancelled, 0 unfinished':terminal==='failed'?'0 succeeded, 1 failed, 0 cancelled, 0 unfinished':terminal==='cancelled'?'0 succeeded, 0 failed, 1 cancelled, 0 unfinished':'0 succeeded, 0 failed, 0 cancelled, 1 unfinished';
assert(a.errors.some(message=>message.includes(expected)),a.errors);assert.deepEqual(drifts,terminal==='done'?[[7]]:[]);
}}finished=true;})().catch(error=>{console.error(error);process.exitCode=1;});
''')
