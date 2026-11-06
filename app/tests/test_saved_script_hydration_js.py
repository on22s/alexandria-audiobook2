"""Execute the saved-book handler with gated hydration and failure responses."""
from pathlib import Path
import subprocess
import unittest


class SavedScriptHydrationTests(unittest.TestCase):
    def test_confirmed_saved_loads_are_serialized_and_only_latest_publishes(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-scripts.js'
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');const posts=[],events=[],toasts=[];
const ctx={document:{getElementById:()=>({style:{}})},console:{error(){}},showConfirm:async()=>true,flushVoiceSaves:async()=>{},ensureCastListEditsDiscardable:async()=>true,currentBookFilename:'',API:{post:(path,body)=>new Promise((resolve,reject)=>posts.push({path,body,resolve,reject}))},applyCurrentBookFilename:name=>{events.push(name);ctx.currentBookFilename=name;ctx.window._selectedUploadIdentity=null;},showToast:(...args)=>toasts.push(args)};
for(const name of ['clearCastListEditor','clearCharacterAliases','resetDesignerForm','clearVoiceSuggestions','loadCharacterAliases','loadCastList','loadChunks','refreshVoiceMetadata','loadVoices','loadSavedScripts','loadDesignedVoices']){ctx[name]=async()=>{};}
ctx.window=ctx;vm.createContext(ctx);const core=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInContext(core.slice(core.indexOf('function enqueueBookSelection('),core.indexOf('function getCurrentBookName(')),ctx);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),ctx);vm.runInContext(source.slice(source.indexOf('async function loadScript('),source.indexOf('async function deleteScript(name)')),ctx);
const turn=()=>new Promise(resolve=>setImmediate(resolve));let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{
const a=ctx.loadScript('A');await turn();const b=ctx.loadScript('B');await turn();assert.strictEqual(posts.length,1);assert.strictEqual(JSON.stringify(posts[0].body),JSON.stringify({name:'A'}));posts[0].resolve({name:'A'});await a;await turn();assert.deepStrictEqual(events,[]);assert.strictEqual(posts.length,2);assert.strictEqual(posts[1].body.name,'B');posts[1].resolve({name:'B'});await b;assert.deepStrictEqual(events,['B.json']);assert.strictEqual(toasts.length,1);
const c=ctx.loadScript('C');await turn();const d=ctx.loadScript('D');await turn();posts[2].reject(Error('stale error'));await c;await turn();assert.strictEqual(toasts.length,1);posts[3].resolve({name:'D'});await d;assert.deepStrictEqual(events,['B.json','D.json']);
const e=ctx.loadScript('E');await turn();posts[4].reject(Error('current error'));await e;assert(toasts.at(-1)[0].includes('current error'));const f=ctx.loadScript('F','a'.repeat(64));await turn();assert.strictEqual(JSON.stringify(posts[5].body),JSON.stringify({name:'F',expected_revision:'a'.repeat(64)}));posts[5].resolve({name:'F'});await f;assert.strictEqual(events.at(-1),'F.json');finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_invalid_load_token_sets_unavailable_identity_not_adoption(self):
        # DS3074 fail-closed: a load response with no valid identity
        # must leave an explicit unavailable local context, so the next
        # unscoped read cannot adopt an arbitrary server book. A later
        # explicit reload with a valid token replaces it (recovery).
        source = Path(__file__).resolve().parent.parent / 'static/js/app-scripts.js'
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');const source=fs.readFileSync(process.argv[1],'utf8');
const ctx={document:{getElementById:()=>({style:{}})},console:{error(){}},showConfirm:async()=>true,flushVoiceSaves:async()=>{},ensureCastListEditsDiscardable:async()=>true,showToast(){},showActionError(){},currentBookFilename:''};
ctx.window=ctx;vm.createContext(ctx);
let mode='invalid';const events=[];
ctx.API={post:async(url,body)=>{return mode==='invalid'?{status:'loaded',name:body.name,book_identity_token:null}:{status:'loaded',name:body.name,book_identity_token:'c'.repeat(64)};}};
ctx.applyCurrentBookFilename=name=>{ctx.currentBookFilename=name;ctx.window._selectedUploadIdentity=null;events.push(name);};
for(const name of ['clearCastListEditor','clearCharacterAliases','resetDesignerForm','clearVoiceSuggestions','loadCharacterAliases','loadCastList','loadChunks','refreshVoiceMetadata','loadVoices','loadSavedScripts','loadDesignedVoices']){ctx[name]=async()=>{};}
const core=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInContext(core.slice(core.indexOf('function enqueueBookSelection('),core.indexOf('function getCurrentBookName(')),ctx);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),ctx);vm.runInContext(source.slice(source.indexOf('async function loadScript('),source.indexOf('async function deleteScript(name)')),ctx);
let finished=false;process.on('beforeExit',()=>assert(finished));
(async()=>{
 await ctx.loadScript('B');
 assert.deepStrictEqual(events,['B.json']);
 assert(ctx.window._selectedUploadIdentity&&ctx.window._selectedUploadIdentity.unavailable===true&&ctx.window._selectedUploadIdentity.token===null,'invalid token must set explicit unavailable identity');
 assert.strictEqual(ctx.window._selectedUploadIdentity.filename,'B.json');
 mode='valid';
 await ctx.loadScript('B');
 assert.strictEqual(ctx.window._selectedUploadIdentity.token,'c'.repeat(64),'explicit reload with valid token must recover');
 assert(!ctx.window._selectedUploadIdentity.unavailable);
 finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_load_ack_then_hydration_failure_reports_committed_truth(self):
        # DS3011/DS3018: after the load POST acknowledges, a hydration
        # failure must report loaded-but-unrefreshed, not load failed.
        source = Path(__file__).resolve().parent.parent / 'static/js/app-scripts.js'
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');const source=fs.readFileSync(process.argv[1],'utf8');
const errors=[],toasts=[];
const ctx={document:{getElementById:()=>({style:{}})},console:{error(){}},showConfirm:async()=>true,flushVoiceSaves:async()=>{},ensureCastListEditsDiscardable:async()=>true,showToast:(...args)=>toasts.push(args),showActionError:(...args)=>errors.push(args),currentBookFilename:''};
ctx.window=ctx;vm.createContext(ctx);
ctx.API={post:async(url,body)=>{return {status:'loaded',name:body.name,book_identity_token:'c'.repeat(64)};}};
ctx.applyCurrentBookFilename=name=>{ctx.currentBookFilename=name;ctx.window._selectedUploadIdentity=null;};
for(const name of ['clearCastListEditor','clearCharacterAliases','resetDesignerForm','clearVoiceSuggestions','loadCharacterAliases','loadCastList','loadSavedScripts','loadDesignedVoices']){ctx[name]=async()=>{};}
ctx.loadChunks=async()=>{};ctx.refreshVoiceMetadata=async()=>{throw new Error('hydration exploded');};ctx.loadVoices=async()=>{};
const core=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInContext(core.slice(core.indexOf('function enqueueBookSelection('),core.indexOf('function getCurrentBookName(')),ctx);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),ctx);vm.runInContext(source.slice(source.indexOf('async function loadScript('),source.indexOf('async function deleteScript(name)')),ctx);
let finished=false;process.on('beforeExit',()=>assert(finished));
(async()=>{
 await ctx.loadScript('B');
 // The sliced showActionError routes through showToast: the committed
 // truth message must appear there with the hydration detail.
 assert(toasts.some(message=>String(message[0]).includes('Script loaded, but Voices could not be refreshed')),'committed truth must be surfaced');
 assert(toasts.some(message=>String(message[0]).includes('hydration exploded')),'hydration detail must be preserved');
 finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_hydration_overlaps_but_voices_wait_for_both_and_admission_stays_ordered(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-scripts.js'
        code = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('async function loadScript('),end=source.indexOf('async function deleteScript(name)',start);
assert(start>=0&&end>start);
let finished=false;process.on('beforeExit',()=>assert(finished,'async cases must complete'));
(async()=>{
 for(const failure of ['none','confirm','flush','post','chunks']){
  const calls=[],toasts=[];let aliasDone,chunkDone;
  const context={document:{getElementById:()=>({style:{}})},console:{error:()=>{}},showConfirm:async()=>failure!=='confirm',
   ensureCastListEditsDiscardable:async()=>true,clearCastListEditor(){},loadCastList:async()=>{},flushVoiceSaves:async()=>{calls.push('flush');if(failure==='flush'){throw Error('flush failed');}},
   API:{post:async(url,body)=>{assert.equal(url,'/api/scripts/load');assert.equal(body.name,'book');calls.push('post');if(failure==='post'){throw Error('post failed');}return {name:'book'};}},
   currentBookFilename:'',
   applyCurrentBookFilename:name=>{assert.equal(name,'book.json');calls.push('book');context.currentBookFilename=name;context.window._selectedUploadIdentity=null;},
   clearCharacterAliases:()=>calls.push('clear-aliases'),resetDesignerForm:()=>calls.push('reset'),clearVoiceSuggestions:()=>calls.push('clear-suggestions'),
   showToast:(message,type)=>toasts.push([message,type]),
   loadCharacterAliases:show=>{assert.equal(show,false);calls.push('aliases');return new Promise(resolve=>{aliasDone=resolve;});},
   loadChunks:force=>{assert.equal(force,true);calls.push('chunks');return new Promise((resolve,reject)=>{chunkDone=()=>failure==='chunks'?reject(Error('chunks failed')):resolve();});},
   refreshVoiceMetadata:async()=>calls.push('metadata'),loadVoices:async()=>calls.push('voices'),loadSavedScripts:()=>calls.push('library'),loadDesignedVoices:()=>calls.push('designed')};
  context.window=context;vm.createContext(context);const core=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');vm.runInContext(core.slice(core.indexOf('function enqueueBookSelection('),core.indexOf('function getCurrentBookName(')),context);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),context);vm.runInContext(source.slice(start,end),context);
  const pending=context.loadScript('book');
  for(let i=0;i<20;i++){await Promise.resolve();}
  if(['confirm','flush','post'].includes(failure)){
   await pending;assert(!calls.includes('aliases'));assert(!calls.includes('chunks'));assert(!calls.includes('voices'));
   if(failure!=='confirm'){assert(toasts.some(t=>t[1]==='error'));}continue;
  }
  assert(aliasDone);assert(chunkDone,'chunks must start while alias hydration is gated');
  assert(calls.indexOf('flush')<calls.indexOf('post'));assert(calls.indexOf('post')<calls.indexOf('aliases'));
  assert(calls.indexOf('metadata')<calls.indexOf('chunks'));assert(!calls.includes('voices'));aliasDone();for(let i=0;i<10;i++){await Promise.resolve();}
  assert(!calls.includes('voices'),'voice rendering must also wait for chunks');
  chunkDone();await pending;
  if(failure==='chunks'){assert(!calls.includes('voices'));assert(!calls.includes('library'));assert(toasts.some(t=>t[1]==='error'&&t[0].includes('chunks failed')));}
  else{assert(calls.includes('voices'));assert(calls.indexOf('voices')<calls.indexOf('library'));assert(calls.includes('designed'));}
 }
 finished=true;
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
