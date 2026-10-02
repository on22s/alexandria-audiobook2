"""Native registry discovery and actual reload dispatcher with concurrent task states."""
from pathlib import Path
import json
import subprocess
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script
import core


class TaskDiscoveryTests(unittest.TestCase):
    def test_remote_persona_and_local_voicelab_are_permitted_by_native_admission(self):
        states = {name: {'running': name == 'persona'} for name in core.process_state}
        with patch.object(core, 'process_state', states), patch.object(core, '_task_claims', {}), patch.object(core, 'llm_is_on_this_gpu', return_value=False):
            self.assertNotIn('persona', core.gpu_lock_conflicts('voicelab'))
            core.check_global_gpu_lock('voicelab')
        states = {name: {'running': name == 'voicelab'} for name in core.process_state}
        with patch.object(core, 'process_state', states), patch.object(core, '_task_claims', {}), patch.object(core, 'llm_is_on_this_gpu', return_value=False):
            self.assertNotIn('voicelab', core.gpu_lock_conflicts('persona'))
            core.check_global_gpu_lock('persona')

    def test_native_http_includes_dynamic_tasks_without_handles_or_logs(self):
        states={'review':{'running':True,'logs':['private log'],'process':object()},
                'future_task':{'running':True,'processes':[object()]},'idle':{'running':False}}
        before={name:dict(state) for name,state in states.items()}
        app=FastAPI();app.include_router(script.router)
        with patch.object(script,'process_state',states),TestClient(app) as client:
            res=client.get('/api/status')
            self.assertEqual(200,res.status_code,res.text)
            self.assertEqual({'review':{'running':True},'future_task':{'running':True},'idle':{'running':False}},res.json())
            self.assertEqual(before,states)
            self.assertEqual(404,client.get('/api/status/missing').status_code)


SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-workbench.js'
HARNESS=r"""
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8'),coreSource=fs.readFileSync(process.argv[2],'utf8');
const helperStart=source.indexOf('        function reattachTaskActivity(');
const dispatcherStart=source.indexOf('        async function reattachRunningPollers()');
const code=source.slice(helperStart>=0?helperStart:dispatcherStart,source.indexOf('        // Init',dispatcherStart));
const elements={},calls=[],warnings=[],requests=[];
function el(id){return elements[id]||(elements[id]={disabled:false,style:{display:'none'},innerText:''});}
let statuses={};
const ctx={document:{getElementById:el},console,showToast:(message,type)=>warnings.push([message,type]),
 API:{get:async path=>{requests.push(path);return path==='/api/status'?statuses:{logs:['last log'],running:false,...(statuses[path.split('/').at(-1)]||{})};}},
 _pollScriptBatchLogs:()=>calls.push('batch_script'),scriptBatchPoller:null,
 pollScriptLogs:(task,done)=>calls.push(task),loadReviewBatchScripts:async()=>{},pollReviewBatch:()=>calls.push('batch_review'),
 _disableReviewButtons:()=>{},_showReviewControls:()=>{},_onReviewDone:()=>{},loadCharacterAliases:async()=>{},
 pollPersonaStatus:()=>calls.push('persona'),_vlSetRunning:value=>{el('btn-voicelab-start').disabled=value;},refreshVoicelabHealth:()=>{},pollVoicelab:()=>calls.push('voicelab'),
 pollLoraTraining:()=>calls.push('lora_training'),_pollPreparerLogs:task=>calls.push(task),
 pollLogs:task=>calls.push(task),refreshBenchmarkStatus:()=>calls.push('benchmark'),
 pollExport:task=>calls.push(task),dsbLoadProjects:async name=>calls.push('dataset_builder:'+name),loadVoices:async()=>{},loadDesignedVoices:async()=>{},refreshLmStudioStatus:async()=>{},loadChunks:async()=>{},notifyJobDone:()=>{},
 _startPolling:(key,fetch,options)=>{ctx.poll={key,fetch,options};calls.push(key);}};
vm.createContext(ctx);const claimStart=coreSource.indexOf('const taskStartButtons =');vm.runInContext(coreSource.slice(claimStart,coreSource.indexOf('// --- API Helpers ---',claimStart)),ctx);vm.runInContext(code,ctx);
(async()=>{
"""


