"""Attempt-identity and revision-settling discriminators for #2224 recovery."""
import unittest

from tests import test_dataset_ui_ownership_js as ownership


class Ds2224AttemptIdentityJsTests(unittest.TestCase):
    run_scenario = ownership.DatasetUiOwnershipJsTests.run_scenario

    def test_discard_installs_controls_and_expectations_from_one_snapshot(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};context.showConfirm=async()=>true;
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1);applyDatasetMetadataAck('A',{description:'root',global_seed:''})");
context.document.getElementById('dsb-description').value='root';context.document.getElementById('dsb-global-seed').value='31';context.dsbSaveForm();
context.API.post=async()=>{const e=Error('conflict');e.status=409;throw e;};
await assert.rejects(run('dsbSaveMetaQueue.flush()'));
context.API.get=async()=>({description:'external voice',global_seed:'77',samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[a],running:false,logs:[]});
context.API.post=async(url,body)=>{body0=body;return {samples:[],row_revisions:[a],description:body.description,global_seed:body.global_seed};};
var body0;await run('dsbDiscardDatasetDraftAndReload()');
assert.strictEqual(run("document.getElementById('dsb-description').value"),'external voice');
assert.deepStrictEqual(plain(run("datasetExpectedMetadata('A')")),
 {expected_description:'external voice',expected_global_seed:'77'},'discard retires the stale metadata draft');
assert.strictEqual(run("ensureDatasetRowSaveState('A').metadataDraft"),null);
context.document.getElementById('dsb-global-seed').value='45';context.dsbSaveForm();
await run('dsbSaveMetaQueue.flush()');
assert.strictEqual(body0.description,'external voice','a later seed edit cannot restore the abandoned root');
assert.strictEqual(body0.expected_description,'external voice');
''')

    def test_return_invalidates_a_held_retry_response_before_and_after_autosave(self):
        for release_after_save in (False, True):
            with self.subTest(release_after_save=release_after_save):
                self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};context.showConfirm=async()=>true;
const afterSave=''' + ('true' if release_after_save else 'false') + r''';
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
let first=true,release;const held=new Promise(resolve=>release=resolve);context.API.get=async()=>{
 if(first){first=false;const e=Error('corrupt');e.status=500;throw e;}
 await held;return {description:'root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[a],running:false,logs:[]};};
run("dsbRows=[{text:'mine',emotion:'',seed:'',status:'pending'}];dsbSaveRows()");
let posts=[];let refuse=true;context.API.post=async(url,body)=>{posts.push(url);if(refuse){const e=Error('conflict');e.status=409;throw e;}return {row_revisions:{0:a}};};
await assert.rejects(run('dsbSaveRowsQueue.flush()'));refuse=false;
await run('dsbDiscardDatasetDraftAndReload()');
const retry=run('dsbRetryDatasetReload()');await new Promise(resolve=>setImmediate(resolve));
await run('dsbReturnToDatasetDraft()');await run('dsbKeepDatasetDraft()');
run("dsbRows[0].text='after return';dsbSaveRows()");
await run('dsbSaveRowsQueue.flush()');
release();await retry;await new Promise(resolve=>setImmediate(resolve));
if(afterSave){await run('dsbSaveRowsQueue.flush()');}
assert.strictEqual(run('dsbRows[0].text'),'after return','the held Retry must not wipe the returned draft');
assert.strictEqual(run('dsbSaveRowsQueue.isDirty()'),false);
''')

    def test_return_keeps_the_latch_until_keep_editing(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};context.showConfirm=async()=>true;
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const posts=[];let refuse=true;context.API.post=async(url,body)=>{posts.push(url);if(refuse){const e=Error('conflict');e.status=409;throw e;}return {status:'started'};};
context.API.get=async()=>{const e=Error('corrupt');e.status=500;throw e;};
run("dsbRows=[{text:'mine',emotion:'',seed:'',status:'pending'}];dsbSaveRows()");
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
await run('dsbDiscardDatasetDraftAndReload()');
await run('dsbReturnToDatasetDraft()');
assert.strictEqual(run("ensureDatasetRowSaveState('A').awaitingReplacement"),false);
assert.strictEqual(run("ensureDatasetRowSaveState('A').conflictLatch"),true,'Return alone must not release the latch');
const before=posts.length;
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
const select=context.document.getElementById('dsb-project-select');select.value='B';await context.dsbOnProjectChange();
await context.dsbGenerateAll();
assert.strictEqual(posts.length,before,'no implicit mutation until Keep editing');
await run('dsbKeepDatasetDraft()');
assert.strictEqual(run("ensureDatasetRowSaveState('A').conflictLatch"),false);
''')

    def test_keep_editing_unblocks_saves_while_discard_is_held(self):
        self.run_scenario(r'''
