"""Native Node executes real loader/navigation, with controlled API responses."""
import subprocess
import unittest
from tests import test_voice_load_requests_js as loader_tests


class VoiceNavigationReuseJsTests(unittest.TestCase):
    def test_post_save_load_never_stamps_old_rows_with_new_revision(self):
        self.run_js(r'''
const c=client();c.ctx.currentBookFilename='book.json';c.ctx._bookSelectionEpoch=1;
const token='a'.repeat(64),book='b'.repeat(64);
c.snapshot={revision:'0'.repeat(64),book_token:book,voices:[{name:'Alice',config:{type:'custom',voice:'Old'}}]};
c.saveGate.resolve();
const prior=c.ctx.loadVoices(true);prior.catch(()=>{});await turn();
c.gates.get('/api/voice_config/snapshot').resolve(c.snapshot);await turn();await turn();
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',c.ctx),'0'.repeat(64));
assert.strictEqual(c.el('voices-list').innerHTML,'old cards','renderer must wait for held resources');
// Confirmed save ACK installs a NEW snapshot global while old load holds resources.
vm.runInContext('_voiceSaveSnapshot={revision:"1".repeat(64),book_token:"b".repeat(64),book_identity_token:"a".repeat(64),voices:[{name:"Alice",config:{type:"custom",voice:"New"}}],seed_changes:[]}',c.ctx);
c.snapshot={revision:'1'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'New'}}]};
c.ctx._selectedUploadIdentity={filename:'book.json',token};
const post=c.ctx.loadVoices(false,{expectedSelection:c.ctx._selectedUploadIdentity,expectedIdentityToken:token});
await turn();
c.resolveAll();
const priorResult=await prior.catch(e=>e);
// Cross-realm-safe check: prior either aborted with a marked obsolete
// error or was safely coalesced to a truly fresh result — the essential
// guarantee below is no stale render/new-stamp, not the error shape.
assert(priorResult?.voiceLoadObsolete===true || /superseded|Active book changed/.test(String(priorResult?.message||'')),
  'stale pre-save load must abort with a marked obsolete error, got: '+(priorResult?.message||priorResult));
await turn();await turn();await turn();await turn();
if(c.gates.get('/api/voice_config/snapshot')){c.gates.get('/api/voice_config/snapshot').resolve(c.snapshot);}
c.resolveAll();await turn();if(c.gates.get('/api/voice_config/snapshot')){c.gates.get('/api/voice_config/snapshot').resolve(c.snapshot);}c.resolveAll();
await post;
const html=c.el('voices-list').innerHTML;
// Drawn config must be New, never Old (card markup carries the voice).
const drawn=c.ctx._voicesByName.Alice.config.voice;
const drawnCards=c.draws.filter(v=>v.name==='Alice');
assert(html.includes('Alice'),'post-save load must render Alice rows');
assert(drawnCards.every(v=>v.config.voice!=='Old'),'no Old row may be drawn/published after ACK');
assert.strictEqual(vm.runInContext('_voiceCardsRevision',c.ctx),'1'.repeat(64),'new rows must be stamped with their own new revision');
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',c.ctx),'1'.repeat(64));
assert.strictEqual(drawn,'New','global voice must be New after post-save refresh');
// A subsequent ordinary load reuses the New cards (revision match)
// instead of re-rendering Old rows that a collector save could revert.
const drawsBefore=c.draws.length;
const again=c.ctx.loadVoices(false,{expectedSelection:c.ctx._selectedUploadIdentity,expectedIdentityToken:token});
await turn();if(c.gates.get('/api/voice_config/snapshot')){c.gates.get('/api/voice_config/snapshot').resolve(c.snapshot);}c.resolveAll();await again;
assert.strictEqual(c.draws.length,drawsBefore,'reused cards must stay New, draws='+c.draws.length);
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',c.ctx),'1'.repeat(64));
''')

    def test_snapshot_resolution_microtask_ack_never_labels_old_rows(self):
        self.run_js(r'''
const c=client();c.ctx.currentBookFilename='book.json';c.ctx._bookSelectionEpoch=1;
const token='a'.repeat(64),book='b'.repeat(64);c.ctx._selectedUploadIdentity={filename:'book.json',token};
Object.assign(c.ctx,{getVoiceListFocusSnapshot:()=>null,getVoicePanelSnapshot:()=>null,restoreVoicePanels:()=>{},restoreVoiceListFocus:()=>{}});
let post,scheduled=false;
c.ctx.renderVoiceDrafts=()=>{if(scheduled)return;scheduled=true;queueMicrotask(()=>{
 vm.runInContext('_voiceSaveSnapshot={revision:"1".repeat(64),book_token:"b".repeat(64),book_identity_token:"a".repeat(64),voices:[{name:"Alice",config:{type:"custom",voice:"New"}}],seed_changes:[]}',c.ctx);
 c.snapshot={revision:'1'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'New'}}]};
 post=c.ctx.loadVoices(false,{expectedSelection:c.ctx._selectedUploadIdentity,expectedIdentityToken:token});post.catch(()=>{});
});};
c.snapshot={revision:'0'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'Old'}}]};
c.saveGate.resolve();const prior=c.ctx.loadVoices(true);prior.catch(()=>{});await turn();
c.gates.get('/api/voice_config/snapshot').resolve(c.snapshot);await turn();
for(const [url,g] of c.gates){if(url!='/api/voice_config/snapshot')g.resolve(url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}
const outcome=await prior.catch(e=>e);
assert(outcome?.voiceLoadObsolete===true||/superseded|Active book changed/.test(String(outcome?.message||'')), 'prior must abort marked, got '+(outcome?.message||outcome));
for(let n=0;n<8;n++){await turn();for(const [url,g] of c.gates){g.resolve(url==='/api/voice_config/snapshot'?c.snapshot:url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
await post;
const html=c.el('voices-list').innerHTML;
const drawn=c.draws.filter(v=>v.name==='Alice').map(v=>v.config.voice);
assert(drawn.includes('New')&&!drawn.includes('Old'),'drawn config.voice must be New with zero Old draws, got '+drawn.join(','));
assert.strictEqual(vm.runInContext('_voiceCardsRevision',c.ctx),'1'.repeat(64));
assert.strictEqual(vm.runInContext('_voicesByName.Alice.config.voice',c.ctx),'New');
''')

    def test_pending_after_write_unscoped_fresh_load_fulfills_saved_new(self):
        self.run_js(r'''
const c=client();c.ctx.currentBookFilename='book.json';c.ctx._bookSelectionEpoch=1;
const token='a'.repeat(64),book='b'.repeat(64);
c.ctx._selectedUploadIdentity={filename:'book.json',token};
Object.assign(c.ctx,{getVoiceListFocusSnapshot:()=>null,getVoicePanelSnapshot:()=>null,restoreVoicePanels:()=>{},restoreVoiceListFocus:()=>{}});
c.saveGate.resolve();
const prior=c.ctx.loadVoices(true);prior.catch(()=>{});await turn();
const oldGates=new Map(c.gates);
oldGates.get('/api/voice_config/snapshot').resolve({revision:'0'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'Old'}}]});
await turn();await turn();
// ACK installs the new snapshot and bumps the queue revision.
c.ctx.newSnapshot={revision:'1'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'New'}}]};
vm.runInContext('_voiceSaveSnapshot=newSnapshot',c.ctx);
c.ctx.voiceSaveQueue.getRevision=()=>1;
const fresh=c.ctx.loadVoices(false);fresh.catch(()=>{});
for(const [url,g] of oldGates){if(url!='/api/voice_config/snapshot'){g.resolve(url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
for(let n=0;n<10;n++){await turn();for(const [url,g] of c.gates){g.resolve(url==='/api/voice_config/snapshot'?c.ctx.newSnapshot:url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
const [oldResult,newResult]=await Promise.allSettled([prior,fresh]);
assert.strictEqual(oldResult.status,'rejected','prior pre-write load must safely abort');
assert(oldResult.reason?.voiceLoadObsolete===true||/superseded|Active book changed/.test(String(oldResult.reason?.message||'')), 'prior abort must be marked obsolete, got '+(oldResult.reason?.message||oldResult.reason));
assert.strictEqual(newResult.status,'fulfilled','fresh post-write load must fulfill, not reuse the obsolete pre-write promise: '+(newResult.reason?.message||''));
assert(c.el('voices-list').innerHTML.includes('Alice:'),'fresh read must publish the saved New rows, got '+c.el('voices-list').innerHTML);
assert.strictEqual(vm.runInContext('_voicesByName.Alice.config.voice',c.ctx),'New','fresh read must publish the saved New voice');
assert.strictEqual(c.reads.filter(p=>p==='/api/voice_config/snapshot').length,2,'exactly two snapshot reads (prior + fresh), got '+c.reads.join(','));
assert.strictEqual(vm.runInContext('_voiceCardsRevision',c.ctx),'1'.repeat(64));
''')

    def test_waiter_secondary_pending_stale_after_write_falls_through_to_fresh(self):
        self.run_js(r'''
const c=client();c.ctx.currentBookFilename='book.json';c.ctx._bookSelectionEpoch=1;
const token='a'.repeat(64),book='b'.repeat(64);
c.ctx._selectedUploadIdentity={filename:'book.json',token};
Object.assign(c.ctx,{getVoiceListFocusSnapshot:()=>null,getVoicePanelSnapshot:()=>null,restoreVoicePanels:()=>{},restoreVoiceListFocus:()=>{}});
c.saveGate.resolve();
const prior=c.ctx.loadVoices(true);prior.catch(()=>{});await turn();
const oldGates=new Map(c.gates);
oldGates.get('/api/voice_config/snapshot').resolve({revision:'0'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'Old'}}]});
await turn();await turn();
// Waiter uses the sel:<token> identity form while the prior load used
// the ctx: form, so it cannot early-return the prior promise: it
// genuinely waits, then reaches the secondary pending-return branch.
const waiter=c.ctx.loadVoices(false,{expectedSelection:c.ctx._selectedUploadIdentity,expectedIdentityToken:token});waiter.catch(()=>{});await turn();
const second=c.ctx.loadVoices(true,{expectedSelection:c.ctx._selectedUploadIdentity,expectedIdentityToken:token});second.catch(()=>{});await turn();
c.ctx.newSnapshot={revision:'1'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'New'}}]};
vm.runInContext('_voiceSaveSnapshot=newSnapshot',c.ctx);
c.ctx.voiceSaveQueue.getRevision=()=>1;
c.snapshot=c.ctx.newSnapshot;
for(const [url,g] of oldGates){if(url!='/api/voice_config/snapshot'){g.resolve(url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
for(let n=0;n<10;n++){await turn();for(const [url,g] of c.gates){g.resolve(url==='/api/voice_config/snapshot'?c.ctx.newSnapshot:url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
await waiter;
assert(c.el('voices-list').innerHTML.includes('Alice:'),'waiter must publish the saved New rows, got '+c.el('voices-list').innerHTML);
assert.strictEqual(vm.runInContext('_voicesByName.Alice.config.voice',c.ctx),'New');
assert.strictEqual(c.reads.filter(p=>p==='/api/voice_config/snapshot').length,3,'prior + secondary + fresh snapshot reads, got '+c.reads.join(','));
''')

    def test_save_narrator_strategy_ack_old_load_never_publishes_new_revision(self):
        self.run_js(r'''
const c=client();c.ctx.currentBookFilename='book.json';c.ctx._bookSelectionEpoch=1;
const token='a'.repeat(64),book='b'.repeat(64);
c.ctx._selectedUploadIdentity={filename:'book.json',token};
Object.assign(c.ctx,{getVoiceListFocusSnapshot:()=>null,getVoicePanelSnapshot:()=>null,restoreVoicePanels:()=>{},restoreVoiceListFocus:()=>{}});
let toast=null;const actionErrors=[];
c.ctx.showToast=(message,kind)=>{toast=[message,kind];};
c.ctx.showActionError=(action,error)=>{actionErrors.push([action,String(error&&error.message||error)]);};
c.ctx.API.post=async(url,payload)=>{assert.strictEqual(url,'/api/narrator/strategy');assert.strictEqual(payload.book_token,book);return {revision:'1'.repeat(64)};};
c.snapshot={revision:'0'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'Old'}}]};
c.saveGate.resolve();
const prior=c.ctx.loadVoices(true);prior.catch(()=>{});await turn();
c.gates.get('/api/voice_config/snapshot').resolve(c.snapshot);await turn();await turn();
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',c.ctx),'0'.repeat(64));
// Extract the ACTUAL saveNarratorStrategy from the shipped source (the
// fixture slice stops before it); no fabricated copy.
vm.runInContext(source.slice(source.indexOf('window.saveNarratorStrategy'),source.indexOf('window.updateNarratorPreviewFields')),c.ctx);
assert.strictEqual(typeof c.ctx.saveNarratorStrategy,'function','actual saveNarratorStrategy must be extracted from app-core.js');
await c.ctx.saveNarratorStrategy('global');
assert.deepStrictEqual(toast,['Narrator strategy saved.','success']);
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',c.ctx),'1'.repeat(64),'actual ACK must install the acknowledged revision');
assert.deepStrictEqual(actionErrors,[]);
for(const [url,g] of c.gates){if(url!='/api/voice_config/snapshot'){g.resolve(url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
const priorResult=await prior.catch(e=>e);
assert(priorResult?.voiceLoadObsolete===true||/superseded|Active book changed/.test(String(priorResult?.message||'')), 'old load must abort marked obsolete after narrator ACK, got '+(priorResult?.message||priorResult));
assert.strictEqual(c.el('voices-list').innerHTML,'old cards','old rows must never publish under the newer revision');
assert.strictEqual(c.draws.length,0,'old rows must never be drawn after the ACK');
c.snapshot={revision:'1'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'New'}}]};
const follow=c.ctx.loadVoices(false);follow.catch(()=>{});await turn();
for(let n=0;n<8;n++){await turn();for(const [url,g] of c.gates){g.resolve(url==='/api/voice_config/snapshot'?c.snapshot:url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
await follow;
assert.strictEqual(vm.runInContext('_voiceCardsRevision',c.ctx),'1'.repeat(64));
assert.strictEqual(vm.runInContext('_voicesByName.Alice.config.voice',c.ctx),'New','follow-up reload must show the acknowledged New rows');
const drawn=c.draws.map(v=>v.config.voice);
assert(drawn.includes('New')&&!drawn.includes('Old'),'drawn voices must be New with zero Old, got '+drawn.join(','));
''')

    def test_refresh_local_epoch_switch_classifies_obsolete_before_server_mismatch(self):
        self.run_js(r'''
const c=client();c.ctx.currentBookFilename='book.json';c.ctx._bookSelectionEpoch=1;
const tokenA='a'.repeat(64),tokenB='c'.repeat(64),book='b'.repeat(64);
c.ctx._selectedUploadIdentity={filename:'book.json',token:tokenA};
vm.runInContext(`_voiceSaveSnapshot={revision:"${'0'.repeat(64)}",book_token:"${book}",book_identity_token:"${tokenA}",voices:[]}`,c.ctx);
c.saveGate.resolve();
const snapB={revision:'0'.repeat(64),book_token:book,book_identity_token:tokenB,voices:[]};
async function refreshHeld(opts){
  const p=c.ctx.refreshVoiceMetadata(opts);p.catch(()=>{});await turn();
  return {p,gate:c.gates.get('/api/voice_config/snapshot')};
}
// Unscoped binding: local epoch switch during GET + other-tab token
// snapshot must classify as local-obsolete, never visible server mismatch.
let h=await refreshHeld(undefined);
c.ctx._bookSelectionEpoch=2;
h.gate.resolve(snapB);
let e=await h.p.catch(err=>err);
assert(e?.voiceLoadObsolete===true,'unscoped local epoch switch must be marked obsolete before server comparison, got '+(e?.message||e));
// expectedIdentityToken-only binding: same ordering.
c.ctx._bookSelectionEpoch=1;
h=await refreshHeld({expectedIdentityToken:tokenA});
c.ctx._bookSelectionEpoch=2;
h.gate.resolve(snapB);
e=await h.p.catch(err=>err);
assert(e?.voiceLoadObsolete===true,'token-only local epoch switch must be marked obsolete before server comparison, got '+(e?.message||e));
// Real A->B local switch (filename AND epoch change) during GET must
// classify obsolete via the shared local guard, never the generic
// queue 'Voice edits changed' error.
c.ctx._bookSelectionEpoch=1;c.ctx.currentBookFilename='book.json';
h=await refreshHeld(undefined);
c.ctx.currentBookFilename='other.json';c.ctx._bookSelectionEpoch=2;
h.gate.resolve(snapB);
e=await h.p.catch(err=>err);
assert(e?.voiceLoadObsolete===true&&!/Voice edits changed/.test(String(e?.message||'')), 'filename+epoch switch must be marked obsolete, not a queue error, got '+(e?.message||e));
c.ctx.currentBookFilename='book.json';
// Stable-local control: same other-tab snapshot with no local switch
// remains a visible (unmarked) server mismatch.
c.ctx._bookSelectionEpoch=1;
h=await refreshHeld(undefined);
h.gate.resolve(snapB);
e=await h.p.catch(err=>err);
assert(e?.voiceLoadObsolete!==true&&/Active book changed while refreshing voices/.test(String(e?.message||'')), 'stable-local server mismatch must stay visible/unmarked, got '+(e?.message||e));
''')

    def test_narrator_ack_before_metadata_response_never_regresses_global(self):
        self.run_js(r'''
const c=client();c.ctx.currentBookFilename='book.json';c.ctx._bookSelectionEpoch=1;
const token='a'.repeat(64),book='b'.repeat(64);
c.ctx._selectedUploadIdentity={filename:'book.json',token};
Object.assign(c.ctx,{getVoiceListFocusSnapshot:()=>null,getVoicePanelSnapshot:()=>null,restoreVoicePanels:()=>{},restoreVoiceListFocus:()=>{}});
let toast=null;const actionErrors=[];
c.ctx.showToast=(message,kind)=>{toast=[message,kind];};
c.ctx.showActionError=(action,error)=>{actionErrors.push([action,String(error&&error.message||error)]);};
c.ctx.API.post=async(url,payload)=>{assert.strictEqual(url,'/api/narrator/strategy');return {revision:'1'.repeat(64)};};
vm.runInContext(`_voiceSaveSnapshot={revision:"${'0'.repeat(64)}",book_token:"${book}",book_identity_token:"${token}",voices:[{name:"Alice",config:{type:"custom",voice:"Old"}}]}`,c.ctx);
vm.runInContext(source.slice(source.indexOf('window.saveNarratorStrategy'),source.indexOf('window.updateNarratorPreviewFields')),c.ctx);
c.saveGate.resolve();
const prior=c.ctx.loadVoices(true);prior.catch(()=>{});await turn();
assert(c.reads.includes('/api/voice_config/snapshot'),'metadata GET must be in flight (held)');
// Actual narrator ACK during the held GET installs rev1 immutably.
await c.ctx.saveNarratorStrategy('global');
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',c.ctx),'1'.repeat(64));
assert.deepStrictEqual(toast,['Narrator strategy saved.','success']);
// Release the OLD rev0 GET response plus catalogs.
c.gates.get('/api/voice_config/snapshot').resolve({revision:'0'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'Old'}}]});
for(const [url,g] of c.gates){if(url!=='/api/voice_config/snapshot'){g.resolve(url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
const priorResult=await prior.catch(err=>err);
assert(priorResult?.voiceLoadObsolete===true||/superseded/.test(String(priorResult?.message||'')), 'held stale GET must abort marked obsolete, got '+(priorResult?.message||priorResult));
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',c.ctx),'1'.repeat(64),'acknowledged revision must be preserved, never regressed to rev0');
assert(vm.runInContext('_voiceCardsRevision',c.ctx)!=='0'.repeat(64),'card revision must not regress to rev0');
assert.strictEqual(c.el('voices-list').innerHTML,'old cards','old rows must never publish after the ACK');
assert.strictEqual(c.draws.length,0,'old rows must never be drawn');
assert.deepStrictEqual(actionErrors,[]);
// Follow-up fresh load works and shows the acknowledged state.
c.snapshot={revision:'1'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'New'}}]};
const follow=c.ctx.loadVoices(false);follow.catch(()=>{});await turn();
for(let n=0;n<8;n++){await turn();for(const [url,g] of c.gates){g.resolve(url==='/api/voice_config/snapshot'?c.snapshot:url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
await follow;
assert.strictEqual(vm.runInContext('_voiceCardsRevision',c.ctx),'1'.repeat(64));
assert.strictEqual(vm.runInContext('_voicesByName.Alice.config.voice',c.ctx),'New');
''')

    def test_fresh_read_before_old_get_resolves_after_narrator_ack_fulfills_new(self):
        self.run_js(r'''
const c=client();c.ctx.currentBookFilename='book.json';c.ctx._bookSelectionEpoch=1;
const token='a'.repeat(64),book='b'.repeat(64);
c.ctx._selectedUploadIdentity={filename:'book.json',token};
Object.assign(c.ctx,{getVoiceListFocusSnapshot:()=>null,getVoicePanelSnapshot:()=>null,restoreVoicePanels:()=>{},restoreVoiceListFocus:()=>{}});
c.ctx.showToast=()=>{};c.ctx.showActionError=()=>{};
c.ctx.API.post=async(url,payload)=>{assert.strictEqual(url,'/api/narrator/strategy');return {revision:'1'.repeat(64)};};
c.ctx.initial={revision:'0'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'Old'}}]};
vm.runInContext('_voiceSaveSnapshot=initial',c.ctx);
vm.runInContext(source.slice(source.indexOf('window.saveNarratorStrategy'),source.indexOf('window.updateNarratorPreviewFields')),c.ctx);
c.saveGate.resolve();
const prior=c.ctx.loadVoices(true);prior.catch(()=>{});await turn();
await c.ctx.saveNarratorStrategy('global');
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',c.ctx),'1'.repeat(64));
// Fresh same-key read BEFORE the old GET resolves: it must not adopt
// the provably obsolete pending (pending provenance was bound at
// pending-load creation, so the ACK makes it stale for early reuse).
const fresh=c.ctx.loadVoices(false);fresh.catch(()=>{});
for(const [url,g] of c.gates){g.resolve(url==='/api/voice_config/snapshot'?c.ctx.initial:url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}
const priorResult=await prior.catch(err=>err);
assert(priorResult?.voiceLoadObsolete===true||/superseded/.test(String(priorResult?.message||'')), 'prior must abort marked obsolete, got '+(priorResult?.message||priorResult));
for(let n=0;n<10;n++){await turn();for(const [url,g] of c.gates){g.resolve(url==='/api/voice_config/snapshot'?{revision:'1'.repeat(64),book_token:book,book_identity_token:token,voices:[{name:'Alice',config:{type:'custom',voice:'New'}}]}:url==='/api/voice_library'?{count:12}:[{id:'catalog'}]);}}
await fresh;
assert.strictEqual(c.reads.filter(p=>p==='/api/voice_config/snapshot').length,2,'fresh read must issue its own second GET, got '+c.reads.join(','));
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',c.ctx),'1'.repeat(64));
assert.strictEqual(vm.runInContext('_voiceCardsRevision',c.ctx),'1'.repeat(64));
assert.strictEqual(vm.runInContext('_voicesByName.Alice.config.voice',c.ctx),'New','fresh read must publish the acknowledged New rows');
const drawn=c.draws.map(v=>v.config.voice);
assert(drawn.includes('New')&&!drawn.includes('Old'),'drawn voices must be New with zero Old, got '+drawn.join(','));
''')

    def test_pending_navigation_shares_startup_load_but_explicit_refresh_stays_fresh(self):
        self.run_js(r'''
const c=client();const startup=c.ctx.loadVoices();const navigation=c.ctx.loadVoices(false);await turn();assert.strictEqual(c.reads.length,0);c.saveGate.resolve();await turn();assert.strictEqual(c.reads.length,5,'pending navigation must share the startup fetches');c.resolveAll();await Promise.all([startup,navigation]);assert.strictEqual(c.draws.length,1);
c.reads.length=0;const forced=c.ctx.loadVoices();await turn();assert.strictEqual(c.reads.length,5);c.resolveAll();await forced;
c.reads.length=0;c.ctx.voiceSaveQueue.flush=()=>Promise.reject(Error('save failed'));await assert.rejects(c.ctx.loadVoices(false),/save failed/);c.ctx.voiceSaveQueue.flush=()=>Promise.resolve();await finish(c,true);assert.strictEqual(c.reads.length,5,'rejected pending promise must clear for recovery');
''')

    def run_js(self, body):
        code = loader_tests.SETUP + r"""
async function finish(c, force=false){const promise=c.ctx.loadVoices(force);await turn();c.resolveAll();await promise;}
let finished=false;process.on('beforeExit',()=>assert(finished,'all assertions must finish'));
""" + '\n(async()=>{\n' + body + '\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', code, str(loader_tests.SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_navigation_reuses_resources_and_cards_but_checks_metadata_and_expires(self):
        self.run_js(r"""
const c=client();let clock=0;c.ctx.performance.now=()=>clock;c.saveGate.resolve();
await finish(c);assert.strictEqual(c.reads.length,5);assert.strictEqual(c.draws.length,1);
const cards=c.el('voices-list').innerHTML;
for(let i=0;i<3;i++){c.reads.length=0;clock+=1000;await finish(c);assert.deepStrictEqual(c.reads,['/api/voice_config/snapshot']);assert.strictEqual(c.draws.length,1);assert.strictEqual(c.el('voices-list').innerHTML,cards);}
c.reads.length=0;clock=10000;await finish(c);assert.strictEqual(c.reads.length,5);assert.strictEqual(c.draws.length,2);
c.reads.length=0;await finish(c,true);assert.strictEqual(c.reads.length,5);assert.strictEqual(c.draws.length,3,'mutation callers force fresh dropdowns/cards within TTL');
""")

    def test_changed_revision_or_book_renders_even_with_fresh_resources(self):
        self.run_js(r"""
const c=client();c.saveGate.resolve();await finish(c);
c.snapshot.revision='1'.repeat(64);c.snapshot.voices=[{name:'Bob',config:{type:'custom',voice:'Ryan'}}];c.reads.length=0;await finish(c);assert.strictEqual(c.reads.length,1);assert.strictEqual(c.draws.length,2);assert(c.el('voices-list').innerHTML.includes('Bob:'));
c.snapshot.book_token='c'.repeat(64);c.snapshot.voices=[{name:'Carol',config:{type:'custom',voice:'Ryan'}}];c.reads.length=0;await finish(c);assert.strictEqual(c.reads.length,1);assert.strictEqual(c.draws.length,3);assert(c.el('voices-list').innerHTML.includes('Carol:'));
""")

    def test_partial_resource_failure_retries_and_save_guards_survive_reuse(self):
        self.run_js(r"""
const c=client();c.saveGate.resolve();let pending=c.ctx.loadVoices(false);await turn();c.gates.get('/api/voice_design/list').reject(Error('optional failed'));c.resolveAll();await pending;assert.strictEqual(c.draws.length,1);
c.reads.length=0;await finish(c);assert.strictEqual(c.reads.length,5,'optional failure must not cache degraded resources');
const cards=c.el('voices-list').innerHTML;c.reads.length=0;c.ctx.voiceSaveQueue.flush=()=>Promise.reject(Error('unsaved edits'));await assert.rejects(c.ctx.loadVoices(false),/unsaved edits/);assert.strictEqual(c.reads.length,0);assert.strictEqual(c.el('voices-list').innerHTML,cards);
c.ctx.voiceSaveQueue.flush=()=>Promise.resolve();pending=c.ctx.loadVoices(false);await turn();c.setDirty();c.resolveAll();await assert.rejects(pending,/Voice edits changed/);assert.strictEqual(c.el('voices-list').innerHTML,cards);
""")

    def test_real_navigation_requests_reuse_and_regular_call_still_forces(self):
        self.run_js(r"""
const c=client(),loads=[];let handler;
const link={dataset:{tab:'voices'},classList:{remove(){},add(){}},removeAttribute(){},setAttribute(){},addEventListener(name,fn){assert.strictEqual(name,'click');handler=fn;}};
c.ctx.document.querySelectorAll=selector=>selector==='.nav-link'?[link]:[{style:{}}];c.ctx.document.getElementById=id=>({style:{},classList:{contains:()=>false}});c.ctx.rememberTab=()=>{};c.ctx.loadVoices=(...args)=>loads.push(args);
c.ctx.window=c.ctx;c.ctx.location={hash:'#setup'};c.ctx.history={pushState:(_s,_t,hash)=>c.ctx.location.hash=hash};c.ctx.addEventListener=()=>{};
const a=source.indexOf('const TAB_STORAGE_KEY =');const b=source.indexOf('// --- LLM model picker:',a);assert(a>=0&&b>a);vm.runInContext(source.slice(a,b),c.ctx);
handler({preventDefault(){},currentTarget:link});assert.strictEqual(loads.length,1);assert.strictEqual(loads[0][0],false,'navigation must request refreshResources=false (reuse)');assert(loads[0][1]==null,'ordinary navigation passes neutral absent/null options');
""")
