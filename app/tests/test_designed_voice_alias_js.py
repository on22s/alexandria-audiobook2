"""Exercise designed-voice alias editing in the real JavaScript handlers."""
from pathlib import Path
import os
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "static/js/app-scripts.js"


class DesignedVoiceAliasJsTests(unittest.TestCase):
    def test_persona_editor_uses_selected_card_alias_and_transcript(self):
        script = r'''
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const reset = source.slice(source.indexOf('function invalidateDesignerWork()'), source.indexOf('window.generateDesignPreview ='));
const open = source.slice(source.indexOf('window.openVoiceDesignEditor ='), source.indexOf('window.onDesignedVoiceSelect ='));
const elements = {};
const context = {
    window: {_currentPreviewFile:'A.wav',_editingDesignedVoiceId:'A'},
    document: {
        getElementById(id) {
            if (!elements[id]) { elements[id] = {value:'A stale',innerHTML:'A options',style:{},dataset:{aliasLookupFailed:'true'}}; }
            return elements[id];
        },
        querySelector: () => ({click() {}})
    }
};
context.document.getElementById('design-sample-text');
context.document.getElementById('design-alias-select');
vm.runInNewContext(reset + open, context);
for (const alias of ['TARGET B', '']) {
    const card = {querySelector(selector) {
        return {
            '.design-description':{value:'B description'},
            '.ref-text':{value:'B transcript'},
            '.alias-select':{value:alias,innerHTML:'B alias options'}
        }[selector];
    }};
    const button = {closest: selector => selector === '.card-body' ? card : {dataset:{voice:'B'}}};
    context.window.openVoiceDesignEditor(button);
    assert.strictEqual(elements['design-voice-name'].value, 'B');
    assert.strictEqual(elements['design-source-name'].value, 'B');
    assert.strictEqual(elements['design-description'].value, 'B description');
    assert.strictEqual(elements['design-sample-text'].value, 'B transcript');
    assert.strictEqual(elements['design-alias-select'].value, alias);
    assert.strictEqual(elements['design-alias-select'].innerHTML, 'B alias options');
    assert.strictEqual(elements['design-alias-select'].dataset.aliasLookupFailed, 'false');
    assert.strictEqual(context.window._currentPreviewFile, null);
    assert.strictEqual(context.window._editingDesignedVoiceId, null);
}
'''
        result = subprocess.run(["node", "-e", script, str(SOURCE)],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_lookup_failure_preserves_alias_and_fresh_result_populates_options(self):
        script = r'''
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const save = source.slice(source.indexOf('window.saveDesignedVoice ='), source.indexOf('window.playDesignedVoice ='));
const open = source.slice(source.indexOf('window.openDesignedVoiceForEdit ='), source.indexOf('window.openVoiceDesignEditor ='));
const identity = source.slice(source.indexOf('function invalidateDesignerWork()'), source.indexOf('function resetDesignerForm()'));

async function run(lookupFails) {
    const elements = {};
    for (const id of ['design-voice-name', 'design-source-name', 'design-description', 'design-sample-text',
                      'design-alias-select', 'design-preview-audio', 'design-preview-container', 'btn-design-preview']) {
        elements[id] = {value: '', style: {}, focus() {}};
    }
    const select = elements['design-alias-select'];
    select.dataset = {};
    select.options = [];
    let selected = '';
    Object.defineProperty(select, 'innerHTML', {set() { select.options = [{value: ''}]; selected = ''; }});
    Object.defineProperty(select, 'value', {
        get() { return selected; },
        set(value) { selected = select.options.some(option => option.value === value) ? value : ''; }
    });
    select.appendChild = option => select.options.push(option);
    const aliasCard = {value: 'Old'};
    let autosaves = 0;
    const context = {
        window: {_designedVoicesCache: [{id: 'id1', name: 'Voice', filename: 'voice.wav'}], _voicesNames: ['Stale']},
        document: {
            getElementById: id => elements[id],
            createElement: () => ({value: '', text: ''}),
            querySelector: selector => selector === '[data-tab="designer"]' ? {click() {}} : {
                querySelector: () => aliasCard
            }
        },
        API: {get: async () => { if (lookupFails) { throw new Error('offline'); }
            return [{name: 'Voice', config: {alias_of: 'Old'}}, {name: 'Old', config: {}}];
        }, post: async () => ({})},
        showToast() {}, loadDesignedVoices() {}, saveVoicesDebounced() {autosaves++;}
    };
    vm.runInNewContext(identity + save + open, context);
    await context.window.openDesignedVoiceForEdit('id1');
    if (lookupFails) {
        context.window._currentPreviewFile = 'voice.wav';
        await context.window.saveDesignedVoice();
        assert.strictEqual(aliasCard.value, 'Old');
        assert.strictEqual(autosaves, 0);
    } else {
        assert.strictEqual(select.value, 'Old');
        assert(select.options.some(option => option.value === 'Old'));
        assert(!select.options.some(option => option.value === 'Stale'));
    }
}
(async () => { await run(true); await run(false); })().catch(error => {console.error(error); process.exitCode = 1;});
'''
        source = os.environ.get("DESIGNED_VOICE_JS_SOURCE", str(SOURCE))
        result = subprocess.run(["node", "-e", script, source], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_saved_alias_targets_character_names_with_css_metacharacters(self):
        script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const save = source.slice(source.indexOf('window.saveDesignedVoice ='), source.indexOf('window.playDesignedVoice ='));
async function run(name, escaped) {
    const fields = {
        'design-voice-name': {value:name}, 'design-source-name': {value:name},
        'design-description': {value:'Description'}, 'design-sample-text': {value:'Transcript'},
        'design-alias-select': {value:'TARGET',dataset:{}}
    };
    const alias = {value:'OLD'};
    const posts = [], toasts = [], escapes = [];
    let saves = 0;
    const context = {
        window: {_currentPreviewFile:'preview.wav', _designedVoicesCache:[]},
        CSS: {escape(value) { escapes.push(value); assert.strictEqual(value, name); return escaped; }},
        document: {
            getElementById: id => fields[id],
            querySelector(selector) {
                assert.strictEqual(selector, `.voice-card[data-voice="${escaped}"]`);
                return {querySelector: selector => { assert.strictEqual(selector, '.alias-select'); return alias; }};
            }
        },
        API: {post: async (path,data) => { posts.push({path,data}); }},
        loadDesignedVoices() {}, saveVoicesDebounced() {saves++;},
        showToast(message,kind) {toasts.push({message,kind});}
    };
    vm.runInNewContext(save, context);
    await context.window.saveDesignedVoice();
    assert.deepStrictEqual(toasts, []);
    assert.deepStrictEqual(escapes, [name]);
    assert.strictEqual(alias.value, 'TARGET');
    assert.strictEqual(saves, 1);
    assert.strictEqual(posts.length, 1);
    assert.strictEqual(posts[0].path, '/api/voice_design/save');
    assert.strictEqual(posts[0].data.name, name);
    assert.strictEqual(posts[0].data.preview_file, 'preview.wav');
}
(async () => {
    await run('The "Doctor"', 'The\\ \\"Doctor\\"');
    await run('Back\\Slash', 'Back\\\\Slash');
    await run('Square] Name', 'Square\\]\\ Name');
    await run('Plain Name', 'Plain\\ Name');
})().catch(error => {console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(["node", "-e", script, str(SOURCE)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class DesignedVoiceSaveGuardJsTests(unittest.TestCase):
    def test_pending_save_blocks_duplicates_and_failure_allows_retry(self):
        script = r"""
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('window.saveDesignedVoice ='), source.indexOf('window.playDesignedVoice ='));
function deferred() { let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise,resolve,reject}; }
async function run(fail) {
 const elements = {'design-voice-name':{value:'Voice'},'design-description':{value:'Calm'},
  'design-sample-text':{value:'Sample'},'design-source-name':{value:''},'design-alias-select':{value:'',dataset:{}}};
 const requests=[], toasts=[]; let reloads=0; let pending=deferred();
 const context={window:{_currentPreviewFile:'preview.wav',_editingDesignedVoiceId:'existing',_designedVoicesCache:[{id:'existing'}]},
  document:{getElementById:id=>elements[id]}, API:{post(path,payload){requests.push({path,payload});return pending.promise;}},
  showToast:(...args)=>toasts.push(args),loadDesignedVoices(){reloads++;}};
 vm.runInNewContext(code,context);
 const first=context.window.saveDesignedVoice();
 assert.strictEqual(requests.length,1);
 const duplicate=context.window.saveDesignedVoice();
 assert.strictEqual(requests.length,1,'pending save must not dispatch a second mutation');
 assert.strictEqual(requests[0].path,'/api/voice_design/save');
 assert.deepStrictEqual(JSON.parse(JSON.stringify(requests[0].payload)),
  {name:'Voice',description:'Calm',sample_text:'Sample',preview_file:'preview.wav',voice_id:'existing'});
 if(fail){pending.reject(new Error('save offline'));}else{pending.resolve({status:'saved'});}
 await Promise.all([first,duplicate]);
 assert.strictEqual(reloads,fail?0:1);
 assert.strictEqual(elements['design-voice-name'].value,fail?'Voice':'');
 assert.strictEqual(context.window._editingDesignedVoiceId,fail?'existing':null);
 assert.strictEqual(toasts.length,fail?1:0);
 if(fail){assert.strictEqual(toasts[0][1],'error');assert(toasts[0][0].includes('save offline'));}
 elements['design-voice-name'].value='Retry Voice';pending=deferred();
 const retry=context.window.saveDesignedVoice();assert.strictEqual(requests.length,2);
 assert.strictEqual(requests[1].payload.name,'Retry Voice');
 pending.resolve({status:'saved'});await retry;
 assert.strictEqual(reloads,fail?1:2);
}
(async()=>{await run(false);await run(true);})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(SOURCE)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_invalid_name_or_missing_preview_does_not_block_later_valid_save(self):
        script = r"""
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('window.saveDesignedVoice ='), source.indexOf('window.playDesignedVoice ='));
const elements={'design-voice-name':{value:''},'design-description':{value:'Calm'},'design-sample-text':{value:'Sample'},
 'design-source-name':{value:''},'design-alias-select':{value:'',dataset:{}}};
const requests=[],toasts=[];
const context={window:{_currentPreviewFile:'preview.wav',_designedVoicesCache:[]},document:{getElementById:id=>elements[id]},
 API:{post:async(path,payload)=>{requests.push({path,payload});return {}; }},showToast:(...args)=>toasts.push(args),loadDesignedVoices(){}};
vm.runInNewContext(code,context);
(async()=>{
 await context.window.saveDesignedVoice();assert.strictEqual(requests.length,0);
 elements['design-voice-name'].value='Voice';context.window._currentPreviewFile=null;
 await context.window.saveDesignedVoice();assert.strictEqual(requests.length,0);
 assert.strictEqual(toasts.length,2);assert(toasts.every(t=>t[1]==='warning'));
 context.window._currentPreviewFile='preview.wav';await context.window.saveDesignedVoice();
 assert.strictEqual(requests.length,1);assert.strictEqual(requests[0].payload.voice_id,null);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(SOURCE)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
