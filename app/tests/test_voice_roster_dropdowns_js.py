"""Lazy roster options preserve native-select values and ownership boundaries."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
HARNESS = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const handlers={};const containers=Object.fromEntries(['voices-list','chunks-table-body'].map(id=>[id,{dataset:{},addEventListener(type,fn){handlers[id+type]=fn;}}]));
const context={currentBookFilename:'',window:{_voicesNames:['Zed','Alice','Alice','  Bob  '],_voiceRosterBookToken:'book-a'},
 escapeHtml:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;'),
 document:{getElementById:id=>containers[id],createElement:()=>({value:'',textContent:''}),createDocumentFragment:()=>({children:[],appendChild(o){this.children.push(o);}})}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('// Voice roster dropdowns:'),source.indexOf('// End voice roster dropdowns.')),context);
const start=source.indexOf('function buildSpeakerSelect(');vm.runInContext(source.slice(start,source.indexOf('// Check if any audio',start)),context);
function select(kind,current,self='Alice'){
 const s={dataset:{voiceRoster:kind,rosterBook:'book-a'},disabled:false,options:[{value:current,textContent:current}],selected:0,
 closest(query){return query==='.voice-card'?{dataset:{voice:self}}:this;},replaceChildren(fragment){this.options=fragment.children;this.selected=this.options.length?0:-1;}};
 Object.defineProperty(s,'value',{get(){return this.options[this.selected]?.value||'';},set(v){this.selected=this.options.findIndex(o=>o.value===v);}});return s;
}
const ensure=context.ensureVoiceRosterOptions;
'''


class VoiceRosterDropdownTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', HARNESS + code, str(SOURCE)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_initial_options_and_editor_order_custom_blank_and_empty(self):
        self.run_js(r'''
const html=context.buildSpeakerSelect({id:2,speaker:'Unknown <&>'});assert.equal((html.match(/<option/g)||[]).length,1);assert(html.includes('Unknown &lt;&amp;&gt; (custom)'));
const s=select('speaker','Unknown <&>');ensure(s);assert.equal(s.value,'Unknown <&>');assert.deepEqual(s.options.map(o=>o.value),['Unknown <&>','Alice','Bob','Zed']);
assert(context.buildSpeakerSelect({id:2,speaker:''}).includes('value="Alice"'));
context.window._voicesNames=[];assert(!context.buildSpeakerSelect({id:2,speaker:''}).includes('<option'));
''')

    def test_alias_save_without_opening_and_self_exclusion(self):
        self.run_js(r'''
context.window._voicesNames=['Alice','Zed','Bob'];
assert.equal(context.renderInitialAliasOptions({name:'Alice',config:{alias_of:'Zed'}}),'<option value="Zed" selected>Zed</option>');
for(const alias of ['','Alice','missing']){assert.equal(context.renderInitialAliasOptions({name:'Alice',config:{alias_of:alias}}),'');}
const s=select('alias','Zed');assert.equal(s.value,'Zed');ensure(s);assert.equal(s.value,'Zed');assert.deepEqual(s.options.map(o=>o.value),['','Zed','Bob']);s.value='';ensure(s);assert.equal(s.value,'');
''')

    def test_cached_options_and_roster_invalidation_preserve_unsaved_value(self):
        self.run_js(r'''
const s=select('speaker','Alice');ensure(s);const old=s.options;ensure(s);assert.equal(s.options,old);
context.window._voicesNames=['New','Zed'];ensure(s);assert.notEqual(s.options,old);assert.equal(s.value,'Alice');assert.deepEqual(s.options.map(o=>o.value),['Alice','New','Zed']);
''')

    def test_book_switch_refuses_old_controls_and_accepts_new_controls(self):
        self.run_js(r'''
const old=select('speaker','Alice');ensure(old);const opts=old.options;context.currentBookFilename='new-book';assert.equal(ensure(old),false);context.currentBookFilename='';context.window._voiceRosterBookToken='book-b';context.window._voicesNames=['Other'];
assert.equal(ensure(old),false);assert.equal(old.options,opts);assert.equal(old.value,'Alice');const fresh=select('speaker','Other');fresh.dataset.rosterBook='book-b';assert.equal(ensure(fresh),true);assert.deepEqual(fresh.options.map(o=>o.value),['Other']);
''')

    def test_pointer_keyboard_focus_disabled_and_redraw_delegation(self):
        self.run_js(r'''
assert.equal(Object.keys(handlers).length,6);
for(const event of ['pointerdown','focusin','keydown']){const s=select('speaker','Alice');let prevented=false;handlers['chunks-table-body'+event]({target:s,preventDefault(){prevented=true;}});assert.equal(s.options.length,3);assert.equal(s.value,'Alice');assert(!prevented);}
const disabled=select('alias','Zed');disabled.disabled=true;handlers['voices-listfocusin']({target:disabled,preventDefault(){throw Error('unexpected');}});assert.equal(disabled.options.length,1);assert(disabled.disabled);
const fresh=select('alias','Zed');handlers['voices-listpointerdown']({target:fresh,preventDefault(){}});assert.equal(fresh.value,'Zed');assert(!fresh.options.some(o=>o.value==='Alice'));
''')

    def test_programmatic_alias_assignment_requires_population(self):
        self.run_js(r'''
context.window._voicesNames=['Alice','Bob','Zed'];const missing=select('alias','Bob');missing.value='Zed';assert.equal(missing.value,'');
const s=select('alias','Bob');assert(ensure(s));s.value='Zed';assert.equal(s.value,'Zed');
''')

    def test_designer_open_materializes_options_before_copy_and_rejects_old_book(self):
        self.run_js(r'''
const scripts=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-scripts.js'),'utf8');
const elements={};for(const id of ['design-voice-name','design-source-name','design-description','design-sample-text','design-alias-select','design-preview-container','btn-design-preview','design-status']){elements[id]={value:'retained',dataset:{},style:{}};}
const alias=select('alias','Zed');Object.defineProperty(alias,'innerHTML',{get(){return this.options.map(o=>o.value).join('|');}});
const card={dataset:{voice:'Alice'},querySelector:q=>q==='.alias-select'?alias:q==='.ref-text'?{value:'transcript'}:null};
context.document.getElementById=id=>elements[id];context.document.querySelector=()=>({click(){}});context.ensureDesignerEditsDiscardable=()=>true;context.getVoiceCardDescription=()=> 'description';context.resetDesignerForm=()=>{};context.markDesignerFormClean=()=>{};context.showToast=()=>{};
vm.runInContext(scripts.slice(scripts.indexOf('window.openVoiceDesignEditor ='),scripts.indexOf('window.onDesignedVoiceSelect =')),context);
(async()=>{await context.window.openVoiceDesignEditor({closest:()=>card});assert.equal(elements['design-alias-select'].innerHTML,'|Zed|  Bob  ');assert.equal(elements['design-alias-select'].value,'Zed');
elements['design-voice-name'].value='retained';context.currentBookFilename='changed';await context.window.openVoiceDesignEditor({closest:()=>card});assert.equal(elements['design-voice-name'].value,'retained');})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_designer_save_populates_native_value_and_refuses_book_change(self):
        self.run_js(r'''
const scripts=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-scripts.js'),'utf8');
context.window._voicesNames=['Alice','Bob','Zed'];
const elements={};for(const id of ['design-voice-name','design-source-name','design-description','design-sample-text','design-alias-select','btn-design-save','design-save-status']){elements[id]={value:'description',dataset:{},style:{}};}
elements['design-source-name'].value='Alice';elements['design-alias-select'].value='Zed';
const alias=select('alias','Bob'),card={querySelector:()=>alias};let saves=0;
context.document.getElementById=id=>elements[id];context.document.querySelector=()=>card;context.CSS={escape:s=>s};context.API={post:async()=>({voice_id:'saved'})};
context.getDesignerSynthesisInputs=()=>({description:'description',sample_text:'description'});context.window._currentPreviewFile='preview.wav';context.window._designerPreviewInputs={file:'preview.wav',description:'description',sample_text:'description'};
context.loadDesignedVoices=()=>{};context.isDesignerGenerationCurrent=()=>true;context.saveVoicesDebounced=()=>{saves++;};context.resetDesignerForm=()=>{};context.showToast=()=>{};context.showActionError=()=>{throw Error('unexpected');};context.getActionErrorMessage=()=>'';
vm.runInContext(scripts.slice(scripts.indexOf('function getDesignerSaveSnapshot()'),scripts.indexOf('window.playDesignedVoice =')),context);
(async()=>{await context.window.saveDesignedVoice();assert.equal(alias.value,'Zed');assert.equal(saves,1);
alias.value='Bob';context.API.post=async()=>{context.currentBookFilename='changed';return {voice_id:'saved'};};await context.window.saveDesignedVoice();assert.equal(alias.value,'Bob');assert.equal(saves,1);})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_metadata_refresh_rejects_book_switch_before_installing_roster(self):
        self.run_js(r'''
context.currentBookFilename='old';context.voiceSaveQueue={getRevision:()=>0,isDirty:()=>false};context.flushVoiceSaves=async()=>{};context.renderVoiceDrafts=()=>{};
context.API={get:async()=>{context.currentBookFilename='new';return {voices:[{name:'Other'}],revision:'a'.repeat(64),book_token:'b'.repeat(64)};}};
const refreshStart=source.indexOf('async function refreshVoiceMetadata()');vm.runInContext(source.slice(refreshStart,source.indexOf('let _voiceResourcesRefreshedAt',refreshStart)),context);
(async()=>{await assert.rejects(context.refreshVoiceMetadata());assert.deepEqual(context.window._voicesNames,['Zed','Alice','Alice','  Bob  ']);})().catch(e=>{console.error(e);process.exitCode=1;});
''')
