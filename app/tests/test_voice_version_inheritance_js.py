"""Native version create waits for saved voice edits and refuses stale admission."""
from pathlib import Path
import subprocess
import unittest

class VoiceVersionInheritanceTests(unittest.TestCase):
    def test_flush_failure_or_changed_book_refuses_creation_and_no_ryan_override_is_sent(self):
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');let posts=[],events=[],resolveFlush,fail=false,choice={name:'teen',description:'teen'};
const card={dataset:{voice:'Alice 日本語'},isConnected:true},button={disabled:false,closest:()=>card};const c={window:null,currentBookFilename:'A',showPresetEditor:async()=>choice,showToast(){},flushVoiceSaves:async()=>{events.push('flush');if(fail){throw Error('unsaved');}await new Promise(r=>resolveFlush=r);},API:{post:async(p,b)=>{events.push('post');posts.push(b);}},loadVoices:async()=>events.push('refresh')};c.window=c;vm.createContext(c);vm.runInContext(s.slice(s.indexOf('function showActionError('),s.indexOf('function showConfirm(')),c);const a=s.indexOf('window.addVoiceVersion =');vm.runInContext(s.slice(a,s.indexOf('window.saveNarratorStrategy',a)),c);
let done=false;process.on('beforeExit',()=>assert(done));(async()=>{let pending=c.addVoiceVersion(button);await new Promise(setImmediate);assert(button.disabled);assert.equal(posts.length,0);assert.deepEqual(events,['flush']);resolveFlush();await pending;assert.deepEqual(events,['flush','post','refresh']);assert.deepEqual(JSON.parse(JSON.stringify(posts[0])),{version_id:'teen',age_group:'teen'});assert(!button.disabled);
for(const changed of ['book','card']){pending=c.addVoiceVersion(button);await new Promise(setImmediate);if(changed==='book'){c.currentBookFilename='B';}else{card.isConnected=false;}resolveFlush();await pending;assert.equal(posts.length,1);assert(!button.disabled);c.currentBookFilename='A';card.isConnected=true;}
fail=true;await c.addVoiceVersion(button);assert.equal(posts.length,1);assert(!button.disabled);choice=null;const before=events.length;await c.addVoiceVersion(button);assert.equal(events.length,before);done=true;})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
