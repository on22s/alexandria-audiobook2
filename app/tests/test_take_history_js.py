"""Snapshot UI races preserve line selection, drafts and literal labels."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-take-history.js'
SETUP = r'''
context.window=context;context.URLSearchParams=URLSearchParams;
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children]){child.remove();}this.textContent='';for(const child of nodes){this.appendChild(child);}};
Element.prototype.append=function(...nodes){for(const child of nodes){this.appendChild(child);}};
Element.prototype.pause=function(){this.paused=true;};Element.prototype.load=function(){this.loaded=true;};
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(v){if(v){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
for(const id of ['editor-take-save','editor-take-list','editor-take-more','editor-take-first','editor-take-status','editor-take-cancel','editor-auto-take-refresh','editor-auto-take-on','editor-auto-take-off','editor-auto-take-status']){body.appendChild(new Element('div',{id}));}
let selected={chunk:{id:0,uid:'u',text:'Synthetic words.',instruct:'',speaker:'Hero',audio_path:'line.wav',audio_revision:'r1'}};
context.chunkSnapshotBookToken='a'.repeat(64);context.getEditorReviewTarget=()=>selected;
context.getChunkAudioIdentity=c=>JSON.stringify([c.audio_path,c.audio_revision]);
let edits=false;context.isEditorEditPending=()=>edits;context.ensureEditorRenderSnapshot=async()=>{};
const errors=[],toasts=[],posts=[],gets=[];context.showActionError=(...v)=>errors.push(v);context.showToast=(...v)=>toasts.push(v);
context.showConfirm=async()=>true;
context.API={post:(url,body)=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});posts.push({url,body,resolve,reject});return promise;},get:url=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});gets.push({url,resolve,reject});return promise;}};
const sample=(state='ready')=>({version:'b'.repeat(32),book_token:context.chunkSnapshotBookToken,uid:'<img onerror=bad()>',created:1,take:3,state,protected:false,sha256:'c'.repeat(64)});
const result=(records=[sample()])=>({records,total:records.length,next_offset:null,usage:{records:records.length,bytes:4800,max_records:4096,max_bytes:1073741824}});
const tick=()=>new Promise(resolve=>setImmediate(resolve));load();
const polls=[];context._startPolling=(key,fetch,options)=>polls.push({key,fetch,...options});
const redraws=[];context.loadChunks=async force=>redraws.push(force);
'''


class TakeHistoryJsTests(unittest.TestCase):
    def run_js(self, body):
        script = DOM_FIXTURE + SETUP + '\nlet done=false;process.on("beforeExit",()=>assert(done));(async()=>{' + body + '\ndone=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        run = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_old_list_reply_and_old_audio_clear_when_line_or_book_changes(self):
        self.run_js(r'''
const pending=context.loadEditorTakeSnapshots(true);await tick();assert.equal(gets.length,1);assert(gets[0].url.includes('all_books=true'));
selected={chunk:{...selected.chunk,uid:'different'}};context.applyTakeHistoryInspector();gets[0].resolve(result());await pending;
assert.equal(document.getElementById('editor-take-list').children.length,0);
const next=context.loadEditorTakeSnapshots(true);await tick();gets[1].resolve(result());await next;
const audio=document.getElementById('editor-take-list').querySelectorAll('audio')[0];assert(audio);assert.equal(audio.preload,'none');assert(audio.src.includes('sha256='));
context.chunkSnapshotBookToken='d'.repeat(64);context.applyTakeHistoryInspector();assert(audio.paused);assert(audio.loaded);assert.equal(document.getElementById('editor-take-list').children.length,0);
''')

    def test_save_is_uid_revision_bound_duplicate_clicks_block_and_drafts_refuse(self):
        self.run_js(r'''
const original=JSON.stringify(selected.chunk);const pending=context.saveEditorTakeSnapshot();await tick();assert.equal(posts.length,1);assert.equal(posts[0].url,'/api/chunks/0/take_snapshots');assert.equal(posts[0].body.expected_uid,'u');assert.equal(posts[0].body.expected_audio_revision,'r1');assert.equal(posts[0].body.expected_book_token,context.chunkSnapshotBookToken);
await context.saveEditorTakeSnapshot();assert.equal(posts.length,1);
selected={chunk:{...selected.chunk,uid:'other'}};posts[0].resolve(sample());await pending;assert.equal(toasts.length,0);assert.equal(gets.length,0);assert.equal(JSON.parse(original).audio_path,selected.chunk.audio_path);
edits=true;await context.saveEditorTakeSnapshot();assert.equal(posts.length,1);
''')

    def test_snapshot_labels_are_literal_and_actions_use_exact_version_hash(self):
        self.run_js(r'''
const pending=context.loadEditorTakeSnapshots(true);await tick();gets[0].resolve(result());await pending;
const list=document.getElementById('editor-take-list');assert.equal(list.querySelectorAll('img').length,0);assert(list.children[0].children[0].textContent.includes('<img onerror=bad()>'));
const current=sample();const identity=vm.runInContext('takeHistoryIdentity',context);const action=context.changeEditorTakeSnapshot(current,'protect',identity,context.chunkSnapshotBookToken);await tick();assert.equal(posts.length,1);assert.equal(posts[0].body.version,current.version);assert.equal(posts[0].body.sha256,current.sha256);assert.equal(posts[0].body.expected_uid,current.uid);assert.equal(posts[0].body.source_book_token,current.book_token);assert.equal(posts[0].body.action,'protect');
posts[0].resolve({});await tick();gets[1].resolve(result([{...current,protected:true}]));await action;assert.equal(selected.chunk.audio_revision,'r1');assert.equal(posts.length,1);
''')

    def test_permanent_removal_rechecks_target_after_confirmation(self):
        self.run_js(r'''
const pending=context.loadEditorTakeSnapshots();await tick();gets[0].resolve(result([sample('deleted')]));await pending;
let confirm;context.showConfirm=()=>new Promise(resolve=>confirm=resolve);
const action=context.changeEditorTakeSnapshot(sample('deleted'),'purge',vm.runInContext('takeHistoryIdentity',context),context.chunkSnapshotBookToken);await tick();selected.chunk.audio_revision='new';confirm(true);await action;assert.equal(posts.length,0);
''')

    def test_deleted_and_pending_copies_have_no_audio_and_remain_visible_in_usage(self):
        self.run_js(r'''
const pending=context.loadEditorTakeSnapshots();await tick();gets[0].resolve(result([sample('deleted'),{...sample('pending'),version:'e'.repeat(32)}]));await pending;
assert.equal(document.getElementById('editor-take-list').querySelectorAll('audio').length,0);assert(document.getElementById('editor-take-status').textContent.includes('Deleted and incomplete copies count'));assert(document.getElementById('editor-take-list').children[0].children[2].children.some(b=>b.textContent==='Undo deletion'));
''')

    def test_two_saves_waiting_on_the_same_render_barrier_issue_one_request(self):
        self.run_js(r'''
let release;context.ensureEditorRenderSnapshot=()=>new Promise(resolve=>release=resolve);
const one=context.saveEditorTakeSnapshot();await tick();const first=release;
const two=context.saveEditorTakeSnapshot();await tick();const second=release;
first();second();await tick();assert.equal(posts.length,1);
selected.chunk.audio_revision='changed';posts[0].resolve(sample());await Promise.all([one,two]);assert.equal(posts.length,1);
''')

    def test_save_never_retargets_after_line_book_or_take_changes_during_render_barrier(self):
        self.run_js(r'''
for (const change of [
    () => {selected={chunk:{...selected.chunk,id:1,uid:'other'}};},
    () => {context.chunkSnapshotBookToken='d'.repeat(64);},
    () => {selected.chunk.audio_revision='new-revision';},
]) {
    selected={chunk:{id:0,uid:'u',text:'Synthetic words.',instruct:'',speaker:'Hero',audio_path:'line.wav',audio_revision:'r1'}};
    context.chunkSnapshotBookToken='a'.repeat(64);
    let release;context.ensureEditorRenderSnapshot=()=>new Promise(resolve=>release=resolve);
    const pending=context.saveEditorTakeSnapshot();await tick();change();release();await pending;
    assert.equal(posts.length,0);assert.equal(gets.length,0);
}
''')

    def test_policy_refresh_and_change_are_serialized_and_use_admitted_book_revision(self):
        self.run_js(r'''
context.renderAutomaticTakePolicy();assert(document.getElementById('editor-auto-take-on').disabled);
const pending=context.loadAutomaticTakePolicy();await tick();assert.equal(gets[0].url,'/api/take_history_policy');
await context.loadAutomaticTakePolicy();assert.equal(gets.length,1);assert(document.getElementById('editor-auto-take-refresh').disabled);
const policy={enabled:false,book_token:'e'.repeat(64),revision:'f'.repeat(64),error:null};gets[0].resolve(policy);await pending;
assert(!document.getElementById('editor-auto-take-on').disabled);assert(document.getElementById('editor-auto-take-off').disabled);
const change=context.changeAutomaticTakePolicy(true);await tick();await context.changeAutomaticTakePolicy(true);assert.equal(posts.length,1);
assert.equal(posts[0].url,'/api/take_history_policy');assert.equal(posts[0].body.expected_book_token,policy.book_token);assert.equal(posts[0].body.expected_revision,policy.revision);assert.equal(posts[0].body.enabled,true);
posts[0].resolve({...policy,enabled:true,revision:'1'.repeat(64)});await change;
assert(document.getElementById('editor-auto-take-on').disabled);assert(!document.getElementById('editor-auto-take-off').disabled);
assert.equal(selected.chunk.audio_revision,'r1');assert.equal(toasts.length,1);
''')

    def test_failed_policy_read_or_write_requires_refresh_before_retry(self):
        self.run_js(r'''
const pending=context.loadAutomaticTakePolicy();await tick();gets[0].reject(new Error('unreadable'));await pending;
await context.changeAutomaticTakePolicy(true);assert.equal(posts.length,0);assert(document.getElementById('editor-auto-take-on').disabled);
const reload=context.loadAutomaticTakePolicy();await tick();gets[1].resolve({enabled:false,book_token:'a'.repeat(64),revision:'b'.repeat(64)});await reload;
const change=context.changeAutomaticTakePolicy(true);await tick();posts[0].reject(new Error('book changed'));await change;
await context.changeAutomaticTakePolicy(true);assert.equal(posts.length,1);assert.equal(errors.length,2);
assert(!document.getElementById('editor-auto-take-refresh').disabled);assert(document.getElementById('editor-auto-take-off').disabled);
''')

    def test_damaged_policy_can_be_disabled_without_a_selected_line_or_saved_drafts(self):
        self.run_js(r'''
selected=null;edits=true;
const pending=context.loadAutomaticTakePolicy();await tick();const policy={enabled:null,book_token:'a'.repeat(64),revision:'b'.repeat(64),error:'Damaged <script> policy'};
gets[0].resolve(policy);await pending;
assert.equal(document.getElementById('editor-auto-take-status').textContent,policy.error);assert(!document.getElementById('editor-auto-take-off').disabled);
const change=context.changeAutomaticTakePolicy(false);await tick();assert.equal(posts.length,1);assert.equal(posts[0].body.enabled,false);
posts[0].resolve({...policy,enabled:false,error:null});await change;assert.equal(toasts.length,1);assert.equal(errors.length,0);
''')

    def test_restore_controls_require_supported_same_book_uid_and_recorded_evidence(self):
        self.run_js(r'''
const eligible={...sample(),uid:'u',has_restore_evidence:true};
for(const [records,supported,count] of [
    [[eligible],true,1], [[{...eligible,book_token:'e'.repeat(64)}],true,0],
    [[{...eligible,uid:'other'}],true,0], [[{...eligible,has_restore_evidence:false}],true,0],
    [[{...eligible,state:'deleted'}],true,0], [[eligible],false,0],
]) {
    context.renderEditorTakeSnapshots({...result(records),restore_supported:supported},context.chunkSnapshotBookToken,context.getTakeHistoryIdentity());
    assert.equal(document.getElementById('editor-take-list').querySelectorAll('button').filter(b=>b.textContent==='Check & restore').length,count);
}
''')

    def test_restore_request_binds_current_path_counter_and_source_then_waits_for_audio_task(self):
        self.run_js(r'''
selected.chunk.take=5;const record={...sample(),uid:'u',has_restore_evidence:true};
const identity=context.getTakeHistoryIdentity();const restore=context.restoreEditorTakeSnapshot(record,identity,context.chunkSnapshotBookToken);
await tick();assert.equal(posts.length,1);assert.equal(posts[0].url,'/api/chunks/0/take_restore');
assert.equal(posts[0].body.expected_take,5);assert.equal(posts[0].body.expected_audio_path,'line.wav');
assert.equal(posts[0].body.expected_audio_revision,'r1');assert.equal(posts[0].body.version,record.version);
assert.equal(posts[0].body.sha256,record.sha256);assert.equal(posts[0].body.expected_uid,'u');
await context.restoreEditorTakeSnapshot(record,identity,context.chunkSnapshotBookToken);assert.equal(posts.length,1);
posts[0].resolve({status:'started'});await restore;assert.equal(polls.length,1);assert.equal(vm.runInContext('takeHistoryBusy',context),true);
assert.equal(polls[0].doneCheck({running:true}),false);assert.equal(polls[0].doneCheck({running:false}),true);
polls[0].onTick({running:true});assert(document.getElementById('editor-take-status').textContent.includes('checking restoration'));
const cancel=context.cancelEditorTakeRestore();await tick();assert.equal(posts[1].url,'/api/cancel_audio');
posts[1].resolve({status:'cancelling'});await cancel;assert.equal(vm.runInContext('takeHistoryBusy',context),true);
polls[0].onDone({running:false,status:'cancelled'});await tick();assert.deepEqual(redraws,[true]);
assert.equal(vm.runInContext('takeHistoryBusy',context),false);assert.equal(document.getElementById('editor-take-cancel').hidden,true);
assert.equal(selected.chunk.audio_revision,'r1');assert.equal(selected.chunk.take,5);
''')

    def test_restore_rechecks_render_barrier_and_late_completion_cannot_retarget_selected_line(self):
        self.run_js(r'''
const record={...sample(),uid:'u',has_restore_evidence:true};selected.chunk.take=3;
for(const change of [()=>{selected.chunk.take++;},()=>{selected.chunk.audio_path='other.wav';},()=>{selected.chunk.uid='other';},()=>{context.chunkSnapshotBookToken='e'.repeat(64);},()=>{edits=true;}]) {
    selected.chunk={id:0,uid:'u',text:'Synthetic words.',instruct:'',speaker:'Hero',audio_path:'line.wav',audio_revision:'r1',take:3};
    context.chunkSnapshotBookToken='a'.repeat(64);edits=false;let release;
    context.ensureEditorRenderSnapshot=()=>new Promise(resolve=>release=resolve);
    const pending=context.restoreEditorTakeSnapshot(record,context.getTakeHistoryIdentity(),context.chunkSnapshotBookToken);
    await tick();change();release();await pending;assert.equal(posts.length,0);
}
edits=false;context.ensureEditorRenderSnapshot=async()=>{};selected.chunk.uid='u';
const pending=context.restoreEditorTakeSnapshot(record,context.getTakeHistoryIdentity(),context.chunkSnapshotBookToken);await tick();
posts[0].resolve({status:'started'});await pending;selected.chunk.uid='different';polls[0].onDone({running:false,status:'done'});
assert.equal(redraws.length,0);assert.equal(selected.chunk.uid,'different');assert.equal(vm.runInContext('takeHistoryBusy',context),false);
''')

    def test_late_restore_admission_reply_preserves_another_lines_status_and_still_releases_busy(self):
        self.run_js(r'''
const record={...sample(),uid:'u',has_restore_evidence:true};
const pending=context.restoreEditorTakeSnapshot(record,context.getTakeHistoryIdentity(),context.chunkSnapshotBookToken);
await tick();assert.equal(posts.length,1);
selected.chunk.uid='other';document.getElementById('editor-take-status').textContent='Other line: refresh snapshots.';
posts[0].resolve({status:'started'});await pending;
assert.equal(document.getElementById('editor-take-status').textContent,'Other line: refresh snapshots.');
assert.equal(polls.length,1);assert.equal(vm.runInContext('takeHistoryBusy',context),true);
polls[0].onDone({running:false,status:'done'});
assert.equal(document.getElementById('editor-take-status').textContent,'Other line: refresh snapshots.');
assert.equal(redraws.length,0);assert.equal(vm.runInContext('takeHistoryBusy',context),false);
''')
