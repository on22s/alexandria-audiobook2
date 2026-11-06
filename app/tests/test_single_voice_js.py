"""Single-voice action executes the REAL canonical queue + guide JS.

Harness loads the actual slices of app-core.js (createSerializedSaveQueue,
enqueueVoiceDraft/flushVoiceSaves/_voiceSaveSnapshot, refreshVoiceMetadata)
and the actual app-single-voice.js. Only the HTTP transport (API.get/post)
and the DOM are mocked -- never the queue/helper contract.
"""
from pathlib import Path
import subprocess
import unittest

CORE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
VOICE = Path(__file__).resolve().parent.parent / 'static/js/app-single-voice.js'


class SingleVoiceJsTests(unittest.TestCase):
    def run_node(self, scenario_js):
        code = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const core=fs.readFileSync(process.argv[1],'utf8'),voice=fs.readFileSync(process.argv[2],'utf8');
const helper=core.slice(core.indexOf('function createSerializedSaveQueue('),core.indexOf('// --- API Helpers ---'));
const saves=core.slice(core.indexOf('let _voiceStatusClearTimer ='),core.indexOf('// Auto-save on any change inside the voices list'));
const refresh=core.slice(core.indexOf('async function refreshVoiceMetadata()'),core.indexOf('let _voiceResourcesRefreshedAt'));
const literalStart=core.indexOf('function _isLiteralSourcePreservingChecked()');
assert.notStrictEqual(literalStart,-1,'canonical literal accessor must exist in app-core.js');
const literalEnd=core.indexOf('\n        function ',literalStart+10);
const literalSlice=core.slice(literalStart,literalEnd);
// Real loaders/renderer/collector for DS058/DS021: no mock call-count proof.
const rosterSlice=core.slice(core.indexOf('const emptyVoiceRoster'),core.indexOf('function ensureVoiceRosterOptions('));
const metaSlice=core.slice(core.indexOf('function getTraitBadgeHtml('),core.indexOf('function getStateVoiceCardsMarkup('));
const cardsSlice=core.slice(core.indexOf('function getStateVoiceCardsMarkup('),core.indexOf('function getSavedAdapterOption('));
const collectSlice=core.slice(core.indexOf('function collectVoiceConfig('),core.indexOf('function onVoiceReadyChange('));
const voiceStateSlice=core.slice(core.indexOf('let _voiceResourcesRefreshedAt'),core.indexOf('function getVoiceSeedRepairMarkup('));
const loadVoicesSlice=core.slice(core.indexOf('async function loadVoices('),core.indexOf('window.selectVoiceVersion'));
function makeElement(id){return {id,children:[],checked:false,value:'',textContent:'',listeners:{},
 appendChild(node){this.children.push(node);return node;},
 replaceChildren(...nodes){this.children=[...nodes];
  if(this.value && !this.children.some(child=>child.value===this.value)){this.value=this.children[0]?.value ?? '';}},
 addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);},
 fire(type){for(const fn of (this.listeners[type] || [])){fn();}},remove(){},}}
const elements={};
for(const id of ['source-preview-text','source-preview-meta','source-preview-status','source-preview-confirm','single-voice-source','script-literal-source-preserving','voice-save-status','voice-save-drafts','voices-list']){
  elements[id]=makeElement(id);
}
const document={getElementById:id=>elements[id]||null,querySelectorAll:()=>[],
 createElement:tag=>makeElement('option-'+tag)};
