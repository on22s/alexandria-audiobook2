"""Manual wait is distinguished from model work immediately, preserving other ETA."""
from pathlib import Path
import subprocess
import unittest
class ManualActivityTests(unittest.TestCase):
    def test_paused_task_hides_model_wait_and_eta_until_resumed(self):
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const c={Date:{now:()=>50000},formatDuration:n=>n+'s'};vm.createContext(c);const a=s.indexOf('const ACTIVITY_MARKER');vm.runInContext(s.slice(a,s.indexOf('// Every task',a)),c);
const el={},track={count:1,changedAt:20000},status={running:true,paused:true,logs:['[PAUSED]'],eta:{eta_seconds:20}};
for(const manual of [null,{id:'one'}]){status.manual_request=manual;c.renderActivity(el,status,track);assert.match(el.textContent,/Paused/);assert.match(el.textContent,/Resume/);assert(!el.textContent.includes('Working'));assert(!el.textContent.includes('waiting on the model'));assert(!el.textContent.includes('left'));assert.equal(el.hidden,false);}
status.manual_request=null;status.paused=false;status.logs.push('[RESUMED]');c.renderActivity(el,status,track);assert.match(el.textContent,/Working/);assert.match(el.textContent,/20s left/);assert(!el.textContent.includes('waiting on the model'));assert.equal(track.changedAt,50000);status.running=false;c.renderActivity(el,status,track);assert.equal(el.hidden,true);
'''
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_pending_reply_is_announced_without_model_wait_or_running_eta(self):
        source=Path(__file__).resolve().parent.parent/'static/js/app-core.js'
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');const c={Date:{now:()=>50000},formatDuration:n=>n+'s'};vm.createContext(c);const a=s.indexOf('const ACTIVITY_MARKER');vm.runInContext(s.slice(a,s.indexOf('// Every task',a)),c);
const el={},status={running:true,logs:[],eta:{eta_seconds:20},manual_request:{id:'one'}};c.renderActivity(el,status,{count:0,changedAt:20000});assert.match(el.textContent,/your reply/);assert.match(el.textContent,/top of the page/);assert(!el.textContent.includes('waiting on the model'));assert(!el.textContent.includes('left'));assert.equal(el.hidden,false);
status.manual_request=null;c.renderActivity(el,status,{count:0,changedAt:20000});assert.match(el.textContent,/waiting on the model/);assert.match(el.textContent,/20s left/);status.running=false;c.renderActivity(el,status,{});assert.equal(el.hidden,true);
'''
        r=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
