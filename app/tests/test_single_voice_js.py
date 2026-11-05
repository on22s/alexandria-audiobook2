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
const revision=n=>String(n).padStart(64,'0'),book='b'.repeat(64);
const customAsset=v=>({type:'custom',voice:v,ref_audio:null,ref_text:null,adapter_id:null,adapter_path:null,members:null});const cloneAsset=(a,tx)=>({type:'clone',voice:null,ref_audio:a,ref_text:tx,adapter_id:null,adapter_path:null,members:null});const loraAsset=(id,pp)=>({type:'lora',voice:null,ref_audio:null,ref_text:null,adapter_id:id,adapter_path:pp,members:null});let serverRevision=0, writes=[], previewPosts=0, snapshotGets=0, confirmAnswer=true, confirmCalls=0;
let serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',seed:'7',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'custom',voice:'Ryan',seed:'-1',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Ryan')}];
let previewError=null, saveError=null, previewSha='a'.repeat(64);
let snapshotError=null, saveHook=null, previewVoicesOverride=null, previewSpeakersOverride=null, previewTextOverride=null;
const context={currentBookFilename:'book.txt',_bookSelectionEpoch:1,document,elements,window:{addEventListener(){},_bookSelectionEpoch:1,
  localStorage:{setItem(){},getItem(){return null;},removeItem(){}}},
 escapeHtml:String,showToast:(...args)=>{context.toasts.push(args);},confirmMessages:[],showConfirm:async message=>{confirmCalls++;context.confirmMessages.push(message);return confirmAnswer;},
 showActionError:(...args)=>{context.actionErrors.push(args);},toasts:[],actionErrors:[],isEditorEditPending:()=>false,
 setTimeout:()=>1,clearTimeout(){},AbortController,console:{error(){}},
 ensureVoiceRosterCache(){},renderVoiceDrafts(){},
 API:{get:async(url,options)=>{if(url==='/api/single-narrator/source-preview'){return {book_token:book,source_sha256:previewSha,source_name:'book.txt',encoding:'utf-8',total_chars:10,truncated:false,text_start:previewTextOverride ?? 'Synthetic text.'};}
   if(url==='/api/voice_config/snapshot'){snapshotGets++;if(snapshotError){throw snapshotError;}return {revision:revision(serverRevision),book_token:book,book_id:'book',config:{},voices:serverVoices,seed_changes:[]};}
   throw new Error('unexpected GET '+url);},
  post:async(url,body)=>{if(url==='/api/voice_config/single-voice-preview'){previewPosts++;if(previewError){throw previewError;}
     const speakers=previewSpeakersOverride ?? [{name:'ALICE',line_count:1,changes:[]},{name:'BOB',line_count:1,changes:['voice changed','ready reset (needs new listening approval)']}];
     return {book_token:book,revision:body.revision,source_speaker:body.source_speaker,affected_speakers:speakers.length,affected_lines:speakers.reduce((sum,row)=>sum+row.line_count,0),
      speakers,
      voices:previewVoicesOverride ?? {ALICE:{type:'custom',voice:'Serena',seed:'7',ready:false},BOB:{type:'custom',voice:'Serena',seed:'-1',ready:false}}};}
   if(url==='/api/voice_config/save'){writes.push(JSON.parse(JSON.stringify(body)));if(saveHook){saveHook();}if(saveError){throw saveError;}
     serverRevision++;return {book_token:book,revision:revision(serverRevision)};}
   throw new Error('unexpected POST '+url);}}};
