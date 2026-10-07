from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
HARNESS=r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');const source=fs.readFileSync(process.argv[1],'utf8'),artifact=process.argv[2];
const marker=source.includes('const pendingChunkEdits =')?'const pendingChunkEdits =':'window.updateChunk =';
const edits=source.slice(source.indexOf(marker),source.indexOf('window.generateChunk ='));
const render=source.slice(source.indexOf('async function _runBatchRender('),source.indexOf("document.getElementById('btn-merge').addEventListener"));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function client(){
 const controls={text:'focused new line',speaker:'NEW SPEAKER',instruct:'gentle',pause_after:''};
 const row={dataset:{id:'7'},classList:{add(){}},querySelector:()=>null,querySelectorAll:()=>Object.entries(controls).map(([field,value])=>({value,getAttribute:()=>`updateChunk(7, '${field}', this.value)`}))};
 const calls=[],errors=[],gates=[],gets=[],batches=[];let hold=true,fail=false,holdRead=false,readRelease;
 fs.writeFileSync(artifact,JSON.stringify([{id:7,text:'',speaker:'OLD',status:'pending'}]));
 const context={currentBookFilename:'fixture-book',refreshDeliveryReview:async()=>null,invalidateEditorIntegrity(){},refreshEditorIntegrity:async()=>{},window:{},document:{getElementById:()=>({style:{}}),querySelector:()=>row,querySelectorAll:selector=>selector==='#chunks-table-body tr[data-id]'?[row]:[]},console:{error(){},log(){}},showToast:error=>errors.push(error),showConfirm:async()=>true,cancelRender(){},_startPolling(){},loadChunks:async()=>{},runDriftCheck(){},API:{post:async(url,data)=>{
 calls.push({url,data:JSON.parse(JSON.stringify(data))});
 if(url==='/api/chunks/7'){
  if(hold){await new Promise(resolve=>gates.push(resolve));}if(fail){throw new Error('fixture row write failed');}
  const rows=JSON.parse(fs.readFileSync(artifact));rows[0]={...rows[0],...data,status:'pending'};fs.writeFileSync(artifact,JSON.stringify(rows));return{};
 }
 batches.push({url,data:JSON.parse(JSON.stringify(data)),artifact:JSON.parse(fs.readFileSync(artifact))});return{total_chunks:1,workers:1};
 },get:async url=>{assert.strictEqual(url,'/api/chunks');gets.push(url);const snapshot=JSON.parse(fs.readFileSync(artifact));if(holdRead){await new Promise(resolve=>readRelease=resolve);holdRead=false;}return snapshot;}}};
 context.window=context;vm.createContext(context);vm.runInContext(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')),context);vm.runInContext('let isRenderingAll=false;let cachedChunks=[{id:7,text:"",speaker:"OLD",instruct:"",status:"pending"}];'+edits+render,context);
 return{context,controls,calls,errors,gates,gets,batches,setHold:x=>hold=x,setFail:x=>fail=x,holdNextRead:()=>holdRead=true,releaseRead:()=>readRelease(),getReadRelease:()=>readRelease};
}
let finished=false;process.on('beforeExit',()=>assert(finished,'Editor barrier assertions must finish'));
'''

class EditorRenderBarrierTests(unittest.TestCase):
    def run_node(self,code):
        with tempfile.TemporaryDirectory() as tmp:
            result=subprocess.run(['node','-e',HARNESS+code,str(SOURCE),str(Path(tmp)/'chunks.json')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_both_render_buttons_serialize_rows_and_include_focused_uncommitted_controls(self):
        self.run_node(r'''
(async()=>{for(const button of ['renderAll','renderBatchFast']){
 const a=client();const first=a.context.updateChunk(7,'text','earlier line');const second=a.context.updateChunk(7,'speaker','EARLIER SPEAKER');const rendering=a.context[button]();await tick();
 assert.strictEqual(a.gets.length,0,'render must not read server snapshot before pending writes');assert.strictEqual(a.batches.length,0);assert.strictEqual(a.gates.length,1,'same row requests must be serialized');
 a.gates.shift()();await tick();assert.strictEqual(a.gets.length,0);assert.strictEqual(a.gates.length,1);a.gates.shift()();await tick();assert.strictEqual(a.gets.length,0);assert.strictEqual(a.gates.length,1);a.gates.shift()();await Promise.all([first,second,rendering]);
 assert.strictEqual(a.batches.length,1);assert.deepStrictEqual(a.batches[0].data,{indices:[7]});assert.strictEqual(a.batches[0].artifact[0].text,'focused new line');assert.strictEqual(a.batches[0].artifact[0].speaker,'NEW SPEAKER');assert.strictEqual(a.batches[0].artifact[0].pause_after,null);assert.strictEqual(a.errors.length,0);
}finished=true;})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_failed_row_save_aborts_render_and_successful_retry_uses_latest_artifact(self):
        self.run_node(r'''
(async()=>{const a=client();a.setHold(false);a.setFail(true);await a.context.renderAll();assert.strictEqual(a.gets.length,0);assert.strictEqual(a.batches.length,0);assert(a.errors.some(e=>e.includes('row write failed')));assert.strictEqual(JSON.parse(fs.readFileSync(artifact))[0].text,'');
 a.setFail(false);a.controls.text='corrected after failure';await a.context.renderAll();assert.strictEqual(a.batches.length,1);assert.strictEqual(a.batches[0].artifact[0].text,'corrected after failure');finished=true;})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_edits_during_snapshot_and_confirmation_are_flushed_and_resnapshotted(self):
        self.run_node(r'''
(async()=>{const a=client();a.setHold(false);a.holdNextRead();const rendering=a.context.renderBatchFast();await tick();assert(a.getReadRelease());a.controls.text='typed during GET';await a.context.updateChunk(7,'text',a.controls.text);a.releaseRead();await rendering;assert.strictEqual(a.gets.length,2);assert.strictEqual(a.batches[0].artifact[0].text,'typed during GET');
 const b=client();b.setHold(false);b.context.showConfirm=async()=>{b.controls.text='typed during confirm';b.controls.speaker='CONFIRMED SPEAKER';return true;};await b.context.renderAll(true);assert.strictEqual(b.gets.length,2);assert.strictEqual(b.batches[0].artifact[0].text,'typed during confirm');assert.strictEqual(b.batches[0].artifact[0].speaker,'CONFIRMED SPEAKER');finished=true;})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_unchanged_controls_do_not_overwrite_other_client_and_pending_revert_is_saved(self):
        self.run_node(r'''
(async()=>{const a=client();a.setHold(false);Object.assign(a.controls,{text:'',speaker:'OLD',instruct:'',pause_after:''});fs.writeFileSync(artifact,JSON.stringify([{id:7,text:'other client changed source',speaker:'REMOTE',status:'pending'}]));await a.context.renderAll();assert.strictEqual(a.calls.filter(call=>call.url==='/api/chunks/7').length,0);assert.strictEqual(a.batches[0].artifact[0].text,'other client changed source');assert.strictEqual(a.batches[0].artifact[0].speaker,'REMOTE');
 const b=client();Object.assign(b.controls,{text:'',speaker:'OLD',instruct:'',pause_after:''});const earlier=b.context.updateChunk(7,'text','pending earlier edit');const render=b.context.renderAll();await tick();b.gates.shift()();await tick();assert.strictEqual(b.gates.length,1);b.gates.shift()();await Promise.all([earlier,render]);assert.strictEqual(JSON.parse(fs.readFileSync(artifact))[0].text,'');assert.strictEqual(b.batches.length,0);finished=true;})().catch(error=>{console.error(error);process.exitCode=1;});
''')
