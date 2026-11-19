"""Issue #2079 review: upgrading an old config must keep its custom review text.

These tests drive the REAL loadConfig() and loadMissingPromptDefaults() slices,
including the checked-in defaults fetched from /api/default_prompts. All prompt
text is synthetic.
"""
import json
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js'


HARNESS = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(process.argv[1],'app-core.js'),'utf8');
const input=JSON.parse(process.argv[2]);
const elements={},toasts=[],defaultCalls=[];
const element=id=>elements[id]??={id,value:'',checked:false,style:{},hidden:true,disabled:false,textContent:'',innerHTML:'',attributes:{},setAttribute(n,v){this.attributes[n]=String(v);},getAttribute(n){return this.attributes[n]??null;},removeAttribute(n){delete this.attributes[n];},replaceChildren(){},appendChild(){},classList:{},addEventListener(){},querySelectorAll(){return [];}};
const ctx={window:{},document:{getElementById:element,createElement:()=>({appendChild(){},childElementCount:1})},showToast:(...a)=>toasts.push(a),showConfirm:async()=>true,console:{warn(){},error(){}},
 API:{get:async url=>{if(url==='/api/default_prompts'){defaultCalls.push(url);return input.defaults;}if(url==='/api/config'){return input.config;}throw Error('unexpected GET '+url);},post:async()=>({status:'saved'})},
 llmProfiles:{local:{base_url:'http://localhost:1/v1',api_key:'local',model_name:'fixture'},remote:null},currentLlmMode:'local',legacyChunkSize:3000};