const revision=n=>String(n).padStart(64,'0'),book='b'.repeat(64),snapshotBookToken='e'.repeat(64),otherIdentity='d'.repeat(64),otherSnapshot='c'.repeat(64);
const customAsset=v=>({type:'custom',voice:v,ref_audio:null,ref_text:null,adapter_id:null,adapter_path:null,members:null});const cloneAsset=(a,tx)=>({type:'clone',voice:null,ref_audio:a,ref_text:tx,adapter_id:null,adapter_path:null,members:null});const loraAsset=(id,pp)=>({type:'lora',voice:null,ref_audio:null,ref_text:null,adapter_id:id,adapter_path:pp,members:null});let serverRevision=0, writes=[], previewPosts=0, snapshotGets=0, confirmAnswer=true, confirmCalls=0;
let serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',seed:'7',description:'Calm synthetic narrator.'},persona_pending:false},{name:'BOB',config:{type:'custom',voice:'Ryan',seed:'-1',description:'Calm synthetic narrator.'},persona_pending:false}];
let previewError=null, saveError=null, previewSha='a'.repeat(64);
let snapshotError=null, saveHook=null, previewVoicesOverride=null, previewSpeakersOverride=null, previewTextOverride=null;
const context={currentBookFilename:'book.txt',_bookSelectionEpoch:1,document,elements,window:{addEventListener(){},_bookSelectionEpoch:1,
  localStorage:{setItem(){},getItem(){return null;},removeItem(){}},_loraModelsCache:[],_cloneVoicesCache:[],_designedVoicesCache:[],_voicesNames:[],_voicesByName:{},_lineCounts:null,_voiceSuggestions:null},
 escapeHtml:String,showToast:(...args)=>{context.toasts.push(args);},confirmMessages:[],showConfirm:async message=>{confirmCalls++;context.confirmMessages.push(message);return confirmAnswer;},
 showActionError:(...args)=>{context.actionErrors.push(args);},toasts:[],actionErrors:[],isEditorEditPending:()=>false,
 setTimeout:()=>1,clearTimeout(){},AbortController,console:{error(){}},performance:{now:()=>Date.now()},AVAILABLE_VOICES:[],
 getLibraryVoiceReference:()=>null,getTraitBadgeHtml:()=>'',getVoiceCandidateMarkup:()=>'',renderStyleTimeline:()=>'',getSavedAdapterOption:()=>'',disableStaleCardControls:s=>s,getLoraModelsById:()=>new Map(),loadCastLibrary:async()=>{},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},saveVoicesDebounced(){},renderVoiceSuggestions(){},getVoicePanelSnapshot:()=>({bookToken:null,panels:[]}),restoreVoicePanels(){},getVoiceListFocusSnapshot:()=>null,restoreVoiceListFocus(){},getVoiceSeedRepairMarkup:()=>'',renderPersonaRecoveryTargets(){},applyTtsCapabilityGuidance(){},
 ensureVoiceRosterCache(){},renderVoiceDrafts(){},
 API:{get:async(url,options)=>{if(url==='/api/single-narrator/source-preview'){return {book_token:book,source_sha256:previewSha,source_name:'book.txt',encoding:'utf-8',total_chars:10,truncated:false,text_start:previewTextOverride ?? 'Synthetic text.'};}
   if(url==='/api/voice_config/snapshot'){snapshotGets++;if(snapshotError){throw snapshotError;}return {revision:revision(serverRevision),book_token:snapshotBookToken,book_id:'book',book_identity_token:book,config:{},voices:serverVoices,seed_changes:[]};}
   if(url==='/api/voice_design/list'||url==='/api/clone_voices/list'||url==='/api/lora/models'){return [];}
   throw new Error('unexpected GET '+url);},
  post:async(url,body)=>{if(url==='/api/voice_config/single-voice-preview'){previewPosts++;if(previewError){throw previewError;}
     const speakers=previewSpeakersOverride ?? [{name:'ALICE',line_count:1,changes:[]},{name:'BOB',line_count:1,changes:['voice changed','ready reset (needs new listening approval)']}];
     return {book_token:snapshotBookToken,revision:body.revision,source_speaker:body.source_speaker,affected_speakers:speakers.length,affected_lines:speakers.reduce((sum,row)=>sum+row.line_count,0),
      speakers,
      voices:previewVoicesOverride ?? {ALICE:{type:'custom',voice:'Serena',seed:'7',ready:false},BOB:{type:'custom',voice:'Serena',seed:'-1',ready:false}}};}
   if(url==='/api/voice_config/save'){writes.push(JSON.parse(JSON.stringify(body)));if(saveHook){saveHook();}if(saveError){
     const definite=saveError && (saveError.status===400||saveError.status===409||saveError.status===422);
     if(!definite){serverVoices=serverVoices.map(row=>{const patch=body.voices[row.name];return patch?{...row,config:{...row.config,...patch}}:row;});serverRevision++;}
     throw saveError;}
     serverVoices=serverVoices.map(row=>{const patch=body.voices[row.name];return patch?{...row,config:{...row.config,...patch}}:row;});
     serverRevision++;return {book_token:snapshotBookToken,revision:revision(serverRevision)};}
   throw new Error('unexpected POST '+url);}}};
