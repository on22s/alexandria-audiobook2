"""Actual preset actions retain cache/text on failed or pending config writes."""
from pathlib import Path
import json
import subprocess
import unittest

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
HARNESS=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const elements={},toasts=[],posts=[];let persisted=null,mode='fail',release,arrive;const entered=new Promise(resolve=>arrive=resolve);
const element=id=>elements[id]??={value:'',checked:false,style:{},replaceChildren(){},appendChild(){},classList:{},addEventListener(){}};
const context={window:{prompt:(_question,defaultValue)=>_question==='Preset name:'?'new':'description',confirm:()=>true},document:{getElementById:element,createElement:()=>({appendChild(){},childElementCount:1})},showToast:(...args)=>toasts.push(args),showConfirm:async()=>true,showPresetEditor:async()=>({name:'new',description:'description'}),console,
 API:{post:async(url,payload)=>{posts.push(JSON.parse(JSON.stringify(payload)));if(mode==='fail'){throw Error('HTTP 500 fixture');}if(mode==='hold'){arrive();await new Promise(resolve=>release=resolve);}persisted=JSON.parse(JSON.stringify(payload));},get:async()=>persisted}};
context.window={...context.window};vm.createContext(context);const run=code=>vm.runInContext(code,context);
run(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')));
run(source.slice(source.indexOf('let promptPresets ='),source.indexOf('window.previewAttributionPrompt =')));
run(source.slice(source.indexOf('function getNumFieldValue('),source.indexOf('// --- Desktop notifications ---')));
run(source.slice(source.indexOf('function buildConfigPayload('),source.indexOf("document.getElementById('config-form').addEventListener")));
context.llmProfiles={local:{base_url:'http://localhost:1/v1',api_key:'local',model_name:'fixture'},remote:null};context.currentLlmMode='local';context.legacyChunkSize=3000;
function seed(){
 run("promptPresets=[{name:'michel2_full',builtin:true,system_prompt:'builtin S',user_prompt:'builtin U'},{name:'mine',system_prompt:'mine S',user_prompt:'mine U'}];renderPromptPresets(promptPresets,'mine');");
 for(const pass of ['pass1','pass3']){run(`passPromptDefaults.${pass}={system_prompt:'default S',user_prompt:'default U'};renderPassPromptPresets('${pass}',[{name:'mine',system_prompt:'mine S',user_prompt:'mine U'}],'mine');`);}
}
function snapshot(){return run('JSON.stringify({promptPresets,activePromptPreset,passPromptPresets,activePassPromptPreset})');}
(async()=>{
'''


class PromptPresetTransactionTests(unittest.TestCase):
    def test_user_switch_preserves_edits_until_unchanged_approval(self):
        self.run_case(r"""
let completed=false;process.on('beforeExit',()=>assert(completed,'switch assertions must finish'));
for(const pass of ['attribution','pass1','pass3']){
 for(const outcome of ['cancel','approve','editor','cache','defaults','error']){
  if(pass==='attribution'&&outcome==='defaults'){continue;}
  seed();const attribution=pass==='attribution',select=element(attribution?'prompt-preset-select':pass+'-prompt-preset-select'),field=element(attribution?'system-prompt':pass+'-system-prompt');
  const oldValue=select.value,target=attribution?'0':'-1';field.value='Unsaved — 日本語';
  let resolve,reject,decisions=0;context.showConfirm=()=>{decisions++;return new Promise((yes,no)=>{resolve=yes;reject=no;});};
  select.value=target;const switching=select.onchange();assert.strictEqual(field.value,'Unsaved — 日本語');assert.strictEqual(select.value,oldValue);assert.strictEqual(decisions,1);
  select.value=target;await select.onchange();assert.strictEqual(decisions,1,'duplicate cannot queue another decision');assert.strictEqual(select.value,oldValue);
  if(outcome==='editor'){field.value='Later edit';}
  if(outcome==='cache'){run(attribution?'promptPresets.push({name:"new"})':`passPromptPresets.${pass}.push({name:'new'})`);}
  if(outcome==='defaults'){run(`passPromptDefaults.${pass==='attribution'?'pass1':pass}.system_prompt='Changed default'`);}
  if(outcome==='error'){reject(Error('confirmation failed'));}else{resolve(outcome!=='cancel');}
  await switching;
  assert.strictEqual(field.value,outcome==='approve'?(attribution?'builtin S':'default S'):outcome==='editor'?'Later edit':'Unsaved — 日本語');
  assert.strictEqual(select.value,outcome==='approve'?target:oldValue);
 }
 seed();let decisions=0;context.showConfirm=async()=>{decisions++;return true;};const select=element(pass==='attribution'?'prompt-preset-select':pass+'-prompt-preset-select');select.value=pass==='attribution'?'0':'-1';await select.onchange();assert.strictEqual(decisions,0,'clean changes need no discard decision');
}
seed();element('system-prompt').value='Kept during save';run('configSavePending=true;');element('prompt-preset-select').value='0';await element('prompt-preset-select').onchange();assert.strictEqual(element('system-prompt').value,'Kept during save');assert.strictEqual(element('prompt-preset-select').value,'1');run('configSavePending=false;');
seed();element('system-prompt').value='Edited builtin';run("renderPromptPresets(promptPresets,'michel2_full');");element('system-prompt').value='Edited builtin';const payload=context.promptPresetPayload();assert.strictEqual(payload.active,'michel2_full (edited)');assert.strictEqual(payload.own.find(p=>p.name===payload.active).system_prompt,'Edited builtin');
assert.strictEqual(posts.length,0);completed=true;
""")

    def test_save_dialog_cancel_or_changed_prompt_cannot_write(self):
        self.run_case(r'''
mode='success';for(const pass of ['attribution','pass1','pass3']){for(const change of ['cancel','editor','selection','cache']){
seed();let answer,options;context.showPresetEditor=value=>{options=value;return new Promise(resolve=>answer=resolve);};const count=posts.length;const save=pass==='attribution'?context.window.savePromptPreset():context.window.savePassPromptPreset(pass);assert(options.title.includes(pass));assert.strictEqual(options.name,'mine');
if(change==='editor'){element(pass==='attribution'?'system-prompt':pass+'-system-prompt').value='later edit';}else if(change==='selection'){if(pass==='attribution'){element('prompt-preset-select').value='0';run('applyPromptPreset(0)');}else{run(`applyPassPromptPreset('${pass}',-1)`);}}else if(change==='cache'){run(pass==='attribution'?'promptPresets.push({name:"added"})':`passPromptPresets.${pass}.push({name:'added'})`);}
const after=snapshot();answer(change==='cancel'?null:{name:'new',description:'description'});await save;assert.strictEqual(posts.length,count);assert.strictEqual(snapshot(),after);if(change==='editor'){assert.strictEqual(element(pass==='attribution'?'system-prompt':pass+'-system-prompt').value,'later edit');}
}}
seed();context.showPresetEditor=async options=>{assert(options.validateName('michel2_full').includes('built-in'));assert.strictEqual(options.validateName('mine'),'');return null;};await context.window.savePromptPreset();
''')

    def test_delete_confirmation_cancellation_and_stale_editor_do_not_write(self):
        self.run_case(r'''
mode='success';for(const pass of ['attribution','pass1','pass3']){for(const change of ['cancel','editor','selection','cache']){
seed();let answer;const messages=[];context.showConfirm=message=>{messages.push(message);return new Promise(resolve=>answer=resolve);};const remove=()=>pass==='attribution'?context.window.deletePromptPreset():context.window.deletePassPromptPreset(pass);const count=posts.length;const pending=remove();await remove();assert.strictEqual(messages.length,1);assert(messages[0].includes('mine'));assert(messages[0].includes(pass==='attribution'?'michel2_full':'checked-in default'));
if(change==='editor'){element(pass==='attribution'?'system-prompt':pass+'-system-prompt').value='later edit';}else if(change==='selection'){if(pass==='attribution'){element('prompt-preset-select').value='0';run('applyPromptPreset(0)');}else{run(`applyPassPromptPreset('${pass}',-1)`);}}else if(change==='cache'){run(pass==='attribution'?'promptPresets.push({name:"added"})':`passPromptPresets.${pass}.push({name:'added'})`);}
const after=snapshot();answer(change!=='cancel');await pending;assert.strictEqual(posts.length,count);assert.strictEqual(snapshot(),after);if(change==='editor'){assert.strictEqual(element(pass==='attribution'?'system-prompt':pass+'-system-prompt').value,'later edit');}
}}
context.showConfirm=async()=>true;seed();await context.window.deletePassPromptPreset('pass3');assert.strictEqual(run('activePassPromptPreset.pass3'),'default');seed();await context.window.deletePromptPreset();assert.strictEqual(run('activePromptPreset'),'michel2_full');
''')

    def run_case(self,code):
        result=subprocess.run(['node','-e',HARNESS+code+"\n})().catch(error=>{console.error(error);process.exitCode=1;});",str(SOURCE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
        return json.loads(result.stdout) if result.stdout else None

    def test_all_saves_and_deletes_retain_cache_and_editor_text_on_failure(self):
        self.run_case(r'''
for(const [action,pass] of [['save','attribution'],['delete','attribution'],['save','pass1'],['delete','pass1'],['save','pass3'],['delete','pass3']]){
 seed();const id=pass==='attribution'?'system-prompt':pass+'-system-prompt';element(id).value='unsaved editor text';const before=snapshot(),errors=toasts.length;
 if(pass==='attribution'){await context.window[action==='save'?'savePromptPreset':'deletePromptPreset']();}else{await context.window[action==='save'?'savePassPromptPreset':'deletePassPromptPreset'](pass);}
 assert.strictEqual(snapshot(),before,action+' '+pass+' cannot publish rejected cache mutation');assert.strictEqual(element(id).value,'unsaved editor text');assert.strictEqual(toasts.length,errors+1);assert.strictEqual(toasts.at(-1)[1],'error');assert.strictEqual(persisted,null);
 const next=context.buildConfigPayload(2);assert(!next.prompt_presets.some(p=>p.name==='new'));assert(!next.prompts.pass1_prompt_presets.some(p=>p.name==='new'));assert(!next.prompts.pass3_prompt_presets.some(p=>p.name==='new'));
}
''')

    def test_ack_commits_cache_pending_edits_survive_and_concurrent_writes_are_rejected(self):
        self.run_case(r'''
seed();mode='hold';element('pass1-system-prompt').value='submitted';const before=snapshot();const saving=context.window.savePassPromptPreset('pass1');await entered;assert.strictEqual(snapshot(),before);element('pass1-system-prompt').value='typed while waiting';
await context.window.deletePassPromptPreset('pass3');await assert.rejects(context.saveConfigPayload(context.buildConfigPayload(2)),/Wait/);assert.strictEqual(posts.length,1);assert.strictEqual(snapshot(),before);
release();await saving;assert.strictEqual(run('activePassPromptPreset.pass1'),'new');assert.strictEqual(run('passPromptPresets.pass1.find(p=>p.name==="new").system_prompt'),'submitted');assert.strictEqual(element('pass1-system-prompt').value,'typed while waiting');assert.strictEqual(persisted.prompts.pass1_preset,'new');assert.strictEqual(run('configSavePending'),false);
mode='success';await context.window.deletePassPromptPreset('pass1');assert.strictEqual(run('activePassPromptPreset.pass1'),'default');assert(!run('passPromptPresets.pass1.some(p=>p.name==="new")'));assert.strictEqual(persisted.prompts.pass1_preset,'default');
// A pending ordinary config refresh owns the same writer until its acknowledgement work ends.
let done,ready;const reached=new Promise(resolve=>ready=resolve);const ordinary=context.saveConfigPayload(context.buildConfigPayload(2),async()=>{ready();await new Promise(resolve=>done=resolve);});await reached;const count=posts.length;await context.window.savePromptPreset();assert.strictEqual(posts.length,count);done();await ordinary;assert.strictEqual(run('configSavePending'),false);
''')

    def test_save_controls_status_and_duplicate_submit_follow_shared_lock(self):
        self.run_case(r'''
seed();const save=element('config-save-button');save.innerHTML='<i></i>Save Configuration';save.disabled=false;
const top=element('config-save-button-top');top.innerHTML='Top Save Configuration';top.disabled=false;const preset={disabled:false},builtinDelete={disabled:true};element('config-form').querySelectorAll=selector=>{assert.strictEqual(selector,'[data-config-save-action]');return [save,top,preset,builtinDelete];};
let submit; element('config-form').addEventListener=(name,handler)=>{assert.strictEqual(name,'submit');submit=handler;};const a=source.indexOf("document.getElementById('config-form').addEventListener");run(source.slice(a,source.indexOf('// --- Script Tab ---',a)));
context.syncCurrentLlmProfile=()=>{throw Error('duplicate submit must stop before validation');};
mode='hold';let finish,arrived;const refresh=new Promise(resolve=>arrived=resolve);const saving=context.saveConfigPayload(context.buildConfigPayload(2),async()=>{arrived();await new Promise(resolve=>finish=resolve);});await entered;
assert(save.disabled&&top.disabled&&preset.disabled&&builtinDelete.disabled);assert.strictEqual(top.innerHTML,'Saving…');assert.strictEqual(save.innerHTML,'Saving…');assert.strictEqual(element('config-save-status').textContent,'Saving configuration…');
const count=toasts.length;await submit({preventDefault(){}});assert.strictEqual(posts.length,1);assert.strictEqual(toasts.length,count,'second submit is harmless');
release();await refresh;assert(save.disabled,'post-save refresh still owns controls');finish();await saving;
assert(!save.disabled&&!top.disabled&&!preset.disabled);assert.strictEqual(top.innerHTML,'Top Save Configuration');assert(builtinDelete.disabled,'pre-existing disabled action restored');assert.strictEqual(save.innerHTML,'<i></i>Save Configuration');assert.strictEqual(element('config-save-status').textContent,'Configuration saved.');
mode='fail';await assert.rejects(context.saveConfigPayload(context.buildConfigPayload(2)),/HTTP 500/);assert(!save.disabled&&!top.disabled&&!preset.disabled);assert.strictEqual(top.innerHTML,'Top Save Configuration');assert(builtinDelete.disabled);assert.strictEqual(save.innerHTML,'<i></i>Save Configuration');assert.strictEqual(run('configSavePending'),false);assert(element('config-save-status').textContent.includes('Review the error and current saved settings before trying again'));
''')

    def test_actual_node_api_over_http_preserves_state_on_native_write_failure_then_commits(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading,tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import system
        from attribution_prompt_variants import builtin_presets
        app=FastAPI();app.include_router(system.router)
        with tempfile.TemporaryDirectory() as root,patch.object(system,'CONFIG_PATH',str(Path(root,'config.json'))),patch.object(system,'project_manager',SimpleNamespace(invalidate_config_cache=lambda:None,engine=None)),TestClient(app,raise_server_exceptions=False) as client:
            path=Path(root,'config.json');path.write_text('{}');original_bytes=path.read_bytes();requests=[];statuses=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    body=self.rfile.read(int(self.headers['Content-Length']));requests.append(json.loads(body))
                    if len(requests)==1:
                        with patch.object(system,'atomic_json_write',side_effect=OSError('fixture disk write failure')) as write:
                            response=client.post(self.path,content=body,headers={'Content-Type':'application/json'});write.assert_called_once()
                        assert path.read_bytes()==original_bytes
                    else:response=client.post(self.path,content=body,headers={'Content-Type':'application/json'})
                    statuses.append(response.status_code);self.send_response(response.status_code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(response.content)));self.end_headers();self.wfile.write(response.content)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            code="const builtins="+json.dumps(builtin_presets())+";const base="+json.dumps('http://127.0.0.1:'+str(server.server_port))+r''';
seed();run('renderPromptPresets('+JSON.stringify(builtins)+',"michel2_full");');for(const [id,value] of Object.entries({'tts-mode':'local','tts-device':'auto','tts-language':'English'})){element(id).value=value;}
context.fetch=(url,request)=>fetch(base+url,request);run(source.slice(source.indexOf('const API = {'),source.indexOf('// --- Setup Tab ---')));
element('pass3-system-prompt').value='HTTP-persisted instruction';const before=snapshot();await context.window.savePassPromptPreset('pass3');assert.strictEqual(snapshot(),before);assert.strictEqual(element('pass3-system-prompt').value,'HTTP-persisted instruction');assert.strictEqual(toasts.at(-1)[1],'error');
await context.window.savePassPromptPreset('pass3');assert.strictEqual(run('activePassPromptPreset.pass3'),'new',JSON.stringify(toasts));assert.strictEqual(run('passPromptPresets.pass3.find(p=>p.name==="new").system_prompt'),'HTTP-persisted instruction');assert.strictEqual(toasts.at(-1)[1],'success');
'''
            try:self.run_case(code)
            finally:server.shutdown();server.server_close();worker.join(timeout=2)
            self.assertFalse(worker.is_alive());self.assertEqual(2,len(requests));self.assertEqual([500,200],statuses);saved=json.loads(path.read_text());self.assertEqual('new',saved['prompts']['pass3_preset']);self.assertEqual('HTTP-persisted instruction',next(p for p in saved['prompts']['pass3_prompt_presets'] if p['name']=='new')['system_prompt'])

    def test_successful_attribution_save_and_delete_publish_exact_payload_and_selection(self):
        payload=self.run_case(r'''
seed();mode='success';element('system-prompt').value='new S';element('user-prompt').value='new U';await context.window.savePromptPreset();assert.strictEqual(run('activePromptPreset'),'new');assert.strictEqual(persisted.prompts.attribution_preset,'new');assert.strictEqual(persisted.prompt_presets.find(p=>p.name==='new').system_prompt,'new S');assert(!persisted.prompt_presets.some(p=>p.builtin));
await context.window.deletePromptPreset();assert.strictEqual(run('activePromptPreset'),'michel2_full');assert(!run('promptPresets.some(p=>p.name==="new")'));assert.strictEqual(persisted.prompts.attribution_preset,'michel2_full');assert(!persisted.prompt_presets.some(p=>p.name==='new'));console.log(JSON.stringify(persisted));
''')
        self.assertEqual('michel2_full',payload['prompts']['attribution_preset'])
