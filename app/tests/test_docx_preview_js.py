from pathlib import Path
import subprocess
import unittest

from tests.test_ui_help_js import DOM_FIXTURE


class DocxPreviewJsTests(unittest.TestCase):
    def test_exact_preview_accept_cancel_and_stale_responses(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        code = DOM_FIXTURE + r'''
const source=fs.readFileSync(process.argv[1],'utf8');
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
for(const id of ['file-upload','upload-status','docx-preview-panel','docx-preview-policy','docx-preview-warnings','docx-preview-text','docx-preview-accept','docx-preview-cancel','existing-upload-select','cast-list-panel']){body.appendChild(new Element('div',{id}));}
for(const id of ['docx-preview-text','docx-preview-policy','docx-preview-warnings']){
Object.defineProperty(document.getElementById(id),'innerHTML',{set(){throw Error('HTML sink');}});
}
context.FormData=class{constructor(){this.entries=[];}append(key,value){this.entries.push([key,value]);}};
context.encodeURIComponent=encodeURIComponent;
const pending=[],selections=[];
context.fetch=(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));
context.API={post:async(url,body)=>{selections.push({url,body});return {stored_filename:body.filename,book_identity_token:'a'.repeat(64)};}};
vm.runInContext(source.slice(source.indexOf('_handleError: async (res) => {'),source.indexOf('get: async (url) => {')).replace('_handleError:', 'API._handleError =').replace(/,\s*$/, ';'),context);
let discardAllowed=true;
context.ensureCastListEditsDiscardable=async()=>discardAllowed;
context.clearCastListEditor=()=>{};context.loadCastList=async()=>{};context.loadExistingScriptUploads=async()=>{};
context.window._bookSelectionEpoch=0;
vm.runInContext("let currentBookFilename='prior.txt';"+source.slice(source.indexOf('function applyCurrentBookFilename('),source.indexOf('function getCurrentBookName(')),context);
vm.runInContext(source.slice(source.indexOf('let docxPreviewCandidate = null;'),source.indexOf("document.getElementById('file-upload').addEventListener('change', async () => {")),context);
const file=document.getElementById('file-upload'),panel=document.getElementById('docx-preview-panel');
const result={policy_text:'Exact policy',warnings:['Specific warning'],text:' “Hello” 001 é <img onerror=boom>\n\nLast.',preview_digest:'b'.repeat(64)};
const resolve=(n,value)=>pending[n].resolve({ok:true,json:async()=>value});
function start(name){file.files=[{name}];context.window._bookSelectionEpoch++;const request={};context.window._existingUploadSelectionRequest=request;return context.window.previewDocxUpload(file.files[0],request);}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
let work=start('first.docx');assert.equal(pending[0].url,'/api/upload?select_active=false&docx_preview=true');resolve(0,result);await work;
assert.equal(panel.hidden,false);assert.equal(document.getElementById('docx-preview-text').textContent,result.text);
assert.equal(document.getElementById('docx-preview-policy').textContent,result.policy_text);
assert.equal(document.getElementById('docx-preview-warnings').textContent,'Specific warning');assert.equal(selections.length,0);
context.window.cancelDocxPreview();assert.equal(panel.hidden,true);await context.window.acceptDocxPreview();assert.equal(pending.length,1);
work=start('accepted.docx');const exactFile=file.files[0];resolve(1,result);await work;
const accepting=context.window.acceptDocxPreview();await context.window.acceptDocxPreview();assert.equal(pending.length,3);
assert.strictEqual(pending[2].options.body.entries[0][1],exactFile);assert(pending[2].url.endsWith('&docx_confirmed=true&docx_preview_digest='+result.preview_digest));
resolve(2,{stored_filename:'accepted.txt',preview_digest:result.preview_digest});await accepting;assert.equal(selections.length,1);assert.equal(selections[0].url,'/api/uploads/select');assert.equal(panel.hidden,true);
work=start('old.docx');const newer=start('new.docx');resolve(4,{...result,text:'new'});await newer;resolve(3,{...result,text:'old'});await work;
assert.equal(document.getElementById('docx-preview-text').textContent,'new');
context.applyCurrentBookFilename('other.txt');await context.window.acceptDocxPreview();assert.equal(pending.length,5);assert.equal(panel.hidden,true);
work=start('stale-accept.docx');resolve(5,result);await work;const stale=context.window.acceptDocxPreview();context.window.cancelDocxPreview();resolve(6,{stored_filename:'stale.txt'});await stale;assert.equal(selections.length,1);
work=start('changed-file.docx');resolve(7,result);await work;file.files=[{name:'changed.docx'}];await context.window.acceptDocxPreview();assert.equal(pending.length,8);
work=start('bad.docx');file.value='bad.docx';pending[8].resolve({ok:false,json:async()=>({detail:'Unsupported table'})});await work;assert.equal(panel.hidden,true);assert.equal(document.getElementById('upload-status').textContent,'Unsupported table');assert.equal(file.value,'');
work=start('cancel-inflight.docx');context.window.cancelDocxPreview();resolve(9,result);await work;assert.equal(panel.hidden,true);assert.equal(selections.length,1);
work=start('epoch.docx');resolve(10,result);await work;context.window._bookSelectionEpoch++;await context.window.acceptDocxPreview();assert.equal(pending.length,11);
work=start('wrong-receipt.docx');resolve(11,result);await work;const wrong=context.window.acceptDocxPreview();resolve(12,{stored_filename:'wrong.txt',preview_digest:'c'.repeat(64)});await wrong;assert.equal(selections.length,1);assert.equal(panel.hidden,true);
work=start('malformed-response.docx');resolve(13,{text:'plausible'});await work;assert.equal(panel.hidden,true);await context.window.acceptDocxPreview();assert.equal(pending.length,14);
let finishSelection;context.API.post=()=>new Promise(resolve=>{finishSelection=resolve;});
work=start('committing.docx');resolve(14,result);await work;const committing=context.window.acceptDocxPreview();resolve(15,{stored_filename:'committing.txt',preview_digest:result.preview_digest});await settle();
assert.equal(document.getElementById('docx-preview-cancel').disabled,true,document.getElementById('upload-status').textContent);context.window.cancelDocxPreview();assert.equal(panel.hidden,false);
finishSelection({stored_filename:'committing.txt'});await committing;assert.equal(panel.hidden,true);assert.equal(document.getElementById('docx-preview-cancel').disabled,false);
work=start('unsaved.docx');resolve(16,result);await work;discardAllowed=false;
context.API.post=async()=>{throw Error('selection must not run');};
const declined=context.window.acceptDocxPreview();resolve(17,{stored_filename:'unsaved.txt',preview_digest:result.preview_digest});await declined;assert.equal(panel.hidden,false);assert.equal(document.getElementById('docx-preview-accept').disabled,false);
context.window.cancelDocxPreview();discardAllowed=true;
work=start('proxy.docx');pending[18].resolve({ok:false,status:502,statusText:'Bad Gateway',json:async()=>{throw SyntaxError('HTML');}});await work;assert.equal(document.getElementById('upload-status').textContent,'Bad Gateway');
vm.runInContext(source.slice(source.indexOf("document.getElementById('file-upload').addEventListener('change', async () => {"),source.indexOf('let _scriptStartOver = false;')),context);
context.API.upload=async()=>{throw Error('DOCX must not use immediate upload selection');};
file.files=[{name:'dispatch.docx'}];
const dispatch=file.listeners.change[0]();await settle();
assert.equal(pending.length,20);assert(pending[19].url.endsWith('docx_preview=true'));
resolve(19,result);await dispatch;assert.equal(panel.hidden,false);
assert.equal(vm.runInContext('currentBookFilename',context),'committing.txt');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
