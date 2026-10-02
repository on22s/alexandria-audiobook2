from pathlib import Path
import subprocess
import tempfile
import unittest

STATIC = Path(__file__).resolve().parent.parent / 'static/js'


class SerializedSaveQueueTests(unittest.TestCase):
    def test_coalescing_failure_and_revision_acknowledgement(self):
        code=r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');const start=source.indexOf('function createSerializedSaveQueue('),end=source.indexOf('// --- API Helpers ---',start);assert(start>=0);
const timers=new Map();let id=0;const context={setTimeout(fn){timers.set(++id,fn);return id;},clearTimeout(id){timers.delete(id);}};vm.runInNewContext(source.slice(start,end),context);
let finished=false;process.on('beforeExit',()=>assert(finished,'asynchronous save assertions must finish'));
(async()=>{
 const writes=[],gates=[],saved=[],errors=[];let active=0,maxActive=0;
 const queue=context.createSerializedSaveQueue({write:async(value,revision)=>{active++;maxActive=Math.max(active,maxActive);writes.push({value,revision});await new Promise(resolve=>gates.push(resolve));active--;},onSaved:()=>saved.push(true),onError:error=>errors.push(error)});
 const initial={text:'first'};queue.enqueue(initial);initial.text='caller mutation';const first=queue.flush();assert(queue.isDirty());assert.strictEqual(writes[0].value.text,'first');
 queue.enqueue({text:'second'});queue.enqueue({text:'third'});const joined=queue.flush();assert.strictEqual(writes.length,1);gates.shift()();await new Promise(resolve=>setImmediate(resolve));
 assert.strictEqual(writes.length,2);assert.strictEqual(writes[1].value.text,'third');assert.strictEqual(saved.length,0,'older acknowledgement must not mark a newer revision saved');gates.shift()();await Promise.all([first,joined]);assert.strictEqual(maxActive,1);assert.strictEqual(saved.length,1);assert(!queue.isDirty());assert.strictEqual(errors.length,0);
 let fail=true;const attempts=[];const retry=context.createSerializedSaveQueue({write:async value=>{attempts.push(value.text);if(fail){throw new Error('fixture disk failure');}}});retry.enqueue({text:'retained'});await assert.rejects(retry.flush(),/disk failure/);assert(retry.isDirty());fail=false;await retry.flush();assert.deepStrictEqual(attempts,['retained','retained']);assert(!retry.isDirty());
 fail=true;retry.enqueue({text:'older failed'});await assert.rejects(retry.flush(),/disk failure/);retry.enqueue({text:'newer retained'});fail=false;await retry.flush();assert.strictEqual(attempts.at(-1),'newer retained');assert(!retry.isDirty());finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',code,str(STATIC/'app-core.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_actual_dataset_row_handler_serializes_inflight_saves_and_persists_latest_rows(self):
        code=r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');const core=fs.readFileSync(process.argv[1],'utf8'),work=fs.readFileSync(process.argv[2],'utf8'),artifact=process.argv[3];
const queue=core.slice(core.indexOf('function createSerializedSaveQueue('),core.indexOf('// --- API Helpers ---'));
const handlers=work.slice(work.indexOf('const dsbRowSaveStates ='),work.indexOf('function ensureDatasetRowsEditable()'));
const timers=new Map();let id=0,active=0,maxActive=0;const writes=[],releases=[],errors=[];
const context={setTimeout(fn){timers.set(++id,fn);return id;},clearTimeout(id){timers.delete(id);},API:{post:async(url,value)=>{active++;maxActive=Math.max(active,maxActive);writes.push(JSON.parse(JSON.stringify(value)));await new Promise(resolve=>releases.push(resolve));fs.writeFileSync(artifact,JSON.stringify(value));active--;return{status:'ok'};}},_toastSaveError:(name,error)=>errors.push(error),document:{getElementById:()=>({value:''})}};
vm.createContext(context);vm.runInContext(queue+'let dsbCurrentProject="book",dsbRows=[{text:"older",emotion:"",seed:0}],dsbSaveRowsQueue=null,dsbSaveMetaQueue=null,dsbSaveRowsTimer=null,dsbSaveMetaTimer=null;'+handlers,context);
let finished=false;process.on('beforeExit',()=>assert(finished,'actual handlers must finish'));
function fire(){const [key,fn]=timers.entries().next().value;timers.delete(key);fn();}
(async()=>{context.dsbSaveRows();fire();assert.strictEqual(writes.length,1);vm.runInContext('dsbRows=[{text:"newer",emotion:"calm",seed:0}];dsbSaveRows();',context);fire();assert.strictEqual(writes.length,1,'a newer row POST must wait for the in-flight older write');releases.shift()();await new Promise(resolve=>setImmediate(resolve));assert.strictEqual(writes.length,2);releases.shift()();await new Promise(resolve=>setImmediate(resolve));const saved=JSON.parse(fs.readFileSync(artifact));assert.strictEqual(saved.rows[0].text,'newer');assert.strictEqual(saved.rows[0].seed,0);assert.strictEqual(maxActive,1);assert.strictEqual(errors.length,0);finished=true;})().catch(error=>{console.error(error);while(releases.length){releases.shift()();}process.exitCode=1;});
'''
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run(['node','-e',code,str(STATIC/'app-core.js'),str(STATIC/'app-workbench.js'),str(Path(tmp)/'state.json')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)


    def test_actual_voice_handlers_and_book_actions_wait_for_latest_save_or_abort_on_failure(self):
        code=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');const core=fs.readFileSync(process.argv[1],'utf8'),scripts=fs.readFileSync(process.argv[2],'utf8');
const helper=core.slice(core.indexOf('function createSerializedSaveQueue('),core.indexOf('// --- API Helpers ---'));
const saves=core.slice(core.indexOf('let _voiceStatusClearTimer ='),core.indexOf('// Auto-save on any change inside the voices list'));
const actions=scripts.slice(scripts.indexOf('async function saveScript()'),scripts.indexOf('async function deleteScript('));
const bookContext=core.slice(core.indexOf("let currentBookFilename = ''"),core.indexOf('async function loadConfig()'));
let finished=false;process.on('beforeExit',()=>assert(finished,'voice/book action assertions must finish'));
(async()=>{for(const operation of ['save','load','failure']){
 let value={ALICE:{description:'older'}},persisted=null,requests=0;const writes=[],gates=[],errors=[],toasts=[],elements={'voice-save-status':{innerHTML:''},'save-script-name':{value:'saved book'}};
 const context={window:{addEventListener(){},localStorage:{setItem(){},getItem(){return null;},removeItem(){}}},document:{getElementById:id=>elements[id]||(elements[id]={}),querySelectorAll:()=>[{}]},setTimeout:()=>1,clearTimeout(){},console:{error:()=>{}},collectVoiceConfig:()=>value,API:{post:async(url,body)=>{if(url!=='/api/voice_config/save'){assert.strictEqual(url,operation==='save'?'/api/scripts/save':'/api/scripts/load');requests++;assert.strictEqual(persisted.ALICE.description,'newer');return{status:'saved',name:'next'};}writes.push(body.voices);await new Promise((resolve,reject)=>gates.push({resolve,reject}));persisted=body.voices;return{status:'saved',book_token:body.book_token,revision:String(writes.length).padStart(64,'0')};}},fetch:async()=>{throw Error('book actions must use shared API');},showConfirm:async()=>true,showToast:(message,type)=>{toasts.push([message,type]);if(type==='error'){errors.push(message);}},resetDesignerForm(){},clearVoiceSuggestions(){},clearCharacterAliases(){},loadCharacterAliases:async()=>{},loadChunks:async()=>{},loadVoices:async()=>{},loadSavedScripts(){},loadDesignedVoices(){}};
 vm.createContext(context);vm.runInContext(helper+saves+bookContext+actions+"_voiceSaveSnapshot={revision:\"0\".repeat(64),book_token:\"b\".repeat(64)};",context);context.saveVoicesDebounced();const initial=context.flushVoiceSaves().catch(()=>{});value={ALICE:{description:'newer'}};context.saveVoicesDebounced();const action=operation==='save'?context.saveScript():context.loadScript('next');await new Promise(resolve=>setImmediate(resolve));assert.strictEqual(requests,0);assert.strictEqual(writes.length,1);
 if(operation==='failure'){gates.shift().reject(new Error('fixture failed save'));await Promise.all([action,initial]);assert.strictEqual(requests,0);assert.strictEqual(writes.length,1);assert(elements['voice-save-status'].innerHTML.includes('save failed'));assert.strictEqual(errors.length,1);continue;}
 gates.shift().resolve();await new Promise(resolve=>setImmediate(resolve));assert.strictEqual(writes.length,2);assert.strictEqual(requests,0);assert(!elements['voice-save-status'].innerHTML.includes('>saved'),'older acknowledgement cannot show saved');gates.shift().resolve();await Promise.all([action,initial]);assert.strictEqual(requests,1);assert.strictEqual(errors.length,0);if(operation==='load'){assert.strictEqual(context.getCurrentBookName(),'next');assert(toasts.some(([message,type])=>message.includes('loaded')&&type==='success'));}
}finished=true;})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',code,str(STATIC/'app-core.js'),str(STATIC/'app-scripts.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
