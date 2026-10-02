import subprocess
import unittest
from tests.test_voice_load_requests_js import SETUP, SOURCE

class PersonaRefreshReuseTests(unittest.TestCase):
    def test_completed_reads_and_failure_retries(self):
        code=SETUP+r'''
let finished=false;process.on("beforeExit",()=>assert(finished,"All asynchronous assertions must finish"));
(async()=>{
const a=source.indexOf('let refreshedResources = [];'),b=source.indexOf('showToast(failed ?',a);assert(a>=0&&b>a);
for(const mode of ['success','design','metadata','save']){
const c=client();c.saveGate.promise.catch(()=>{});
const pending=vm.runInContext('(async()=>{'+source.slice(a,b)+'})()',c.ctx);
if(mode==='save'){c.saveGate.reject(Error('save failed'));}else{c.saveGate.resolve();}
await turn();if(mode==='design'){c.gates.get('/api/voice_design/list').reject(Error('design failed'));}
if(mode==='metadata'){c.gates.get('/api/voice_config/snapshot').reject(Error('metadata failed'));}
c.resolveAll();await turn();c.resolveAll();await turn();c.resolveAll();await pending;
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
