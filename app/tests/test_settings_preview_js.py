"""Settings preview captures the real form and refuses stale or unsafe replies."""
from pathlib import Path
import json
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE
from tests import test_prompt_preset_transactions_js as actions

STATIC=Path(__file__).resolve().parent.parent/'static'
SETUP=r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}this.textContent='';this.append(...children);};
Element.prototype.focus=function(){document.activeElement=this;};
const form=body.appendChild(new Element('form',{id:'config-form'}));
const html=fs.readFileSync(process.argv[2],'utf8');
function parseMarkupAttrs(markup){return Object.fromEntries([...markup.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(attr=>[attr[1],attr[2]??'']));}
for(const match of html.matchAll(/<([a-z0-9]+)\b([^>]*id="setup-preview-[^"]+"[^>]*)>/g)){form.appendChild(new Element(match[1],parseMarkupAttrs(match[2])));}
for(const match of html.matchAll(/<([a-z0-9]+)\b([^>]*data-setup-message="(preview[^"]+)"[^>]*)>([^<]*)<\/\1>/g)){
 const attrs=parseMarkupAttrs(match[2]);
 const node=document.getElementById(attrs.id)||form.appendChild(new Element(match[1],attrs));
 for(const [key,value] of Object.entries(attrs)){node.setAttribute(key,value);}node.textContent=match[4];
}
const searchResults=body.appendChild(new Element('div',{id:'setup-settings-results',hidden:''}));
body.appendChild(new Element('div',{id:'setup-settings-search-status'}));
const input=form.appendChild(new Element('input',{id:'synthetic-input'}));
const calls=[],errors=[];let proposal={llm_mode:'local',llm:{api_key:'PRIVATE_SYNTHETIC_KEY'},tts:{parallel_workers:2}};
context.configSavePending=false;context.getConfigPreviewProposal=()=>JSON.parse(JSON.stringify(proposal));
context.showConfigValidationError=error=>errors.push(error.message);
context.applySystemStatusText=(id,text)=>{document.getElementById(id).textContent=text;};
function response(){return {changes:[{path:'tts.parallel_workers',label:'Parallel workers',scope:'Audio generation',before:'2',after:'3',was_default:false}],defaults:[],active_after:'local',baseline:'read',warning_count:0,unmanaged_fields:0,mirror_alignment:false,notice:'Defaults and URL normalization are included.',timing:'Preview does not apply changes.'};}
context.API={post:async(path,payload)=>{calls.push({path,payload});return response();}};
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));let finished=false;process.on('beforeExit',()=>assert(finished,'scenario must finish assertions'));
(async()=>{
'''


class SettingsPreviewJsTests(unittest.TestCase):
    def run_js(self,code):
        result=subprocess.run(['node','-e',DOM_FIXTURE+SETUP+code+"\nfinished=true;})().catch(error=>{console.error(error);process.exitCode=1;});",str(STATIC/'js/app-settings.js'),str(STATIC/'index.html')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_real_payload_builder_captures_active_and_inactive_profiles_without_mutation(self):
        code=r'''
const begin=source.indexOf('function getParallelWorkersInput(');const end=source.indexOf('function buildConfigPayload(',begin);assert(begin>=0&&end>begin);run(source.slice(begin,end));seed();
for(const location of ['local','remote']){
 context.currentLlmMode=location;context.llmProfiles={local:{base_url:'http://local/v1',api_key:'saved local',model_name:'local',max_output_tokens:null},remote:{base_url:'http://remote/v1',api_key:'saved remote',model_name:'remote',max_output_tokens:2000}};
 element('llm-url').value='http://edited/v1';element('llm-key').value='edited synthetic';element('llm-model').value='edited model';element('llm-max-output-tokens').value='';element('llm-top-k').value='0';element('parallel-workers').value='-2';
 element('llm-provider-headers').value='{"Authorization":"synthetic"}';element('llm-provider-extra-body').value='{"reasoning":false}';
 const profilesBefore=JSON.stringify(context.llmProfiles),fieldsBefore=Object.entries(elements).map(([id,node])=>[id,JSON.stringify(node)]),promptsBefore=snapshot();
 const proposal=context.getConfigPreviewProposal();assert.strictEqual(JSON.stringify(context.llmProfiles),profilesBefore);for(const [id,value] of fieldsBefore){assert.strictEqual(JSON.stringify(elements[id]),value,'Preview changed '+id);}assert.strictEqual(snapshot(),promptsBefore);
 assert.strictEqual(proposal['llm_'+location].model_name,'edited model');assert.strictEqual(proposal['llm_'+location].max_output_tokens,null);assert.strictEqual(proposal.tts.parallel_workers,1);
 context.syncCurrentLlmProfile();const actual=context.buildConfigPayload(context.getParallelWorkersInput());assert.strictEqual(JSON.stringify(proposal),JSON.stringify(actual));
}
'''
        actions.PromptPresetTransactionTests().run_case(code)

    def test_programmatic_generation_and_preset_writers_invalidate_previews(self):
        code=r'''
seed();let invalidations=0;context.window.invalidateSetupPreview=()=>invalidations++;
context.applyPromptPreset(0);assert.strictEqual(invalidations,1);context.applyPassPromptPreset('pass1',0);assert.strictEqual(invalidations,2);
'''
        actions.PromptPresetTransactionTests().run_case(code)
        core=(STATIC/'js/app-core.js').read_text()
        for signature in ['function applyGenerationSettings(g) {','function populateLlmInputs(mode) {']:
            self.assertIn('window.invalidateSetupPreview?.();',core[core.index(signature):core.index(signature)+140])
        self.assertIn('window.invalidateSetupPreview?.();\n            configSavePending = true;',core)

    def test_preview_is_a_post_to_preview_only_and_renders_text_not_html(self):
        self.run_js(r'''
context.API.post=async(path,payload)=>{calls.push({path,payload});const d=response();d.changes[0].after='<img onerror=bad()>';return d;};await context.previewSetupChanges();assert.strictEqual(calls.length,1);assert.strictEqual(calls[0].path,'/api/config/preview');assert.strictEqual(document.getElementById('setup-preview-results').hidden,false);assert.strictEqual(body.querySelectorAll('img').length,0);assert.strictEqual(document.getElementById('setup-preview-changes').children[0].children[2].children[1].children[1].textContent,'<img onerror=bad()>');assert(document.getElementById('setup-preview-status').textContent.includes('No settings were applied'));
''')

    def test_locale_changes_update_headings_without_replacing_evidence_or_requesting(self):
        self.run_js(r'''
context.API.post=async(path,payload)=>{calls.push({path,payload});return {...response(),baseline:'damaged',warning_count:1,changes:[{path:'tts.mode',label:'Mode <img>',scope:'Audio generation',before:'local',after:'remote <script>',was_default:true}],defaults:[{path:'tts.parallel_workers',label:'Workers',scope:'Audio generation',before:'2',after:'3',was_default:true}],unmanaged_fields:2};};
await context.previewSetupChanges();
const results=document.getElementById('setup-preview-results');
const row=document.getElementById('setup-preview-changes').children[0];
const saved=row.children[2].children[0],proposed=row.children[2].children[1];
assert.strictEqual(saved.children[0].textContent,'Saved');assert.strictEqual(proposed.children[0].textContent,'Would save');
const defaultRow=document.getElementById('setup-preview-defaults').children[0];
const warnings=['status','notice','timing','unmanaged','active'].map(suffix=>document.getElementById('setup-preview-'+suffix).textContent);
const before=JSON.stringify(calls[0].payload);input.focus();
for(const locale of ['qps-ploc','qps-ploc','unavailable','en']){
 context.applySetupUiLocale(locale);
 assert.strictEqual(document.getElementById('setup-preview-changes').children[0],row);
 assert.strictEqual(document.getElementById('setup-preview-defaults').children[0],defaultRow);
 assert.strictEqual(document.activeElement,input);assert.strictEqual(results.hidden,false);
 assert.strictEqual(saved.children[0].textContent,locale==='qps-ploc'?'[Saaveed]':'Saved');
 assert.strictEqual(proposed.children[0].textContent,locale==='qps-ploc'?'[Woouuld saavee]':'Would save');
 assert.strictEqual(defaultRow.children[2].children[0].children[0].textContent,saved.children[0].textContent);
 assert.strictEqual(saved.children[1].textContent,'local (default)');assert.strictEqual(proposed.children[1].textContent,'remote <script>');
 assert.strictEqual(row.children[0].textContent,'Mode <img>');assert.strictEqual(row.children[1].textContent,'Audio generation');
 assert.deepStrictEqual(['status','notice','timing','unmanaged','active'].map(suffix=>document.getElementById('setup-preview-'+suffix).textContent),warnings);
 assert.strictEqual(JSON.stringify(calls[0].payload),before);assert.strictEqual(calls.length,1);assert.strictEqual(body.querySelectorAll('script,img').length,0);
}
''')

    def test_pending_preview_uses_current_locale_without_reposting(self):
        self.run_js(r'''
let release;context.API.post=(path,payload)=>{calls.push({path,payload});return new Promise(resolve=>release=resolve);};
const pending=context.previewSetupChanges();await tick();context.applySetupUiLocale('qps-ploc');release(response());await pending;
const row=document.getElementById('setup-preview-changes').children[0];
assert.strictEqual(row.children[2].children[0].children[0].textContent,'[Saaveed]');
assert.strictEqual(row.children[2].children[1].children[0].textContent,'[Woouuld saavee]');
assert.strictEqual(calls.length,1);assert.strictEqual(document.getElementById('setup-preview-results').hidden,false);
''')

    def test_preview_chrome_localizes_real_markup_and_keeps_actions_and_evidence(self):
        self.run_js(r'''
const keys=['previewAction','previewTitle','previewClose'];
const nodes=keys.map(key=>body.querySelectorAll('[data-setup-message]').find(node=>node.getAttribute('data-setup-message')===key));
assert(nodes.every(Boolean));assert.deepStrictEqual(nodes.map(node=>node.textContent),['Preview changes','Settings change preview','Close preview']);
assert.strictEqual(nodes[0].hasAttribute('data-config-save-action'),true);
assert.strictEqual(nodes[1].tagName,'H5');assert.strictEqual(nodes[1].getAttribute('tabindex'),'-1');
const panel=document.getElementById('setup-preview-panel');assert.strictEqual(panel.hidden,true);
const attrs=nodes.map(node=>JSON.stringify(node.attrs));
vm.runInContext(nodes[0].getAttribute('onclick'),context);await tick();
assert.strictEqual(panel.hidden,false);
const row=document.getElementById('setup-preview-changes').children[0];
const summary=document.getElementById('setup-preview-default-summary').textContent;assert(summary.length>0);
const warning=document.getElementById('setup-preview-status').textContent;assert(warning.includes('No settings were applied'));
for(const locale of ['qps-ploc','qps-ploc','unsupported','en']){
 context.applySetupUiLocale(locale);
 assert.deepStrictEqual(nodes.map(node=>node.textContent),locale==='qps-ploc'?['[Preeviieew chaangees]','[Seettiings chaangee preeviieew]','[Cloosee preeviieew]']:['Preview changes','Settings change preview','Close preview']);
 assert.deepStrictEqual(nodes.map(node=>JSON.stringify(node.attrs)),attrs);
 assert.strictEqual(panel.hidden,false);
 assert.strictEqual(document.getElementById('setup-preview-changes').children[0],row);
 assert.strictEqual(document.getElementById('setup-preview-results').hidden,false);
 assert.strictEqual(document.getElementById('setup-preview-default-summary').textContent,locale==='qps-ploc'?'[0 miissiing seettiings fiilleed wiith deefaauults]':summary);
 assert.strictEqual(document.getElementById('setup-preview-status').textContent,warning);
 assert.strictEqual(calls.length,1);
}
vm.runInContext(nodes[2].getAttribute('onclick'),context);
assert.strictEqual(document.getElementById('setup-preview-panel').hidden,true);
assert.strictEqual(document.getElementById('setup-preview-results').hidden,true);assert.strictEqual(calls.length,1);
''')

    def test_default_summary_preserves_counts_across_locales_and_pending_responses(self):
        self.run_js(r'''
let release;context.API.post=(path,payload)=>{calls.push({path,payload});return new Promise(resolve=>release=resolve);};
for(const count of [0,1,500]){
 const pending=context.previewSetupChanges();await tick();context.applySetupUiLocale('qps-ploc');
 const data=response();data.defaults=Array.from({length:count},()=>({...data.changes[0],was_default:true}));release(data);await pending;
 const summary=document.getElementById('setup-preview-default-summary');
 assert.strictEqual(summary.textContent,'['+count+' miissiing seettiings fiilleed wiith deefaauults]');
 assert.strictEqual(summary.getAttribute('data-setup-count'),String(count));
 const row=document.getElementById('setup-preview-defaults').children[0];const payload=JSON.stringify(calls[calls.length-1].payload);const requests=calls.length;
 for(const locale of ['qps-ploc','qps-ploc','unsupported','en']){
  context.applySetupUiLocale(locale);
  assert.strictEqual(summary.textContent,locale==='qps-ploc'?'['+count+' miissiing seettiings fiilleed wiith deefaauults]':count+' missing settings filled with defaults');
  assert.strictEqual(document.getElementById('setup-preview-default-details').hidden,!count);
  assert.strictEqual(document.getElementById('setup-preview-defaults').children.length,count);
  assert.strictEqual(document.getElementById('setup-preview-defaults').children[0],row);
  assert.strictEqual(JSON.stringify(calls[calls.length-1].payload),payload);assert.strictEqual(calls.length,requests);
 }
}
''')

    def test_edits_close_and_save_invalidation_refuse_pending_results(self):
        self.run_js(r'''
let release;context.API.post=()=>new Promise(resolve=>release=resolve);const first=context.previewSetupChanges();await tick();input.fire('input');release(response());await first;assert.strictEqual(document.getElementById('setup-preview-results').hidden,true);
const second=context.previewSetupChanges();await tick();context.closeSetupPreview();release(response());await second;assert.strictEqual(document.getElementById('setup-preview-panel').hidden,true);
const third=context.previewSetupChanges();await tick();context.window.invalidateSetupPreview();release(response());await third;assert.strictEqual(document.getElementById('setup-preview-results').hidden,true);
''')

    def test_newest_request_is_serialized_and_direct_state_changes_are_rechecked(self):
        self.run_js(r'''
const gates=[];context.API.post=(path,payload)=>{calls.push({path,payload});return new Promise(resolve=>gates.push(resolve));};const a=context.previewSetupChanges();await tick();proposal.tts.parallel_workers=4;const b=context.previewSetupChanges();const c=context.previewSetupChanges();assert.strictEqual(calls.length,1);gates.shift()(response());await tick();assert.strictEqual(calls.length,2);assert.strictEqual(calls[1].payload.tts.parallel_workers,4);gates.shift()(response());await Promise.all([a,b,c]);assert.strictEqual(document.getElementById('setup-preview-results').hidden,false);
const d=context.previewSetupChanges();await tick();proposal.llm_mode='remote';gates.shift()(response());await d;assert.strictEqual(document.getElementById('setup-preview-results').hidden,true);assert(document.getElementById('setup-preview-status').textContent.includes('Preview again'));
''')

    def test_missing_damaged_and_defaults_are_never_called_unchanged(self):
        self.run_js(r'''
for(const baseline of ['missing','damaged']){context.API.post=async()=>({...response(),changes:[],baseline,warning_count:1,defaults:[{path:'tts.mode',label:'Mode',scope:'Audio generation',before:'local',after:'local',was_default:true}],unmanaged_fields:2});await context.previewSetupChanges();assert.strictEqual(document.getElementById('setup-preview-results').hidden,false);assert.strictEqual(document.getElementById('setup-preview-default-details').hidden,false);assert(document.getElementById('setup-preview-unmanaged').textContent.includes('2 additional'));assert(!document.getElementById('setup-preview-status').textContent.includes('unchanged'));}
''')

    def test_invalid_proposal_and_bad_or_failed_reply_clear_old_comparison(self):
        self.run_js(r'''
await context.previewSetupChanges();context.getConfigPreviewProposal=()=>{throw Error('Invalid numeric setting');};await context.previewSetupChanges();assert.strictEqual(errors.length,1);assert.strictEqual(calls.length,1);assert.strictEqual(document.getElementById('setup-preview-results').hidden,true);
context.getConfigPreviewProposal=()=>proposal;for(const reply of [null,{...response(),baseline:'certified'},{...response(),changes:[{}]}]){context.API.post=async()=>reply;await context.previewSetupChanges();assert.strictEqual(document.getElementById('setup-preview-results').hidden,true);assert(document.getElementById('setup-preview-status').textContent.includes('Preview unavailable'));}
context.API.post=async()=>{throw Error('PRIVATE_SYNTHETIC_SERVER_DETAIL');};await context.previewSetupChanges();assert(!document.getElementById('setup-preview-status').textContent.includes('PRIVATE_SYNTHETIC'));
context.configSavePending=true;const count=calls.length;await context.previewSetupChanges();assert.strictEqual(calls.length,count);
''')
