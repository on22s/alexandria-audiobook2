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
const context={window:{prompt:(_question,defaultValue)=>_question==='Preset name:'?'new':'description',confirm:()=>true},document:{getElementById:element,createElement:()=>({appendChild(){},childElementCount:1})},showToast:(...args)=>toasts.push(args),console,
 API:{post:async(url,payload)=>{posts.push(JSON.parse(JSON.stringify(payload)));if(mode==='fail'){throw Error('HTTP 500 fixture');}if(mode==='hold'){arrive();await new Promise(resolve=>release=resolve);}persisted=JSON.parse(JSON.stringify(payload));},get:async()=>persisted}};
context.window={...context.window};vm.createContext(context);const run=code=>vm.runInContext(code,context);
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