ctx.window={...ctx.window};vm.createContext(ctx);
ctx.t=ctx.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english)));
ctx.formatUiSafetyText=ctx.formatUiSafetyText||((key,english,values=Object())=>ctx.t(key,english,values));
ctx.tSafety=ctx.tSafety||((key,english,values=Object())=>Object.fromEntries([['primary',ctx.t(key,english,values)],['secondary',null]]));
const run=code=>vm.runInContext(code,ctx),read=expr=>vm.runInContext(expr,ctx);
run(fs.readFileSync(path.join(process.argv[1],'app-i18n.js'),'utf8'));
run(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')));
run(source.slice(source.indexOf('let promptPresets ='),source.indexOf('window.previewAttributionPrompt =')));
run(source.slice(source.indexOf('function renderReviewLabelsOnlyPointer('),source.indexOf('// The whole payload in one place')));
run(source.slice(source.indexOf('function getNumFieldValue('),source.indexOf('// --- Desktop notifications ---')));
run(source.slice(source.indexOf('function buildConfigPayload('),source.indexOf("document.getElementById('config-form').addEventListener")));
run(source.slice(source.indexOf('function getOnThisGpuInput('),source.indexOf('// Reflects the last-SAVED')));
run(source.slice(source.indexOf('let promptDefaultsLoadPending ='),source.indexOf('async function loadConfig()')));
run(source.slice(source.indexOf('async function loadConfig()'),source.indexOf('// Reset prompts and generation settings to factory defaults')));
for(const name of ['renderConfigWarnings','applyPauseSupport','renderActiveLlmModeBadge','onLlmModeChange','toggleSubBatchFields','toggleTTSMode','applyCurrentBookFilename','applyGenerationSettings','renderReviewLabelsOnlyPointer']){ctx[name]=()=>{};}
ctx.populateLlmInputs=()=>{element('llm-url').value=ctx.llmProfiles.local.base_url;element('llm-model').value=ctx.llmProfiles.local.model_name;element('llm-key').value=ctx.llmProfiles.local.api_key;};
element('config-form').elements=[];
'''


CUSTOM_SYS = 'Synthetic saved system text'
CUSTOM_USR = 'Synthetic saved user text {context} {batch}'
CHECKED_SYS = 'Synthetic checked-in system text'
CHECKED_USR = 'Synthetic checked-in user text {context} {batch}'


def make_config(review_system=CUSTOM_SYS, review_user=CUSTOM_USR, with_preset_keys=False):
    prompts = {'review_system_prompt': review_system, 'review_user_prompt': review_user}
    if with_preset_keys:
        prompts.update({'review_preset': 'default', 'review_prompt_presets': []})
    return {
        'llm_mode': 'local',
        'llm_local': {'base_url': 'http://localhost:1/v1', 'api_key': 'local', 'model_name': 'fixture'},
        'tts': {'mode': 'local'},
        'prompts': prompts,
        'prompt_presets': [],
    }


def make_defaults():
    result = {
        'review_system_prompt': CHECKED_SYS, 'review_user_prompt': CHECKED_USR,
        'persona_system_prompt': 'Synthetic persona system',
        'persona_user_prompt': 'Synthetic persona user {speaker}',
        'persona_advanced_prompt': 'Synthetic persona advanced',
        'pass1_system_prompt': 'Synthetic pass1 system',
        'pass1_user_prompt': 'Synthetic pass1 user',
        'pass3_system_prompt': 'Synthetic pass3 system',
        'pass3_user_prompt': 'Synthetic pass3 user',
        'cast_whole_book_prompt': 'Synthetic cast whole',
        'cast_incremental_prompt': 'Synthetic cast incremental',
        'generation': {},
    }
    for name in ('pass1_compact', 'pass3_gaps', 'pass3_directed'):
        result[f'{name}_system_prompt'] = f'Synthetic {name} system'
        result[f'{name}_user_prompt'] = f'Synthetic {name} user'
    return result


class ReviewPresetUpgradeJsTests(unittest.TestCase):
    def run_js(self, code, config=None, defaults=None):
        payload = json.dumps({
            'config': config or make_config(),
            'defaults': defaults or make_defaults(),
        })
        result = subprocess.run(['node', '-e', HARNESS + code, str(SOURCE), payload],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_custom_review_text_survives_load_and_default_restores_checked_in(self):
        self.run_js(r'''
(async()=>{
 await ctx.loadConfig();
 assert.strictEqual(element('review-system-prompt').value,'Synthetic saved system text');
 assert.strictEqual(element('review-user-prompt').value,'Synthetic saved user text {context} {batch}');
 assert.strictEqual(read('activePassPromptPreset.review'),'default');
 assert.strictEqual(read('passPromptDefaults.review.system_prompt'),'Synthetic checked-in system text','Default must be the checked-in text');
 const payload=ctx.buildConfigPayload(2);
 assert.strictEqual(payload.prompts.review_system_prompt,'Synthetic saved system text','Save keeps the custom text in the field review_script reads');
 assert.strictEqual(payload.prompts.review_user_prompt,'Synthetic saved user text {context} {batch}');
 assert.strictEqual(payload.prompts.review_preset,'default (edited)');
 assert.strictEqual(payload.prompts.review_prompt_presets.find(p=>p.name==='default (edited)').system_prompt,'Synthetic saved system text','custom text recorded as an edited preset, not lost');
 const select=element('review-prompt-preset-select');select.value='-1';await select.onchange();
 assert.strictEqual(element('review-system-prompt').value,'Synthetic checked-in system text','Default restores checked-in, not custom');
 assert.strictEqual(element('review-user-prompt').value,'Synthetic checked-in user text {context} {batch}');
 assert.strictEqual(read('activePassPromptPreset.review'),'default');
})();''')

    def test_absent_or_null_review_text_uses_checked_in_without_false_edited(self):
        for prompts in (
            {'review_system_prompt': None, 'review_user_prompt': None},
            {},
        ):
            with self.subTest(prompts=prompts):
                config = make_config()
                config['prompts'] = prompts
                code = r'''
(async()=>{
 await ctx.loadConfig();
 assert.strictEqual(element('review-system-prompt').value,'Synthetic checked-in system text');
 assert.strictEqual(element('review-user-prompt').value,'Synthetic checked-in user text {context} {batch}');
 assert.strictEqual(read('activePassPromptPreset.review'),'default');
 const payload=ctx.buildConfigPayload(2);
 assert.strictEqual(payload.prompts.review_preset,'default');
 assert.strictEqual(JSON.stringify(payload.prompts.review_prompt_presets),'[]','no false default (edited) preset');
 assert.strictEqual(payload.prompts.review_system_prompt,'Synthetic checked-in system text');
})();'''
                self.run_js(code, config=config)

    def test_custom_text_when_defaults_were_loaded_before_render(self):
        # A second Setup load (or a previous Retry) can leave the defaults loaded
        # before this loadConfig renders presets, so no defaults GET follows.
        code = r'''
(async()=>{
 await ctx.loadMissingPromptDefaults();
 assert.strictEqual(defaultCalls.length,1);
 assert.strictEqual(read('passPromptDefaults.review.system_prompt'),'Synthetic checked-in system text');
 await ctx.loadConfig();
 assert.strictEqual(defaultCalls.length,1,'defaults already loaded: loadConfig needs no second GET');
 assert.strictEqual(element('review-system-prompt').value,'Synthetic saved system text');
 assert.strictEqual(element('review-user-prompt').value,'Synthetic saved user text {context} {batch}');
 assert.strictEqual(read('passPromptDefaults.review.system_prompt'),'Synthetic checked-in system text','defaults must not be replaced by the custom text');
 const payload=ctx.buildConfigPayload(2);
 assert.strictEqual(payload.prompts.review_system_prompt,'Synthetic saved system text');
 assert.strictEqual(payload.prompts.review_user_prompt,'Synthetic saved user text {context} {batch}');
 const select=element('review-prompt-preset-select');select.value='-1';await select.onchange();
 assert.strictEqual(element('review-system-prompt').value,'Synthetic checked-in system text','Default restores checked-in in this order too');
})();'''
        self.run_js(code)

    def test_absent_text_when_defaults_were_loaded_before_render(self):
        config = make_config()
        config['prompts'] = {}
        code = r'''
(async()=>{
 await ctx.loadMissingPromptDefaults();
 await ctx.loadConfig();
 assert.strictEqual(element('review-system-prompt').value,'Synthetic checked-in system text');
 assert.strictEqual(element('review-user-prompt').value,'Synthetic checked-in user text {context} {batch}');
 assert.strictEqual(read('activePassPromptPreset.review'),'default');
 const payload=ctx.buildConfigPayload(2);
 assert.strictEqual(payload.prompts.review_preset,'default');
 assert.strictEqual(JSON.stringify(payload.prompts.review_prompt_presets),'[]');
})();'''
        self.run_js(code, config=config)


if __name__ == '__main__':
    unittest.main()
