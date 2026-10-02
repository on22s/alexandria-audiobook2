"""Actual render/review polling callbacks report worker and per-item outcomes."""
from pathlib import Path
import os
import subprocess
import unittest

from tests.test_chunk_refresh_js import SETUP

SOURCE = Path(os.environ.get('BATCH_COMPLETION_SOURCE', Path(__file__).resolve().parent.parent / 'static/js/app-core.js'))
RENDER_SETUP = r'''
let poll,snapshot=[],audio={running:true};const toasts=[],drifts=[],requests=[];
ctx.ensureEditorRenderSnapshot=async()=>[1,2,3,4,5,6].map(id=>chunk(id));
ctx.showToast=(message,tone)=>toasts.push({message,tone});ctx.showConfirm=async()=>true;
ctx.cancelRender=()=>run('isRenderingAll=false');ctx.runDriftCheck=ids=>drifts.push(Array.from(ids));
ctx._startPolling=(key,fetch,options)=>{assert.strictEqual(key,'render_batch');poll={fetch,...options};};ctx.API.post=async()=>({});
ctx.API.get=async url=>{requests.push(url);if(url==='/api/status/audio'){return audio;}assert.strictEqual(url,'/api/chunks');return snapshot;};
if(source.includes('function getBatchOutcome(')){run(source.slice(source.indexOf('function getBatchOutcome('),source.indexOf('function pollReviewBatch()')));}
run(source.slice(source.indexOf('async function _runBatchRender('),source.indexOf('window.renderAll =')));
const startBatch=()=>ctx._runBatchRender('/fixture',false,{label:'fixture',describeStart:()=>''});
'''


class BatchCompletionJsTests(unittest.TestCase):
    def run_js(self, code, setup=SETUP + RENDER_SETUP):
        script = setup + '\n(async()=>{\n' + code + '\n})().catch(e=>{console.error(e);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(SOURCE)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_pending_start_and_done_chunks_do_not_finish_before_worker_stops(self):
        self.run_js(r'''
await startBatch();snapshot=[1,2,3,4,5,6].map(id=>chunk(id));
let data=await poll.fetch();assert.strictEqual(poll.doneCheck(data),false,'pending startup still belongs to running worker');assert.strictEqual(timers.size,0);
snapshot=snapshot.map(c=>({...c,status:'done'}));data=await poll.fetch();assert.strictEqual(poll.doneCheck(data),false,'done chunks alone do not end worker lifecycle');
audio={running:false};data=await poll.fetch();assert.strictEqual(poll.doneCheck(data),true);await poll.onDone(data);
assert.deepStrictEqual(toasts,[{message:'Batch complete: 6 succeeded, 0 failed, 0 cancelled, 0 unfinished',tone:'success'}]);assert.deepStrictEqual(drifts,[[1,2,3,4,5,6]]);
assert.deepStrictEqual(requests,Array(3).fill(['/api/status/audio','/api/chunks']).flat());assert.strictEqual(timers.size,0);
''')

    def test_terminal_partial_results_count_pending_skipped_missing_and_cancelled(self):
        self.run_js(r'''
await startBatch();audio={running:false};snapshot=[chunk(1,'done'),chunk(2,'error'),chunk(3,'cancelled'),chunk(4,'skipped'),chunk(5,'pending'),chunk(999,'done')];
const data=await poll.fetch();assert.strictEqual(poll.doneCheck(data),true);await poll.onDone(data);
assert.deepStrictEqual(toasts,[{message:'Batch incomplete: 1 succeeded, 1 failed, 1 cancelled, 3 unfinished',tone:'warning'}]);assert.deepStrictEqual(drifts,[[1]]);
await startBatch();snapshot=[];const empty=await poll.fetch();await poll.onDone(empty);assert.strictEqual(toasts.at(-1).message,'Batch incomplete: 0 succeeded, 0 failed, 0 cancelled, 6 unfinished');assert.strictEqual(drifts.length,1);
''')

    def test_unavailable_status_rejects_poll_and_manual_cancel_does_not_show_success(self):
        self.run_js(r'''
await startBatch();audio={};await assert.rejects(poll.fetch(),/Audio task status is unavailable/);assert.deepStrictEqual(requests,['/api/status/audio']);assert.strictEqual(toasts.length,0);assert.strictEqual(run('isRenderingAll'),true);
audio={running:true};snapshot=[chunk(1,'pending')];const data=await poll.fetch();ctx.cancelRender();assert.strictEqual(poll.doneCheck(data),true);await poll.onDone(data);assert.strictEqual(toasts.length,0);assert.strictEqual(drifts.length,0);
''')

    def test_actual_review_callback_reports_counts_and_preserves_recovery_controls(self):
        setup = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const fields={},recovery=[],notifications=[];let poll;
const el=id=>fields[id]||(fields[id]={style:{},disabled:true,innerHTML:''});
const ctx={document:{getElementById:el},API:{},createTaskLogRenderer:()=>()=>{},_startPolling:(key,fetch,options)=>{assert.strictEqual(key,'batch_review');poll=options;},_showTaskRecoveryPanel:(...args)=>recovery.push(args),notifyJobDone:task=>notifications.push(task)};
vm.createContext(ctx);const begin=source.includes('function getBatchOutcome(')?source.indexOf('function getBatchOutcome('):source.indexOf('function pollReviewBatch()');
vm.runInContext(source.slice(begin,source.indexOf('// --- Persona Generation ---',begin)),ctx);
'''
        self.run_js(r'''
ctx.pollReviewBatch();
for(const [statuses,cancel,label,counts] of [
 [['done','failed','incomplete','pending'],false,'incomplete','1 completed, 1 failed, 0 cancelled, 2 unfinished'],
 [['done','done'],false,'complete','2 completed, 0 failed, 0 cancelled, 0 unfinished'],
 [['done','cancelled','pending'],true,'stopped','1 completed, 0 failed, 1 cancelled, 1 unfinished'],
 [[],false,'incomplete','0 completed, 0 failed, 0 cancelled, 0 unfinished']]){
 const state={running:false,cancel,tasks:statuses.map(status=>({status}))};const before=JSON.stringify(state);poll.onDone(state);
 assert.strictEqual(el('review-batch-status-msg').innerHTML,`<span class="${label==='complete'?'text-success':'text-warning'}">Batch review ${label}: ${counts}.</span>`);
 assert.strictEqual(el('btn-review-batch-start').disabled,false);assert.strictEqual(el('btn-pause-batch-review').style.display,'none');assert.strictEqual(el('btn-cancel-batch-review').style.display,'none');
 assert.strictEqual(recovery.at(-1)[2],state);assert.strictEqual(notifications.at(-1),'batch_review');assert.strictEqual(JSON.stringify(state),before);
}
assert.strictEqual(recovery.length,4);assert.strictEqual(notifications.length,4);
''', setup)
