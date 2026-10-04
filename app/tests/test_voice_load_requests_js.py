"""Actual Voices loader overlaps independent reads after saved-edit barrier."""
from pathlib import Path
import os
import subprocess
import unittest

SOURCE = Path(os.environ.get('VOICE_LOAD_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-core.js'))
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const turn=()=>new Promise(setImmediate);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function client(){
 const fields={},reads=[],gates=new Map(),debug=[],draws=[];let saveGate=deferred(),revision=0,dirty=false;
 const el=id=>fields[id]||(fields[id]={value:'',innerHTML:id==='voices-list'?'old cards':'',style:{}});
 const ctx={window:null,performance:{now:()=>0},document:{getElementById:el},console:{debug:(...args)=>debug.push(args)},API:{get:url=>{reads.push(url);const gate=deferred();gates.set(url,gate);return gate.promise;}},
 voiceSaveQueue:{flush:()=>saveGate.promise,getRevision:()=>revision,isDirty:()=>dirty},renderVoiceDrafts(){},refreshVoicesScope(){},updateNarratorPreviewFields(){},renderReadyCount(){},onToggleHideReady(){},renderVoiceSuggestions(){},saveVoicesDebounced(){throw Error('unexpected default save');},
 createVoiceCard:voice=>{draws.push(voice);return `<card>${voice.name}:${ctx._designedVoicesCache[0].id}:${ctx._cloneVoicesCache[0].id}:${ctx._loraModelsCache[0].id}</card>`;}};ctx.window=ctx;
 vm.createContext(ctx);const load=(a,b)=>{const first=source.indexOf(a);vm.runInContext(source.slice(first,source.indexOf(b,first)),ctx);};
 load('async function refreshVoiceMetadata()', 'window.selectVoiceVersion =');load('async function flushVoiceSaves()', 'async function discardVoiceEditsAndReload()');
 vm.runInContext('let _voiceSaveSnapshot=null;let _voiceRecoveryDrafts=[];',ctx);
 ctx.loadCastLibrary=async()=>{const lib=await ctx.API.get('/api/voice_library');ctx._lineCounts={Alice:lib.count};};
 ctx._designedVoicesCache=[{id:'old-design'}];ctx._cloneVoicesCache=[{id:'old-clone'}];ctx._loraModelsCache=[{id:'old-lora'}];
 const snapshot={revision:'0'.repeat(64),book_token:'b'.repeat(64),voices:[{name:'Alice',config:{type:'custom',voice:'Ryan'}}]};
 const resolveAll=()=>{for(const [url,gate]of gates){gate.resolve(url==='/api/voice_config/snapshot'?snapshot:url==='/api/voice_library'?{count:12}:[{id:url}]);}};
 return {ctx,el,reads,gates,debug,draws,saveGate,snapshot,resolveAll,setDirty:()=>dirty=true};
}
'''


class VoiceLoadRequestsJsTests(unittest.TestCase):
    def run_js(self, code):
        script = SETUP + '\nlet finished=false;process.on(\"beforeExit\",()=>assert(finished,\"Loader assertions must finish\"));\n(async()=>{\n' + code + '\nfinished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_all_five_reads_overlap_after_flush_and_cards_wait_for_every_dependency(self):
        self.run_js(r'''
const c=client();const loading=c.ctx.loadVoices();await turn();assert.strictEqual(c.reads.length,0,'pending edits must save before resource reads');assert.strictEqual(c.el('voices-list').innerHTML,'old cards');
c.saveGate.resolve();await turn();assert.strictEqual(new Set(c.reads).size,5,'all independent requests must start before any response');assert.strictEqual(c.reads.length,5);
c.gates.get('/api/voice_config/snapshot').resolve(c.snapshot);c.gates.get('/api/clone_voices/list').resolve([{id:'clone'}]);c.gates.get('/api/lora/models').resolve([{id:'lora'}]);c.gates.get('/api/voice_library').resolve({count:12});await turn();assert.strictEqual(c.draws.length,0);assert.strictEqual(c.el('voices-list').innerHTML,'old cards');
c.gates.get('/api/voice_design/list').resolve([{id:'design'}]);await loading;assert.strictEqual(c.draws.length,1);assert.strictEqual(c.el('voices-list').innerHTML,'<card>Alice:design:clone:lora</card>');assert.strictEqual(c.ctx._lineCounts.Alice,12);assert.deepStrictEqual(c.debug,[]);
''')

    def test_optional_failures_preserve_cached_options_and_successful_metadata_renders(self):
        self.run_js(r'''
const c=client();c.saveGate.resolve();const loading=c.ctx.loadVoices();await turn();assert.strictEqual(c.reads.length,5);
c.gates.get('/api/voice_design/list').reject(Error('optional design unavailable'));c.gates.get('/api/voice_library').reject(Error('optional cast unavailable'));c.gates.get('/api/clone_voices/list').resolve([{id:'clone'}]);c.gates.get('/api/lora/models').resolve([{id:'lora'}]);c.gates.get('/api/voice_config/snapshot').resolve(c.snapshot);const report=await loading;assert.deepStrictEqual([...report.failedResources].sort(),['/api/voice_design/list','/api/voice_library'].sort());
assert.strictEqual(c.el('voices-list').innerHTML,'<card>Alice:old-design:clone:lora</card>');assert.strictEqual(c.debug.length,2);assert.strictEqual(c.ctx._voicesByName.Alice.name,'Alice');
''')

    def test_save_failure_and_concurrent_edit_never_replace_voice_cards(self):
        self.run_js(r'''
let c=client();c.saveGate.promise.catch(()=>{});const rejected=c.ctx.loadVoices();const checked=assert.rejects(rejected,/save unavailable/);c.saveGate.reject(Error('save unavailable'));await turn();assert.strictEqual(c.reads.length,0);await checked;assert.strictEqual(c.el('voices-list').innerHTML,'old cards');
c=client();c.saveGate.resolve();const loading=c.ctx.loadVoices();await turn();c.setDirty();c.resolveAll();await assert.rejects(loading,/Voice edits changed while refreshing/);assert.strictEqual(c.el('voices-list').innerHTML,'old cards');assert.strictEqual(c.draws.length,0);
''')
