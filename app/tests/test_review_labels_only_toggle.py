"""The labels-only review switch exists, round-trips, and can be reset (#2045).

WHAT THIS VERIFIES.
- The switch is markup in the review-prompt group ("Delivery notes and review
  prompts"), has a label and a help block, and both keys are translated in all
  four catalogs (not just present in English).
- The real `loadConfig()` sets the switch from `config.prompts.review_labels_only`
  (true, false, and a legacy config with no key), and the real
  `buildConfigPayload()` sends `prompts.review_labels_only` both ways.
- `POST /api/config` then `GET /api/config` really keep the value: the server
  round-trip, not just the payload shape.
- `POST /api/config/preview` names the change with a readable label.
"""
import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import system
from tests import test_prompt_preset_transactions_js as fixtures

STATIC = Path(__file__).resolve().parent.parent / 'static'
SOURCE = STATIC / 'js' / 'app-core.js'
I18N = STATIC / 'i18n'
LOCALES = ('en', 'zh-CN', 'ja', 'es-419')
LABEL_KEY = 'setup.prompts.review-labels-only.label'
HELP_KEY = 'setup.prompts.review-labels-only.help'
FIELD_HELP_KEY = 'help.field.review-labels-only.what'
POINTER_KEY = 'script.review.labels-only.pointer'
POINTER_ON_KEY = 'script.review.labels-only.state.on'
POINTER_OFF_KEY = 'script.review.labels-only.state.off'
POINTER_HELP_KEY = 'help.action.reviewlabelsonlypointer'
PROFILE = {'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'k', 'model_name': 'm'}


class SwitchInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs, self.labels, self.help_blocks = [], [], []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'input' and attrs.get('id') == 'review-labels-only':
            self.inputs.append(attrs)
        if tag == 'label' and attrs.get('for') == 'review-labels-only':
            self.labels.append(attrs)
        if tag == 'div' and attrs.get('id') == 'review-labels-only-help':
            self.help_blocks.append(attrs)


class PointerInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons, self.pointers = [], []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == 'button' and attrs.get('id') == 'btn-review-labels-only':
            self.buttons.append(attrs)
        if attrs.get('id') == 'review-labels-only-pointer':
            self.pointers.append((tag, attrs))


class LabelsOnlyPointerMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / 'index.html').read_text(encoding='utf-8')
        cls.parser = PointerInventory()
        cls.parser.feed(cls.html)

    def test_the_pointer_is_one_script_tab_button_next_to_the_review_toggles(self):
        self.assertEqual(1, len(self.parser.buttons), 'exactly one labels-only pointer')
        self.assertEqual(1, len(self.parser.pointers), 'one dynamic text node')
        button = self.parser.buttons[0]
        self.assertEqual('button', button['type'])
        self.assertIn('btn-link', button.get('class', '').split())
        self.assertEqual('openSetupSetting(\'review-labels-only\')', button.get('onclick'))
        self.assertEqual('reviewLabelsOnlyPointer', button.get('data-help-key'))
        tag, pointer = self.parser.pointers[0]
        self.assertEqual('span', tag)
        self.assertIn('data-i18n-skip', pointer, 'JS paints the value, so the catalog is not a second source')
        script_tab = self.html.index('id="script-tab"')
        force_rerun = self.html.index('id="review-force-rerun"')
        toggle = self.html.index('id="btn-review-labels-only"')
        review_start = self.html.index('id="btn-review-script"')
        self.assertLess(script_tab, force_rerun)
        self.assertLess(force_rerun, toggle)
        self.assertLess(toggle, review_start, 'the pointer sits with the other review toggles')

    def test_the_pointer_and_its_help_are_in_every_catalog(self):
        english = json.loads((I18N / 'ui.en.json').read_text(encoding='utf-8'))
        self.assertEqual('Labels-only review: {state} — change in Setup → Prompt studio', english[POINTER_KEY])
        self.assertEqual('on', english[POINTER_ON_KEY])
        self.assertEqual('off', english[POINTER_OFF_KEY])
        self.assertIn('Setup', english[POINTER_HELP_KEY])
        for locale in LOCALES:
            catalog = json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))
            with self.subTest(locale=locale):
                for key in (POINTER_KEY, POINTER_ON_KEY, POINTER_OFF_KEY, POINTER_HELP_KEY):
                    self.assertIn(key, catalog)
                    self.assertTrue(catalog[key].strip())
                self.assertIn('{state}', catalog[POINTER_KEY])
                if locale != 'en':
                    self.assertNotEqual(english[POINTER_KEY], catalog[POINTER_KEY],
                                        f'{locale} must translate the pointer')