const token = 'a'.repeat(64);
context.showConfirm = async () => true;
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'draft',emotion:'',seed:'',status:'pending'}]");
let release;
context.API.get = async () => new Promise(resolve => { release = resolve; });
const pending = run('dsbDiscardDatasetDraftAndReload()');
await new Promise(resolve => setImmediate(resolve));
assert.strictEqual(run("ensureDatasetRowSaveState('A').awaitingReplacement"), true);
await run('dsbKeepDatasetDraft()');
assert.strictEqual(run("ensureDatasetRowSaveState('A').awaitingReplacement"), false);
release({description:'peer', samples:[{text:'discarded'}], row_revisions:[token]});
await pending;
assert.strictEqual(run('dsbRows[0].text'), 'draft');
let saved;
context.API.post = async (url, body) => {
    saved = body.rows[0].text;
    return {status:'ok', row_revisions:[token]};
};
run("dsbRows[0].text='still editing';dsbSaveRows()");
await run('dsbSaveRowsQueue.flush()');
assert.strictEqual(saved, 'still editing');
''')

    def test_older_full_ack_keeps_a_newer_voice_draft(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
context.document.getElementById('dsb-description').value='root';
let release;const held=new Promise(resolve=>release=resolve);let n=0;
context.API.post=async(url,body)=>{
 if(url.endsWith('update_rows')){if(++n===1){await held;return {samples:[{text:'old',emotion:'',seed:'',status:'pending'},{text:'second',emotion:'',seed:'',status:'pending'}],row_revisions:[a,b],description:'root',global_seed:''};}}
 return {samples:[],row_revisions:[c],description:body.description,global_seed:body.global_seed};};
context.dsbAddRow('','second');const first=run('dsbSaveRowsQueue.flush()');
context.document.getElementById('dsb-description').value='typed new voice';context.dsbSaveForm();
release();await first;await run('dsbSaveMetaQueue.flush()');
assert.strictEqual(run("document.getElementById('dsb-description').value"),'typed new voice');
assert.strictEqual(run("ensureDatasetRowSaveState('A').metadata.description"),'typed new voice');
''')

    def test_older_full_ack_settles_only_its_revision_and_keeps_newer_append(self):
        self.run_scenario(r"""
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'},{text:'second',emotion:'',seed:'',status:'pending'},{text:'my newer third',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64),'b'.repeat(64),'c'.repeat(64)],3);ensureDatasetRowSaveState('A').sequence=0;");
let posted;context.API.post=async(url,body)=>{posted=body;return {sample_count:2,description:'root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'pending'},{text:'second',emotion:'',seed:'',status:'pending'}],row_revisions:[a,b]};};
run('dsbSaveRows()');run("ensureDatasetRowSaveState('A').fullRevision=2");await run('dsbSaveRowsQueue.flush()');
assert.deepStrictEqual(plain(run('dsbRows.map(r=>r.text)')),['old','second','my newer third'],'a newer queued append stays visible');
assert.strictEqual(run("ensureDatasetRowSaveState('A').fullRevision"),2,'the newer structural draft is not retired by the older ACK');
assert.deepStrictEqual(plain(run("ensureDatasetRowSaveState('A').tokens")),[a,b],'only the acknowledged revisions advance');
let payload;context.Blob=class{constructor(parts){payload=JSON.parse(parts[0]);}};context.URL={createObjectURL:()=>'/blob',revokeObjectURL(){}};context.document.createElement=()=>({click(){}});
run('dsbCopyDatasetDraft()');
assert.deepStrictEqual(payload.rows.map(r=>r.text),['old','second','my newer third']);
assert.strictEqual(posted.rows.length,3,'Copy draft matches the refused structural payload');
""")

    def test_stale_terminal_get_leaves_all_row_components_untouched(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
let fetchFn;const handlers={};context._startPolling=(key,fetch,h)=>{fetchFn=fetch;Object.assign(handlers,h);return()=>{};};
context.API.get=async()=>({description:'root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'done',audio_url:'/old.wav'}],row_revisions:[a],running:false,logs:[]});
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1);applyDatasetMetadataAck('A',{description:'root',global_seed:''});dsbStartPolling('A')");
const oldGet=fetchFn();
context.API.post=async()=>({samples:[{text:'old',emotion:'',seed:'',status:'pending',audio_url:null}],row_revisions:[b],description:'new root',global_seed:''});
context.document.getElementById('dsb-description').value='new root';context.dsbSaveForm();await run('dsbSaveMetaQueue.flush()');
handlers.onTick(await oldGet);
assert.strictEqual(run('dsbRows[0].status'),'pending','the stale GET must not restore audio or a done badge');
assert.strictEqual(run('dsbRows[0].audio_url'),null);
assert.deepStrictEqual(plain(run("ensureDatasetRowSaveState('A').tokens")),[b]);
context.API.get=async()=>({description:'new root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'done',audio_url:'/fresh.wav'}],row_revisions:[c],running:false,logs:[]});
handlers.onTick(await fetchFn());
assert.strictEqual(run('dsbRows[0].audio_url'),'/fresh.wav','a fresh terminal update still applies');
''')

    def test_second_failed_discard_still_offers_both_recovery_choices(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};context.showConfirm=async()=>true;
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
let fail=true;context.API.get=async()=>{if(fail){const e=Error('corrupt');e.status=500;throw e;}
 return {description:'root',global_seed:'',samples:[{text:'old',emotion:'',seed:'',status:'pending'}],row_revisions:[a],running:false,logs:[]};};
run("dsbRows=[{text:'mine',emotion:'',seed:'',status:'pending'}];dsbSaveRows()");
const refuse=async()=>{context.API.post=async()=>{const e=Error('conflict');e.status=409;throw e;};await assert.rejects(run('dsbSaveRowsQueue.flush()'));};
await refuse();await run('dsbDiscardDatasetDraftAndReload()');
fail=false;await run('dsbRetryDatasetReload()');
assert.strictEqual(run('dsbSaveRowsQueue.isDirty()'),false);
fail=true;run("dsbRows=[{text:'imported',emotion:'',seed:'',status:'pending'}];dsbSaveRows()");await refuse();await run('dsbDiscardDatasetDraftAndReload()');
const banner=context.document.getElementById('dsb-project-load-status').innerHTML;
assert(banner.includes('dsbRetryDatasetReload()'),'Retry must be offered again');
assert(banner.includes('dsbReturnToDatasetDraft()'),'Return to draft must be offered again');
assert.strictEqual(run("ensureDatasetRowSaveState('A').awaitingReplacement"),true);
''')


if __name__ == '__main__':
    unittest.main()
