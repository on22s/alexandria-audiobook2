import json
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE
from tts_capabilities import get_tts_capability_modes

STATIC = Path(__file__).resolve().parent.parent / 'static'

class TtsCapabilityGuidanceJsTests(unittest.TestCase):
    def test_mode_changes_preserve_assignment_fields_and_warn_without_posting(self):
        setup = r'''
context.window=context;document.addEventListener=()=>{};
for(const id of ['tts-mode','tts-capability-summary','tts-url-group','tts-device-group','tts-local-options','tts-external-timeout-group']){body.appendChild(new Element('div',{id}));}
const card=new Element('div',{class:'voice-card card-body'}), choice=new Element('input',{class:'voice-type'});choice.value='lora';choice.checked=true;card.appendChild(choice);body.appendChild(card);
const originalQuery=card.querySelector;card.querySelector=s=>s==='.voice-type:checked'?choice:card.querySelectorAll(s)[0]||null;
for(const kind of ['custom','builtin-lora','clone','lora','design','ensemble']){card.appendChild(new Element('div',{class:kind+'-opts'}));}
context.API={post(){throw Error('must not save');}};
load();
'''
        code = DOM_FIXTURE + setup + '\nconst modes=' + json.dumps(get_tts_capability_modes()) + r''';
document.getElementById('tts-mode').value='external';context.setTtsCapabilityModes(modes);
assert.equal(choice.checked,true);assert.equal(choice.value,'lora');assert.equal(document.getElementById('tts-url-group').style.display,'');assert.equal(document.getElementById('tts-device-group').style.display,'none');assert.equal(card.querySelector('.tts-capability-notice').hidden,false);
assert.match(card.querySelector('.tts-capability-notice').textContent,/assignment is kept/);
document.getElementById('tts-mode').value='local';context.applyTtsCapabilityGuidance();assert.equal(choice.checked,true);assert.equal(choice.value,'lora');assert.equal(card.querySelector('.tts-capability-notice').hidden,true);assert.equal(document.getElementById('tts-device-group').style.display,'');
document.getElementById('tts-mode').value='external';let saves=0;context.saveVoicesDebounced=()=>{saves++;};
const core=fs.readFileSync(process.argv[1].replace('app-tts-capabilities.js','app-core.js'),'utf8'),start=core.indexOf('window.toggleVoiceType =');vm.runInContext(core.slice(start,core.indexOf('        async function refreshVoiceMetadata()',start)),context);
choice.value='design';context.toggleVoiceType(choice);assert.equal(saves,1);assert.equal(card.querySelector('.tts-capability-notice').hidden,false);assert.equal(card.querySelector('.design-opts').style.display,'block');assert.equal(choice.value,'design');
context.setTtsCapabilityModes(null);assert.equal(choice.checked,true);assert.match(card.querySelector('.tts-capability-notice').textContent,/capabilities unavailable/);assert(!choice.hasAttribute('aria-description'));assert.match(document.getElementById('tts-capability-summary').textContent,/unavailable/);
'''
        result = subprocess.run(['node', '-e', code, str(STATIC/'js/app-tts-capabilities.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
