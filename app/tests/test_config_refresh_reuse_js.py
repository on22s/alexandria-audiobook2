import json
from pathlib import Path
import subprocess
import unittest

class ConfigRefreshReuseTests(unittest.TestCase):
    def test_actual_callback_success_and_independent_retry(self):
        s=(Path(__file__).resolve().parent.parent/'static/js/app-core.js').read_text()
        a=s.index('await saveConfigPayload(config, async () => {')+len('await saveConfigPayload(config, async () => {');body=s[a:s.index('\n                });',a)]
        a=s.index('async function reloadPromptPresets(');helper=s[a:s.index('\n        }',a)+len('\n        }')]
        js=r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
let finished=false;process.on("beforeExit",()=>assert(finished,"All asynchronous assertions must finish"));
(async()=>{for(const failures of [0,1,2]){
const reads=[],renders=[],warnings=[],toasts=[];
const c={currentLlmMode:'remote',savedLlmMode:'local',currentIsRemote:false,failoverIsRemote:false,activePromptPreset:'old',
API:{get:async p=>{reads.push(p);if(reads.length<=failures){throw Error('known read failure');}return {is_remote:true,failover_is_remote:true,prompts:{attribution_preset:'custom'},prompt_presets:[{name:'custom'}]};}},
renderActiveLlmModeBadge:()=>{},renderConfigWarnings:x=>warnings.push(x),renderPromptPresets:(p,a)=>renders.push({p,a}),showToast:(text,kind)=>toasts.push({text,kind}),console:{debug:()=>{}}};
vm.createContext(c);vm.runInContext(HELPER,c);await vm.runInContext('(async()=>{'+BODY+'})()',c);
assert.equal(reads.length,failures?2:1);assert.equal(c.savedLlmMode,'remote');assert.equal(c.currentIsRemote,failures===0);assert.equal(c.failoverIsRemote,failures===0);
assert.equal(warnings.length,failures?0:1);assert.equal(renders.length,failures===2?0:1);assert.equal(c.activePromptPreset,failures===2?'old':'custom');assert.deepEqual(toasts,[{text:'Configuration Saved!',kind:'success'}]);
if(!failures){assert.deepEqual(JSON.parse(JSON.stringify(renders)),[{p:[{name:'custom'}],a:'custom'}]);await c.reloadPromptPresets('mine');assert.equal(reads.length,2);assert.equal(c.activePromptPreset,'mine');}
}finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'''.replace('HELPER',json.dumps(helper)).replace('BODY',json.dumps(body))
        r=subprocess.run(['node','-e',js],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,r.returncode,r.stderr)
