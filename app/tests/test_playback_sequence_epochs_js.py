"""Actual sequence-player callbacks cannot cross Stop/restart ownership."""
from pathlib import Path
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),plays=[],pauses=[],attempts=[],toasts=[];
const classes=()=>({values:new Set(),add(name){this.values.add(name);},remove(name){this.values.delete(name);},replace(oldName,newName){this.values.delete(oldName);this.values.add(newName);}});
const rows=[0,1,2].map(id=>({dataset:{id:String(id)},classList:classes(),scrollIntoView(){}}));
const audios=rows.map((row,id)=>({src:'fixture.wav',currentTime:7,onended:null,onerror:null,
 getAttribute(name){assert.strictEqual(name,'src');return this.src;},closest(){return row;},pause(){pauses.push(id);},
 play(){plays.push(id);let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});attempts.push({id,resolve,reject});return promise;}}));
const button={innerHTML:'Play Sequence',onclick:null,classList:classes()};button.classList.add('btn-primary');
const ctx={console:{error(){}},showToast:(text,tone)=>toasts.push({text,tone}),
 showActionError:(text,error,recovery,tone)=>toasts.push({text,reason:error&&error.message,tone}),API:{get:async()=>({pauses:[]})},setTimeout,clearTimeout,document:{
 getElementById(id){assert.strictEqual(id,'btn-play-seq');return button;},
 querySelectorAll(selector){if(selector==='audio'||selector==='.chunk-audio'){return audios;}assert.strictEqual(selector,'tr');return rows;}}};ctx.window=ctx;
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('let isPlayingSequence ='),source.indexOf('let isRenderingAll =')),ctx);
vm.runInContext(source.slice(source.indexOf('window.stopOthers ='),source.indexOf('const pendingChunkEdits =')),ctx);
vm.runInContext(source.slice(source.indexOf("let currentBookFilename ="),source.indexOf('function enqueueBookSelection')),ctx);
const settle=async()=>{await Promise.resolve();await Promise.resolve();};
const playing=()=>vm.runInContext('isPlayingSequence',ctx);
const snapshot=()=>JSON.stringify({plays,pauses,button:button.innerHTML,highlight:rows.map(row=>[...row.classList.values]),toasts});
'''


class PlaybackSequenceEpochJsTests(unittest.TestCase):
    def run_js(self, code):
        script = SETUP + '\n(async()=>{\n' + code + '\n})().catch(error=>{console.error(error);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_stopped_play_rejection_cannot_advance_or_stop_a_restarted_run(self):
        for empty_tail in (False, True):
            with self.subTest(empty_tail=empty_tail):
                self.run_js(('audios[1].src="";audios[2].src="";\n' if empty_tail else '') + r'''
await ctx.playSequence();const old=attempts[0];ctx.stopSequence();await ctx.playSequence();
assert.deepStrictEqual(plays,[0,0]);const before=snapshot();
old.reject(new Error('stopped play'));await settle();
assert.strictEqual(snapshot(),before);assert.strictEqual(playing(),true);
attempts[1].resolve();await settle();assert.strictEqual(snapshot(),before);
''')

        for retire in ("ctx.stopSequence();", "ctx.applyCurrentBookFilename('book-B');", "ctx.applyCurrentBookFilename('book-A');"):
            for restart in (False, True):
                with self.subTest(pending_plan_retirement=retire, restart=restart):
                    self.run_js(r"""
vm.runInContext('currentBookFilename="book-A"',ctx);
let rejectOld;ctx.API.get=()=>new Promise((resolve,reject)=>{rejectOld=reject;});
const old=ctx.playSequence();RETIRE
ctx.API.get=async()=>({pauses:[]});
RESTART
const before=snapshot();rejectOld(new Error('retired plan'));await old;await settle();
assert.strictEqual(snapshot(),before);assert.strictEqual(playing(),EXPECTED);
""".replace('RETIRE',retire).replace('RESTART','await ctx.playSequence();' if restart else '').replace('EXPECTED','true' if restart else 'false'))

    def test_saved_old_ended_and_error_handlers_cannot_mutate_restart(self):
        for event in ('onended', 'onerror'):
            with self.subTest(event=event):
                self.run_js(r'''
