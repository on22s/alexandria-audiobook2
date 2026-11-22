"""Qualified-base-model discriminators for #2224 draft and structural saves."""
import unittest

from tests import test_dataset_ui_ownership_js as ownership


class Ds2224DraftBaseJsTests(unittest.TestCase):
    run_scenario = ownership.DatasetUiOwnershipJsTests.run_scenario

    def test_refused_full_draft_keeps_its_original_base_across_meta_and_flush(self):
        self.run_scenario(r'''
const a='a'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const sent=[];
context.API.post=async(url,body)=>{
 if(url.endsWith('update_rows')){sent.push({kind:'rows',count:body.expected_count,rev:body.expected_revisions['0']});
  const error=Error('conflict');error.status=409;throw error;}
 if(url.endsWith('update_meta')){sent.push({kind:'meta',count:body.expected_count,rev:body.expected_revisions['0']});
  const error=Error('conflict');error.status=409;throw error;}
 throw Error(url);};
context.dsbAddRow('','mine');
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
context.document.getElementById('dsb-description').value='new root';context.dsbSaveForm();
await assert.rejects(run('dsbSaveMetaQueue.flush()'));
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
assert.deepStrictEqual(plain(sent),[
 {kind:'rows',count:1,rev:a},{kind:'meta',count:1,rev:a},{kind:'rows',count:1,rev:a}]);
assert.deepStrictEqual(plain(run('dsbRows.map(r=>r.text)')),['old','mine'],
 'the refused draft stays visible and recoverable');
''')

    def test_focused_clean_row_after_external_poll_keeps_its_old_edit_base(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
const handlers={};context._startPolling=(key,fetch,h)=>{Object.assign(handlers,h);return()=>{};};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'done',audio_url:'/ok.wav'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
context.dsbStartPolling('A');
const active={tagName:'TEXTAREA',value:'old'};context.document.activeElement=active;
const tbody=context.document.getElementById('dsb-table-body');
tbody.children=[{contains(node){return node===active;}}];
handlers.onTick({running:false,samples:[{text:'external',emotion:'',seed:'',status:'pending'}],row_revisions:[b],logs:[]});
assert.strictEqual(run('dsbRows[0].text'),'old','a focused clean control keeps its displayed value');
assert.strictEqual(run("ensureDatasetRowSaveState('A').tokens[0]"),a,'no external token adoption');
let sent;context.API.post=async(url,body)=>{sent=body;return {row_revisions:{0:b}};};
run("dsbUpdateRow(0,'text','old suffix')");
await run('dsbSaveRowsQueue.flush()');
assert.strictEqual(sent.edits[0].expected_revision,a,'a stale typed draft must fail its CAS, not overwrite external');
''')

    def test_verified_empty_base_sends_zero_count_and_empty_revision_map(self):
        self.run_scenario(r'''
context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[];applyDatasetRowRevisions('A',[],0)");
let sent;context.API.post=async(url,body)=>{sent=body;const error=Error('conflict');error.status=409;throw error;};
context.dsbAddRow();
await assert.rejects(run('dsbSaveRowsQueue.flush()'));
assert.strictEqual(sent.expected_count,0,'[] is a verified empty base, not missing revision support');
assert.deepStrictEqual(plain(sent.expected_revisions),{});
''')

    def test_two_own_appends_advance_base_count_and_revisions(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'one',emotion:'',seed:'',status:'pending'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
const bodies=[];let firstCall,release;
const called=new Promise(resolve=>firstCall=resolve);
context.API.post=async(url,body)=>{bodies.push(body);if(bodies.length===1){firstCall();await new Promise(y=>release=y);return {row_revisions:[a,b]};}return {row_revisions:[a,b,c]};};
context.dsbAddRow('','two');const first=run('dsbSaveRowsQueue.flush()');await called;
context.dsbAddRow('','three');release();await first;await run('dsbSaveRowsQueue.flush()');
assert.deepStrictEqual(plain(bodies.map(body=>body.expected_count)),[1,2],
 'the second append must use the count and revisions returned by its own predecessor');
assert.deepStrictEqual(plain(bodies[1].expected_revisions),{'0':a,'1':b});
''')

    def test_own_metadata_invalidation_advances_a_pending_full_draft(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'done',audio_url:'/old.wav'}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
