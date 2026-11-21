"""Deferred-response behavior of the production Editor feedback handlers."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest

from tests.test_editor_render_barrier import HARNESS
from tests.test_editor_repair_js import UI_SETUP
from tests.test_take_history_js import SETUP as TAKE_SETUP
from tests.test_editor_review_js import SETUP as REVIEW_SETUP
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'
PY = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');'''


class EditorFeedbackFollowupTests(unittest.TestCase):
    def run_barrier(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            script = HARNESS.replace('console:{error(){},log(){}}', 'console:console') + body
            result = subprocess.run(['node', '-e', script, str(STATIC / 'js/app-core.js'), str(Path(tmp) / 'chunks.json')], capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def run_review(self, body):
        script = (DOM_FIXTURE + REVIEW_SETUP + 'context.URLSearchParams=URLSearchParams;\n'
                  + body + '\ncompleted=true;finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});')
        result = subprocess.run(['node', '-e', script, str(STATIC / 'js/app-editor-review.js'), str(STATIC / 'index.html')], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def run_repair_render(self, body):
        core = ("const core=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');"
                "vm.runInContext(core.slice(core.indexOf('window.cancelRender ='),core.indexOf(\"document.getElementById('btn-merge').addEventListener\")),context);"
                "context.console=console;"
                "for(const p of ['cancelRender','renderAll','renderBatchFast','startRender']){context[p]=context.window[p];}")
        script = (DOM_FIXTURE + UI_SETUP + '\n' + core + '\n' + body
                  + '\nfinished=true;finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});')
        result = subprocess.run(['node', '-e', script, str(STATIC / 'js/app-editor-repair.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def run_take(self, body):
        script = DOM_FIXTURE + TAKE_SETUP + '\n(async()=>{\n' + body + '\ndone=true;finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(STATIC / 'js/app-take-history.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_late_refusal_from_replaced_row_cannot_poison_new_book(self):
        self.run_barrier(r'''
(async()=>{
 const a=client(),marks=[];a.row.classList={add:v=>marks.push(v),remove:v=>marks.push('clear:'+v)};
 const pending=a.context.applyChunkEdits(7,{text:'refused in old book'});await tick();assert.strictEqual(a.gates.length,1);
 a.row.dataset.uid='replacement';a.row.dataset.bookToken='new-book';
 vm.runInContext('chunkSnapshotBookToken="new-book";cachedChunks=[{id:7,uid:"replacement",text:"new book",status:"done",error:null}];',a.context);
 a.setFail(true);a.gates.shift()();await pending.catch(()=>{});
 assert.strictEqual(vm.runInContext('failedChunkEdits.size',a.context),0);
 assert.strictEqual(vm.runInContext('getEditorFailedEditsSummary()',a.context),'');
 assert.strictEqual(vm.runInContext('isEditorEditPending()',a.context),false);
 assert(!marks.includes('text-danger'),marks);
 await a.context.flushChunkEdits();
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_success_clears_only_matching_field_failure_across_queued_saves(self):
        self.run_barrier(r'''
(async()=>{
 const a=client();a.setHold(false);a.setFail(true);
 await a.context.updateChunk(7,'text','refused text');
 let fields=vm.runInContext('getFailedChunkEdits().map(f=>f.field)',a.context);
 assert.equal(JSON.stringify(fields),'["text"]');
 a.setFail(false);
 await a.context.updateChunk(7,'instruct','gentle delivery');
 fields=vm.runInContext('getFailedChunkEdits().map(f=>f.field)',a.context);
 assert.equal(JSON.stringify(fields),'["text"]','a successful field must not clear another field failure');
 await a.context.updateChunk(7,'text','corrected text');
 assert.strictEqual(vm.runInContext('getEditorFailedEditsSummary()',a.context),'');
 assert.strictEqual(vm.runInContext('failedChunkEdits.size',a.context),0);
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_authoritative_status_error_and_newer_intent_survive_late_reply(self):
        self.run_barrier(r'''
(async()=>{
 const a=client();a.setHold(false);
 vm.runInContext('cachedChunks=[{id:7,uid:"row-7",text:"",instruct:"",speaker:"OLD",status:"error",error:"old refusal"}];',a.context);
 a.context.API.post=async(url,data)=>url.startsWith('/api/chunks/7?')?{status:'pending',error:null,...data}:{};
 await a.context.applyChunkEdits(7,{text:'semantic fix'});
 let row=vm.runInContext('cachedChunks[0]',a.context);
 assert.strictEqual(row.status,'pending');assert.strictEqual(row.error,null);
 vm.runInContext('cachedChunks[0].status="error";cachedChunks[0].error="old refusal";',a.context);
 a.context.API.post=async(url,data)=>url.startsWith('/api/chunks/7?')?{status:'error',error:'old refusal',...data}:{};
 await a.context.applyChunkEdits(7,{pause_after:300});
 row=vm.runInContext('cachedChunks[0]',a.context);assert.strictEqual(row.error,'old refusal');
 const b=client();
 b.context.API.post=async(url,data)=>{if(!url.startsWith('/api/chunks/7?')){return {};}await new Promise(resolve=>b.gates.push(resolve));return {status:'pending',error:null,text:'SERVER-NORMALIZED'};};
 const first=b.context.applyChunkEdits(7,{text:'first intent'}),second=b.context.applyChunkEdits(7,{text:'newer intent'});
 await tick();b.gates.shift()();await tick();
 assert.strictEqual(vm.runInContext('cachedChunks[0].text',b.context),'newer intent');
 b.gates.shift()();await Promise.all([first,second]);
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_inspector_error_clears_on_semantic_edit_and_survives_pause_only(self):
        self.run_review(r'''
const core=fs.readFileSync(process.argv[1].replace('app-editor-review.js','app-core.js'),'utf8');
vm.runInContext(core.slice(core.indexOf('const pendingChunkEdits ='),core.indexOf('function getEditorFieldValue(')),context);
context.invalidateEditorIntegrity=()=>{};context.refreshEditorIntegrity=async()=>{};
context.API.post=async(url,data)=>({status:'pending',error:null,...data});
await context.refreshEditorReview();document.getElementById('editor-quality-list').children[0].children[0].fire('click');
const lineError=document.getElementById('editor-line-error');assert(lineError.textContent.includes('synthetic refusal'));
await context.applyChunkEdits(0,{text:'repaired text'});context.renderEditorLineInspector();
assert(lineError.textContent.includes('No render error'),lineError.textContent);
context.cachedChunks[0].status='error';context.cachedChunks[0].error='still refused';
context.API.post=async(url,data)=>({status:'error',error:'still refused',...data});
await context.applyChunkEdits(0,{pause_after:300});context.renderEditorLineInspector();
assert(lineError.textContent.includes('still refused'),lineError.textContent);
''')

    def test_stale_line_during_preflight_posts_nothing_and_writes_no_status(self):
        self.run_review(r'''
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-speech-verification.js'),'utf8'),context);
context.getChunkAudioIdentity=c=>c.audio_revision;context.isEditorEditPending=()=>false;
const chunk=context.cachedChunks[1];chunk.audio_revision='selected';chunk.audio_path='one.wav';
chunk.speech_verification_historical={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'selected',status:'no_content_candidate',expected_text:'Historical input',input_digest:'d'.repeat(64),audio:{sha256:'e'.repeat(64)}};
chunk.speech_verification_historical_reason='legacy';delete chunk.speech_verification;
context.API.get=async()=>{const data=facts();data.rows[1].speech='not_checked';return data;};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
assert.equal(document.getElementById('editor-line-speech-recheck').disabled,false);
let release;context.ensureEditorRenderSnapshot=()=>new Promise(resolve=>release=resolve);
const status=document.getElementById('editor-line-speech-status'),before=status.textContent;
let posts=0;context.API.post=async()=>{posts++;return {};};
const pending=context.recheckEditorSpeech();await tick();assert.equal(posts,0);
context.selectEditorReviewLineById(0);release();await pending;
assert.equal(posts,0,'a stale line must not be posted');assert.equal(status.textContent,before);
assert.equal(run('speechVerificationBusy'),false);
''')

    def _repair_ready(self, mode):
        return ("Object.defineProperty(Element.prototype,'classList',{get(){const node=this;return {contains:v=>node.className.split(/\\s+/).includes(v),add(v){if(!node.className.split(/\\s+/).includes(v)){node.className=(node.className+' '+v).trim();}},remove(v){node.className=node.className.split(/\\s+/).filter(x=>x!==v).join(' ');}};}});\n"
                "const tts=document.createElement('input');tts.setAttribute('id','tts-mode');tts.value=\"" + mode + "\";body.appendChild(tts);\n" + r'''
for(const id of ['btn-batch-fast','btn-regen-all','btn-cancel-render']){const e=document.createElement('button');e.setAttribute('id',id);body.appendChild(e);}
const tr=document.createElement('tr');tr.setAttribute('data-id','2');const badge=document.createElement('span');badge.className='badge';tr.appendChild(badge);body.appendChild(tr);
context.console=console;context.showConfirm=async()=>true;context._startPolling=()=>{};
context.refreshDeliveryReview=async()=>null;context.loadChunks=async()=>{};context.runDriftCheck=()=>{};
context.ensureEditorRenderSnapshot=async()=>[row];
const renderPosts=[];context.API.post=async(url,data)=>{
 if(url.includes('/generate_batch')){renderPosts.push({url,data});if(failRender){throw Error('GPU busy');}return {total_chunks:1,workers:1,batch_size:1,batch_seed:0};}
 return {book_token:context.chunkSnapshotBookToken,repair_revision:'c'.repeat(64),rows:[row],rendered_count:1};};
await context.loadEditorRepairOptions();document.getElementById('editor-repair-speakers').children[0].selected=true;
await context.previewEditorRepair();
''')

    def run_repair_scenario(self, mode, body):
        script = self._repair_ready(mode) + body
        self.run_repair_render(script)

    def test_real_local_and_external_dispatch_report_actual_start(self):
        for mode, endpoint in (('local', '/api/generate_batch_fast'), ('external', '/api/generate_batch')):
            with self.subTest(mode=mode):
                self.run_repair_scenario(mode, "let failRender=false;\n"
                    "await context.generateEditorRepair();\n"
                    "assert.equal(renderPosts.length,1);assert.equal(renderPosts[0].url,'" + endpoint + "');\n"
                    "assert(!document.getElementById('editor-repair-status').textContent.includes('did not start'));\n")

    def test_real_gpu_busy_and_confirmation_dismissal_report_refusal(self):
        self.run_repair_scenario('local', r'''
let failRender=true;await context.generateEditorRepair();
assert(document.getElementById('editor-repair-status').textContent.includes('did not start'));
''')
        self.run_repair_scenario('local', r'''
let failRender=false;context.showConfirm=async()=>false;
await context.generateEditorRepair();assert.equal(renderPosts.length,0);
assert(document.getElementById('editor-repair-status').textContent.includes('did not start'));
''')

    def test_old_book_render_settling_cannot_overwrite_new_book_status(self):
        self.run_repair_scenario('local', r'''
let failRender=true;
context.API.post=async(url,data)=>{
 if(url.includes('/generate_batch')){
  context.chunkSnapshotBookToken='c'.repeat(64);
  vm.runInContext('resetEditorRepairPreview()',context);
  document.getElementById('editor-repair-status').textContent='New book: choose a selection.';
  throw Error('GPU busy');}
 return {book_token:context.chunkSnapshotBookToken,repair_revision:'c'.repeat(64),rows:[row],rendered_count:1};};
await context.generateEditorRepair();
const status=document.getElementById('editor-repair-status');
assert.equal(status.textContent,'New book: choose a selection.');
assert(!status.textContent.includes('did not start'));
assert.equal(vm.runInContext('editorRepairPreview',context),null);
''')

    def test_take_failure_clears_on_selection_retry_loads_and_locale_repaint_keeps_it(self):
        self.run_take(r'''
const failed=context.loadEditorTakeSnapshots();await tick();gets[0].reject(Error('Archive unavailable'));await failed;
const status=document.getElementById('editor-take-status');
assert(status.textContent.includes('could not be loaded'));
assert.equal(status.getAttribute('data-i18n'),'take.status.load-failed');
vm.runInContext("uiCatalogs.set('es-419',{meta:{review_status:{all:'human-reviewed'}},messages:{'take.status.load-failed':'No se pudieron cargar las instantaneas.'}});",context);
vm.runInContext("applyUiLocale('es-419')",context);
assert(status.textContent.includes('No se pudieron cargar'),status.textContent);
selected={chunk:{...selected.chunk,uid:'other'}};context.applyTakeHistoryInspector();
assert(status.textContent.includes('No snapshots loaded'),status.textContent);
const retry=context.loadEditorTakeSnapshots();await tick();
assert(status.textContent.includes('Loading saved snapshots'),status.textContent);
gets[1].reject(Error('second refusal'));await retry;
const stale=context.loadEditorTakeSnapshots();await tick();const newer=context.loadEditorTakeSnapshots();await tick();
gets[2].reject(Error('old refusal'));await stale;
assert(status.textContent.includes('Loading saved snapshots'),status.textContent);
gets[3].resolve(result());await newer;
assert(status.textContent.includes('saved snapshots in this view'),status.textContent);
''')

    def test_repair_locale_repaint_keeps_keyed_live_announcement(self):
        self.run_repair_scenario('local', r'''
let failRender=true;
const status=document.getElementById('editor-repair-status');
assert.equal(status.getAttribute('data-i18n'),'editor.repair.preview-summary');
vm.runInContext("uiCatalogs.set('es-419',{meta:{review_status:{all:'human-reviewed'}},messages:{'editor.repair.preview-summary':'{lines} lineas seleccionadas; {rendered} con audio.'}});",context);
vm.runInContext("applyUiLocale('es-419')",context);
assert(status.textContent.includes('lineas seleccionadas'),status.textContent);
await context.generateEditorRepair();
assert(status.textContent.includes('did not start'));
assert.equal(status.getAttribute('data-i18n'),'editor.repair.render-not-started');
''')


if __name__ == '__main__':
    unittest.main()
