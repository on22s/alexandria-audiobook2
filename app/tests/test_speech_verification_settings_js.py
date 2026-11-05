"""Optional future-render controls preserve configuration until loaded."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC=Path(__file__).resolve().parent.parent/'static'


class CheckboxInventoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs_by_id={}
        self.labels_by_for={}
        self.current_label_for=None

    def handle_starttag(self, tag, attrs):
        attributes=dict(attrs)
        if tag=='input' and attributes.get('id'):
            self.inputs_by_id[attributes['id']]=attributes
        if tag=='label' and attributes.get('for'):
            self.current_label_for=attributes['for']
            self.labels_by_for[self.current_label_for]=''

    def handle_endtag(self, tag):
        if tag=='label':
            self.current_label_for=None

    def handle_data(self, data):
        if self.current_label_for is not None:
            self.labels_by_for[self.current_label_for]=self.labels_by_for.get(self.current_label_for,'')+data


class SpeechVerificationSettingsJsTests(unittest.TestCase):
    def test_off_default_roundtrip_and_unloaded_save_does_not_override_settings(self):
        script=DOM_FIXTURE+r'''
Object.defineProperty(Element.prototype,'disabled',{
 get(){return this.hasAttribute('disabled');},
 set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}
});
const field=body.appendChild(new Element('input',{id:'speech-verification-enabled'}));
const retryField=body.appendChild(new Element('input',{id:'speech-retry-enabled'}));
field.checked=false;field.disabled=true;retryField.checked=false;retryField.disabled=true;
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{});
retryField.checked=true;assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{});
context.setSpeechVerificationControls(undefined);assert.equal(field.checked,false);assert.equal(field.disabled,false);
assert.equal(retryField.checked,false);assert.equal(retryField.disabled,true);
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:false},speech_retry:{enabled:false}});
context.setSpeechVerificationControls({enabled:true},{enabled:true});assert.equal(field.checked,true);assert.equal(retryField.checked,true);assert.equal(retryField.disabled,false);
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:true},speech_retry:{enabled:true}});
context.setSpeechVerificationControls({enabled:true},{enabled:false});assert.equal(retryField.checked,false);
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:true},speech_retry:{enabled:false}});
context.setSpeechVerificationControls({enabled:false},{enabled:true});assert.equal(field.checked,false);assert.equal(retryField.checked,false);assert.equal(retryField.disabled,true);
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:false},speech_retry:{enabled:false}});
context.setSpeechVerificationControls({enabled:true},{enabled:true});assert.equal(retryField.disabled,false);
field.checked=false;context.updateSpeechRetryControls();assert.equal(retryField.disabled,true);assert.equal(retryField.checked,false);
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:false},speech_retry:{enabled:false}});
field.checked=true;context.updateSpeechRetryControls();assert.equal(retryField.disabled,false);retryField.checked=true;
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:true},speech_retry:{enabled:true}});
retryField.checked=false;assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:true},speech_retry:{enabled:false}});
'''
        result=subprocess.run(['node','-e',script,str(STATIC/'js/app-speech-verification.js')],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_actual_index_html_checkbox_inventory(self):
        parser=CheckboxInventoryParser()
        parser.feed((STATIC/'index.html').read_text(encoding='utf-8'))
        verification_input=parser.inputs_by_id.get('speech-verification-enabled')
        retry_input=parser.inputs_by_id.get('speech-retry-enabled')
        self.assertIsNotNone(verification_input)
        self.assertIsNotNone(retry_input)
        self.assertEqual('checkbox',verification_input.get('type'))
        self.assertIn('disabled',verification_input)
        self.assertEqual('checkbox',retry_input.get('type'))
        self.assertIn('disabled',retry_input)
        self.assertTrue(retry_input.get('title'))
        self.assertIn('retry',retry_input['title'].lower())
        self.assertIn('missing or extra speech',parser.labels_by_for.get('speech-verification-enabled','').lower())
        self.assertIn('retry',parser.labels_by_for.get('speech-retry-enabled','').lower())
        self.assertIn('missing or extra speech',parser.labels_by_for.get('speech-retry-enabled','').lower())


if __name__ == '__main__':
    unittest.main()
