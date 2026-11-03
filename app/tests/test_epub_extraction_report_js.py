from pathlib import Path
import subprocess
import unittest

from tests.test_ui_help_js import DOM_FIXTURE


class EpubExtractionReportJsTests(unittest.TestCase):
    def test_real_upload_handler_fences_reports_and_uses_text_only(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        markup = (source.parent.parent / 'index.html').read_text(encoding='utf-8')
        for element_id in ('epub-extraction-report-enabled', 'epub-extraction-report-panel', 'epub-extraction-report-json'):
            self.assertIn(f'id="{element_id}"', markup)
        code = DOM_FIXTURE + r'''
const source=fs.readFileSync(process.argv[1],'utf8');
for(const id of ['file-upload','upload-status','epub-extraction-report-enabled','epub-extraction-report-panel','epub-extraction-report-json','existing-upload-select','cast-list-panel']){body.appendChild(new Element('div',{id}));}
const preview=document.getElementById('epub-extraction-report-json');
Object.defineProperty(preview,'innerHTML',{set(){throw Error('HTML sink');}});
context.ensureCastListEditsDiscardable=async()=>true;
context.enqueueBookSelection=async(request,action)=>action();
context.escapeHtml=value=>String(value);context.getActionErrorMessage=()=> 'Upload failed';
context.clearCastListEditor=()=>{};context.loadCastList=async()=>{};context.loadExistingScriptUploads=async()=>{};
context.window._bookSelectionEpoch=0;
const pending=[];context.API={upload:(file,options)=>new Promise((resolve,reject)=>pending.push({file,options,resolve,reject}))};
let start=source.indexOf('function renderEpubExtractionReport('),end=source.indexOf('window.renderEpubExtractionReport = renderEpubExtractionReport;',start);
vm.runInContext(source.slice(start,end)+'window.renderEpubExtractionReport = renderEpubExtractionReport;',context);
start=source.indexOf('function applyCurrentBookFilename(');end=source.indexOf('function enqueueBookSelection(',start);
vm.runInContext("let currentBookFilename='';"+source.slice(start,end),context);
start=source.indexOf("document.getElementById('file-upload').addEventListener('change', async () => {");
end=source.indexOf('// Generate resumes saved progress',start);
vm.runInContext(source.slice(start,end),context);
const file=document.getElementById('file-upload'),enabled=document.getElementById('epub-extraction-report-enabled'),panel=document.getElementById('epub-extraction-report-panel');
const listener=file.listeners.change[0];
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
enabled.checked=true;file.files=[{name:'first.epub'}];const first=listener();await settle();
assert.equal(pending[0].options.includeExtractionReport,true);
const report={source_sha256:'a'.repeat(64),extracted_text_sha256:'b'.repeat(64),rows:[{label:'<img src=x onerror=boom>'}]};
pending[0].resolve({stored_filename:'first.txt',extraction_report:report});await first;
assert.equal(panel.hidden,false);assert.deepEqual(JSON.parse(preview.textContent),report);assert.equal(panel.open,false);
context.ensureCastListEditsDiscardable=async()=>false;file.files=[{name:'declined.epub'}];await listener();
assert.equal(panel.hidden,false);assert.deepEqual(JSON.parse(preview.textContent),report);assert.equal(pending.length,1);
context.ensureCastListEditsDiscardable=async()=>true;
context.applyCurrentBookFilename('other.json');assert.equal(panel.hidden,true);assert.equal(preview.textContent,'');
file.files=[{name:'old.epub'}];const old=listener();await settle();
file.files=[{name:'new.epub'}];const newer=listener();await settle();
pending[2].resolve({stored_filename:'new.txt',extraction_report:{source_sha256:'new'}});await newer;
pending[1].resolve({stored_filename:'old.txt',extraction_report:{source_sha256:'old'}});await old;
assert.equal(JSON.parse(preview.textContent).source_sha256,'new');
file.files=[{name:'bad.epub'}];const bad=listener();await settle();assert.equal(panel.hidden,true);
pending[3].reject(Error('failure'));await bad;assert.equal(preview.textContent,'');
file.files=[{name:'plain.txt'}];const plain=listener();await settle();assert.equal(pending[4].options.includeExtractionReport,false);
pending[4].resolve({stored_filename:'plain.txt'});await plain;assert.equal(panel.hidden,true);
file.files=[{name:'off.epub'}];enabled.checked=false;const off=listener();await settle();assert.equal(pending[5].options.includeExtractionReport,false);
pending[5].resolve({stored_filename:'off.txt'});await off;assert.equal(panel.hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_api_report_flag_is_explicit_and_does_not_change_batch_default(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),requests=[];
const context={FormData:class{append(){}},fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({})};}};
vm.createContext(context);vm.runInContext(source.slice(source.indexOf('const API = {'),source.indexOf('function getTaskLogUpdate('))+'globalThis.API=API;',context);
(async()=>{await context.API.upload({});await context.API.upload({},{selectActive:false});await context.API.upload({},{includeExtractionReport:true});
assert.equal(requests[0].url,'/api/upload?select_active=true');assert.equal(requests[1].url,'/api/upload?select_active=false');
assert.equal(requests[2].url,'/api/upload?select_active=true&include_extraction_report=true');
assert(requests.every(row=>row.options.method==='POST'));})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