class TaskReattachmentJsTests(unittest.TestCase):
    def run_js(self,code):
        result=subprocess.run(['node','-e',HARNESS+code+'\n})().catch(e=>{console.error(e);process.exitCode=1;});',str(SOURCE),str(SOURCE.with_name('app-core.js'))],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)

    def test_remote_persona_and_local_voicelab_both_restore(self):
        self.run_js(r"""
statuses={persona:{running:true},voicelab:{running:true}};await ctx.reattachRunningPollers();
assert.deepStrictEqual(calls,['persona','voicelab']);assert(el('btn-voicelab-start').disabled);assert.strictEqual(requests[0],'/api/status');
assert(!requests.includes('/api/status/persona'));assert(!requests.includes('/api/status/voicelab'));
""")

    def test_training_preparer_audio_benchmark_adapters_and_idle_dispatch(self):
        self.run_js(r"""
for(const task of ['lora_training','preparer','batch_preparer','audio','benchmark']){
 calls.length=0;statuses={[task]:{running:true},review:{running:false}};await ctx.reattachRunningPollers();assert.deepStrictEqual(calls,[task]);
}
assert(el('btn-lora-train').disabled);assert.strictEqual(el('btn-lora-cancel').style.display,'inline-block');assert.strictEqual(el('lora-progress-section').style.display,'block');
statuses={lora_training:{running:false},preparer:{running:false}};calls.length=0;await ctx.reattachRunningPollers();assert.strictEqual(calls.length,0);
""")

    def test_one_failed_adapter_does_not_suppress_other_running_tasks(self):
        self.run_js(r"""
statuses={batch_review:{running:true},voicelab:{running:true}};ctx.loadReviewBatchScripts=async()=>{throw Error('batch unavailable');};
await ctx.reattachRunningPollers();assert.deepStrictEqual(calls,['voicelab']);assert(warnings.some(w=>w[0].includes('batch unavailable')));
ctx.API.get=async()=>{throw Error('registry unavailable');};calls.length=0;await ctx.reattachRunningPollers();
assert.strictEqual(calls.length,0);assert(warnings.some(w=>w[0].includes('registry unavailable')));
""")

    def test_exports_and_builder_restore_exact_owner_and_missing_owner_is_visible(self):
        self.run_js(r"""
for(const task of ['audacity_export','m4b_export','chapter_export']){calls.length=0;statuses={[task]:{running:true}};await ctx.reattachRunningPollers();assert.deepStrictEqual(calls,[task]);}
statuses={dataset_builder:{running:true,dataset_name:'Book 日本語 #'}};calls.length=0;await ctx.reattachRunningPollers();assert.deepStrictEqual(calls,['dataset_builder:Book 日本語 #']);
statuses={dataset_builder:{running:true},voicelab:{running:true}};calls.length=0;await ctx.reattachRunningPollers();assert(calls.includes('voicelab'));assert(warnings.some(w=>w[0].includes('project is unavailable')));
""")

    def test_request_tasks_disable_until_done_and_dynamic_tasks_receive_fallback(self):
        self.run_js(r"""
for(const [task,button,status] of [['voices','btn-suggest-voices','suggest-status'],['llm_test','llm-test-btn','llm-test-result'],['voice_design','btn-design-preview','design-status'],['lmstudio_optimize','lmstudio-optimize-toggle','lmstudio-status-badge']]){
 statuses={[task]:{running:true}};calls.length=0;await ctx.reattachRunningPollers();assert.deepStrictEqual(calls,['reattach:'+task]);assert(el(button).disabled);
 ctx.poll.options.onTick({logs:['<not HTML>'],running:true});assert.strictEqual(el(status).textContent,'<not HTML>');
 await ctx.poll.options.onDone({running:false,logs:[]});assert(!el(button).disabled);assert(el(status).textContent.includes('original request result is unavailable'));
}
for(const task of ['lora_test','drift_check','future_task','constructor','__proto__']){
 statuses=Object.fromEntries([[task,{running:true}]]);calls.length=0;await ctx.reattachRunningPollers();assert.deepStrictEqual(calls,['reattach:'+task]);await ctx.poll.options.onDone({running:false,logs:['worker failed']});
}
""")

    def test_slow_review_hydration_does_not_delay_independent_controls(self):
        self.run_js(r"""
let release;
ctx.loadReviewBatchScripts=()=>new Promise(resolve=>{release=resolve;});
statuses={batch_review:{running:true},voicelab:{running:true},lora_training:{running:true},audacity_export:{running:true}};
const restored=ctx.reattachRunningPollers();
for(let i=0;i<30;i++){await Promise.resolve();}
assert(release,'review hydration started');
assert(calls.includes('voicelab'),'independent Voice Lab must attach before review resolves');
assert(calls.includes('lora_training'));assert(calls.includes('audacity_export'));
assert(!calls.includes('batch_review'));
release();await restored;assert(calls.includes('batch_review'));
""")

    def test_shared_script_log_controls_remain_serialized(self):
        self.run_js(r"""
let release;
ctx.loadReviewBatchScripts=()=>new Promise(resolve=>{release=resolve;});
statuses={batch_review:{running:true},nicknames:{running:true},voicelab:{running:true}};
const restored=ctx.reattachRunningPollers();
for(let i=0;i<30;i++){await Promise.resolve();}
assert(release);assert(!calls.includes('nicknames'),'same log controls remain serialized');
assert(calls.includes('voicelab'));
release();await restored;
assert(calls.indexOf('batch_review')<calls.indexOf('nicknames'));
""")

    def test_every_current_registered_task_gets_a_restoration_or_activity_observer(self):
        # Dispatch coverage only; simultaneous admission is verified separately.
        self.run_js('const names=' + json.dumps(list(core.process_state)) + r""";
statuses=Object.fromEntries(names.map(name=>[name,{running:true,dataset_name:'Book'}]));
await ctx.reattachRunningPollers();assert.strictEqual(calls.length,names.length);assert.deepStrictEqual(warnings,[['drift check is still running.','info'],['report explanation is still running.','info']]);
""")

    def test_report_explanation_reload_uses_activity_observer_until_completion(self):
        self.run_js(r"""
statuses={report_explanation:{running:true}};
await ctx.reattachRunningPollers();assert.deepStrictEqual(calls,['reattach:report_explanation']);
assert.strictEqual(ctx.poll.key,'reattach:report_explanation');
assert(!ctx.poll.options.doneCheck({running:true}));assert(ctx.poll.options.doneCheck({running:false}));
await ctx.poll.fetch();assert(requests.includes('/api/status/report_explanation'));
await ctx.poll.options.onDone({running:false,logs:['Explanation saved.']});
assert.deepStrictEqual(warnings,[['report explanation is still running.','info'],['Explanation saved.','info']]);
""")
