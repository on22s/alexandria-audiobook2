from tests.test_support import create_test_api_server
from contextlib import ExitStack
from pathlib import Path
import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'

HARNESS = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const helper=source.slice(source.indexOf('function createSerializedSaveQueue('),source.indexOf('// --- API Helpers ---'));
const api=source.slice(source.indexOf('const API = {'),source.indexOf('// --- Setup Tab ---'));
const saves=source.slice(source.indexOf('let _voiceStatusClearTimer ='),source.indexOf('// Auto-save on any change inside the voices list'));
const refresh=source.slice(source.indexOf('async function refreshVoiceMetadata()'),source.indexOf('let _voiceResourcesRefreshedAt'));
const revision=n=>String(n).padStart(64,'0'),book='b'.repeat(64);
function storageFor(file){
 function read(){return fs.existsSync(file)?JSON.parse(fs.readFileSync(file)):{};}
 return {get length(){return Object.keys(read()).length;},key:index=>Object.keys(read())[index]??null,
 getItem:key=>read()[key]??null,setItem(key,value){const d=read();d[key]=value;fs.writeFileSync(file,JSON.stringify(d));},
 removeItem(key){const d=read();delete d[key];fs.writeFileSync(file,JSON.stringify(d));}};
}
function client(storage,API,value={ALICE:{type:'design',description:'pending',seed:'0'}}){
 const status={innerHTML:'',textContent:''},panel={innerHTML:'',textContent:''},events={},errors=[],confirmations=[];
 const context={window:{localStorage:storage,addEventListener:(name,fn)=>events[name]=fn},
 document:{getElementById:id=>id==='voice-save-status'?status:id==='voice-save-drafts'?panel:null,querySelectorAll:()=>[{}]},
 setTimeout:()=>1,clearTimeout(){},console:{error(){}},Date,Math,API,fetch:(url,options)=>fetch(new URL(url,process.argv[3]),options),
 collectVoiceConfig:()=>value,escapeHtml:text=>String(text).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
 showToast:message=>errors.push(message),showConfirm:async text=>{confirmations.push(text);return true;}};
 vm.createContext(context);vm.runInContext(helper+(API?'':api)+saves+refresh+'function isDirty(){return voiceSaveQueue.isDirty();}',context);
 context.loadVoices=()=>context.refreshVoiceMetadata();
 return {context,status,panel,events,errors,confirmations,setValue:newValue=>value=newValue};
}
let finished=false;process.on('beforeExit',()=>assert(finished,'draft assertions must finish'));
'''


class VoiceDraftTests(unittest.TestCase):
    def run_node(self, code, *args):
        result = subprocess.run(['node','-e',HARNESS+code,str(SOURCE),*map(str,args)],capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_acknowledgement_keeps_newest_draft_and_tabs_keep_separate_records(self):
        code=r'''
