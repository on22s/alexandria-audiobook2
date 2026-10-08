"""Real UI handlers under delayed acknowledgements and newer local actions."""
from pathlib import Path
import subprocess
import unittest
from tests import test_dataset_ui_ownership_js as dataset
from tests import test_training_ui_contract as training
ROOT=Path(__file__).resolve().parent.parent/'static/js'
SETUP=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');const core=fs.readFileSync(process.argv[1],'utf8'),scripts=fs.readFileSync(process.argv[2],'utf8');
const fields={},errors=[],toasts=[];function el(id){return fields[id]||(fields[id]={style:{},textContent:'',innerHTML:'',disabled:false});}function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};}
const selected={value:'original',checked:false},style={value:'old',checked:false},body={querySelector:q=>q.includes('select')?selected:q.includes('style')?style:null},card={querySelector:q=>q==='.card-body'?body:null,querySelectorAll:()=>[selected,style]};
const c={window:null,currentBookFilename:'book',CSS:{escape:x=>x},document:{getElementById:el,querySelector:()=>card,querySelectorAll:()=>[]},showToast:(...args)=>toasts.push(args),showActionError:(...args)=>errors.push(args),loadCastLibrary:async()=>{},escapeHtml:String,API:{}};c.window=c;vm.createContext(c);function run(text){return vm.runInContext(text,c);}
let finished=false;process.on('beforeExit',()=>assert(finished,'async assertions must finish'));
(async()=>{
'''
class VoiceUiTests(unittest.TestCase):
    def run_js(self,code):
        result=subprocess.run(['node','-e',SETUP+code+'\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});',str(ROOT/'app-core.js'),str(ROOT/'app-scripts.js')],capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_empty_ensemble_and_missing_prefill(self):
        self.run_js(r'''
run(core.slice(core.indexOf('function ensembleMembersMarkup('),core.indexOf('window.toggleVoiceType =')));c._voicesNames=['A','B'];c.suggestEnsembleMembers=()=>['B'];assert(!c.ensembleMembersMarkup('A',[]).includes(' checked'));assert(c.ensembleMembersMarkup('A',undefined).includes(' checked'));assert(c.ensembleMembersMarkup('A',['B']).includes(' checked'));
''')

    def test_pending_single_ack_after_dismissal(self):
        self.run_js(r'''
run(core.slice(core.indexOf('function applySuggestionToCard('),core.indexOf('function updateSuggestionToolbar(')));c.updateSuggestionToolbar=()=>{};c._voiceSuggestionContext={isCurrent:()=>true};const gate=deferred();let packet;c.API.post=async(url,data)=>{packet=JSON.parse(JSON.stringify(data));return gate.promise;};c._voiceSuggestions={A:{type:'lora',adapter_id:'submitted',character_style:'submitted style'}};const pending=c.applyVoiceSuggestion('A');c.clearVoiceSuggestions();gate.resolve({});await pending;assert.equal(packet.suggestion.adapter_id,'submitted');assert.equal(selected.value,'submitted');assert.equal(style.value,'submitted style');assert(!c._voiceSuggestions.A);
''')

    def test_bulk_ack_preserves_newer_suggestion_edit_book(self):
        self.run_js(r'''
run(core.slice(core.indexOf('function applySuggestionToCard('),core.indexOf('function updateSuggestionToolbar(')));c.updateSuggestionToolbar=()=>{};
for(const change of ['suggestion','edit','book','unchanged']){c.currentBookFilename='book';c._voiceSuggestions={A:{type:'lora',adapter_id:'submitted',character_style:'old'}};selected.value='original';const gate=deferred();let packet;c.API.post=async(url,data)=>{packet=data;return gate.promise;};const pending=c.applyAllVoiceSuggestions();if(change==='suggestion'){c._voiceSuggestions={A:{type:'lora',adapter_id:'new'}};}if(change==='edit'){selected.value='local';}if(change==='book'){c.currentBookFilename='other';}gate.resolve({});await pending;assert.equal(packet.suggestions.A.adapter_id,'submitted');assert.equal(selected.value,change==='edit'?'local':change==='unchanged'?'submitted':'original');if(change==='suggestion'){assert.equal(c._voiceSuggestions.A.adapter_id,'new');}}
assert(toasts.some(t=>t[0].includes('newer edits')));
''')

    def test_bulk_cast_pending_failure_retry(self):
        self.run_js(r'''
run(core.slice(core.indexOf('let castApplyBulkPending ='),core.indexOf('// Identity anchors that take over')));c._collectCastApplyMapping=()=>({A:'role'});c.setCastStatus=()=>{};c.getActionErrorMessage=(title,e)=>title+e.message;c.getCastApplyWarningsHtml=()=>'';c._selectedCast='cast';let count=0,gate=deferred();c.API.post=()=>{count++;return gate.promise;};const first=c.submitCastApplyBulk(['book']);await c.submitCastApplyBulk(['book']);assert.equal(count,1);assert(el('btn-cast-apply-bulk-submit').disabled);gate.reject(Error('offline'));await first;assert(!el('btn-cast-apply-bulk-submit').disabled);gate=deferred();const retry=c.submitCastApplyBulk(['book']);assert.equal(count,2);gate.resolve({results:[{name:'book',count:1}]});await retry;assert(el('cast-panel').innerHTML.includes('Applied to 1 book'));assert(!el('btn-cast-apply-bulk-submit').disabled);
''')

    def test_confirmed_delete_refresh_error(self):
        self.run_js(r'''
run(scripts.slice(scripts.indexOf('window.deleteDesignedVoice ='),scripts.indexOf('window.openDesignedVoiceForEdit =')));let deleted=0;c.applyLibraryVoiceRemoval=async(key,button,message,current,remove)=>remove();c.fetch=async()=>{deleted++;return{ok:true};};c.loadDesignedVoices=async()=>{throw Error('offline');};c.loadVoices=async()=>{};await c.deleteDesignedVoice('x');assert.equal(deleted,1);assert.equal(errors[0][0],'Voice deleted; list refresh failed');assert(toasts.some(t=>t[0].includes('deleted')));
''')

    def test_panel_shared_ownership(self):
        training.TrainingUiContractTests().run_js(r'''
for(const fail of [false,true]){const pending=[];vm.runInContext('window.API=API',ctx);ctx.API.get=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));ctx.API.post=async()=>({session_id:'new',pairs:[]});element('lora-comparison-panel').scrollIntoView=()=>{};const old=ctx.openLoraCandidateComparison('old');await ctx.openLoraBlindReview('new');const current=element('lora-comparison-panel').innerHTML;if(fail){pending[0].reject(Error('old error'));}else{pending[0].resolve({candidate_id:'old',probe_pairs:[]});}await old;assert.equal(element('lora-comparison-panel').innerHTML,current);const history=ctx.openLoraReviewHistory('history');await ctx.openLoraBlindReview('newer');const newer=element('lora-comparison-panel').innerHTML;pending[1].resolve({reviews:[]});await history;assert.equal(element('lora-comparison-panel').innerHTML,newer);}ctx.API.get=async()=>({candidate_id:'current',probe_pairs:[]});await ctx.openLoraCandidateComparison('current');assert(element('lora-comparison-panel').innerHTML.includes('current'));
''')

    def test_backup_probe_unknown_visible_and_retries(self):
        training.TrainingUiContractTests().run_js(r'''
