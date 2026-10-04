"""Native voice-change loads retain current editors across delayed replies."""
from pathlib import Path
import subprocess
import unittest

class VoiceStateLoadFeedbackJsTests(unittest.TestCase):
    def test_busy_retry_edits_and_book_ownership(self):
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');let resolve,reject,calls=0;const messages=[];const choice={value:'main'};const target={innerHTML:'',isConnected:true,querySelectorAll:()=>[choice]};const card={dataset:{voice:'Alice /'},isConnected:true,querySelector:()=>target};const button={disabled:false,attrs:{},closest:()=>card,setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];}};
const c={currentBookFilename:'A',window:{},console:{debug(){}},showToast:m=>messages.push(m),renderVoiceStateRows:data=>'rendered '+data.marker,API:{get:url=>{assert.strictEqual(url,'/api/voices/Alice%20%2F/state_timeline');calls++;return new Promise((r,j)=>{resolve=r;reject=j;});}}};vm.createContext(c);const a=s.indexOf('const pendingVoiceStateLoads =');vm.runInContext(s.slice(a,s.indexOf('async function applyVoiceStates(',a)),c);
let done=false;process.on('beforeExit',()=>assert(done));(async()=>{
let pending=c.openVoiceStates(button);assert(button.disabled);assert.strictEqual(button.attrs['aria-busy'],'true');assert(target.innerHTML.includes('Loading voice changes'));await c.openVoiceStates(button);assert.strictEqual(calls,1);reject(Error('<internal>'));await pending;assert(!button.disabled);assert.strictEqual(button.attrs['aria-busy'],undefined);assert(target.innerHTML.includes('Retry voice changes'));assert(!target.innerHTML.includes('<internal>'));
pending=c.openVoiceStates(button);resolve({marker:'first'});await pending;assert.strictEqual(target.innerHTML,'rendered first');assert.strictEqual(c.window._voiceStateSuggestions['Alice /'].marker,'first');
pending=c.openVoiceStates(button);choice.value='version:edited';resolve({marker:'obsolete'});await pending;assert.strictEqual(target.innerHTML,'rendered first');assert.strictEqual(choice.value,'version:edited');assert.strictEqual(c.window._voiceStateSuggestions['Alice /'].marker,'first');assert(messages.at(-1).includes('kept'));
pending=c.openVoiceStates(button);c.currentBookFilename='B';resolve({marker:'wrong book'});await pending;assert.strictEqual(target.innerHTML,'rendered first');assert(!button.disabled);
c.currentBookFilename='A';pending=c.openVoiceStates(button);const before=target.innerHTML;reject(Error('offline'));await pending;assert.strictEqual(target.innerHTML,before);pending=c.openVoiceStates(button);card.isConnected=false;resolve({marker:'detached'});await pending;assert.strictEqual(target.innerHTML,before);assert(!button.disabled);done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
