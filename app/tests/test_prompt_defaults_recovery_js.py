"""Native default loading rejects malformed replies and retains in-flight edits."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'

class PromptDefaultsRecoveryJsTests(unittest.TestCase):
    def test_failed_load_retry_and_edits_preserved(self):
        script = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
const fields={};const el=id=>fields[id]||(fields[id]={value:'',textContent:'',hidden:true,disabled:false});let calls=0,reply,resolve;
const c={document:{getElementById:el},activePassPromptPreset:{pass1:'default',pass3:'default'},passPromptDefaults:{pass1:{system_prompt:'old S',user_prompt:'old U'},pass3:{system_prompt:'old3 S',user_prompt:'old3 U'}},API:{get:async path=>{assert.strictEqual(path,'/api/default_prompts');calls++;if(reply instanceof Error){throw reply;}return reply;}},console:{warn(){}}};vm.createContext(c);const a=s.indexOf('let promptDefaultsLoadPending =');assert(a>=0);vm.runInContext(s.slice(a,s.indexOf('async function loadConfig()',a)),c);
const good=Object.fromEntries(['review_system_prompt','review_user_prompt','persona_system_prompt','persona_user_prompt','persona_advanced_prompt','pass1_system_prompt','pass1_user_prompt','pass3_system_prompt','pass3_user_prompt'].map(key=>[key,key+' default']));
let finished=false;process.on('beforeExit',()=>assert(finished,'native async assertions must finish'));
(async()=>{
 el('persona-system-prompt').value='existing custom';reply=Error('raw transport');await c.loadMissingPromptDefaults();assert(!el('prompt-defaults-retry').hidden);assert(!el('prompt-defaults-retry').disabled);assert.match(el('prompt-defaults-status').textContent,/retry loading prompts/);assert(!el('prompt-defaults-status').textContent.includes('raw transport'));assert.strictEqual(el('persona-system-prompt').value,'existing custom');
 for(const invalid of [{},null,{...good,pass1_user_prompt:42},{...good,review_system_prompt:''}]){reply=invalid;await c.loadMissingPromptDefaults();assert(!el('prompt-defaults-retry').hidden);assert.strictEqual(el('review-user-prompt').value,'');assert.strictEqual(c.passPromptDefaults.pass1.system_prompt,'old S');}
 c.API.get=()=>{calls++;return new Promise(done=>resolve=done);};const pending=c.loadMissingPromptDefaults();assert(el('prompt-defaults-retry').disabled);const beforeCalls=calls;await c.loadMissingPromptDefaults();assert.strictEqual(calls,beforeCalls);el('review-user-prompt').value='typed while loading';el('pass1-system-prompt').value='edited default';c.activePassPromptPreset.pass3='mine';resolve(good);await pending;
 assert.strictEqual(el('review-user-prompt').value,'typed while loading');assert.strictEqual(el('persona-system-prompt').value,'existing custom');assert.strictEqual(el('review-system-prompt').value,good.review_system_prompt);assert.strictEqual(el('pass1-system-prompt').value,'edited default');assert.strictEqual(el('pass1-user-prompt').value,good.pass1_user_prompt);assert.strictEqual(el('pass3-system-prompt').value,'');assert.strictEqual(c.activePassPromptPreset.pass3,'mine');assert(el('prompt-defaults-retry').hidden);assert(!el('prompt-defaults-retry').disabled);assert.strictEqual(c.passPromptDefaults.pass3.system_prompt,good.pass3_system_prompt);finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