class LabelsOnlyHelpAccuracyTests(unittest.TestCase):
    """Labels-only mode replaces both custom review prompts (review_script.py:1594)."""

    def test_the_switch_help_and_tooltip_say_custom_review_prompts_are_ignored(self):
        english = json.loads((I18N / 'ui.en.json').read_text(encoding='utf-8'))
        self.assertIn('Review System Prompt', english[HELP_KEY])
        self.assertIn('Review User Prompt Template', english[HELP_KEY])
        self.assertIn('ignored', english[HELP_KEY])
        self.assertIn('ignored', english[FIELD_HELP_KEY])
        self.assertLessEqual(len(english[FIELD_HELP_KEY]), 300, 'the tooltip has a length cap')
        for locale in LOCALES[1:]:
            catalog = json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))
            with self.subTest(locale=locale):
                self.assertNotEqual(english[HELP_KEY], catalog[HELP_KEY])
                self.assertNotEqual(english[FIELD_HELP_KEY], catalog[FIELD_HELP_KEY])
                self.assertGreater(len(catalog[HELP_KEY]), 60, 'the whole switch help is translated')

    def test_the_reviewer_really_replaces_both_custom_prompts_in_labels_only_mode(self):
        source = (Path(__file__).resolve().parent.parent / 'review_script.py').read_text(encoding='utf-8')
        block = source[source.index('if params.review_labels_only:', source.index('usr_template = params.user_prompt_template')):]
        self.assertIn('sys_prompt = REVIEW_LABEL_SYSTEM', block[:400])
        self.assertIn('user_prompt = context', block[:400])


class LabelsOnlyMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / 'index.html').read_text(encoding='utf-8')
        cls.parser = SwitchInventory()
        cls.parser.feed(cls.html)

    def test_the_switch_is_one_bootstrap_form_switch_with_label_and_help(self):
        self.assertEqual(1, len(self.parser.inputs), 'exactly one labels-only switch')
        switch = self.parser.inputs[0]
        self.assertIn('form-check-input', switch.get('class', '').split())
        self.assertEqual('checkbox', switch.get('type'))
        self.assertEqual('switch', switch.get('role'))
        self.assertEqual(1, len(self.parser.labels))
        self.assertEqual(LABEL_KEY, self.parser.labels[0].get('data-i18n'))
        self.assertEqual(1, len(self.parser.help_blocks))
        self.assertEqual(HELP_KEY, self.parser.help_blocks[0].get('data-i18n'))
        self.assertEqual(self.parser.help_blocks[0]['id'], switch.get('aria-describedby'))

    def test_the_switch_sits_in_the_review_prompt_group_and_stays_expert_only(self):
        start = self.html.index('data-i18n="setup.prompts.summary.delivery-notes-and-revie"')
        toggle = self.html.index('id="review-labels-only"')
        review_prompt = self.html.index('id="review-system-prompt"')
        persona_group = self.html.index('data-i18n="setup.prompts.summary.persona-and-voice-design"')
        self.assertLess(start, toggle)
        self.assertLess(toggle, review_prompt)
        self.assertLess(review_prompt, persona_group)
        # The whole group lives inside the expert-only Prompt studio section.
        section = self.html.rindex('id="settings-prompts"', 0, start)
        self.assertIn('data-setup-advanced-section hidden', self.html[section:self.html.index('>', section)])

    def test_all_four_catalogs_carry_both_keys_in_the_page_language(self):
        english = json.loads((I18N / 'ui.en.json').read_text(encoding='utf-8'))
        self.assertEqual('Labels-only review', english[LABEL_KEY])
        self.assertIn('never rewrites the script text', english[HELP_KEY])
        self.assertIn('review again', english[HELP_KEY])
        self.assertIn('own label prompt', english[HELP_KEY])
        self.assertIn('labels', english[FIELD_HELP_KEY].lower())
        self.assertIn('never rewritten', english[FIELD_HELP_KEY])
        for locale in LOCALES:
            catalog = json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))
            with self.subTest(locale=locale):
                for key in (LABEL_KEY, HELP_KEY, FIELD_HELP_KEY):
                    self.assertIn(key, catalog)
                    self.assertTrue(catalog[key].strip())
                if locale != 'en':
                    self.assertNotEqual(english[LABEL_KEY], catalog[LABEL_KEY],
                                        f'{locale} must translate the switch label')