let metaBody,rowsBody,releaseMeta;
const gate=new Promise(resolve=>releaseMeta=resolve);
context.API.post=async(url,body)=>{
 if(url.endsWith('update_meta')){metaBody=body;await gate;
  return {samples:[{text:'old',emotion:'',seed:'',status:'pending',audio_url:null}],row_revisions:[b]};}
 if(url.endsWith('update_rows')){rowsBody=body;return {row_revisions:[b,c]};}
 throw Error(url);};
context.document.getElementById('dsb-description').value='new root';context.dsbSaveForm();const meta=run('dsbSaveMetaQueue.flush()');
while(!metaBody){await new Promise(resolve=>setImmediate(resolve));}
context.dsbAddRow('','ours');
assert.strictEqual(metaBody.expected_revisions['0'],a,'metadata carries the same acknowledged base');
releaseMeta();await meta;await run('dsbSaveRowsQueue.flush()');
assert.strictEqual(rowsBody.expected_revisions['0'],b,
 'the append must use the revision from its own metadata predecessor');
assert.strictEqual(rowsBody.expected_count,1);
''')

    def test_committed_writer_order_beats_enqueue_order(self):
        self.run_scenario(r'''
const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);context.showToast=()=>{};context._toastSaveError=()=>{};
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'first',emotion:'',seed:'',status:'pending'},{text:'second',emotion:'',seed:'',status:'done'}];applyDatasetRowRevisions('A',['a'.repeat(64),'b'.repeat(64)],2)");
let giveMeta;
context.API.post=async(url,body)=>{
 if(url.endsWith('edit_rows')){return {row_revisions:{1:c}};}
 if(url.endsWith('update_meta')){return new Promise(resolve=>giveMeta=resolve);}
 throw Error(url);};
context.document.getElementById('dsb-description').value='new root';context.dsbSaveForm();
context.dsbUpdateRow(1,'text','edited');
await run('dsbSaveRowsQueue.flush()');
const meta=run('dsbSaveMetaQueue.flush()');
while(typeof giveMeta!=='function'){await new Promise(resolve=>setImmediate(resolve));}
giveMeta({samples:[{text:'first',emotion:'',seed:'',status:'pending'},{text:'second',emotion:'',seed:'',status:'pending'}],row_revisions:[a,c]});
await meta;
assert.deepStrictEqual(plain(run('dsbRows.map(r=>r.status)')),['pending','pending'],
 'a metadata write committed last carries the authoritative after-state');
assert.deepStrictEqual(plain(run("ensureDatasetRowSaveState('A').tokens")),[a,c]);
''')

    def test_status_audio_cells_repaint_while_a_live_input_is_protected(self):
        self.run_scenario(r'''
run(source.slice(source.indexOf('function dsbRepaintRowStatus('),source.indexOf('window.dsbUpdateRow')));
const active={tagName:'TEXTAREA',value:'old'};context.document.activeElement=active;
const tbody=context.document.getElementById('dsb-table-body');
const cellNames=['index','emotion','text','seed','status','actions'];
const fresh=cellNames.map(name=>({name}));
const old=cellNames.map(name=>({name:'old '+name,
 replaceWith(cell){const from=fresh.indexOf(cell);assert(from>=0,'replacement must come from the new row');
  fresh.splice(from,1); old[old.indexOf(this)]=cell;}}));
const attrs={'data-dsb-status':'done','data-dsb-audio':'/old.wav'};
tbody.children=[{children:old,getAttribute(name){return attrs[name];},
 setAttribute(name,value){attrs[name]=value;},contains(node){return node===active;}}];
context.document.createElement=()=>({firstElementChild:{children:fresh}});
run("dsbCurrentProject='A';dsbRows=[{text:'old',emotion:'',seed:'',status:'pending',audio_url:null}];applyDatasetRowRevisions('A',['a'.repeat(64)],1)");
run('dsbRenderTable([0])');
assert.strictEqual(attrs['data-dsb-status'],'pending');
assert.strictEqual(attrs['data-dsb-audio'],'');
assert.deepStrictEqual(old.map(cell=>cell.name),['old index','old emotion','old text','old seed','status','actions'],
 'repaint the status and actions without detaching the live input');
''')


if __name__ == '__main__':
    unittest.main()
