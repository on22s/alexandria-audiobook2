"""Actual inspections publish only the current request and directory result."""
from pathlib import Path
import os
import subprocess
import unittest

SOURCE = Path(os.environ.get('INSPECT_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-voicelab.js'))
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const fields={},requests=[];const el=id=>fields[id]||(fields[id]={value:'',innerHTML:''});
const ctx={window:null,document:{getElementById:el},escapeHtml:text=>String(text).replaceAll('<','&lt;'),API:{get:url=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});requests.push({url,resolve,reject});return promise;}}};ctx.window=ctx;vm.createContext(ctx);
const first=source.includes('let _voicelabInspectRequest =')?source.indexOf('let _voicelabInspectRequest ='):source.indexOf('window.voicelabInspect =');vm.runInContext(source.slice(first,source.indexOf('function _vlSetRunning(',first)),ctx);
const result=count=>({narrator_count:count,manifest:{trained:count,profiled:count},quality:{zip_count:count}});
'''


class VoicelabInspectIdentityJsTests(unittest.TestCase):
    def run_js(self, code):
        script = SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_old_success_or_error_cannot_overwrite_new_directory_or_same_directory_request(self):
        self.run_js(r'''
for(const same of [false,true]){for(const failure of [false,true]){
 el('vl-zips_dir').value='A #?';const old=ctx.voicelabInspect();const a=requests.at(-1);el('vl-zips_dir').value=same?'A #?':'B 日本';const newer=ctx.voicelabInspect();const b=requests.at(-1);b.resolve(result(2));await newer;const expected=el('vl-inspect').innerHTML;assert.match(expected,/2 narrator folders/);
 if(failure){a.reject(Error('old failure'));}else{a.resolve(result(1));}await old;assert.strictEqual(el('vl-inspect').innerHTML,expected);assert.strictEqual(a.url,'/api/voicelab/inspect?zips_dir=A%20%23%3F');
}}
''')

    def test_edit_without_new_request_suppresses_stale_result_and_explicit_run_root_is_retained(self):
        self.run_js(r'''
for(const failure of [false,true]){
 el('vl-zips_dir').value='A';const pending=ctx.voicelabInspect();const a=requests.at(-1);el('vl-zips_dir').value='B';const before=el('vl-inspect').innerHTML;if(failure){a.reject(Error('old failure'));}else{a.resolve(result(1));}await pending;assert.strictEqual(el('vl-inspect').innerHTML,before);
}
const run=ctx.voicelabInspect('/actual/run');const a=requests.at(-1);el('vl-zips_dir').value='unrelated form';a.resolve(result(3));await run;assert.match(el('vl-inspect').innerHTML,/3 narrator folders/);assert.strictEqual(a.url,'/api/voicelab/inspect?zips_dir=%2Factual%2Frun');
const current=ctx.voicelabInspect();requests.at(-1).reject(Error('<current unavailable>'));await current;assert.match(el('vl-inspect').innerHTML,/&lt;current unavailable>/);
''')
