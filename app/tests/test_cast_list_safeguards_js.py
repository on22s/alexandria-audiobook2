"""Exercise native cast deletion decisions and ownership with known rejects."""
from pathlib import Path
import os
import subprocess
import unittest

SOURCE = Path(os.environ.get('CAST_UI_SOURCE', Path(__file__).resolve().parent.parent / "static/js/app-core.js"))

class CastListSafeguardsJsTests(unittest.TestCase):
    def test_delete_cancel_book_switch_and_confirm(self):
        script = r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('async function deleteCastList('),end=source.indexOf('let characterAliasesLoaded',start);
assert(start>=0&&end>start);
let deletes=0,refreshes=0;const panel={style:{display:'block'}};
const context={currentBookFilename:'A.txt',API:{del:async()=>deletes++},document:{getElementById:()=>panel},
 loadCastList:async()=>refreshes++,showToast(){},showConfirm:async()=>false};vm.createContext(context);vm.runInContext(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')),context);
const helpers=source.indexOf('let castListLoaded =');vm.runInContext(source.slice(helpers,source.indexOf('async function buildCastList()',helpers)),context);
vm.runInContext(source.slice(start,end),context);
(async()=>{
 await context.deleteCastList();assert.strictEqual(deletes,0);assert.strictEqual(refreshes,0);assert.strictEqual(panel.style.display,'block');
 let resolve;context.showConfirm=()=>new Promise(done=>resolve=done);const pending=context.deleteCastList();assert.strictEqual(panel.disabled,true);assert.strictEqual(await context.ensureCastListEditsDiscardable(),false);await context.deleteCastList();assert.strictEqual(deletes,0);context.currentBookFilename='B.txt';resolve(true);await pending;assert.strictEqual(panel.disabled,false);
 assert.strictEqual(deletes,0);assert.strictEqual(refreshes,0);
 context.showConfirm=async()=>true;await context.deleteCastList();assert.strictEqual(deletes,1);assert.strictEqual(refreshes,1);assert.strictEqual(panel.style.display,'none');
})().catch(error=>{console.error(error);process.exitCode=1;});'''
        result=subprocess.run(['node','-e',script,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_dirty_snapshot_detects_edits_added_removed_rows_and_refuses_changed_confirmation(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const a=source.indexOf('let castListLoaded ='),b=source.indexOf('async function buildCastList()',a);assert(a>=0&&b>a);
let rows=[['ALICE','A']],answers=0;const panel={style:{display:'block'},innerHTML:'editor'};
const c={currentBookFilename:'A',document:{getElementById:()=>panel,querySelectorAll:()=>rows.map(v=>({querySelector:s=>({value:v[s==='.cast-list-name'?0:1]})}))},showConfirm:async()=>{answers++;return false;},showToast(){}};
vm.createContext(c);vm.runInContext(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')),c);vm.runInContext(source.slice(a,b),c);const run=s=>vm.runInContext(s,c);
(async()=>{
 run('castListEditorSnapshot=getCastListEditorSnapshot();');assert(await c.ensureCastListEditsDiscardable());assert.strictEqual(answers,0);
 for(const changed of [[['ALICE','changed']],[['ALICE','A'],['BOB','']],[]]){rows=changed;assert.strictEqual(await c.ensureCastListEditsDiscardable(),false);}
 rows=[['ALICE','changed']];let resolve;c.showConfirm=()=>new Promise(done=>resolve=done);
 const pending=c.ensureCastListEditsDiscardable();rows[0][0]='edited again';resolve(true);assert.strictEqual(await pending,false);
 c.showConfirm=async()=>true;assert.strictEqual(await c.ensureCastListEditsDiscardable(),true);
 c.clearCastListEditor();assert.strictEqual(panel.innerHTML,'');assert.strictEqual(run('castListEditorSnapshot'),null);
})().catch(e=>{console.error(e);process.exitCode=1;});'''
        result=subprocess.run(['node','-e',script,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_actual_book_loaders_refuse_dirty_cast_and_refresh_after_accept(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');
const core=fs.readFileSync(process.argv[1],'utf8'),scripts=fs.readFileSync(process.argv[2],'utf8');
function client(){
 let rows=[['ALICE','A']],accept=false,requests=[],handlers={},confirms=[];
 const elements={};const el=id=>elements[id]||(elements[id]={value:'',style:{display:'block'},innerHTML:'old editor',files:[],addEventListener:(event,fn)=>handlers[id+event]=fn});
 const c={window:null,currentBookFilename:'A.txt',console:{error(){}},escapeHtml:String,
  document:{getElementById:el,querySelectorAll:()=>rows.map(v=>({querySelector:s=>({value:v[s==='.cast-list-name'?0:1]})}))},
  API:{get:async url=>{requests.push(url);assert.strictEqual(url,'/api/cast_list');return{cast:[{name:'BOB'}],count:1};},post:async url=>{requests.push(url);return{stored_filename:'B.txt',name:'B'};},upload:async()=>{requests.push('upload');return{stored_filename:'B.txt'};}},
  applyCurrentBookFilename:name=>c.currentBookFilename=name,showToast(){},showConfirm:async text=>{confirms.push(text);return !text.startsWith('Discard')||accept;},flushVoiceSaves:async()=>{},loadExistingScriptUploads:async()=>{},clearCharacterAliases(){},resetDesignerForm(){},clearVoiceSuggestions(){},loadCharacterAliases:async()=>{},loadChunks:async()=>{},loadVoices:async()=>{},loadSavedScripts(){},loadDesignedVoices(){}};c.window=c;vm.createContext(c);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),c);
 function load(text,start,end){const a=text.indexOf(start),b=text.indexOf(end,a);assert(a>=0&&b>a);vm.runInContext(text.slice(a,b),c);}
 load(core,'let castListLoaded =','async function buildCastList()');load(core,'function renderCastListStatus(','let characterAliasesLoaded');
 load(core,'window.selectExistingScriptUpload =','// Generate resumes saved progress');load(scripts,'async function loadScript(name)','async function deleteScript(name)');
 vm.runInContext('castListEditorSnapshot=getCastListEditorSnapshot();',c);rows[0][0]='EDITED';el('existing-upload-select').value='B.txt';el('file-upload').files=[{}];
 return{c,el,requests,confirms,handlers,accept:()=>accept=true};
}
let finished=false;process.on('beforeExit',()=>assert(finished,'all loader assertions must finish'));
(async()=>{
 for(const mode of ['reuse','upload','saved']){
  const x=client();const load=()=>mode==='reuse'?x.c.selectExistingScriptUpload():mode==='upload'?x.handlers['file-uploadchange']():x.c.loadScript('B');
  await load();assert.strictEqual(x.requests.length,0,mode+' cancel must not send a book change');assert.strictEqual(x.c.currentBookFilename,'A.txt');assert.strictEqual(x.el('cast-list-panel').innerHTML,'old editor');
  assert(x.confirms.includes('Discard unsaved cast-list changes?'));
  x.accept();await load();assert.strictEqual(x.c.currentBookFilename,mode==='saved'?'B.json':'B.txt');
  assert.strictEqual(x.el('cast-list-panel').innerHTML,'');assert.strictEqual(x.el('cast-list-panel').style.display,'none');assert(x.requests.includes('/api/cast_list'));assert(x.el('cast-list-status').innerHTML.includes('1 people'));
 }
 finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});'''
        result=subprocess.run(['node','-e',script,str(SOURCE),str(SOURCE.with_name('app-scripts.js'))],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_pending_failed_save_keeps_edits_blocks_duplicates_and_book_switch(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');const source=fs.readFileSync(process.argv[1],'utf8');
let reject,posts=0;const fields={disabled:false},status={textContent:''},panel={style:{display:'block'},innerHTML:'edited rows'};
const c={window:{},currentBookFilename:'A',document:{getElementById:id=>id==='cast-list-fields'?fields:id==='cast-list-editor-status'?status:panel,querySelectorAll:()=>[{querySelector:s=>({value:s==='.cast-list-name'?'EDITED':'alias'})}]},API:{post:()=>{posts++;return new Promise((_,no)=>reject=no);}},showToast(){},loadCastList:async()=>{throw Error('failed save must not reload');}};
vm.createContext(c);vm.runInContext(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')),c);const a=source.indexOf('let castListLoaded =');vm.runInContext(source.slice(a,source.indexOf('async function buildCastList()',a)),c);const b=source.indexOf('async function saveCastList()');vm.runInContext(source.slice(b,source.indexOf('let characterAliasesLoaded',b)),c);vm.runInContext('castListLoaded=true;',c);
let finished=false;process.on('beforeExit',()=>assert(finished,'save assertions must finish'));
(async()=>{const save=c.saveCastList();assert.strictEqual(fields.disabled,true);assert.strictEqual(status.textContent,'Saving cast list…');await c.saveCastList();assert.strictEqual(posts,1);assert.strictEqual(await c.ensureCastListEditsDiscardable(),false);reject(Error('offline'));await save;assert.strictEqual(fields.disabled,false);assert.strictEqual(panel.innerHTML,'edited rows');finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'''
        result=subprocess.run(['node','-e',script,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_editor_refresh_keeps_dirty_pending_and_inflight_edits(self):
        script=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
let rows=[['ALICE','A']],gets=0,posts=0,resolveGet,resolvePost;
const panel={style:{display:'block'},innerHTML:'original editor'},fields={disabled:false},status={textContent:''};
const c={window:{},currentBookFilename:'A',escapeHtml:String,showToast(){},
 document:{getElementById:id=>id==='cast-list-panel'?panel:id==='cast-list-fields'?fields:status,
 querySelectorAll:()=>rows.map(v=>({querySelector:s=>({value:v[s==='.cast-list-name'?0:1]})}))},
 API:{get:()=>{gets++;return new Promise(yes=>resolveGet=yes);},post:()=>{posts++;return new Promise(yes=>resolvePost=yes);}}};
vm.createContext(c);vm.runInContext(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')),c);const a=source.indexOf('let castListLoaded =');vm.runInContext(source.slice(a,source.indexOf('async function buildCastList()',a)),c);
const b=source.indexOf('function renderCastListStatus(');vm.runInContext(source.slice(b,source.indexOf('let characterAliasesLoaded',b)),c);
const run=s=>vm.runInContext(s,c);run('castListLoaded=true;castListEditorSnapshot=getCastListEditorSnapshot();');
let finished=false;process.on('beforeExit',()=>assert(finished,'all refresh assertions must finish'));
(async()=>{
 rows[0][0]='EDITED';await c.loadCastList(true);assert.strictEqual(gets,0);assert.strictEqual(panel.innerHTML,'original editor');
 const saving=c.saveCastList();assert.strictEqual(posts,1);await c.loadCastList(true);assert.strictEqual(gets,0);assert.strictEqual(fields.disabled,true);
 resolvePost({count:1});await saving;assert.strictEqual(gets,0,'acknowledged save must not replace editor with another fetch');assert.strictEqual(panel.innerHTML,'original editor');assert.strictEqual(fields.disabled,false);assert.strictEqual(run('castListEditorSnapshot'),c.getCastListEditorSnapshot());
 const loading=c.loadCastList(true);assert.strictEqual(gets,1);rows[0][1]='new alias';resolveGet({cast:[{name:'SERVER'}],count:1});await loading;assert.strictEqual(panel.innerHTML,'original editor');assert.notStrictEqual(run('castListEditorSnapshot'),c.getCastListEditorSnapshot());
 run('castListEditorSnapshot=getCastListEditorSnapshot();');const oldBook=c.loadCastList(true);c.currentBookFilename='B';resolveGet({cast:[{name:'OLD BOOK'}],count:1});await oldBook;assert.strictEqual(panel.innerHTML,'original editor');
 c.currentBookFilename='A';const normal=c.loadCastList(true);resolveGet({cast:[{name:'SERVER'}],count:1});await normal;assert(panel.innerHTML.includes('SERVER'),'unchanged current-book refresh still renders');finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});'''
        result=subprocess.run(['node','-e',script,str(SOURCE)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_failed_fetch_is_unknown_and_keeps_existing_editor_instead_of_claiming_absence(self):
        script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
let html='',reply={cast:[{name:'Alice'}],count:1},failure=null;const status={};Object.defineProperty(status,'innerHTML',{get:()=>html,set:value=>html=value});Object.defineProperty(status,'textContent',{get:()=>html.replace(/<[^>]*>/g,''),set:value=>html=value});
const panel={innerHTML:'Existing cast editor rows',style:{display:'block'}},toasts=[];
const c={currentBookFilename:'loaded-book.txt',castListRequest:0,castListLoaded:true,castListMutationPending:false,castListEditorSnapshot:null,getCastListEditorSnapshot:()=> 'unchanged',escapeHtml:String,
 document:{getElementById:id=>id==='cast-list-panel'?panel:status},API:{get:async()=>{if(failure){throw failure;}return reply;}},showToast:(...args)=>toasts.push(args)};
c.window=c;vm.createContext(c);let a=source.indexOf('function showActionError(');vm.runInContext(source.slice(a,source.indexOf('function showConfirm(',a)),c);
a=source.indexOf('function renderCastListStatus(');vm.runInContext(source.slice(a,source.indexOf('async function saveCastList(',a)),c);
(async()=>{
await c.loadCastList(false);assert(status.textContent.includes('1 people'));
failure=Error('fixture unavailable');await c.loadCastList(true);assert(!status.textContent.includes('No cast list'));assert(!toasts.some(row=>row[0].includes('Select a book first')));assert(status.textContent.includes('not confirmed'));assert.strictEqual(panel.innerHTML,'Existing cast editor rows');assert.strictEqual(c.castListLoaded,false);
failure=null;reply={cast:null,count:0};await c.loadCastList(false);assert(status.textContent.includes('No cast list'));assert.strictEqual(c.castListLoaded,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
