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
for(const id of ['tts-diagnostics-status','tts-diagnostics-records','tts-diagnostics-download','tts-diagnostics-preview']){body.appendChild(new Element('div',{id}));}
const downloads=[];context.Blob=function(parts,options){downloads.push({text:parts.join(''),type:options.type});};context.URL={createObjectURL:()=> 'blob:synthetic',revokeObjectURL:()=>{}};context.setTimeout=callback=>callback();Element.prototype.click=function(){};
const requests=[];context.API={get:()=>new Promise((resolve,reject)=>requests.push({resolve,reject}))};load();
(async()=>{
const first=context.refreshTtsDiagnostics(),second=context.refreshTtsDiagnostics();
const currentReport={sections:{tts:{records:[]}}};requests[1].resolve(currentReport);await second;assert.equal(document.getElementById('tts-diagnostics-download').disabled,false);assert.match(document.getElementById('tts-diagnostics-status').textContent,/No TTS operations/);
const exact=document.getElementById('tts-diagnostics-preview').textContent;assert.equal(exact,JSON.stringify({sections:{tts:{records:[]}}},null,2));context.downloadTtsDiagnostics();assert.equal(downloads[0].text,exact);assert.equal(downloads[0].type,'application/json');
currentReport.sections.tts.records.push({operation:'mutated after preview'});context.downloadTtsDiagnostics();assert.equal(downloads[1].text,exact);
requests[0].resolve({sections:{tts:{records:[{operation:'old'}]}}});await first;assert.match(document.getElementById('tts-diagnostics-status').textContent,/No TTS operations/);
const failure=context.refreshTtsDiagnostics();assert.equal(document.getElementById('tts-diagnostics-download').disabled,true);requests[2].reject(Error('PRIVATE_PATH'));await failure;assert.match(document.getElementById('tts-diagnostics-status').textContent,/Could not read/);assert(!document.getElementById('tts-diagnostics-status').textContent.includes('PRIVATE_PATH'));
assert.equal(document.getElementById('tts-diagnostics-preview').textContent,'');context.downloadTtsDiagnostics();assert.equal(downloads.length,2);
const loaded=context.refreshTtsDiagnostics();requests[3].resolve({sections:{tts:{records:[],residency:{state:'observed',residents:[{family:'<img src=x onerror=boom>',device:'cuda:0',load_capture:null}]}}}});await loaded;
const modelRow=document.getElementById('tts-diagnostics-records').children[0];assert.match(modelRow.textContent,/Loaded local models/);assert.match(modelRow.textContent,/load identity unavailable/);assert.match(modelRow.textContent,/<img/);assert.equal(modelRow.children.length,0);
const busy=context.refreshTtsDiagnostics();requests[4].resolve({sections:{tts:{records:[],residency:{state:'busy',residents:null}}}});await busy;assert.match(document.getElementById('tts-diagnostics-records').children[0].textContent,/details are busy/);assert.equal(document.getElementById('tts-diagnostics-records').children.length,1);
const inventory=context.refreshTtsDiagnostics();requests[5].resolve({sections:{tts:{records:[],assets:[{model_id:'SyntheticBase',state:'required_assets_observed'}],device:{state:'observed',gpu_backend:'mps'}}}});await inventory;const inventoryRows=document.getElementById('tts-diagnostics-records').children;assert.equal(inventoryRows.length,2);assert.match(inventoryRows[0].textContent,/contents not verified/);assert.match(inventoryRows[1].textContent,/mps/);assert.match(inventoryRows[1].textContent,/capacity is not measured/);
const pending=context.refreshTtsDiagnostics();requests[6].resolve({sections:{tts:{records:[],residency:{state:'observed',residents:[],cleanup_pending:true}}}});await pending;assert.match(document.getElementById('tts-diagnostics-records').children[1].textContent,/cleanup is incomplete/);
const incomplete=context.refreshTtsDiagnostics();requests[7].resolve({sections:{tts:{records:[],collection:{runtime:'unavailable'},device:{state:'unavailable'}}}});await incomplete;const incompleteRows=document.getElementById('tts-diagnostics-records').children;assert.match(incompleteRows[0].textContent,/Report incomplete/);assert.match(incompleteRows[1].textContent,/probe failed/);assert.match(document.getElementById('tts-diagnostics-preview').textContent,/unavailable/);assert.equal(document.getElementById('tts-diagnostics-download').disabled,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',code,str(STATIC/'js/app-tts-diagnostics.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
