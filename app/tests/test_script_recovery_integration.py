"""Real production poller/dispatcher integration for script recovery terminal effects."""
from pathlib import Path
import json
import subprocess
import unittest

RESULTS = []
CORE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
WORKBENCH = Path(__file__).resolve().parent.parent / 'static/js/app-workbench.js'

HARNESS = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8'), wb=fs.readFileSync(process.argv[2],'utf8');
const elements={}, effects={notifications:[],logWrites:[],requests:[],responses:[],posts:[],polls:[],reads:[],releases:[],events:[]};
const event=(type,detail)=>effects.events.push({type,detail,epoch:context?._bookSelectionEpoch});
function el(id){if(id==='toast-container'){return elements[id]||(elements[id]={insertAdjacentHTML(){},addEventListener(){}});}return elements[id]||(elements[id]={disabled:false,style:{display:'none'},innerText:'',textContent:'',classList:{add(){},remove(){},contains(){return false;}},addEventListener(name,fn){this.listeners=this.listeners||{};this.listeners[name]=fn;}});}
const timers=[],unhandled=[];
process.on('unhandledRejection', reason => unhandled.push(String(reason)));
let statuses={};
const context={document:{getElementById:el,hidden:false},console:{error:(...a)=>process.stderr.write('JSERR '+a.map(String).join(' ')+'\n'),debug(){}},
 setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){},
 showToast:(message,type)=>effects.notifications.push(['toast',message,type]),
 showActionError:(action,error,recovery,type)=>effects.notifications.push(['action',action,type]),
 notifyJobDone:(task,id,outcome,state)=>{event('notify',[task,outcome]);effects.notifications.push(['job',task,outcome]);},
 createTaskLogRenderer:element=>state=>{effects.logWrites.push(['logs',element,state.logs||[]]);},
 renderActivity(){},syncPauseButton(){},renderScriptTabProgress(){},renderManualRequest(){},
 getTaskLabel:name=>name,TASK_LABELS:{},isTaskFailed:()=>false,refreshPersonaVoiceResources:async()=>true,
 API:{get:async path=>{
   effects.requests.push(path); event('get',path);
   const response = path==='/api/status' ? statuses
     : path.startsWith('/api/status/') ? (statuses[path.split('/').at(-1)]||{running:false,logs:[]})
     : path.includes('include_detail') ? (effects.reads.push(path),event('read',path),{recoverable:true,detail:{failed_chunk:1,failed_pass:'segment',source:'x',prompt:{system:'s',user:'u'}}})
     : {recoverable:true};
   effects.responses.push([path,response]); return response;
 },post:async(url,data)=>{
   event('post',url);effects.posts.push([url,data]);
   if(url==='/api/generate_script'||url==='/api/generate_script/retry'){
     return {task_claim_id:statuses.script?.task_claim_id||'a'.repeat(32)};
   }
   return {status:'ok',resolution:'accepted'};
 }},
 pollScriptLogs:(task,done)=>{event('observer',task);effects.polls.push(['logs:'+task,task]);},
 _pollScriptBatchLogs:()=>effects.polls.push(['batch','batch_script']),
 pollReviewBatch:()=>effects.polls.push(['review','batch_review']),
 loadReviewBatchScripts:async()=>{},_disableReviewButtons(){},_showReviewControls(){},_onReviewDone(){},
 loadCharacterAliases:async()=>{},pollPersonaStatus:()=>effects.polls.push(['persona','persona']),
 _vlSetRunning(){},refreshVoicelabHealth(){},pollVoicelab(){},pollLoraTraining(){},_pollPreparerLogs(){},
 pollLogs:task=>effects.polls.push(['logs',task]),refreshBenchmarkStatus(){},pollExport(){},
 dsbLoadProjects:async()=>{},loadVoices:async()=>{},loadDesignedVoices:async()=>{},refreshLmStudioStatus:async()=>{},
 loadChunks:async()=>{},attachEditorOutlierCheck(){},reattachTaskActivity(){},claimTaskStart(){},releaseTaskStart(){},
};
context.window=context;
context.bootstrap={Toast:class{constructor(){}show(){}}};
vm.createContext(context);
vm.runInContext('let currentBookFilename="book-A";window._bookSelectionEpoch=0;window._scriptRecoveryBookEpoch=0;window._selectedUploadIdentity=null;let toastSequence=0;let scriptBatchPoller=null;',context);
function coreSlice(start,end){const a=core.indexOf(start);const b=core.indexOf(end,a);assert(a>=0&&b>a,'missing core slice '+start);vm.runInContext(core.slice(a,b),context);}
function wbSlice(start,end){const a=wb.indexOf(start);const b=wb.indexOf(end,a);assert(a>=0&&b>a,'missing workbench slice '+start);vm.runInContext(wb.slice(a,b),context);}
context.t=(key,english,values={})=>String(english).replace(/\{(\w+)\}/g,(m,n)=>Object.hasOwn(values,n)?String(values[n]):m);
coreSlice('function _resetPauseBtn(', '// Same "unknown error"');
coreSlice('function showToast(', 'function showConfirm(');
coreSlice('function escapeHtml(', 'function getInlineStringArgument(');
coreSlice('const _pollGen =', '// A run can pause ITSELF');
coreSlice('async function pollLogs(', '// --- Script tab progress');
coreSlice('function scriptRecoveryContextMatches(', 'function captureScriptRecoveryOwnership()');
coreSlice('function captureScriptRecoveryOwnership()', 'async function onCopyRecoveryPrompt()');
coreSlice('function getTaskOwnershipMessage(', 'function renderActivity(');
Object.assign(context,{getLoadedScriptSourceFilename:()=> 'book-A',ensureScriptStartConfirmed:async()=>true,_scriptStartOver:false,_isStripFrontMatterChecked:()=>false,_isLiteralSourcePreservingChecked:()=>false});
coreSlice("document.getElementById('btn-gen-script').addEventListener('click',", '// Pause is SIGSTOP');
coreSlice('window.retryScriptGeneration =', '// --- Script Batch Mode ---');
coreSlice('const taskStartButtons =', '// --- API Helpers ---');
wbSlice('function reattachTaskActivity(', '// Init');
const originalRelease=context.releaseTaskStart;
context.releaseTaskStart=(name)=>{event('release',name);effects.releases.push(name);return originalRelease(name);};
const detail=()=>({failed_chunk:1,failed_pass:'segment',source:'x',prompt:{system:'s',user:'u'}});
const run=code=>vm.runInContext(code,context);
const tick=async()=>{const fn=timers.shift();assert(fn,'no scheduled poll tick');await fn();};
const settle=async()=>{for(let i=0;i<60;i++){await Promise.resolve();}};
'''


LISTENER_EXTRAS = r"""
coreSlice('        function getLoadedScriptSourceFilename(', '        // Pause is SIGSTOP');
coreSlice('        async function ensureScriptStartConfirmed(', '        // navigator.clipboard');
coreSlice('        function _isLiteralSourcePreservingChecked(', '        let reviewRestartSnapshot =');
coreSlice("document.getElementById('btn-gen-script').addEventListener('click',", '// Pause is SIGSTOP');
context.confirmIfRemote = async () => true;
el('script-first-person-narrator').value = '';
"""


class ScriptRecoveryIntegrationTests(unittest.TestCase):
    def run_js(self, code):
        script = HARNESS + '\n(async()=>{\n' + code + r'''
