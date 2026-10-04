"""Real recovery callbacks and notifications distinguish neutral summaries and pauses."""
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const elements={},notifications=[];let poll;
function el(id){if(!elements[id]){const classes=new Set();elements[id]={style:{},value:'',innerHTML:'',innerText:'',checked:false,classList:{contains:name=>classes.has(name),add:name=>classes.add(name),remove:name=>classes.delete(name)}};}return elements[id];}
function Notice(title,options){notifications.push({title,...options});}Notice.permission='granted';
const ctx={window:null,currentBookFilename:'book.txt',Notification:Notice,document:{getElementById:el,visibilityState:'hidden',hasFocus:()=>false},console,API:{get:async()=>[]},
 _startPolling:(key,fetch,options)=>poll={key,...options},showToast:()=>{},renderManualRequest:()=>{},loadVoices:async()=>({refreshedResources:['/api/voice_design/list','/api/clone_voices/list'],failedResources:[]}),_resetPauseBtn:()=>{},createTaskLogRenderer:()=>()=>{}};ctx.window=ctx;
vm.createContext(ctx);function load(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert(a>=0&&b>a);vm.runInContext(source.slice(a,b),ctx);}
load('function escapeHtml(', '// Parse a numeric input');
load('const taskStartButtons =','// --- API Helpers ---');
if(source.includes('function isTaskFailed(')){load('function isTaskFailed(', '// --- Desktop notifications ---');}
load('const TASK_LABELS =', '// --- Navigation ---');
load('function _showReviewControls(', "document.getElementById('btn-review-script').addEventListener");
load('function _showTaskRecoveryPanel(', 'function pollReviewBatch()');
load('let personaVoiceRefreshRequest =', 'async function pollPersonaStatus()');
load('async function pollPersonaStatus()', '// --- Voices Tab ---');
load('const PAUSE_BUTTON_FOR_TASK =', '// What a run is waiting on');
'''


class TaskOutcomeJsTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', SETUP + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});', str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_all_three_recovery_callers_reject_zero_failure_and_negated_summaries(self):
        self.run_js(r'''
for(const logs of [['Batch finished: 4 completed, 0 failed'],['No errors'],['No errors or failures'],['0 section(s) failed'],['Batches failed: 0'],['0 batch(es) failed'],['0 failures'],['This review completed without recorded failed or skipped sections.'],['Error calling LLM API (attempt 1)','Task persona completed successfully.']]){
const status={running:false,logs};ctx._onReviewDone(status);assert.strictEqual(el('review-recovery-panel').style.display,'none',logs.join('|'));
ctx._showTaskRecoveryPanel('task-panel','nicknames',status,'retry');assert.strictEqual(el('task-panel').style.display,'none');
el('persona-recovery-panel').open=false;await ctx.pollPersonaStatus();await poll.onDone(status);assert.strictEqual(el('persona-recovery-panel').open,false);assert.strictEqual(el('btn-cancel-personas').style.display,'none');
}
''')

    def test_real_failures_incomplete_batches_and_errors_after_success_remain_visible(self):
        self.run_js(r'''
for(const status of [{logs:['Error: invalid source']},{logs:['0 failed, 2 errors']},{logs:['Batches failed: 1']},{logs:['Task review failed with return code 1.']},{logs:['Task review completed, but 1 section(s) could not be reviewed.']},{logs:['Task persona completed successfully.','Error: saved output missing']},{logs:['0 failed'],tasks:[{status:'failed'}]},{logs:['no errors'],tasks:[{status:'incomplete'}]}]){
ctx._onReviewDone(status);assert.strictEqual(el('review-recovery-panel').style.display,'');ctx._showTaskRecoveryPanel('task-panel','nicknames',status,'retry');assert.strictEqual(el('task-panel').style.display,'');
el('persona-recovery-panel').open=false;await ctx.pollPersonaStatus();await poll.onDone(status);assert.strictEqual(el('persona-recovery-panel').open,true);assert.match(el('persona-recovery-context').textContent,/Stage: persona generation/);
}
''')

    def test_auto_pause_title_is_paused_and_notification_is_not_repeated(self):
        self.run_js(r'''
const status={paused:true,running:true,logs:['[AUTO-PAUSE] retries exhausted']};ctx.syncPauseButton('script',status);assert.strictEqual(notifications.length,1);assert.strictEqual(notifications[0].title,'Script generation paused');assert.match(notifications[0].body,/Press Resume/);ctx.syncPauseButton('script',status);assert.strictEqual(notifications.length,1);
ctx.notifyJobDone('review');assert.strictEqual(notifications[1].title,'Script review finished');
ctx.document.visibilityState='visible';ctx.document.hasFocus=()=>true;ctx.notifyJobDone('review');assert.strictEqual(notifications.length,2);
''')

    def test_persona_completion_notifies_even_when_refresh_fails(self):
        self.run_js(r'''
ctx.loadVoices=async()=>{throw Error('fixture refresh failure');};await ctx.pollPersonaStatus();await poll.onDone({running:false,logs:['Task persona completed successfully.']});assert.strictEqual(notifications.length,1);assert.strictEqual(notifications[0].title,'Persona generation finished');assert.match(el('persona-refresh-status').textContent,/could not be fully refreshed/);assert.strictEqual(el('persona-refresh-retry').hidden,false);assert.strictEqual(el('persona-refresh-retry').disabled,false);
ctx.document.visibilityState='visible';ctx.document.hasFocus=()=>true;await ctx.pollPersonaStatus();await poll.onDone({running:false,logs:[]});assert.strictEqual(notifications.length,1);
''')
