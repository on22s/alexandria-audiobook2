"""Exercise the real picker with deferred uploads and playback responses."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-audio-channels.js'
SETUP = r'''
context.window=context;context.addEventListener=()=>{};context.removeEventListener=()=>{};
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(v){if(v){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Element.prototype.replaceChildren=function(...nodes){for(const n of [...this.children]){n.remove();}for(const n of nodes){this.appendChild(n);}};
Element.prototype.append=function(...nodes){for(const n of nodes){this.appendChild(n);}};
Element.prototype.pause=function(){this.paused=true;};
const requests=[],removed=[],revoked=[];
context.AbortController=AbortController;context.FormData=FormData;
context.URL={createObjectURL:()=> 'blob:fixture',revokeObjectURL:url=>revoked.push(url)};
context.API={_handleError:async r=>{if(!r.ok){throw Error('fixture refused');}}};
context.fetch=(url,options={})=>{if(options.method==='DELETE'){removed.push(url);return Promise.resolve({ok:true});}let resolve;const promise=new Promise(r=>resolve=r);requests.push({url,options,resolve});return promise;};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
load();const container=new Element('div');body.appendChild(container);const file=new File(['synthetic'],'stereo.wav');
const picker=context.createAudioChannelPicker(container,file,'clone');
const select=container.querySelectorAll('select')[0],button=container.querySelectorAll('button')[0],audio=container.querySelectorAll('audio')[0];select.value='left';
const ok={ok:true,json:async()=>({token:'a'.repeat(32)}),headers:{get:()=> '2'},blob:async()=>new Blob(['audio'])};
'''


class AudioChannelsJsTests(unittest.TestCase):
    def run_js(self, body):
        code = DOM_FIXTURE + SETUP + '\nlet done=false;process.on("beforeExit",()=>assert(done));(async()=>{' + body + '\ndone=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_preview_reuses_stage_and_selection_is_explicit_then_dispose_revokes(self):
        self.run_js(r'''
assert.equal(requests.length,0);assert.equal(picker.getSelection().token,null);
const preview=button.onclick();assert.equal(requests.length,1);assert.equal(requests[0].options.body.get('purpose'),'clone');requests[0].resolve(ok);await flush();assert.equal(requests.length,2);assert(requests[1].url.includes('channel=left'));requests[1].resolve(ok);await preview;assert.equal(audio.hidden,false);assert.equal(picker.getSelection().token,'a'.repeat(32));
select.value='right';select.onchange();assert(audio.paused);assert.equal(audio.hidden,true);assert.deepEqual(revoked,['blob:fixture']);
const second=button.onclick();await flush();assert.equal(requests.length,3);assert(requests[2].url.includes('channel=right'));requests[2].resolve(ok);await second;picker.dispose();await flush();assert.equal(container.children.length,0);assert.equal(removed.length,1);
''')

    def test_closed_or_changed_picker_discards_late_audio_and_cleans_late_upload(self):
        self.run_js(r'''
const pending=button.onclick();picker.dispose();requests[0].resolve(ok);await pending;await flush();assert.equal(requests.length,1);assert.equal(removed.length,1);assert.equal(audio.hidden,true);
const other=context.createAudioChannelPicker(container,file,'preparer');const b=container.querySelectorAll('button')[0],s=container.querySelectorAll('select')[0],a=container.querySelectorAll('audio')[0];s.value='left';const p=b.onclick();requests[1].resolve(ok);await flush();assert.equal(requests.length,3);s.value='right';s.onchange();requests[2].resolve(ok);await p;assert.equal(a.hidden,true);assert.equal(b.disabled,false);assert.equal(other.getSelection().channel,'right');other.dispose();
''')

    def test_submission_lock_survives_late_preview_completion(self):
        self.run_js(r'''
const pending=button.onclick();picker.setDisabled(true);assert.equal(select.disabled,true);requests[0].resolve(ok);await flush();requests[1].resolve(ok);await pending;assert.equal(button.disabled,true);assert.equal(select.disabled,true);assert.equal(picker.getSelection().channel,'left');picker.setDisabled(false);assert.equal(button.disabled,false);assert.equal(select.disabled,false);picker.dispose();
''')
