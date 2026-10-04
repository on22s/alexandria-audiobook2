"""Native integrity links reveal filtered targets and retain missing-row guidance."""
from pathlib import Path
import subprocess
import unittest

class IntegrityJumpTests(unittest.TestCase):
    def test_jump_reveals_filtered_target_without_changing_filter_for_visible_or_missing_rows(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),s=fs.readFileSync(process.argv[1],'utf8');let scrolls=0,highlights=0,timers=[];
const filter={checked:true},toasts=[];const hidden={style:{display:'none'},querySelector:()=>null,scrollIntoView:()=>scrolls++,classList:{add:()=>highlights++,remove:()=>highlights--}};
const visible={...hidden,style:{display:''},querySelector:()=>({})};const c={document:{getElementById:()=>filter,querySelector:q=>q.includes('"0"')?hidden:q.includes('"1"')?visible:null,querySelectorAll:()=>[hidden,visible]},showToast:(m,t)=>toasts.push([m,t]),setTimeout:fn=>timers.push(fn)};vm.createContext(c);
let a=s.indexOf('function scrollToChunkRow(');vm.runInContext(s.slice(a,s.indexOf('// --- Editor Tab ---',a)),c);a=s.indexOf('function applyDriftFilter(');vm.runInContext(s.slice(a,s.indexOf('async function runDriftCheck(',a)),c);
c.scrollToChunkRow(0);assert.equal(filter.checked,false);assert.equal(hidden.style.display,'');assert.equal(scrolls,1);assert.equal(highlights,1);assert(toasts.some(([m])=>m.includes('entry 1')));timers.shift()();assert.equal(highlights,0);
filter.checked=true;c.applyDriftFilter();c.scrollToChunkRow(1);assert.equal(filter.checked,true);assert.equal(scrolls,2);assert.equal(hidden.style.display,'none');
c.scrollToChunkRow(9);assert.equal(filter.checked,true);assert.equal(scrolls,2);assert(toasts.some(([m])=>m.includes('Entry 10 is not in the table yet')));
'''
        result=subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