assert.deepStrictEqual(unhandled,[]);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(CORE), str(WORKBENCH)],
                                capture_output=True, text=True, timeout=20)
        for line in result.stdout.splitlines():
            if line.startswith('RESULT '):
                RESULTS.append(json.loads(line[len('RESULT '):]))
        self.assertEqual(0, result.returncode, result.stderr)

    def test_32_real_poller_observer_terminal_orders(self):
        self.run_js(r'''
const orders=[];
const sources=['reattach','unconfirmed','logs','terminal'];
for(const first of sources) for(const second of sources) for(const third of sources){
 if(first===second||second===third||first===third) continue;
 if(orders.length<24) orders.push([first,second,third]);
}
for(const first of sources) for(const second of sources){
 if(first===second) continue;
 if(orders.length<32) orders.push([first,second]);
}
for(const order of orders){
 for(const key of Object.keys(effects)){effects[key].length=0;}
 timers.length=0;
 statuses={script:{running:true,ownership_unconfirmed:true,logs:['live']},persona:{running:false}};
 context._scriptRecoveryRunActive=false;context._scriptRecoveryAcceptedRun=0;
 context._scriptTerminalCleanup=null;
 context.reattachTaskActivity('script',[],'script-logs',null,
  {running:true,ownership_unconfirmed:true,logs:['live']});
 await settle();
 const token=context._scriptRecoveryRunToken;
 assert.strictEqual(context.getScriptAdmission(token).state,'accepted');
 let expectedPollers=1;
 for(const source of order){
  if(source==='logs'||source==='reattach'){
   const key=source==='logs'?'logs:script':'reattach:script';
   context._startPolling(key,async()=>({running:false,logs:['terminal']}),{doneCheck:s=>!s.running});
   expectedPollers++;
  } else if(source==='terminal'){
   context.onScriptTaskTerminal({running:false,status:'failed',logs:['terminal']},token,source);
  } else {
   context.reattachTaskActivity('script',[],'script-logs',null,
    {running:true,ownership_unconfirmed:true,logs:['live']});
   expectedPollers++;
  }
  await settle();
  if(timers.length){await tick();}
 }
 context.onScriptTaskTerminal({running:false,status:'failed',logs:['terminal']},token,'final');
 await settle();
 const jobs=effects.notifications.filter(n=>n[0]==='job').length;
 const releases=effects.releases.filter(n=>n==='script').length;
 assert(jobs>=1,'at least one accepted run must be observed '+order.join(','));
 assert.strictEqual(jobs,effects.reads.length,'one recovery read per job notification');
 assert.strictEqual(jobs,releases,'one claim release per job notification');
 assert(effects.requests.length>=1,'script observer fetched status');
 assert.deepStrictEqual(unhandled,[]);
 if(!context._scriptRecoveryRunActive){
  assert.strictEqual(el('btn-gen-script').disabled,false,'controls restored after terminal');
 }
 const finalJobs=effects.notifications.filter(n=>n[0]==='job').length;
 const finalReleases=effects.releases.filter(n=>n==='script').length;
 console.log('RESULT '+JSON.stringify({order,expected:{jobsPerAcceptedRun:1,readsPerJob:1,releasesPerJob:1},
  observed:{jobs:finalJobs,reads:effects.reads.length,releases:finalReleases,polls:[...effects.polls],
   requests:[...effects.requests],posts:[...effects.posts],notifications:[...effects.notifications],
   controls:{genDisabled:el('btn-gen-script').disabled},epoch:context._bookSelectionEpoch},
  failures:[]}));
}
''')
        target = Path.home() / '.cache/aa2-2241-matrix'
        target.mkdir(parents=True, exist_ok=True)
        (target / 'observer-orders.json').write_text(json.dumps({
            'cases': len(RESULTS), 'detail': RESULTS,
        }, indent=2), encoding='utf-8')
        self.assertGreaterEqual(len(RESULTS), 32)

    def test_persona_survives_script_selection_change(self):
        scenario = r'''
statuses={script:{running:true,logs:['live']},persona:{running:true,logs:['persona live']}};
context._scriptRecoveryRunToken=0;context._scriptRecoveryRunActive=false;
context._scriptRecoveryAcceptedRun=0;context._scriptAdmissions={};context._scriptAdmissionSeq=0;
context._scriptAdmissionsByToken={};context._scriptTerminalDeliveries={};
context._bookSelectionEpoch=1;
const original=context.API.get.bind(context.API);
let observedStale=false;
context.API.get=async path=>{
 const response=await original(path);
 if(path==='/api/status/script'&&context._bookSelectionEpoch!==1){observedStale=true;}
 return response;
};
context.reattachRunningPollers();
await settle();
assert(effects.polls.some(p=>p[0]==='logs:script')||effects.requests.includes('/api/status/script'),'script poller attached');
assert(effects.requests.includes('/api/status/persona')||effects.polls.some(p=>p[1]==='persona'),'persona observer attached');
context._bookSelectionEpoch=2;
statuses={script:{running:false,status:'failed',logs:['terminal']},persona:{running:false,logs:['persona done']}};
for(let i=0;i<timers.length;i++){await tick();}
assert(!observedStale,'stale script response must not mutate new selection');
assert.strictEqual(effects.notifications.filter(n=>n[0]==='job'&&n[1]==='persona').length,1,'one persona completion notification');
assert.strictEqual(effects.releases.filter(n=>n==='persona').length,1,'one persona claim release');
assert(effects.requests.filter(p=>p==='/api/status/persona').length>=1,'persona poller remains attached');
console.log('RESULT '+JSON.stringify({expected:{personaObservers:1,duplicates:0},
 observed:{requests:[...effects.requests],events:[...effects.events],polls:[...effects.polls],
  notifications:[...effects.notifications],releases:[...effects.releases]},failures:[]}));
'''
        script = (HARNESS.replace("pollPersonaStatus:()=>effects.polls.push(['persona','persona']),", "")
                  + "\nvm.runInContext(core.slice(core.indexOf('        async function pollPersonaStatus('), core.indexOf('        // --- Voices Tab ---')), context);\n"
                  + '\n(async()=>{\n' + scenario + '\n})().catch(e=>{console.error(e);process.exitCode=1;});\n')
        captured = []
        original = subprocess.run
        def spy(args, **kwargs):
            completed = original(args, **kwargs)
            for line in completed.stdout.splitlines():
                if line.startswith('RESULT '):
                    captured.append(json.loads(line[len('RESULT '):]))
            return completed
        subprocess.run = spy
        try:
            result = subprocess.run(['node','-e',script,str(CORE),str(WORKBENCH)],capture_output=True,text=True,timeout=20)
        finally:
            subprocess.run = original
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertNotIn('JSERR', result.stderr, 'persona completion handler must not error')
        target = Path.home() / '.cache/aa2-2241-matrix'
        target.mkdir(parents=True, exist_ok=True)
        (target/'persona-cases.json').write_text(json.dumps({'cases':len(captured),'detail':captured},indent=2),encoding='utf-8')
        self.assertEqual(len(captured),1)

    def test_named_and_three_way_registered_listener_schedules(self):
        from tests.test_script_recovery_matrix import MATRIX, NAMED, ROWS
        defaults = {name: values[0] for name, values in MATRIX}
        scenarios = [dict(defaults, **value, name=key) for key, value in NAMED]
        scenarios += [row for row in ROWS if row['name'].startswith('3way:')]
        detail = []
        for scenario in scenarios:
            setup = LISTENER_EXTRAS + '''
statuses={script:{running:true,logs:['live']},persona:{running:false}};
el('file-upload').files=[];
const row=ROWSPEC;
run('window._scriptStartOver=' + String(row.initiator==='startover'));
const click=el('btn-gen-script').listeners.click;
if(row.initiator==='retry'){await context.refreshScriptRecovery();await run('window.retryScriptGeneration()');}else{await click();}
await settle();
if(timers.length){await tick();}
await settle();
const jobs=effects.notifications.filter(n=>n[0]==='job').length;
const expected={posts:1,unhandled:0,
 notices: 0,
 active: row.outcome==='success'||row.initiator==='retry',
 genDisabled: row.outcome==='success'||row.initiator==='retry'};
assert.strictEqual(effects.posts.length,expected.posts,'exact POST count');
assert.strictEqual(unhandled.length,expected.unhandled);
assert.strictEqual(effects.notifications.filter(n=>n[0]==='action').length,expected.notices,'exact notification count');
assert.strictEqual(el('btn-gen-script').disabled,!!context._scriptRecoveryRunActive,'Generate control follows admission');
assert(effects.events.length>=2,'ordered effects recorded');
console.log('RESULT '+JSON.stringify({name:row.name,input:row,expected,
 observed:{posts:[...effects.posts],requests:[...effects.requests],events:[...effects.events],
  reads:effects.reads.length,notifications:[...effects.notifications],releases:[...effects.releases],
  polls:[...effects.polls],logWrites:[...effects.logWrites],
  controls:{genDisabled:el('btn-gen-script').disabled,cancel:el('btn-cancel-script').style.display,
   pause:el('btn-pause-script').style.display},
  admission:{active:!!context._scriptRecoveryRunActive,accepted:context._scriptRecoveryAcceptedRun||0,
   generation:context._scriptRecoveryGeneration},epoch:context._bookSelectionEpoch},failures:[]}));
''' .replace('ROWSPEC', json.dumps(scenario))
            script = HARNESS + '\n(async()=>{\n' + setup + "\n})().catch(e=>{console.error(e);process.exitCode=1;});\n"
            captured = []
            original = subprocess.run
            def run_capture(args, **kwargs):
                completed = original(args, **kwargs)
                for line in completed.stdout.splitlines():
                    if line.startswith('RESULT '):
                        captured.append(json.loads(line[len('RESULT '):]))
                return completed
            subprocess.run = run_capture
            try:
                result = subprocess.run(['node','-e',script,str(CORE),str(WORKBENCH)],
                                  capture_output=True,text=True,timeout=20)
            finally:
                subprocess.run = original
            self.assertEqual(0,result.returncode,f'{scenario["name"]}: {result.stderr}')
            detail.extend(captured)
        target = Path.home() / '.cache/aa2-2241-matrix'
        target.mkdir(parents=True,exist_ok=True)
        (target/'listener-cases.json').write_text(json.dumps({'cases':len(detail),'detail':detail},indent=2),encoding='utf-8')
        self.assertEqual(len(detail),len(scenarios))

    def test_listener_case_detects_reservation_mutation(self):
        source = CORE.read_text(encoding='utf-8')
        original = "const runToken = window.reserveScriptRecoveryRun('generate');"
        self.assertIn(original, source)
        target = Path.home() / '.cache/aa2-2241-matrix'
        target.mkdir(parents=True, exist_ok=True)
        mutant = target / 'mutant-listener-reservation.js'
        mutant.write_text(source.replace(original, 'const runToken = 0;', 1), encoding='utf-8')
        scenario = (
            "el('file-upload').files=[];\n"
            "await el('btn-gen-script').listeners.click();\n"
            "await settle();\n"
            "assert(context._scriptRecoveryRunToken>0,'listener reservation lost');\n"
        )
        script = HARNESS + LISTENER_EXTRAS + '\n(async()=>{\n' + scenario + '\n})().catch(e=>{console.error(e);process.exitCode=1;});\n'
        result = subprocess.run(['node','-e',script,str(mutant),str(WORKBENCH)],capture_output=True,text=True,timeout=20)
        self.assertNotEqual(0,result.returncode,'listener case did not detect reservation mutation')


if __name__ == '__main__':
    unittest.main()
