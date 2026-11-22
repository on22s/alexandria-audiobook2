"""Frontend discriminators for the #2224 per-project reconciliation model."""
import unittest

from tests import test_dataset_ui_ownership_js as ownership


class Ds2224FrontendJsTests(unittest.TestCase):
    run_scenario = ownership.DatasetUiOwnershipJsTests.run_scenario

    def test_explicit_empty_emotion_survives_cold_load_and_regeneration(self):
        self.run_scenario(r'''
const t='a'.repeat(64),next='b'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='';dsbRows=[];");
context.API.get=async()=>({description:'root',global_seed:'',samples:[
 {emotion:'',text:'one',seed:'',status:'done',description:'root'}],row_revisions:[t],running:false,logs:[]});
await context.dsbLoadProject('A');
assert.strictEqual(run('dsbRows[0].emotion'),'',
 'an empty emotion must not be promoted to the persisted description');
let sent;
context.API.post=async(url,body)=>{sent=body;return {status:'done',audio_url:'/a.wav',row:{},row_revision:next};};
await context.dsbGenSample(0);
assert.strictEqual(sent.description,'root');
assert.strictEqual((sent.description.match(/root/g)||[]).length,1);
''')

    def test_cold_load_binds_base_and_flushes_before_selecting_pending_rows(self):
        self.run_scenario(r'''
const t0='a'.repeat(64),t1='b'.repeat(64),t2='c'.repeat(64),t3='d'.repeat(64);
context.showToast=()=>{};context._toastSaveError=()=>{};context.dsbStartPolling=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='';dsbRows=[];");
context.API.get=async()=>({description:'old root',global_seed:'',samples:[
 {text:'one',emotion:'',seed:'',status:'done'},{text:'two',emotion:'',seed:'',status:'done'}],
 row_revisions:[t0,t1],running:false,logs:[]});
await context.dsbLoadProject('A');
assert.deepStrictEqual(plain(run("ensureDatasetRowSaveState('A').baseDefinitions")),
 plain(run('dsbRows').map(r=>({emotion:r.emotion,text:r.text,seed:r.seed}))));
elements['dsb-description'].value='new root';context.dsbSaveForm();
let batch=null;
context.API.post=async(url,body)=>{
 if(url.endsWith('update_meta')){return {samples:[
  {text:'one',emotion:'',seed:'',status:'pending'},{text:'two',emotion:'',seed:'',status:'pending'}],
  row_revisions:[t2,t3]};}
 if(url.endsWith('generate_batch')){batch=body;return {status:'started'};}
 throw Error(url);};
await context.dsbGenerateAll();
assert(batch,'a batch must be submitted after the invalidation');
assert.deepStrictEqual(batch.indices,[0,1]);
''')

    def test_metadata_invalidation_rerenders_status_and_audio(self):
        self.run_scenario(r'''
run(source.slice(source.indexOf('function dsbRenderTable('),source.indexOf('window.dsbUpdateRow')));
const t0='a'.repeat(64),t1='b'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'one',emotion:'',seed:'',status:'done',audio_url:'/old.wav'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const tbody=context.document.getElementById('dsb-table-body');let replaced=0;
tbody.children=[{attr:{'data-dsb-status':'done','data-dsb-audio':'/old.wav'},
 getAttribute(n){return this.attr[n];},setAttribute(n,v){this.attr[n]=v;},
 replaceWith(){replaced++;},contains(){return false;}}];
context.document.createElement=()=>({set innerHTML(html){this.firstElementChild={html};}});
context.API.post=async()=>({samples:[{text:'one',emotion:'',seed:'',status:'pending',audio_url:null}],row_revisions:[t1]});
context.dsbSaveForm();await run('dsbSaveMetaQueue.flush()');
assert.strictEqual(run('dsbRows[0].status'),'pending');
assert.strictEqual(replaced,1,'the done badge and its player must be replaced on invalidation');
''')

    def test_focused_dirty_control_is_never_replaced_by_an_external_edit(self):
        self.run_scenario(r'''
run(source.slice(source.indexOf('function dsbRenderTable('),source.indexOf('window.dsbUpdateRow')));
const t0='a'.repeat(64),t1='b'.repeat(64),warnings=[];context.showToast=(...a)=>warnings.push(a);context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'one',emotion:'',seed:'',status:'pending',audio_url:null}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const active={tagName:'TEXTAREA'};context.document.activeElement=active;
const tbody=context.document.getElementById('dsb-table-body');let replaced=0;
tbody.children=[{attr:{'data-dsb-status':'pending','data-dsb-audio':''},
 getAttribute(n){return this.attr[n];},setAttribute(n,v){this.attr[n]=v;},
 replaceWith(){replaced++;},contains(node){return node===active;}}];
context.dsbUpdateRow(0,'text','my typing');
assert.strictEqual(replaced,0);
context.API.post=async(url,body)=>{
 if(url.endsWith('update_meta')){return {samples:[{text:'external',emotion:'',seed:'',status:'pending'}],row_revisions:[t1]};}
 if(url.endsWith('edit_rows')){assert.strictEqual(body.edits[0].expected_revision,t0);
  const e=Error('row changed');e.status=409;throw e;}
 throw Error(url);};
context.dsbSaveForm();await run('dsbSaveMetaQueue.flush()');
assert.strictEqual(run('dsbRows[0].text'),'my typing','the visible draft must survive');
assert.strictEqual(replaced,0,'a live control must never be detached');
assert.strictEqual(run("ensureDatasetRowSaveState('A').tokens[0]"),t0,'no external token adoption');
await assert.rejects(run('dsbSaveRowsQueue.flush()'),/row changed/);
assert.strictEqual(run('dsbRows[0].text'),'my typing','the rejected draft stays recoverable');
assert(warnings.some(a=>String(a[0]).includes('elsewhere')));
''')

    def test_full_list_write_carries_base_count_and_all_revisions(self):
        self.run_scenario(r'''
const t0='a'.repeat(64),t1='b'.repeat(64),t2='c'.repeat(64),t3='d'.repeat(64);
context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'one',emotion:'',seed:'',status:'done'},{text:'two',emotion:'',seed:'',status:'done'}];applyDatasetRowRevisions('A',['a'.repeat(64),'b'.repeat(64)],2)");
let sent;context.API.post=async(url,body)=>{sent=body;return {row_revisions:[t2,t3,t0]};};
context.dsbAddRow('warm','added',0);
await run('dsbSaveRowsQueue.flush()');
assert.strictEqual(sent.expected_count,2,'the BASE count, not the new row count');
assert.deepStrictEqual(plain(sent.expected_revisions),{'0':t0,'1':t1});
assert.strictEqual(sent.rows.length,3);
''')

    def test_own_metadata_ack_advances_draft_token_so_edit_is_not_rejected(self):
        self.run_scenario(r'''
const t0='a'.repeat(64),t1='b'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'one',emotion:'',seed:'',status:'done',audio_url:'/old.wav'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const posts=[];let editBody;context.API.post=async(url,body)=>{
 posts.push(url);
 if(url.endsWith('edit_rows')){editBody=body;return {row_revisions:{0:t0}};}
 if(url.endsWith('update_meta')){return {samples:[{text:'one',emotion:'',seed:'',status:'pending',audio_url:null}],row_revisions:[t1]};}
 throw Error(url);};
context.dsbUpdateRow(0,'text','edited');context.document.getElementById('dsb-description').value='new root';
context.dsbSaveForm();
await run('dsbSaveMetaQueue.flush()');
await run('dsbSaveRowsQueue.flush()');
assert.deepStrictEqual(plain(posts),['/api/dataset_builder/update_meta','/api/dataset_builder/edit_rows']);
assert.strictEqual(editBody.edits[0].expected_revision,t1,
 'the draft must use the token advanced by its own metadata ACK, got '+editBody.edits[0].expected_revision);
''')

    def test_failed_presend_flush_keeps_rows_and_does_not_poll_status(self):
        self.run_scenario(r'''
run(core.slice(core.indexOf('        const API = {'),core.indexOf('        // --- Setup Tab ---')));
const t='a'.repeat(64);context.showToast=()=>{};const toasts=[];context._toastSaveError=(kind,e)=>toasts.push([kind,e]);
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'one',emotion:'',seed:'',status:'done',audio_url:'/old.wav'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const posts=[],gets=[];context.fetch=async(url,request)=>{
 (request?posts:gets).push(url);
 if(request&&url.endsWith('update_meta')){return new Response(JSON.stringify({detail:'corrupt state'}),{status:500});}
 if(request){return new Response(JSON.stringify({status:'started'}),{status:200});}
 return new Response(JSON.stringify({running:false,samples:[],logs:[]}),{status:200});};
context.document.getElementById('dsb-description').value='dirty root';context.dsbSaveForm();
await context.dsbGenerateAll();
assert.deepStrictEqual(posts,['/api/dataset_builder/update_meta'],'no generate request after a failed pre-send flush');
assert.strictEqual(gets.length,0,'no forgiving status poll may clear the visible project');
assert.strictEqual(run('dsbRows[0].text'),'one');
''')


if __name__ == '__main__':
    unittest.main()
