"""Real profile reader/diagnostics and model picker reject stale asynchronous replies."""
import json
from pathlib import Path
import subprocess
import unittest
from tests import test_training_ui_contract as ui

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
SETUP=r'''
const fields={'llm-url':'http://localhost:1234/v1','llm-key':'fixture','llm-model':'chosen','llm-request-timeout':'500','llm-connect-timeout':'3','llm-request-interval':'0','llm-api-retry-limit':'2','llm-on-api-exhaustion':'pause','llm-retry-initial-delay':'1','llm-retry-multiplier':'2','llm-retry-max-delay':'15','llm-retry-jitter':'0.2','llm-provider-headers':'{"X-Route":"blue"}','llm-provider-extra-body':'{"routing":{"group":"blue"}}','llm-reasoning-effort':'low','llm-on-this-gpu':'false','llm-transport':'http'};
for(const [id,value] of Object.entries(fields)){element(id).value=value;}
ctx.currentLlmMode='local';ctx.llmProfiles={local:{base_url:'old cached URL'},remote:{base_url:'remote cache'}};
ctx.document.createElement=()=>({});element('llm-model-options').children=[];element('llm-model-options').appendChild=option=>element('llm-model-options').children.push(option);
Object.defineProperty(element('llm-model-options'),'innerHTML',{get:()=>'',set:()=>element('llm-model-options').children=[]});
element('llm-model-refresh').addEventListener=()=>{};element('llm-model').addEventListener=()=>{};
const validationStart=core.indexOf('function getConfigValidationError(');vm.runInContext(core.slice(validationStart,core.indexOf('function getIntListInput(',validationStart)),ctx);
const profileStart=core.indexOf('function populateLlmInputs('),profileEnd=core.indexOf('// Reflects the last-SAVED',profileStart);vm.runInContext(core.slice(profileStart,profileEnd),ctx);
const modelStart=core.indexOf('let llmModelRequestSequence =')>=0?core.indexOf('let llmModelRequestSequence ='):core.indexOf('async function refreshLlmModels()');vm.runInContext(core.slice(modelStart,core.indexOf('// --- Theme ---',modelStart)),ctx);
const testStart=core.indexOf('async function testLlmConnection()'),testEnd=core.indexOf('const generationControlFields =',testStart)>=0?core.indexOf('const generationControlFields =',testStart):core.indexOf('async function loadConfig()',testStart);vm.runInContext(core.slice(testStart,testEnd),ctx);
ctx.API=vm.runInContext('API',ctx);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
'''


class LlmProfileRequestJsTests(unittest.TestCase):
    def run_js(self,code):
        return ui.TrainingUiContractTests().run_js(SETUP+code)

    def test_picker_suppresses_old_success_failure_mode_switch_edit_and_empty_url(self):
        self.run_js(r'''
const first=deferred(),second=deferred();let count=0;ctx.API.post=()=>++count===1?first.promise:second.promise;
const old=ctx.refreshLlmModels();element('llm-url').value='http://localhost:5678/v1';const newer=ctx.refreshLlmModels();second.resolve({models:['new endpoint model']});await newer;first.resolve({models:['old endpoint model']});await old;assert.deepStrictEqual(element('llm-model-options').children.map(x=>x.value),['new endpoint model']);assert.match(element('llm-model-hint').textContent,/1 model/);
for(const change of ['profile','mode','empty']){
 const gate=deferred();ctx.API.post=()=>gate.promise;const pending=ctx.refreshLlmModels();
 if(change==='profile'){element('llm-provider-headers').value='{"X-Route":"other"}';}else if(change==='mode'){ctx.currentLlmMode='remote';}else{element('llm-url').value='';await ctx.refreshLlmModels();}
 const before=element('llm-model-hint').textContent;gate.reject(Error('old rejected request'));await pending;assert.strictEqual(element('llm-model-hint').textContent,before);assert.deepStrictEqual(element('llm-model-options').children.map(x=>x.value),['new endpoint model']);
}
''')

    def test_models_and_connection_test_send_same_complete_edited_profile_without_cache_mutation(self):
        output=self.run_js(r'''
const before=JSON.stringify(ctx.llmProfiles),requests=[];ctx.API.post=async(url,payload)=>{requests.push({url,payload});return url.endsWith('/models')?{models:['chosen']}:{ok:false,step:'controlled probe',error:'fixture'};};
await ctx.refreshLlmModels();await ctx.testLlmConnection();assert.strictEqual(JSON.stringify(ctx.llmProfiles),before);assert.deepStrictEqual(requests[0].payload,requests[1].payload);assert.strictEqual(requests[0].payload.provider_headers['X-Route'],'blue');assert.deepStrictEqual(JSON.parse(JSON.stringify(requests[0].payload.provider_extra_body)),{routing:{group:'blue'}});assert.strictEqual(requests[1].payload.request_timeout_seconds,500);assert.strictEqual(requests[1].payload.connect_timeout_seconds,3);assert.strictEqual(requests[1].payload.api_retry_limit,2);assert.strictEqual(requests[1].payload.on_api_exhaustion,'pause');assert.strictEqual(requests[1].payload.transport,'http');assert.strictEqual(element('llm-test-btn').disabled,false);
element('llm-transport').value='manual';await ctx.testLlmConnection();assert.strictEqual(requests[2].payload.transport,'manual');ctx.syncCurrentLlmProfile();assert.deepStrictEqual(ctx.llmProfiles.local,requests[2].payload);console.log(JSON.stringify(requests[0].payload));
''')
        data=json.loads(output);self.assertEqual('low',data['reasoning_effort']);self.assertFalse(data['on_this_gpu'])

    def test_invalid_profile_json_is_visible_and_sends_no_diagnostic_request(self):
        self.run_js(r'''
let requests=0;ctx.API.post=async()=>{requests++;};element('llm-provider-headers').value='invalid JSON';await ctx.refreshLlmModels();await ctx.testLlmConnection();assert.strictEqual(requests,0);assert.match(element('llm-model-hint').textContent,/valid JSON/);assert.match(element('llm-test-result').textContent,/valid JSON/);assert.strictEqual(element('llm-test-btn').disabled,false);
''')
