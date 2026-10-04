"""Exercise Voice Lab settings refresh and unavailable-state recovery."""
from pathlib import Path
import subprocess
import unittest


class VoicelabConfigJsTests(unittest.TestCase):
    def test_failed_refresh_replaces_ready_badge_without_erasing_editable_settings(self):
        source = Path(__file__).resolve().parent.parent / "static/js/app-voicelab.js"
        script = r"""
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf('function _vlSetCheckIcon('), source.indexOf('window.saveVoicelabConfig ='));
const elements = {}, errors = [];
let fail = false,checks={rocm_python:true,profiler_model:true,zips_dir:true,epub_dirs:true},profilerErrors=[];
const context = {
 document:{getElementById(id){return elements[id]||(elements[id]={value:'',innerHTML:'',title:'',removeAttribute(key){delete this[key];}});}},
 console:{error(...args){errors.push(args);}},
 API:{get:async path=>{assert.strictEqual(path,'/api/voicelab/config');if(fail){throw new Error('HTTP 503');}
  return {config:{rocm_python:'python-path',profiler_model:'model-path',zips_dir:'zip-path',epub_dirs:['books']},checks,profiler_errors:profilerErrors};}}
};
const guidanceCore=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInNewContext(guidanceCore.slice(guidanceCore.indexOf('function showActionError('),guidanceCore.indexOf('function showConfirm('))+code,context);
let finished=false;process.on('beforeExit',()=>assert(finished));
(async()=>{
 await context.loadVoicelabConfig();
 const readiness = elements['voicelab-readiness'];assert(readiness.innerHTML.includes('bg-success'));
 elements['vl-profiler_model'].value='unsaved-model';fail=true;
 await context.loadVoicelabConfig();
 assert(!readiness.innerHTML.includes('bg-success'),'Failed refresh must not still claim ready');
 assert(readiness.innerHTML.includes('unavailable'));assert(readiness.title.includes('HTTP 503'));
 assert.strictEqual(elements['vl-profiler_model'].value,'unsaved-model');
 assert.strictEqual(elements['vl-zips_dir'].value,'zip-path');assert.strictEqual(errors.length,1);
 fail=false;await context.loadVoicelabConfig();
 assert(readiness.innerHTML.includes('bg-success'));assert(!readiness.innerHTML.includes('unavailable'));
 assert.strictEqual(readiness.title,undefined);assert.strictEqual(elements['vl-profiler_model'].value,'model-path');
 checks={rocm_python:false,batch_train_lora:false,zips_dir:true};await context.loadVoicelabConfig();
 const help=elements['voicelab-readiness-help'];assert(!help.hidden);assert(help.textContent.includes('ROCm Python'));assert(help.textContent.includes('Batch training script'));assert(help.textContent.includes('save settings'));assert(help.textContent.includes('repair the installation'));assert(readiness.title.includes('ROCm Python'));assert(elements['chk-rocm_python'].title.includes('configured path'));
 profilerErrors=['Missing <environment>'];await context.loadVoicelabConfig();assert(help.textContent.includes('Missing <environment>'));
 checks={rocm_python:true};profilerErrors=[];await context.loadVoicelabConfig();assert(help.hidden);assert.strictEqual(help.textContent,'');assert.strictEqual(readiness.title,undefined);assert.strictEqual(elements['chk-rocm_python'].title,'Found');
 fail=true;await context.loadVoicelabConfig();assert(!help.hidden);assert(help.textContent.includes('current fields are kept'));finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
