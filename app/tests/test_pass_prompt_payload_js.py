"""Real config payload/pass preset rendering round-trips edited prompt text."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import system
from three_pass_generate import resolve_three_pass_prompt

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-core.js'


class PassPromptPayloadTests(unittest.TestCase):
    def build(self,custom=False,edited=True):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8'),input=JSON.parse(process.argv[2]);
const defaults={pass1:{system_prompt:'default segment system',user_prompt:'default segment user'},pass3:{system_prompt:'default instruct system',user_prompt:'default instruct user'}};
const presets={pass1:[],pass3:[]},active={pass1:'default',pass3:'default'},elements={};
const ctx={document:{getElementById(id){return elements[id]??={value:'',checked:false,replaceChildren(){},appendChild(){}};},createElement(){return {}; }},llmProfiles:{local:{base_url:'http://localhost:1234/v1',api_key:'local',model_name:'fixture'},remote:null},currentLlmMode:'local',legacyChunkSize:3000,promptPresetPayload:()=>({active:'default',own:[]}),selectedPromptPreset:()=>null,passPromptPresets:presets,activePassPromptPreset:active,passPromptDefaults:defaults};
for(const [id,value] of Object.entries({'tts-mode':'local','tts-device':'auto','tts-language':'English'})){ctx.document.getElementById(id).value=value;}
for(const pass of ['pass1','pass3']){
 if(input.custom){active[pass]='mine '+pass;presets[pass]=[{name:active[pass],description:'keep description',system_prompt:'stored system',user_prompt:'stored user'}];}
 const base=input.custom?presets[pass][0]:defaults[pass];
 ctx.document.getElementById(pass+'-system-prompt').value=input.edited?' edited '+pass+' system\n"é"':base.system_prompt;ctx.document.getElementById(pass+'-user-prompt').value=input.edited?'edited '+pass+' user {batch}':base.user_prompt;
}
const before=JSON.stringify({presets,active});vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function getNumFieldValue('),source.indexOf('// --- Desktop notifications ---')),ctx);
vm.runInContext(source.slice(source.indexOf('function passPromptFields('),source.indexOf('window.savePassPromptPreset =')),ctx);
vm.runInContext(source.slice(source.indexOf('function buildConfigPayload('),source.indexOf("document.getElementById('config-form').addEventListener")),ctx);
const payload=ctx.buildConfigPayload(2);assert.strictEqual(JSON.stringify({presets,active}),before,'payload must not mutate UI cache');
for(const pass of ['pass1','pass3']){ctx.renderPassPromptPresets(pass,payload.prompts[pass+'_prompt_presets'],payload.prompts[pass+'_preset']);}
console.log(JSON.stringify({payload,reloaded:{pass1:[elements['pass1-system-prompt'].value,elements['pass1-user-prompt'].value],pass3:[elements['pass3-system-prompt'].value,elements['pass3-user-prompt'].value]}}));
'''
        result=subprocess.run(['node','-e',script,str(SOURCE),json.dumps({'custom':custom,'edited':edited})],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr);return json.loads(result.stdout)

    def test_default_edits_become_presets_custom_edits_preserve_metadata_and_reload(self):
        for custom in [False,True]:
            with self.subTest(custom=custom):
                output=self.build(custom=custom)
                for name in ['pass1','pass3']:
                    expected=[' edited '+name+' system\n"é"','edited '+name+' user {batch}']
                    self.assertEqual(expected,output['reloaded'][name]);self.assertEqual(tuple(expected),resolve_three_pass_prompt(output['payload'],name))
                    preset=output['payload']['prompts'][name+'_prompt_presets'][0]
                    self.assertEqual('mine '+name if custom else 'default (edited)',preset['name'])
                    if custom:self.assertEqual('keep description',preset['description'])
        unchanged=self.build(edited=False)['payload']['prompts'];self.assertEqual('default',unchanged['pass1_preset']);self.assertEqual([],unchanged['pass1_prompt_presets']);self.assertEqual('default',unchanged['pass3_preset']);self.assertEqual([],unchanged['pass3_prompt_presets'])

    def test_actual_config_http_save_persists_prompt_text_used_after_disk_reload(self):
        output=self.build();payload=output['payload'];app=FastAPI();app.include_router(system.router)
        with tempfile.TemporaryDirectory() as root,patch.object(system,'CONFIG_PATH',str(Path(root,'config.json'))),patch.object(system,'project_manager',SimpleNamespace(invalidate_config_cache=lambda:None,engine=None)),TestClient(app) as client:
            response=client.post('/api/config',json=payload);self.assertEqual(200,response.status_code,response.text)
            saved=json.loads(Path(root,'config.json').read_text())
            for name in ['pass1','pass3']:self.assertEqual(tuple(output['reloaded'][name]),resolve_three_pass_prompt(saved,name))
