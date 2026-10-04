import json
from pathlib import Path
import subprocess
import unittest

class ConfigRefreshReuseTests(unittest.TestCase):
    def test_actual_callback_success_and_independent_retry(self):
        s=(Path(__file__).resolve().parent.parent/'static/js/app-core.js').read_text()
        a=s.index('await saveConfigPayload(config, async () => {')+len('await saveConfigPayload(config, async () => {');body=s[a:s.index('\n                });',a)]
        a=s.index('async function reloadPromptPresets(');helper=s[a:s.index('        window.savePromptPreset',a)]
        js=r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
let finished=false;process.on("beforeExit",()=>assert(finished,"All asynchronous assertions must finish"));
(async()=>{for(const failures of [0,1,2]){
const reads=[],renders=[],warnings=[],toasts=[],fields={};const el=id=>fields[id]||(fields[id]={textContent:'',disabled:false,hidden:true});
const c={config:{llm_mode:'remote'},promptSnapshot:'unchanged',getPromptPresetEditorSnapshot:()=> 'unchanged',document:{getElementById:el},currentLlmMode:'remote',savedLlmMode:'local',currentIsRemote:false,failoverIsRemote:false,activePromptPreset:'old',
API:{get:async p=>{reads.push(p);if(reads.length<=failures){throw Error('known read failure');}return {llm_mode:'remote',is_remote:true,failover_is_remote:true,prompts:{attribution_preset:'custom'},prompt_presets:[{name:'custom'}]};}},
renderActiveLlmModeBadge:()=>{},renderConfigWarnings:x=>warnings.push(x),renderPromptPresets:(p,a)=>renders.push({p,a}),showToast:(text,kind)=>toasts.push({text,kind}),console:{debug:()=>{}}};
vm.createContext(c);vm.runInContext(HELPER,c);await vm.runInContext('(async()=>{'+BODY+'})()',c);
assert.equal(reads.length,1);if(failures){assert.equal(renders.length,0);assert.equal(el('config-refresh-retry').hidden,false);for(let i=0;i<failures;i++){await c.refreshSavedConfigFeedback();}assert.equal(reads.length,failures+1);assert.equal(el('config-refresh-retry').hidden,true);}assert.equal(c.savedLlmMode,'remote');assert.equal(c.currentIsRemote,true);assert.equal(c.failoverIsRemote,true);
assert.equal(warnings.length,1);assert.equal(renders.length,1);assert.equal(c.activePromptPreset,'custom');assert.deepEqual(toasts,[{text:'Configuration Saved!',kind:'success'}]);
if(!failures){assert.deepEqual(JSON.parse(JSON.stringify(renders)),[{p:[{name:'custom'}],a:'custom'}]);await c.reloadPromptPresets('mine');assert.equal(reads.length,2);assert.equal(c.activePromptPreset,'mine');}
}finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'''.replace('HELPER',json.dumps(helper)).replace('BODY',json.dumps(body))
        r=subprocess.run(['node','-e',js],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,r.returncode,r.stderr)
