"""Coherent snapshot/commit protocol discriminators for #2224 remaining findings."""
import unittest

from tests import test_dataset_ui_ownership_js as ownership


class Ds2224SnapshotProtocolJsTests(unittest.TestCase):
    run_scenario = ownership.DatasetUiOwnershipJsTests.run_scenario

    def test_aba_poll_pairs_visible_definition_with_its_exact_revision(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);
context.showToast=()=>{};context._toastSaveError=()=>{};
const handlers={};context._startPolling=(key,fetch,h)=>{Object.assign(handlers,h);return()=>{};};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
context.dsbStartPolling('A');
handlers.onTick({running:false,samples:[{text:'B',emotion:'',seed:'',status:'pending'}],row_revisions:[b],description:'root',global_seed:'',logs:[]});
assert.strictEqual(run('dsbRows[0].text'),'B','the poll shows the external definition');
assert.strictEqual(run("ensureDatasetRowSaveState('A').tokens[0]"),b,
 'a clean displayed definition must be paired with that same revision, not the old one');
let sent;context.API.post=async(url,body)=>{sent=body;const e=Error('conflict');e.status=409;throw e;};
context.dsbAddRow('','mine');
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
assert.strictEqual(sent.expected_revisions['0'],b,
 'after an external ABA restore the write carries B, so the CAS refuses instead of overwriting restored A');
assert.deepStrictEqual(plain(run('dsbRows.map(r=>r.text)')),['B','mine'],
 'the refused structural draft stays visible and recoverable');
''')

    def test_aba_count_change_adopts_the_snapshot_count_and_revisions(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);
context.showToast=()=>{};context._toastSaveError=()=>{};
const handlers={};context._startPolling=(key,fetch,h)=>{Object.assign(handlers,h);return()=>{};};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
context.dsbStartPolling('A');
handlers.onTick({running:false,samples:[{text:'old',emotion:'',seed:'',status:'pending'},{text:'temporary',emotion:'',seed:'',status:'pending'}],row_revisions:[a,c],description:'root',global_seed:'',logs:[]});
assert.deepStrictEqual(plain(run('ensureDatasetRowSaveState("A").tokens')),[a,c],
 'a clean count change must adopt the revisions from that exact response');
let sent;context.API.post=async(url,body)=>{sent=body;const e=Error('conflict');e.status=409;throw e;};
context.dsbAddRow('','mine');
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
assert.strictEqual(sent.expected_count,2,'the restored single row cannot be resurrected under a stale count of 1');
assert.deepStrictEqual(plain(run('dsbRows.map(r=>r.text)')),['old','temporary','mine']);
''')

    def test_batch_completion_then_text_edit_uses_the_completion_revision(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);
context.showToast=()=>{};context._toastSaveError=()=>{};
const handlers={};context._startPolling=(key,fetch,h)=>{Object.assign(handlers,h);return()=>{};};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'generating'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
context.dsbStartPolling('A');
handlers.onTick({running:false,samples:[{text:'old',emotion:'',seed:'',status:'done',audio_url:'/ok.wav'}],row_revisions:[b],description:'root',global_seed:'',logs:[]});
assert.strictEqual(run('dsbRows[0].status'),'done');
assert.strictEqual(run('dsbRows[0].audio_url'),'/ok.wav');
assert.strictEqual(run("ensureDatasetRowSaveState('A').tokens[0]"),b,
 'the completion snapshot advances the clean row token');
let sent;context.API.post=async(url,body)=>{sent=body;return {row_revisions:{0:c}};};
run("dsbUpdateRow(0,'text','old suffix')");
await run('dsbSaveRowsQueue.flush()');
assert.strictEqual(sent.edits[0].expected_revision,b,
 'an ordinary edit after an own batch must not send the pre-generation token');
