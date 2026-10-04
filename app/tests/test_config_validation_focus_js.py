"""Native validation associates rejects with fields and reveals their containers."""
from pathlib import Path
import subprocess
import unittest

class ConfigValidationFocusTests(unittest.TestCase):
    def test_rejected_values_reveal_field_without_changing_it(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        script = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
let focused=0,scrolled=0,shown,toast;
const collapse={tagName:'DIV',parentElement:null,classList:{contains:n=>n==='collapse'},addEventListener:(name,fn,opts)=>{assert.equal(name,'shown.bs.collapse');assert(opts.once);shown=fn;}};
const details={tagName:'DETAILS',open:false,parentElement:collapse,classList:{contains:()=>false}};
const field={value:'{',parentElement:details,focus:()=>focused++,scrollIntoView:()=>scrolled++};
const c={document:{getElementById:()=>field},bootstrap:{Collapse:{getOrCreateInstance:(p,opts)=>{assert.equal(p,collapse);assert.equal(opts.toggle,false);return{show(){}};}}},showToast:m=>toast=m};vm.createContext(c);
let a=s.indexOf('function getConfigValidationError(');assert(a>=0,'field error helper missing');vm.runInContext(s.slice(a,s.indexOf('function isTaskFailed(',a)),c);
a=s.indexOf('function getJsonObjectInput(');vm.runInContext(s.slice(a,s.indexOf('function getEditedLlmProfile(',a)),c);
for(const [fn,raw] of [['getJsonObjectInput','{'],['getJsonObjectInput','[]'],['getOptionalNumberInput','NaN'],['getIntListInput','1,-2'],['getIntListInput','9007199254740992']]){
 field.value=raw;let error;try{c[fn]('invalid','Field',[]);}catch(e){error=e;}assert(error);assert.equal(error.fieldId,'invalid');assert.equal(focused,0);c.showConfigValidationError(error);assert(details.open);assert.equal(focused,0);assert.equal(field.value,raw);shown();assert.equal(focused,1);assert.equal(scrolled,1);assert.equal(toast,error.message);focused=scrolled=0;
}
field.parentElement=null;field.value='{}';assert.equal(JSON.stringify(c.getJsonObjectInput('valid','Field')),'{}');field.value='';assert.equal(c.getOptionalNumberInput('valid','Field'),null);assert.equal(JSON.stringify(c.getIntListInput('valid','Field',[2])),'[2]');
c.showConfigValidationError(Error('unrelated'));assert.equal(focused,0);assert.equal(toast,'unrelated');
'''
        result = subprocess.run(['node','-e',script,str(source)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
