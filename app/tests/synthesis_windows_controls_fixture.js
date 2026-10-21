const assert=require('assert'),fs=require('fs'),vm=require('vm');
class E{
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.value='';this.checked=false;this.disabled=false;this.textContent='';this.id='';this.type='';}
 get value(){return this._value??'';} set value(v){this._value=String(v);}
 appendChild(c){c.parentElement=this;this.children.push(c);return c;}
 replaceChildren(...c){this.children=[];for(const n of c)this.appendChild(n);}
 descendants(){return this.children.flatMap(c=>[c,...c.descendants()]);}
 querySelector(s){const m=/data-limit="([^"]+)"/.exec(s);return this.descendants().find(n=>m&&n.dataset.limit===m[1])||null;}
 get labels(){return form.descendants().filter(e=>e.tagName==='LABEL'&&e.htmlFor===this.id);}
}
const form=new E('form');form.id='config-form';
for(const [id,tag] of [['synthesis-window-budgets','tbody'],['synthesis-windows-enabled','input'],['synthesis-window-options','fieldset'],['synthesis-window-status','p'],['synthesis-sentence-gap','input'],['synthesis-clause-gap','input'],['synthesis-paragraph-gap','input'],['synthesis-edge-trim','input'],['synthesis-fade','input'],['synthesis-crossfade','input'],['synthesis-quiet-threshold','input']]){const e=new E(tag);e.id=id;form.appendChild(e);}
const document={createElement:tag=>new E(tag),getElementById:id=>form.descendants().find(n=>n.id===id)||null};
const c={document};c.window=c;vm.createContext(c);
const core=fs.readFileSync(process.argv[4],'utf8');
vm.runInContext(core.slice(core.indexOf('function getConfigValidationError('),core.indexOf('function openConfigField(')),c);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),c);
const plain=v=>JSON.parse(JSON.stringify(v)),fixture=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));const out=[];
assert.deepStrictEqual(plain(c.getSynthesisWindowConfig()),{});out.push('not_loaded_omits_policy');
const load=()=>c.setSynthesisWindowControls(fixture.settings,fixture.modes);
load();assert.deepStrictEqual(plain(c.getSynthesisWindowConfig()).synthesis_windows,fixture.settings);out.push('all_8_budgets_and_7_seams_canonical_roundtrip');
assert.strictEqual(document.getElementById('synthesis-quiet-threshold').value,'-42.125');out.push('arbitrary_finite_threshold_preserved');
const rows=document.getElementById('synthesis-window-budgets').children;
assert.strictEqual(rows.length,8);for(const row of rows){for(const input of row.descendants().filter(e=>e.tagName==='INPUT')){assert(input.labels.length===1);assert(input.title.length>=25);assert(input.min==='1');}}out.push('all_dynamic_inputs_unique_associated_labels_and_help');
rows[0].querySelector('[data-limit="max_chars"]').value='777';const changed=plain(c.getSynthesisWindowConfig()).synthesis_windows;assert.strictEqual(changed.budgets[rows[0].dataset.budgetKey].max_chars,777);assert.deepStrictEqual(changed.budgets['external.clone'],fixture.settings.budgets['external.clone']);out.push('editing_one_category_keeps_other_modes');
for(const value of ['1.5','0','-1','Infinity','NaN','1000001']){load();document.getElementById('synthesis-window-budgets').children[0].querySelector('[data-limit="max_chars"]').value=value;assert.throws(()=>c.getSynthesisWindowConfig(),error=>error.fieldId===document.getElementById('synthesis-window-budgets').children[0].querySelector('[data-limit="max_chars"]').id);}out.push('invalid_chars_fraction_zero_negative_nonfinite_overflow_rejected');
load();let row=document.getElementById('synthesis-window-budgets').children[0];row.querySelector('[data-limit="max_chars"]').value='';row.querySelector('[data-limit="max_words"]').value='20';assert.throws(()=>c.getSynthesisWindowConfig(),/character limit/);out.push('word_without_chars_rejected');
load();for(const row of document.getElementById('synthesis-window-budgets').children){row.querySelector('[data-limit="max_chars"]').value='';row.querySelector('[data-limit="max_words"]').value='';}assert.throws(()=>c.getSynthesisWindowConfig(),/at least one/);out.push('enabled_empty_budgets_rejected');
document.getElementById('synthesis-windows-enabled').checked=false;assert.deepStrictEqual(plain(c.getSynthesisWindowConfig()).synthesis_windows.budgets,{});out.push('explicit_disabled_empty_budgets_allowed');
load();const captured=JSON.stringify(fixture.settings);c.setSynthesisWindowControls(fixture.settings,undefined);assert.deepStrictEqual(plain(c.getSynthesisWindowConfig()),{});assert(document.getElementById('synthesis-windows-enabled').disabled);assert(document.getElementById('synthesis-window-options').disabled);assert.strictEqual(captured,JSON.stringify(fixture.settings));out.push('missing_capabilities_disables_and_omits_without_input_mutation');
c.setSynthesisWindowControls({}, {'<img src=x onerror=evil>':{voice_categories:['<script>evil</script>']}});assert(form.descendants().every(e=>!['SCRIPT','IMG'].includes(e.tagName)));assert(document.getElementById('synthesis-window-budgets').children[0].children[0].textContent.includes('<img'));out.push('capability_labels_are_text_no_markup_injection');
assert.deepStrictEqual(plain(c.getSynthesisRecoveryConfig()),{});
for(const id of ['synthesis-recovery-enabled','synthesis-recovery-budget']){const e=new E('input');e.id=id;form.appendChild(e);}
c.setSynthesisRecoveryControls(undefined);assert.deepStrictEqual(plain(c.getSynthesisRecoveryConfig()),{synthesis_recovery:{enabled:false,max_audio_mb:256}});out.push('recovery_default_off_and_loaded_controls_roundtrip');
c.setSynthesisRecoveryControls({enabled:true,max_audio_mb:64});assert.deepStrictEqual(plain(c.getSynthesisRecoveryConfig()),{synthesis_recovery:{enabled:true,max_audio_mb:64}});
document.getElementById('synthesis-recovery-enabled').checked=false;assert.strictEqual(plain(c.getSynthesisRecoveryConfig()).synthesis_recovery.max_audio_mb,64);out.push('disabling_recovery_keeps_budget');
for(const value of ['15','4097','16.5','Infinity','NaN']){document.getElementById('synthesis-recovery-budget').value=value;assert.throws(()=>c.getSynthesisRecoveryConfig(),error=>error.fieldId==='synthesis-recovery-budget');}out.push('invalid_recovery_budgets_rejected');
assert(core.includes('window.setSynthesisRecoveryControls?.(config.tts?.synthesis_recovery)'));assert(core.includes("typeof getSynthesisRecoveryConfig === 'function' ? getSynthesisRecoveryConfig() : {}"));out.push('recovery_load_and_save_wired');
async function verifyClear(){
 c.currentBookFilename='book.epub';c.chunkSnapshotBook='book.epub';c.chunkSnapshotBookToken=null;
 const calls=[],toasts=[];c.API={post:async(path,body)=>{calls.push({path,body:plain(body)});}};c.showToast=(...args)=>toasts.push(args);
 vm.runInContext(core.slice(core.indexOf('window.clearSynthesisRecoveryWindows ='),core.indexOf('window.differentTake =')),c);
 await c.clearSynthesisRecoveryWindows();assert.strictEqual(calls.length,0);assert(toasts.pop()[0].includes('Open Editor'));out.push('clear_requires_loaded_editor_identity');
 c.chunkSnapshotBookToken='a'.repeat(64);await c.clearSynthesisRecoveryWindows();assert.deepStrictEqual(calls.pop(),{path:'/api/tts/recovery/clear',body:{expected_book_token:'a'.repeat(64)}});assert.strictEqual(toasts.pop()[1],'success');out.push('clear_posts_captured_book_token');
 let release;c.API.post=()=>new Promise(resolve=>{release=resolve;});const pending=c.clearSynthesisRecoveryWindows();c.currentBookFilename='other.epub';release();await pending;assert.strictEqual(toasts.length,0);out.push('clear_suppresses_stale_book_success');
 c.currentBookFilename='book.epub';c.API.post=async()=>{throw new Error('Tasks are running');};await c.clearSynthesisRecoveryWindows();assert.deepStrictEqual(toasts.pop(),['Tasks are running','error']);out.push('clear_surfaces_server_refusal');
}
verifyClear().then(()=>{const result={passed:out.length,checks:out,scope:'CPU minimal DOM fixture executes actual production helper. No native DOM constraint-validation/browser behavior claimed.'};console.log(JSON.stringify({...result, roundtrip: fixture.settings, changed}));}).catch(error=>{console.error(error);process.exitCode=1;});
