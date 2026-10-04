import subprocess
import unittest
from tests.test_voice_load_requests_js import SETUP, SOURCE

class PersonaRefreshReuseTests(unittest.TestCase):
    def test_completed_reads_and_failure_retries(self):
        code=SETUP+r'''
let finished=false;process.on("beforeExit",()=>assert(finished,"All asynchronous assertions must finish"));
(async()=>{
const a=source.indexOf('let personaVoiceRefreshRequest ='),b=source.indexOf('async function pollPersonaStatus()',a);assert(a>=0&&b>a);
for(const mode of ['success','design','metadata','save']){
const c=client();c.saveGate.promise.catch(()=>{});
c.ctx.currentBookFilename='A';vm.runInContext(source.slice(a,b),c.ctx);const pending=c.ctx.refreshPersonaVoiceResources();
if(mode==='save'){c.saveGate.reject(Error('save failed'));}else{c.saveGate.resolve();}
await turn();if(mode==='design'){c.gates.get('/api/voice_design/list').reject(Error('design failed'));}
if(mode==='metadata'){c.gates.get('/api/voice_config/snapshot').reject(Error('metadata failed'));}
c.resolveAll();await turn();c.resolveAll();await turn();c.resolveAll();const complete=await pending;assert.equal(complete,mode==='success'||mode==='design');assert.equal(c.el('persona-refresh-retry').hidden,complete);
assert.equal(c.reads.length,{success:5,design:6,metadata:7,save:2}[mode]);
const count=path=>c.reads.filter(p=>p===path).length;
assert.equal(count('/api/voice_design/list'),mode==='design'||mode==='metadata'?2:1);
assert.equal(count('/api/clone_voices/list'),mode==='metadata'?2:1);
assert.equal(c.ctx._designedVoicesCache[0].id,'/api/voice_design/list');assert.equal(c.ctx._cloneVoicesCache[0].id,'/api/clone_voices/list');
assert.equal(c.draws.length,mode==='metadata'||mode==='save'?0:1);
}
finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'''
        r=subprocess.run(['node','-e',code,str(SOURCE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,r.returncode,r.stderr)

    def test_refresh_retry_preserves_cache_and_ownership(self):
        code=SETUP+r'''const fields={};const el=id=>fields[id]||(fields[id]={textContent:'',hidden:true,disabled:false});let loads=0,resolve,apiReads=0;
const ctx={currentBookFilename:'A',document:{getElementById:el},console:{debug(){}},loadVoices:async()=>{loads++;throw Error('metadata internal error');},API:{get:async()=>{apiReads++;return{malformed:true};}},_designedVoicesCache:[{id:'old-design'}],_cloneVoicesCache:[{id:'old-clone'}]};ctx.window=ctx;vm.createContext(ctx);const a=source.indexOf('let personaVoiceRefreshRequest ='),b=source.indexOf('async function pollPersonaStatus()',a);vm.runInContext(source.slice(a,b),ctx);
let finished=false;process.on('beforeExit',()=>assert(finished));(async()=>{assert.strictEqual(await ctx.refreshPersonaVoiceResources(),false);assert(!el('persona-refresh-retry').hidden);assert(!el('persona-refresh-status').textContent.includes('internal'));assert.strictEqual(ctx._designedVoicesCache[0].id,'old-design');assert.strictEqual(ctx._cloneVoicesCache[0].id,'old-clone');
ctx.loadVoices=()=>{loads++;return new Promise(done=>resolve=done);};const pending=ctx.refreshPersonaVoiceResources();const readsBefore=apiReads;const count=loads;await ctx.refreshPersonaVoiceResources();assert.strictEqual(loads,count);assert(el('persona-refresh-retry').disabled);ctx.currentBookFilename='B';resolve({refreshedResources:[],failedResources:[]});assert.strictEqual(await pending,false);assert.strictEqual(apiReads,readsBefore,'changed book must not dispatch stale fallback reads');assert.match(el('persona-refresh-status').textContent,/book changed/);assert(!el('persona-refresh-retry').disabled);assert.strictEqual(ctx._designedVoicesCache[0].id,'old-design');
ctx.loadVoices=async()=>({refreshedResources:['/api/voice_design/list','/api/clone_voices/list'],failedResources:[]});assert.strictEqual(await ctx.refreshPersonaVoiceResources(),true);assert(el('persona-refresh-retry').hidden);assert.strictEqual(el('persona-refresh-status').textContent,'');finished=true;})().catch(e=>{console.error(e);process.exitCode=1;});'''
        r=subprocess.run(['node','-e',code,str(SOURCE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,r.returncode,r.stderr)