vm.createContext(context);
vm.runInContext(helper+saves+refresh+literalSlice+rosterSlice+metaSlice+cardsSlice+collectSlice+voiceStateSlice+loadVoicesSlice+voice,context);
context.elements=elements;context.document=document;
context.voiceSaveQueue=vm.runInContext('voiceSaveQueue',context);
context.window._bookSelectionEpoch=1;
let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish'));
(async()=>{
// Central roster assertion (#1724 C094): every scenario starts from the
// canonical Voices load, which must populate the select with real options.
await context.refreshVoiceMetadata();
assert(context.elements['single-voice-source'].children.some(child=>child.value==='ALICE'),'ALICE must be a populated option before selection');
''' + scenario_js + r'''
finished=true;console.log(JSON.stringify({writes,previewPosts,snapshotGets,confirmCalls,toasts:context.toasts.length,actionErrors:context.actionErrors.length}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(CORE), str(VOICE)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        import json as _json
        return _json.loads(result.stdout.strip().splitlines()[-1])

    def test_source_confirmation_binds_and_clears(self):
        self.run_node(r'''
await context.loadSingleVoiceSourcePreview();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false);
context.elements['source-preview-confirm'].checked=true;context.onSingleVoiceSourceConfirm();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),true);
// Real literal-mode input change clears attestation via the actual listener.
context.elements['script-literal-source-preserving'].checked=true;
context.elements['script-literal-source-preserving'].fire('change');
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false,'literal change must clear confirmation');
assert.strictEqual(context.elements['source-preview-confirm'].checked,false);
// Source change (new sha) also clears on the next load; book change and A->B->A reset.
context.elements['script-literal-source-preserving'].checked=false;
await context.loadSingleVoiceSourcePreview();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false,'re-confirm is required after a literal clear');
context.elements['source-preview-confirm'].checked=true;context.onSingleVoiceSourceConfirm();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),true);
previewSha='c'.repeat(64);
await context.loadSingleVoiceSourcePreview();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false,'changed source must not keep confirmation');
previewSha='a'.repeat(64);
await context.loadSingleVoiceSourcePreview();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false,'reloaded source needs new confirmation');
context.elements['source-preview-confirm'].checked=true;context.onSingleVoiceSourceConfirm();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),true);
context.currentBookFilename='other.txt';
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false,'book change must not keep confirmation');
context.window.resetSingleVoiceBook();
context.currentBookFilename='book.txt';
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false,'A->B->A must not revive confirmation');
''')

    def test_voices_load_populates_speaker_select(self):
        # The canonical Voices load (refreshVoiceMetadata, called by
        # loadVoices on tab activation) must populate the single-voice
        # select with the active speakers before any click. Book reset
        # clears it back to the placeholder.
        self.run_node(r'''
const select = context.elements['single-voice-source'];
// The harness already ran the canonical load centrally; the select must
// hold the active speakers before any click.
assert.deepEqual(select.children.map(child => child.value), ['', 'ALICE', 'BOB'],
  'canonical Voices load must populate the select with active speakers');
select.value = 'ALICE';
await context.refreshVoiceMetadata();
assert.strictEqual(select.value, 'ALICE', 'existing selection is preserved across reloads');
context.window.resetSingleVoiceBook();
assert.deepEqual(select.children.map(child => child.value), [''], 'book reset clears the select');
assert.strictEqual(select.value, '');
''')

    def test_preview_cancel_sends_no_bulk_write(self):
        out = self.run_node(r'''
await context.refreshVoiceMetadata();
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=false;
await context.previewSingleVoiceBook();
assert.strictEqual(previewPosts,1);
assert.strictEqual(writes.length,0,'cancel must send 0 bulk writes');
''')
        self.assertEqual(0, len(out['writes']))
        self.assertEqual(1, out['previewPosts'])

    def test_preview_confirm_applies_once_through_canonical_queue(self):
        out = self.run_node(r'''
await context.refreshVoiceMetadata();
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
await context.previewSingleVoiceBook();
assert.strictEqual(confirmCalls,1);
assert.strictEqual(writes.length,1,'exactly one canonical queue save');
assert.strictEqual(writes[0].voices.BOB.voice,'Serena');
assert.strictEqual(writes[0].voices.BOB.ready,false);
''')
        self.assertEqual(1, len(out['writes']))

    def test_newly_pending_edits_during_modal_refuse_without_flushing(self):
        out = self.run_node(r'''
await context.refreshVoiceMetadata();
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
context.showConfirm=async()=>{vm.runInContext('enqueueVoiceDraft({ALICE:{type:"custom",voice:"Serena",seed:"7"}})',context);return true;};
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0,'newly arrived edits must not be flushed by the bulk action');
assert.strictEqual(context.actionErrors.length,1);
''')
        self.assertEqual(0, len(out['writes']))

    def test_preview_failure_sends_no_save(self):
        out = self.run_node(r'''
previewError=new Error('preview refused');
await context.refreshVoiceMetadata();
context.elements['single-voice-source'].value='ALICE';
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0);
assert.strictEqual(context.actionErrors.length,1);
''')
        self.assertEqual(0, len(out['writes']))

    def test_book_change_during_modal_refuses(self):
        out = self.run_node(r'''
await context.refreshVoiceMetadata();
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
context.showConfirm=async()=>{context.currentBookFilename='other.txt';return true;};
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0,'book change must refuse the stale preview');
assert.strictEqual(context.actionErrors.length,1);
''')
        self.assertEqual(0, len(out['writes']))

    def test_source_preview_pending_fetch_discarded_on_book_switch(self):
        # A GET that resolves after a book switch (or A->B->A, same filename,
        # bumped epoch) must not install the old book's preview, and a later
        # confirmation must not bind that stale preview to the current book.
        self.run_node(r'''
const originalGet = context.API.get; let releasePreview = null;
context.API.get = url => url === '/api/single-narrator/source-preview'
  ? new Promise(resolve => { releasePreview = () => resolve({book_token:book,source_sha256:previewSha,source_name:'book.txt',encoding:'utf-8',total_chars:10,truncated:false,text_start:'Synthetic text.'}); })
  : originalGet(url);
const pendingLoad = context.loadSingleVoiceSourcePreview();
context.currentBookFilename='other.txt'; context.window._bookSelectionEpoch=2;
context.window.resetSingleVoiceBook();
releasePreview(); await pendingLoad;
assert.strictEqual(context.elements['source-preview-text'].textContent,'No source preview loaded for this book.','stale preview must not be installed into the new book');
context.elements['source-preview-confirm'].checked=true; context.onSingleVoiceSourceConfirm();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false,'stale preview must not confirm for the new book');
// A->B->A: filename returns, epoch does not.
context.currentBookFilename='book.txt'; context.window._bookSelectionEpoch=3;
const pendingLoad2 = context.loadSingleVoiceSourcePreview();
context.currentBookFilename='other.txt'; context.window._bookSelectionEpoch=4;
context.window.resetSingleVoiceBook();
context.currentBookFilename='book.txt'; context.window._bookSelectionEpoch=5;
context.window.resetSingleVoiceBook();
releasePreview(); await pendingLoad2;
assert.strictEqual(context.elements['source-preview-text'].textContent,'No source preview loaded for this book.','A->B->A must not install the pre-switch preview');
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false);
''')

    def test_source_status_describes_snapshot_not_live_state(self):
        self.run_node(r'''
await context.loadSingleVoiceSourcePreview();
assert(context.elements['source-preview-status'].textContent.includes('Loaded excerpt snapshot not confirmed'),'unconfirmed status must name the snapshot');
context.elements['source-preview-confirm'].checked=true;context.onSingleVoiceSourceConfirm();
assert(context.elements['source-preview-status'].textContent.includes('Loaded excerpt snapshot confirmed'),'confirmed status must name the snapshot');
''')

    def test_apply_saved_then_refresh_fails_reports_saved_truthfully(self):
        out = self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
// The bulk save succeeds; the snapshot refresh after it fails. The
// pre-preview refresh (get 1) still succeeds.
let gets=0; const originalGet=context.API.get;
context.API.get=async(url,...rest)=>{if(url==='/api/voice_config/snapshot'){gets++;if(gets>=2){throw new Error('snapshot unavailable after save');}}return originalGet(url,...rest);};
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,1,'bulk save must have persisted');
assert.strictEqual(context.actionErrors.length,1);
const failure=context.actionErrors[0];
assert(failure[1].message.includes('saved, but Voices could not be refreshed'),'must not claim nothing was applied after a persisted save');
assert(failure[2].includes('was saved'),'recovery copy must state the save persisted');
''')
        self.assertEqual(1, len(out['writes']))

    def test_pre_flush_saved_then_preview_fails_reports_saved_edits(self):
        out = self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
vm.runInContext('enqueueVoiceDraft({ALICE:{type:"custom",voice:"Serena",seed:"7"}})',context);
assert(context.voiceSaveQueue.isDirty(),'precondition: pending card edit');
previewError=new Error('preview refused');
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,1,'the pre-flush must have saved the pending edit');
assert.strictEqual(context.actionErrors.length,1);
assert(context.actionErrors[0][2].includes('pending card edits were saved'),'must not claim nothing applied after a persisted flush');
''')
        self.assertEqual(1, len(out['writes']))

    def test_book_switch_during_pre_flush_is_detected(self):
        out = self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
vm.runInContext('enqueueVoiceDraft({ALICE:{type:"custom",voice:"Serena",seed:"7"}})',context);
saveHook=()=>{context.currentBookFilename='other.txt';};
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,1,'flush saved the pending edit');
assert.strictEqual(previewPosts,0,'no preview POST may be issued against the new book');
assert.strictEqual(context.actionErrors.length,1);
assert(context.actionErrors[0][1].message.includes('Active book changed'));
''')
        self.assertEqual(0, out['previewPosts'])

    def test_busy_guard_gives_feedback(self):
        out = self.run_node(r'''
vm.runInContext('_singleVoiceBusy=true',context);
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0);
assert(context.toasts.length>=1,'busy click must toast instead of silently returning');
vm.runInContext('_singleVoiceBusy=false',context);
''')
        self.assertEqual(0, len(out['writes']))

    def test_dropped_speaker_selection_is_announced(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
serverVoices=[{name:'BOB',config:{type:'custom',voice:'Serena',description:'Calm synthetic narrator.'},persona_pending:false}];
await context.refreshVoiceMetadata();
assert.strictEqual(context.elements['single-voice-source'].value,'','dropped selection resets to placeholder');
assert(context.toasts.some(([message])=>String(message).includes('ALICE')),'dropped speaker must be announced');
''')

    def test_stored_self_alias_survives_unrelated_save_and_explicit_none_clears(self):
        self.run_node(r'''
// Stored self-alias on Alice: renderer shows it selected-invalid,
// collector keeps it through an unrelated edit, None clears both keys.
const selfHtml=vm.runInContext('renderInitialAliasOptions({name:"Alice",config:{type:"custom",voice:"Serena",alias_of:"Alice"}})',context);
assert(selfHtml.includes('Alice')&&selfHtml.includes('selected')&&selfHtml.includes('invalid self-alias'),'stored self-alias must render selected-invalid, got: '+selfHtml);
''')

    def test_preview_source_must_match_captured_source_with_own_voices_key(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
const oldPost=context.API.post;
context.API.post=async(url,body,options)=>{
 const value=await oldPost(url,body,options);
 if(url==='/api/voice_config/single-voice-preview'){return {...value,source_speaker:'BOB'};}
 return value;
};
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0,'echoed other source must write nothing');
assert.strictEqual(context.confirmMessages.length,0,'echoed other source must show no modal');
assert.strictEqual(context.actionErrors.length,1);
assert(context.actionErrors[0][1].message.includes('Invalid voice preview'));
context.API.post=async(url,body,options)=>{
 const value=await oldPost(url,body,options);
 if(url==='/api/voice_config/single-voice-preview'){return {...value,voices:Object.create({ALICE:{type:'custom',voice:'Serena'},BOB:{type:'custom',voice:'Serena'}})};}
 return value;
};
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0,'inherited voices key must write nothing');
assert.strictEqual(context.actionErrors.length,2);
''')

    def test_preview_supports_own_proto_source_name(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='__proto__';
confirmAnswer=false;
const oldPost=context.API.post;
context.API.post=async(url,body,options)=>{
 const value=await oldPost(url,body,options);
 if(url==='/api/voice_config/single-voice-preview'){
  const voices=JSON.parse('{"__proto__":{"type":"custom","voice":"Serena","ready":false},"ALICE":{"type":"custom","voice":"Serena","ready":false}}');
  return {...value,source_speaker:'__proto__',speakers:[{name:'__proto__',line_count:1,changes:[]},{name:'ALICE',line_count:1,changes:[]}],affected_speakers:2,affected_lines:2,voices};
 }
 return value;
};
await context.previewSingleVoiceBook();
assert(context.confirmMessages.some(m=>m.includes('__proto__')),'own __proto__ source must reach the modal');
assert.strictEqual(writes.length,0,'cancel must write nothing');
''')

    def test_preview_rejects_array_voices_and_roster_mismatch(self):
        out = self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
