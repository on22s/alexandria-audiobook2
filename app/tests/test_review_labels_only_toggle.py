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
vm.createContext(context);
const start=source.indexOf('async function loadConfig() {');
const end=source.indexOf('// Reset prompts and generation settings',start);
assert(start>=0&&end>start,'loadConfig boundaries must exist in source order');
vm.runInContext(source.slice(start,end),context);
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
 }
 element('review-labels-only').checked=true;config=fixture(undefined);await context.loadConfig();
 assert.strictEqual(element('review-labels-only').checked,false,'a config without the key means off');
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
context.API.get=async()=>defaults;
element('review-labels-only').checked=true;
await context.window.resetPrompts();
assert.strictEqual(element('review-labels-only').checked,false,'restore defaults must switch it off');
assert.strictEqual(toasts.length,1,'the reset must finish without a swallowed error');
assert.strictEqual(toasts[0][1],'success');
''')


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
