"""Owned local-obsolete refresh presented neutrally after a committed
action; HTTP errors stay visible; server mismatch names book recovery."""
import subprocess
import unittest

from tests import test_voice_load_requests_js as loader_tests


class VoiceRefreshUxJsTests(unittest.TestCase):
    def run_js(self, body):
        code = loader_tests.SETUP + r"""
let finished=false;process.on('beforeExit',()=>assert(finished,'all assertions must finish'));
""" + '\n(async()=>{\n' + body + '\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', code, str(loader_tests.SOURCE)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_committed_select_then_superseded_reload_is_warning_not_failure(self):
        self.run_js(r'''
const c=client();
vm.runInContext(source.slice(source.indexOf('function showToast('),source.indexOf('function showConfirm(')),c.ctx);
const toasts=[];c.ctx.showToast=(message,type)=>toasts.push([message,type]);
const posts=[];c.ctx.API.post=async(url,body)=>{posts.push(url);return {};};
vm.runInContext(source.slice(source.indexOf('window.selectVoiceVersion ='),source.indexOf('window.addVoiceVersion =')),c.ctx);
c.ctx._voiceCardsBookToken='b'.repeat(64);
c.ctx.loadVoices=async()=>{throw Object.assign(new Error('A newer voice load superseded this request; existing edits were preserved.'),{voiceLoadObsolete:true});};
await c.ctx.selectVoiceVersion({value:'v1',closest:()=>({dataset:{voice:'Hero'}})});
assert.strictEqual(posts.length,1,'committed POST must happen exactly once (no automatic repeat), got '+posts.join(','));
assert.strictEqual(toasts.length,1,'exactly one presenter toast expected, got '+JSON.stringify(toasts));
assert.strictEqual(toasts[0][1],'warning','owned local-obsolete refresh must present as warning, got '+toasts[0][1]);
assert(/superseded/i.test(toasts[0][0]),'neutral message must name supersession, got '+toasts[0][0]);
assert(/Check current Voices before repeating/i.test(toasts[0][0]),'must instruct checking Voices before repeating, got '+toasts[0][0]);
assert(!/failed/i.test(toasts[0][0]),'must carry NO failure label (incl. the caller action prefix), got '+toasts[0][0]);
assert(!/could not/i.test(toasts[0][0]),'must carry NO could-not failure label, got '+toasts[0][0]);
assert(!/could not be confirmed/i.test(toasts[0][0]) && !/may have been saved/i.test(toasts[0][0]),'must not invent an unknown save outcome, got '+toasts[0][0]);
''')

    def test_http_status_error_with_marker_stays_visible_error(self):
        self.run_js(r'''
const c=client();
vm.runInContext(source.slice(source.indexOf('function showToast('),source.indexOf('function showConfirm(')),c.ctx);
const toasts=[];c.ctx.showToast=(message,type)=>toasts.push([message,type]);
const httpError=Object.assign(new Error('request exploded'),{voiceLoadObsolete:true,status:500});
c.ctx.showActionError('Version selection failed',httpError,'Reload Voices to check.');
assert.strictEqual(toasts.length,1);
assert.strictEqual(toasts[0][1],'error','HTTP-status error must stay visible/danger even with a marker, got '+toasts[0][1]);
assert(/failed/i.test(toasts[0][0]) && /request exploded/i.test(toasts[0][0]),'normal visible failure must retain detail, got '+toasts[0][0]);
''')

    def test_save_ack_style_supersession_uses_same_neutral_presenter(self):
        self.run_js(r'''
const c=client();
vm.runInContext(source.slice(source.indexOf('function showToast('),source.indexOf('function showConfirm(')),c.ctx);
const toasts=[];c.ctx.showToast=(message,type)=>toasts.push([message,type]);
// Stale-read supersession from a save/ACK snapshot change (no newer load).
const ackError=Object.assign(new Error('A newer voice save superseded this refresh; existing edits were preserved.'),{voiceLoadObsolete:true});
c.ctx.showActionError('Applying cast voices failed',ackError,'Reload Voices and review.');
assert.strictEqual(toasts.length,1);
assert.strictEqual(toasts[0][1],'warning','ACK-style supersession must present as warning, got '+toasts[0][1]);
assert(/superseded by a newer local change/i.test(toasts[0][0]),'neutral ACK-style message expected, got '+toasts[0][0]);
assert(!/failed/i.test(toasts[0][0]) && !/could not/i.test(toasts[0][0]),'no failure label of any kind, got '+toasts[0][0]);
assert(!/may have been saved/i.test(toasts[0][0]),'no unknown-save claim, got '+toasts[0][0]);
''')

    def test_server_mismatch_names_explicit_book_recovery(self):
        self.run_js(r'''
const c=client();c.ctx.currentBookFilename='book.json';c.ctx._bookSelectionEpoch=1;
const tokenA='a'.repeat(64),tokenB='c'.repeat(64),book='b'.repeat(64);
c.ctx._selectedUploadIdentity={filename:'book.json',token:tokenA};
vm.runInContext(`_voiceSaveSnapshot={revision:"${'0'.repeat(64)}",book_token:"${book}",book_identity_token:"${tokenA}",voices:[]}`,c.ctx);
c.saveGate.resolve();
const p=c.ctx.refreshVoiceMetadata();p.catch(()=>{});await turn();
c.gates.get('/api/voice_config/snapshot').resolve({revision:'0'.repeat(64),book_token:book,book_identity_token:tokenB,voices:[]});
const e=await p.catch(err=>err);
assert(e?.voiceLoadObsolete!==true,'stable-local server mismatch must stay unmarked/visible');
assert(/Active book changed while refreshing voices/.test(String(e?.message||'')), 'mismatch message must remain recognizable, got '+(e?.message||e));
assert(/Load the book again from Scripts or re-upload its source/.test(String(e?.message||'')), 'mismatch must explicitly name book reload/reselect recovery, got '+(e?.message||e));
''')
