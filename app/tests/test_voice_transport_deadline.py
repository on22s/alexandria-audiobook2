"""Actual voice transport expiry retains drafts and stops book replacement."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests import test_voice_drafts as drafts


HELPERS = r'''
const scripts=fs.readFileSync(process.argv[3],'utf8');
const turn=()=>new Promise(resolve=>setImmediate(resolve));
async function bounded(promise){
 let timer;
 try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('actual action did not settle after its deadline')),150);})]);}
 finally{clearTimeout(timer);}
}
function useTimers(c){
 const timers=new Map();let sequence=0;
 c.setTimeout=(fn,delay)=>{const id=++sequence;timers.set(id,{fn,delay});return id;};
 c.clearTimeout=id=>timers.delete(id);
 return timers;
}
function getSnapshot(c){return vm.runInContext('JSON.stringify(_voiceSaveSnapshot)',c);}
function loadAction(c){
 vm.runInContext(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')),c);
 vm.runInContext(scripts.slice(scripts.indexOf('async function loadScript(name)'),scripts.indexOf('async function deleteScript(name)')),c);
 c.ensureCastListEditsDiscardable=async()=>{throw Error('failed save must block book selection');};
}
'''


class VoiceTransportDeadlineTests(unittest.TestCase):
    def run_node(self, code):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ['node', '-e', drafts.HARNESS + HELPERS + code, str(drafts.SOURCE),
                 str(Path(tmp) / 'drafts.json'), str(drafts.SOURCE.with_name('app-scripts.js'))],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_fetch_and_both_response_body_stalls_retain_drafts_and_block_load(self):
        self.run_node(r'''
(async()=>{
 for(const stalled of ['fetch','body','errorBody']){
  const storage=storageFor(process.argv[2]);let mode=stalled,release,loadPosts=0;
  const requests=[];
  const a=client(storage,null);
  a.context.fetch=async(url,options)=>{
   if(!options){return{ok:true,json:async()=>({revision:revision(0),book_token:book,book_id:'synthetic',voices:[],config:{}})};}
   if(url==='/api/scripts/load'){loadPosts++;throw Error('unexpected load');}
   assert.strictEqual(url,'/api/voice_config/save');requests.push(options);
   const success={ok:true,json:async()=>({book_token:book,revision:revision(1)})};
   if(mode==='normal'){return success;}
   if(mode==='conflict'){return{ok:false,status:409,statusText:'Conflict',json:async()=>({detail:'Voice configuration changed; reload it before saving'})};}
   if(mode==='fetch'){return await new Promise(resolve=>{release=()=>resolve(success);});}
   return{ok:mode!=='errorBody',status:503,statusText:'Unavailable',json:()=>new Promise(resolve=>{release=()=>resolve(mode==='errorBody'?{detail:'late error'}:{book_token:book,revision:revision(1)});})};
  };
  await a.context.refreshVoiceMetadata();loadAction(a.context);
  const timers=useTimers(a.context);a.context.saveVoicesDebounced();
  const originalDraft=fs.readFileSync(process.argv[2]),snapshot=getSnapshot(a.context);
  let saveError;const save=a.context.flushVoiceSaves().catch(error=>{saveError=error;});
  const load=a.context.loadScript('another synthetic book');await turn();
  assert.strictEqual(loadPosts,0);assert.strictEqual(requests.length,1);
  const deadlines=[...timers.values()].filter(item=>item.delay===60000);
  assert.strictEqual(deadlines.length,1,'voice save must own one finite 60-second deadline');
  deadlines[0].fn();await bounded(Promise.all([save,load]));
  assert(saveError.message.includes('timed out'));assert(requests[0].signal.aborted);
  assert(a.status.innerHTML.includes('save failed'));assert(a.errors.some(error=>error.includes('timed out')));
  assert(a.context.isDirty());assert.strictEqual(loadPosts,0);assert.strictEqual(getSnapshot(a.context),snapshot);
  assert.deepStrictEqual(fs.readFileSync(process.argv[2]),originalDraft);
  assert(![...timers.values()].some(item=>item.delay===60000),'expiry retires its timer');
  release();await turn();await turn();
  assert(a.context.isDirty(),'late ACK cannot acknowledge an expired write');
  assert.strictEqual(getSnapshot(a.context),snapshot);assert.deepStrictEqual(fs.readFileSync(process.argv[2]),originalDraft);
  mode='conflict';await assert.rejects(a.context.flushVoiceSaves(),/configuration changed/);
  assert(a.context.isDirty());assert.strictEqual(JSON.parse(requests[1].body).revision,revision(0));
  assert.deepStrictEqual(fs.readFileSync(process.argv[2]),originalDraft);
  mode='normal';await a.context.flushVoiceSaves();
  assert(!a.context.isDirty());assert.strictEqual(storage.length,0);assert.strictEqual(loadPosts,0);
 }
 finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_success_http_and_json_failures_clear_deadlines_without_changing_transport(self):
        self.run_node(r'''
(async()=>{
 const storage=storageFor(process.argv[2]),a=client(storage,null),timers=useTimers(a.context),requests=[];
 let mode='ok';
 a.context.fetch=async(url,options)=>{
  requests.push({url,options});
  return{ok:mode!=='http',status:422,statusText:'Unprocessable',json:async()=>{
   if(mode==='json'){throw Error('invalid JSON');}
   return mode==='http'?{detail:{message:'synthetic refusal'}}:{answer:'confirmed'};
  }};
 };
 const invoke=options=>vm.runInContext(`API.post('/synthetic', {seed:0}, ${options})`,a.context);
 assert.strictEqual((await invoke('{timeoutMs:60000}')).answer,'confirmed');assert.strictEqual(timers.size,0);
 assert(!requests[0].options.signal.aborted);assert.strictEqual(JSON.parse(requests[0].options.body).seed,0);
 mode='http';await assert.rejects(invoke('{timeoutMs:60000}'),error=>error.status===422&&error.detail.message==='synthetic refusal');assert.strictEqual(timers.size,0);
 mode='json';await assert.rejects(invoke('{timeoutMs:60000}'),/invalid JSON/);assert.strictEqual(timers.size,0);
 mode='ok';assert.strictEqual((await invoke('undefined')).answer,'confirmed');
 assert(!Object.hasOwn(requests.at(-1).options,'signal'),'ordinary callers keep their existing transport');
 for(const value of ['0','-1','Infinity','NaN','true']){
  const count=requests.length;await assert.rejects(invoke(`{timeoutMs:${value}}`),/positive finite/);assert.strictEqual(count,requests.length);
 }
 finished=true;
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


if __name__ == '__main__':
    unittest.main()
