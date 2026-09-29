"""Exercise designed-voice alias editing in the real JavaScript handlers."""
from pathlib import Path
import os
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "static/js/app-scripts.js"


class DesignedVoiceAliasJsTests(unittest.TestCase):
    def test_lookup_failure_preserves_alias_and_fresh_result_populates_options(self):
        script = r'''
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const save = source.slice(source.indexOf('window.saveDesignedVoice ='), source.indexOf('window.playDesignedVoice ='));
const open = source.slice(source.indexOf('window.openDesignedVoiceForEdit ='), source.indexOf('window.openVoiceDesignEditor ='));

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
    vm.runInNewContext(save + open, context);
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
