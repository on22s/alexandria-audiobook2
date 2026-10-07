"""Native Apply/Clear serialize a speaker and capture choices before awaits."""
from pathlib import Path
import subprocess
import unittest

class VoiceStateSavePendingJsTests(unittest.TestCase):
    def test_same_speaker_guard_captured_rows_failure_and_book_change(self):
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');let release,fail=false,loads=0;const requests=[],toasts=[];let confirms=0,refreshes=0;
const first={value:'library:adapter',disabled:false},second={value:'version:original',disabled:false};const rows=[{dataset:{fromIndex:'2',age:'child'},querySelector:()=>first},{dataset:{fromIndex:'8',age:'adult'},querySelector:()=>second}];const apply={disabled:false,innerHTML:'Apply'},clear={disabled:false,innerHTML:'Clear'};const card={isConnected:true,dataset:{voice:'Alice'},querySelector:()=>({innerHTML:''}),querySelectorAll:q=>q==='.voice-state-row'?rows:[first,second,apply,clear]};apply.closest=clear.closest=()=>card;
const c={currentBookFilename:'A',_voiceSaveSnapshot:{book_token:'a'.repeat(64)},window:null,showConfirm:async()=>{confirms++;return true;},showToast:(...x)=>toasts.push(x),loadVoices:async()=>refreshes++,API:{get:async()=>{loads++;return {};},post:async(path,body)=>{requests.push([path,JSON.parse(JSON.stringify(body))]);if(path.endsWith('/versions')){await new Promise((r,j)=>release=()=>fail?j(Error('refused')):r());}},del:async path=>requests.push([path])}};c.window=c;c._voiceStateSuggestions={Alice:{states:[{sources:{library_unused:[{adapter_id:'adapter',config:{type:'lora'}}]}}]}};vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);const a=s.indexOf('const pendingVoiceStateLoads =');vm.runInContext(s.slice(a,s.indexOf('// Editor: from this line on',a)),c);
let done=false;process.on('beforeExit',()=>assert(done));(async()=>{
let pending=c.applyVoiceStates(apply);assert(apply.disabled&&clear.disabled&&first.disabled&&second.disabled);assert.strictEqual(apply.textContent,'Applying…');await c.applyVoiceStates(apply);await c.clearVoiceStates(clear);assert.strictEqual(requests.length,1);assert.strictEqual(confirms,0);await c.openVoiceStates({closest:()=>card});assert.strictEqual(loads,0,'refresh cannot replace panel while saving');second.value='version:later';release();await pending;assert.strictEqual(requests[1][1].points[1].version_id,'original');assert.strictEqual(requests[0][1].book_token,'a'.repeat(64));assert.strictEqual(requests[1][1].book_token,'a'.repeat(64));assert.strictEqual(refreshes,1);assert(!apply.disabled&&!clear.disabled&&!first.disabled);assert.strictEqual(apply.innerHTML,'Apply');
requests.length=0;pending=c.applyVoiceStates(apply);c.currentBookFilename='B';release();await pending;assert.strictEqual(requests.length,1,'no timeline write after book changed');assert(!apply.disabled&&!clear.disabled);assert.strictEqual(refreshes,1);
c.currentBookFilename='A';fail=true;pending=c.applyVoiceStates(apply);release();await pending;assert(!apply.disabled&&!first.disabled);assert(toasts.at(-1)[0].includes('refused'));fail=false;await c.clearVoiceStates(clear);assert.strictEqual(confirms,1);assert(!clear.disabled);assert.strictEqual(clear.innerHTML,'Clear');assert(requests.at(-1)[0].includes('?book_token='+ 'a'.repeat(64)));done=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stderr)
