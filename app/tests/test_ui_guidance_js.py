"""Execute guidance state and reload cancellation with the native handlers."""
from pathlib import Path
import subprocess
import unittest

from tests.test_ui_help_js import DOM_FIXTURE

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-core.js'

# The Setup header button exactly as index.html authors it (#2016): an icon and
# a data-i18n span inside the button, plus the eight fields Auto-Configure reads,
# applies and re-reads. The authored nodes are kept so a test can prove they were
# never replaced rather than re-created.
AUTO_CONFIGURE_FIXTURE = r'''
Element.prototype.querySelector=function(selector){return this.querySelectorAll(selector)[0]||null;};
Element.prototype.scrollIntoView=function(){};
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
// Real innerHTML assignment replaces every child, which is exactly the damage
// #2016 was: a button whose authored span silently disappeared.
Object.defineProperty(Element.prototype,'innerHTML',{get(){return this.htmlText||'';},set(value){this.htmlText=String(value);for(const child of [...this.children]){child.remove();}}});
context.escapeHtml=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
const pendingRequests=[],toasts=[];
context.API={get:()=>new Promise((resolve,reject)=>pendingRequests.push({resolve,reject}))};
context.showToast=(...args)=>toasts.push(args);
context.showActionError=(...args)=>toasts.push(args);
context.toggleTTSMode=()=>{};context.toggleSubBatchFields=()=>{};
function buildButton(){
 const button=body.appendChild(new Element('button',{id:'btn-auto-configure',type:'button',class:'btn btn-sm btn-outline-info'}));
 button.appendChild(new Element('i',{class:'fas fa-magic me-1'}));
 const span=button.appendChild(new Element('span',{'data-i18n':'setup.header.auto-configure'}));
 span.textContent='Auto-Configure';
 return button;
}
function buildFields(){
 for(const [id,property,value] of [['tts-mode','value','local'],['parallel-workers','value',1],['compile-codec','checked',false],['batch-group-by-type','checked',true],['sub-batch-enabled','checked',true],['sub-batch-min-size','value',4],['sub-batch-ratio','value',5],['sub-batch-max-items','value',4]]){
  const field=body.appendChild(new Element('input',{id}));field[property]=value;
 }
 const banner=body.appendChild(new Element('div',{id:'auto-config-banner'}));banner.style.display='none';
 body.appendChild(new Element('div',{id:'auto-config-msg'}));
}
function catalog(messages){return {meta:{review_status:{all:'machine-draft',safety:'not-reviewed'}},messages};}
function setUiCatalog(locale,payload){vm.runInContext('uiCatalogs.set('+JSON.stringify(locale)+','+JSON.stringify(payload)+')',context);}
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),context);
const source=fs.readFileSync(process.argv[1],'utf8');
const a=source.indexOf('async function autoConfigureSettings()');
vm.runInContext(source.slice(a,source.indexOf('// Local/Remote LLM profile state.',a)),context);
const SPANISH=catalog({
 'setup.header.auto-configure':'Configuración automática',
 'setup.auto-config.detecting':'Detectando…',
 'setup.auto-config.suggested':'Ajustes sugeridos: {summary}. Revisa los ajustes de TTS a continuación.',
 'setup.auto-config.suggested-external':'Ajustes sugeridos: {summary}. Se seleccionó TTS externo: inicia un servidor de TTS compatible antes de generar audio.',
 'setup.auto-config.settings-changed':'Los ajustes de TTS cambiaron durante la detección. Revísalos antes de volver a ejecutar la configuración automática.',
 'setup.auto-config.failed':'No se pudo detectar el hardware',
 'setup.auto-config.failed-recovery':'Revisa los detalles del hardware y los ajustes de TTS.',
});
'''

