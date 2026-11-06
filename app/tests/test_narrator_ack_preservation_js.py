"""Acknowledged narrator strategy survives ordinary card saves (#1724 A2).

Real saveNarratorStrategy -> collectVoiceConfig -> enqueueVoiceDraft ->
flushVoiceSaves against the single-voice harness's mocked transport;
only the narrator POST acknowledgement and the voice-config save
application are simulated server-side. The ordinary collector must omit
narrator_strategy so the guarded save's exclude-unset merge preserves
the server-acknowledged value.
"""
import unittest


SETUP_NARRATOR = r'''
serverVoices=[{name:'NARRATOR',config:{type:'custom',voice:'Serena',seed:'7',description:'Synthetic narrator.',narrator_strategy:'global'},persona_pending:false}];
await context.refreshVoiceMetadata();
vm.runInContext(core.slice(core.indexOf('window.saveNarratorStrategy ='),core.indexOf('window.updateNarratorPreviewFields =')),context);
const previousPost=context.API.post;
let narratorHold=null,narratorFailures=0;
context.API.post=async(url,body)=>{
 if(url==='/api/narrator/strategy'){
  if(narratorFailures>0){narratorFailures--;const err=new Error('narrator refused');err.status=400;err.detail='narrator refused';throw err;}
  if(narratorHold){await narratorHold.promise;}
  serverVoices=serverVoices.map(row=>({...row,config:{...row.config,narrator_strategy:body.strategy}}));serverRevision++;
  return{strategy:body.strategy,revision:revision(serverRevision)};
 }
 return previousPost(url,body);
};
function narratorCard(style){
 return {dataset:{voice:'NARRATOR',version:''},querySelector(sel){
  const fields={'.alias-select':{value:''},'.voice-type:checked':{value:'custom'},'.voice-select':{value:'Serena'},'.character-style':{value:style},'.voice-ready':{checked:false},'.voice-seed':{value:'7'}};
  return fields[sel]||null;
 }};
}
function collectNarrator(style){
 document.querySelectorAll=selector=>selector==='.voice-card'?[narratorCard(style)]:[];
 return vm.runInContext('collectVoiceConfig()',context);
}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};}
'''


