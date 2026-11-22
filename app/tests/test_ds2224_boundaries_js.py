"""Boundary discriminators for the #2224 coherent snapshot/recovery protocol."""
import unittest

from tests import test_dataset_ui_ownership_js as ownership


class Ds2224BoundaryJsTests(unittest.TestCase):
    run_scenario = ownership.DatasetUiOwnershipJsTests.run_scenario

    def test_clean_controls_follow_observed_metadata_but_a_draft_keeps_its_base(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
const handlers={};context._startPolling=(key,fetch,h)=>{Object.assign(handlers,h);return()=>{};};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1);applyDatasetMetadataAck('A',{description:'root',global_seed:''})");
context.dsbStartPolling('A');
handlers.onTick({running:false,samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[a],description:'external voice',global_seed:'77',logs:[]});
assert.strictEqual(run("document.getElementById('dsb-description').value"),'external voice');
assert.strictEqual(run("document.getElementById('dsb-global-seed').value"),'77');
assert.deepStrictEqual(plain(run("datasetExpectedMetadata('A')")),
 {expected_description:'external voice',expected_global_seed:'77'});
let body;context.API.post=async(url,payload)=>{body=payload;return {samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[a],description:'external voice',global_seed:'31'};};
context.document.getElementById('dsb-global-seed').value='31';context.dsbSaveForm();await run('dsbSaveMetaQueue.flush()');
assert.strictEqual(body.description,'external voice','the peer voice must survive a seed-only edit');
assert.strictEqual(body.expected_description,'external voice');
assert.strictEqual(body.expected_global_seed,'77');
''')

    def test_dirty_metadata_control_keeps_its_original_expectation_across_poll(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
const handlers={};context._startPolling=(key,fetch,h)=>{Object.assign(handlers,h);return()=>{};};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1);applyDatasetMetadataAck('A',{description:'root',global_seed:'5'})");
context.dsbStartPolling('A');
context.document.getElementById('dsb-description').value='my root';context.document.getElementById('dsb-global-seed').value='9';context.dsbSaveForm();
handlers.onTick({running:false,samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[a],description:'external voice',global_seed:'77',logs:[]});
assert.strictEqual(run("document.getElementById('dsb-description').value"),'my root','dirty control is not replaced');
let body;context.API.post=async(url,payload)=>{body=payload;const e=Error('conflict');e.status=409;throw e;};
await assert.rejects(run('dsbSaveMetaQueue.flush()'));
assert.strictEqual(body.description,'my root');
assert.strictEqual(body.expected_description,'root','draft stays bound to the base it captured');
assert.strictEqual(body.expected_global_seed,'5');
''')

    def test_failed_reload_after_discard_keeps_the_refused_draft_latched(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const posts=[],gets=[];context.API.post=async(url,body)=>{posts.push(url);const e=Error('conflict');e.status=409;throw e;};
run("dsbRows=[{text:'mine',emotion:'',seed:'',status:'pending'},{text:'my second',emotion:'',seed:'',status:'pending'}];dsbSaveRows()");
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
let fail=true;context.API.get=async(url)=>{gets.push(url);if(fail){const e=Error('corrupt');e.status=500;throw e;}return {description:'root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'pending'},{text:'second',emotion:'',seed:'',status:'pending'}],row_revisions:[a,'b'.repeat(64)],running:false,logs:[]};};
await run('dsbDiscardDatasetDraftAndReload()');
assert.deepStrictEqual(plain(run('dsbRows.map(r=>r.text)')),['mine','my second'],'failed GET must not discard the draft');
assert.strictEqual(run('dsbSaveRowsQueue.isDirty()'),true);
assert.strictEqual(run("ensureDatasetRowSaveState('A').conflictLatch"),true);
assert.strictEqual(posts.filter(p=>p.endsWith('update_rows')).length,1,'no replay during recovery');
fail=false;await run('dsbDiscardDatasetDraftAndReload()');
assert.deepStrictEqual(plain(run('dsbRows.map(r=>r.text)')),['old','second']);
assert.strictEqual(run('dsbSaveRowsQueue.isDirty()'),false);
assert.strictEqual(posts.filter(p=>p.endsWith('update_rows')).length,1);
''')

    def test_refused_import_is_not_replayed_by_switch_generation_or_reopen(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};context.showConfirm=async()=>true;
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'},{text:'other',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64),'b'.repeat(64)],2)");
const posts=[];let refuse=true;context.API.post=async(url,body)=>{posts.push(url);if(refuse){const e=Error('conflict');e.status=409;throw e;}return {status:'started',row_revisions:[a,b]};};
run("dsbRows=[{text:'mine',emotion:'',seed:'',status:'pending'},{text:'my second',emotion:'',seed:'',status:'pending'}];dsbSaveRows()");
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
const before=posts.length;
const select=context.document.getElementById('dsb-project-select');select.value='B';await context.dsbOnProjectChange();
assert.strictEqual(posts.length,before,'switch must not replay the refused payload');
assert.strictEqual(select.value,'A');
await context.dsbGenerateAll();assert.strictEqual(posts.length,before,'generation must not replay it');
await run("dsbLoadProject('A')");assert.strictEqual(posts.length,before,'reopen must not replay it');
refuse=false;context.API.get=async()=>({description:'root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'pending'},{text:'other',emotion:'',seed:'',status:'pending'}],row_revisions:[a,b],running:false,logs:[]});
await run('dsbDiscardDatasetDraftAndReload()');
assert.strictEqual(posts.length,before,'explicit discard sends no write');
''')

    def test_clean_focused_row_adopts_a_verified_token_when_definition_matches(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run(source.slice(source.indexOf('function dsbRenderTable('),source.indexOf('window.dsbUpdateRow')));
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'done'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const active={tagName:'TEXTAREA',value:'old'};context.document.activeElement=active;
const tbody=context.document.getElementById('dsb-table-body');tbody.children=[{contains(node){return node===active;},getAttribute(){return 'done';},setAttribute(){},children:[]}];
context.API.post=async()=>({samples:[{text:'old',emotion:'',seed:'',status:'pending',audio_url:null}],row_revisions:[b],description:'new root',global_seed:''});
context.dsbSaveForm();await run('dsbSaveMetaQueue.flush()');
assert.strictEqual(run("ensureDatasetRowSaveState('A').tokens[0]"),b,'focused but unchanged control adopts its own ACK');
let sent;context.API.post=async(url,body)=>{sent=body;return {row_revisions:{0:c}};};
run("dsbUpdateRow(0,'text','old changed')");await run('dsbSaveRowsQueue.flush()');
assert.strictEqual(sent.edits[0].expected_revision,b,'the following edit uses the verified token');
''')

    def test_delayed_terminal_get_cannot_roll_back_a_newer_metadata_ack(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
let fetchFn;const handlers={};context._startPolling=(key,fetch,h)=>{fetchFn=fetch;Object.assign(handlers,h);return()=>{};};
context.API.get=async()=>({description:'root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[a],running:false,logs:[]});
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1);applyDatasetMetadataAck('A',{description:'root',global_seed:''});dsbStartPolling('A')");
const oldGet=fetchFn();
context.API.post=async(url,body)=>({samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[a],description:'new voice',global_seed:''});
context.document.getElementById('dsb-description').value='new voice';context.dsbSaveForm();await run('dsbSaveMetaQueue.flush()');
handlers.onTick(await oldGet);
assert.deepStrictEqual(plain(run("datasetExpectedMetadata('A')")),{expected_description:'new voice',expected_global_seed:''},'old GET must not restore root');
assert.strictEqual(run("document.getElementById('dsb-description').value"),'new voice');
context.API.get=async()=>({description:'new voice',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[a],running:false,logs:[]});
handlers.onTick(await fetchFn());
assert.strictEqual(run("document.getElementById('dsb-description').value"),'new voice','fresh GET still applies');
''')

    def test_full_receipt_reconciles_survivor_status_and_audio(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
context.document.getElementById('dsb-description').value='root';
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'first',emotion:'',seed:'',status:'done',audio_url:'/first.wav'},{text:'second',emotion:'',seed:'',status:'done',audio_url:'/second.wav'}];applyDatasetRowRevisions('A',['a'.repeat(64),'b'.repeat(64)],2)");
let body;context.API.post=async(url,payload)=>{body=payload;return {status:'ok',sample_count:1,description:'root',global_seed:'',
 samples:[{text:'second',emotion:'',seed:'',status:'pending',audio_url:null}],row_revisions:['c'.repeat(64)]};};
run("dsbRows.splice(0,1);dsbSaveRows()");await run('dsbSaveRowsQueue.flush()');
assert.strictEqual(run('dsbRows[0].status'),'pending','the reindexed survivor follows the durable projection');
assert.strictEqual(run('dsbRows[0].audio_url'),null,'the old player is gone');
let batch;context.API.post=async(url,payload)=>{if(url.endsWith('generate_batch')){batch=payload;return {status:'started'};}throw Error(url);};
run('dsbStartPolling=()=>{}');await context.dsbGenerateAll();
assert.deepStrictEqual(plain(batch.indices),[0],'Generate Pending selects the survivor at its new index');
''')

    def test_conflict_copy_includes_metadata_and_ordinary_export_does_not(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
let payload;context.Blob=class {constructor(parts){payload=JSON.parse(parts[0]);}};
context.URL={createObjectURL:()=>'/blob',revokeObjectURL(){}};context.document.createElement=()=>({click(){}});
context.document.getElementById('dsb-description').value='pending root';context.document.getElementById('dsb-global-seed').value='31';context.dsbSaveForm();
context.API.post=async()=>{const e=Error('conflict');e.status=409;throw e;};await assert.rejects(run('dsbSaveMetaQueue.flush()'));
run('dsbCopyDatasetDraft()');
assert.deepStrictEqual(plain(payload),{description:'pending root',global_seed:'31',rows:[{emotion:'',text:'old'}]},'conflict copy carries the metadata draft');
run('dsbExport()');
assert.deepStrictEqual(plain(payload),[{emotion:'',text:'old'}],'ordinary export keeps its rows-only format');
''')


if __name__ == '__main__':
    unittest.main()
