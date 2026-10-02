"""Actual Dataset Builder handlers, API client, and polling engine under start failures."""
import unittest
from tests import test_dataset_ui_ownership_js as fixture

SETUP=r"""
// Use the actual HTTP client and shared polling engine; fake only transport/time.
context.Response=Response;
run(core.slice(core.indexOf('        const API = {'),core.indexOf('        // --- Setup Tab ---')));
run(core.slice(core.indexOf('        const _pollGen = {}'),core.indexOf('        // A run can pause ITSELF')));
context.showToast=()=>{};let notifications=0;context.notifyJobDone=()=>notifications++;
context.showConfirm=async()=>true;
run("dsbCurrentProject='A';dsbRows=[{text:'one',emotion:'warm',seed:0,status:'done',audio_url:'/old.wav'},{text:'two',emotion:'sad',seed:'',status:'error',audio_url:null},{text:'three',emotion:'',seed:'',status:'pending',audio_url:null}];");
context.document.getElementById('dsb-description').value='root';
const flush=async()=>{await new Promise(resolve=>setImmediate(resolve));};
"""


class DatasetBatchStartTests(unittest.TestCase):
    run_scenario=fixture.DatasetUiOwnershipJsTests.run_scenario

    def test_http_rejection_restores_original_statuses_and_idle_server_unlocks_without_notification(self):
        self.run_scenario(SETUP+r"""
const before=plain(run('dsbRows'));let resolveStatus;const requests=[];
context.fetch=(url,request)=>{requests.push([url,request]);return request?Promise.resolve(new Response(JSON.stringify({detail:'GPU occupied'}),{status:400})):new Promise(resolve=>resolveStatus=resolve);};
await context.dsbGenerateAll(true);
assert.deepStrictEqual(plain(run('dsbRows')),before);assert(run('dsbBatchRunning'));assert.strictEqual(elements['dsb-btn-cancel'].style.display,'none');
assert.strictEqual(requests.length,2);assert.strictEqual(requests[0][0],'/api/dataset_builder/generate_batch');assert.deepStrictEqual(JSON.parse(requests[0][1].body).indices,[0,1,2]);
resolveStatus(new Response(JSON.stringify({running:false,samples:before,logs:[]})));await flush();
assert.deepStrictEqual(plain(run('dsbRows')),before);assert(!run('dsbBatchRunning'));assert.strictEqual(elements['dsb-btn-gen-all'].style.display,'');assert.strictEqual(elements['dsb-btn-cancel'].style.display,'none');assert.strictEqual(notifications,0);
""")

    def test_lost_start_response_reconciles_running_job_then_real_completion(self):
        self.run_scenario(SETUP+r"""
let active=true;context.fetch=async(url,request)=>{if(request){throw new TypeError('connection lost');}return new Response(JSON.stringify({running:active,samples:[{text:'one',status:active?'generating':'done',audio_url:active?null:'/new.wav'}],logs:[]}));};
await context.dsbGenerateAll(true);await flush();assert(run('dsbBatchRunning'));assert.strictEqual(elements['dsb-btn-cancel'].style.display,'');assert.strictEqual(run('dsbRows[0].status'),'generating');
active=false;await flushTimers();await flush();assert(!run('dsbBatchRunning'));assert.strictEqual(run('dsbRows[0].status'),'done');assert.strictEqual(run('dsbRows[0].audio_url'),'/new.wav');assert.strictEqual(notifications,1);
""")

    def test_pending_duplicate_is_blocked_and_old_response_cannot_attach_to_new_project(self):
        self.run_scenario(SETUP+r"""
for(const rejected of [false,true]){
 let settle;const requests=[];context.fetch=(url,request)=>{requests.push(url);return new Promise(resolve=>settle=resolve);};
 run("dsbCurrentProject='A';dsbRows=[{text:'A',emotion:'',seed:'',status:'pending',audio_url:null}];");
 const pending=context.dsbGenerateAll();await flush();assert(run('dsbBatchRunning'));assert.strictEqual(elements['dsb-btn-cancel'].style.display,'none');
 await context.dsbGenerateAll();assert.strictEqual(requests.length,1);
 run("dsbStopBatch();dsbCurrentProject='B';dsbRows=[{text:'B',status:'done',audio_url:'/B.wav'}];");const before=plain(run('dsbRows'));
 settle(new Response(JSON.stringify(rejected?{detail:'rejected'}:{status:'started'}),{status:rejected?400:200}));await pending;await flush();
 assert.strictEqual(requests.length,1);assert.deepStrictEqual(plain(run('dsbRows')),before);assert(!run('dsbBatchRunning'));assert.strictEqual(elements['dsb-btn-gen-all'].style.display,'');
}
""")

    def test_status_failure_keeps_edits_blocked_until_retry_reconciles_and_decline_does_not_post(self):
        self.run_scenario(SETUP+r"""
let gets=0,posts=0;context.fetch=async(url,request)=>{if(request){posts++;return new Response(JSON.stringify({detail:'rejected'}),{status:400});}if(++gets===1){throw Error('status offline');}return new Response(JSON.stringify({running:false,samples:[],logs:[]}));};
await context.dsbGenerateAll();await flush();assert(run('dsbBatchRunning'));assert.strictEqual(elements['dsb-btn-cancel'].style.display,'none');
await flushTimers();await flush();assert(!run('dsbBatchRunning'));assert.strictEqual(gets,2);assert.strictEqual(notifications,0);
const before=plain(run('dsbRows'));context.showConfirm=async()=>false;await context.dsbGenerateAll(true);assert.strictEqual(posts,1);assert.deepStrictEqual(plain(run('dsbRows')),before);
""")

    def test_idle_project_load_normalizes_previously_running_controls(self):
        self.run_scenario(SETUP+r"""
run('dsbBatchRunning=true;');for(const id of ['dsb-btn-gen-all','dsb-btn-regen-all']){context.document.getElementById(id).style.display='none';}context.document.getElementById('dsb-btn-cancel').style.display='';context.fetch=async()=>new Response(JSON.stringify({running:false,description:'root',samples:[{text:'idle',status:'done'}]}));
await context.dsbLoadProject('A');assert(!run('dsbBatchRunning'));assert.strictEqual(elements['dsb-btn-gen-all'].style.display,'');assert.strictEqual(elements['dsb-btn-regen-all'].style.display,'');assert.strictEqual(elements['dsb-btn-cancel'].style.display,'none');
""")

    def test_idle_status_load_during_pending_start_cannot_unlock_duplicate_submission(self):
        self.run_scenario(SETUP+r"""
let resolveStart,posts=0;context.fetch=(url,request)=>{if(request){posts++;return new Promise(resolve=>resolveStart=resolve);}return Promise.resolve(new Response(JSON.stringify({running:false,description:'root',samples:[{text:'one',status:'pending'}],logs:[]})));};
const start=context.dsbGenerateAll();await flush();await context.dsbLoadProject('A');assert(run('dsbBatchRunning'));assert.strictEqual(elements['dsb-btn-gen-all'].style.display,'none');
await context.dsbGenerateAll();assert.strictEqual(posts,1);resolveStart(new Response(JSON.stringify({status:'started'})));await start;await flush();assert(!run('dsbBatchRunning'));
""")
