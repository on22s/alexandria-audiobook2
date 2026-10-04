"""Native acknowledged-save refresh does not discard edits or repeat writes."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'

class ConfigRefreshFeedbackJsTests(unittest.TestCase):
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