await ctx.playSequence();const old=audios[0].EVENT;ctx.stopSequence();await ctx.playSequence();
const before=snapshot();old();await settle();
assert.strictEqual(snapshot(),before);assert.strictEqual(playing(),true);
'''.replace('EVENT', event))

        for filename in ('book-B', 'book-A'):
            for event in ('onended', 'onerror', 'reject'):
                with self.subTest(book_apply=filename, late_event=event):
                    self.run_js(r"""
vm.runInContext('currentBookFilename="book-A"',ctx);
await ctx.playSequence();const old=audios[0].EVENT,oldAttempt=attempts[0];
ctx.applyCurrentBookFilename('FILENAME');assert.strictEqual(playing(),false);
const retired=snapshot();
if ('EVENT' === 'reject') { oldAttempt.reject(new Error('old book request')); } else { old(); }
await settle();assert.strictEqual(snapshot(),retired);
await ctx.playSequence();assert.deepStrictEqual(plays,[0,0]);const restarted=snapshot();
if ('EVENT' !== 'reject') { old(); }
await settle();assert.strictEqual(snapshot(),restarted);assert.strictEqual(playing(),true);
""".replace('FILENAME',filename).replace('EVENT',event))

    def test_stop_retires_handlers_and_resets_audio_feedback_and_button(self):
        self.run_js(r'''
ctx.applyCurrentBookFilename('book-A');assert.deepStrictEqual(pauses,[]);assert.ok(audios.every(audio=>audio.currentTime===7));
await ctx.playSequence();assert.strictEqual(typeof audios[0].onended,'function');assert.strictEqual(typeof audios[0].onerror,'function');
ctx.stopSequence();assert.strictEqual(playing(),false);
assert.deepStrictEqual(pauses,[0,1,2]);assert.ok(audios.every(audio=>audio.currentTime===0&&audio.onended===null&&audio.onerror===null));
assert.ok(rows.every(row=>!row.classList.values.has('table-primary')));assert.ok(button.classList.values.has('btn-primary'));
assert.strictEqual(button.onclick,ctx.playSequence);assert.strictEqual(toasts.length,0);
const before=snapshot();attempts[0].reject(new Error('stopped'));await settle();assert.strictEqual(snapshot(),before);
''')

    def test_active_run_error_and_rejection_skip_once_and_complete_normally(self):
        self.run_js(r'''
await ctx.playSequence();const error=audios[0].onerror;error();assert.deepStrictEqual(plays,[0,1]);
attempts[0].reject(new Error('same failed track'));error();await settle();assert.deepStrictEqual(plays,[0,1]);
assert.strictEqual(audios[0].onended,null);assert.strictEqual(audios[0].onerror,null);
audios[1].onended();assert.deepStrictEqual(plays,[0,1,2]);audios[2].onended();
assert.strictEqual(playing(),false);assert.strictEqual(button.onclick,ctx.playSequence);
assert.strictEqual(toasts.length,1);assert.strictEqual(toasts[0].tone,'warning');assert.match(toasts[0].text,/1 chunk\(s\) skipped/);
''')

    def test_plan_pauses_apply_nonzero_and_zero_and_duplicate_end_is_once(self):
        self.run_js(r'''
ctx.API.get=async()=>({pauses:[{chunk_id:0,pause_ms:30},{chunk_id:1,pause_ms:0}]});
await ctx.playSequence();const end=audios[0].onended;end();end();
assert.deepStrictEqual(plays,[0]);await new Promise(r=>setTimeout(r,45));
assert.deepStrictEqual(plays,[0,1]);audios[1].onended();assert.deepStrictEqual(plays,[0,1,2]);
// A refused plan (a stored pause export would also refuse) plays nothing and says why.
ctx.stopSequence();const before=plays.length;
ctx.API.get=async()=>{throw new Error('Playback pauses unavailable: Shorten the pause before exporting.');};
await ctx.playSequence();await settle();
assert.strictEqual(plays.length,before);assert.strictEqual(playing(),false);
assert.deepStrictEqual(toasts.at(-1),{text:'Could not load playback timing',reason:'Playback pauses unavailable: Shorten the pause before exporting.',tone:'warning'});
''')

    def test_stop_and_restart_cancel_pending_plan_pause(self):
        self.run_js(r'''
ctx.API.get=async()=>({pauses:[{chunk_id:0,pause_ms:50}]});
await ctx.playSequence();audios[0].onended();ctx.stopSequence();
await new Promise(r=>setTimeout(r,70));assert.deepStrictEqual(plays,[0]);assert.strictEqual(playing(),false);
await ctx.playSequence();audios[0].onended();await ctx.playSequence();
await new Promise(r=>setTimeout(r,70));assert.deepStrictEqual(plays,[0,0,0]);assert.strictEqual(playing(),true);
''')

    def test_trim_start_and_end_control_advance(self):
        self.run_js(r'''
