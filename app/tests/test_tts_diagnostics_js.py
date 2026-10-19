from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE
STATIC=Path(__file__).resolve().parent.parent/'static'

class TtsDiagnosticsJsTests(unittest.TestCase):
    def test_out_of_order_reads_and_failure_invalidate_the_download(self):
        code=DOM_FIXTURE+r'''
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Element.prototype.replaceChildren=function(){this.children=[];};
for(const id of ['tts-diagnostics-status','tts-diagnostics-records','tts-diagnostics-download']){body.appendChild(new Element('div',{id}));}
const requests=[];context.API={get:()=>new Promise((resolve,reject)=>requests.push({resolve,reject}))};load();
(async()=>{
const first=context.refreshTtsDiagnostics(),second=context.refreshTtsDiagnostics();
requests[1].resolve({sections:{tts:{records:[]}}});await second;assert.equal(document.getElementById('tts-diagnostics-download').disabled,false);assert.match(document.getElementById('tts-diagnostics-status').textContent,/No TTS operations/);
requests[0].resolve({sections:{tts:{records:[{operation:'old'}]}}});await first;assert.match(document.getElementById('tts-diagnostics-status').textContent,/No TTS operations/);
const failure=context.refreshTtsDiagnostics();assert.equal(document.getElementById('tts-diagnostics-download').disabled,true);requests[2].reject(Error('PRIVATE_PATH'));await failure;assert.match(document.getElementById('tts-diagnostics-status').textContent,/Could not read/);assert(!document.getElementById('tts-diagnostics-status').textContent.includes('PRIVATE_PATH'));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',code,str(STATIC/'js/app-tts-diagnostics.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
