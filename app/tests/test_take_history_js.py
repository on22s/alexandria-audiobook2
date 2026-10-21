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
for(const id of ['editor-take-save','editor-take-list','editor-take-more','editor-take-first','editor-take-status']){body.appendChild(new Element('div',{id}));}
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