LOAD_HARNESS = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const elements={};
const element=id=>elements[id]??={value:'',checked:false,textContent:'',style:{},elements:[],max:0};
let config=null;
const context={window:{},document:{getElementById:element},API:{get:async()=>config},console,
 showConfirm:async()=>true,escapeHtml:value=>String(value),
 renderConfigWarnings(){},applyPauseSupport(){},toggleSubBatchFields(){},toggleTTSMode(){},
 renderActiveLlmModeBadge(){},populateLlmInputs(){},onLlmModeChange(){},
 renderPromptPresets(){},renderPassPromptPresets(){},applyGenerationSettings(){},
 applyCurrentBookFilename(){},
 loadMissingPromptDefaults(){throw Error('the fixture supplies every prompt');},
 llmProfiles:{},currentLlmMode:'local',savedLlmMode:'local',currentIsRemote:false,
 failoverIsRemote:false,legacyChunkSize:0,maxPauseMs:0,activePromptPreset:'michel2_full',
 currentBookFilename:'',
 passPromptDefaults:{pass1:{system_prompt:'s',user_prompt:'u'},pass3:{system_prompt:'s',user_prompt:'u'},
  cast:{whole_book_prompt:'w',incremental_prompt:'i'}}};
context.t=(key,english,values=Object())=>Object.keys(values).reduce(
 (text,name)=>text.split('{'+name+'}').join(String(values[name])),String(english));
