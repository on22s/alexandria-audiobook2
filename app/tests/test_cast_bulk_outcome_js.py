"""Bulk cast UI reports successful and failed books separately."""
from pathlib import Path
import subprocess
import unittest
SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class CastBulkOutcomeJsTests(unittest.TestCase):
    def test_all_failed_partial_success_and_empty_results_have_honest_headings(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');const panel={innerHTML:''},statuses=[];let reply;
const c={window:{_selectedCast:'A'},document:{getElementById:()=>panel},_collectCastApplyMapping:()=>({Alice:'role'}),API:{post:async()=>reply},setCastStatus:(text,error)=>statuses.push({text,error}),getCastApplyWarningsHtml:()=>'',escapeHtml:String};vm.createContext(c);const a=source.indexOf('async function submitCastApplyBulk(');vm.runInContext(source.slice(a,source.indexOf('// Identity anchors that take over',a)),c);
(async()=>{
for(const [results,expected,isError] of [
 [[{name:'Missing A',count:0,error:'Saved script not found'},{name:'Missing B',count:0,error:'Saved script not found'}],'No books updated; 2 failed',true],
 [[{name:'Saved',count:2},{name:'Missing',count:0,error:'Saved script not found'}],'Applied to 1 book; 1 failed',true],
 [[{name:'Saved',count:2}],'Applied to 1 book',false],
 [[],'No books updated',true]]){
 reply={results};await c.submitCastApplyBulk(['A','B']);const status=statuses.at(-1);assert(status.text.includes(expected),status.text);assert.strictEqual(!!status.error,isError);assert(panel.innerHTML.includes(expected),panel.innerHTML);if(isError){assert(!status.text.includes('text-success'));}
}
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
