"""Recovery repaint, metadata rebase, guarded reread and worker-poll discriminators."""
import unittest

from tests import test_dataset_ui_ownership_js as ownership


class Ds2224RecoveryRepaintJsTests(unittest.TestCase):
    run_scenario = ownership.DatasetUiOwnershipJsTests.run_scenario

    def test_discard_forces_physical_table_replacement_over_a_focused_draft(self):
        self.run_scenario(r'''
const token='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};context.showConfirm=async()=>true;
run(source.slice(source.indexOf('function dsbRenderTable('),source.indexOf('window.dsbUpdateRow')));
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'draft',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const active={tagName:'TEXTAREA',value:'draft'};context.document.activeElement=active;
const tbody=context.document.getElementById('dsb-table-body');tbody.children=[{contains(node){return node===active;},children:[],getAttribute(){return 'pending';},setAttribute(){}}];
context.API.get=async()=>({description:'root',global_seed:'',samples:[{text:'peer',emotion:'',seed:'',status:'pending'}],row_revisions:[token],running:false,logs:[]});
run("dsbRows=[{text:'draft',emotion:'',seed:'',status:'pending'}];dsbSaveRows()");
await run('dsbDiscardDatasetDraftAndReload()');
assert(tbody.innerHTML.includes('>peer</textarea>'),'the physical control must show the replacement');
assert(!tbody.innerHTML.includes('>draft</textarea>'),'the focused draft must be detached');
''')

    def test_verified_predecessor_ack_rebases_a_newer_seed_draft_expectation(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1);applyDatasetMetadataAck('A',{description:'root',global_seed:''})");
context.document.getElementById('dsb-description').value='root';
let release;const held=new Promise(resolve=>release=resolve);let n=0,posts=[];
context.API.post=async(url,body)=>{posts.push(body);if(n++===0){await held;return {samples:[],row_revisions:[a],description:'new root',global_seed:''};}return {samples:[],row_revisions:[a],description:body.description,global_seed:body.global_seed};};
context.dsbSaveForm();const first=run('dsbSaveMetaQueue.flush()');
context.document.getElementById('dsb-description').value='new root';context.dsbSaveForm();
context.document.getElementById('dsb-global-seed').value='44';context.dsbSaveForm();
release();await first;await run('dsbSaveMetaQueue.flush()');
assert.strictEqual(run("document.getElementById('dsb-description').value"),'new root');
assert.strictEqual(run("ensureDatasetRowSaveState('A').metadata.description"),'new root');
assert.strictEqual(posts.at(-1).expected_description,'new root','the seed follow-up binds to its verified predecessor');
assert.strictEqual(posts.at(-1).global_seed,'44');
''')

    def test_guarded_followup_read_cannot_install_a_superseded_row_snapshot(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
let fetchFn;const handlers={};context._startPolling=(key,fetch,h)=>{fetchFn=fetch;Object.assign(handlers,h);return()=>{};};
let pass=0,releaseSecond;const gate=new Promise(resolve=>releaseSecond=resolve);
context.API.get=async()=>{
 if(pass++===0)return {description:'root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'done',audio_url:'/old.wav'}],row_revisions:[a],running:false,logs:[]};
 await gate;return {description:'root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'done',audio_url:'/old.wav'}],row_revisions:[a],running:false,logs:[]};};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'corrected',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['b'.repeat(64)],1);applyDatasetMetadataAck('A',{description:'root',global_seed:''});dsbStartPolling('A')");
const first=fetchFn();
context.API.post=async()=>({samples:[{text:'corrected',emotion:'',seed:'',status:'pending',audio_url:null}],row_revisions:[c],description:'new root',global_seed:''});
context.document.getElementById('dsb-description').value='new root';context.dsbSaveForm();await run('dsbSaveMetaQueue.flush()');
handlers.onTick(await first);
await new Promise(resolve=>setImmediate(resolve));
context.API.post=async()=>({row_revisions:{0:c}});
run("dsbUpdateRow(0,'text','corrected again')");await run('dsbSaveRowsQueue.flush()');
releaseSecond();await run("ensureDatasetRowSaveState('A').refreshPromise");
assert.strictEqual(run('dsbRows[0].text'),'corrected again','the delayed follow-up must not restore old text');
assert.strictEqual(run('dsbRows[0].audio_url'),null);
assert.strictEqual(run('dsbRows[0].status'),'pending');
''')

    def test_discard_of_a_running_worker_shares_normal_batch_setup(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};context.showConfirm=async()=>true;
let polling=false;context._startPolling=()=>{polling=true;return()=>{};};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'draft',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
context.API.get=async()=>({description:'root',global_seed:'',samples:[{text:'peer',emotion:'',seed:'',status:'generating'}],row_revisions:[a],running:true,ownership_unconfirmed:false,logs:['[1/1] peer'],dataset_name:'A'});
await run('dsbDiscardDatasetDraftAndReload()');
assert.strictEqual(run('dsbBatchRunning'),true,'the replacement adopts the running worker');
assert.strictEqual(polling,true,'and attaches the same ownership poll');
assert.strictEqual(context.document.getElementById('dsb-btn-cancel').style.display,'');
assert.strictEqual(run('dsbRows[0].status'),'generating');
''')


if __name__ == '__main__':
    unittest.main()
