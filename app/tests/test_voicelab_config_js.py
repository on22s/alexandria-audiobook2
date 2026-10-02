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
let fail = false;
const context = {
 document:{getElementById(id){return elements[id]||(elements[id]={value:'',innerHTML:'',title:'',removeAttribute(key){delete this[key];}});}},
 console:{error(...args){errors.push(args);}},
 API:{get:async path=>{assert.strictEqual(path,'/api/voicelab/config');if(fail){throw new Error('HTTP 503');}
  return {config:{rocm_python:'python-path',profiler_model:'model-path',zips_dir:'zip-path',epub_dirs:['books']},checks:{rocm_python:true,profiler_model:true,zips_dir:true,epub_dirs:true}};}}
};
vm.runInNewContext(code,context);
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
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
