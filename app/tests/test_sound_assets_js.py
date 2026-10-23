"""Asset-library DOM controls reject stale responses and keep labels literal."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-sound-assets.js'
SETUP = r'''
context.window=context;context.confirm=()=>true;context.AbortController=AbortController;
context.FormData=class{constructor(){this.values=[];}append(...args){this.values.push(args);}};
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children])child.remove();this.textContent='';for(const node of nodes)this.appendChild(node);};
Element.prototype.append=function(...nodes){for(const node of nodes)this.appendChild(node);};
Element.prototype.pause=function(){this.paused=true;};Element.prototype.load=function(){this.loaded=true;};
Element.prototype.play=function(){this.played=true;return Promise.resolve();};
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value)this.setAttribute('disabled','');else this.removeAttribute('disabled');}});
for(const id of ['sound-assets-file','sound-assets-import','sound-assets-cancel','sound-assets-status','sound-assets-list'])body.appendChild(new Element('div',{id}));
const requests=[];
context.fetch=(url,options={})=>new Promise((resolve,reject)=>{
requests.push({url,options,resolve:value=>resolve({json:async()=>value}),reject});
options.signal?.addEventListener('abort',()=>{const error=new Error('aborted');error.name='AbortError';reject(error);});});
context.API={_handleError:async()=>{}};
const sample=(state='ready',id='a')=>({asset_id:id.repeat(32),state,display_name:'Rain <img onerror=bad()>.wav',canonical:state==='ready'?{sha256:'b'.repeat(64),frames:48000,frame_rate:24000,channels:1,sample_width:4}:null});
const tick=()=>new Promise(resolve=>setImmediate(resolve));load();
const api=vm.runInContext('SoundAssets',context),list=document.getElementById('sound-assets-list'),message=document.getElementById('sound-assets-status');
'''


class SoundAssetsJsTests(unittest.TestCase):
    def run_js(self, body):
        script = DOM_FIXTURE + SETUP + '\nlet done=false;process.on("beforeExit",()=>assert(done));(async()=>{' + body + '\ndone=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_newer_refresh_wins_and_labels_remain_literal(self):
        self.run_js(r'''
const old=api.refresh(),current=api.refresh();requests[1].resolve({assets:[sample()]});await current;
requests[0].resolve({assets:[]});await old;
assert.equal(list.children.length,1);assert.equal(list.querySelectorAll('img').length,0);
assert.equal(list.querySelectorAll('strong')[0].textContent,sample().display_name);
''')

    def test_audition_uses_exact_identity_offset_and_refresh_stops_old_audio(self):
        self.run_js(r'''
const ready=api.refresh();requests[0].resolve({assets:[sample()]});await ready;
const start=list.querySelectorAll('input')[0],play=list.querySelectorAll('button')[0],audio=list.querySelectorAll('audio')[0];
assert(audio.hidden);assert.equal(audio.preload,'none');start.value='0.5';play.onclick();
assert(audio.src.includes('start_frame=12000'));assert(audio.src.includes('sha256='+sample().canonical.sha256));assert(audio.played);
start.value='0.29';play.onclick();assert(audio.src.includes('start_frame=6960'));
const prior=audio.src;start.value='2';play.onclick();assert.equal(audio.src,prior);assert(message.textContent.includes('within'));
start.value='-0.000001';play.onclick();assert.equal(audio.src,prior);
const fresh=api.refresh();requests[1].resolve({assets:[]});await fresh;assert(audio.paused);assert(audio.loaded);
audio.onerror();assert.equal(message.textContent,'Asset library refreshed.');
''')

    def test_invalid_or_duplicate_entries_never_render_controls(self):
        self.run_js(r'''
for(const assets of [[sample(),sample()],[{...sample(),canonical:{...sample().canonical,frames:Infinity}}],[{...sample(),asset_id:'../unsafe'}]]){
const request=api.refresh();requests.at(-1).resolve({assets});await request;
assert.equal(list.querySelectorAll('button').length,0);assert(message.textContent.includes('Invalid'));
}
''')

    def test_duplicate_upload_click_and_cancel_preserve_uncertain_outcome_message(self):
        self.run_js(r'''
document.getElementById('sound-assets-file').files=[{name:'rain.wav'}];
const pending=api.importFile();await api.importFile();assert.equal(requests.length,1);
assert(document.getElementById('sound-assets-import').disabled);api.cancel();await tick();
assert.equal(requests.length,2);requests[1].resolve({assets:[sample('incomplete')]});await pending;
assert(message.textContent.includes('Cleanup may still be running'));assert(!document.getElementById('sound-assets-import').disabled);
assert(document.getElementById('sound-assets-cancel').hidden);assert.equal(list.querySelectorAll('audio').length,0);
''')

    def test_cleanup_requires_confirmation_and_targets_only_incomplete_identity(self):
        self.run_js(r'''
const ready=api.refresh();requests[0].resolve({assets:[sample('incomplete')]});await ready;
const cleanup=list.querySelectorAll('button')[0];context.confirm=()=>false;await cleanup.onclick();assert.equal(requests.length,1);
context.confirm=()=>true;const pending=cleanup.onclick();assert.equal(requests[1].url,'/api/sound_design/assets/'+sample().asset_id+'/purge_incomplete');
assert.equal(requests[1].options.method,'POST');requests[1].resolve({});await tick();requests[2].resolve({assets:[]});await pending;
assert(message.textContent.includes('cleaned up'));
''')