vm.runInContext('window.API=API',ctx);ctx.API.get=async path=>{if(path.endsWith('/backups')){throw Error('offline');}return[{id:'adapter',name:'Adapter'}];};await ctx.loadLoraModels();assert(element('lora-models-list').innerHTML.includes('Adapter'));assert(element('lora-models-refresh-status').textContent.includes('unavailable'));assert(!element('lora-models-refresh-retry').hidden);ctx.API.get=async path=>path.endsWith('/backups')?{backups:[],total_size_bytes:0,free_bytes:9000000000,low_space_warning:false}:[{id:'adapter',name:'Adapter'}];await ctx.loadLoraModels();assert.equal(element('lora-models-refresh-status').textContent,'');assert(element('lora-models-refresh-retry').hidden);
''')

    def test_sample_ownership_then_edit_after_ack(self):
        dataset.DatasetUiOwnershipJsTests().run_scenario(r'''
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'original',emotion:'warm',seed:0,status:'pending'}]");context.document.getElementById('dsb-description').value='voice';context.showToast=()=>{};let finish,count=0;context.API.post=()=>{count++;return new Promise(resolve=>finish=resolve);};const pending=context.dsbGenSample(0);assert.equal(run('dsbRows[0].status'),'generating');context.dsbUpdateRow(0,'text','changed');await context.dsbRemoveRow(0);await context.dsbGenSample(0);await context.dsbGenerateAll();assert.equal(count,1);assert.equal(run('dsbRows.length'),1);assert.equal(run('dsbRows[0].text'),'original');assert(run('dsbBuildRowHtml(dsbRows[0],0)').includes('disabled'));finish({audio_url:'/ack.wav'});await pending;assert.equal(run('dsbRows[0].status'),'done');assert.equal(run('dsbRows[0].audio_url'),'/ack.wav');context.dsbUpdateRow(0,'text','after');assert.equal(run('dsbRows[0].text'),'after');assert.equal(run('dsbRows[0].status'),'pending');
''')