previewVoicesOverride=[];
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0,'array voices must be refused as invalid preview');
assert.strictEqual(context.actionErrors.length,1);
previewVoicesOverride={ALICE:{type:'custom',voice:'Serena',ready:false},BOB:{type:'custom',voice:'Serena',ready:false},CAROL:{type:'custom',voice:'Serena',ready:false}};
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0,'voices map with a non-roster speaker must be refused');
assert.strictEqual(context.actionErrors.length,2);
''')
        self.assertEqual(0, len(out['writes']))

    def test_zero_line_speaker_modal_copy_is_honest(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=false;
previewSpeakersOverride=[{name:'ALICE',line_count:1,changes:[]},{name:'BOB',line_count:0,changes:[]}];
await context.previewSingleVoiceBook();
assert(context.confirmMessages.some(message=>message.includes('BOB: 0 lines') && message.includes('voice config will still be updated')),'0-line speaker must be disclosed as still updated');
''')

    def test_confirm_shows_clone_source_asset_and_cancel_writes_nothing(self):
        out = self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=false;
previewVoicesOverride={ALICE:{type:'clone',voice:null,ref_audio:'C:\\private\\ref.wav',ref_text:'transcript must not dump',adapter_id:null,adapter_path:null,members:null,adapter_generation_sha256:null},BOB:{type:'clone',voice:null,ref_audio:'C:\\private\\ref.wav',ref_text:'transcript must not dump',adapter_id:null,adapter_path:null,members:null,adapter_generation_sha256:null}};
await context.previewSingleVoiceBook();
assert(context.confirmMessages.some(m=>m.includes('clone reference')&&m.includes('C:\\private\\ref.wav')),'modal must name the clone reference being copied');
assert(!context.confirmMessages.some(m=>m.includes('transcript must not dump')),'modal must not dump the reference transcript');
assert.strictEqual(writes.length,0,'cancel must write nothing');
''')
        self.assertEqual(0, len(out['writes']))

    def test_confirm_shows_lora_source_asset_id_path_and_pin(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=false;
previewVoicesOverride={ALICE:{type:'lora',voice:null,ref_audio:null,ref_text:null,adapter_id:'my-adapter',adapter_path:'C:\\models\\my-adapter',members:null,adapter_generation_sha256:'f'.repeat(64)},BOB:{type:'lora',voice:null,ref_audio:null,ref_text:null,adapter_id:'my-adapter',adapter_path:'C:\\models\\my-adapter',members:null,adapter_generation_sha256:'f'.repeat(64)}};
await context.previewSingleVoiceBook();
assert(context.confirmMessages.some(m=>m.includes('LoRA adapter my-adapter')&&m.includes('C:\\models\\my-adapter')&&m.includes('ffffffffffff')),'modal must name LoRA id, path and pin');
assert.strictEqual(writes.length,0,'cancel must write nothing');
''')

    def test_source_preview_filename_labels_are_escaped(self):
        self.run_node(r'''
const evilPreview={text_start:'Synthetic text.',source_name:'evil\nname\u202etxt',imported_source_name:'imp\r\u202e.docx',encoding:'utf-8',total_chars:10,truncated:false,source_sha256:'a'.repeat(64),book_token:'b'.repeat(64)};
vm.runInContext('_singleVoiceSourcePreview = ' + JSON.stringify(evilPreview), context);
context.renderSingleVoiceSourcePreview();
const meta=context.elements['source-preview-meta'].textContent;
assert(!meta.includes('\n')&&!meta.includes('\r')&&!meta.includes('\u202e'),'displayed filename must escape newline/bidi controls');
assert(meta.includes('\\n')&&meta.includes('\\u{202e}')&&meta.includes('\\r'),'escaped forms must be visible');
assert(vm.runInContext('_singleVoiceSourcePreview.source_name',context).includes('\n'),'raw returned string must remain unchanged');
''')

    def test_excerpt_count_uses_code_points(self):
        self.run_node(r'''
previewTextOverride='😀'.repeat(4000);
await context.loadSingleVoiceSourcePreview();
assert(context.elements['source-preview-meta'].textContent.includes('excerpt 4000 shown'),'astral excerpt must count code points, not UTF-16 units');
assert(!context.elements['source-preview-meta'].textContent.includes('8000'));
''')

    def test_reload_clears_binding_on_literal_change_without_event(self):
        self.run_node(r'''
await context.loadSingleVoiceSourcePreview();
context.elements['source-preview-confirm'].checked=true;context.onSingleVoiceSourceConfirm();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),true);
// Programmatic literal change (no change event) then reload: binding must clear.
context.elements['script-literal-source-preserving'].checked=true;
await context.loadSingleVoiceSourcePreview();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false,'reload must clear a binding whose literal state changed');
''')



    def test_bulk_readiness_is_absent_and_percard_ready_preserved(self):
        import pathlib as _p
        js = (_p.Path(__file__).resolve().parent.parent / 'static/js/app-single-voice.js').read_text(encoding='utf-8')
        html = (_p.Path(__file__).resolve().parent.parent / 'static/index.html').read_text(encoding='utf-8')
        core = (_p.Path(__file__).resolve().parent.parent / 'static/js/app-core.js').read_text(encoding='utf-8')
        self.assertNotIn('markSingleVoiceReady', js)
        self.assertNotIn('singleVoiceAllMatchSingleAsset', js)
        self.assertNotIn('markSingleVoiceReady', html)
        self.assertIn('previewSingleVoiceBook', js)
        self.assertIn('voice-ready', core)
        self.assertIn('per-card Ready', html)

    def test_other_tab_source_not_confirmed(self):
        self.run_node(r'''
context.window._selectedUploadIdentity={token:book};
const oldGet=context.API.get;
context.API.get=async(url,...args)=>{
 const value=await oldGet(url,...args);
 return url==='/api/single-narrator/source-preview' ? {...value,book_token:'c'.repeat(64),text_start:'Other book source.'} : value;
};
await context.loadSingleVoiceSourcePreview();
assert.strictEqual(context.actionErrors.length,1);
assert.strictEqual(context.elements['source-preview-text'].textContent,'No source preview loaded for this book.');
context.elements['source-preview-confirm'].checked=true;
context.onSingleVoiceSourceConfirm();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),false);
''')

    def test_other_tab_book_not_bulk_approved(self):
        out = self.run_node(r'''
const oldGet=context.API.get;
context.API.get=async(url,...args)=>{
 const value=await oldGet(url,...args);
 if(url==='/api/voice_config/snapshot'){return {...value,book_token:'c'.repeat(64),book_identity_token:'d'.repeat(64)};}
 return value;
};
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
const cachedToken=vm.runInContext('_voiceSaveSnapshot.book_token',context);
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0);
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.book_token',context),cachedToken);
assert.strictEqual(context.actionErrors.length,1);
''')
        self.assertEqual(0, len(out['writes']))

    def test_unscoped_refresh_refuses_other_tab_book_and_keeps_globals(self):
        # DS3074: tab 1 loaded book A (identity 'b'*64 via harness
        # initial refresh). Another tab switches the server to book B
        # ('d'*64). An unscoped refresh (Voices tab activation) must
        # refuse B instead of adopting it, keeping A's globals/cards.
        # Intentional local selection of B (selection identity, like
        # loadScript/upload) then binds and accepts B.
        self.run_node(r'''
const oldGet=context.API.get;
const beforeSnapshot=vm.runInContext('JSON.stringify(_voiceSaveSnapshot)',context);
context.API.get=async(url,...args)=>{
 const value=await oldGet(url,...args);
 if(url==='/api/voice_config/snapshot'){return {...value,book_identity_token:'d'.repeat(64),voices:[{name:'CAROL',config:{type:'custom',voice:'Ryan'},persona_pending:true}]};}
 return value;
};
await assert.rejects(context.refreshVoiceMetadata(),/Active book changed/);
assert.strictEqual(vm.runInContext('JSON.stringify(_voiceSaveSnapshot)',context),beforeSnapshot,'A globals must not be replaced by B');
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.book_identity_token',context),book);
// Intentional local B: selection identity authorizes the new token.
context.window._selectedUploadIdentity={filename:'book.txt',token:'d'.repeat(64),request:{}};
await context.refreshVoiceMetadata();
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.book_identity_token',context),'d'.repeat(64));
// A-B-A: intentional return to A binds and accepts A again.
context.window._selectedUploadIdentity={filename:'book.txt',token:book,request:{}};
context.API.get=oldGet;
await context.loadVoices(true);
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.book_identity_token',context),book);
''')

    def test_escape_label_covers_line_and_bidi_controls(self):
        self.run_node(r'''
const cases=[
 ['ALICE', 'ALICE'],
 ['A\u2028B', 'A\\u{2028}B'],
 ['A\u2029B', 'A\\u{2029}B'],
 ['A\u0085B', 'A\\u{85}B'],
 ['A\u202eB', 'A\\u{202e}B'],
 ['A\u200fB', 'A\\u{200f}B'],
 ['A\u2066B', 'A\\u{2066}B'],
 ['A\nB\rC\tD', 'A\\nB\\rC\\tD'],
 ['A\u{1bca0}B', 'A\\u{1bca0}B'],
];
for(const [input,expected] of cases){
 assert.strictEqual(vm.runInContext('singleVoiceEscapeLabel',context)(input),expected,'escape must cover '+JSON.stringify(input));
}
assert.strictEqual(vm.runInContext('singleVoiceEscapeLabel',context)('A\u2028B').includes('\u2028'),false);
assert.strictEqual(vm.runInContext('singleVoiceEscapeLabel',context)('A😀B'),'A😀B','emoji must survive display escaping');
assert.strictEqual(vm.runInContext('singleVoiceEscapeLabel',context)('Acafé日本語B'),'Acafé日本語B','legitimate Unicode letters must survive display escaping unchanged');
for(const [input,expected] of [['A​B','A\\u{200b}B'],['A‌B','A\\u{200c}B'],['A‍B','A\\u{200d}B'],['A⁠B','A\\u{2060}B'],['A﻿B','A\\u{feff}B'],['A­B','A\\u{ad}B']]){
 assert.strictEqual(vm.runInContext('singleVoiceEscapeLabel',context)(input),expected,'zero-width escape must cover '+JSON.stringify(input));
}
''')

    def test_unavailable_selection_refuses_server_book_before_publishing(self):
        # DS3074 fail-closed companion: an explicit unavailable local
        # identity (invalid load token) must refuse a valid server-C
        # snapshot BEFORE globals/cards change; a valid explicit
        # reload identity then publishes C normally.
        self.run_node(r'''
const beforeSnapshot=vm.runInContext('JSON.stringify(_voiceSaveSnapshot)',context);
context.window._selectedUploadIdentity={filename:'book.txt',token:null,unavailable:true,request:{}};
const oldGet=context.API.get;
context.API.get=async(url,...args)=>{
 const value=await oldGet(url,...args);
 if(url==='/api/voice_config/snapshot'){return {...value,book_identity_token:'c'.repeat(64),voices:[{name:'CAROL',config:{type:'custom',voice:'Ryan'},persona_pending:true}]};}
 return value;
};
await assert.rejects(context.refreshVoiceMetadata(),/Active book changed/);
assert.strictEqual(vm.runInContext('JSON.stringify(_voiceSaveSnapshot)',context),beforeSnapshot,'unavailable identity must not publish server C globals');
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.book_identity_token',context),book);
// Explicit valid reload identity recovers: matching token publishes.
context.window._selectedUploadIdentity={filename:'book.txt',token:'c'.repeat(64),request:{}};
await context.refreshVoiceMetadata();
assert.strictEqual(vm.runInContext('_voiceSaveSnapshot.book_identity_token',context),'c'.repeat(64));
''')

    def test_unavailable_selection_guidance_requires_book_reload_not_voices(self):
        self.run_node(r'''
context.window._selectedUploadIdentity={filename:'book.txt',token:null,unavailable:true,request:{}};
const beforeSnapshot=vm.runInContext('JSON.stringify(_voiceSaveSnapshot)',context);
await context.loadSingleVoiceSourcePreview();
assert.strictEqual(context.actionErrors.length,1);
assert(context.actionErrors[0][1].message.includes('Reload or reselect this book'),'unavailable preview must direct to book reload, got: '+context.actionErrors[0][1].message);
assert(!context.actionErrors[0][1].message.includes('Voices are still loading'),'unavailable preview must not say Voices are still loading');
assert.strictEqual(context.elements['source-preview-text'].textContent,'No source preview loaded for this book.');
context.elements['single-voice-source'].value='ALICE';
await context.previewSingleVoiceBook();
assert.strictEqual(context.actionErrors.length,2);
assert(context.actionErrors[1][1].message.includes('Reload or reselect this book'),'unavailable bulk preview must direct to book reload');
assert.strictEqual(writes.length,0,'unavailable preview must write nothing');
assert.strictEqual(vm.runInContext('JSON.stringify(_voiceSaveSnapshot)',context),beforeSnapshot,'unavailable must publish no snapshot');
// Explicit valid same-book reload recovers (existing loadScript/upload path).
context.window._selectedUploadIdentity={filename:'book.txt',token:book,request:{}};
await context.loadSingleVoiceSourcePreview();
assert.strictEqual(context.actionErrors.length,2,'valid reload must not add a failure');
assert.strictEqual(context.elements['source-preview-text'].textContent,'Synthetic text.');
''')

    def test_stale_source_rejection_after_new_book_success_is_ignored(self):
        self.run_node(r'''
const originalGet = context.API.get; let releaseFirst = null;
let calls = 0;
context.API.get = (url,...args)=>{ if(url==='/api/single-narrator/source-preview'){ calls++; if(calls===1){ return new Promise((resolve,reject)=>{ releaseFirst=()=>reject(new Error('old load failed')); }); } } return originalGet(url,...args); };
const firstLoad = context.loadSingleVoiceSourcePreview();
context.currentBookFilename='other.txt'; context.window._bookSelectionEpoch=2; context.window.resetSingleVoiceBook();
context.currentBookFilename='book.txt'; context.window._bookSelectionEpoch=3;
await context.loadSingleVoiceSourcePreview();
context.elements['source-preview-confirm'].checked=true; context.onSingleVoiceSourceConfirm();
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),true);
releaseFirst(); await firstLoad;
assert.strictEqual(context.isSingleVoiceSourceConfirmed(),true);
assert(context.elements['source-preview-text'].textContent.includes('Synthetic'));
''')

    def test_bulk_apply_rerenders_real_cards_and_later_edit_does_not_revert(self):
        out = self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,1);
