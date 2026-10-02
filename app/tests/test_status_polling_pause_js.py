"""Deferred status handlers preserve request bounds and server action state."""
from pathlib import Path
import subprocess
import unittest

STATIC=Path(__file__).resolve().parent.parent/'static/js'
SETUP=r'''const assert=require('assert'),fs=require('fs'),vm=require('vm');
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function classes(initial=[]){const set=new Set(initial);return {add:x=>set.add(x),remove:x=>set.delete(x),contains:x=>set.has(x)};}
let finished=false;process.on('beforeExit',()=>assert(finished,'handler test must finish its final assertions'));
'''

class StatusPollingPauseJsTests(unittest.TestCase):
    def run_js(self,script):
        result=subprocess.run(['node','-e',SETUP+script,str(STATIC)],capture_output=True,text=True,timeout=20)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_pending_poll_bounds_independence_release_and_rendering(self):
        self.run_js(r'''const source=fs.readFileSync(process.argv[1]+'/app-workbench.js','utf8');
const elements={},calls=[];for(const id of ['sys-gpu-val','sys-build-val','sys-build','sys-gpu','sys-disk-val','sys-disk','sys-eta','sys-eta-val','stale-build-banner']){elements[id]={style:{},textContent:'',title:'',classList:classes()};}
const context={document:{getElementById:id=>elements[id],querySelector:()=>({content:'old123'})},API:{get:url=>{const d=deferred();calls.push({url,...d});return d.promise;}},console:{error(){}}};
vm.createContext(context);vm.runInContext(source.slice(source.indexOf('const PAGE_BUILD ='),source.indexOf('async function refreshLmStudioStatus()')),context);
const stats={runtime:{short_revision:'new456',revision:'new456full',branch:'main',python:'3.10',packages:{torch:'cpu',missing:null}},gpu:{reserved_gb:2,total_gb:8,allocated_percent:95},disk:{free_gb:12,low_space:true}};
(async()=>{
 const system=context.updateSystemStats();const eta=context.updateEtaStatus();
 for(let i=0;i<30;i++){await context.updateSystemStats();await context.updateEtaStatus();}
 assert.deepStrictEqual(calls.map(c=>c.url),['/api/system/stats','/api/status/eta']);
 calls[1].resolve({running:true,label:'Review',progress:'2/10',eta_seconds:90});await eta;
 assert.strictEqual(elements['sys-eta-val'].textContent,'Review — 2/10 (ETA 1m 30s)');assert.strictEqual(elements['sys-eta'].style.display,'flex');
 const idle=context.updateEtaStatus();assert.strictEqual(calls.length,3);calls[2].resolve({running:false});await idle;assert.strictEqual(elements['sys-eta'].style.display,'none');
 const elapsed=context.updateEtaStatus();calls[3].resolve({running:true,label:'Training',elapsed_seconds:3660});await elapsed;
 assert.strictEqual(elements['sys-eta-val'].textContent,'Training (running 1h 1m)');
 calls[0].resolve(stats);await system;
 assert.strictEqual(elements['sys-build-val'].textContent,'build new456');assert(elements['sys-build'].title.includes('torch cpu'));assert(elements['sys-build'].title.includes('Revision: new456full'));
 assert.strictEqual(elements['stale-build-banner'].style.display,'block');assert.strictEqual(elements['sys-gpu-val'].textContent,'2.0/8.0 GB');assert(elements['sys-gpu'].classList.contains('text-danger'));
 assert.strictEqual(elements['sys-disk-val'].textContent,'12 GB');assert(elements['sys-disk'].classList.contains('text-danger'));
 for(const name of ['updateSystemStats','updateEtaStatus']){
  const pending=context[name]();const index=calls.length-1;for(let i=0;i<5;i++){await context[name]();}assert.strictEqual(calls.length,index+1);
  calls[index].reject(new Error('offline'));await pending;
  const retry=context[name]();assert.strictEqual(calls.length,index+2);calls[index+1].resolve(name==='updateSystemStats'?{...stats,gpu:null,disk:{free_gb:100,low_space:false}}:{running:true,label:'Working',eta_seconds:null});await retry;
 }
 assert.strictEqual(elements['sys-gpu-val'].textContent,'N/A');assert(elements['sys-disk'].classList.contains('text-light'));assert.strictEqual(elements['sys-eta-val'].textContent,'Working');
 const mismatch=context.updateSystemStats();calls.at(-1).resolve({...stats,gpu_mismatch:true,gpu_mismatch_vendor:'AMD'});await mismatch;assert.strictEqual(elements['sys-gpu-val'].textContent,'CPU fallback!');assert(elements['sys-gpu'].title.includes('AMD'));
 finished=true;
})().catch(error=>{finished=true;console.error(error);process.exitCode=1;});''')

    def test_all_pause_handlers_read_fresh_server_state_before_action(self):
        self.run_js(self.get_pause_setup()+r'''(async()=>{
 for(const [task,btnId,handler,prefix] of cases){for(const paused of [false,true]){
  const s=setup(paused,btnId);const pending=s.context.window[handler]();
  assert.strictEqual(s.gets.length,1);assert.strictEqual(s.gets[0].url,'/api/status/'+task);assert.strictEqual(s.posts.length,0);assert.strictEqual(s.btn.disabled,true);
  await s.context.window[handler]();assert.strictEqual(s.gets.length,1);s.gets[0].resolve({paused,running:true});await pending;
  assert.deepStrictEqual(s.posts,[prefix+(paused?'/resume':'/pause')]);assert.strictEqual(s.btn.disabled,false);
  assert.strictEqual(s.btn.classList.contains('btn-outline-success'),!paused);assert(s.btn.innerHTML.includes(paused?'Pause':'Resume'));
 }}
 for(const [task,btnId,handler] of cases){
  const s=setup(false,btnId);const pending=s.context.window[handler]();s.gets[0].reject(new Error('status offline'));await pending;
  assert.strictEqual(s.posts.length,0);assert.strictEqual(s.timers.length,0);assert.strictEqual(s.btn.disabled,false);assert(s.toasts[0][0].includes('status'));assert(s.toasts[0][0].includes('offline'));
  s.btn.disabled=true;await s.context.window[handler]();assert.strictEqual(s.gets.length,1);assert.strictEqual(s.posts.length,0);
 }
 finished=true;
})().catch(error=>{finished=true;console.error(error);process.exitCode=1;});''')

    def test_post_only_503_retry_limit_and_error_cleanup(self):
        self.run_js(self.get_pause_setup()+r'''(async()=>{
 for(const [task,btnId,handler,prefix] of cases){for(const paused of [false,true]){for(const failures of [2,3]){
  const s=setup(paused,btnId);let count=0;s.context.API.post=async url=>{s.posts.push(url);if(count++<failures){throw {status:503,message:'startup'};}return {};};
  const pending=s.context.window[handler]();s.gets[0].resolve({paused,running:true});await pending;
  assert.strictEqual(s.gets.length,1);assert.deepStrictEqual(s.posts,Array(3).fill(prefix+(paused?'/resume':'/pause')));assert.deepStrictEqual(s.timers,[700,700]);assert.strictEqual(s.btn.disabled,false);
  if(failures===3){assert(s.toasts[0][0].startsWith(paused?'Resume failed':'Pause failed'));}else{assert.strictEqual(s.toasts.length,0);}
 }}}
 const [task,btnId,handler,prefix]=cases[0];const s=setup(true,btnId);s.context.API.post=async url=>{s.posts.push(url);throw {status:400,message:'refused'};};
 const pending=s.context.window[handler]();s.gets[0].resolve({paused:true});await pending;assert.deepStrictEqual(s.posts,[prefix+'/resume']);assert.strictEqual(s.timers.length,0);assert.strictEqual(s.btn.disabled,false);assert(s.toasts[0][0].includes('Resume failed: refused'));
 const status=setup(false,btnId);const reading=status.context.window[handler]();status.gets[0].reject({status:503,message:'status startup'});await reading;assert.strictEqual(status.posts.length,0);assert.strictEqual(status.timers.length,0);assert.strictEqual(status.gets.length,1);
 finished=true;
})().catch(error=>{finished=true;console.error(error);process.exitCode=1;});''')

    def get_pause_setup(self):
        return r'''const source=fs.readFileSync(process.argv[1]+'/app-core.js','utf8');const vl=fs.readFileSync(process.argv[1]+'/app-voicelab.js','utf8');
const factory=source.slice(source.indexOf('function _makePauseResumeHandler('),source.indexOf('const _scriptPauseResume'));
const mapping=source.slice(source.indexOf('const PAUSE_BUTTON_FOR_TASK ='),source.indexOf('const _autoPauseNotified'));
const declarations=[...source.matchAll(/const\s+_\w+PauseResume\s*=\s*_makePauseResumeHandler\([\s\S]*?;/g)].map(match=>match[0]).join('\n');
const exports=[...source.matchAll(/window\.pauseResume\w+\s*=\s*_\w+PauseResume;/g)].map(match=>match[0]).join('\n');
const vlStart=vl.slice(vl.indexOf('const _voicelabPauseResume'),vl.indexOf('function _vlSetCheckIcon'));
const cases=[['script','btn-pause-script','pauseResumeScript','/api/generate_script'],['batch_script','btn-pause-batch-script','pauseResumeBatchScript','/api/generate_script/batch'],
 ['review','btn-pause-review','pauseResumeReview','/api/review_script'],['batch_review','btn-pause-batch-review','pauseResumeBatchReview','/api/review_script/batch'],
 ['nicknames','btn-pause-nick','pauseResumeNicknames','/api/find_nicknames'],['voicelab','btn-vl-pause','pauseResumeVoicelab','/api/voicelab']];
function setup(serverPaused,btnId){
 const btn={disabled:false,innerHTML:'original',classList:classes(serverPaused?['btn-outline-warning']:['btn-outline-success'])};const gets=[],posts=[],timers=[],toasts=[];
 const context={window:{},document:{getElementById:id=>{assert.strictEqual(id,btnId);return btn;}},
 API:{get:url=>{const d=deferred();gets.push({url,...d});return d.promise;},post:async url=>{posts.push(url);return {}; }},
 setTimeout:(fn,ms)=>{timers.push(ms);fn();},showToast:(...args)=>toasts.push(args)};
 vm.createContext(context);vm.runInContext(factory+mapping+declarations+exports+vlStart,context);return {context,btn,gets,posts,timers,toasts};
}
'''
