"""Audiobookshelf panel handlers: reset, preview gating, key clearing (#1727)."""
from pathlib import Path
import subprocess
import unittest


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
vm.runInContext(source,vm.createContext(ctx));
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
vm.runInContext(source,vm.createContext(ctx));
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


if __name__ == '__main__':
    unittest.main()