const html = context.elements['voices-list'].innerHTML;
assert(html && html.includes('Serena'),'loadVoices must rerender real cards with the applied voice');
assert(vm.runInContext('_voiceCardsBookToken',context)===snapshotBookToken);
assert(vm.runInContext('_voiceCardsRevision',context)===revision(serverRevision));
const BOB = vm.runInContext('_voiceSaveSnapshot.voices.find(function(v){return v.name==="BOB"})',context);
assert.strictEqual(BOB.config.voice,'Serena');
// Parse the RERENDERED markup into a synthetic DOM (real rendered
// values, not a hand-written expectation) and run the actual collector
// + save path over it: a stale DOM would yield Ryan and revert.
function parseCards(html){
  const chunks=html.split('<div class="card voice-card');
  const cards=[];
  for(let i=1;i<chunks.length;i++){
    const chunk=chunks[i];
    const voice=(chunk.match(/data-voice="([^"]+)"/)||[])[1];
    const voiceSel=(chunk.match(/class="form-select voice-select"[^>]*>(.*?)<\/select>/s)||[])[1]||'';
    const voiceVal=(voiceSel.match(/value="([^"]*)"[^>]*selected/)||[])[1]||'';
    const aliasSel=(chunk.match(/alias-select[^>]*>(.*?)<\/select>/s)||[])[1]||'';
    const aliasVal=(aliasSel.match(/value="([^"]*)"[^>]*selected/)||[])[1]||'';
    const ready=/voice-ready[^>]*checked/.test(chunk);
    const styleVal=(chunk.match(/class="form-control character-style"[^>]*value="([^"]*)"/)||[])[1]||'';
    cards.push({dataset:{voice:voice,version:''},querySelector:function(sel){
      if(sel==='.alias-select') return {value:aliasVal};
      if(sel==='.voice-type:checked') return {value:'custom'};
      if(sel==='.voice-select') return {value:voiceVal};
      if(sel==='.character-style') return {value:styleVal};
      if(sel==='.voice-ready') return {checked:ready};
      if(sel==='.voice-seed') return null;
      if(sel==='.voice-presentation') return null;
      return null;
    }});
  }
  return cards;
}
const cards=parseCards(html);
assert.strictEqual(cards.length,2,'parser must find both rerendered cards');
assert.strictEqual(cards.find(function(c){return c.dataset.voice==='BOB'}).querySelector('.voice-select').value,'Serena');
document.querySelectorAll=function(selector){return selector==='.voice-card'?cards:[];};
context.__collected=vm.runInContext('collectVoiceConfig()',context);
assert.strictEqual(context.__collected.BOB.voice,'Serena','collector must read the rerendered DOM, not stale Ryan');
vm.runInContext('enqueueVoiceDraft(__collected)',context);
await vm.runInContext('flushVoiceSaves()',context);
assert.strictEqual(writes.length,2);
const snapshot2 = await context.API.get('/api/voice_config/snapshot');
assert.strictEqual(snapshot2.voices.find(function(v){return v.name==="BOB"}).config.voice,'Serena','later ordinary edit must not revert');
''')
        self.assertEqual(2, len(out['writes']))

    def test_legacy_alias_rendered_and_preserved(self):
        self.run_node(r'''