# Success path: the authored span and icon survive the busy state, the banner
# and every string are translated, and a later language switch still reaches the
# button (#2016: replacing innerHTML made it stay English for good).
AUTO_CONFIGURE_LOCALE_JS = r'''
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{
const button=buildButton();buildFields();
const authored=button.children[1],authoredIcon=button.children[0];
const span=()=>button.querySelector('[data-i18n]');
setUiCatalog('es-419',SPANISH);await context.applyUiLocale('es-419');
assert.strictEqual(span(),authored);assert.strictEqual(span().textContent,'Configuración automática');
const pending=context.autoConfigureSettings();
assert(span(),'while detecting the button still holds its data-i18n span');
assert.strictEqual(span(),authored);
assert.strictEqual(span().textContent,'Detectando…','busy label is the translated key');
assert.strictEqual(span().getAttribute('data-i18n'),'setup.auto-config.detecting');
assert.strictEqual(button.querySelector('i').className,'fas fa-spinner fa-spin me-1');
assert(button.disabled);
pendingRequests.at(-1).resolve({gpu:{total_gb:16}});await pending;
assert.strictEqual(span(),authored,'the authored span is never replaced');
assert.strictEqual(button.querySelector('i'),authoredIcon);
assert.strictEqual(button.querySelector('i').className,'fas fa-magic me-1');
assert.strictEqual(span().getAttribute('data-i18n'),'setup.header.auto-configure');
assert.strictEqual(span().textContent,'Configuración automática');
assert.strictEqual(button.disabled,false);
const msg=document.getElementById('auto-config-msg').innerHTML;
assert(msg.includes('data-i18n="setup.auto-config.suggested"'),msg);
assert(msg.includes('Ajustes sugeridos: High VRAM (16.0 GB)'),msg);
assert(!msg.includes('>Suggested settings'),'the rendered banner text is the translation, not the authored English');
await context.applyUiLocale('en');
assert.strictEqual(span().getAttribute('data-i18n'),'setup.header.auto-configure');
assert.strictEqual(span().textContent,'Auto-Configure');
setUiCatalog('ja',catalog({'setup.header.auto-configure':'自動設定'}));await context.applyUiLocale('ja');
assert.strictEqual(span().textContent,'自動設定','a language switch still reaches the button');
await context.applyUiLocale('en');assert.strictEqual(span().textContent,'Auto-Configure');
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''

# Failure path and the other English-only strings: the busy label, the toast, the
# external banner sentence, the action error and its recovery text.
AUTO_CONFIGURE_ERRORS_JS = r'''
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{
const button=buildButton();buildFields();
const span=()=>button.querySelector('[data-i18n]');
setUiCatalog('es-419',SPANISH);await context.applyUiLocale('es-419');
const failing=context.autoConfigureSettings();
assert(span(),'while detecting the button still holds its data-i18n span');
assert.strictEqual(span().textContent,'Detectando…');
pendingRequests.at(-1).reject(Error('no hardware'));await failing;
assert.strictEqual(toasts.length,1);
assert.strictEqual(toasts[0][0],'No se pudo detectar el hardware');
assert.strictEqual(toasts[0][1].message,'no hardware');
assert.strictEqual(toasts[0][2],'Revisa los detalles del hardware y los ajustes de TTS.');
assert.strictEqual(span().getAttribute('data-i18n'),'setup.header.auto-configure');
assert.strictEqual(span().textContent,'Configuración automática','an error also restores the button');
assert.strictEqual(button.querySelector('i').className,'fas fa-magic me-1');
assert.strictEqual(button.disabled,false);
const changed=context.autoConfigureSettings();
document.getElementById('tts-mode').value='external';
pendingRequests.at(-1).resolve({gpu:{total_gb:16}});await changed;
assert.strictEqual(toasts.at(-1)[0],'Los ajustes de TTS cambiaron durante la detección. Revísalos antes de volver a ejecutar la configuración automática.');
assert.strictEqual(toasts.at(-1)[1],'warning');
const external=context.autoConfigureSettings();
pendingRequests.at(-1).resolve({gpu:{total_gb:4}});await external;
const msg=document.getElementById('auto-config-msg').innerHTML;
assert(msg.includes('data-i18n="setup.auto-config.suggested-external"'),msg);
assert(msg.includes('Se seleccionó TTS externo'),msg);
assert.strictEqual(button.disabled,false);
assert.strictEqual(span().textContent,'Configuración automática');
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''
class UiGuidanceJsTests(unittest.TestCase):
    def test_theme_switch_finishes_color_transitions_without_finishing_other_motion(self):
        self.run_js(r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
const finished=[],fields={};let theme='light';
class ColorTransition{constructor(property){this.transitionProperty=property;}finish(){finished.push(this.transitionProperty);}}
const transitions=['background-color','color','border-left-color','box-shadow','transform'].map(property=>new ColorTransition(property));
transitions.push({transitionProperty:'background-color',finish(){throw Error('A progress animation must not be finished');}});
const context={CSSTransition:ColorTransition,localStorage:{setItem(){}},document:{documentElement:{removeAttribute(){theme='light';},setAttribute(name,value){theme=value;},getAttribute(){return theme;}},getAnimations(){assert.equal(theme,'night');return transitions;},getElementById:id=>fields[id]||(fields[id]={setAttribute(){}})}};
vm.createContext(context);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),context);const start=source.indexOf('const THEMES =');vm.runInContext(source.slice(start,source.indexOf('// Sync button label/icon',start)),context);
context.applyTheme('night');assert.deepEqual(finished,['background-color','color','border-left-color','box-shadow']);assert.equal(theme,'night');
''')

    def test_pending_auto_config_preserves_edits_and_ignores_older_requests(self):
        self.run_js(r'''const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