class NarratorAckPreservationJsTests(unittest.TestCase):
    def run_node(self, body):
        # Local import: a module-level TestCase import would make the
        # unittest loader rediscover the whole single-voice suite here.
        from tests.test_single_voice_js import SingleVoiceJsTests
        return SingleVoiceJsTests().run_node(SETUP_NARRATOR + body)

    def test_ordinary_edit_after_ack_preserves_strategy(self):
        r = self.run_node(r'''
await context.window.saveNarratorStrategy('chapter');
assert.strictEqual(context.actionErrors.length,0);
assert.strictEqual(serverVoices[0].config.narrator_strategy,'chapter','actual handler must first save requested strategy');
context.__collected=collectNarrator('steady');
assert(!('narrator_strategy' in context.__collected.NARRATOR),'ordinary collector must omit the ACK-owned strategy field');
vm.runInContext('enqueueVoiceDraft(__collected)',context);await vm.runInContext('flushVoiceSaves()',context);
assert.strictEqual(serverVoices[0].config.narrator_strategy,'chapter','ordinary save must preserve the acknowledged strategy');
''')
        self.assertEqual(1, len(r['writes']))

    def test_card_edit_while_post_held_preserves_acknowledged_strategy(self):
        r = self.run_node(r'''
narratorHold=deferred();
const saving=context.window.saveNarratorStrategy('chapter');saving.catch(()=>{});
await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
assert.strictEqual(serverVoices[0].config.narrator_strategy,'global','POST must still be held');
context.__collected=collectNarrator('calm');
assert(!('narrator_strategy' in context.__collected.NARRATOR),'in-flight draft must omit the strategy field');
vm.runInContext('enqueueVoiceDraft(__collected)',context);
narratorHold.resolve();narratorHold=null;
await saving;
await vm.runInContext('flushVoiceSaves()',context);
assert.strictEqual(context.actionErrors.length,0);
assert.strictEqual(serverVoices[0].config.narrator_strategy,'chapter','draft captured during POST must not revert the ACK');
''')
        self.assertEqual(1, len(r['writes']))

    def test_failed_narrator_save_then_ordinary_save_keeps_server_strategy(self):
        r = self.run_node(r'''
narratorFailures=1;
await context.window.saveNarratorStrategy('chapter');
assert(context.actionErrors.length>=1,'definite narrator refusal must surface');
assert.strictEqual(serverVoices[0].config.narrator_strategy,'global','definite refusal commits nothing');
context.__collected=collectNarrator('steady');
vm.runInContext('enqueueVoiceDraft(__collected)',context);await vm.runInContext('flushVoiceSaves()',context);
assert.strictEqual(serverVoices[0].config.narrator_strategy,'global','ordinary save must not fabricate the failed strategy');
''')
        self.assertEqual(1, len(r['writes']))

    def test_newer_strategy_ack_then_ordinary_save_preserves_newer(self):
        r = self.run_node(r'''
await context.window.saveNarratorStrategy('chapter');
assert.strictEqual(serverVoices[0].config.narrator_strategy,'chapter');
await context.window.saveNarratorStrategy('focus');
assert.strictEqual(serverVoices[0].config.narrator_strategy,'focus','newer explicit ACK must win');
context.__collected=collectNarrator('steady');
vm.runInContext('enqueueVoiceDraft(__collected)',context);await vm.runInContext('flushVoiceSaves()',context);
assert.strictEqual(serverVoices[0].config.narrator_strategy,'focus','ordinary save must preserve the newer acknowledged strategy');
''')
        self.assertEqual(1, len(r['writes']))

    def test_book_switch_before_send_retains_draft_with_no_write(self):
        r = self.run_node(r'''
await context.window.saveNarratorStrategy('chapter');
assert.strictEqual(serverVoices[0].config.narrator_strategy,'chapter');
context.__collected=collectNarrator('steady');
vm.runInContext('enqueueVoiceDraft(__collected)',context);
// Book switch: the snapshot now belongs to another book, so the
// queued draft's pre-send guard must refuse as not-sent (no write,
// no replay, draft retained for explicit recovery).
vm.runInContext('_voiceSaveSnapshot={..._voiceSaveSnapshot,book_token:"c".repeat(64)}',context);
const outcome=await vm.runInContext('flushVoiceSaves()',context).then(()=>({status:'fulfilled'}),e=>({status:'rejected',message:e.message,notSent:e.voiceSaveNotSent===true}));
assert.strictEqual(outcome.status,'rejected','book-switched draft must not be sent');
assert.strictEqual(serverVoices[0].config.narrator_strategy,'chapter','no write may occur after a book switch');
''')
        self.assertEqual(0, len(r['writes']))

    def test_invalid_ack_revision_is_unconfirmed_not_success(self):
        r = self.run_node(r'''
let serverStrategy='global';
context.API.post=async(url,body)=>{
 if(url==='/api/narrator/strategy'){
  serverStrategy=body.strategy; // server actually commits...
  return{strategy:body.strategy,revision:'not-a-revision'}; // ...but the ACK is malformed
 }
 return previousPost(url,body);
};
const beforeRevision=vm.runInContext('_voiceSaveSnapshot.revision',context);
await context.window.saveNarratorStrategy('chapter');
assert.strictEqual(serverStrategy,'chapter','transport case: server committed before the bad ACK');
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',context),beforeRevision,'bad ACK must invent no revision');
assert(context.actionErrors.length>=1,'unconfirmed outcome must surface honestly');
assert(/could not be confirmed/i.test(context.actionErrors[0][1]),'message must say unconfirmed, got '+context.actionErrors[0][1]);
assert(/may have been saved/i.test(context.actionErrors[0][1]),'unconfirmed message must retain that the server may have committed, got '+context.actionErrors[0][1]);
assert.strictEqual(context.toasts.filter(t=>/saved/i.test(t[0])).length,0,'bad ACK must never toast validated success');
''')
        self.assertEqual(0, len(r['writes']))

    def test_array_revision_ack_is_unconfirmed_not_success(self):
        r = self.run_node(r'''
let serverStrategy='global';
context.API.post=async(url,body)=>{
 if(url==='/api/narrator/strategy'){
  serverStrategy=body.strategy; // server actually commits...
  return{strategy:body.strategy,revision:['a'.repeat(64)]}; // ...but revision is an ARRAY (regex coercion trap)
 }
 return previousPost(url,body);
};
const beforeRevision=vm.runInContext('_voiceSaveSnapshot.revision',context);
await context.window.saveNarratorStrategy('chapter');
assert.strictEqual(serverStrategy,'chapter','transport case: server committed before the array ACK');
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.revision',context),beforeRevision,'array revision must never be installed');
assert(context.actionErrors.length>=1,'unconfirmed outcome must surface honestly');
assert(/could not be confirmed/i.test(context.actionErrors[0][1]),'message must say unconfirmed, got '+context.actionErrors[0][1]);
assert(/may have been saved/i.test(context.actionErrors[0][1]),'unconfirmed message must retain that the server may have committed, got '+context.actionErrors[0][1]);
assert.strictEqual(context.toasts.filter(t=>/saved/i.test(t[0])).length,0,'array ACK must never toast validated success');
''')
        self.assertEqual(0, len(r['writes']))

    def test_preflight_not_sent_is_reported_not_sent_not_unknown(self):
        r = self.run_node(r'''
// Dirty draft enqueued, then the snapshot token changes (book switch)
// before the preflight flush: the canonical queue write guard refuses
// as voiceSaveNotSent (provably no request sent).
context.__collected=collectNarrator('steady');
vm.runInContext('enqueueVoiceDraft(__collected)',context);
vm.runInContext('_voiceSaveSnapshot={..._voiceSaveSnapshot,book_token:"c".repeat(64)}',context);
const previewPostsBefore=writes.length;
context.elements['single-voice-source'].value='NARRATOR';
await context.previewSingleVoiceBook();
assert(context.actionErrors.length>=1,'preflight failure must surface');
const message=context.actionErrors[0][1];
assert(/not sent/i.test(message),'UI must say not sent, got '+message);
assert(!/may have been saved/i.test(message),'not-sent must never claim maybe-saved/unknown');
assert.strictEqual(writes.length,previewPostsBefore,'no voice-config save POST may occur');
''')
        self.assertEqual(0, len(r['writes']))


if __name__ == '__main__':
    unittest.main()
