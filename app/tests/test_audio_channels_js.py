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

    def test_preparer_start_consumes_preview_once_and_same_file_can_start_again(self):
        self.run_js(r'''
picker.dispose();await flush();
const workbench=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-workbench.js'),'utf8');
const core=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');
const lookup=document.getElementById;
document.getElementById=id=>{let node=lookup(id);if(!node){node=new Element('input',{id});node.value='';node.checked=false;node.files=[];body.appendChild(node);}return node;};
context.getNumFieldValue=(_id,fallback)=>fallback;context.showToast=()=>{};
context.createTaskLogRenderer=()=>()=>{};context.notifyJobDone=()=>{};
context.getTaskCompletionOutcome=state=>state.status;context.escapeHtml=String;
const polls=[];context._startPolling=(name,fetch,options)=>polls.push({name,options});
vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),context);
const begin=workbench.indexOf('let prepBatchQueue =');
vm.runInContext(workbench.slice(begin,workbench.indexOf('// List/download the dataset ZIPs',begin)),context);
context.loadPreparerOutputs=()=>{};
let staged=0,refuse=false;const consumed=new Set(),accepted=[];
context.fetch=async(url,options={})=>{
 if(options.method==='DELETE'){return ok;}
 if(url==='/api/audio_channels/stage'){staged++;assert.strictEqual(options.body.get('purpose'),'preparer');return {...ok,json:async()=>({token:String(staged).padStart(32,'0')})};}
 if(url.startsWith('/api/audio_channels/preview')){return ok;}
 assert(['/api/preparer/start','/api/preparer/batch/upload_start'].includes(url));
 const fd=options.body,cfg=JSON.parse(fd.get('config_json')),batch=url.includes('/batch/');
 const token=batch?cfg.tasks[0].preview_token:fd.get('preview_token');
 assert.strictEqual(batch?cfg.tasks[0].audio_channel:cfg.audio_channel,'right');
 if(refuse){return {ok:false};}
 if(token){assert(!consumed.has(token),'a moved stage must never be submitted twice');consumed.add(token);}
 else{const upload=fd.get(batch?'audio_files':'audio_file');assert(upload);assert.strictEqual(await upload.text(),await file.text());}
 accepted.push({batch,token});return ok;
};
for(const batch of [false,true]){
 const shell=new Element('div');body.appendChild(shell);
 const owned=context.createAudioChannelPicker(shell,file,'preparer');
 const channel=shell.querySelectorAll('select')[0],preview=shell.querySelectorAll('button')[0];channel.value='right';
 document.getElementById('prep-batch-mode').checked=batch;
 document.getElementById('prep-audio-file').files=[file];
 if(batch){context.ownedPicker=owned;context.ownedFile=file;vm.runInContext('prepBatchQueue=[{audio:ownedFile,picker:ownedPicker}]',context);context.preparerChannelPicker=null;}
 else{context.preparerChannelPicker=owned;}
 await preview.onclick();const token=owned.getSelection().token;assert(token);
 refuse=true;const before=polls.length;await context.startPreparer();assert.strictEqual(polls.length,before);assert.strictEqual(owned.getSelection().token,token);
 refuse=false;await context.startPreparer();assert.strictEqual(accepted.at(-1).token,token);
 assert.strictEqual(owned.getSelection().token,null);assert.strictEqual(owned.getSelection().channel,'right');
 polls.at(-1).options.onDone({running:false,status:'cancelled',logs:[]});
 await context.startPreparer();assert(!accepted.at(-1).token);assert.strictEqual(document.getElementById('prep-audio-file').files[0],file);
 polls.at(-1).options.onDone({running:false,status:'done',logs:[]});
 await preview.onclick();const fresh=owned.getSelection().token;assert(fresh&&fresh!==token);
 owned.consumeStage(token);assert.strictEqual(owned.getSelection().token,fresh,'stale acknowledgement cannot clear a new preview');
 owned.dispose();await flush();
}
assert.strictEqual(accepted.length,4);assert.strictEqual(staged,4);
''')
