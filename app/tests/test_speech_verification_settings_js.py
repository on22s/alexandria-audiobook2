"""Optional future-render controls preserve configuration until loaded."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC=Path(__file__).resolve().parent.parent/'static'


class SpeechVerificationSettingsJsTests(unittest.TestCase):
    def test_off_default_roundtrip_and_unloaded_save_does_not_override_settings(self):
        script=DOM_FIXTURE+r'''
const field=body.appendChild(new Element('input',{id:'speech-verification-enabled'}));
field.checked=false;field.disabled=true;
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{});
context.setSpeechVerificationControls(undefined);assert.equal(field.checked,false);assert.equal(field.disabled,false);
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:false}});
context.setSpeechVerificationControls({enabled:true});assert.equal(field.checked,true);
assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:true}});
field.checked=false;assert.deepEqual(JSON.parse(JSON.stringify(context.getSpeechVerificationConfig())),{speech_verification:{enabled:false}});
'''
        result=subprocess.run(['node','-e',script,str(STATIC/'js/app-speech-verification.js')],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
