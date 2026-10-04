"""Deferred real UI handlers must preserve the currently selected editor."""
from pathlib import Path
import subprocess
import unittest

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-scripts.js'
SETUP=r'''const assert=require('assert');const fs=require('fs');const vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8'),core=fs.readFileSync(process.argv[2],'utf8');
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function setup(){
 const elements={};const toasts=[];const posts=[];const gets=[];
 const ids=['cast-list-panel','design-voice-name','design-source-name','design-description','design-sample-text','design-alias-select',
 'design-preview-audio','design-preview-container','design-status','btn-design-preview','btn-design-save','design-save-status'];
 for(const id of ids){elements[id]={value:'',innerHTML:'',style:{},dataset:{},options:[],disabled:false,focusCount:0,focus(){this.focusCount++;},appendChild(option){this.options.push(option);}};}
 const context={window:{},document:{getElementById:id=>elements[id],createElement:()=>({}),querySelector:()=>({click(){}})},
 API:{post:(...args)=>{const d=deferred();posts.push({args,...d});return d.promise;},get:(...args)=>{const d=deferred();gets.push({args,...d});return d.promise;}},
 escapeHtml:String,showToast:(...args)=>toasts.push(args),showConfirm:async()=>true,console:{error(){}},Date,ensureCastListEditsDiscardable:async()=>true,clearCastListEditor(){},loadCastList:async()=>{},flushVoiceSaves:async()=>{},clearVoiceSuggestions(){},clearCharacterAliases(){},loadCharacterAliases:async()=>{}};
 vm.createContext(context);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),context);const bookStart=core.indexOf('let currentBookFilename =');vm.runInContext(core.slice(bookStart,core.indexOf('async function loadConfig()',bookStart)),context);vm.runInContext(source,context);
 context.loadSavedScripts=()=>{};context.loadDesignedVoices=()=>{};context.loadChunks=async()=>{};context.loadVoices=async()=>{};
 function select(name){const body={querySelector:selector=>({'.design-description':{value:name+' desc'},'.ref-text':{value:name+' text'},'.alias-select':{value:name+' alias',innerHTML:name+' options'}}[selector])};
  return context.window.openVoiceDesignEditor({closest:selector=>selector==='.card-body'?body:{dataset:{voice:name}}});}
 return {context,elements,toasts,posts,gets,select};
}
function state(s){return JSON.stringify({fields:Object.fromEntries(Object.entries(s.elements).map(([key,e])=>[key,{value:e.value,innerHTML:e.innerHTML,display:e.style.display,src:e.src,dataset:{...e.dataset},disabled:e.disabled,focusCount:e.focusCount,options:e.options.map(o=>({...o}))}])),file:s.context.window._currentPreviewFile,id:s.context.window._editingDesignedVoiceId,toasts:s.toasts});}
'''

