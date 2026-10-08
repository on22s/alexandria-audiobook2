"""Execute actual UI handlers, including retries and stale project responses."""
from pathlib import Path
import subprocess
import unittest

STATIC = Path(__file__).resolve().parent.parent / 'static/js'


class ImprovementUiErrorTests(unittest.TestCase):
    def test_config_retry_confirms_draft_replacement_and_preserves_pending_edits(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(process.argv[1],'app-core.js'),'utf8');
const fields={},decisions=[],requests=[];const el=id=>fields[id]??={id,value:'initial',checked:false,style:{}};
const ids=['llm-url','llm-model','tts-url','parallel-workers'];el('config-form').elements=ids.map(el);
const ctx={document:{getElementById:el},console:{error(){}},llmProfiles:{},passPromptDefaults:{pass1:{system_prompt:'default'},pass3:{system_prompt:'default'}},API:{get:()=>new Promise((resolve,reject)=>requests.push({resolve,reject}))},showConfirm:()=>new Promise(resolve=>decisions.push(resolve))};
for(const name of ['renderConfigWarnings','applyPauseSupport','renderActiveLlmModeBadge','onLlmModeChange','toggleSubBatchFields','toggleTTSMode','renderPromptPresets','renderPassPromptPresets','applyCurrentBookFilename']){ctx[name]=()=>{};}
ctx.populateLlmInputs=()=>{el('llm-url').value=ctx.llmProfiles.local.base_url;el('llm-model').value=ctx.llmProfiles.local.model_name;};
for(const id of ['review-system-prompt','review-user-prompt','persona-system-prompt','persona-user-prompt']){el(id).value='filled';}
vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('async function loadConfig()'),source.indexOf('// Reset prompts and generation settings to factory defaults')),ctx);
const stored={llm:{base_url:'stored-url',model_name:'stored-model'},tts:{url:'stored-tts',parallel_workers:2}};
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{
const first=ctx.loadConfig();requests.at(-1).reject(Error('initial failure'));await first;
for(const id of ids){el(id).value='draft-'+id;}
const cancelled=ctx.loadConfig();assert.strictEqual(decisions.length,1);assert.strictEqual(requests.length,1);decisions.at(-1)(false);await cancelled;assert.strictEqual(el('parallel-workers').value,'draft-parallel-workers');
const changed=ctx.loadConfig();el('tts-url').value='later edit';decisions.at(-1)(true);await changed;assert.strictEqual(requests.length,1);assert.strictEqual(el('tts-url').value,'later edit');
const approved=ctx.loadConfig();decisions.at(-1)(true);await new Promise(resolve=>setImmediate(resolve));requests.at(-1).resolve(stored);await approved;assert.strictEqual(el('tts-url').value,'stored-tts');assert.strictEqual(el('parallel-workers').value,2);assert.strictEqual(el('llm-url').value,'stored-url');
const pending=ctx.loadConfig();el('llm-model').value='edited during fetch';requests.at(-1).resolve(stored);await pending;assert.strictEqual(el('llm-model').value,'edited during fetch');assert(el('config-warning-msg').textContent.includes('kept'));
finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});''')

    def run_js(self, program):
        result = subprocess.run(['node','-e',program,str(STATIC)], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_config_failure_is_visible_preserves_inputs_and_success_clears_retry(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(process.argv[1],'app-core.js'),'utf8');
const fields={},errors=[];let fail=true;
const element=id=>fields[id]||(fields[id]={value:'unsaved input',innerHTML:'',textContent:'',style:{},checked:false});
const ctx={legacyChunkSize:123,console:{error:(...args)=>errors.push(args),warn:()=>{}},document:{getElementById:element},llmProfiles:{},
 passPromptDefaults:{pass1:{system_prompt:'preset',user_prompt:'preset'},pass3:{system_prompt:'preset',user_prompt:'preset'}},
 API:{get:async path=>{assert.strictEqual(path,'/api/config');if(fail){throw Error('HTTP 503 <img src=x>');}return {tts:{mode:'local'}};}}};
for(const name of ['applyPauseSupport','renderActiveLlmModeBadge','populateLlmInputs','onLlmModeChange','toggleSubBatchFields','toggleTTSMode','renderPromptPresets','renderPassPromptPresets','applyCurrentBookFilename']){ctx[name]=()=>{};}
vm.createContext(ctx);{const guidanceCore=typeof core==='string'?core:source;vm.runInContext(guidanceCore.slice(guidanceCore.indexOf('function showActionError('),guidanceCore.indexOf('function showConfirm(')),ctx);}
vm.runInContext(source.slice(source.indexOf('function renderConfigWarnings('),source.indexOf('function populateLlmInputs(')),ctx);
vm.runInContext(source.slice(source.indexOf('async function loadConfig()'),source.indexOf('// Reset prompts and generation settings to factory defaults')),ctx);
(async()=>{
 await ctx.loadConfig();
 assert(element('config-warning-msg').textContent.includes('HTTP 503 <img src=x>'));
 assert.strictEqual(element('config-warning-msg').innerHTML,'');
 assert.strictEqual(element('config-warning-banner').style.display,'');
 assert.strictEqual(element('config-load-retry').style.display,'');
 assert.strictEqual(element('max-tokens').value,'unsaved input');assert.strictEqual(ctx.legacyChunkSize,123);
 fail=false;await ctx.loadConfig();
 assert.strictEqual(element('config-load-retry').style.display,'none');
 assert.strictEqual(element('config-warning-banner').style.display,'none');
 assert.strictEqual(element('tts-mode').value,'local');assert.strictEqual(errors.length,1);
 ctx.renderConfigWarnings({config_warnings:[{field:'tts.mode',message:'Invalid mode'}]});
 assert(element('config-warning-msg').textContent.includes('Invalid mode'));
 assert.strictEqual(element('config-warning-banner').style.display,'');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_sample_error_is_escaped_visible_cleared_on_retry_and_not_applied_to_other_project(self):
        self.run_js(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const core=fs.readFileSync(path.join(process.argv[1],'app-core.js'),'utf8');
const source=fs.readFileSync(path.join(process.argv[1],'app-workbench.js'),'utf8');
const row={text:'Original line.',emotion:'calm',seed:'',status:'pending'},toasts=[];let fail=true,stale=false;
const ctx={window:{},dsbRows:[row],dsbCurrentProject:'fixture',dsbBatchRunning:false,
 document:{getElementById:id=>({value:id==='dsb-description'?'Fixture voice':'-1'})},
 showToast:(...args)=>toasts.push(args),console:{error:()=>{}},dsbRenderTable:()=>{},
 isDatasetProjectLoading:()=>false,isDatasetProjectSelected:()=>!stale,API:{post:async()=>{if(fail){throw Error('<img src=x onerror=boom()> bad reference');}return {audio_url:'/new.wav'};}}};
vm.createContext(ctx);{const guidanceCore=typeof core==='string'?core:source;vm.runInContext(guidanceCore.slice(guidanceCore.indexOf('function showActionError('),guidanceCore.indexOf('function showConfirm(')),ctx);}
vm.runInContext(core.slice(core.indexOf('function escapeHtml('),core.indexOf('// Parse a numeric input')),ctx);
vm.runInContext(source.slice(source.indexOf('function dsbBuildRowHtml('),source.indexOf('function dsbRenderTable(')),ctx);
vm.runInContext(source.slice(source.indexOf('function getDatasetRowDefinition('),source.indexOf('function ensureDatasetRowSaveState(')),ctx);
vm.runInContext(source.slice(source.indexOf('function ensureDatasetRowsEditable('),source.indexOf('function dsbAddRow(')),ctx);
vm.runInContext(source.slice(source.indexOf('window.dsbGenSample ='),source.indexOf('// Batch generation')),ctx);
(async()=>{
 await ctx.window.dsbGenSample(0);
 assert.strictEqual(row.status,'error');assert(row.error.includes('bad reference'));assert.strictEqual(toasts.length,1);
 const html=ctx.dsbBuildRowHtml(row,0);assert(html.includes('&lt;img'));assert(!html.includes('<img src=x'));assert(html.includes('bad reference'));
 fail=false;await ctx.window.dsbGenSample(0);
 assert.strictEqual(row.status,'done');assert.strictEqual(row.error,'');assert.strictEqual(row.audio_url,'/new.wav');
 assert(!ctx.dsbBuildRowHtml(row,0).includes('bad reference'));
 fail=true;stale=true;await ctx.window.dsbGenSample(0);
 assert.strictEqual(toasts.length,1);assert.strictEqual(row.error,'');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
