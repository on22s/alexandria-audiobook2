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
  let replies=0;
  b.context.API.post=async(url,data)=>{if(!url.startsWith('/api/chunks/7?')){return {};}await new Promise(resolve=>b.gates.push(resolve));replies++;return {status:'pending',error:null,text:replies===1?'SERVER-NORMALIZED':data.text};};
  const first=b.context.applyChunkEdits(7,{text:'first intent'}),second=b.context.applyChunkEdits(7,{text:'newer intent'});
  await tick();b.gates.shift()();await tick();
  assert.strictEqual(vm.runInContext('cachedChunks[0].text',b.context),'SERVER-NORMALIZED','cache holds only the confirmed first reply');
  assert.strictEqual(vm.runInContext('queuedChunkEdits.get(JSON.stringify(["book-token","row-7"])).size',b.context),1,'newer draft stays queued, not cached as saved');
  assert.strictEqual(b.row.querySelector('.chunk-text').value,'focused new line','visible draft is untouched by the first reply');
  b.gates.shift()();await Promise.all([first,second]);
  assert.strictEqual(vm.runInContext('cachedChunks[0].text',b.context),'newer intent','the second reply confirms its own draft');
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


    def test_reused_numeric_id_never_inherits_old_queue_or_failure(self):
        self.run_barrier(r'''
(async()=>{
 const marks=[];const a=client();a.row.classList={add:v=>marks.push(v),remove:v=>marks.push('clear:'+v)};
 const old1=a.context.applyChunkEdits(7,{text:'old text'});
 const old2=a.context.applyChunkEdits(7,{instruct:'old queued draft'});
 await tick();assert.strictEqual(a.gates.length,1,'same old row must still serialize');
 a.row.dataset.uid='replacement';a.row.dataset.bookToken='new-book';
 vm.runInContext('chunkSnapshotBookToken="new-book";cachedChunks=[{id:7,uid:"replacement",text:"new book",instruct:"",status:"done",error:null}];',a.context);
 const fresh=a.context.applyChunkEdits(7,{text:'new save'});
 await tick();assert.strictEqual(a.gates.length,2,'new book must not queue behind the old book POST');
 const query=new URL(a.calls[1].url,'http://fixture').searchParams;
 assert.strictEqual(query.get('expected_uid'),'replacement');assert.strictEqual(query.get('expected_book_token'),'new-book');
 a.gates[1]();await tick();await fresh;
 assert.strictEqual(vm.runInContext('cachedChunks[0].text',a.context),'new save');
 a.setHold(false);a.setFail(true);a.gates[0]();await old1.catch(()=>{});await old2.catch(()=>{});
 assert.strictEqual(vm.runInContext('getEditorFailedEditsSummary()',a.context),'');
 assert.strictEqual(vm.runInContext('isEditorEditPending()',a.context),false);
 assert(!marks.includes('text-danger'),marks);
 assert.strictEqual(vm.runInContext('cachedChunks[0].text',a.context),'new save');
 finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_barrier_waits_only_for_current_book_and_named_row(self):
        self.run_barrier(r'''
(async()=>{
 const a=client();const stale=a.context.applyChunkEdits(7,{text:'stale book'});await tick();
 a.row.dataset.uid='replacement';a.row.dataset.bookToken='new-book';
 vm.runInContext('chunkSnapshotBookToken="new-book";cachedChunks=[{id:7,uid:"replacement",text:"new book"}];',a.context);
 let settled=false;const barrier=a.context.flushChunkEdits().then(()=>{settled=true;});
 await tick();assert(settled,'an unrelated old-book POST must not own the new book barrier');
 assert.strictEqual(vm.runInContext('isEditorEditPending()',a.context),false);
 let waited=false; const scoped=a.context.flushChunkEditsFor('book-token',new Set(['row-7'])).then(()=>{waited=true;});
 await tick();assert(!waited,'a barrier for the old row must still see its pending POST');
 a.setHold(false);a.gates.shift()();await Promise.all([stale,scoped]);assert(waited);
 finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_confirmed_cache_visible_draft_and_retry_survive_render_barrier(self):
        self.run_barrier(r'''
(async()=>{
 const a=client();a.setHold(false);
 const saved={id:7,uid:'row-7',text:'A saved',instruct:'',speaker:'OLD',status:'done',error:null,audio_path:'a.wav',audio_revision:'r1'};
 vm.runInContext('cachedChunks='+JSON.stringify([saved]),a.context);fs.writeFileSync(artifact,JSON.stringify([saved]));
 Object.assign(a.controls,{text:'A saved',instruct:'',speaker:'OLD',pause_after:''});
 const first=a.context.applyChunkEdits(7,{text:'A saved'});
 a.context.API.post=(original=>async(url,data)=>{
  if(!url.startsWith('/api/chunks/7?')){return original(url,data);}
  if(data.text==='B draft'){throw Error('stale revision');}
  return original(url,data);})(a.context.API.post);
 await first;
 a.controls.text='B draft';
 const draft=a.context.applyChunkEdits(7,{text:'B draft'});await draft.catch(()=>{});
 assert.strictEqual(vm.runInContext('cachedChunks[0].text',a.context),'A saved','refused draft must not be cached as saved');
 assert.strictEqual(a.row.querySelector('.chunk-text').value,'B draft','visible draft must be left alone');
 assert.strictEqual(vm.runInContext('getEditorFailedEditsSummary().includes("Line 8")',a.context),true);
 a.context.API.post=async(url,data)=>{a.calls.push({url,data:JSON.parse(JSON.stringify(data))});return url.startsWith('/api/chunks/7?')?{status:'done',error:null,audio_path:'a.wav',audio_revision:'r1',...data}:{};};
  const before=a.calls.length;const barrier=a.context.ensureEditorRenderSnapshot();await tick();
  await barrier;
  assert(a.calls.slice(before).some(call=>call.data.text==='B draft'),'the redraw barrier must re-save the visible draft');
 assert.strictEqual(vm.runInContext('cachedChunks[0].text',a.context),'B draft','successful retry confirms the draft');
 assert.strictEqual(vm.runInContext('getEditorFailedEditsSummary()',a.context),'');
 assert.strictEqual(vm.runInContext('cachedChunks[0].audio_path',a.context),'a.wav','unrelated audio field must survive field saves');
 finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_semantic_save_refreshes_badge_and_stops_stale_audio_but_pause_keeps_it(self):
        self.run_review(r'''
const core=fs.readFileSync(process.argv[1].replace('app-editor-review.js','app-core.js'),'utf8');
Object.defineProperty(Element.prototype,'classList',{get(){const node=this;return {
 contains:v=>node.className.split(/\s+/).includes(v),
 add(v){if(!this.contains(v)){node.className=(node.className+' '+v).trim();}},
 remove(v){node.className=node.className.split(/\s+/).filter(x=>x!==v).join(' ');},
 toggle(v,force){const on=force===undefined?!this.contains(v):force;if(on){this.add(v);}else{this.remove(v);}return on;}
};}});
Object.defineProperty(Element.prototype,'innerText',{get(){return this.textContent;},set(v){this.textContent=v;}});
Object.defineProperty(Element.prototype,'outerHTML',{get(){return '';},set(html){const parent=this.parentElement;if(!parent){return;}const audio=html.match(/<audio[^>]*class="chunk-audio"[^>]*src="([^"]+)"/);if(!audio){return;}const node=new Element('audio',{class:'chunk-audio',src:audio[1]});const index=parent.children.indexOf(this);parent.children.splice(index,1,node);node.parentElement=parent;}});
Element.prototype.insertAdjacentHTML=function(position,html){
 if(position==='afterend'){return;}
 if(position!=='beforeend'){throw Error('unsupported position');}
 const audio=html.match(/<audio[^>]*class="chunk-audio"[^>]*src="([^"]+)"/);
 const node=new Element(audio?'audio':'span',audio?{class:'chunk-audio',src:audio[1]}:{class:'text-muted small'});
 if(!audio){node.textContent='No audio';}
 this.appendChild(node);};
vm.runInContext(core.slice(core.indexOf('        function updateChunkRow('),core.indexOf('        function ensureChunkRefresh(')),context);
vm.runInContext(core.slice(core.indexOf('        const pendingChunkEdits ='),core.indexOf('        function getEditorFieldValue(')),context);
context.invalidateEditorIntegrity=()=>{};context.refreshEditorIntegrity=async()=>{};context.applyDriftFilter=()=>{};
context._driftBadge=()=>'';context.getChunkRowState=()=>'pending';context.ensureEditorRenderSnapshot=async()=>context.cachedChunks;
const row=document.querySelector('#chunks-table-body tr[data-id="1"]');
row.children.filter(c=>c.classList.contains('chunk-audio')).forEach(c=>c.remove());
row.appendChild(new Element('span',{class:'badge'}));
const actions=row.appendChild(new Element('div',{class:'chunk-actions'}));
actions.appendChild(new Element('audio',{class:'chunk-audio',src:'/one.wav','data-id':'1'}));
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
const play=document.getElementById('editor-line-play');assert.equal(play.disabled,false);
context.API.post=async(url,data)=>({status:'pending',error:null,audio_path:null,audio_revision:null,...data});
await context.applyChunkEdits(1,{text:'new semantic words'});context.renderEditorLineInspector();
assert.equal(row.querySelector('.badge').textContent,'pending');
assert.equal(actions.querySelectorAll('audio.chunk-audio').length,0,'stale player must be removed');
assert.equal(actions.querySelectorAll('.text-muted').length,1);
assert.equal(play.disabled,true,'inspector must not play the old take');
context.API.post=async(url,data)=>({status:'done',error:null,audio_path:'one.wav',audio_revision:'r2',...data});
await context.applyChunkEdits(1,{pause_after:300});context.renderEditorLineInspector();
assert.equal(row.querySelector('.badge').textContent,'done');
assert.equal(actions.querySelectorAll('audio.chunk-audio').length,1,'a pause-only save keeps valid audio');
assert.equal(play.disabled,false);
''')

    def test_loading_and_snapshot_warning_repaint_in_selected_locale(self):
        self.run_take(r'''
const pending=context.loadEditorTakeSnapshots();await tick();
const status=document.getElementById('editor-take-status');
assert.equal(status.getAttribute('data-i18n'),'take.status.loading');
vm.runInContext("uiCatalogs.set('es-419',{meta:{review_status:{all:'human-reviewed'}},messages:{'take.status.loading':'Cargando instantaneas guardadas…'}});",context);
vm.runInContext("applyUiLocale('es-419')",context);
assert(status.textContent.includes('Cargando instantaneas'),status.textContent);
gets[0].resolve(result());await pending;assert.equal(status.getAttribute('data-i18n'),'take.status.summary.no-restore');
''')
        self.run_review(r'''
context.t=vm.runInContext('(key,english,values={},locale=getUiLocale())=>translateUiMessage(key,english,values,locale)',context);
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-speech-verification.js'),'utf8'),context);
context.getChunkAudioIdentity=c=>c.audio_revision;context.isEditorEditPending=()=>false;
context.ensureEditorRenderSnapshot=async()=>context.cachedChunks;context.ensureChunkRefresh=async()=>context.cachedChunks;
context.refreshEditorReview=async()=>{vm.runInContext('editorReviewFresh=false',context);};
const chunk=context.cachedChunks[1];chunk.audio_revision='selected';chunk.audio_path='one.wav';
chunk.speech_verification_historical={book_token:context.chunkSnapshotBookToken,uid:chunk.uid,audio_revision:'selected',status:'review',expected_text:'Historical input',input_digest:'d'.repeat(64),audio:{sha256:'e'.repeat(64)}};
chunk.speech_verification_historical_reason='legacy';delete chunk.speech_verification;
context.API.get=async()=>facts();context.API.post=async()=>({book_token:context.chunkSnapshotBookToken,uid:chunk.uid,receipt:{audio_revision:'selected'}});
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
assert.equal(document.getElementById('editor-line-speech-recheck').disabled,false);
await context.recheckEditorSpeech();
const status=document.getElementById('editor-line-speech-status');
assert.equal(status.getAttribute('data-i18n'),'safety.editor.speech.snapshot-changed');
vm.runInContext("uiCatalogs.set('es-419',{meta:{review_status:{all:'human-reviewed'}},messages:{'safety.editor.speech.snapshot-changed':'Revision de voz registrada, pero la instantanea cambio.'}});",context);
vm.runInContext("applyUiLocale('es-419')",context);
assert.equal(status.querySelectorAll('.ui-safety-primary')[0].textContent,'Revision de voz registrada, pero la instantanea cambio.');
assert.equal(status.querySelectorAll('.ui-safety-secondary').length,1,'authored English safety wording stays beside the translation');
''')

    def test_render_barrier_pins_book_through_flush_and_snapshot(self):
        self.run_barrier(r'''
(async()=>{
 const a=client();
 const barrier=a.context.ensureEditorRenderSnapshot().catch(error=>error);
 await tick();
 assert.strictEqual(a.gates.length,1,'A must have a gated row write before the book switch');
 assert.strictEqual(a.gets.length,0);
 a.row.dataset.uid='b-uid';a.row.dataset.bookToken='book-token-b';
 vm.runInContext('chunkSnapshotBookToken="book-token-b";cachedChunks=[{id:7,uid:"b-uid",text:"B saved",status:"done",error:null}];',a.context);
 const bSave=a.context.applyChunkEdits(7,{text:'B pending draft'});
 await tick();assert.strictEqual(a.gates.length,2,'B has its own pending save');
 a.gates.shift()();await tick();
 assert.strictEqual(a.gets.length,0,'the A barrier must not read B\'s snapshot');
 const error=await barrier;
 assert(error && /book changed/i.test(error.message),'the A barrier must refuse, not return B\'s snapshot: '+JSON.stringify(error));
 a.setHold(false);a.gates.shift()();await bSave.catch(()=>{});
 finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_failure_follows_uid_index_move_and_retry_clears_it(self):
        self.run_barrier(r'''
(async()=>{
 const a=client();a.setHold(false);a.setFail(true);
 await a.context.updateChunk(7,'text','bad text');
 let summary=vm.runInContext('getEditorFailedEditsSummary()',a.context);
 assert(summary.includes('Line 8'),summary);
 assert.strictEqual(vm.runInContext('failedChunkEdits.size',a.context),1);
 vm.runInContext('cachedChunks=[{id:3,uid:"row-7",text:"saved",status:"done",error:null}];',a.context);
 summary=vm.runInContext('getEditorFailedEditsSummary()',a.context);
 assert(summary.includes('Line 4'),summary,'the visible line must follow the uid in the current snapshot');
 a.context.API.post=async(url,data)=>url.startsWith('/api/chunks/3?')?{status:'done',error:null,...data}:{};
 await a.context.applyChunkEdits(3,{text:'bad text'});
 assert.strictEqual(vm.runInContext('getEditorFailedEditsSummary()',a.context),'', 'retry by the same uid must clear the earlier refusal');
 assert.strictEqual(vm.runInContext('failedChunkEdits.size',a.context),0);
 finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_row_failure_marker_derived_on_unrelated_success_and_retry(self):
        self.run_barrier(r'''
(async()=>{
 const marks=[];const a=client();a.setHold(false);
 a.row.classList={add:v=>marks.push('add:'+v),remove:v=>marks.push('remove:'+v),contains:v=>false,
  toggle:(v,on)=>marks.push((on?'add:':'remove:')+v)};
 a.setFail(true);
 await a.context.updateChunk(7,'text','bad text');
 assert(marks.includes('add:text-danger'),marks);
 a.setFail(false);
 await a.context.updateChunk(7,'pause_after',300);
 assert(!marks.includes('remove:text-danger'),marks,'an unrelated successful field must not hide a failed field');
 assert.equal(JSON.stringify(vm.runInContext('getFailedChunkEdits().map(f=>f.field)',a.context)),'["text"]');
 await a.context.updateChunk(7,'text','corrected');
 assert(marks.includes('remove:text-danger'),marks,'the marker clears when the last failure settles');
 finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_player_identity_pause_generating_and_semantic_invalidation(self):
        self.run_review(r'''
const core=fs.readFileSync(process.argv[1].replace('app-editor-review.js','app-core.js'),'utf8');
Object.defineProperty(Element.prototype,'classList',{get(){const node=this;return {
 contains:v=>node.className.split(/\s+/).includes(v),
 add(v){if(!this.contains(v)){node.className=(node.className+' '+v).trim();}},
 remove(v){node.className=node.className.split(/\s+/).filter(x=>x!==v).join(' ');},
 toggle(v,force){const on=force===undefined?!this.contains(v):force;if(on){this.add(v);}else{this.remove(v);}return on;}
};}});
Object.defineProperty(Element.prototype,'innerText',{get(){return this.textContent;},set(v){this.textContent=v;}});
Element.prototype.insertAdjacentHTML=function(position,html){
 if(position==='afterend'){return;}
 if(position!=='beforeend'){throw Error('unsupported position');}
 const audio=html.match(/<audio[^>]*class="chunk-audio"[^>]*src="([^"]+)"/);
 const node=new Element(audio?'audio':'span',audio?{class:'chunk-audio',src:audio[1]}:{class:'text-muted small'});
 if(!audio){node.textContent='No audio';}
 this.appendChild(node);};
vm.runInContext(core.slice(core.indexOf('        function updateChunkRow('),core.indexOf('        function ensureChunkRefresh(')),context);
vm.runInContext(core.slice(core.indexOf('        const pendingChunkEdits ='),core.indexOf('        function getEditorFieldValue(')),context);
context.invalidateEditorIntegrity=()=>{};context.refreshEditorIntegrity=async()=>{};context.applyDriftFilter=()=>{};
context._driftBadge=()=>'';context.getChunkRowState=()=>'pending';context.ensureEditorRenderSnapshot=async()=>context.cachedChunks;
const row=document.querySelector('#chunks-table-body tr[data-id="1"]');
row.children.filter(c=>c.classList.contains('chunk-audio')).forEach(c=>c.remove());
row.appendChild(new Element('span',{class:'badge bg-secondary chunk-status-label','data-status':'done'}));
const actions=row.appendChild(new Element('div',{class:'chunk-actions'}));
const player=actions.appendChild(new Element('audio',{class:'chunk-audio',src:'/one.wav?t=r1','data-id':'1'}));
let loads=0;player.load=()=>{loads++;};player.pause=()=>{};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
context.API.post=async(url,data)=>({status:'done',error:null,audio_path:'one.wav',audio_revision:'r1',...data});
await context.applyChunkEdits(1,{pause_after:300});
assert.strictEqual(actions.querySelectorAll('audio.chunk-audio').length,1);
assert.strictEqual(actions.querySelector('audio.chunk-audio'),player,'identical audio keeps the same player element');
assert.strictEqual(loads,0,'an unchanged audio identity must not reload the player');
context.API.post=async(url,data)=>({status:'generating',error:null,audio_path:'one.wav',audio_revision:'r1',...data});
await context.applyChunkEdits(1,{pause_after:400});
assert.strictEqual(actions.querySelectorAll('audio.chunk-audio').length,1,'generating with valid audio keeps the player');
assert.strictEqual(loads,0);
context.API.post=async(url,data)=>({status:'pending',error:null,audio_path:null,audio_revision:null,...data});
await context.applyChunkEdits(1,{text:'new semantic words'});
assert.strictEqual(actions.querySelectorAll('audio.chunk-audio').length,0,'a cleared path removes the stale player');
assert.strictEqual(actions.querySelectorAll('.text-muted').length,1);
''')

    def test_status_badge_keeps_locale_selector_after_save_and_switch(self):
        self.run_review(r'''
const core=fs.readFileSync(process.argv[1].replace('app-editor-review.js','app-core.js'),'utf8');
Object.defineProperty(Element.prototype,'classList',{get(){const node=this;return {
 contains:v=>node.className.split(/\s+/).includes(v),
 add(v){if(!this.contains(v)){node.className=(node.className+' '+v).trim();}},
 remove(v){node.className=node.className.split(/\s+/).filter(x=>x!==v).join(' ');},
 toggle(v,force){const on=force===undefined?!this.contains(v):force;if(on){this.add(v);}else{this.remove(v);}return on;}
};}});
Object.defineProperty(Element.prototype,'innerText',{get(){return this.textContent;},set(v){this.textContent=v;}});
Element.prototype.insertAdjacentHTML=function(position,html){
 if(position==='afterend'){return;}
 if(position!=='beforeend'){throw Error('unsupported position');}
 const audio=html.match(/<audio[^>]*class="chunk-audio"[^>]*src="([^"]+)"/);
 const node=new Element(audio?'audio':'span',audio?{class:'chunk-audio',src:audio[1]}:{class:'text-muted small'});
 if(!audio){node.textContent='No audio';}
 this.appendChild(node);};
vm.runInContext(core.slice(core.indexOf('        function updateChunkRow('),core.indexOf('        function ensureChunkRefresh(')),context);
vm.runInContext(core.slice(core.indexOf('        function chunkStatusLabel('),core.indexOf('        // The Script tab')),context);
vm.runInContext(core.slice(core.indexOf('        function renderVoiceLocaleChrome('),core.indexOf('        function getVoiceSectionStorageKey(')),context);
vm.runInContext(core.slice(core.indexOf('        const pendingChunkEdits ='),core.indexOf('        function getEditorFieldValue(')),context);
context.invalidateEditorIntegrity=()=>{};context.refreshEditorIntegrity=async()=>{};context.applyDriftFilter=()=>{};
context._driftBadge=()=>'';context.getChunkRowState=()=>'pending';context.ensureEditorRenderSnapshot=async()=>context.cachedChunks;
const row=document.querySelector('#chunks-table-body tr[data-id="1"]');
row.children.filter(c=>c.classList.contains('chunk-audio')).forEach(c=>c.remove());
const badge=row.appendChild(new Element('span',{class:'badge bg-secondary chunk-status-label','data-status':'pending'}));
const actions=row.appendChild(new Element('div',{class:'chunk-actions'}));
await context.refreshEditorReview();context.selectEditorReviewLineById(1);
context.API.post=async(url,data)=>({status:'done',error:null,audio_path:null,audio_revision:null,...data});
await context.applyChunkEdits(1,{text:'updated line'});
assert(badge.classList.contains('chunk-status-label'),badge.className);
assert.strictEqual(badge.getAttribute('data-status'),'done');
assert.strictEqual(badge.textContent,'done');
context.t=vm.runInContext('(key,english,values={},locale=getUiLocale())=>translateUiMessage(key,english,values,locale)',context);
vm.runInContext("uiCatalogs.set('es-419',{meta:{review_status:{all:'human-reviewed'}},messages:{'core.editor.status.done':'hecho'}});",context);
vm.runInContext("applyUiLocale('es-419')",context);
context.renderVoiceLocaleChrome();
assert.strictEqual(badge.textContent,'hecho',badge.textContent);
''')


if __name__ == '__main__':
    unittest.main()