const fields={},toasts=[],requests=[];const el=id=>id==='btn-auto-configure'?button:(fields[id]??={value:'initial',checked:false,style:{},innerHTML:'',scrollIntoView(){}});
const icon={tag:'i',attrs:{},className:'fas fa-magic me-1'};const label={tag:'span',attrs:{'data-i18n':'setup.header.auto-configure'},textContent:'Auto-Configure',setAttribute(k,v){this.attrs[k]=String(v);},getAttribute(k){return Object.hasOwn(this.attrs,k)?this.attrs[k]:null;},hasAttribute(k){return Object.hasOwn(this.attrs,k);},removeAttribute(k){delete this.attrs[k];}};
const button={disabled:false,children:[icon,label],querySelector(selector){return this.children.find(child=>selector==='i'?child.tag==='i':child.attrs['data-i18n']!==undefined)||null;}};
const c={document:{getElementById:el},API:{get:()=>new Promise((resolve,reject)=>requests.push({resolve,reject}))},escapeHtml:String,showToast:(...args)=>toasts.push(args),showActionError:(...args)=>toasts.push(args),toggleTTSMode(){},toggleSubBatchFields(){}};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);const a=s.indexOf('async function autoConfigureSettings()');vm.runInContext(s.slice(a,s.indexOf('// Local/Remote LLM profile state.',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{
for(const [id,property] of [['tts-mode','value'],['parallel-workers','value'],['compile-codec','checked'],['batch-group-by-type','checked'],['sub-batch-enabled','checked'],['sub-batch-min-size','value'],['sub-batch-ratio','value'],['sub-batch-max-items','value']]){
 const count=toasts.length;const waiting=c.autoConfigureSettings();
 const unchanged=()=>JSON.stringify(Object.fromEntries(Object.entries(fields).filter(([k])=>k!==id&&k!=='btn-auto-configure').map(([k,v])=>[k,{value:v.value,checked:v.checked}])));const before=unchanged();el(id)[property]=property==='value'?'changed':!el(id).checked;const value=el(id)[property];requests.at(-1).resolve({gpu:null});await waiting;
 assert.strictEqual(el(id)[property],value,id+' newer edit retained');assert.strictEqual(unchanged(),before,'no partial application');assert.strictEqual(toasts.length,count+1);assert(toasts.at(-1)[0].includes('changed'));assert.strictEqual(el('btn-auto-configure').disabled,false);
}
const first=c.autoConfigureSettings(),old=requests.at(-1);const second=c.autoConfigureSettings(),latest=requests.at(-1);old.resolve({gpu:null});await first;assert.strictEqual(el('btn-auto-configure').disabled,true,'older completion cannot enable current request');latest.resolve({gpu:{total_gb:16}});await second;assert.strictEqual(el('parallel-workers').value,4);assert.strictEqual(el('tts-mode').value,'local');assert.strictEqual(el('sub-batch-max-items').value,8);assert.strictEqual(el('btn-auto-configure').disabled,false);
const stale=c.autoConfigureSettings(),staleRequest=requests.at(-1);const current=c.autoConfigureSettings();const count=toasts.length;staleRequest.reject(Error('old error'));await stale;assert.strictEqual(toasts.length,count);requests.at(-1).reject(Error('current error'));await current;assert.strictEqual(toasts.length,count+1);assert.strictEqual(el('btn-auto-configure').disabled,false);finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});''')

    def test_auto_configure_keeps_its_i18n_span_and_follows_locale_switches(self):
        self.run_auto_config_js(AUTO_CONFIGURE_LOCALE_JS)

    def test_auto_configure_busy_toast_banner_and_failure_are_translated(self):
        self.run_auto_config_js(AUTO_CONFIGURE_ERRORS_JS)

    def test_provider_heading_names_hidden_transport_gpu_and_reasoning_controls(self):
        from html.parser import HTMLParser
        class Section(HTMLParser):
            def __init__(self):
                super().__init__(); self.summaries = []; self.summary = False
            def handle_starttag(self, tag, attrs):
                if tag == 'summary': self.summary = True; self.summaries.append('')
            def handle_endtag(self, tag):
                if tag == 'summary': self.summary = False
            def handle_data(self, data):
                if self.summary: self.summaries[-1] += data
        section = Section(); section.feed((SOURCE.parent.parent / 'index.html').read_text())
        heading = next(h for h in section.summaries if h.startswith('Provider request options'))
        for setting in ['transport', 'GPU sharing', 'reasoning']:
            self.assertIn(setting, heading)

    def test_banned_tokens_guidance_does_not_recommend_disabling_reasoning(self):
        from html.parser import HTMLParser
        class Field(HTMLParser):
            def __init__(self):
                super().__init__(); self.placeholder = None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get('id') == 'banned-tokens':
                    self.placeholder = attrs.get('placeholder', '')
        html = (SOURCE.parent.parent / 'index.html').read_text()
        field = Field(); field.feed(html)
        self.assertIsNotNone(field.placeholder)
        self.assertNotIn('<think>', field.placeholder)
        self.assertNotIn('<reasoning>', field.placeholder)
        start = html.index('for="banned-tokens"')
        guidance = html[start:html.index('<div class="mb-3 form-check', start)]
        self.assertNotIn('Use to disable thinking mode', guidance)
        self.assertIn('Do not ban', guidance)
        self.assertIn('Reasoning effort', guidance)
        self.assertIn('low', guidance)

    def test_confirmation_preserves_multiline_text_and_voice_statuses_are_live(self):
        from html.parser import HTMLParser
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__(); self.by_id = {}
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs: self.by_id[attrs['id']] = attrs
        elements = Elements()
        elements.feed((SOURCE.parent.parent / 'index.html').read_text())
        with self.subTest(confirmation='multiline'):
            self.assertIn('white-space: pre-line', elements.by_id['confirmModalBody'].get('style', ''))
        for key in ['suggest-status', 'persona-status', 'cast-status', 'narrator-preview-status', 'voice-save-status']:
            with self.subTest(status=key):
                self.assertEqual(elements.by_id[key].get('role'), 'status')
                self.assertEqual(elements.by_id[key].get('aria-live'), 'polite')
                self.assertEqual(elements.by_id[key].get('aria-atomic'), 'true')

    def test_prompt_preset_controls_disclose_setup_scope(self):
        from html.parser import HTMLParser
        class Buttons(HTMLParser):
            def __init__(self):
                super().__init__(); self.actions = {}; self.action = None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'button':
                    self.action = attrs.get('onclick') if 'data-config-save-action' in attrs else None
                    if self.action: self.actions[self.action] = ''
            def handle_data(self, data):
                if self.action: self.actions[self.action] += data
            def handle_endtag(self, tag):
                if tag == 'button': self.action = None
        html = (SOURCE.parent.parent / 'index.html').read_text()
        buttons = Buttons(); buttons.feed(html)
        for action in ['deletePromptPreset()', "deletePassPromptPreset('pass1')", "deletePassPromptPreset('pass3')"]:
            self.assertEqual(buttons.actions[action], 'Delete preset and save Setup')
        self.assertIn('<strong>Save preset and Setup</strong> gives it a name and also saves all other edited Setup settings', html)

    def test_selected_llm_profile_requires_save_and_validation_keeps_active_mode(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
const elements={};const el=id=>elements[id]||(elements[id]={attributes:{},setAttribute(name,value){this.attributes[name]=String(value);},getAttribute(name){return this.attributes[name] ?? null;},hasAttribute(name){return name in this.attributes;},value:'',style:{},textContent:'',innerHTML:''});let fail=false;
const c={currentLlmMode:'local',savedLlmMode:'local',document:{getElementById:el},syncCurrentLlmProfile:()=>{if(fail){throw Error('invalid');}},populateLlmInputs(){},showToast(){},showConfigValidationError(){}};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);const a=s.indexOf('function renderActiveLlmModeBadge()');vm.runInContext(s.slice(a,s.indexOf('async function testLlmConnection()',a)),c);
c.renderActiveLlmModeBadge();assert.strictEqual(el('llm-active-mode-badge').textContent,'Active: Local');el('llm-mode').value='remote';c.onLlmModeChange();assert.match(el('llm-active-mode-badge').textContent,/Active: Local.*Selected: Remote.*Save to apply/);assert.strictEqual(c.savedLlmMode,'local');c.savedLlmMode='remote';c.renderActiveLlmModeBadge();assert.strictEqual(el('llm-active-mode-badge').textContent,'Active: Remote (Thunder / network)');fail=true;el('llm-mode').value='local';c.onLlmModeChange();assert.strictEqual(c.currentLlmMode,'remote');assert.strictEqual(el('llm-mode').value,'remote');'''
        self.run_js(script)

    def test_connection_check_does_not_claim_to_apply_or_save_settings(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
const fields={};const el=id=>fields[id]||(fields[id]={value:'',style:{},textContent:'',innerHTML:'',removeAttribute(name){delete this[name];},scrollIntoView(){}});el('llm-url').value='https://example.invalid/v1';let requests=[];
const profile={base_url:el('llm-url').value,model_name:'<model>'};
const t=(key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english));
const c={currentLlmMode:'remote',t,document:{getElementById:el},getEditedLlmProfile:()=>({...profile}),escapeHtml:t=>String(t).replaceAll('<','&lt;').replaceAll('>','&gt;'),API:{post:async(path,body)=>{requests.push({path,body});return{ok:true,is_remote:true,base_url:profile.base_url,model:profile.model_name,reply:'ok'};}}};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);c.URL=URL;vm.runInContext(s.slice(s.indexOf('function setFieldFeedback('),s.indexOf('function showConfirm(')),c);const a=s.indexOf('async function testLlmConnection()');vm.runInContext(s.slice(a,s.indexOf('const generationControlFields =',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{await c.testLlmConnection();assert.strictEqual(requests.length,1);assert.strictEqual(requests[0].path,'/api/llm/test');assert.deepStrictEqual(requests[0].body,profile);assert.match(el('auto-config-msg').innerHTML,/Connection check:/);assert.match(el('auto-config-msg').innerHTML,/does not save or apply settings/);assert.match(el('auto-config-msg').innerHTML,/Save Configuration/);assert(!el('auto-config-msg').innerHTML.includes('Auto-configured'));assert(el('auto-config-msg').innerHTML.includes('&lt;model&gt;'));assert.strictEqual(el('llm-url').value,profile.base_url);assert.match(el('llm-test-result').innerHTML,/llm-test-pill is-ok/);assert.match(el('llm-test-result').innerHTML,/&lt;model&gt;/);assert.strictEqual(el('llm-test-result').innerHTML.split('llm-test-pill').length-1,4);assert.match(el('llm-test-result').innerHTML,/ms round trip/);assert.strictEqual(el('llm-test-btn').disabled,false);finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'''
        self.run_js(script)

    def test_reload_cancel_is_noop_and_confirmation_reloads_once(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');let reloads=0,choice=false;
const c={location:{reload:()=>reloads++},showConfirm:async text=>{assert.match(text,/Unsaved changes may be lost/);return choice;}};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);c.t=c.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english))),c.formatUiSafetyText=c.formatUiSafetyText||((key,english,values=Object())=>c.t(key,english,values)),c.tSafety=c.tSafety||((key,english,values=Object())=>Object.fromEntries([['primary',c.t(key,english,values)],['secondary',null]]));const a=s.indexOf('async function reloadPageAfterConfirmation()');assert(a>=0);vm.runInContext(s.slice(a,s.indexOf('async function confirmIfRemote(',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished,'reload assertions must finish'));(async()=>{await c.reloadPageAfterConfirmation();assert.strictEqual(reloads,0);choice=true;await c.reloadPageAfterConfirmation();assert.strictEqual(reloads,1);finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'''
        self.run_js(script)

    def test_result_empty_state_hides_after_success_but_not_failed_or_cancelled_merge(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
const fields={};let callbacks;const el=id=>fields[id]||(fields[id]={style:{display:'none'},textContent:'',setAttribute(){}});
const c={document:{getElementById:el},Date,fetch:async()=>({ok:true,status:200}),API:{},createTaskLogRenderer:()=>()=>{},notifyJobDone(){},_startPolling:(_key,_fetch,options)=>callbacks=options};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);
const a=s.indexOf('function isTaskFailed(');vm.runInContext(s.slice(a,s.indexOf('// --- Desktop notifications',a)),c);const b=s.indexOf('function getTaskCompletionOutcome(');vm.runInContext(s.slice(b,s.indexOf('function notifyJobDone(',b)),c);
vm.runInContext(s.slice(s.indexOf('let finalAudioRequest =')),c);
(async()=>{for(const status of ['failed','cancelled','done']){el('audio-player-container').style.display='none';el('audio-empty-state').style.display='';await c.pollLogs('audio','logs');callbacks.onDone({status,logs:status==='done'?['Task audio completed successfully.']:['merge incomplete; review logs']});await new Promise(resolve=>setImmediate(resolve));assert.strictEqual(el('audio-player-container').style.display,status==='done'?'block':'none');assert.strictEqual(el('audio-empty-state').style.display,status==='done'?'none':'');}})().catch(e=>{console.error(e);process.exitCode=1;});'''
        self.run_js(script)

    def test_auto_config_external_requires_server_and_save_without_changing_tiers(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
const fields={};const icon={tag:'i',attrs:{},className:'fas fa-magic me-1'};const label={tag:'span',attrs:{'data-i18n':'setup.header.auto-configure'},textContent:'Auto-Configure',setAttribute(k,v){this.attrs[k]=String(v);},getAttribute(k){return Object.hasOwn(this.attrs,k)?this.attrs[k]:null;},hasAttribute(k){return Object.hasOwn(this.attrs,k);},removeAttribute(k){delete this.attrs[k];}};
const button={disabled:false,children:[icon,label],querySelector(selector){return this.children.find(child=>selector==='i'?child.tag==='i':child.attrs['data-i18n']!==undefined)||null;}};
const el=id=>id==='btn-auto-configure'?button:(fields[id]||(fields[id]={style:{},innerHTML:'',scrollIntoView(){}}));let stats={},applied;
const c={document:{getElementById:el},API:{get:async()=>stats},escapeHtml:t=>String(t),showToast(){}};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);const a=s.indexOf('async function autoConfigureSettings()');vm.runInContext(s.slice(a,s.indexOf('function _applyAutoSettings(',a)),c);c._applyAutoSettings=settings=>applied=settings;
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{for(const input of [{},{gpu:{total_gb:24},gpu_mismatch:true},{gpu:{total_gb:4}}]){stats=input;await c.autoConfigureSettings();assert.strictEqual(applied.ttsMode,'external');assert.strictEqual(applied.parallelWorkers,1);assert.match(el('auto-config-msg').innerHTML,/start a compatible TTS server and enter its URL/);assert.match(el('auto-config-msg').innerHTML,/Save to apply/);assert.strictEqual(el('btn-auto-configure').disabled,false);}stats={gpu:{total_gb:16}};await c.autoConfigureSettings();assert.strictEqual(applied.ttsMode,'local');assert.strictEqual(applied.subBatchMaxItems,8);assert(!el('auto-config-msg').innerHTML.includes('start a compatible TTS server'));finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'''
        self.run_js(script)

    def test_trait_estimates_remain_qualified_and_escape_model_text(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
const c={escapeHtml:t=>String(t).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);c.t=c.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english))),c.formatUiSafetyText=c.formatUiSafetyText||((key,english,values=Object())=>c.t(key,english,values)),c.tSafety=c.tSafety||((key,english,values=Object())=>Object.fromEntries([['primary',c.t(key,english,values)],['secondary',null]]));const a=s.indexOf('function getTraitBadgeHtml(');vm.runInContext(s.slice(a,s.indexOf('function createVoiceCard(',a)),c);
assert.strictEqual(c.getTraitBadgeHtml(null),'');assert.strictEqual(c.getTraitBadgeHtml({gender:'unknown',age_group:'unknown'}),'');const html=c.getTraitBadgeHtml({states:[{gender:'female',age_group:'young_adult'},{gender:'<script>',age_group:'elderly'}],ageless:true,lines:12});assert.match(html,/Script estimate: female · young adult → &lt;script&gt; · elderly · ageless/);assert.match(html,/Model-inferred from 12 script lines; review against the source/);assert(!html.includes('<script>'));'''
        self.run_js(script)

    def test_generating_row_is_busy_and_becomes_actionable_when_complete(self):
        script=r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');let child={tag:'button'};const container={querySelector:selector=>selector==='button'?(child.tag==='button'?child:null):selector==='.tp'?(child.tag==='div'?child:null):null,replaceChild:node=>child=node};const tr={querySelector:selector=>selector==='.chunk-actions'?container:null};const c={escapeHtml:String,t:(key,english)=>english,document:{querySelector:()=>tr,createElement:tag=>({tag,style:{}})},applyDriftFilter(){},generateChunk(){}};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);const a=s.indexOf('function updateChunkRow(');vm.runInContext(s.slice(a,s.indexOf('function ensureChunkRefresh(',a)),c);
assert(c.updateChunkRow({id:1,status:'generating'}));assert.strictEqual(child.tag,'div');assert(child.innerHTML.includes('role="status"'));assert(child.innerHTML.includes('aria-label="Generating audio"'));assert(child.innerHTML.includes('Generating…'));assert(!child.innerHTML.includes('width: 100%'));assert(!child.innerHTML.includes('role="progressbar"'));
c.updateChunkRow({id:1,status:'done'});assert.strictEqual(child.tag,'button');assert(child.innerHTML.includes('Gen'));
'''
        self.run_js(script)

    def test_upload_failure_preserves_list_and_old_response_cannot_overwrite_recovery(self):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const fields={};const el=id=>fields[id]||(fields[id]={innerHTML:''});let reply,rendered=0;
const c={document:{getElementById:el},API:{get:async()=>{if(reply instanceof Error){throw reply;}return reply;}},console:{debug(){}},escapeHtml:t=>String(t).replaceAll('<','&lt;').replaceAll('"','&quot;'),renderScriptBatchUploads:()=>rendered++,scriptBatchUploads:[]};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);const a=s.indexOf('let existingUploadsLoadRequest =');vm.runInContext(s.slice(a,s.indexOf('window.selectExistingScriptUpload =',a)),c);
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{reply=[{filename:'old <file>.txt',size:10}];await c.loadExistingScriptUploads();assert.strictEqual(rendered,1);const before=el('existing-upload-select').innerHTML;reply=Error('<internal>');await c.loadExistingScriptUploads();assert.strictEqual(el('existing-upload-select').innerHTML,before);assert.strictEqual(c.scriptBatchUploads[0].filename,'old <file>.txt');assert(el('existing-uploads-status').innerHTML.includes('Retry uploads'));assert(!el('existing-uploads-status').innerHTML.includes('<internal>'));
let resolve,reject;c.API.get=()=>new Promise((done,fail)=>{resolve=done;reject=fail;});const old=c.loadExistingScriptUploads();c.API.get=async()=>[{filename:'new.txt',size:20}];await c.loadExistingScriptUploads();reject(Error('stale failure'));await old;assert.strictEqual(el('existing-uploads-status').innerHTML,'');assert(el('existing-upload-select').innerHTML.includes('new.txt'));
c.API.get=()=>new Promise(done=>resolve=done);const slow=c.loadExistingScriptUploads();c.API.get=async()=>[{filename:'newest.txt',size:20}];await c.loadExistingScriptUploads();resolve([{filename:'stale.txt',size:1}]);await slow;assert.strictEqual(c.scriptBatchUploads[0].filename,'newest.txt');assert(!el('existing-upload-select').innerHTML.includes('stale.txt'));finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self.run_js(script)

    def test_key_visibility_preserves_value_and_theme_names_current_and_next(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
const fields={};const el=id=>fields[id]||(fields[id]={type:'password',value:'env:PRIVATE_KEY',textContent:'',setAttribute(k,v){this[k]=v;}});let theme=null;const writes=[];
const c={document:{getElementById:el,documentElement:{removeAttribute(){theme=null;},setAttribute(k,v){theme=v;},getAttribute(){return theme;}}},localStorage:{setItem:(k,v)=>writes.push([k,v])}};vm.createContext(c);vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-i18n.js'),'utf8'),c);
let a=s.indexOf('function toggleLlmKeyVisibility()');vm.runInContext(s.slice(a,s.indexOf('// Sync button label/icon',a)),c);
const original=el('llm-key').value;c.toggleLlmKeyVisibility();assert.strictEqual(el('llm-key').type,'text');assert.strictEqual(el('llm-key-toggle').textContent,'Hide key');assert.strictEqual(el('llm-key-toggle')['aria-pressed'],'true');assert.strictEqual(el('llm-key').value,original);
c.toggleLlmKeyVisibility();assert.strictEqual(el('llm-key').type,'password');assert.strictEqual(el('llm-key-toggle').textContent,'Show key');assert.strictEqual(el('llm-key-toggle')['aria-pressed'],'false');assert.strictEqual(el('llm-key').value,original);assert.strictEqual(writes.length,0);
for(const [current,next] of [['Light','Night'],['Night','Super Night'],['Super Night','Cyberpunk'],['Cyberpunk','Bread'],['Bread','Leek'],['Leek','Light']]){if(current==='Light'){c.applyTheme('light');}else{c.cycleTheme();}assert.strictEqual(el('theme-label').textContent,'Theme: '+current);assert.strictEqual(el('theme-toggle')['aria-label'],`Current theme: ${current}. Switch to ${next}.`);assert.strictEqual(el('theme-toggle').title,el('theme-toggle')['aria-label']);}
c.cycleTheme();assert.strictEqual(theme,null);assert.strictEqual(writes.at(-1)[1],'light');
'''
        self.run_js(script)

    def run_js(self,script):
        result=subprocess.run(['node','-e',script,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def run_auto_config_js(self,body,source=None):
        script=DOM_FIXTURE+AUTO_CONFIGURE_FIXTURE+body
        result=subprocess.run(['node','-e',script,str(source or SOURCE)],capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