''')

    def test_metadata_ack_invalidates_status_and_audio_behind_a_pending_append(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64),d='d'.repeat(64);
context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'done',audio_url:'/old.wav'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
let metaBody,rowsBody,releaseMeta;
const gate=new Promise(resolve=>releaseMeta=resolve);
context.API.post=async(url,body)=>{
 if(url.endsWith('update_meta')){metaBody=body;await gate;
  return {samples:[{text:'old',emotion:'',seed:'',status:'pending',audio_url:null}],row_revisions:[b],description:'new root',global_seed:'5'};}
 if(url.endsWith('update_rows')){rowsBody=body;return {row_revisions:[b,c]};}
 throw Error(url);};
context.document.getElementById('dsb-description').value='new root';
context.document.getElementById('dsb-global-seed').value='5';
context.dsbSaveForm();const meta=run('dsbSaveMetaQueue.flush()');
while(!metaBody){await new Promise(resolve=>setImmediate(resolve));}
context.dsbAddRow('','ours');
releaseMeta();await meta;
assert.strictEqual(run('dsbRows[0].status'),'pending','the done badge must follow the invalidation');
assert.strictEqual(run('dsbRows[0].audio_url'),null,'the old player must be removed immediately');
assert.strictEqual(run("ensureDatasetRowSaveState('A').tokens[0]"),b);
await run('dsbSaveRowsQueue.flush()');
assert.strictEqual(rowsBody.expected_revisions['0'],b);
let batch;context.API.post=async(url,body)=>{if(url.endsWith('generate_batch')){batch=body;return {status:'started'};}throw Error(url);};
run('dsbStartPolling=()=>{}');
await context.dsbGenerateAll();
assert.deepStrictEqual(plain(batch.indices),[0,1],'both newly pending rows must be generated');
''')

    def test_refused_import_draft_stays_dirty_after_unrelated_delta(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64);
context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'},{text:'other',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64),'b'.repeat(64)],2)");
const bodies=[];context.API.post=async(url,body)=>{bodies.push({url,body});const e=Error('conflict');e.status=409;throw e;};
run("dsbRows=[{text:'imported first',emotion:'',seed:'',status:'pending'},{text:'imported second',emotion:'',seed:'',status:'pending'}];dsbSaveRows()");
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
assert.strictEqual(run("ensureDatasetRowSaveState('A').fullRevision!==null"),true,
 'a refused structural draft must stay dirty');
 run("dsbUpdateRow(0,'text','manually revised first')");
 await assert.rejects(run('dsbSaveRowsQueue.flush()'));
 assert.strictEqual(bodies.length,1,
  'the refused payload is never replayed by a later implicit flush');
 assert.deepStrictEqual(plain(run('dsbRows.map(r=>r.text)')),['manually revised first','imported second'],
  'both imported values stay visible until explicit recovery');
 assert.strictEqual(run('dsbSaveRowsQueue.isDirty()'),true);
 ''')

    def test_explicit_conflict_recovery_reloads_without_replaying_the_refused_write(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const posts=[],gets=[];
context.API.post=async(url,body)=>{posts.push(url);const e=Error('conflict');e.status=409;throw e;};
context.API.get=async(url)=>{gets.push(url);return {description:'root',global_seed:'',samples:[{text:'theirs',emotion:'',seed:'',status:'pending'}],row_revisions:['d'.repeat(64)],running:false,logs:[]};};
run("dsbRows=[{text:'imported',emotion:'',seed:'',status:'pending'}];dsbSaveRows()");
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
const banner=context.document.getElementById('dsb-project-load-status').innerHTML;
assert(banner.includes('Discard and reload'),'the conflict must expose an actionable recovery control');
assert(banner.includes('Copy draft for review'),'a copy path must keep the draft available');
await run('dsbDiscardDatasetDraftAndReload()');
assert.deepStrictEqual(plain(posts),['/api/dataset_builder/update_rows'],'recovery must not replay the refused write');
assert.deepStrictEqual(plain(gets),['/api/dataset_builder/status/A']);
assert.deepStrictEqual(plain(run('dsbRows.map(r=>r.text)')),['theirs']);
assert.strictEqual(run('dsbSaveRowsQueue.isDirty()'),false);
assert.strictEqual(context.document.getElementById('dsb-project-load-status').innerHTML,'');
''')

    def test_metadata_expectations_track_acknowledged_root_and_seed(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);
context.showToast=()=>{};context._toastSaveError=()=>{};
context.document.getElementById('dsb-description').value='root';
context.document.getElementById('dsb-global-seed').value='5';
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1);applyDatasetMetadataAck('A',{description:'root',global_seed:'5'})");
let metaBody,sampleBody;
context.API.post=async(url,body)=>{
 if(url.endsWith('update_meta')){metaBody=body;return {samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[b],description:'new root',global_seed:'9'};}
 if(url.endsWith('generate_sample')){sampleBody=body;return {row_revision:c,row:{text:'old',emotion:'',seed:'',status:'done'},audio_url:'/ok.wav'};}
 throw Error(url);};
context.document.getElementById('dsb-description').value='new root';
context.document.getElementById('dsb-global-seed').value='9';
context.dsbSaveForm();await run('dsbSaveMetaQueue.flush()');
assert.strictEqual(metaBody.expected_description,'root');
assert.strictEqual(metaBody.expected_global_seed,'5');
await context.dsbGenSample(0);
assert.strictEqual(sampleBody.expected_description,'new root','generation binds to the acknowledged new root');
assert.strictEqual(sampleBody.expected_global_seed,'9');
''')


if __name__ == '__main__':
    unittest.main()
