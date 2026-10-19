"""Real Editor timing UI: Unicode offsets, seeking, highlighting and stale replies."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-word-timing.js'
SETUP = r'''
context.window=context;context.URLSearchParams=URLSearchParams;
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children]){child.remove();}this.textContent='';for(const child of nodes){this.appendChild(child);}};
Element.prototype.querySelector=function(s){return this.querySelectorAll(s)[0]||null;};
Element.prototype.removeEventListener=function(event,fn){this.listeners[event]=(this.listeners[event]||[]).filter(x=>x!==fn);};
Element.prototype.focus=function(){document.activeElement=this;};
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(v){if(v){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Object.defineProperty(Element.prototype,'classList',{get(){const n=this;return {contains:value=>n.className.split(/\s+/).includes(value),toggle(value,on){const values=new Set(n.className.split(/\s+/));if(on){values.add(value);}else{values.delete(value);}n.className=[...values].join(' ');}};}});
document.createTextNode=text=>{const n=new Element('span');n.textContent=text;return n;};
for(const id of ['editor-word-spans','editor-word-status','editor-word-timing','editor-word-cancel']){body.appendChild(new Element('div',{id}));}
const row=new Element('tr'),audio=row.appendChild(new Element('audio',{class:'chunk-audio'}));audio.currentTime=0;audio.ended=false;audio.pause=()=>{};audio.load=()=>{audio.loaded=true;};
let selected={chunk:{id:0,uid:'u',text:'🌟 Amber arrives.',speaker:'Hero',audio_path:'line.wav',audio_revision:'take1'},row};
context.chunkSnapshotBookToken='a'.repeat(64);context.getEditorReviewTarget=()=>selected;
context.getChunkAudioIdentity=c=>JSON.stringify([c.audio_path,c.audio_revision]);
let edits=false;context.isEditorEditPending=()=>edits;context.ensureEditorRenderSnapshot=async()=>{};
const errors=[],posts=[];context.showActionError=(...v)=>errors.push(v);context.showToast=(...v)=>errors.push(v);
context.API={post:(url,body)=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});posts.push({url,body,resolve,reject});return promise;}};
const result=()=>({display_text:'🌟 Amber arrives.',spans:[{from:2,to:7,start:.2,end:.7},{from:8,to:15,start:.8,end:1.3}],notice:'ASR estimate',cached:false,binding:{book_token:context.chunkSnapshotBookToken,uid:'u',row:{audio_revision:'take1'},audio_sha256:'b'.repeat(64)}});
const tick=()=>new Promise(resolve=>setImmediate(resolve));load();
'''


class WordTimingJsTests(unittest.TestCase):
    def run_js(self, body):
        code = DOM_FIXTURE + SETUP + '\nlet done=false;process.on("beforeExit",()=>assert(done));(async()=>{' + body + '\ndone=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        output = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, output.returncode, output.stderr)

    def test_unicode_spans_seek_native_player_and_track_playback_without_generation(self):
        self.run_js(r'''
const pending=context.createEditorWordTiming();await tick();assert.equal(posts.length,1);assert.equal(posts[0].body.expected_audio_revision,'take1');posts[0].resolve(result());await pending;
const area=document.getElementById('editor-word-spans'),buttons=area.querySelectorAll('button');assert(audio.loaded);assert(audio.src.includes('word_timing/audio?'));assert.equal(buttons[0].textContent,'Amber');assert.equal(buttons[1].textContent,'arrives');assert.equal(area.children[0].textContent,'🌟 ');
buttons[1].onclick();assert.equal(audio.currentTime,.8);audio.fire('seeked');assert.equal(buttons[1].getAttribute('aria-current'),'true');assert.equal(buttons[0].getAttribute('aria-current'),null);assert.equal(posts.length,1);
audio.ended=true;audio.fire('ended');assert.equal(buttons[1].getAttribute('aria-current'),null);
selected={...selected,chunk:{...selected.chunk,audio_revision:'take2'}};context.applyWordTimingInspector();assert.equal(area.children.length,0);assert.equal(audio.listeners.timeupdate.length,0);
''')

    def test_late_reply_cannot_paint_another_line_or_override_pending_edits(self):
        self.run_js(r'''
const pending=context.createEditorWordTiming();await tick();selected={...selected,chunk:{...selected.chunk,uid:'replacement'}};context.applyWordTimingInspector();posts[0].resolve(result());await pending;assert.equal(document.getElementById('editor-word-spans').children.length,0);assert(document.getElementById('editor-word-status').textContent.includes('changed'));
selected={...selected,chunk:{...selected.chunk,uid:'u'}};const second=context.createEditorWordTiming();await tick();edits=true;posts[1].resolve(result());await second;assert.equal(document.getElementById('editor-word-spans').children.length,0);
''')

    def test_failed_snapshot_does_not_start_asr_and_wrong_take_result_is_refused(self):
        self.run_js(r'''
context.ensureEditorRenderSnapshot=async()=>{throw Error('save failed');};await context.createEditorWordTiming();assert.equal(posts.length,0);assert.equal(errors.length,1);
context.ensureEditorRenderSnapshot=async()=>{};const p=context.createEditorWordTiming();await tick();const wrong=result();wrong.binding.row.audio_revision='other';posts[0].resolve(wrong);await p;assert.equal(document.getElementById('editor-word-spans').children.length,0);assert.equal(errors.length,2);
''')

    def test_reattached_timing_prevents_new_start_and_retains_cancel_after_redraw(self):
        self.run_js(r'''
context.applyWordTimingTaskActivity(true);context.applyWordTimingInspector();
assert(document.getElementById('editor-word-timing').disabled);assert(!document.getElementById('editor-word-cancel').disabled);
await context.createEditorWordTiming();assert.equal(posts.length,0);
context.applyWordTimingTaskActivity(false);assert(!document.getElementById('editor-word-timing').disabled);assert(document.getElementById('editor-word-cancel').disabled);
assert(document.getElementById('editor-word-status').textContent.includes('saved result'));
const own=context.createEditorWordTiming();await tick();posts[0].resolve(result());await own;
const readyStatus=document.getElementById('editor-word-status').textContent;context.applyWordTimingTaskActivity(true);context.applyWordTimingTaskActivity(false);
assert.equal(document.getElementById('editor-word-status').textContent,readyStatus);assert(document.getElementById('editor-word-spans').children.length>0);
''')
