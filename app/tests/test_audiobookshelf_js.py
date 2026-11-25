"""Audiobookshelf panel handlers: reset, preview gating, key clearing (#1727)."""
from pathlib import Path
import subprocess
import unittest

# A fresh panel context per case: the module's saved-config state is per page,
# so every restoring test needs its own copy of the real script. The <select>
# model matches Chrome: replaceChildren clears the value and the first appended
# option is selected, and assigning an id that is not listed deselects.
_HARNESS = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(process.argv[1],'utf8');
const panelHtml=fs.readFileSync(path.join(path.dirname(process.argv[1]),'..','index.html'),'utf8');
const SELECTS=new Set(['abs-library','abs-folder','abs-export-format']);
function inlineHandler(id,event){
  const tag=(panelHtml.match(new RegExp('<[^>]*\\bid="'+id+'"[^>]*>'))||[])[0]||'';
  const found=tag.match(new RegExp('\\b'+event+'="([^"]*)"'));
  return found?found[1]:null;
}
function makeContext(){
  const calls=[];
  const elements={};
  const plain=()=>({style:{},textContent:'',value:'',checked:false,disabled:false,children:[],
    replaceChildren(){this.children=[];this.value='';},appendChild(child){this.children.push(child);}});
  const select=()=>{
    const element=plain();
    let selectedIndex=-1;
    Object.defineProperty(element,'value',{
      get(){return selectedIndex<0?'':String(element.children[selectedIndex].value);},
      set(next){const text=next===null||next===undefined?'':String(next);
        selectedIndex=element.children.findIndex(child=>String(child.value)===text);},
    });
    element.replaceChildren=function(){this.children=[];selectedIndex=-1;};
    element.appendChild=function(child){this.children.push(child);if(selectedIndex<0){selectedIndex=0;}};
    return element;
  };
  const mk=id=>SELECTS.has(id)?select():plain();
  const el=id=>elements[id]||(elements[id]=mk(id));
  const ctx={document:{getElementById:el,createElement:()=>plain()},console:{error(){}},
    chunkSnapshotBookToken:'a'.repeat(64),showActionError(){},setInterval:()=>0,clearInterval:()=>{},
    API:{get:null,post:async(url)=>{throw new Error('unexpected POST '+url);}}};
  vm.createContext(ctx);
  Object.assign(ctx,{t:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),formatUiSafetyText:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),tSafety:()=>({primary:'',secondary:null}),renderUiSafety:(target,k,e,v={})=>{target.textContent=String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m);},getIntlUiLocale:()=> 'en'});
  vm.runInContext(source,ctx);
  function type(id,next){
    el(id).value=next;
    const handler=inlineHandler(id,'oninput');
    if(handler){vm.runInContext(handler,ctx);}
  }
  function change(id,next){
    el(id).value=next;
    const handler=inlineHandler(id,'onchange');
    if(handler){vm.runInContext(handler,ctx);}
  }
  return {calls,el,ctx,type,change};
}
'''


class AudiobookshelfJsTests(unittest.TestCase):
    def _run(self, script):
        source = Path(__file__).parent.parent / 'static/js/app-audiobookshelf.js'
        result = subprocess.run(['node', '-e', script, str(source)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode,
                         msg=f'node failed: {result.stderr}\n{result.stdout}')

    def test_reset_and_preview_gate(self):
        self._run(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const elements={};
const el=id=>elements[id]||(elements[id]={style:{},textContent:'',value:'',checked:false,disabled:false,
  replaceChildren(){this.children=[];},appendChild(child){(this.children=this.children||[]).push(child);},
  children:[]});
const calls=[];
const ctx={document:{getElementById:el},console:{error(){}},chunkSnapshotBookToken:'a'.repeat(64),
  showActionError(){},setInterval:(fn,ms)=>0,clearInterval:()=>{},API:{get:async url=>{calls.push(['GET',url]);return{deliveries:[]};},
    post:async (url,body)=>{calls.push(['POST',url,body]);
      if(url==='/api/abs/preview'){return{preview_id:'p1',book_token:'a'.repeat(64),export_file:'cloned_audiobook.mp3',
        base_url:'http://abs.test:13378',library_id:'lib_1',folder_id:'fol_1',title:'Test Book',author:'Test Author',series:'',
        sha256:'s',output_bytes:42,input_revision:'r'};}
      throw new Error('unexpected '+url);}}};
vm.createContext(ctx);
Object.assign(ctx,{t:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),formatUiSafetyText:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),tSafety:()=>({primary:'',secondary:null}),renderUiSafety:(target,k,e,v={})=>{target.textContent=String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m);},getIntlUiLocale:()=> 'en'});
vm.runInContext(source,ctx);
(async()=>{
el('abs-library').value='lib_1';el('abs-folder').value='fol_1';el('abs-title').value='Test Book';
await ctx.previewAbsSend();
assert.ok(el('abs-preview-out').textContent.startsWith('Server: http://abs.test:13378'),'preview shows server');
assert.ok(el('abs-preview-out').textContent.includes('Destination: library lib_1'),'exact preview destination');
assert.ok(el('abs-preview-out').textContent.includes('SHA-256: s'),'preview shows SHA');
assert.strictEqual(el('abs-send-btn').disabled,true,'send disabled without right-to-share');
const html=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'..','index.html'),'utf8');
const checkbox=html.match(/<input\b[^>]*id="abs-right-to-share"[^>]*>/)[0];
const changeHandler=checkbox.match(/onchange="([^"]+)"/)[1];
el('abs-right-to-share').checked=true;vm.runInContext(changeHandler,ctx);
assert.strictEqual(el('abs-send-btn').disabled,false,'send enabled after confirmation');
ctx.resetAbsPreview();
assert.strictEqual(el('abs-send-btn').disabled,true,'any change invalidates the preview');
assert.strictEqual(el('abs-preview-out').textContent,'');
assert.strictEqual(el('abs-right-to-share').checked,false,'identity change requires rights confirmation again');
assert.match(el('abs-status').textContent,/Preview cleared/);
assert.strictEqual(calls.length,1,'preview is the only network call and it carries no upload');
assert.strictEqual(calls[0][0],'POST');assert.strictEqual(calls[0][1],'/api/abs/preview');
assert.strictEqual(calls[0][2].title,'Test Book');
let resolvePreview;
ctx.API.post=()=>new Promise(resolve=>{resolvePreview=resolve;});
const pending=ctx.previewAbsSend();
ctx.resetAbsPreview();
resolvePreview({preview_id:'stale',output_bytes:42});await pending;
el('abs-right-to-share').checked=true;vm.runInContext(changeHandler,ctx);
assert.strictEqual(el('abs-send-btn').disabled,true,'stale preview response cannot enable sending after reset');
assert.strictEqual(el('abs-preview-out').textContent,'','stale response must not restore the preview');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_config_clears_key_field(self):
        self._run(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const elements={};
const el=id=>elements[id]||(elements[id]={style:{},textContent:'',value:'',checked:false,disabled:false,
  replaceChildren(){this.children=[];},appendChild(child){(this.children=this.children||[]).push(child);},
  children:[]});
el('abs-server-url').value='http://abs.test:13378';
el('abs-api-key').value='the-secret-key';
el('abs-library').value='lib_1';el('abs-folder').value='fol_1';
let posted=null;
const ctx={document:{getElementById:el},console:{error(){}},chunkSnapshotBookToken:'a'.repeat(64),
  showActionError(){},API:{get:async()=>{throw new Error('unexpected GET');},
    post:async (url,body)=>{posted={url,body};return{base_url:'http://abs.test:13378',has_key:true,key_store:'OS credential store'};}}};
vm.createContext(ctx);
Object.assign(ctx,{t:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),formatUiSafetyText:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),tSafety:()=>({primary:'',secondary:null}),renderUiSafety:(target,k,e,v={})=>{target.textContent=String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m);},getIntlUiLocale:()=> 'en'});
vm.runInContext(source,ctx);
(async()=>{
await ctx.saveAbsConfig();
assert.strictEqual(posted.url,'/api/abs/config');
assert.strictEqual(posted.body.api_key,'the-secret-key');
assert.strictEqual(el('abs-api-key').value,'','key cleared from the field after save');
assert.match(el('abs-status').textContent,/stored/);
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_libraries_fill_selects(self):        self._run(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const elements={};
const mk=()=>({style:{},textContent:'',value:'',checked:false,disabled:false,children:[],
  replaceChildren(){this.children=[];},appendChild(child){this.children.push(child);}});
const el=id=>elements[id]||(elements[id]=mk());
el('abs-server-url').value='http://abs.test:13378';
let seenUrl=null;
const ctx={document:{getElementById:el,createElement:()=>mk()},console:{error(){}},chunkSnapshotBookToken:'a'.repeat(64),
  showActionError(){},API:{get:async url=>{seenUrl=url;return{libraries:[
    {id:'lib_1',name:'Main',folders:[{id:'fol_1',fullPath:'/books'},{id:'fol_2',fullPath:'/more'}]},
    {id:'lib_2',name:'Pods',folders:[{id:'fol_3',fullPath:'/pods'}]}]};},
    post:async()=>{throw new Error('unexpected POST');}}};
vm.createContext(ctx);
Object.assign(ctx,{t:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),formatUiSafetyText:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),tSafety:()=>({primary:'',secondary:null}),renderUiSafety:(target,k,e,v={})=>{target.textContent=String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m);},getIntlUiLocale:()=> 'en'});
vm.runInContext(source,ctx);
(async()=>{
await ctx.loadAbsLibraries();
assert.ok(seenUrl.startsWith('/api/abs/libraries?base_url=http%3A%2F%2Fabs.test%3A13378'));
assert.strictEqual(el('abs-library').children.length,2);
el('abs-library').value='lib_1';ctx.fillAbsFolders();
assert.strictEqual(el('abs-folder').children.length,2);
el('abs-library').value='lib_2';ctx.fillAbsFolders();
assert.strictEqual(el('abs-folder').children.length,1);
assert.match(el('abs-status').textContent,/Loaded 2 libraries/);
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_send_cancel_progress_survives_reset(self):
        self._run(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const elements={};
const el=id=>elements[id]||(elements[id]={style:{},textContent:'',value:'',checked:false,disabled:false,
  replaceChildren(){this.children=[];},appendChild(child){(this.children=this.children||[]).push(child);},
  children:[]});
el('abs-server-url').value='http://abs.test:13378';
el('abs-library').value='lib_1';el('abs-folder').value='fol_1';el('abs-title').value='Test Book';
el('abs-export-format').value='cloned_audiobook.mp3';
let resolveSend=null;
let sentBody=null;
let cancelBody=null;
let pollFn=null;
let timerCleared=false;
const ctx={document:{getElementById:el,createElement:()=>({style:{},textContent:'',className:'',children:[],appendChild(c){this.children.push(c);}})},
  console:{error(){}},chunkSnapshotBookToken:'a'.repeat(64),showActionError(){},
  setInterval:(fn,ms)=>{pollFn=fn;return 1;},clearInterval:()=>{timerCleared=true;},
  API:{get:async url=>{
    if(url.startsWith('/api/abs/progress')){return{progress:{sent:50,total:100}};}
    if(url.startsWith('/api/abs/deliveries')){return{deliveries:[]};}
    throw new Error('unexpected GET '+url);},
    post:async (url,body)=>{
    if(url==='/api/abs/preview'){return{preview_id:'p1',book_token:'a'.repeat(64),export_file:'cloned_audiobook.mp3',
      base_url:'http://abs.test:13378',library_id:'lib_1',folder_id:'fol_1',title:'Test Book',author:'',series:'',
      sha256:'abc',output_bytes:100,input_revision:'r'};}
    if(url==='/api/abs/send'){sentBody=body;return new Promise(res=>{resolveSend=()=>res(
      {status:'unknown',verification_pending:true,upload_accepted_at:123,verification_note:'Upload accepted by the server (HTTP 200); delivery is not yet verified.',
       export_file:'cloned_audiobook.mp3',title:'Test Book',output_bytes:100,sha256:'abc'});});}
    if(url==='/api/abs/cancel'){cancelBody=body;return{preview_id:body.preview_id,cancelling:true};}
    throw new Error('unexpected POST '+url);}}};
vm.createContext(ctx);
Object.assign(ctx,{t:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),formatUiSafetyText:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),tSafety:()=>({primary:'',secondary:null}),renderUiSafety:(target,k,e,v={})=>{target.textContent=String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m);},getIntlUiLocale:()=> 'en'});
vm.runInContext(source,ctx);
(async()=>{
await ctx.previewAbsSend();
el('abs-right-to-share').checked=true;ctx.updateAbsSendEnabled();
const pending=ctx.sendAbsExport();
if(pollFn){await pollFn();}
assert.match(el('abs-progress').textContent,/50 \/ 100/,'progress polls and renders during send');
ctx.resetAbsPreview();
await ctx.cancelAbsSend();
assert.ok(cancelBody&&cancelBody.preview_id==='p1','cancel uses the original preview id after reset');
assert.strictEqual(sentBody.acknowledge_unknown,false,'acknowledgement never overrides unknown');
assert.strictEqual(sentBody.right_to_share,true);
assert.strictEqual(sentBody.base_url,'http://abs.test:13378','exact preview server sent');
resolveSend();await pending;
assert.strictEqual(el('abs-status').textContent,'Cancel requested. If the server already received the bytes, the outcome stays unknown until you verify delivery.',
  'stale completion does not overwrite the new book context (ledger still records the bound outcome server-side)');
assert.ok(timerCleared,'progress interval cleared after send');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_stale_deliveries_ignored_and_unknown_stays_unknown(self):
        self._run(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const elements={};
const el=id=>elements[id]||(elements[id]={style:{},textContent:'',value:'',checked:false,disabled:false,
  replaceChildren(){this.children=[];},appendChild(child){(this.children=this.children||[]).push(child);},
  children:[]});
let resolveStale=null;
let staleMode=true;
const unknownEntry={id:'d1',export_file:'cloned_audiobook.mp3',title:'Test Book',status:'unknown',
  output_bytes:100,sha256:'s',reconciliation:'candidate only'};
const ctx={document:{getElementById:el,createElement:()=>({style:{},textContent:'',className:'',children:[],
  appendChild(c){this.children.push(c);}})},
  console:{error(){}},chunkSnapshotBookToken:'a'.repeat(64),showActionError(){},
  setInterval:()=>0,clearInterval:()=>{},
  API:{get:async url=>{
    if(url.startsWith('/api/abs/deliveries')){
      if(staleMode){return new Promise(res=>{resolveStale=()=>res({deliveries:[unknownEntry]});});}
      return{deliveries:[unknownEntry]};}
    throw new Error('unexpected GET '+url);},
    post:async (url,body)=>{
    if(url==='/api/abs/reconcile'){assert.strictEqual(body.delivery_id,'d1');return{status:'unknown',reconciliation:'candidate only'};}
    throw new Error('unexpected POST '+url);}}};
vm.createContext(ctx);
Object.assign(ctx,{t:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),formatUiSafetyText:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),tSafety:()=>({primary:'',secondary:null}),renderUiSafety:(target,k,e,v={})=>{target.textContent=String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m);},getIntlUiLocale:()=> 'en'});
vm.runInContext(source,ctx);
(async()=>{
const first=ctx.loadAbsDeliveries();
ctx.resetAbsPreview();
el('abs-status').textContent='cleared-after-reset';
resolveStale();await first;
assert.strictEqual(el('abs-status').textContent,'cleared-after-reset','stale deliveries response ignored after reset');
staleMode=false;
await ctx.loadAbsDeliveries();
assert.strictEqual(el('abs-deliveries').children.length,1,'one unknown delivery rendered');
const row=el('abs-deliveries').children[0];
assert.ok(row.children.some(child=>child.textContent==='Verify delivery'),'unknown offers verify delivery');
await ctx.reconcileAbsDelivery('d1');
assert.match(el('abs-status').textContent,/Still unknown/);
assert.match(el('abs-status').textContent,/Byte verification/);
assert.ok(!/Reconciled as delivered/.test(el('abs-status').textContent),'never claims reconciled-delivered on candidate evidence');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_reconcile_sent_renders_byte_verified(self):
        self._run(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const elements={};
const el=id=>elements[id]||(elements[id]={style:{},textContent:'',value:'',checked:false,disabled:false,
  replaceChildren(){this.children=[];},appendChild(child){(this.children=this.children||[]).push(child);},
  children:[]});
const ctx={document:{getElementById:el,createElement:()=>({style:{},textContent:'',className:'',children:[],
  appendChild(c){this.children.push(c);}})},
  console:{error(){}},chunkSnapshotBookToken:'a'.repeat(64),showActionError(){},
  setInterval:()=>0,clearInterval:()=>{},
  API:{get:async url=>{
    if(url.startsWith('/api/abs/deliveries')){return{deliveries:[{id:'d1',export_file:'cloned_audiobook.mp3',title:'Test Book',status:'sent',output_bytes:100,sha256:'s',reconciliation:'verified by raw bytes'}]};}
    throw new Error('unexpected GET '+url);},
    post:async (url,body)=>{
    if(url==='/api/abs/reconcile'){return{status:'sent',reconciliation:'verified by raw bytes'};}
    throw new Error('unexpected POST '+url);}}};
vm.createContext(ctx);
Object.assign(ctx,{t:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),formatUiSafetyText:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),tSafety:()=>({primary:'',secondary:null}),renderUiSafety:(target,k,e,v={})=>{target.textContent=String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m);},getIntlUiLocale:()=> 'en'});
vm.runInContext(source,ctx);
(async()=>{
await ctx.loadAbsDeliveries();
const row=el('abs-deliveries').children[0];
assert.ok(!row.children.some(child=>child.textContent==='Verify delivery'),'sent offers no verify');
assert.ok(row.children[0].textContent.includes('Delivered — file bytes verified'),'sent row reads verified, not raw status');
await ctx.reconcileAbsDelivery('d1');
assert.match(el('abs-status').textContent,/Delivered — file bytes verified/);
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_send_accepted_shows_verify_then_verified_delivered(self):
        self._run(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const elements={};
const el=id=>elements[id]||(elements[id]={style:{},textContent:'',value:'',checked:false,disabled:false,
  replaceChildren(){this.children=[];},appendChild(child){(this.children=this.children||[]).push(child);},
  children:[]});
el('abs-server-url').value='http://abs.test:13378';
el('abs-library').value='lib_1';el('abs-folder').value='fol_1';el('abs-title').value='Test Book';
el('abs-export-format').value='cloned_audiobook.mp3';
const acceptedEntry={id:'d1',status:'unknown',verification_pending:true,upload_accepted_at:123,
  verification_note:'Upload accepted by the server (HTTP 200); delivery is not yet verified.',
  export_file:'cloned_audiobook.mp3',title:'Test Book',output_bytes:100,sha256:'abc'};
let deliveries=[acceptedEntry];
const ctx={document:{getElementById:el,createElement:()=>({style:{},textContent:'',className:'',children:[],
  appendChild(c){this.children.push(c);}})},
  console:{error(){}},chunkSnapshotBookToken:'a'.repeat(64),showActionError(){},
  setInterval:()=>0,clearInterval:()=>{},
  API:{get:async url=>{
    if(url.startsWith('/api/abs/progress')){return{progress:null};}
    if(url.startsWith('/api/abs/deliveries')){return{deliveries};}
    throw new Error('unexpected GET '+url);},
    post:async (url,body)=>{
    if(url==='/api/abs/preview'){return{preview_id:'p1',book_token:'a'.repeat(64),export_file:'cloned_audiobook.mp3',
      base_url:'http://abs.test:13378',library_id:'lib_1',folder_id:'fol_1',title:'Test Book',author:'',series:'',
      sha256:'abc',output_bytes:100,input_revision:'r'};}
    if(url==='/api/abs/send'){return acceptedEntry;}
    if(url==='/api/abs/reconcile'){assert.strictEqual(body.delivery_id,'d1');return{status:'sent',verification_pending:false,reconciliation:'verified by raw bytes'};}
    throw new Error('unexpected POST '+url);}}};
vm.createContext(ctx);
Object.assign(ctx,{t:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),formatUiSafetyText:(k,e,v={})=>String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m),tSafety:()=>({primary:'',secondary:null}),renderUiSafety:(target,k,e,v={})=>{target.textContent=String(e).replace(/\{(\w+)\}/g,(m,n)=>Object.prototype.hasOwnProperty.call(v,n)?String(v[n]):m);},getIntlUiLocale:()=> 'en'});
vm.runInContext(source,ctx);
(async()=>{
await ctx.previewAbsSend();
el('abs-right-to-share').checked=true;ctx.updateAbsSendEnabled();
await ctx.sendAbsExport();
assert.match(el('abs-status').textContent,/Upload accepted; verify delivery/,'accepted send reads upload accepted, not delivered');
assert.ok(!/Delivered/.test(el('abs-status').textContent),'accepted send never claims delivered');
await ctx.loadAbsDeliveries();
const row=el('abs-deliveries').children[0];
assert.ok(row.children[0].textContent.includes('Upload accepted; verify delivery'),'deliveries row shows accepted label, not raw unknown');
assert.ok(row.children.some(child=>child.textContent==='Verify delivery'),'accepted row offers Verify delivery');
assert.ok(row.children.some(child=>child.textContent.includes('not yet verified')),'verification note rendered');
await ctx.reconcileAbsDelivery('d1');
assert.match(el('abs-status').textContent,/Delivered — file bytes verified/,'verified reconcile reads delivered verified');
assert.ok(!/Upload accepted/.test(el('abs-status').textContent),'verified status no longer reads accepted');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_saved_config_restores_server_url_without_contacting_the_server(self):
        self._run(_HARNESS + r'''
