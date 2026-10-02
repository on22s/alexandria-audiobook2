"""Run the four real core log callbacks on bounded windows and growing logs."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const elements={},polls={},pause=[],manual=[],activity=[];let notices=0;
function element(id){if(elements[id]){return elements[id];}let text='';const stats={replaces:0,appends:0,scrolls:0};const el={value:'',style:{},stats,scrollHeight:42};
Object.defineProperty(el,'innerText',{get:()=>text,set:value=>{text=value;stats.replaces++;}});Object.defineProperty(el,'scrollTop',{set:()=>stats.scrolls++});el.appendChild=node=>{text+=node.textContent;stats.appends++;};elements[id]=el;return el;}
const ctx={window:null,console,Date,document:{getElementById:element,querySelector:()=>null,createTextNode:text=>({textContent:text}),createElement:()=>({textContent:''})},API:{get:async()=>({})},
 _startPolling:(key,fetch,options)=>polls[key]={fetch,...options},syncPauseButton:(...args)=>pause.push(args),syncSnapshotButton:()=>{},renderManualRequest:(...args)=>manual.push(args),renderActivity:(...args)=>activity.push(args),
 _updateReviewBatchTotals:()=>{},notifyJobDone:()=>notices++,_showTaskRecoveryPanel:()=>{},loadSavedScripts:()=>{},loadVoices:async()=>{},showToast:()=>{},loadChunks:()=>{}};ctx.window=ctx;
vm.createContext(ctx);const run=code=>vm.runInContext(code,ctx);
function load(start,end){const a=source.indexOf(start);assert(a>=0);const b=end?source.indexOf(end,a):source.length;assert(b>a);run(source.slice(a,b));}
load('const taskStartButtons =','// --- API Helpers ---');
if(source.includes('function getTaskLogUpdate(')){load('function getTaskLogUpdate(', '// --- Setup Tab ---');}
load('function _pollScriptBatchLogs()', '// --- Single review');
load('function pollReviewBatch()', '// --- Persona Generation ---');
load('async function pollPersonaStatus()', '// --- Voices Tab ---');
load('async function pollLogs(');
const cases=[['batch_script','script-logs',()=>ctx._pollScriptBatchLogs()],['batch_review','script-logs',()=>ctx.pollReviewBatch()],['persona','voices-logs',()=>ctx.pollPersonaStatus()],['logs:script','script-logs',()=>ctx.pollLogs('script','script-logs',null,'activity')]];
'''


class CoreLogRenderJsTests(unittest.TestCase):
    def run_js(self, code):
        script = SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_all_pollers_append_once_skip_unchanged_and_reset_rotations_and_runs(self):
        self.run_js(r'''
for(const [key,id,start] of cases){
 await start();const el=element(id),tick=polls[key].onTick;const before={...el.stats};const logs=['<img src=x onerror="boom()">','same'];
 tick({run_id:'one',running:true,logs});assert.strictEqual(el.innerText,logs.join('\n'));assert.strictEqual(el.stats.replaces,before.replaces+1);
 const pauses=pause.length,manuals=manual.length,activities=activity.length;
 for(let i=0;i<10;i++){const same=logs.slice();same.join=()=>{throw Error('unchanged full join');};tick({run_id:'one',running:true,logs:same});}
 assert.strictEqual(el.stats.replaces,before.replaces+1);assert.strictEqual(el.stats.appends,before.appends);assert.strictEqual(el.stats.scrolls,before.scrolls+1);
 if(key!=='persona'){assert.strictEqual(pause.length,pauses+10);}if(key==='persona'||key==='logs:script'){assert.strictEqual(manual.length,manuals+10);}if(key==='logs:script'){assert.strictEqual(activity.length,activities+10);}
 tick({run_id:'one',running:true,logs:[...logs,'new']});assert.strictEqual(el.innerText,logs.join('\n')+'\nnew');assert.strictEqual(el.stats.appends,before.appends+1);assert.strictEqual(el.style.whiteSpace,'pre-wrap');
 for(const current of [['same','new','newer'],['newer'],['corrected'],[]]){tick({run_id:'one',running:true,logs:current});assert.strictEqual(el.innerText,current.join('\n'));}
 tick({run_id:'two',running:true,logs:['new run']});assert.strictEqual(el.innerText,'new run');assert(!polls[key].doneCheck({running:true}));assert(polls[key].doneCheck({running:false}));
 await start();polls[key].onTick({start_time:3,running:true,logs:['reattached']});assert.strictEqual(el.innerText,'reattached');
}
''')

    def test_batch_ring_eviction_never_hides_new_lines_at_the_same_or_shorter_length(self):
        self.run_js(r'''
ctx._pollScriptBatchLogs();const tick=polls.batch_script.onTick,el=element('script-logs');
for(const logs of [['a','b','c'],['b','c','d'],['c','d','e'],['e','f'],['f','g']]){tick({start_time:1,running:true,logs});assert.strictEqual(el.innerText,logs.join('\n'));}
tick({start_time:2,running:true,logs:['new run first']});assert.strictEqual(el.innerText,'new run first');
''')

    def test_growing_ten_thousand_line_log_only_appends_new_text(self):
        self.run_js(r'''
for(const [key,id,start] of cases){await start();const el=element(id),tick=polls[key].onTick,before={...el.stats};const logs=Array.from({length:10000},(_,i)=>'line '+i);tick({run_id:'large',running:true,logs:logs.slice()});
for(let i=0;i<100;i++){logs.push('tail '+i);const next=logs.slice();next.join=()=>{throw Error('full accumulated join');};tick({run_id:'large',running:true,logs:next});}
assert.strictEqual(el.stats.replaces,before.replaces+1);assert.strictEqual(el.stats.appends,before.appends+100);assert.strictEqual(el.innerText,logs.join('\n'));}
''')