vm.runInContext('window._voicesNames=["ALICE","BOB"]',context);
const markup = vm.runInContext('renderInitialAliasOptions({name:"BOB",config:{alias:"ALICE"}})',context);
assert(markup.includes('ALICE') && markup.includes('selected'),'render must preselect legacy alias');
const markupNone = vm.runInContext('renderInitialAliasOptions({name:"BOB",config:{}})',context);
assert.strictEqual(markupNone,'');
assert.strictEqual(vm.runInContext('getEffectiveVoiceAlias({alias:"ALICE"})',context),'ALICE');
// Actual collector path: load a book whose BOB carries only the legacy
// key, parse the rendered card, collect, and save: alias_of must survive
// an unrelated autosave (the collector sends it), and choosing None via
// the select must send alias_of null (the guarded-save seam then clears
// BOTH keys server-side, covered by the python guard test).
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',seed:'7',description:'Calm synthetic narrator.'},persona_pending:false},{name:'BOB',config:{type:'custom',voice:'Ryan',seed:'-1',description:'Calm synthetic narrator.',alias:'ALICE'},persona_pending:false}];
await vm.runInContext('loadVoices(true)',context);
const html = context.elements['voices-list'].innerHTML;
assert(html.includes('ALICE') && html.includes('selected'),'rendered card must show legacy alias selected');
function parseCards(html){
  const chunks=html.split('<div class="card voice-card');
  const cards=[];
  for(let i=1;i<chunks.length;i++){
    const chunk=chunks[i];
    const voice=(chunk.match(/data-voice="([^"]+)"/)||[])[1];
    const voiceSel=(chunk.match(/class="form-select voice-select"[^>]*>(.*?)<\/select>/s)||[])[1]||'';
    const voiceVal=(voiceSel.match(/value="([^"]*)"[^>]*selected/)||[])[1]||'';
    const aliasSel=(chunk.match(/alias-select[^>]*>(.*?)<\/select>/s)||[])[1]||'';
    const aliasVal=(aliasSel.match(/value="([^"]*)"[^>]*selected/)||[])[1]||'';
    const ready=/voice-ready[^>]*checked/.test(chunk);
    cards.push({dataset:{voice:voice,version:''},querySelector:function(sel){
      if(sel==='.alias-select') return {value:aliasVal};
      if(sel==='.voice-type:checked') return {value:'custom'};
      if(sel==='.voice-select') return {value:voiceVal};
      if(sel==='.character-style') return {value:''};
      if(sel==='.voice-ready') return {checked:ready};
      if(sel==='.voice-seed') return null;
      if(sel==='.voice-presentation') return null;
      return null;
    }});
  }
  return cards;
}
const cards=parseCards(html);
const bobCard=cards.find(function(c){return c.dataset.voice==='BOB'});
assert.strictEqual(bobCard.querySelector('.alias-select').value,'ALICE','rendered legacy alias must round-trip through the DOM');
document.querySelectorAll=function(selector){return selector==='.voice-card'?cards:[];};
context.__collected=vm.runInContext('collectVoiceConfig()',context);
assert.strictEqual(context.__collected.BOB.alias_of,'ALICE','collector must preserve the effective legacy alias');
''')

    def test_lost_response_after_commit_is_unknown_not_nothing(self):
        out = self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
const err=new Error('The voice save timed out. Your edits were retained; the server save could not be confirmed.');
saveError=err;
await context.previewSingleVoiceBook();
assert.strictEqual(context.actionErrors.length,1);
const failure=context.actionErrors[0];
assert(failure[1].message.includes('could not be confirmed') && failure[1].message.includes('timed out'));
assert(failure[2].includes('may have been saved'));
assert(context.voiceSaveQueue.isDirty(),'draft retained for canonical recovery');
// The stub committed on the server despite the lost reply (unknown
// outcome now, not a definite refusal): a direct snapshot read without
// a flush proves the persisted state without retrying the ambiguous write.
const snap = await context.API.get('/api/voice_config/snapshot');
assert.strictEqual(snap.voices.find(function(v){return v.name==="BOB"}).config.voice,'Serena','server persisted despite lost reply');
''')
        self.assertEqual(1, len(out['writes']))

    def test_definite_refusal_preserves_detail(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
const err=new Error('Voice version is applied; select another base version or clear its timeline points first.');
err.status=409; err.detail=err.message;
saveError=err;
await context.previewSingleVoiceBook();
const message=context.actionErrors[0][1];
assert(message.message.includes('refused by the server') && message.message.includes('Voice version is applied'));
''')

    def test_5xx_save_is_unknown_not_definite(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
const err=new Error('Bad Gateway');
err.status=502;
saveError=err;
await context.previewSingleVoiceBook();
assert.strictEqual(context.actionErrors.length,1);
assert(context.actionErrors[0][1].message.includes('could not be confirmed'));
assert(context.actionErrors[0][2].includes('may have been saved'));
''')

    def test_preflush_lost_response_is_unknown(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
vm.runInContext('enqueueVoiceDraft({ALICE:{type:"custom",voice:"Serena",seed:"7"}})',context);
const err=new Error('The voice save timed out. Your edits were retained; the server save could not be confirmed.');
saveError=err;
await context.previewSingleVoiceBook();
assert.strictEqual(context.actionErrors.length,1);
assert(context.actionErrors[0][1].message.includes('Pending voice edits could not be confirmed'));
assert(!context.actionErrors[0][2].includes('Nothing was applied'));
''')

    def test_post_save_loadvoices_failure_is_persisted(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
context.loadVoices=async()=>{throw new Error('render failed after save');};
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,1);
assert.strictEqual(context.actionErrors.length,1);
assert(context.actionErrors[0][1].message.includes('voice was saved'));
''')

    def test_newer_edit_after_modal_is_preserved_not_flushed(self):
        out = self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
confirmAnswer=true;
context.showConfirm=async()=>{vm.runInContext('enqueueVoiceDraft({ALICE:{type:"custom",voice:"Serena",seed:"7"}})',context);return true;};
await context.previewSingleVoiceBook();
assert.strictEqual(writes.length,0);
assert(context.voiceSaveQueue.isDirty());
''')
        self.assertEqual(0, len(out['writes']))

if __name__ == '__main__':
    unittest.main()