vm.createContext(context);
vm.runInContext(helper+saves+refresh+literalSlice+voice,context);
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

    def test_ready_refuses_preexisting_dirty_queue_and_retains_drafts(self):
        out = self.run_node(r'''
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',seed:'7',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'custom',voice:'Serena',seed:'-1',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')}];
await context.refreshVoiceMetadata();
vm.runInContext('enqueueVoiceDraft({ALICE:{type:"custom",voice:"Serena",seed:"7"}})',context);
assert(context.voiceSaveQueue.isDirty(),'precondition: real queue is dirty');
confirmAnswer=true;
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'preexisting dirty queue must refuse, not flush+approve');
assert(context.voiceSaveQueue.isDirty(),'drafts must be retained untouched');
assert(context.toasts.length>=1);
''')
        self.assertEqual(0, len(out['writes']))

    def test_ready_ab_a_during_modal_refuses(self):
        out = self.run_node(r'''
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',seed:'7',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'custom',voice:'Serena',seed:'-1',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')}];
confirmAnswer=true;
context.showConfirm=async()=>{context.currentBookFilename='other.txt';context.window._bookSelectionEpoch=2;
  context.currentBookFilename='book.txt';context.window._bookSelectionEpoch=3;return true;};
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'A->B->A while the modal is open must refuse');
assert.strictEqual(context.actionErrors.length,1);
''')
        self.assertEqual(0, len(out['writes']))

    def test_ready_cancel_sends_no_write(self):
        out = self.run_node(r'''
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',seed:'7',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'custom',voice:'Serena',seed:'-1',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')}];
confirmAnswer=false;
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'cancel must send 0 writes');
assert.strictEqual(context.actionErrors.length,0);
''')
        self.assertEqual(0, len(out['writes']))

    def test_ready_flush_failure_retains_drafts(self):
        self.run_node(r'''
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',seed:'7',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'custom',voice:'Serena',seed:'-1',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')}];
confirmAnswer=true; saveError=new Error('save refused');
await context.markSingleVoiceReady();
assert(context.actionErrors.length>=1);
assert(context.voiceSaveQueue.isDirty(),'failed flush must retain the ready drafts for retry');
''')

    def test_ready_action_requires_unanimous_single_voice(self):
        out = self.run_node(r'''
await context.refreshVoiceMetadata();
confirmAnswer=true;
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'mixed voices must not bulk-ready');
assert.strictEqual(context.actionErrors.length,1);
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',seed:'7',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'custom',voice:'Serena',seed:'-1',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,1);
assert.strictEqual(writes[0].voices.BOB.ready,true);
''')
        self.assertEqual(1, len(out['writes']))

    def test_ready_refuses_empty_but_accepts_custom_without_persona(self):
        # #1724: empty {} (null descriptor) must not bulk-ready; valid custom without persona (descriptor present, persona_pending true) must.
        out = self.run_node(r'''
confirmAnswer=true;
serverVoices=[{name:'ALICE',config:{},persona_pending:true,single_voice_asset:null},{name:'BOB',config:{},persona_pending:true,single_voice_asset:null}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'empty assets must not bulk-ready');
assert.strictEqual(context.actionErrors.length,1);
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Ryan'},persona_pending:true,single_voice_asset:customAsset('Ryan')},{name:'BOB',config:{type:'custom',voice:'Ryan'},persona_pending:true,single_voice_asset:customAsset('Ryan')}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,1,'valid custom without persona text must bulk-ready via descriptor');
''')
        self.assertEqual(1, len(out['writes']))

    def test_ready_refuses_legacy_alias_and_corrupt_timeline(self):
        out = self.run_node(r'''
confirmAnswer=true;
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',description:'Calm synthetic narrator.',alias:'CAROL'},persona_pending:false,single_voice_asset:null},{name:'BOB',config:{type:'custom',voice:'Serena',description:'Calm synthetic narrator.',alias:'CAROL'},persona_pending:false,single_voice_asset:null}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'legacy alias must refuse bulk-ready');
assert.strictEqual(context.actionErrors.length,1);
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',description:'Calm synthetic narrator.',version_timeline:'v1'},persona_pending:false,single_voice_asset:null},{name:'BOB',config:{type:'custom',voice:'Serena',description:'Calm synthetic narrator.',version_timeline:'v1'},persona_pending:false,single_voice_asset:null}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'corrupt (non-array) timeline must refuse bulk-ready');
assert.strictEqual(context.actionErrors.length,2);
''')
        self.assertEqual(0, len(out['writes']))

    def test_ready_ignores_distinct_preserved_descriptions(self):
        out = self.run_node(r'''
confirmAnswer=true;
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',description:'Alice persona'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'custom',voice:'Serena',description:'Bob persona'},persona_pending:false,single_voice_asset:customAsset('Serena')}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,1,'distinct preserved descriptions + SAME custom voice must bulk-ready');
assert.strictEqual(writes[0].voices.BOB.ready,true);
serverVoices=[{name:'ALICE',config:{type:'clone',ref_audio:'refs/shared.wav',ref_text:'Synthetic reference text.',description:'Alice persona'},persona_pending:false,single_voice_asset:cloneAsset('refs/shared.wav','Synthetic reference text.')},{name:'BOB',config:{type:'clone',ref_audio:'refs/shared.wav',ref_text:'Synthetic reference text.',description:'Bob persona'},persona_pending:false,single_voice_asset:cloneAsset('refs/shared.wav','Synthetic reference text.')}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,2,'distinct descriptions + SAME clone ref pair must bulk-ready');
serverVoices=[{name:'ALICE',config:{type:'lora',adapter_id:'lora-1',adapter_path:'lora_models/lora-1',description:'Alice persona'},persona_pending:false,single_voice_asset:loraAsset('lora-1','lora_models/lora-1')},{name:'BOB',config:{type:'lora',adapter_id:'lora-1',adapter_path:'lora_models/lora-1',description:'Bob persona'},persona_pending:false,single_voice_asset:loraAsset('lora-1','lora_models/lora-1')}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,3,'distinct descriptions + SAME adapter must bulk-ready');
''')
        self.assertEqual(3, len(out['writes']))

    def test_ready_still_refuses_distinct_actual_assets(self):
        out = self.run_node(r'''
confirmAnswer=true;
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',description:'Alice persona'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'custom',voice:'Ryan',description:'Bob persona'},persona_pending:false,single_voice_asset:customAsset('Ryan')}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'distinct custom voices must refuse');
assert.strictEqual(context.actionErrors.length,1);
serverVoices=[{name:'ALICE',config:{type:'clone',ref_audio:'refs/a.wav',ref_text:'Synthetic reference text.',description:'Alice persona'},persona_pending:false,single_voice_asset:cloneAsset('refs/a.wav','Synthetic reference text.')},{name:'BOB',config:{type:'clone',ref_audio:'refs/b.wav',ref_text:'Synthetic reference text.',description:'Bob persona'},persona_pending:false,single_voice_asset:cloneAsset('refs/b.wav','Synthetic reference text.')}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'distinct clone references must refuse');
assert.strictEqual(context.actionErrors.length,2);
serverVoices=[{name:'ALICE',config:{type:'lora',adapter_id:'lora-1',adapter_path:'lora_models/lora-1',description:'Alice persona'},persona_pending:false,single_voice_asset:loraAsset('lora-1','lora_models/lora-1')},{name:'BOB',config:{type:'lora',adapter_id:'lora-2',adapter_path:'lora_models/lora-2',description:'Bob persona'},persona_pending:false,single_voice_asset:loraAsset('lora-2','lora_models/lora-2')}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'distinct adapters must refuse');
assert.strictEqual(context.actionErrors.length,3);
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',description:'Alice persona'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'lora',adapter_id:'lora-1',adapter_path:'lora_models/lora-1',description:'Bob persona'},persona_pending:false,single_voice_asset:loraAsset('lora-1','lora_models/lora-1')}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'distinct types must refuse');
assert.strictEqual(context.actionErrors.length,4);
''')
        self.assertEqual(0, len(out['writes']))

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

    def test_ready_saved_then_refresh_fails_reports_saved_truthfully(self):
        out = self.run_node(r'''
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena',seed:'7',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')},{name:'BOB',config:{type:'custom',voice:'Serena',seed:'-1',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')}];
confirmAnswer=true;
// First refresh (pre-modal) succeeds; the refresh after the ready save fails.
let gets=0; const originalGet=context.API.get;
context.API.get=async(url,...rest)=>{if(url==='/api/voice_config/snapshot'){gets++;if(gets>=2){throw new Error('snapshot unavailable after save');}}return originalGet(url,...rest);};
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,1,'ready save must have persisted');
assert.strictEqual(context.actionErrors.length,1);
assert(context.actionErrors[0][1].message.includes('marked ready, but Voices could not be refreshed'),'must not claim nothing was marked ready after a persisted save');
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
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0);
assert(context.toasts.length>=1,'busy click must toast instead of silently returning');
vm.runInContext('_singleVoiceBusy=false',context);
''')
        self.assertEqual(0, len(out['writes']))

    def test_dropped_speaker_selection_is_announced(self):
        self.run_node(r'''
context.elements['single-voice-source'].value='ALICE';
serverVoices=[{name:'BOB',config:{type:'custom',voice:'Serena',description:'Calm synthetic narrator.'},persona_pending:false,single_voice_asset:customAsset('Serena')}];
await context.refreshVoiceMetadata();
assert.strictEqual(context.elements['single-voice-source'].value,'','dropped selection resets to placeholder');
assert(context.toasts.some(([message])=>String(message).includes('ALICE')),'dropped speaker must be announced');
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


    def test_ready_refuses_empty_clone_and_missing_descriptor(self):
        out = self.run_node(r'''
confirmAnswer=true;
serverVoices=[{name:'ALICE',config:{type:'clone'},persona_pending:false,single_voice_asset:null},{name:'BOB',config:{type:'clone'},persona_pending:false,single_voice_asset:null}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'empty clone must send zero saves');
assert.strictEqual(context.actionErrors.length,1);
serverVoices=[{name:'ALICE',config:{type:'custom',voice:'Serena'},persona_pending:false},{name:'BOB',config:{type:'custom',voice:'Serena'},persona_pending:false}];
await context.markSingleVoiceReady();
assert.strictEqual(writes.length,0,'missing descriptor must fail closed');
assert.strictEqual(context.actionErrors.length,2);
''')
        self.assertEqual(0, len(out['writes']))

if __name__ == '__main__':
    unittest.main()
