"""Execute actual Voice Lab handlers with delayed HTTP responses in Node."""
from pathlib import Path
import subprocess
import unittest


HARNESS = r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const elements={};
function element(id){return elements[id]||(elements[id]={value:'',checked:false,disabled:false,style:{display:'none'},innerHTML:'',innerText:'',scrollHeight:0});}
const ctx={window:null,document:{getElementById:element,querySelectorAll:()=>[{value:'name'}]},
 API:{},_makePauseResumeHandler:()=>()=>{},_resetPauseBtn:()=>{},showToast:()=>{},
 showConfirm:async()=>true,escapeHtml:x=>String(x),cancelTask:()=>{},notifyJobDone:()=>{},
 _startPolling:(key,fetch,options)=>{ctx.poll={key,fetch,options};},console,Date};ctx.window=ctx;
element('vl-zips_dir').value='A';
for(const [id,value] of Object.entries({'vl-target-loss':'4.15','vl-max-epochs':'6','vl-lora-r':'64','vl-candidate-checkpoints':'2'})){element(id).value=value;}
vm.createContext(ctx);
const core=fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-core.js'),'utf8');
const helper=core.slice(core.indexOf('        function getTaskLogUpdate('),core.indexOf('        // --- Setup Tab ---'));
vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),ctx);vm.runInContext(helper,ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
const flush=async()=>{for(let i=0;i<8;i++){await Promise.resolve();}};
(async()=>{
"""


class VoicelabRunStateJsTests(unittest.TestCase):
    def run_js(self, code):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-voicelab.js'
        script = HARNESS + code + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
        result = subprocess.run(['node', '-e', script, str(source), str(source.parent.parent / 'index.html')], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_pending_start_hides_controls_and_failure_restores_start(self):
        self.run_js(r"""
const pre=deferred(),start=deferred();let preCalls=0,startCalls=0;
ctx.API.post=(path)=>{if(path.endsWith('/preflight')){preCalls++;return pre.promise;}startCalls++;return start.promise;};
const attempt=ctx.startVoicelab();await flush();
assert(element('btn-vl-start').disabled);assert.strictEqual(element('btn-vl-cancel').style.display,'none');
assert.strictEqual(element('btn-vl-start').textContent,'Preflighting…');assert(element('voicelab-status').textContent.includes('Checking configuration'));
await ctx.startVoicelab();assert.strictEqual(preCalls,1,'duplicate preflight/start refused');
pre.resolve({ready:true,stages:['name'],preflight_id:'id'});await flush();
assert.strictEqual(startCalls,1);assert.strictEqual(element('btn-vl-pause').style.display,'none');
assert.strictEqual(element('btn-vl-cancel').style.display,'none','no cancel before registration');
start.resolve({zips_dir:'/resolved/A'});await attempt;
assert.strictEqual(element('btn-vl-cancel').style.display,'inline-block');assert(element('btn-vl-start').disabled);
assert(ctx.poll);
// A later rejected start restores controls and leaves a visible error.
ctx._vlSetRunning(false);ctx.API.post=async path=>{if(path.endsWith('/preflight')){return {ready:true,stages:['name'],preflight_id:'id'};}throw Error('rejected');};
await ctx.startVoicelab();assert(!element('btn-vl-start').disabled);assert.strictEqual(element('btn-vl-cancel').style.display,'none');
assert(element('voicelab-status').innerHTML.includes('rejected'));
ctx.API.post=async()=>({ready:false,stages:['name']});await ctx.startVoicelab();assert(!element('btn-vl-start').disabled);assert(element('voicelab-status').textContent.includes('blocked'));
ctx.API.post=async()=>({ready:true,stages:['name']});ctx.showConfirm=()=>false;await ctx.startVoicelab();assert(!element('btn-vl-start').disabled);assert(element('voicelab-status').textContent.includes('cancelled'));
ctx.API.post=async()=>{throw Error('offline');};await ctx.startVoicelab();assert(!element('btn-vl-start').disabled);assert(element('voicelab-status').textContent.includes('configured paths, stages and numeric fields'));
""")

    def test_completion_inspects_run_root_and_empty_status_clears_badges(self):
        self.run_js(r"""
const paths=[];ctx.API.get=async path=>{paths.push(path);return {manifest:{},quality:{},narrator_count:0};};
ctx.pollVoicelab('/resolved/A');
element('vl-zips_dir').value='B';ctx.poll.options.onDone({running:false,status:'done'});await flush();
assert(paths.includes('/api/voicelab/inspect?zips_dir=%2Fresolved%2FA'));assert(!paths.some(x=>x.includes('zips_dir=B')));
// A reattached poll gets its root from server status, not from the form.
paths.length=0;ctx.pollVoicelab();ctx.poll.options.onTick({running:true,zips_dir:'/server/A',tasks:null});
ctx.poll.options.onDone({running:false,status:'done'});await flush();
assert(paths.includes('/api/voicelab/inspect?zips_dir=%2Fserver%2FA'));
// Manual Inspect still follows the editable form.
paths.length=0;await ctx.voicelabInspect();assert(paths.includes('/api/voicelab/inspect?zips_dir=B'));
""")

    def test_log_append_and_rotation_preserve_reader_position(self):
        self.run_js(r"""
ctx.document.createTextNode=text=>({textContent:text});const el=element('voicelab-logs');el.clientHeight=100;el.scrollHeight=1000;el.scrollTop=0;el.appendChild=node=>{el.innerText+=node.textContent;};
ctx.pollVoicelab('A');const tick=ctx.poll.options.onTick;
tick({run_id:'one',running:true,logs:['a','b']});assert.strictEqual(el.scrollTop,1000);
el.scrollTop=200;tick({run_id:'one',running:true,logs:['a','b','c']});assert.strictEqual(el.innerText,'a\nb\nc');assert.strictEqual(el.scrollTop,200);
tick({run_id:'one',running:true,logs:['b','c','d']});assert.strictEqual(el.innerText,'b\nc\nd');assert.strictEqual(el.scrollTop,200);
el.scrollTop=880;tick({run_id:'one',running:true,logs:['b','c','d','e']});assert.strictEqual(el.scrollTop,1000);
""")

    def test_empty_status_clears_previous_stage_badges(self):
        self.run_js(r"""
ctx.pollVoicelab('A');
ctx.poll.options.onTick({running:true,tasks:[{name:'train',status:'done'}]});
assert.strictEqual(element('vl-stage-progress').style.display,'flex');
ctx.poll.options.onTick({running:true,tasks:[]});
assert.strictEqual(element('vl-stage-progress').style.display,'none');assert.strictEqual(element('vl-stage-progress').innerHTML,'');

""")

    def test_health_is_single_flight_periodic_and_fresh_at_completion(self):
        self.run_js(r"""
const first=deferred(),second=deferred();let healthCalls=0,active=0,maxActive=0;
ctx.API.get=async path=>{assert.strictEqual(path,'/api/voicelab/health');healthCalls++;active++;maxActive=Math.max(active,maxActive);
 try{return await (healthCalls===1?first.promise:second.promise);}finally{active--;}};
const one=ctx.refreshVoicelabHealth(),duplicate=ctx.refreshVoicelabHealth();await flush();assert.strictEqual(healthCalls,1);
const final=ctx.refreshVoicelabHealth(true);await flush();assert.strictEqual(healthCalls,1);
first.resolve({status:'running'});await flush();assert.strictEqual(healthCalls,2);
second.resolve({status:'ok'});await Promise.all([one,duplicate,final]);assert.strictEqual(maxActive,1);
assert(element('vl-health-body').innerHTML.includes('>ok<'),'final health supersedes earlier running response');
""")

    def test_health_polling_uses_one_coordinated_response_per_tick(self):
        self.run_js(r"""
let statusCalls=0;ctx.API.get=async path=>{
 assert.strictEqual(path,'/api/status/voicelab?include_health=true');statusCalls++;
 return {running:statusCalls<=7,tasks:[],health:{status:statusCalls<=7?'running':'ok'}};};
ctx.pollVoicelab('A');
for(let i=0;i<8;i++){const s=await ctx.poll.fetch();ctx.poll.options.onTick(s);}
// Completion health is already in the final status response.
await flush();assert.strictEqual(statusCalls,8);
assert(element('vl-health-body').innerHTML.includes('>ok<'));
""")
