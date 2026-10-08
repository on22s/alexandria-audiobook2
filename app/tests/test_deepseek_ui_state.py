"""Actual UI handlers reject stale responses and retain independent user drafts."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests import test_chunk_refresh_js as chunks
from tests import test_current_book_narrator_js as books
from tests import test_batch_script_cancel_js as batch
from tests import test_llm_profile_requests_js as profiles
from tests import test_prompt_reset_defaults_js as reset
from tests import test_training_ui_contract as training_ui

# A disposable source copy can verify these guards against the pre-fix code.
if os.environ.get('DEEPSEEK_UI_BASELINE'):
    root = Path(os.environ['DEEPSEEK_UI_BASELINE'])
    chunks.SOURCE = books.SOURCE = batch.SOURCE = training_ui.CORE = root / 'app-core.js'
    books.SCRIPTS = root / 'app-scripts.js'
    reset.actions.SOURCE = root / 'app-core.js'

class UiStateTests(unittest.TestCase):
    def test_regenerated_same_path_audio_is_not_retained_unchanged_audio_is(self):
        chunks.ChunkRefreshJsTests().run_js(r'''
let reply={revision:'v1',full:true,total:1,changed_ids:[1],chunks:[{...chunk(1,'done'),uid:'one',audio_path:'audio.wav',audio_revision:'audio-1'}]};ctx.API.get=async()=>reply;await ctx.loadChunks(true);
let replaced=0,metadata;const player={src:'/audio.wav?t=old',dataset:{id:'1'},paused:false,ended:false,currentTime:8,isConnected:true,setAttribute(){},addEventListener:(event,fn)=>metadata=fn,load:()=>metadata(),play:()=>Promise.resolve(),pause(){}};
ctx.document.querySelectorAll=()=>[player];body.querySelector=()=>({replaceWith:p=>{assert.equal(p,player);replaced++;}});
reply={...reply,revision:'unrelated',chunks:[{...reply.chunks[0],pause_after:500}]};await ctx.loadChunks(true);assert.equal(replaced,1);assert.equal(player.currentTime,8);
for(const change of ['text','revision']){
 replaced=0;reply={...reply,revision:'changed-'+change,chunks:[{...reply.chunks[0],...(change==='text'?{text:'Regenerated text',audio_revision:null}:{audio_revision:'audio-2'})}]};await ctx.loadChunks(true);assert.equal(replaced,0,change);assert(body.innerHTML.includes('audio.wav'));
}
''')

    def test_native_audio_publication_changes_revision_at_the_same_path(self):
        import shutil
        import tempfile
        import soundfile as sf
        from project import ProjectManager
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = ProjectManager(tmp)
            manager.save_chunks([{'id':0, 'uid':'one', 'speaker':'Narrator',
                                  'text':'hello', 'status':'pending', 'audio_path':None}])
            def export(source, name):
                relative = 'voicelines/' + name + '.wav'
                shutil.copyfile(source, root / relative)
                return relative
            manager._export_chunk_audio = export
            source = root / 'sample.wav'
            snapshots = []
            for amplitude in (.1, .2):
                sf.write(source, [amplitude] * 2400, 24000)
                manager._publish_generated_chunk_audio(manager.load_chunks()[0], str(source), 'Narrator')
                snapshots.append(manager.load_chunks()[0])
            self.assertEqual(snapshots[0]['audio_path'], snapshots[1]['audio_path'])
            self.assertNotEqual(snapshots[0]['audio_revision'], snapshots[1]['audio_revision'])
            self.assertEqual(snapshots[1]['audio_revision'], ProjectManager(tmp).load_chunks()[0]['audio_revision'])
            pcm, rate = sf.read(root / snapshots[1]['audio_path'])
            self.assertEqual(24000, rate)
            self.assertAlmostEqual(.2, float(pcm[0]), places=4)

    def test_file_selections_serialize_backend_and_ignore_stale_success_failure(self):
        books.CurrentBookNarratorJsTests().run_js(r'''
for(const fail of [false,true]){
const pending=[];const turn=()=>new Promise(resolve=>setImmediate(resolve));ctx.API.upload=file=>new Promise((resolve,reject)=>pending.push({file,resolve,reject}));
const A={name:'A.txt'},B={name:'B.txt'};el('file-upload').files=[A];const a=handlers['file-upload:change']();await turn();el('file-upload').files=[B];const b=handlers['file-upload:change']();await turn();assert.equal(pending.length,1,'serialize backend selection');
if(fail){pending[0].reject(Error('stale A error'));}else{pending[0].resolve({stored_filename:'A.txt'});}await a;await turn();assert.equal(pending.length,2);assert(!el('upload-status').innerHTML.includes('stale A error'));pending[1].resolve({stored_filename:'B.txt'});await b;assert.equal(run('currentBookFilename'),'B.txt');assert(el('upload-status').innerHTML.includes('B.txt'));
}
// Existing and saved-book operations join the same backend selection queue.
const pending=[];const turn=()=>new Promise(resolve=>setImmediate(resolve));ctx.API.upload=file=>new Promise(resolve=>pending.push(resolve));
el('file-upload').files=[{name:'old.txt'}];const uploading=handlers['file-upload:change']();await turn();
el('existing-upload-select').value='selected.txt';selectedFilename='selected.txt';const selecting=ctx.selectExistingScriptUpload();await turn();pending[0]({stored_filename:'old.txt'});await Promise.all([uploading,selecting]);assert.equal(run('currentBookFilename'),'selected.txt');
const a=scripts.indexOf('async function loadScript(name)'),b=scripts.indexOf('async function deleteScript(name)',a);run(scripts.slice(a,b));ctx.API.post=async()=>({name:'saved'});el('file-upload').files=[{name:'other.txt'}];const old=handlers['file-upload:change']();await turn();const saved=ctx.loadScript('saved');await turn();pending[1]({stored_filename:'other.txt'});await Promise.all([old,saved]);assert.equal(run('currentBookFilename'),'saved.json');
''')

    def test_queue_changes_during_each_preparation_phase_require_fresh_start(self):
        batch.BatchScriptCancelJsTests().run_js(r'''
for(const phase of ['upload','preflight','confirm']){
 const c=client(phase),pending=c.ctx._startBatchScript();await turn();vm.runInContext('scriptBatchQueue=[{file:{name:"two.txt"}}]',c.ctx);c.setPhase('normal');c.gate.resolve(true);await pending;
 assert.equal(c.requests.filter(x=>x.url.endsWith('/start')).length,0,phase);assert(c.toasts.some(x=>x.message.includes('selection changed')));assert.equal(c.el('btn-gen-script').disabled,false);
 await c.ctx._startBatchScript();const started=c.requests.find(x=>x.url.endsWith('/start'));assert(started);assert.equal(started.data.tasks[0].filename,'two.txt');
}
''')

    def test_connection_results_are_bound_to_current_profile_and_request(self):
        profiles.LlmProfileRequestJsTests().run_js(r'''
for(const change of ['mode','url','model','bad']){
 const gate=deferred();ctx.API.post=()=>gate.promise;const pending=ctx.testLlmConnection();
 if(change==='mode'){ctx.currentLlmMode='remote';}else if(change==='url'){element('llm-url').value='http://new.invalid/v1';}else if(change==='model'){element('llm-model').value='new';}else{element('llm-request-timeout').value='bad';}
 const before=element('llm-test-result').innerHTML;gate.resolve({ok:true,reply:'OLD',base_url:'old',model:'old'});await pending;assert.equal(element('llm-test-result').innerHTML,before);assert(!String(element('auto-config-msg').innerHTML).includes('OLD'));assert.equal(element('llm-test-btn').disabled,false);element('llm-request-timeout').value='500';
}
const first=deferred(),second=deferred();let n=0;ctx.API.post=()=>++n===1?first.promise:second.promise;const old=ctx.testLlmConnection();const current=ctx.testLlmConnection();first.reject(Error('old failure'));await old;assert.equal(element('llm-test-btn').disabled,true);second.resolve({ok:false,step:'new',error:'current failure'});await current;assert(element('llm-test-result').innerHTML.includes('current failure'));assert.equal(element('llm-test-btn').disabled,false);
''')

    def test_reset_selects_default_identity_and_preserves_user_presets(self):
        app=FastAPI();app.include_router(reset.system.router)
        with TestClient(app) as client: defaults=client.get('/api/default_prompts').json()
        code='const fields='+json.dumps(reset.FIELDS)+';const defaults='+json.dumps(defaults)+';const builtins='+json.dumps(reset.builtin_presets())+';'+reset.SETUP+r'''
for(const [id,value] of Object.entries({'tts-mode':'local','tts-device':'auto','tts-language':'English'})){element(id).value=value;}
const owned={name:'mine',system_prompt:'mine S',user_prompt:'mine U',builtin:false};
for(const selected of ['michel2_full','mine']){
 run('renderPromptPresets('+JSON.stringify([...builtins,owned])+','+JSON.stringify(selected)+');');const before=run('JSON.stringify(promptPresets)');context.API.get=async()=>defaults;await context.window.resetPrompts();assert.equal(run('activePromptPreset'),'default');assert.equal(run('JSON.stringify(promptPresets)'),before);const payload=context.buildConfigPayload(2);assert.equal(payload.prompts.attribution_preset,'default');assert.equal(payload.prompts.user_prompt,builtins.find(p=>p.name==='default').user_prompt);
}
console.log(JSON.stringify(context.buildConfigPayload(2)));
'''
        reset.actions.PromptPresetTransactionTests().run_case(code)

    def test_new_manual_draft_retained_after_acknowledgement_unchanged_answer_not(self):
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        if os.environ.get('DEEPSEEK_UI_BASELINE'): source=Path(os.environ['DEEPSEEK_UI_BASELINE'])/'app-core.js'
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');
for(const edit of [false,true]){
 const elements={};function el(id){return elements[id]||(elements[id]={value:'',textContent:'',innerHTML:'',children:[],hidden:false,disabled:false,appendChild(x){this.children.push(x);}});}
 let full={id:'A',sequence:1,messages:[]};const c={window:null,console,document:{getElementById:el,createElement:()=>({children:[],setAttribute(){},append(...x){this.children.push(...x);}})},API:{get:async()=>({pending:full}),post:async()=>({})},showToast(){},notifyJobDone(){}};c.window=c;vm.createContext(c);vm.runInContext(s.slice(s.indexOf('let _manualShown ='),s.indexOf('async function pollLogs(',s.indexOf('let _manualShown ='))),c);
 await c.renderManualRequest({running:true,manual_request:full},'script');el('manual-llm-reply').value='submitted A';await c.submitManualReply();await c.renderManualRequest({running:false},'script');if(edit){el('manual-llm-reply').value='NEW UNSENT DRAFT';}full={id:'B',sequence:2,messages:[]};await c.renderManualRequest({running:true,manual_request:full},'script');assert.equal(el('manual-llm-retained-replies').children.length,edit?1:0);if(edit){assert.equal(el('manual-llm-retained-replies').children[0].children[1].value,'NEW UNSENT DRAFT');}
}
'''
        result=subprocess.run(['node','-e','(async()=>{'+script+'})().catch(e=>{console.error(e);process.exitCode=1;});',str(source)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