ctx.API.get=async()=>({pauses:[],trims:[
 {chunk_id:0,start_ms:50,end_ms:80},
 {chunk_id:1,start_ms:0,end_ms:1000},
 {chunk_id:2,start_ms:0,end_ms:1000}]});
await ctx.playSequence();
assert.deepStrictEqual(plays,[0]);
assert.ok(Math.abs(audios[0].currentTime - 0.05) < 1e-6);
audios[0].currentTime=0.1;
audios[0].ontimeupdate();
assert.deepStrictEqual(plays,[0,1]);
assert.strictEqual(audios[0].ontimeupdate,null);
''')

    def test_runtime_skip_uses_gap_for_actual_neighbors(self):
        self.run_js(r'''
const gaps=[];
ctx.API.get=async url => {
  if (url.includes('playback_gap')) { gaps.push(url); return {pause_ms:30}; }
  return {pauses:[],trims:[
   {chunk_id:0,start_ms:0,end_ms:1000},
   {chunk_id:1,start_ms:0,end_ms:1000},
   {chunk_id:2,start_ms:0,end_ms:1000}]};
};
await ctx.playSequence();
audios[0].onended();
assert.deepStrictEqual(plays,[0,1]);
attempts[1].reject(new Error('failed decode'));await settle();
assert.deepStrictEqual(plays,[0,1]);
assert.strictEqual(gaps.length,1);
assert.match(gaps[0],/from_id=0&to_id=2/);
await new Promise(r=>setTimeout(r,45));
assert.deepStrictEqual(plays,[0,1,2]);
''')

    def test_elapsed_pause_is_not_applied_again_after_a_play_error(self):
        self.run_js(r'''
ctx.API.get=async url => {
  if (url.includes('playback_gap')) { return {pause_ms:200}; }
  return {pauses:[{chunk_id:0,pause_ms:200}],trims:[
   {chunk_id:0,start_ms:0,end_ms:1000},
   {chunk_id:1,start_ms:0,end_ms:1000},
   {chunk_id:2,start_ms:0,end_ms:1000}]};
};
await ctx.playSequence();
audios[0].onended();
await new Promise(r=>setTimeout(r,220));
assert.deepStrictEqual(plays,[0,1]);
attempts[1].reject(new Error('failed play'));await settle();
await new Promise(r=>setTimeout(r,10));
assert.deepStrictEqual(plays,[0,1,2]);
''')

    def test_zero_trim_start_resets_stale_audio_position(self):
        self.run_js(r'''
ctx.API.get=async()=>({pauses:[],trims:[
 {chunk_id:0,start_ms:0,end_ms:1000},
 {chunk_id:1,start_ms:0,end_ms:1000},
 {chunk_id:2,start_ms:0,end_ms:1000}]});
audios[0].currentTime=7;
await ctx.playSequence();
assert.strictEqual(audios[0].currentTime,0);
assert.deepStrictEqual(plays,[0]);
ctx.stopSequence();
''')

    def test_stop_removes_pending_canplay_listeners(self):
        self.run_js(r'''
let ready=null;
audios[0].readyState=0;audios[0].networkState=1;
audios[0].addEventListener=(name,fn)=>{if(name==='canplay'){ready=fn;}};
audios[0].removeEventListener=(name,fn)=>{if(name==='canplay'&&ready===fn){ready=null;}};
audios[0].load=()=>{};
await ctx.playSequence();
assert.deepStrictEqual(plays,[]);
assert.strictEqual(typeof ready,'function');
ctx.stopSequence();
assert.strictEqual(ready,null);
assert.strictEqual(playing(),false);
''')

    def test_an_explicit_empty_trim_plan_fails_closed(self):
        self.run_js(r'''
ctx.API.get=async()=>({pauses:[],trims:[]});
await ctx.playSequence();
assert.deepStrictEqual(plays,[]);
assert.strictEqual(playing(),false);
assert.match(toasts.at(-1).text,/3 chunk\(s\) skipped/);
''')

    def test_missing_source_and_undefined_play_promise_keep_normal_sequence_order(self):
        self.run_js(r'''
audios[0].src='';audios[1].play=()=>{plays.push(1);return undefined;};
await ctx.playSequence();assert.deepStrictEqual(plays,[1]);audios[1].onended();assert.deepStrictEqual(plays,[1,2]);
attempts[0].resolve();await settle();assert.strictEqual(playing(),true);audios[2].onended();
assert.strictEqual(playing(),false);assert.strictEqual(toasts.length,0);assert.strictEqual(button.onclick,ctx.playSequence);
''')
