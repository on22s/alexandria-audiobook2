"""Production cleanup handlers preserve operation ownership and truthful outcomes."""
import unittest

from tests import test_clone_import_lifecycle_js


PREPARE = r'''
ctx.AbortController=class{constructor(){this.signal={};}abort(){}};
let prepares=0,accepts=0,deletes=0;
ctx.createAudioChannelPicker=()=>({getSelection:()=>({channel:'mix',token:'b'.repeat(32)}),dispose(){}});
const prepared=()=>({cleanup_token:'a'.repeat(32),plan:{operations:[{op:'gain',value:1}]},audio_channel:'mix',measures:{},candidate_measures:{}});
const prepareReply=()=>({ok:true,json:async()=>prepared()});
const openReady=()=>{ctx._openCloneImportModal(file('cleanup.wav'));el('clone-import-ref-text').value='Exact transcript';el('clone-import-rights-confirmed').checked=true;};
const listen=()=>{el('clone-cleanup-listened-original').checked=true;el('clone-cleanup-listened-candidate').checked=true;};
'''


class CleanupUiReviewTests(unittest.TestCase):
    def run_js(self, code):
        test_clone_import_lifecycle_js.CloneImportLifecycleJsTests.run_js(self, PREPARE + code)

    def test_plain_import_blocks_cleanup_and_unlocks_controls_on_refusal(self):
        self.run_js(r'''
openReady();const importing=el('clone-import-submit').onclick();
await el('clone-cleanup-prepare').onclick();
assert.strictEqual(uploads.length,1);assert.strictEqual(el('clone-cleanup-fields').disabled,true);
complete(0,false);await importing;assert.strictEqual(el('clone-cleanup-fields').disabled,false);
''')

    def test_prepare_blocks_plain_import_and_duplicate_prepares(self):
        self.run_js(r'''
let finish;ctx.fetch=async(url,options)=>{if(url.endsWith('/prepare')){prepares++;return await new Promise(resolve=>{finish=resolve;});}throw Error('overlapping request '+url);};
openReady();const preparing=el('clone-cleanup-prepare').onclick();
await el('clone-import-submit').onclick();await el('clone-cleanup-prepare').onclick();
assert.strictEqual(prepares,1);assert.strictEqual(uploads.length,0);assert.strictEqual(el('clone-import-submit').disabled,true);
finish(prepareReply());await preparing;assert.strictEqual(el('clone-import-submit').disabled,false);
''')

    def test_prepare_catches_rejected_channel_stage(self):
        self.run_js(r'''
ctx.createAudioChannelPicker=()=>({getSelection:()=>({channel:'mix',pending:Promise.reject(Error('stage unavailable'))}),dispose(){}});
openReady();await el('clone-cleanup-prepare').onclick();
assert(toasts.some(item=>item[0].includes('preparation failed')));assert.strictEqual(el('clone-import-submit').disabled,false);
''')

    def test_failed_reprepare_does_not_leave_old_verification_armed(self):
        self.run_js(r'''
let posts=0;ctx.fetch=async(url,options)=>{if(options.method==='DELETE'){return {ok:true};}posts++;return posts===1?prepareReply():{ok:false,status:400,json:async()=>({detail:'candidate refused'})};};
openReady();await el('clone-cleanup-prepare').onclick();listen();await el('clone-cleanup-prepare').onclick();
assert.strictEqual(el('clone-cleanup-players').hidden,true);assert.strictEqual(el('clone-cleanup-summary').textContent,'');
assert(!el('clone-import-status').textContent.includes('prepared and verified'));assert.strictEqual(el('clone-cleanup-accept').disabled,true);
''')

    def test_reprepare_discards_previous_session_before_post(self):
        self.run_js(r'''
const calls=[];ctx.fetch=async(url,options)=>{calls.push(options.method+' '+url);if(options.method==='DELETE'){deletes++;return {ok:true};}return prepareReply();};
openReady();await el('clone-cleanup-prepare').onclick();listen();await el('clone-cleanup-prepare').onclick();
assert.strictEqual(deletes,1);assert(calls[1].startsWith('DELETE '));assert(calls[2].endsWith('/prepare'));
assert.strictEqual(el('clone-cleanup-listened-original').checked,false);assert.strictEqual(el('clone-cleanup-listened-candidate').checked,false);
''')

    def test_discard_reply_cannot_reset_a_new_modal(self):
        self.run_js(r'''
let finish;ctx.fetch=async(url,options)=>options.method==='DELETE'?await new Promise(resolve=>{finish=resolve;}):prepareReply();
openReady();await el('clone-cleanup-prepare').onclick();const discarding=el('clone-cleanup-reject').onclick();const finishOld=finish;
hidden();openReady();await el('clone-cleanup-prepare').onclick();
finishOld({ok:true});await discarding;assert.strictEqual(el('clone-cleanup-players').hidden,false);
''')

    def test_discard_is_truthful_about_already_accepted_voices(self):
        self.run_js(r'''
ctx.fetch=async(url,options)=>options.method==='DELETE'?{ok:true}:prepareReply();
openReady();await el('clone-cleanup-prepare').onclick();await el('clone-cleanup-reject').onclick();
assert(el('clone-import-status').textContent.includes('already accepted voices remain'));assert(!el('clone-import-status').textContent.includes('Candidate rejected'));
''')

    def test_lost_accept_reply_retires_preview_and_refreshes_with_warning(self):
        self.run_js(r'''
let lists=0;ctx.API.get=async()=>{lists++;return [];};
ctx.fetch=async(url,options)=>{if(url.endsWith('/accept')){accepts++;throw Error('connection lost after request');}return prepareReply();};
openReady();await el('clone-cleanup-prepare').onclick();listen();await el('clone-cleanup-accept').onclick();
assert.strictEqual(lists,1);assert.strictEqual(el('clone-cleanup-players').hidden,true);await el('clone-cleanup-accept').onclick();assert.strictEqual(accepts,1);
assert(toasts.some(item=>item[0].includes('may have completed')&&item[0].includes('Do not')));
''')

    def test_success_with_unreadable_receipt_is_acknowledged_not_retried(self):
        self.run_js(r'''
ctx.fetch=async(url,options)=>url.endsWith('/accept')?{ok:true,json:async()=>{throw Error('lost receipt');}}:prepareReply();
openReady();await el('clone-cleanup-prepare').onclick();listen();await el('clone-cleanup-accept').onclick();
assert.strictEqual(el('clone-cleanup-players').hidden,true);assert.strictEqual(refreshes,1);
assert(toasts.some(item=>item[0].includes('accepted as a new voice')));
''')

    def test_terminal_refusal_retires_candidate_but_busy_preserves_it(self):
        self.run_js(r'''
let status=503;ctx.fetch=async(url,options)=>url.endsWith('/accept')?{ok:false,status,json:async()=>({detail:'fixture refusal'})}:prepareReply();
openReady();await el('clone-cleanup-prepare').onclick();listen();await el('clone-cleanup-accept').onclick();
assert.strictEqual(el('clone-cleanup-players').hidden,false);status=400;await el('clone-cleanup-accept').onclick();
assert.strictEqual(el('clone-cleanup-players').hidden,true);assert(!el('clone-import-status').textContent.includes('prepared and verified'));
''')

    def test_edit_invalidation_clears_verified_status_and_unknown_dc_is_labelled(self):
        self.run_js(r'''
ctx.fetch=async()=>prepareReply();openReady();await el('clone-cleanup-prepare').onclick();
assert(el('clone-cleanup-summary').textContent.includes('DC removal unknown'));
el('clone-cleanup-gain').onchange();assert(!el('clone-import-status').textContent.includes('prepared and verified'));
''')
