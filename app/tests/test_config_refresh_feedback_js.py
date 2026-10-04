"""Native acknowledged-save refresh does not discard edits or repeat writes."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'

class ConfigRefreshFeedbackJsTests(unittest.TestCase):
    def test_confirmed_save_with_failed_ui_callback_and_rejected_save(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');
const buttons=[{disabled:false,innerHTML:'Save'},{disabled:true,innerHTML:'Save'}],status={textContent:''};
const fields={'config-form':{querySelectorAll:()=>buttons},'config-save-button':buttons[0],'config-save-button-top':buttons[1],'config-save-status':status,'toast-container':{insertAdjacentHTML(){}}};
let writes=0,rejectPost=false,followups=0;const errors=[];
const c={document:{getElementById:id=>fields[id]||{}},console:{debug:(...args)=>errors.push(args)},API:{post:async()=>{if(rejectPost){throw Error('server refused');}writes++;}}};vm.createContext(c);
vm.runInContext('let configSavePending=false;let toastSequence=0;',c);
for(const [start,end] of [['function showToast(', 'function showActionError('],['function escapeHtml(', 'function getInlineStringArgument('],['async function saveConfigPayload(', 'function getPromptEditorBoxes(']]){
 const at=s.indexOf(start);vm.runInContext(s.slice(at,s.indexOf(end,at)),c);
}
let finished=false;process.on('beforeExit',()=>assert(finished,'asynchronous assertions must finish'));
(async()=>{
 // Run the native toast with Bootstrap unavailable: acknowledgement must survive a UI failure.
 await c.saveConfigPayload({},()=>{followups++;c.showToast('Configuration Saved!','success');});
 assert.strictEqual(writes,1);assert.strictEqual(followups,1);
 assert.match(status.textContent,/Configuration saved, but/);assert(!status.textContent.includes('not confirmed'));
 assert.strictEqual(errors.length,1,'keep the UI failure available for debugging');
 assert.strictEqual(buttons[0].disabled,false);assert.strictEqual(buttons[1].disabled,true);
 assert.strictEqual(buttons[0].innerHTML,'Save');assert.strictEqual(buttons[1].innerHTML,'Save');
 rejectPost=true;await assert.rejects(c.saveConfigPayload({},()=>followups++),/server refused/);
 assert.match(status.textContent,/Save was not confirmed/);assert.strictEqual(writes,1);assert.strictEqual(followups,1);
 assert.strictEqual(buttons[0].disabled,false);assert.strictEqual(buttons[1].disabled,true);
 rejectPost=false;await c.saveConfigPayload({},()=>followups++);
 assert.strictEqual(writes,2);assert.strictEqual(followups,2);assert.strictEqual(status.textContent,'Configuration saved.');
 finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_acknowledged_save_refresh_failure_retry_and_editor_race(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');let submit,ack,resolveRead,reads=0,posts=0,rendered=0;
const fields={};const el=id=>fields[id]||(fields[id]={value:'',innerHTML:'Save',textContent:'',hidden:true,disabled:false,querySelectorAll:()=>[],addEventListener:(event,fn)=>{if(id==='config-form'&&event==='submit'){submit=fn;}}});el('parallel-workers').value='2';let editor='original';
const c={document:{getElementById:el},currentLlmMode:'local',savedLlmMode:'remote',currentIsRemote:false,failoverIsRemote:false,renderActiveLlmModeBadge(){el('badge').textContent=c.savedLlmMode;},renderConfigWarnings(){},getPromptPresetEditorSnapshot:()=>editor,renderPromptPresets:()=>{rendered++;editor='server preset';},syncCurrentLlmProfile(){},buildConfigPayload:()=>({llm_mode:c.currentLlmMode}),showToast(){},console:{debug(){}},API:{post:async(path,payload)=>{assert.strictEqual(path,'/api/config');assert.strictEqual(payload.llm_mode,'local');posts++;await new Promise(done=>ack=done);},get:async path=>{assert.strictEqual(path,'/api/config');reads++;return new Promise((done,fail)=>resolveRead={done,fail});}}};vm.createContext(c);vm.runInContext('let configSavePending=false;let activePromptPreset="default";',c);
let a=s.indexOf('async function saveConfigPayload(');vm.runInContext(s.slice(a,s.indexOf('function getPromptEditorBoxes(',a)),c);a=s.indexOf('async function reloadPromptPresets(');vm.runInContext(s.slice(a,s.indexOf('window.savePromptPreset',a)),c);a=s.indexOf("document.getElementById('config-form').addEventListener");vm.runInContext(s.slice(a,s.indexOf('// --- Script Tab ---',a)),c);
const good={llm_mode:'local',is_remote:false,failover_is_remote:true,prompt_presets:[],prompts:{attribution_preset:'default'}};let finished=false;process.on('beforeExit',()=>assert(finished,'save/refresh assertions must finish'));
(async()=>{
 const save=submit({preventDefault(){}});assert.strictEqual(posts,1);editor='typed while POST pending';c.currentLlmMode='remote';ack();for(let i=0;i<8;i++){await Promise.resolve();}assert.strictEqual(c.savedLlmMode,'local','badge describes submitted mode');assert.strictEqual(reads,1);resolveRead.fail(Error('raw transport'));await save;
 assert.strictEqual(el('config-save-status').textContent,'Configuration saved.');assert.match(el('config-refresh-status').textContent,/Configuration saved, but/);assert(!el('config-refresh-status').textContent.includes('raw transport'));assert(!el('config-refresh-retry').hidden);assert(!el('config-refresh-retry').disabled);assert.strictEqual(editor,'typed while POST pending');
 const retry=c.refreshSavedConfigFeedback();assert(el('config-refresh-retry').disabled);const readCount=reads;await c.refreshSavedConfigFeedback();assert.strictEqual(reads,readCount);editor='typed while GET pending';resolveRead.done(good);await retry;assert.strictEqual(posts,1);assert.strictEqual(editor,'typed while GET pending');assert.strictEqual(rendered,0);assert(el('config-refresh-retry').hidden);assert.match(el('config-refresh-status').textContent,/later prompt edits were kept/);assert(c.failoverIsRemote);
 vm.runInContext('savedConfigPromptSnapshot="typed while GET pending";',c);const healthy=c.refreshSavedConfigFeedback();resolveRead.done(good);await healthy;assert.strictEqual(rendered,1);assert.strictEqual(editor,'server preset');assert.strictEqual(el('config-refresh-status').textContent,'');assert.strictEqual(posts,1);
 const invalid=c.refreshSavedConfigFeedback();resolveRead.done({llm_mode:'local',prompt_presets:null});await invalid;assert(!el('config-refresh-retry').hidden);assert.strictEqual(rendered,1);
 const stale=c.refreshSavedConfigFeedback();const staleRead=resolveRead;const latest=c.refreshSavedConfigFeedback(true);const latestRead=resolveRead;staleRead.done({...good,llm_mode:'remote'});await stale;assert(el('config-refresh-retry').disabled,'older response cannot finish newer refresh');assert.strictEqual(c.savedLlmMode,'local');latestRead.done(good);await latest;assert(!el('config-refresh-retry').disabled);assert.strictEqual(c.savedLlmMode,'local');finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