const h=makeContext();
const hostile='http://127.0.0.1:43007/"><b>q</b>';
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  return {base_url:hostile,has_key:true,library_id:'lib_1',folder_id:'fol_1'};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
assert.strictEqual(h.el('abs-server-url').value,hostile,'server URL restored as a value, not markup');
assert.strictEqual(h.el('abs-api-key').value,'','the key is neither shown nor requested');
assert.match(h.el('abs-status').textContent,/Restored saved server/,'restore is reported in the status line');
assert.match(h.el('abs-status').textContent,/API key is stored/,'has_key true uses the stored-key wording');
assert.ok(!/in the/.test(h.el('abs-status').textContent),'no key_store is invented when the reply omits it');
assert.deepStrictEqual(h.calls,[['GET','/api/abs/config']],'one local config read, no libraries or preview call');
assert.strictEqual(h.el('abs-library').children.length,0,'libraries stay empty until the user loads them');
assert.strictEqual(h.el('abs-folder').children.length,0);
await h.ctx.loadAbsSavedConfig();
assert.deepStrictEqual(h.calls,[['GET','/api/abs/config']],'reopening the panel never refetches over user edits');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_saved_config_without_a_key_says_so(self):
        self._run(_HARNESS + r'''
const h=makeContext();
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  return {base_url:'http://abs.test:13378',has_key:false,library_id:null,folder_id:null};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
assert.strictEqual(h.el('abs-server-url').value,'http://abs.test:13378');
assert.match(h.el('abs-status').textContent,/API key is not stored yet/,'has_key false says no key is stored');
assert.ok(!/API key is stored/.test(h.el('abs-status').textContent),'never claims a key that is not stored');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_invalid_or_empty_config_shows_nothing_and_is_not_refetched(self):
        self._run(_HARNESS + r'''
async function check(reply){
  const h=makeContext();
  h.ctx.API.get=async url=>{h.calls.push(['GET',url]);return reply;};
  await h.ctx.loadAbsSavedConfig();
  assert.strictEqual(h.el('abs-server-url').value,'','no URL is invented');
  assert.strictEqual(h.el('abs-status').textContent,'','no misleading status is shown');
  assert.deepStrictEqual(h.calls,[['GET','/api/abs/config']]);
  await h.ctx.loadAbsSavedConfig();
  assert.strictEqual(h.calls.length,1,'an empty successful reply is cached, not refetched on every open');
}
(async()=>{
await check(null);
await check({});
await check({has_key:true});
await check({base_url:''});
await check({base_url:42});
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_failed_config_read_is_retried_on_the_next_open(self):
        self._run(_HARNESS + r'''
const h=makeContext();
let attempt=0;
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  attempt+=1;
  if(attempt===1){throw new Error('keyring unavailable');}
  return {base_url:'http://abs.test:13378',has_key:true,library_id:'lib_1',folder_id:'fol_1'};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
assert.strictEqual(h.el('abs-server-url').value,'','a failed read restores nothing');
assert.strictEqual(h.calls.length,1);
await h.ctx.loadAbsSavedConfig();
assert.strictEqual(h.calls.length,2,'the next open retries after a failed read');
assert.strictEqual(h.el('abs-server-url').value,'http://abs.test:13378','the retry restores the saved server');
assert.match(h.el('abs-status').textContent,/Restored saved server/);
await h.ctx.loadAbsSavedConfig();
assert.strictEqual(h.calls.length,2,'a successful read is cached for the page');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_failed_config_read_retry_does_not_overwrite_typed_values(self):
        self._run(_HARNESS + r'''
const h=makeContext();
let attempt=0;
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  attempt+=1;
  if(attempt===1){throw new Error('keyring unavailable');}
  return {base_url:'http://saved.test:1',has_key:true,library_id:'lib_1',folder_id:'fol_1'};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
h.type('abs-server-url','http://typed.test:2');
h.el('abs-status').textContent='typed';
await h.ctx.loadAbsSavedConfig();
assert.strictEqual(h.calls.length,2,'the retry is a real second request');
assert.strictEqual(h.el('abs-server-url').value,'http://typed.test:2','the retry leaves a typed URL alone');
assert.strictEqual(h.el('abs-status').textContent,'typed','the retry does not overwrite the status line');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_concurrent_panel_opens_issue_one_config_request(self):
        self._run(_HARNESS + r'''
const h=makeContext();
let resolveConfig=null;
h.ctx.API.get=url=>{h.calls.push(['GET',url]);return new Promise(resolve=>{resolveConfig=resolve;});};
(async()=>{
const first=h.ctx.loadAbsSavedConfig();
const second=h.ctx.loadAbsSavedConfig();
const third=h.ctx.loadAbsSavedConfig();
assert.strictEqual(h.calls.length,1,'opening before the first reply issues exactly one request');
resolveConfig({base_url:'http://abs.test:13378',has_key:true,library_id:'',folder_id:''});
await Promise.all([first,second,third]);
assert.deepStrictEqual(h.calls,[['GET','/api/abs/config']],'still one config read after all opens settle');
assert.strictEqual(h.el('abs-server-url').value,'http://abs.test:13378','the shared reply still restores the server');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_panel_open_hook_loads_on_open_not_close(self):
        self._run(r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'..','index.html'),'utf8');
const tail=html.slice(html.indexOf('id="abs-delivery-options"'));
const tag=tail.slice(0,tail.indexOf('>'));
const handler=tag.split('ontoggle="')[1].split('"')[0];
let calls=0;
const fn=vm.runInNewContext('(function(){'+handler+'})',{loadAbsSavedConfig:()=>{calls+=1;}});
fn.call({open:false});
assert.strictEqual(calls,0,'a close before the first open does not restore');
fn.call({open:true});
assert.strictEqual(calls,1,'opening the disclosure restores the saved config');
fn.call({open:false});
assert.strictEqual(calls,1,'closing after the first open does not restore again');
console.log('ok');
''')

    def test_saved_library_and_folder_are_preselected_only_after_load(self):
        self._run(_HARNESS + r'''
const h=makeContext();
const base='http://abs.test:13378';
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  if(url==='/api/abs/config'){return {base_url:base,has_key:true,library_id:'lib_1',folder_id:'fol_1'};}
  return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1',fullPath:'/books'},{id:'fol_2',fullPath:'/more'}]},
                     {id:'lib_2',name:'Pods',folders:[{id:'fol_3',fullPath:'/pods'}]}]};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
assert.strictEqual(h.el('abs-library').children.length,0,'restoring the URL must not load libraries');
await h.ctx.loadAbsLibraries();
assert.ok(h.calls[1][1].startsWith('/api/abs/libraries?base_url=http%3A%2F%2Fabs.test%3A13378'),'load uses the restored URL');
assert.strictEqual(h.el('abs-library').value,'lib_1','saved library preselected after Load libraries');
assert.strictEqual(h.el('abs-folder').children.length,2);
assert.strictEqual(h.el('abs-folder').value,'fol_1','saved folder preselected from the saved library');
assert.strictEqual(h.ctx.getAbsDestination().library_id,'lib_1');
assert.strictEqual(h.ctx.getAbsDestination().folder_id,'fol_1');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_missing_saved_library_or_folder_is_cleared(self):
        self._run(_HARNESS + r'''
async function run(config,libraries){
  const h=makeContext();
  h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
    if(url==='/api/abs/config'){return config;}
    return {base_url:config.base_url,libraries};};
  await h.ctx.loadAbsSavedConfig();
  await h.ctx.loadAbsLibraries();
  return h;
}
(async()=>{
const gone=await run({base_url:'http://abs.test:13378',has_key:true,library_id:'lib_gone',folder_id:'fol_1'},
  [{id:'lib_1',name:'Main',folders:[{id:'fol_1',fullPath:'/books'}]}]);
assert.strictEqual(gone.el('abs-library').value,'','a vanished saved library is cleared');
assert.strictEqual(gone.el('abs-folder').value,'','its folder is cleared too');
assert.strictEqual(gone.ctx.getAbsDestination().library_id,'','cleared library cannot be sent');
assert.strictEqual(gone.ctx.getAbsDestination().folder_id,'','cleared folder cannot be sent');
await gone.ctx.previewAbsSend();
assert.match(gone.el('abs-status').textContent,/Choose a library and folder first/,'preview refuses without a cleared library');
assert.ok(gone.calls.every(call=>call[0]==='GET'),'preview refused before any network call');
const folderGone=await run({base_url:'http://abs.test:13378',has_key:true,library_id:'lib_1',folder_id:'fol_gone'},
  [{id:'lib_1',name:'Main',folders:[{id:'fol_1',fullPath:'/books'}]}]);
assert.strictEqual(folderGone.el('abs-library').value,'lib_1');
assert.strictEqual(folderGone.el('abs-folder').value,'','a vanished saved folder is cleared');
assert.strictEqual(folderGone.ctx.getAbsDestination().folder_id,'','cleared folder cannot be sent');
await folderGone.ctx.previewAbsSend();
assert.match(folderGone.el('abs-status').textContent,/Choose a library and folder first/,'preview refuses without a cleared folder');
assert.ok(folderGone.calls.every(call=>call[0]==='GET'),'preview refused before any network call');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_saved_folder_from_another_library_is_not_preselected(self):
        self._run(_HARNESS + r'''
const h=makeContext();
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  if(url==='/api/abs/config'){return {base_url:'http://abs.test:13378',has_key:true,library_id:'lib_2',folder_id:'fol_1'};}
  return {base_url:'http://abs.test:13378',libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1',fullPath:'/books'}]},
                     {id:'lib_2',name:'Pods',folders:[{id:'fol_3',fullPath:'/pods'}]}]};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_2');
assert.strictEqual(h.el('abs-folder').children.length,1,'folders belong to the selected library');
assert.strictEqual(h.el('abs-folder').value,'','a folder from a different library is not preselected');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_saved_selection_is_not_applied_to_a_different_server(self):
        self._run(_HARNESS + r'''
const h=makeContext();
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  if(url==='/api/abs/config'){return {base_url:'http://saved.test:13378',has_key:true,library_id:'lib_2',folder_id:'fol_1'};}
  const target=decodeURIComponent(url.split('base_url=')[1]).replace(/\/+$/,'');
  return {base_url:target,libraries:[{id:'lib_9',name:'Other first',folders:[{id:'fol_9',fullPath:'/other'}]},
                     {id:'lib_1',name:'Main',folders:[{id:'fol_1',fullPath:'/books'}]},
                     {id:'lib_2',name:'Pods',folders:[{id:'fol_3',fullPath:'/pods'}]}]};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
h.el('abs-server-url').value='http://other.test:13378';
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_9','another server starts at its own first library, not the saved one');
assert.strictEqual(h.el('abs-folder').value,'fol_9','another server starts at its own first folder, not the saved one');
h.el('abs-server-url').value='http://other.test:13378/';
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_9','a trailing slash on another server is still another server');
assert.strictEqual(h.el('abs-folder').value,'fol_9','its first folder is kept too');
h.el('abs-library').value='lib_2';h.ctx.fillAbsFolders();
assert.strictEqual(h.el('abs-folder').children.length,1,'changing library still repopulates folders');
assert.strictEqual(h.el('abs-folder').value,'fol_3','the saved folder does not stick to another library');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_saved_spelling_is_normalised_after_restore(self):
        self._run(_HARNESS + r'''
const h=makeContext();
const base='http://abs.test:13378';
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  if(url==='/api/abs/config'){return {base_url:base,has_key:true,library_id:'lib_2',folder_id:'fol_3'};}
  return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'}]},
                                   {id:'lib_2',name:'Pods',folders:[{id:'fol_3'}]},
                                   {id:'lib_3',name:'More',folders:[{id:'fol_4'}]}]};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
h.el('abs-server-url').value=base;
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_2','the exact saved spelling selects the saved library');
assert.strictEqual(h.el('abs-folder').value,'fol_3','the exact saved spelling selects the saved folder');
h.el('abs-library').value='lib_1';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_1';
h.el('abs-server-url').value=base+'/';
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_2','a trailing slash names the same saved server');
assert.strictEqual(h.el('abs-folder').value,'fol_3','so the saved folder is preselected too');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_saved_spelling_is_normalised_after_save(self):
        self._run(_HARNESS + r'''
const h=makeContext();
const base='http://abs.test:13378';
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'}]},
                                   {id:'lib_2',name:'Pods',folders:[{id:'fol_3'}]}]};};
h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
  return {base_url:body.base_url.replace(/\/+$/,''),has_key:true,key_store:null,
          library_id:body.library_id,folder_id:body.folder_id};};
(async()=>{
h.type('abs-server-url',base+'/');
await h.ctx.loadAbsLibraries();
h.el('abs-library').value='lib_2';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_3';
await h.ctx.saveAbsConfig();
h.el('abs-library').value='lib_1';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_1';
h.el('abs-server-url').value=base;
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_2','after Save the exact spelling finds the saved library');
assert.strictEqual(h.el('abs-folder').value,'fol_3','and the saved folder');
h.el('abs-library').value='lib_1';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_1';
h.el('abs-server-url').value=base+'/';
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_2','after Save the trailing slash finds the saved library');
assert.strictEqual(h.el('abs-folder').value,'fol_3','and the saved folder');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_after_restore_keeps_the_saved_pair(self):
        self._run(_HARNESS + r'''
const base='http://abs.test:13378';
const store={base_url:base,has_key:true,library_id:'lib_2',folder_id:'fol_3'};
function fresh(){
  const h=makeContext();
  h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
    if(url==='/api/abs/config'){return Object.assign({},store);}
    return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'}]},
                                     {id:'lib_2',name:'Pods',folders:[{id:'fol_3'}]}]};};
  h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
    store.base_url=body.base_url;store.library_id=body.library_id;store.folder_id=body.folder_id;store.has_key=true;
    return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
  return h;
}
(async()=>{
const first=fresh();
await first.ctx.loadAbsSavedConfig();
await first.ctx.saveAbsConfig();
const posted=first.calls.find(call=>call[0]==='POST');
assert.strictEqual(posted[2].library_id,'lib_2','Save before any Load keeps the restored library');
assert.strictEqual(posted[2].folder_id,'fol_3','and the restored folder');
assert.strictEqual(posted[2].api_key,null,'an empty key field still posts no key');
await first.ctx.loadAbsLibraries();
assert.strictEqual(first.el('abs-library').value,'lib_2','Load after that Save preselects the kept library');
assert.strictEqual(first.el('abs-folder').value,'fol_3','and the kept folder');
const second=fresh();
await second.ctx.loadAbsSavedConfig();
await second.ctx.loadAbsLibraries();
assert.strictEqual(second.el('abs-library').value,'lib_2','a reopened panel still restores the library');
assert.strictEqual(second.el('abs-folder').value,'fol_3','and the folder');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_of_a_new_key_after_restore_keeps_the_saved_pair(self):
        self._run(_HARNESS + r'''
const base='http://abs.test:13378';
const store={base_url:base,has_key:false,library_id:'lib_2',folder_id:'fol_3'};
function fresh(){
  const h=makeContext();
  h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
    if(url==='/api/abs/config'){return Object.assign({},store);}
    return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'}]},
                                     {id:'lib_2',name:'Pods',folders:[{id:'fol_3'}]}]};};
  h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
    store.base_url=body.base_url;store.library_id=body.library_id;store.folder_id=body.folder_id;store.has_key=true;
    return {base_url:body.base_url,has_key:true,key_store:'OS credential store',library_id:body.library_id,folder_id:body.folder_id};};
  return h;
}
(async()=>{
const first=fresh();
await first.ctx.loadAbsSavedConfig();
first.el('abs-api-key').value='the-new-key';
await first.ctx.saveAbsConfig();
const posted=first.calls.find(call=>call[0]==='POST');
assert.strictEqual(posted[2].api_key,'the-new-key','the new key is posted');
assert.strictEqual(posted[2].library_id,'lib_2','storing a new key keeps the restored library');
assert.strictEqual(posted[2].folder_id,'fol_3','and the restored folder');
const second=fresh();
await second.ctx.loadAbsSavedConfig();
await second.ctx.loadAbsLibraries();
assert.strictEqual(second.el('abs-library').value,'lib_2','after storing the key a reopened panel restores the library');
assert.strictEqual(second.el('abs-folder').value,'fol_3','and the folder');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_after_restore_accepts_a_trailing_slash(self):
        self._run(_HARNESS + r'''
const base='http://abs.test:13378';
const h=makeContext();
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  return {base_url:base,has_key:true,library_id:'lib_2',folder_id:'fol_3'};};
h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
  return {base_url:body.base_url.replace(/\/+$/,''),has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
h.el('abs-server-url').value=base+'/';
await h.ctx.saveAbsConfig();
const posted=h.calls.find(call=>call[0]==='POST');
assert.strictEqual(posted[2].library_id,'lib_2','a trailing slash on the restored server still keeps the library');
assert.strictEqual(posted[2].folder_id,'fol_3','and the folder');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_for_another_server_posts_nulls(self):
        self._run(_HARNESS + r'''
const h=makeContext();
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  return {base_url:'http://saved.test:13378',has_key:true,library_id:'lib_2',folder_id:'fol_1'};};
h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
  return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
h.el('abs-server-url').value='http://other.test:13378';
await h.ctx.saveAbsConfig();
const posted=h.calls.find(call=>call[0]==='POST');
assert.strictEqual(posted[2].library_id,null,'a typed other server posts no library, as on main');
assert.strictEqual(posted[2].folder_id,null,'and no folder, as on main');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_after_load_and_clear_posts_nulls(self):
        self._run(_HARNESS + r'''
const base='http://abs.test:13378';
const h=makeContext();
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  if(url==='/api/abs/config'){return {base_url:base,has_key:true,library_id:'lib_2',folder_id:'fol_3'};}
  return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'}]},
                                   {id:'lib_2',name:'Pods',folders:[{id:'fol_3'}]}]};};
h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
  return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_2');
h.el('abs-library').value='';h.el('abs-folder').value='';
await h.ctx.saveAbsConfig();
const posted=h.calls.find(call=>call[0]==='POST');
assert.strictEqual(posted[2].library_id,null,'a cleared select posts null, as on main');
assert.strictEqual(posted[2].folder_id,null,'and its cleared folder');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_after_the_saved_ids_are_no_longer_listed_posts_nulls(self):
        self._run(_HARNESS + r'''
const base='http://abs.test:13378';
const h=makeContext();
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  if(url==='/api/abs/config'){return {base_url:base,has_key:true,library_id:'lib_2',folder_id:'fol_3'};}
  return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'}]}]};};
h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
  return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'','the vanished saved library is cleared');
await h.ctx.saveAbsConfig();
const posted=h.calls.find(call=>call[0]==='POST');
assert.strictEqual(posted[2].library_id,null,'an unlisted saved library is not resurrected');
assert.strictEqual(posted[2].folder_id,null,'nor its folder');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_without_saved_config_posts_main_body(self):
        self._run(_HARNESS + r'''
const base='http://abs.test:13378';
function fresh(){
  const h=makeContext();
  h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
    if(url==='/api/abs/config'){return {};}
    return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'}]},
                                     {id:'lib_2',name:'Pods',folders:[{id:'fol_3'}]}]};};
  h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
    return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
  return h;
}
(async()=>{
const before=fresh();
await before.ctx.loadAbsSavedConfig();
before.el('abs-server-url').value=base;
await before.ctx.saveAbsConfig();
const early=before.calls.find(call=>call[0]==='POST');
assert.deepStrictEqual({...early[2]},{base_url:base,api_key:null,library_id:null,folder_id:null},
  'with no saved config Save posts exactly main\'s empty body');
const listed=fresh();
await listed.ctx.loadAbsSavedConfig();
listed.el('abs-server-url').value=base;
await listed.ctx.loadAbsLibraries();
listed.el('abs-library').value='lib_2';listed.ctx.fillAbsFolders();listed.el('abs-folder').value='fol_3';
await listed.ctx.saveAbsConfig();
const chosen=listed.calls.find(call=>call[0]==='POST');
assert.deepStrictEqual({...chosen[2]},{base_url:base,api_key:null,library_id:'lib_2',folder_id:'fol_3'},
  'with no saved config Save posts exactly main\'s listed body');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_after_restore_without_a_library_posts_nulls(self):
        self._run(_HARNESS + r'''
const base='http://abs.test:13378';
const h=makeContext();
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  return {base_url:base,has_key:true};};
h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
  return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
await h.ctx.saveAbsConfig();
const posted=h.calls.find(call=>call[0]==='POST');
assert.deepStrictEqual({...posted[2]},{base_url:base,api_key:null,library_id:null,folder_id:null},
  'a saved config with no library posts nulls, as on main');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_with_a_failed_config_read_posts_nulls(self):
        self._run(_HARNESS + r'''
const base='http://abs.test:13378';
const h=makeContext();
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);throw new Error('config unavailable');};
h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
  return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
h.el('abs-server-url').value=base;
await h.ctx.saveAbsConfig();
const posted=h.calls.find(call=>call[0]==='POST');
assert.strictEqual(posted[2].library_id,null,'an unread config cannot resurrect a library');
assert.strictEqual(posted[2].folder_id,null,'an unread config cannot resurrect a folder');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_during_the_config_read_posts_nulls(self):
        self._run(_HARNESS + r'''
const base='http://abs.test:13378';
const h=makeContext();
let resolveConfig=null,resolveSave=null;
h.ctx.API.get=url=>{h.calls.push(['GET',url]);return new Promise(resolve=>{resolveConfig=resolve;});};
h.ctx.API.post=(url,body)=>{h.calls.push(['POST',url,body]);return new Promise(resolve=>{resolveSave=resolve;});};
(async()=>{
const pendingConfig=h.ctx.loadAbsSavedConfig();
h.type('abs-server-url',base);
const pendingSave=h.ctx.saveAbsConfig();
const posted=h.calls.find(call=>call[0]==='POST');
assert.deepStrictEqual({...posted[2]},{base_url:base,api_key:null,library_id:null,folder_id:null},
  'an unanswered config read posts main\'s empty body, so a same-URL Save in that race erases the saved ids');
resolveConfig({base_url:base,has_key:true,library_id:'lib_2',folder_id:'fol_3'});
await pendingConfig;
resolveSave({base_url:base,has_key:true,key_store:null,library_id:null,folder_id:null});
await pendingSave;
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_saved_config_without_a_library_keeps_main_first_pair(self):
        self._run(_HARNESS + r'''
const h=makeContext();
const base='http://abs.test:13378';
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  if(url==='/api/abs/config'){return {base_url:base,has_key:true};}
  return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'},{id:'fol_2'}]},
                                   {id:'lib_2',name:'Pods',folders:[{id:'fol_3'}]}]};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_1','a config with no saved library leaves the first library, as on main');
assert.strictEqual(h.el('abs-folder').value,'fol_1','and the first folder, as on main');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_two_saves_remember_the_last_responding_server(self):
        self._run(_HARNESS + r'''
const h=makeContext();
const a='http://a.test:13378';
const b='http://b.test:13378';
let resolveA=null,resolveB=null;
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  const target=decodeURIComponent(url.split('base_url=')[1]);
  if(target===a){return {base_url:a,libraries:[{id:'lib_1',name:'A1',folders:[{id:'fol_1'}]},
                                                {id:'lib_2',name:'A2',folders:[{id:'fol_2'}]}]};}
  return {base_url:b,libraries:[{id:'lib_9',name:'B9',folders:[{id:'fol_9'}]},
                                {id:'lib_2',name:'B2',folders:[{id:'fol_3'}]}]};};
h.ctx.API.post=(url,body)=>{h.calls.push(['POST',url,body]);
  return new Promise(resolve=>{if(body.base_url===a){resolveA=resolve;}else{resolveB=resolve;}});};
(async()=>{
h.el('abs-server-url').value=a;
await h.ctx.loadAbsLibraries();
h.el('abs-library').value='lib_2';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_2';
const pendingA=h.ctx.saveAbsConfig();
h.el('abs-server-url').value=b;
await h.ctx.loadAbsLibraries();
h.el('abs-library').value='lib_2';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_3';
const pendingB=h.ctx.saveAbsConfig();
resolveB({base_url:b,has_key:true,library_id:'lib_2',folder_id:'fol_3'});
await pendingB;
resolveA({base_url:a,has_key:true,library_id:'lib_2',folder_id:'fol_2'});
await pendingA;
assert.match(h.el('abs-status').textContent,/Server saved \(http:\/\/a\.test:13378\)/,'the last-arriving reply sets the status');
h.el('abs-server-url').value=a;
h.el('abs-library').value='lib_1';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_1';
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_2','the last-arriving reply is what Load preselects on its server');
assert.strictEqual(h.el('abs-folder').value,'fol_2');
h.el('abs-server-url').value=b;
h.el('abs-library').value='lib_9';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_9';
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_9','the superseded reply is not applied to the other server');
assert.strictEqual(h.el('abs-folder').value,'fol_9');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_book_change_during_the_config_read_still_restores(self):
        self._run(_HARNESS + r'''
const h=makeContext();
const base='http://abs.test:13378';
let resolveConfig=null;
h.ctx.API.get=url=>{h.calls.push(['GET',url]);return new Promise(resolve=>{resolveConfig=resolve;});};
(async()=>{
const pending=h.ctx.loadAbsSavedConfig();
h.ctx.chunkSnapshotBookToken='b'.repeat(64);
h.ctx.resetAbsPreview();
resolveConfig({base_url:base,has_key:true,library_id:'lib_1',folder_id:'fol_1'});
await pending;
assert.strictEqual(h.el('abs-server-url').value,base,'a book change during the read still restores the saved URL');
assert.match(h.el('abs-status').textContent,/Restored saved server/,'and says where it came from');
assert.deepStrictEqual(h.calls,[['GET','/api/abs/config']],'the book change does not issue another read');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_second_load_without_saved_config_matches_main(self):
        self._run(_HARNESS + r'''
const h=makeContext();
h.el('abs-server-url').value='http://abs.test:13378';
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);return {libraries:[
  {id:'lib_1',name:'Main',folders:[{id:'fol_1'},{id:'fol_2'}]},
  {id:'lib_2',name:'Pods',folders:[{id:'fol_3'}]}]};};
(async()=>{
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_1');
h.el('abs-library').value='lib_2';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_3';
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_1','without a saved selection a second Load starts at the first library, as on main');
assert.strictEqual(h.el('abs-folder').value,'fol_1','and the first folder, as on main');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_typing_during_save_load_preview_send_behaves_like_main(self):
        self._run(_HARNESS + r'''
async function typingDuringSave(){
  const h=makeContext();
  h.el('abs-server-url').value='http://abs.test:13378';
  h.el('abs-api-key').value='the-secret-key';
  let resolveSave=null;
  h.ctx.API.post=(url,body)=>{h.calls.push(['POST',url,body]);return new Promise(resolve=>{resolveSave=resolve;});};
  const pending=h.ctx.saveAbsConfig();
  h.type('abs-server-url','http://typed-while-saving.test:1');
  resolveSave({base_url:'http://abs.test:13378',has_key:true,key_store:'OS credential store',library_id:null,folder_id:null});
  await pending;
  assert.strictEqual(h.el('abs-api-key').value,'','typing during Save still clears the key field');
  assert.match(h.el('abs-status').textContent,/Server saved/,'typing during Save still shows the save outcome');
  assert.strictEqual(h.el('abs-server-url').value,'http://typed-while-saving.test:1','typing during Save keeps the typed value');
}
async function typingDuringLoad(){
  const h=makeContext();
  h.el('abs-server-url').value='http://abs.test:13378';
  let resolveLoad=null;
  h.ctx.API.get=url=>{h.calls.push(['GET',url]);return new Promise(resolve=>{resolveLoad=resolve;});};
  const pending=h.ctx.loadAbsLibraries();
  h.type('abs-server-url','http://typed-while-loading.test:2');
  resolveLoad({libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'}]},{id:'lib_2',name:'Pods',folders:[{id:'fol_2'}]}]});
  await pending;
  assert.strictEqual(h.el('abs-library').children.length,2,'typing during Load still applies the listing');
  assert.strictEqual(h.el('abs-library').value,'lib_1','the first listed library is selected as on main');
  assert.match(h.el('abs-status').textContent,/Loaded 2 libraries/,'typing during Load still shows the listing status');
}
async function typingDuringPreview(){
  const h=makeContext();
  h.el('abs-server-url').value='http://abs.test:13378';
  h.el('abs-library').appendChild({value:'lib_1'});h.el('abs-folder').appendChild({value:'fol_1'});h.el('abs-title').value='Test Book';
  let resolvePreview=null;
  h.ctx.API.post=(url,body)=>{h.calls.push(['POST',url,body]);return new Promise(resolve=>{resolvePreview=resolve;});};
  const pending=h.ctx.previewAbsSend();
  h.type('abs-server-url','http://typed-while-previewing.test:3');
  resolvePreview({preview_id:'p1',output_bytes:42,base_url:'http://abs.test:13378',library_id:'lib_1',folder_id:'fol_1',export_file:'cloned_audiobook.mp3',sha256:'s',input_revision:'r',title:'Test Book',author:'',series:''});
  await pending;
  assert.match(h.el('abs-preview-out').textContent,/Server: http:\/\/abs.test/,'typing during Preview still renders it');
  assert.match(h.el('abs-status').textContent,/Preview ready/,'typing during Preview keeps the outcome');
}
async function typingDuringSend(){
  const h=makeContext();
  h.el('abs-server-url').value='http://abs.test:13378';
  h.el('abs-library').appendChild({value:'lib_1'});h.el('abs-folder').appendChild({value:'fol_1'});h.el('abs-title').value='Test Book';
  h.el('abs-export-format').value='cloned_audiobook.mp3';
  let resolveSend=null;
  h.ctx.API.get=async url=>{
    if(url.startsWith('/api/abs/deliveries')){return {deliveries:[]};}
    throw new Error('unexpected GET '+url);};
  h.ctx.API.post=(url,body)=>{h.calls.push(['POST',url,body]);
    if(url==='/api/abs/preview'){return Promise.resolve({preview_id:'p1',output_bytes:100,base_url:'http://abs.test:13378',library_id:'lib_1',folder_id:'fol_1',export_file:'cloned_audiobook.mp3',sha256:'abc',input_revision:'r',title:'Test Book',author:'',series:''});}
    if(url==='/api/abs/send'){return new Promise(resolve=>{resolveSend=resolve;});}
    throw new Error('unexpected POST '+url);};
  await h.ctx.previewAbsSend();
  h.el('abs-right-to-share').checked=true;h.ctx.updateAbsSendEnabled();
  const pending=h.ctx.sendAbsExport();
  h.type('abs-server-url','http://typed-while-sending.test:4');
  resolveSend({status:'sent',output_bytes:100,sha256:'abc',export_file:'cloned_audiobook.mp3',title:'Test Book'});
  await pending;
  assert.match(h.el('abs-status').textContent,/Delivered/,'typing during Send still shows the outcome');
  assert.ok(!/Sending/.test(h.el('abs-status').textContent),'the status is not left on Sending');
}
(async()=>{
await typingDuringSave();
await typingDuringLoad();
await typingDuringPreview();
await typingDuringSend();
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_late_config_reply_never_overwrites_typed_or_saved_values(self):
        self._run(_HARNESS + r'''
async function typedThenReply(){
  const h=makeContext();
  let resolveConfig=null;
  h.ctx.API.get=url=>{h.calls.push(['GET',url]);return new Promise(resolve=>{resolveConfig=resolve;});};
  const pending=h.ctx.loadAbsSavedConfig();
  h.type('abs-server-url','http://typed.test:1');
  const typedStatus=h.el('abs-status').textContent;
  resolveConfig({base_url:'http://saved.test:1',has_key:true,library_id:'lib_1',folder_id:'fol_1'});
  await pending;
  assert.strictEqual(h.el('abs-server-url').value,'http://typed.test:1','a late reply leaves the typed URL alone');
  assert.strictEqual(h.el('abs-status').textContent,typedStatus,'a late reply does not overwrite the status line');
  assert.ok(!/Restored saved server/.test(typedStatus),'the stale reply never restores a server');
}
async function typedThenCleared(){
  const h=makeContext();
  let resolveConfig=null;
  h.ctx.API.get=url=>{h.calls.push(['GET',url]);return new Promise(resolve=>{resolveConfig=resolve;});};
  const pending=h.ctx.loadAbsSavedConfig();
  h.type('abs-server-url','http://typed.test:1');
  h.type('abs-server-url','');
  const clearedStatus=h.el('abs-status').textContent;
  resolveConfig({base_url:'http://saved.test:1',has_key:true,library_id:'lib_1',folder_id:'fol_1'});
  await pending;
  assert.strictEqual(h.el('abs-server-url').value,'','a URL typed and cleared on purpose stays cleared');
  assert.strictEqual(h.el('abs-status').textContent,clearedStatus,'the cleared field keeps its status');
  assert.ok(!/Restored saved server/.test(h.el('abs-status').textContent));
  await h.ctx.loadAbsSavedConfig();
  assert.deepStrictEqual(h.calls,[['GET','/api/abs/config']],'a dropped reply is never refetched over the edit');
  assert.strictEqual(h.el('abs-server-url').value,'','a later open never refills the cleared field');
}
async function savedThenReply(){
  const h=makeContext();
  const serverB='http://new.test:2';
  let resolveConfig=null;
  h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
    if(url==='/api/abs/config'){return new Promise(resolve=>{resolveConfig=resolve;});}
    return {base_url:serverB,libraries:[{id:'lib_1',name:'One',folders:[{id:'fol_1'}]},{id:'lib_2',name:'Two',folders:[{id:'fol_2'}]}]};};
  h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
    return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
  const pending=h.ctx.loadAbsSavedConfig();
  h.type('abs-server-url',serverB);
  await h.ctx.loadAbsLibraries();
  h.el('abs-library').value='lib_2';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_2';
  await h.ctx.saveAbsConfig();
  resolveConfig({base_url:'http://old.test:1',has_key:false,library_id:'lib_1',folder_id:'fol_1'});
  await pending;
  assert.strictEqual(h.el('abs-server-url').value,serverB,'a config reply from before Save cannot revert it');
  assert.match(h.el('abs-status').textContent,/Server saved/,'the save result stays on screen');
  assert.ok(!/Restored saved server/.test(h.el('abs-status').textContent),'the pre-Save reply restores nothing');
  h.el('abs-library').value='';h.el('abs-folder').value='';
  await h.ctx.loadAbsLibraries();
  assert.strictEqual(h.el('abs-library').value,'lib_2','the older reply cannot replace the just-saved library');
  assert.strictEqual(h.el('abs-folder').value,'fol_2','the older reply cannot replace the just-saved folder');
  assert.strictEqual(h.calls.filter(call=>call[0]==='GET'&&call[1]==='/api/abs/config').length,1,'a dropped reply is not refetched later');
}
(async()=>{
await typedThenReply();
await typedThenCleared();
await savedThenReply();
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_browser_restored_url_is_not_replaced_by_the_first_restore(self):
        self._run(_HARNESS + r'''
const h=makeContext();
h.el('abs-server-url').value='http://typed-bfcache.test:1';
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  return {base_url:'http://saved.test:13378',has_key:true,library_id:'lib_1',folder_id:'fol_1'};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
assert.strictEqual(h.el('abs-server-url').value,'http://typed-bfcache.test:1','a browser-restored URL is left alone');
assert.strictEqual(h.el('abs-status').textContent,'','no restore status is shown over restored text');
assert.deepStrictEqual(h.calls,[['GET','/api/abs/config']]);
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_save_server_never_rewrites_the_url_field(self):
        self._run(_HARNESS + r'''
async function typingDuringSaveSurvives(){
  const h=makeContext();
  h.type('abs-server-url','http://abs.test:13378/');
  let resolveSave=null;
  h.ctx.API.post=(url,body)=>{h.calls.push(['POST',url,body]);return new Promise(resolve=>{resolveSave=resolve;});};
  const pending=h.ctx.saveAbsConfig();
  h.type('abs-server-url','http://typed-while-saving.test:1');
  resolveSave({base_url:'http://abs.test:13378',has_key:true,library_id:null,folder_id:null});
  await pending;
  assert.strictEqual(h.el('abs-server-url').value,'http://typed-while-saving.test:1','typing during Save survives the reply');
}
async function typedSpellingIsRemembered(){
  const h=makeContext();
  const typed='http://abs.test:13378/';
  h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
    return {base_url:'http://abs.test:13378',libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1'}]},
                       {id:'lib_2',name:'Pods',folders:[{id:'fol_2'}]}]};};
  h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
    return {base_url:'http://abs.test:13378',has_key:true,key_store:null,
            library_id:body.library_id,folder_id:body.folder_id};};
  h.type('abs-server-url',typed);
  await h.ctx.loadAbsLibraries();
  h.el('abs-library').value='lib_2';h.ctx.fillAbsFolders();h.el('abs-folder').value='fol_2';
  await h.ctx.saveAbsConfig();
  assert.strictEqual(h.el('abs-server-url').value,typed,'Save server leaves the typed spelling in the URL field');
  h.el('abs-library').value='';h.el('abs-folder').value='';
  await h.ctx.loadAbsLibraries();
  assert.strictEqual(h.el('abs-library').value,'lib_2','the saved library is matched under the typed spelling');
  assert.strictEqual(h.el('abs-folder').value,'fol_2','the saved folder is matched under the typed spelling');
}
(async()=>{
await typingDuringSaveSurvives();
await typedSpellingIsRemembered();
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_saved_selection_survives_save_then_reload(self):
        self._run(_HARNESS + r'''
const h=makeContext();
const base='http://abs.test:13378';
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  if(url==='/api/abs/config'){return {base_url:base,has_key:true,library_id:'lib_1',folder_id:'fol_1'};}
  return {base_url:base,libraries:[{id:'lib_1',name:'Main',folders:[{id:'fol_1',fullPath:'/books'}]},
                     {id:'lib_2',name:'Pods',folders:[{id:'fol_3',fullPath:'/pods'}]}]};};
h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
  return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_1');
h.el('abs-library').value='lib_2';h.ctx.fillAbsFolders();
h.el('abs-folder').value='fol_3';
await h.ctx.saveAbsConfig();
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_2','after Save server, Load preselects the just-saved library');
assert.strictEqual(h.el('abs-folder').value,'fol_3','after Save server, Load preselects the just-saved folder');
assert.strictEqual(h.ctx.getAbsDestination().folder_id,'fol_3','the kept selection is what would be sent');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')

    def test_saving_another_server_after_a_listing_posts_nulls_and_load_starts_at_first_pair(self):
        self._run(_HARNESS + r'''
const h=makeContext();
const serverA='http://a.test:13378';
const serverB='http://b.test:13378';
h.ctx.API.get=async url=>{h.calls.push(['GET',url]);
  if(url==='/api/abs/config'){return {base_url:serverA,has_key:true,library_id:'lib_2',folder_id:'fol_2'};}
  const target=decodeURIComponent(url.split('base_url=')[1]).replace(/\/+$/,'');
  if(target===serverB){return {base_url:target,libraries:[{id:'lib_9',name:'B9',folders:[{id:'fol_9'}]},
                                         {id:'lib_2',name:'B2',folders:[{id:'fol_3'}]}]};}
  return {base_url:target,libraries:[{id:'lib_1',name:'A',folders:[{id:'fol_1'}]},
                     {id:'lib_2',name:'A2',folders:[{id:'fol_2'}]}]};};
h.ctx.API.post=async(url,body)=>{h.calls.push(['POST',url,body]);
  return {base_url:body.base_url,has_key:true,key_store:null,library_id:body.library_id,folder_id:body.folder_id};};
(async()=>{
await h.ctx.loadAbsSavedConfig();
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_2');
h.el('abs-server-url').value=serverB;
await h.ctx.saveAbsConfig();
const posted=h.calls.filter(call=>call[0]==='POST').pop();
assert.strictEqual(posted[2].base_url,serverB);
assert.strictEqual(posted[2].library_id,null,'a listing loaded from another server is not sent under this one');
assert.strictEqual(posted[2].folder_id,null,'its folder is not sent either');
await h.ctx.loadAbsLibraries();
assert.strictEqual(h.el('abs-library').value,'lib_9','Load after saving another server starts at its first library, as on main');
assert.strictEqual(h.el('abs-folder').value,'fol_9','and its first folder, as on main');
console.log('ok');
})().catch(error=>{console.error(error);process.exit(1);});
''')


if __name__ == '__main__':
    unittest.main()