vm.createContext(context);
const start=source.indexOf('async function loadConfig() {');
const end=source.indexOf('// Reset prompts and generation settings',start);
assert(start>=0&&end>start,'loadConfig boundaries must exist in source order');
vm.runInContext(source.slice(start,end),context);
const pointerStart=source.indexOf('function renderReviewLabelsOnlyPointer(');
const pointerEnd=source.indexOf('// The whole payload in one place',pointerStart);
assert(pointerStart>=0&&pointerEnd>pointerStart,'renderer boundaries must exist in source order');
vm.runInContext(source.slice(pointerStart,pointerEnd),context);
function fixture(reviewLabelsOnly){
 const prompts={review_system_prompt:'review S',review_user_prompt:'review U',
  persona_system_prompt:'persona S',persona_user_prompt:'persona U',persona_advanced_prompt:'advanced'};
 if(reviewLabelsOnly!==undefined){prompts.review_labels_only=reviewLabelsOnly;}
 return {llm:{base_url:'http://localhost:1/v1',model_name:'fixture'},llm_mode:'local',llm_failover:false,
  llm_local:{base_url:'http://localhost:1/v1',model_name:'fixture'},
  tts:{mode:'local'},prompts,prompt_presets:[]};
}
(async()=>{
 for(const [value,expected] of [[true,true],[false,false]]){
  element('review-labels-only').checked=!expected;config=fixture(value);await context.loadConfig();
  assert.strictEqual(element('review-labels-only').checked,expected,'config '+value+' must set the switch');
  assert.strictEqual(element('review-labels-only-pointer').textContent,
   'Labels-only review: '+(expected?'on':'off')+' — change in Setup → Prompt studio',
   'the Script-tab pointer must follow the loaded value');
 }
 element('review-labels-only').checked=true;config=fixture(undefined);await context.loadConfig();
 assert.strictEqual(element('review-labels-only').checked,false,'a config without the key means off');
 assert(element('review-labels-only-pointer').textContent.includes('off'),
  'a config without the key leaves the pointer saying off');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class LabelsOnlyRoundTripTests(unittest.TestCase):
    def test_real_load_config_sets_the_switch_both_ways(self):
        result = subprocess.run(['node', '-e', LOAD_HARNESS, str(SOURCE)],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_real_save_payload_carries_the_switch_both_ways(self):
        fixtures.PromptPresetTransactionTests().run_case(r'''
element('review-labels-only').checked=true;
assert.strictEqual(context.buildConfigPayload(2).prompts.review_labels_only,true,'on must save true');
element('review-labels-only').checked=false;
assert.strictEqual(context.buildConfigPayload(2).prompts.review_labels_only,false,'off must save false');
''')

    def test_a_save_updates_the_script_tab_pointer(self):
        fixtures.PromptPresetTransactionTests().run_case(r'''
mode='success';
assert.strictEqual(element('review-labels-only-pointer').textContent,'Labels-only review: off — change in Setup → Prompt studio');
element('review-labels-only').checked=true;
await context.saveConfigPayload(context.buildConfigPayload(2));
assert.strictEqual(posts.at(-1).prompts.review_labels_only,true);
assert.strictEqual(element('review-labels-only-pointer').textContent,'Labels-only review: on — change in Setup → Prompt studio','the pointer follows the saved value');
element('review-labels-only').checked=false;
await context.saveConfigPayload(context.buildConfigPayload(2));
assert.strictEqual(posts.at(-1).prompts.review_labels_only,false);
assert.strictEqual(element('review-labels-only-pointer').textContent,'Labels-only review: off — change in Setup → Prompt studio');
''')

    def test_reset_to_defaults_returns_the_switch_to_the_server_default(self):
        app = FastAPI(); app.include_router(system.router)
        with TestClient(app) as client:
            response = client.get('/api/default_prompts')
            self.assertEqual(200, response.status_code, response.text)
            defaults = response.json()
        self.assertIs(False, defaults.get('review_labels_only'), 'the server default is off')
        fixtures.PromptPresetTransactionTests().run_case(
            "const defaults=" + json.dumps(defaults) + ';' + r'''
const helper=source.indexOf('const generationControlFields =');
assert(helper>=0);run(source.slice(helper,source.indexOf('async function loadConfig()',helper)));
const resetStart=source.indexOf('window.resetPrompts =');
const resetEnd=source.indexOf('function buildConfigPayload(',resetStart);
assert(resetStart>=0&&resetEnd>resetStart);
run(source.slice(resetStart,resetEnd));
run(source.slice(source.indexOf('function renderReviewLabelsOnlyPointer('),source.indexOf('// The whole payload in one place')));
context.API.get=async()=>defaults;
element('review-labels-only').checked=true;
await context.window.resetPrompts();
assert.strictEqual(element('review-labels-only').checked,false,'restore defaults must switch it off');
assert.strictEqual(toasts.length,1,'the reset must finish without a swallowed error');
assert(element('review-labels-only-pointer').textContent.includes('off'),'the pointer follows the reset');
assert.strictEqual(toasts[0][1],'success');
''')


POINTER_SETUP = r'''
Element.prototype.replaceChildren=function(){for(const child of [...this.children]){child.remove();}};
Element.prototype.focus=function(){document.activeElement=this;};
Element.prototype.scrollIntoView=function(){this.scrolled=true;};
context.getComputedStyle=node=>({display:node.style.display||'block'});
const form=body.appendChild(new Element('form',{id:'config-form'}));
const category=body.appendChild(new Element('select',{id:'setup-settings-category'}));
const search=body.appendChild(new Element('input',{id:'setup-settings-search'}));search.value='';
body.appendChild(new Element('button',{id:'setup-settings-clear'}));
const results=body.appendChild(new Element('div',{id:'setup-settings-results',hidden:''}));
body.appendChild(new Element('div',{id:'setup-settings-search-status'}));
const expert=body.appendChild(new Element('input',{id:'setup-expert-toggle',type:'checkbox'}));
const connection=form.appendChild(new Element('section',{'data-settings-section':'Connection',id:'settings-llm'}));
connection.appendChild(new Element('h5'));
const prompts=form.appendChild(new Element('section',{'data-settings-section':'Prompt studio',id:'settings-prompts','data-setup-advanced-section':'',hidden:''}));
prompts.appendChild(new Element('h5'));
const details=prompts.appendChild(new Element('details'));details.open=false;
const reviewPrompt=details.appendChild(new Element('textarea',{id:'review-system-prompt'}));
reviewPrompt.value='custom review prompt';
const label=details.appendChild(new Element('label',{for:'review-labels-only'}));
const toggle=details.appendChild(new Element('input',{id:'review-labels-only',type:'checkbox'}));
toggle.labels=[label];toggle.checked=false;
const stored=new Map();context.localStorage={getItem:key=>stored.has(key)?stored.get(key):null,setItem:(key,value)=>stored.set(key,value)};
let activated=[];context.activateTab=(name,updateHistory)=>{activated.push([name,updateHistory]);};
const routed=document.querySelectorAll.bind(document);
document.querySelectorAll=selector=>selector==='#config-form [data-settings-section]'
 ? form.querySelectorAll('[data-settings-section]') : routed(selector);
const core=fs.readFileSync(process.argv[2],'utf8');
const fieldStart=core.indexOf('function openConfigField('),fieldEnd=core.indexOf('function showConfigValidationError(',fieldStart);
assert(fieldStart>=0&&fieldEnd>fieldStart,'openConfigField boundaries must exist in source order');
vm.runInContext(core.slice(fieldStart,fieldEnd),context);
const workbench=fs.readFileSync(process.argv[3],'utf8');
const statusStart=workbench.indexOf('function applySystemStatusText(');
assert(statusStart>=0,'applySystemStatusText must exist in app-workbench.js');
vm.runInContext(workbench.slice(statusStart,workbench.indexOf('\n        }',statusStart)+10),context);
load();
'''


class LabelsOnlyPointerNavigationTests(unittest.TestCase):
    """The Script-tab pointer navigates through the one existing jump helper."""

    def run_js(self, script):
        from tests.test_ui_help_js import DOM_FIXTURE
        result = subprocess.run(['node', '-e', "eval(require('fs').readFileSync(0,'utf8'));",
                                 str(STATIC / 'js' / 'app-settings.js'), str(SOURCE),
                                 str(STATIC / 'js' / 'app-workbench.js')],
                                input=DOM_FIXTURE + POINTER_SETUP + script,
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_activating_the_pointer_reveals_and_focuses_the_original_switch(self):
        self.run_js(r'''
context.openSetupSetting('review-labels-only');
assert.deepStrictEqual(activated,[['setup',true]],'it must open the Setup tab');
assert(expert.checked,'expert settings must be revealed');
assert(!stored.has('alexandria.setupExpert'),'revealing must not save the reader preference');
assert.strictEqual(prompts.hidden,false,'the Prompt studio section must be shown');
assert.strictEqual(connection.hidden,true,'only the target section is shown');
assert.strictEqual(category.value,'settings-prompts');
assert.strictEqual(details.open,true,'the collapsed disclosure must be opened');
assert.strictEqual(document.activeElement,toggle,'focus must land on the switch');
assert.strictEqual(toggle.scrolled,true,'the switch must be scrolled into view');
assert.strictEqual(toggle.checked,false,'navigating must not change the setting');
assert.strictEqual(reviewPrompt.value,'custom review prompt','edits must be untouched');
assert.strictEqual(results.hidden,true);
''')

    def test_a_field_that_cannot_be_shown_is_not_revealed_or_focused(self):
        self.run_js(r'''
toggle.setAttribute('disabled','');
context.openSetupSetting('review-labels-only');
assert.strictEqual(document.activeElement,undefined,'a disabled switch must not take focus');
assert.strictEqual(details.open,false,'and its disclosure must stay closed');
context.openSetupSetting('not-a-control');
assert.strictEqual(details.open,false,'an unknown id must not open anything');
''')

    def test_every_tab_activation_comes_from_the_one_jump_helper(self):
        source = (STATIC / 'js' / 'app-settings.js').read_text(encoding='utf-8')
        self.assertEqual(1, source.count("activateTab('setup'"), 'one jump path, not a second copy')
        markup = (STATIC / 'index.html').read_text(encoding='utf-8')
        button = markup[markup.index('id="btn-review-labels-only"') - 260:
                        markup.index('id="btn-review-labels-only"') + 260]
        self.assertIn("onclick=\"openSetupSetting('review-labels-only')\"", button)
        self.assertNotIn('activateTab(', button, 'the pointer must not navigate by hand')


class LabelsOnlyServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'
        self.manager = Mock()
        for patcher in (patch.object(system, 'CONFIG_PATH', str(self.path)),
                        patch.object(system, 'project_manager', self.manager)):
            patcher.start(); self.addCleanup(patcher.stop)
        app = FastAPI(); app.include_router(system.router)
        self.client = TestClient(app); self.addCleanup(self.client.close)

    def payload(self, **prompts):
        return {'llm': dict(PROFILE), 'llm_local': dict(PROFILE), 'llm_mode': 'local',
                'tts': {'mode': 'local'}, 'prompts': prompts}

    def test_post_then_get_keeps_the_labels_only_value(self):
        for value in (True, False):
            with self.subTest(value=value):
                response = self.client.post('/api/config', json=self.payload(review_labels_only=value))
                self.assertEqual(200, response.status_code, response.text)
                loaded = self.client.get('/api/config')
                self.assertEqual(200, loaded.status_code, loaded.text)
                self.assertIs(value, loaded.json()['prompts']['review_labels_only'])
                self.assertIs(value, json.loads(self.path.read_text(encoding='utf-8'))['prompts']['review_labels_only'])

    def test_preview_names_the_switch_change(self):
        self.path.write_text(json.dumps(self.payload()), encoding='utf-8')
        response = self.client.post('/api/config/preview', json=self.payload(review_labels_only=True))
        self.assertEqual(200, response.status_code, response.text)
        row = next(row for row in response.json()['changes'] if row['path'] == 'prompts.review_labels_only')
        self.assertEqual('Labels-only review', row['label'])
        self.assertEqual(('Off', 'On'), (row['before'], row['after']))


if __name__ == '__main__':
    unittest.main()