class DesignerResponseIdentityJsTests(unittest.TestCase):
    def run_js(self,script):
        result=subprocess.run(['node','-e',SETUP+script,str(SOURCE),str(SOURCE.with_name('app-core.js'))],capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_preview_success_requires_explicit_save_in_new_and_edit_modes(self):
        self.run_js(r'''(async()=>{
for(const editing of [false,true]){const s=setup();s.select('A');s.context.window._editingDesignedVoiceId=editing?'saved-resource':null;const pending=s.context.window.generateDesignPreview();assert.strictEqual(s.posts.length,1);assert.strictEqual(s.posts[0].args[0],'/api/voice_design/preview');s.posts[0].resolve({audio_url:'/previews/ready.wav'});await pending;const text=s.elements['design-status'].innerHTML;assert(text.includes(editing?'New preview ready':'Preview ready'));assert(text.includes('Save Voice'));assert(!text.includes('Voice re-designed'));assert.strictEqual(s.posts.length,1,'preview must not save implicitly');}
})().catch(error=>{console.error(error);process.exitCode=1;});''')

    def test_unsaved_switch_cancel_duplicate_and_intervening_edits_are_preserved(self):
        self.run_js(r'''(async()=>{
let finished=false;process.on('beforeExit',()=>{assert(finished,'discard guard verification did not finish');});
for(const target of ['saved','persona']){for(const outcome of ['cancel','approve','edit']){const s=setup();s.select('A');s.elements['design-description'].value='unsaved edit';s.context.window._designedVoicesCache=[{id:'B',name:'B',description:'B description',sample_text:'B text',filename:'B.wav'}];let decision,calls=0;s.context.showConfirm=()=>{calls++;return new Promise(resolve=>decision=resolve);};const open=()=>target==='saved'?s.context.window.openDesignedVoiceForEdit('B'):s.select('B');const pending=open();assert.strictEqual(calls,1);await open();assert.strictEqual(calls,1);assert.strictEqual(s.elements['design-description'].value,'unsaved edit');assert.strictEqual(s.gets.length,0);if(outcome==='edit'){s.elements['design-description'].value='newer edit';}decision(outcome!=='cancel');await new Promise(setImmediate);if(outcome==='approve'&&target==='saved'){s.gets[0].resolve([]);}await pending;if(outcome==='approve'){assert.strictEqual(s.elements['design-voice-name'].value,'B');}else{assert.strictEqual(s.elements['design-description'].value,outcome==='edit'?'newer edit':'unsaved edit');assert.strictEqual(s.gets.length,0);}assert.strictEqual(s.context.window._designerDiscardPending,false);}}finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});''')

    def test_lookup_keeps_later_alias_edits_dirty(self):
        self.run_js(r'''(async()=>{
const s=setup();s.context.window._designedVoicesCache=[{id:'A',name:'A',description:'A description',filename:'A.wav'}];const opening=s.context.window.openDesignedVoiceForEdit('A');s.elements['design-description'].value='later description';s.elements['design-alias-select'].value='later alias';s.gets[0].resolve([{name:'A',config:{alias_of:'server alias'}}]);await opening;assert.strictEqual(s.elements['design-description'].value,'later description');assert.strictEqual(s.elements['design-alias-select'].value,'later alias');let calls=0;s.context.showConfirm=async()=>{calls++;return false;};await s.select('B');assert.strictEqual(calls,1);assert.strictEqual(s.elements['design-description'].value,'later description');
})().catch(error=>{console.error(error);process.exitCode=1;});''')

    def test_delayed_save_preserves_newer_card_alias_and_detached_target(self):
        self.run_js(r'''(async()=>{
for(const change of ['none','alias','card']){const s=setup();s.select('Alice');s.context.CSS={escape:value=>value};s.context.window._currentPreviewFile='Alice.wav';const alias={value:'Original'},card={querySelector:()=>alias};let currentCard=card,writes=0;s.context.document.querySelector=()=>currentCard;s.context.saveVoicesDebounced=()=>writes++;s.elements['design-alias-select'].value='Desired';const saving=s.context.window.saveDesignedVoice();if(change==='alias'){alias.value='Newer';}if(change==='card'){currentCard={querySelector:()=>({value:'Other'})};}s.posts[0].resolve({voice_id:'saved'});await saving;assert.strictEqual(writes,change==='none'?1:0);assert.strictEqual(alias.value,change==='none'?'Desired':change==='alias'?'Newer':'Original');assert.strictEqual(s.elements['design-source-name'].value,'');}
})().catch(error=>{console.error(error);process.exitCode=1;});''')

    def test_preview_reset_editor_aba_and_out_of_order_ownership(self):
        self.run_js(r'''(async()=>{
 for(const transition of ['reset','B','ABA']){for(const failure of [false,true]){
  const s=setup();s.select('A');const old=s.context.window.generateDesignPreview();
  assert.strictEqual(s.elements['btn-design-preview'].disabled,true);
  if(transition==='reset'){s.context.resetDesignerForm();}else{s.select('B');if(transition==='ABA'){s.select('A');}}
  const before=state(s);if(failure){s.posts[0].reject(new Error('stale failed'));}else{s.posts[0].resolve({audio_url:'/previews/stale.wav'});}
  await old;assert.strictEqual(state(s),before);assert.strictEqual(s.elements['btn-design-preview'].disabled,false);
 }}
 for(const failure of [false,true]){
  const s=setup();s.select('A');const old=s.context.window.generateDesignPreview();const latest=s.context.window.generateDesignPreview();
  const before=state(s);if(failure){s.posts[0].reject(new Error('stale failed'));}else{s.posts[0].resolve({audio_url:'/previews/stale.wav'});}
  await old;assert.strictEqual(state(s),before);assert.strictEqual(s.elements['btn-design-preview'].disabled,true);
  s.posts[1].resolve({audio_url:'/previews/fresh.wav'});await latest;
  assert.strictEqual(s.context.window._currentPreviewFile,'fresh.wav');assert(s.elements['design-preview-audio'].src.startsWith('/previews/fresh.wav?t='));
  assert.strictEqual(s.elements['design-preview-container'].style.display,'block');assert.strictEqual(s.elements['btn-design-preview'].disabled,false);
 }
 const s=setup();s.select('A');const old=s.context.window.generateDesignPreview();const latest=s.context.window.generateDesignPreview();
 s.posts[1].resolve({audio_url:'/previews/latest.wav'});await latest;const before=state(s);s.posts[0].resolve({audio_url:'/previews/older.wav'});await old;assert.strictEqual(state(s),before);
 const normal=setup();normal.context.resetDesignerForm();normal.elements['design-description'].value='Plain description';normal.elements['design-sample-text'].value='Plain sample';
 const ready=normal.context.window.generateDesignPreview();normal.posts[0].resolve({audio_url:'/previews/normal.wav'});await ready;
 assert.strictEqual(normal.context.window._currentPreviewFile,'normal.wav');assert(normal.elements['design-status'].innerHTML.includes('Preview ready'));
 assert.strictEqual(normal.elements['btn-design-preview'].disabled,false);
 const failed=setup();failed.select('A');const current=failed.context.window.generateDesignPreview();failed.posts[0].reject(new Error('current failed'));await current;
 assert(failed.elements['design-status'].innerHTML.includes('current failed'));assert.strictEqual(failed.elements['btn-design-preview'].disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});''')

    def test_saved_voice_open_clears_previous_preview_status_before_lookup(self):
        self.run_js(r'''(async()=>{
const s=setup();s.select('A');const old=s.context.window.generateDesignPreview();assert(s.elements['design-status'].innerHTML.includes('Generating preview'));s.context.window._designedVoicesCache=[{id:'B',name:'B',description:'B description',sample_text:'B text',filename:'B.wav'}];const opening=s.context.window.openDesignedVoiceForEdit('B');assert.strictEqual(s.elements['design-status'].innerHTML,'');assert.strictEqual(s.elements['design-preview-container'].style.display,'none');assert.strictEqual(s.elements['btn-design-preview'].disabled,false);s.gets[0].resolve([]);await opening;assert(!s.elements['design-status'].innerHTML.includes('Generating'));const before=state(s);s.posts[0].resolve({audio_url:'/previews/old.wav'});await old;assert.strictEqual(state(s),before);
})().catch(error=>{console.error(error);process.exitCode=1;});''')

    def test_saved_voice_lookup_is_discarded_after_new_open_reset_or_preview_editor(self):
        self.run_js(r'''(async()=>{
 for(const failure of [false,true]){
  const s=setup();s.context.window._designedVoicesCache=[{id:'A',name:'A',filename:'A.wav',description:'A desc',sample_text:'A text'},{id:'B',name:'B',filename:'B.wav',description:'B desc',sample_text:'B text'}];
  const old=s.context.window.openDesignedVoiceForEdit('A');const latest=s.context.window.openDesignedVoiceForEdit('B');
  s.gets[1].resolve([{name:'B',config:{alias_of:'B target'}},{name:'B target',config:{}}]);await latest;
  assert.strictEqual(s.elements['design-alias-select'].value,'B target');assert.strictEqual(s.context.window._editingDesignedVoiceId,'B');
  assert.strictEqual(s.context.window._currentPreviewFile,'B.wav');const before=state(s);
  if(failure){s.gets[0].reject(new Error('old offline'));}else{s.gets[0].resolve([{name:'A',config:{alias_of:'A target'}}]);}
  await old;assert.strictEqual(state(s),before);
 }
 for(const failure of [false,true]){
  const s=setup();s.context.window._designedVoicesCache=[{id:'A',name:'A',filename:'A.wav'}];const old=s.context.window.openDesignedVoiceForEdit('A');
  s.select('B');const preview=s.context.window.generateDesignPreview();const before=state(s);
  if(failure){s.gets[0].reject(new Error('old offline'));}else{s.gets[0].resolve([{name:'A',config:{alias_of:'A target'}}]);}
  await old;assert.strictEqual(state(s),before);assert.strictEqual(s.elements['btn-design-preview'].disabled,true);
  s.posts[0].resolve({audio_url:'/previews/B.wav'});await preview;assert.strictEqual(s.context.window._editingDesignedVoiceId,null);assert.strictEqual(s.elements['design-source-name'].value,'B');
 }
 const s=setup();s.context.window._designedVoicesCache=[{id:'A',name:'A',filename:'A.wav'}];const pending=s.context.window.openDesignedVoiceForEdit('A');
 s.context.resetDesignerForm();const before=state(s);s.gets[0].resolve([]);await pending;assert.strictEqual(state(s),before);
})().catch(error=>{console.error(error);process.exitCode=1;});''')

    def test_book_success_invalidates_before_slow_refresh_and_refusal_retains_editor(self):
        self.run_js(r'''(async()=>{
 const s=setup();s.select('A');const preview=s.context.window.generateDesignPreview();const chunks=deferred();const voices=deferred();let chunksStarted=false,voicesStarted=false;
 const post=s.context.API.post;s.context.API.post=(url,...args)=>url==='/api/scripts/load'?Promise.resolve({status:'loaded',name:'Book B'}):post(url,...args);s.context.loadChunks=()=>{chunksStarted=true;return chunks.promise;};s.context.loadVoices=()=>{voicesStarted=true;return voices.promise;};
 const loading=s.context.loadScript('Book B');await new Promise(setImmediate);
 assert(chunksStarted);assert.strictEqual(s.elements['design-voice-name'].value,'');assert.strictEqual(s.context.window._editingDesignedVoiceId,null);
 const before=state(s);s.posts[0].resolve({audio_url:'/previews/A.wav'});await preview;assert.strictEqual(state(s),before);
 s.select('B');const fresh=s.context.window.generateDesignPreview();chunks.resolve();await new Promise(setImmediate);assert(voicesStarted);
 s.posts[1].resolve({audio_url:'/previews/B.wav'});await fresh;const editor=state(s);voices.resolve();await loading;assert.strictEqual(state(s),editor);
 for(const outcome of ['cancel','refuse','network']){
  const current=setup();current.select('A');const before=state(current);let fetchCalls=0;
  current.context.showConfirm=async()=>outcome!=='cancel';current.context.API.post=async()=>{fetchCalls++;throw new Error(outcome==='network'?'offline':'refused');};
  await current.context.loadScript('Book');assert.strictEqual(current.elements['design-voice-name'].value,'A');assert.strictEqual(current.context.window._editingDesignedVoiceId,null);assert.strictEqual(current.elements['design-source-name'].value,'A');
  const after=JSON.parse(state(current));const expected=JSON.parse(before);after.toasts=expected.toasts;assert.deepStrictEqual(after,expected);
  if(outcome==='cancel'){assert.strictEqual(state(current),before);assert.strictEqual(fetchCalls,0);}
 }
})().catch(error=>{console.error(error);process.exitCode=1;});''')

    def test_successful_save_restores_create_label_and_failed_save_retains_edit_state(self):
        self.run_js(r'''(async()=>{
 for(const failure of [false,true]){
  const s=setup();s.context.CSS={escape:value=>value};
  s.context.document.querySelector=selector=>selector.startsWith('.voice-card')?null:{click(){}};
  s.context.window._designedVoicesCache=[{id:'A',name:'Saved A',filename:'A.wav',description:'A desc',sample_text:'A text'}];
  const opening=s.context.window.openDesignedVoiceForEdit('A');s.gets[0].resolve([]);await opening;
  const button=s.elements['btn-design-preview'];assert(button.innerHTML.includes('Re-design Voice'));
  const label=button.innerHTML, file=s.context.window._currentPreviewFile;
  const saving=s.context.window.saveDesignedVoice();
  assert.strictEqual(s.posts[0].args[1].voice_id,'A');assert.strictEqual(s.posts[0].args[1].preview_file,'A.wav');
  if(failure){s.posts[0].reject(new Error('save refused'));}else{s.posts[0].resolve({});}
  await saving;assert.strictEqual(s.context.window._designSavePending,false);
  if(failure){
   assert.strictEqual(s.context.window._currentPreviewFile,file);
   assert.strictEqual(s.context.window._editingDesignedVoiceId,'A');assert.strictEqual(button.innerHTML,label);
   assert.strictEqual(s.elements['design-voice-name'].value,'Saved A');
   assert(s.toasts.some(toast=>toast[0].includes('save refused')));
  }else{
   assert.strictEqual(s.context.window._editingDesignedVoiceId,null);assert(button.innerHTML.includes('Generate Preview'));
   assert(!button.innerHTML.includes('Re-design Voice'));assert.strictEqual(s.elements['design-voice-name'].value,'');
   s.elements['design-voice-name'].value='New voice';
   assert.strictEqual(s.context.window._currentPreviewFile,null);assert.strictEqual(s.elements['design-preview-container'].style.display,'none');
   await s.context.window.saveDesignedVoice();assert.strictEqual(s.posts.length,1,'saved preview must not remain armed');
  }
 }
})().catch(error=>{console.error(error);process.exitCode=1;});''')

    def test_saved_voice_open_hides_old_preview_before_lookup_completes(self):
        self.run_js(r'''
(async()=>{const s=setup();let pauses=0,loads=0;const audio=s.elements['design-preview-audio'];audio.src='/old.wav';audio.pause=()=>pauses++;audio.removeAttribute=name=>{assert.strictEqual(name,'src');delete audio.src;};audio.load=()=>loads++;s.elements['design-preview-container'].style.display='block';s.context.window._currentPreviewFile='old.wav';s.context.window._editingDesignedVoiceId='old';s.context.window._designedVoicesCache=[{id:'new',name:'New',filename:'new.wav'}];
const opening=s.context.window.openDesignedVoiceForEdit('new');await new Promise(setImmediate);assert.strictEqual(s.elements['design-preview-container'].style.display,'none');assert.strictEqual(s.context.window._currentPreviewFile,null);assert.strictEqual(s.context.window._editingDesignedVoiceId,null);assert.strictEqual(audio.src,undefined);assert.strictEqual(pauses,1);assert.strictEqual(loads,1);s.gets[0].resolve([]);await opening;assert.strictEqual(s.context.window._currentPreviewFile,'new.wav');assert.strictEqual(s.elements['design-preview-container'].style.display,'block');})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_saved_playback_stops_prior_audio_and_only_current_failure_notifies(self):
        self.run_js(r'''
(async()=>{const s=setup(),audios=[];let previewPauses=0;s.elements['design-preview-audio'].pause=()=>previewPauses++;s.context.Audio=class{constructor(url){this.url=url;this.pauses=0;this.gate=deferred();audios.push(this);}pause(){this.pauses++;}play(){return this.gate.promise;}};
const old=s.context.window.playDesignedVoice('one #.wav');const latest=s.context.window.playDesignedVoice('two ?.wav');assert.strictEqual(audios[0].pauses,1);assert.strictEqual(previewPauses,2);assert(audios[0].url.includes('one%20%23.wav'));assert(audios[1].url.includes('two%20%3F.wav'));audios[0].gate.reject(Error('old rejection'));await old;assert.strictEqual(s.toasts.length,0);audios[1].gate.reject(Error('current rejection'));await latest;assert(s.toasts.at(-1)[0].includes('try Play again'));assert.strictEqual(s.toasts.at(-1)[1],'warning');
s.context.stopDesignedVoicePlayback(s.elements['design-preview-audio']);assert.strictEqual(audios[1].pauses,1);assert.strictEqual(previewPauses,2,'starting in-form preview keeps itself playing');})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_save_pending_duplicate_and_newer_editor_work_are_preserved(self):
        self.run_js(r'''
(async()=>{for(const transition of ['edit','new-preview','other-voice','failure']){const s=setup();s.context.CSS={escape:value=>value};s.context.document.querySelector=selector=>selector.startsWith('.voice-card')?null:{click(){}};s.select('A');s.context.window._currentPreviewFile='submitted.wav';s.elements['btn-design-save'].innerHTML='Save Voice';
const saving=s.context.window.saveDesignedVoice();assert(s.elements['btn-design-save'].disabled);assert.strictEqual(s.elements['btn-design-save'].innerHTML,'Saving…');assert.strictEqual(s.elements['design-save-status'].textContent,'Saving voice…');await s.context.window.saveDesignedVoice();assert.strictEqual(s.posts.length,1);
if(transition==='edit'){s.elements['design-voice-name'].value='later name';s.elements['design-description'].value='later description';}if(transition==='new-preview'){s.context.window._currentPreviewFile='new.wav';}if(transition==='other-voice'){await s.select('B');s.context.window._currentPreviewFile='B.wav';}
if(transition==='failure'){s.posts[0].reject(Error('refused'));}else{s.posts[0].resolve({voice_id:'saved-A'});}await saving;assert(!s.elements['btn-design-save'].disabled);assert.strictEqual(s.elements['btn-design-save'].innerHTML,'Save Voice');assert.strictEqual(s.context.window._designSavePending,false);
if(transition==='edit'){assert.strictEqual(s.elements['design-voice-name'].value,'later name');assert.strictEqual(s.elements['design-description'].value,'later description');assert.strictEqual(s.context.window._currentPreviewFile,null);assert.strictEqual(s.context.window._editingDesignedVoiceId,'saved-A');assert(s.elements['design-save-status'].textContent.includes('Later form edits were kept'));}
if(transition==='new-preview'){assert.strictEqual(s.context.window._currentPreviewFile,'new.wav');assert(!s.elements['design-save-status'].textContent.includes('Generate a preview'));}if(transition==='other-voice'){assert.strictEqual(s.elements['design-voice-name'].value,'B');assert.strictEqual(s.context.window._currentPreviewFile,'B.wav');}
if(transition==='failure'){assert.strictEqual(s.elements['design-voice-name'].value,'A');assert.strictEqual(s.context.window._currentPreviewFile,'submitted.wav');assert(s.elements['design-save-status'].textContent.includes('Your form is retained'));}}})().catch(e=>{console.error(e);process.exitCode=1;});
''')
