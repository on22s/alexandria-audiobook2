from tests.test_support import create_test_api_server
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent/'static/js/app-core.js'


class GuardedVoiceUiTests(unittest.TestCase):
    def test_acknowledgement_chain_conflict_discard_and_stale_refresh(self):
        code=r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');const source=fs.readFileSync(process.argv[1],'utf8');
const helper=source.slice(source.indexOf('function createSerializedSaveQueue('),source.indexOf('// --- API Helpers ---'));
const saves=source.slice(source.indexOf('let _voiceStatusClearTimer ='),source.indexOf('// Auto-save on any change inside the voices list'));
const refresh=source.slice(source.indexOf('async function refreshVoiceMetadata()'),source.indexOf('let _voiceResourcesRefreshedAt'));
const revision=n=>String(n).padStart(64,'0'),book='b'.repeat(64);let serverRevision=0,value={ALICE:{description:'first'}},reads=0,holdRead=null,confirm=false;const writes=[],errors=[],status={innerHTML:''};
const context={window:{addEventListener(){},localStorage:{setItem(){},getItem(){return null;},removeItem(){}}},document:{getElementById:id=>id==='voice-save-status'?status:null,querySelectorAll:()=>[{}]},setTimeout:()=>1,clearTimeout(){},console:{error(){}},collectVoiceConfig:()=>value,showToast:error=>errors.push(error),showConfirm:async()=>confirm,API:{get:async url=>{assert.strictEqual(url,'/api/voice_config/snapshot');reads++;const result={revision:revision(serverRevision),book_token:book,config:{},voices:[{name:'ALICE',config:{description:'server-'+serverRevision},persona_pending:false}]};if(holdRead){await new Promise(resolve=>holdRead.resolve=resolve);}return result;},post:async(url,body)=>{assert.strictEqual(url,'/api/voice_config/save');writes.push(JSON.parse(JSON.stringify(body)));if(body.revision!==revision(serverRevision)){const error=new Error('newer server voices');error.status=409;throw error;}serverRevision++;return{book_token:book,revision:revision(serverRevision)};}}};
vm.createContext(context);vm.runInContext(helper+saves+refresh+'function cachedRevision(){return _voiceSaveSnapshot.revision;} function dirty(){return voiceSaveQueue.isDirty();}',context);context.loadVoices=()=>context.refreshVoiceMetadata();
let finished=false;process.on('beforeExit',()=>assert(finished,'guarded UI assertions must finish'));
(async()=>{await context.refreshVoiceMetadata();context.saveVoicesDebounced();await context.flushVoiceSaves();assert.strictEqual(writes[0].revision,revision(0));value={ALICE:{description:'second'}};context.saveVoicesDebounced();await context.flushVoiceSaves();assert.strictEqual(writes[1].revision,revision(1));assert.strictEqual(context.cachedRevision(),revision(2));
serverRevision=3;value={ALICE:{description:'retained conflicting edit'}};context.saveVoicesDebounced();await assert.rejects(context.flushVoiceSaves(),/newer server voices/);assert(context.dirty());assert.strictEqual(serverRevision,3);assert(status.innerHTML.includes('edits retained'));assert(status.innerHTML.includes('Discard edits and reload'));
await context.discardVoiceEditsAndReload();assert(context.dirty(),'declining discard keeps draft');confirm=true;await context.discardVoiceEditsAndReload();assert(!context.dirty());assert.strictEqual(context.cachedRevision(),revision(3));
holdRead={};const before=context.window._voicesByName.ALICE.config.description;const pending=context.refreshVoiceMetadata();await new Promise(resolve=>setImmediate(resolve));assert(holdRead.resolve);value={ALICE:{description:'typed while reading'}};context.saveVoicesDebounced();holdRead.resolve();holdRead=null;await assert.rejects(pending,/changed while refreshing/);assert.strictEqual(context.window._voicesByName.ALICE.config.description,before);assert(context.dirty());await context.flushVoiceSaves();assert.strictEqual(writes.at(-1).voices.ALICE.description,'typed while reading');finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',code,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)


    def test_actual_browser_javascript_posts_to_real_guarded_http_and_preserves_newer_artifact(self):
        from contextlib import ExitStack
        import json
        import socket
        import tempfile
        import threading
        import time
        from unittest.mock import patch
        from fastapi import FastAPI
        import uvicorn
        from routers import voices
        code=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');const source=fs.readFileSync(process.argv[1],'utf8'),base=process.argv[2],artifact=process.argv[3];
const helper=source.slice(source.indexOf('function createSerializedSaveQueue('),source.indexOf('// --- API Helpers ---'));
const api=source.slice(source.indexOf('const API = {'),source.indexOf('// --- Setup Tab ---'));
const saves=source.slice(source.indexOf('let _voiceStatusClearTimer ='),source.indexOf('// Auto-save on any change inside the voices list'));
const refresh=source.slice(source.indexOf('async function refreshVoiceMetadata()'),source.indexOf('let _voiceResourcesRefreshedAt'));
let value={ALICE:{type:'design',description:'first actual HTTP edit',seed:'0'}};const status={innerHTML:''};
const context={fetch:(url,options)=>fetch(new URL(url,base),options),window:{addEventListener(){},localStorage:{setItem(){},getItem(){return null;},removeItem(){}}},document:{getElementById:id=>id==='voice-save-status'?status:null,querySelectorAll:()=>[{}]},setTimeout:()=>1,clearTimeout(){},console:{error(){}},collectVoiceConfig:()=>value,showToast(){},showConfirm:async()=>false};vm.createContext(context);vm.runInContext(helper+api+saves+refresh,context);
let finished=false;process.on('beforeExit',()=>assert(finished,'actual HTTP assertions must finish'));
(async()=>{await context.refreshVoiceMetadata();context.saveVoicesDebounced();await context.flushVoiceSaves();assert.strictEqual(JSON.parse(fs.readFileSync(artifact)).ALICE.description,'first actual HTTP edit');
value={ALICE:{type:'design',description:'second actual HTTP edit',seed:'0'}};context.saveVoicesDebounced();await context.flushVoiceSaves();let saved=JSON.parse(fs.readFileSync(artifact));assert.strictEqual(saved.ALICE.description,'second actual HTTP edit');assert.strictEqual(saved.ALICE.seed,'0');assert.strictEqual(saved.ALICE.persona_voice_audit.notes,'preserve');
const other=await fetch(new URL('/api/voice_config/snapshot',base)).then(result=>result.json());const result=await fetch(new URL('/api/voice_config/save',base),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:other.revision,book_token:other.book_token,voices:{ALICE:{type:'design',description:'newer other client',seed:'0'}}})});assert.strictEqual(result.status,200);
const before=fs.readFileSync(artifact);value={ALICE:{type:'design',description:'stale browser edit',seed:'0'}};context.saveVoicesDebounced();await assert.rejects(context.flushVoiceSaves(),error=>error.status===409);assert.deepStrictEqual(fs.readFileSync(artifact),before);assert(status.innerHTML.includes('edits retained'));finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);script=root/'annotated_script.json';script.write_text('[{"speaker":"ALICE","text":"Original source."}]');path=root/'voice_config.json';path.write_text('{"ALICE":{"type":"design","description":"initial","persona_voice_audit":{"notes":"preserve"}}}');(root/'state.json').write_text('{"active_book_id":"book","book_generation":"one"}')
            stack.enter_context(patch.object(voices,'SCRIPT_PATH',str(script)));stack.enter_context(patch.object(voices,'VOICE_CONFIG_PATH',str(path)))
            app=FastAPI();app.include_router(voices.router)
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=create_test_api_server(app)
            thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
            try:
                deadline=time.monotonic()+5
                while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(0.01)
                self.assertTrue(server.started,'owned HTTP server must start')
                result=subprocess.run(['node','-e',code,str(SOURCE),f'http://127.0.0.1:{port}',str(path)],capture_output=True,text=True,timeout=15)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertEqual('newer other client',json.loads(path.read_text())['ALICE']['description'])
            finally:
                server.should_exit=True;thread.join(5);sock.close();self.assertFalse(thread.is_alive(),'owned HTTP server must stop')