(async()=>{
 const storage=storageFor(process.argv[2]),gates=[],writes=[];let server=0;
 const API={get:async()=>({revision:revision(server),book_token:book,book_id:'novel',voices:[],config:{}}),post:async(url,body)=>{writes.push(body);await new Promise(resolve=>gates.push(resolve));server++;return{revision:revision(server),book_token:book};}};
 const a=client(storage,API);await a.context.refreshVoiceMetadata();a.context.saveVoicesDebounced();
 assert.strictEqual(storage.length,1,'edit is persisted synchronously, before timer');
 const flight=a.context.flushVoiceSaves();a.setValue({ALICE:{description:'latest while in flight',seed:'0'}});a.context.saveVoicesDebounced();
 const b=client(storage,API,{ALICE:{description:'other tab'}});await b.context.refreshVoiceMetadata();b.context.saveVoicesDebounced();
 assert.strictEqual(storage.length,2,'tabs do not replace each other drafts');
 gates.shift()();await new Promise(resolve=>setImmediate(resolve));assert.strictEqual(writes.length,2);
 const records=Array.from({length:storage.length},(_,i)=>JSON.parse(storage.getItem(storage.key(i))));
 assert(records.some(row=>row.voices.ALICE.description==='latest while in flight'&&row.revision===revision(1)),'old ACK keeps and rebases only the latest own draft');
 assert(records.some(row=>row.voices.ALICE.description==='other tab'&&row.revision===revision(0)),'other tab base remains unchanged');
 gates.shift()();await flight;assert.strictEqual(storage.length,1);assert(!a.context.isDirty());
 assert.strictEqual(JSON.parse(storage.getItem(storage.key(0))).voices.ALICE.description,'other tab');finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        with tempfile.TemporaryDirectory() as tmp:self.run_node(code,Path(tmp)/'storage.json')

    def test_unload_and_storage_failure_preserve_edits_and_warn(self):
        code=r'''
(async()=>{
 const storage=storageFor(process.argv[2]);let posts=0;
 const API={get:async()=>({revision:revision(0),book_token:book,voices:[],config:{}}),post:async()=>{posts++;throw new Error('offline');}};
 const a=client(storage,API);await a.context.refreshVoiceMetadata();a.context.saveVoicesDebounced();
 let prevented=false;const event={preventDefault(){prevented=true;}};a.events.beforeunload(event);assert(prevented);assert.strictEqual(event.returnValue,'');
 a.events.pagehide();await new Promise(resolve=>setImmediate(resolve));assert.strictEqual(posts,1);assert(a.context.isDirty());assert.strictEqual(storage.length,1);
 const failing={...storage,setItem(){throw new Error('quota exceeded');}};
 const b=client(failing,API);await b.context.refreshVoiceMetadata();b.context.saveVoicesDebounced();assert(b.status.textContent.includes('draft unavailable'));
 prevented=false;b.events.beforeunload(event);assert(prevented);assert.strictEqual(storage.length,1,'failed storage must not remove previous tab draft');
 await assert.rejects(b.context.flushVoiceSaves(),/offline/);assert(b.context.isDirty());finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        with tempfile.TemporaryDirectory() as tmp:self.run_node(code,Path(tmp)/'storage.json')

    def test_recovery_refuses_newer_server_wrong_book_and_edits_during_confirmation(self):
        code=r'''
(async()=>{
 const storage=storageFor(process.argv[2]);let server=0,token=book,posts=0;
 const API={get:async()=>({revision:revision(server),book_token:token,voices:[],config:{}}),post:async()=>{posts++;throw new Error('unexpected');}};
 const a=client(storage,API,{ALICE:{description:'<script>draft</script>'}});await a.context.refreshVoiceMetadata();a.context.saveVoicesDebounced();const original=storage.getItem(storage.key(0));
 server=1;const b=client(storage,API);await b.context.refreshVoiceMetadata();assert(b.panel.innerHTML.includes('&lt;script&gt;'));await b.context.recoverVoiceDraft(0);assert.strictEqual(posts,0);assert(b.errors.some(e=>e.includes('changed since')));assert.strictEqual(storage.getItem(storage.key(0)),original);
 token='c'.repeat(64);const c=client(storage,API);await c.context.refreshVoiceMetadata();await c.context.recoverVoiceDraft(0);assert.strictEqual(posts,0);assert(c.errors.some(e=>e.includes('original book')));
 server=0;token=book;const d=client(storage,API);await d.context.refreshVoiceMetadata();let release;d.context.showConfirm=async()=>new Promise(resolve=>release=resolve);const recovery=d.context.recoverVoiceDraft(0);await new Promise(resolve=>setImmediate(resolve));d.setValue({ALICE:{description:'new local edit'}});d.context.saveVoicesDebounced();release(true);await recovery;assert.strictEqual(posts,0);assert(d.errors.some(e=>e.includes('changed during')));assert.strictEqual(storage.length,2);assert.strictEqual(storage.getItem(storage.key(0)),original);finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        with tempfile.TemporaryDirectory() as tmp:self.run_node(code,Path(tmp)/'storage.json')

    def test_process_exit_before_debounce_and_fresh_process_recovery_through_real_http(self):
        from fastapi import FastAPI
        import uvicorn
        from routers import voices
        first=r'''
(async()=>{const storage=storageFor(process.argv[2]);const a=client(storage,null,{ALICE:{type:'design',description:'large draft '+ 'x'.repeat(70000),seed:'0'}});await a.context.refreshVoiceMetadata();a.context.saveVoicesDebounced();assert.strictEqual(storage.length,1);finished=true;})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        second=r'''
(async()=>{const storage=storageFor(process.argv[2]);const a=client(storage,null);await a.context.refreshVoiceMetadata();assert(a.panel.innerHTML.includes('Unsaved voice draft'));assert.strictEqual(a.confirmations.length,0);await a.context.recoverVoiceDraft(0);assert.strictEqual(a.confirmations.length,1);assert(a.confirmations[0].includes('large draft'));assert.strictEqual(storage.length,0);assert(!a.context.isDirty());finished=true;})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);script=root/'annotated_script.json';script.write_text('[{"speaker":"ALICE","text":"Source."}]')
            path=root/'voice_config.json';path.write_text('{"ALICE":{"type":"design","description":"initial","persona_voice_audit":{"notes":"keep"}}}')
            (root/'state.json').write_text('{"active_book_id":"novel","book_generation":"one"}')
            stack.enter_context(patch.object(voices,'SCRIPT_PATH',str(script)));stack.enter_context(patch.object(voices,'VOICE_CONFIG_PATH',str(path)))
            app=FastAPI();app.include_router(voices.router);sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=create_test_api_server(app)
            thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
            try:
                deadline=time.monotonic()+5
                while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(0.01)
                self.assertTrue(server.started);url=f'http://127.0.0.1:{port}';storage=root/'storage.json'
                before=path.read_bytes();self.run_node(first,storage,url);self.assertEqual(before,path.read_bytes(),'no timer/save ran before process exit')
                draft=json.loads(next(iter(json.loads(storage.read_text()).values())))
                self.assertEqual('novel',draft['book_id']);self.assertEqual('0',draft['voices']['ALICE']['seed'])
                self.run_node(second,storage,url);saved=json.loads(path.read_text());self.assertEqual(draft['voices']['ALICE']['description'],saved['ALICE']['description']);self.assertEqual('0',saved['ALICE']['seed']);self.assertEqual({'notes':'keep'},saved['ALICE']['persona_voice_audit'])
            finally:
                server.should_exit=True;thread.join(5);sock.close();self.assertFalse(thread.is_alive())
