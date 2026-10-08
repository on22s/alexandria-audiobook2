"""Exercise delayed Dataset Builder callbacks with actual frontend functions."""

from pathlib import Path
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "static/js/app-workbench.js"


class DatasetUiOwnershipJsTests(unittest.TestCase):
    def test_project_switch_cannot_publish_old_rows_or_metadata_while_destination_loads(self):
        self.run_scenario(r'''
let finished=false;process.on('beforeExit',()=>assert(finished,'switch assertions must finish'));const warnings=[];context.showToast=(...args)=>warnings.push(args);
run("dsbCurrentProject='A';dsbLoadedProject='A';dsbRows=[{text:'A row',emotion:'warm',seed:0,status:'pending'}]");context.document.getElementById('dsb-project-select').value='B';let resolve;context.API.get=()=>new Promise(done=>resolve=done);const loading=context.dsbOnProjectChange();await new Promise(done=>setImmediate(done));
context.dsbUpdateRow(0,'text','edited A');run('dsbSaveRows();dsbSaveForm()');await flushTimers();assert.strictEqual(posts.length,0);assert.strictEqual(run('dsbRows[0].text'),'A row');assert(warnings.at(-1)[0].includes('load'));
resolve({description:'B voice',samples:[{text:'B row',emotion:'cold',seed:2,status:'pending'}],running:false});await loading;assert.strictEqual(run('dsbLoadedProject'),'B');context.dsbUpdateRow(0,'text','edited B');await flushTimers();assert.strictEqual(posts.length,1);assert.strictEqual(posts[0].data.name,'B');assert.strictEqual(posts[0].data.rows[0].text,'edited B');finished=true;
''')

    def test_status_responses_cannot_overwrite_pending_or_completed_optimization(self):
        self.run_scenario(r'''
let finished=false;process.on('beforeExit',()=>assert(finished,'status assertions must finish'));
run(source.slice(source.indexOf('async function refreshLmStudioStatus()'),source.indexOf('function reattachTaskActivity(')));
const pending=[];let finish;context.API.get=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));context.API.post=()=>new Promise(resolve=>finish=resolve);context.showToast=()=>{};
const toggle=context.document.getElementById('lmstudio-optimize-toggle'),badge=context.document.getElementById('lmstudio-status-badge');toggle.checked=true;
const old=context.refreshLmStudioStatus();const optimizing=context.toggleLmStudioOptimize();const count=pending.length;await context.refreshLmStudioStatus();assert.strictEqual(pending.length,count,'poll must not release a pending optimization');pending[0].resolve({available:true,loaded:true,optimized:false});await old;assert.strictEqual(badge.textContent,'Applying...');assert.strictEqual(toggle.disabled,true);
finish({});await new Promise(resolve=>setImmediate(resolve));pending.at(-1).resolve({available:true,loaded:true,optimized:true,context_length:8192,parallel:1});await optimizing;assert.strictEqual(toggle.checked,true);assert(badge.textContent.startsWith('On'));
const first=context.refreshLmStudioStatus(),second=context.refreshLmStudioStatus();pending.at(-1).resolve({remote:true,optimized:true});await second;const current=badge.textContent;pending[pending.length-2].reject(Error('old status'));await first;assert.strictEqual(badge.textContent,current);assert.strictEqual(toggle.checked,true);finished=true;
''')

    def test_scientific_seed_values_reach_generation_and_export_without_truncation(self):
        self.run_scenario(r'''
const errors=[];context.showToast=(...args)=>errors.push(args);context.document.getElementById('dsb-description').value='voice';const global=context.document.getElementById('dsb-global-seed');let packet,exported;
context.API.post=async(path,body)=>{packet=body;return {audio_url:'/current.wav'};};context.Blob=class {constructor(parts){exported=JSON.parse(parts[0]);}};context.URL={createObjectURL:()=>'/blob',revokeObjectURL(){}};context.document.createElement=()=>({click(){}});
for(const [line,root,expected] of [['1e3','7',1000],['','2e3',2000],['-1','2e3',2000],['0','2e3',0],['','','-1']]){
 run(`dsbCurrentProject='A';dsbRows=[{text:'line',emotion:'',seed:${JSON.stringify(line)},status:'pending'}];`);global.value=root;await context.dsbGenSample(0);assert.strictEqual(packet.seed,Number(expected));context.dsbExport();if(line!==''){assert.strictEqual(exported[0].seed,Number(line));}
}
context.dsbStartPolling=()=>{};run("dsbBatchRunning=false;dsbCurrentProject='A';dsbRows=[{text:'line',emotion:'',seed:'1e3',status:'pending'},{text:'other',emotion:'',seed:'',status:'pending'}]");global.value='2e3';await context.dsbGenerateAll();assert.strictEqual(packet.global_seed,2000);assert.deepStrictEqual(plain(packet.seeds),[1000,-1]);
for(const value of ['1.5','NaN','Infinity','2x','-2','9007199254740992']){
 run(`dsbRows=[{text:'line',emotion:'',seed:${JSON.stringify(value)},status:'pending'}]`);packet=null;exported=null;run('dsbBatchRunning=false');await context.dsbGenSample(0);assert.strictEqual(packet,null,value);assert.strictEqual(run('dsbRows[0].status'),'pending');context.dsbExport();assert.strictEqual(exported,null,value);await context.dsbGenerateAll();assert.strictEqual(packet,null,value);assert.strictEqual(run('dsbRows[0].status'),'pending');
}assert.strictEqual(errors.length,18);assert(errors.every(t=>t[0].includes('safe whole numbers')));
''')

    def test_late_boot_project_list_cannot_clear_restored_owner_selection(self):
        self.run_scenario(r"""
let resolveBoot,listCalls=0;const boot=new Promise(resolve=>resolveBoot=resolve);
const owner=[{name:'Book',done_count:0,sample_count:1}];
context.API.get=async path=>path==='/api/dataset_builder/list'?(++listCalls===1?boot:owner):{running:true,description:'active',samples:[{text:'line',status:'generating'}],logs:[]};
run('dsbStopBatch = () => {}; dsbStartPolling = () => {};');
const select=context.document.getElementById('dsb-project-select');let html='';
Object.defineProperty(select,'innerHTML',{get:()=>html,set:value=>{html=value;select.value='';}});
const initial=context.dsbLoadProjects();await context.dsbLoadProjects('Book');assert.strictEqual(select.value,'Book');
resolveBoot(owner);await initial;assert.strictEqual(select.value,'Book');assert.strictEqual(run('dsbCurrentProject'),'Book');assert(run('dsbBatchRunning'));
""")

    def test_project_list_failure_retains_selection_retries_and_ignores_stale_failure(self):
        self.run_scenario(r"""
const select=context.document.getElementById('dsb-project-select'),status=context.document.getElementById('dsb-project-list-status');select.value='Book';select.innerHTML='old options';
context.API.get=async()=>{throw Error('offline');};await context.dsbLoadProjects();assert.strictEqual(select.value,'Book');assert.strictEqual(select.innerHTML,'old options');assert(status.innerHTML.includes('Retry project list'));
context.API.get=async()=>({wrong:'shape'});await context.dsbLoadProjects();assert.strictEqual(select.innerHTML,'old options');
let reject;context.API.get=()=>new Promise((yes,no)=>reject=no);const stale=context.dsbLoadProjects();context.API.get=async()=>[{name:'Book',done_count:1,sample_count:1}];await context.dsbLoadProjects();assert.strictEqual(select.value,'Book');assert.strictEqual(status.textContent,'');reject(Error('late offline'));await stale;assert.strictEqual(status.textContent,'');
context.API.get=async()=>[];await context.dsbLoadProjects();assert.strictEqual(select.value,'Book');assert(select.innerHTML.includes('not in current list'));assert(status.textContent.includes('loaded rows have been kept'));
""")

    def test_training_save_flushes_rows_and_marks_later_edits_or_regeneration_stale(self):
        self.run_scenario(r"""
run("dsbCurrentProject='Book';dsbRows=[{text:'original',emotion:'warm',seed:0,status:'done',audio_url:'/sample.wav'}];");
let flushes=0,resolvePost;context.flush=async()=>flushes++;run('dsbSaveRowsQueue={flush};');context.showConfirm=async()=>true;context.API.post=(url,body)=>{assert.strictEqual(url,'/api/dataset_builder/save');assert.strictEqual(body.name,'Book');return new Promise(yes=>resolvePost=yes);};
const save=context.dsbSave();for(let i=0;i<8;i++)await Promise.resolve();assert.strictEqual(flushes,1);resolvePost({sample_count:1});await save;const status=context.document.getElementById('dsb-save-status');assert.strictEqual(status.textContent,'Saved! 1 samples.');
run("dsbRows[0].status='generating';applyDatasetTrainingSaveFeedback();");assert(status.textContent.includes('saved before these changes'));
run("dsbRows[0].status='done';applyDatasetTrainingSaveFeedback();");assert(status.textContent.includes('saved before these changes'),'regeneration completion must not resurrect old Saved');
const next=context.dsbSave();for(let i=0;i<8;i++)await Promise.resolve();run("dsbRows[0].text='newer edit';");resolvePost({sample_count:1});await next;assert(status.textContent.includes('saved before these changes'));
run("dsbCurrentProject='Other';applyDatasetTrainingSaveFeedback();");assert.strictEqual(status.textContent,'');
""")

    def test_training_save_refuses_changed_confirmation_flush_and_old_owner_ack(self):
        self.run_scenario(r"""
const reset=()=>run("dsbCurrentProject='Book';dsbRows=[{text:'original',emotion:'warm',status:'done',audio_url:'/sample.wav'}];dsbSaveRowsQueue=null;");reset();let confirm,flush,post,count=0;
context.showConfirm=()=>new Promise(yes=>confirm=yes);context.API.post=()=>{count++;return new Promise(yes=>post=yes);};
const decision=context.dsbSave();await context.dsbSave();run("dsbRows[0].text='changed';");confirm(true);await decision;assert.strictEqual(count,0);
reset();context.showConfirm=async()=>true;context.flush=()=>new Promise(yes=>flush=yes);run('dsbSaveRowsQueue={flush};');const saving=context.dsbSave();for(let i=0;i<8;i++)await Promise.resolve();run("dsbCurrentProject='Other';");flush();await saving;assert.strictEqual(count,0);
reset();const accepted=context.dsbSave();for(let i=0;i<8;i++)await Promise.resolve();assert.strictEqual(count,1);run("dsbCurrentProject='Other';");const status=context.document.getElementById('dsb-save-status');status.textContent='Other project';post({sample_count:1});await accepted;assert.strictEqual(status.textContent,'Other project');
reset();context.API.post=async()=>{throw Error('offline');};await context.dsbSave();assert(status.textContent.includes('could not be confirmed'));assert.strictEqual(run('dsbRows[0].text'),'original');
""")

    def test_restore_active_dataset_selects_exact_owner_and_awaits_its_load(self):
        self.run_scenario(r"""
let resolveOwner;const ownerLoad=new Promise(resolve=>resolveOwner=resolve);let pollingOwner=null;
context.API.get=async path=>path==='/api/dataset_builder/list'?[{name:'Book 日本語 #',done_count:0,sample_count:1}]:ownerLoad;
run('dsbStopBatch = () => {}; dsbStartPolling = name => { window.restoredOwner = name; };');
const restore=context.dsbLoadProjects('Book 日本語 #');for(let i=0;i<8;i++){await Promise.resolve();}
assert.strictEqual(run('dsbCurrentProject'),'Book 日本語 #');let settled=false;restore.then(()=>settled=true);await Promise.resolve();assert(!settled);
resolveOwner({running:true,description:'active book',samples:[{text:'original',status:'generating'}],logs:[]});await restore;
assert.strictEqual(context.restoredOwner,'Book 日本語 #');assert.strictEqual(run('dsbBatchRunning'),true);assert.strictEqual(elements['dsb-btn-cancel'].style.display,'');assert.strictEqual(elements['dsb-btn-gen-all'].style.display,'none');
""")

    def test_batch_generation_blocks_row_mutations_and_late_file_import(self):
        self.run_scenario(r'''
const warnings = [];
context.showToast = (...args) => warnings.push(args);
run("dsbCurrentProject = 'A'; dsbRows = [{text:'original',emotion:'warm',seed:0,status:'done',audio_url:'/old.wav'}]; dsbBatchRunning = true;");
const before = plain(run('dsbRows'));
context.dsbUpdateRow(0, 'text', 'changed');
context.dsbRemoveRow(0);
context.dsbAddRow('new', 'new line', 1);
let reader;
context.FileReader = class {
    constructor() { reader = this; }
    readAsText() {}
};
const fileEvent = () => ({target:{files:[{}],value:'file.json'}});
context.dsbImport(fileEvent());
assert.strictEqual(reader, undefined, 'active batch must not start import');
assert.deepStrictEqual(plain(run('dsbRows')), before);
assert.strictEqual(posts.length, 0);
assert.strictEqual(timers.size, 0, 'blocked edits must not schedule an overwrite');
const html = context.dsbBuildRowHtml(run('dsbRows[0]'), 0);
assert.match(html, /<textarea[^>]*disabled/);
assert.match(html, /<input[^>]*disabled/);
assert.match(html, /<button[^>]*disabled[^>]*dsbRemoveRow|<button[^>]*dsbRemoveRow[^>]*disabled/);
run('dsbBatchRunning = false;');
context.dsbImport(fileEvent());
assert(reader);
run('dsbBatchRunning = true;');
reader.onload({target:{result:'[{"text":"imported"}]'}});
assert.deepStrictEqual(plain(run('dsbRows')), before, 'batch started during file read must block import');
run('dsbBatchRunning = false;');
context.dsbUpdateRow(0, 'text', 'allowed');
assert.strictEqual(run('dsbRows[0].text'), 'allowed');
assert.strictEqual(run('dsbRows[0].status'), 'pending');
assert.strictEqual(timers.size, 1);
assert(warnings.length >= 5);
context._startPolling = () => () => {};
context.API.get = async () => ({running:true,samples:[{text:'loaded',status:'done'}]});
await context.dsbLoadProject('A');
assert.strictEqual(run('dsbBatchRunning'), true);
assert.match(context.dsbBuildRowHtml(run('dsbRows[0]'), 0), /<textarea[^>]*disabled/);
context.API.get = async () => ({running:false,samples:[{text:'loaded',status:'done'}]});
await context.dsbLoadProject('A');
assert.strictEqual(run('dsbBatchRunning'), false);
assert.doesNotMatch(context.dsbBuildRowHtml(run('dsbRows[0]'), 0), /<textarea[^>]*disabled/);
''')

    def test_actual_project_switch_restores_idle_controls_and_stops_old_poller(self):
        self.run_scenario(r"""
run("dsbCurrentProject='A'; dsbRows=[{text:'A'}]; dsbBatchRunning=false;");
elements['dsb-project-select'] = {value:'B'};
context.document.getElementById('dsb-btn-gen-all').style.display='none';
context.document.getElementById('dsb-btn-regen-all').style.display='none';
context.document.getElementById('dsb-btn-cancel').style.display='';
let cancelled = 0;
context.cancelled = () => { cancelled++; };
run('dsbPolling = cancelled;');
context.API.get = async url => {
    assert.strictEqual(url,'/api/dataset_builder/status/B');
    return {running:false, samples:[{text:'B',emotion:'calm',status:'done'}]};
};
await context.dsbOnProjectChange();
assert.strictEqual(cancelled,1);
assert.strictEqual(run('dsbCurrentProject'),'B');
assert.strictEqual(run('dsbBatchRunning'),false);
assert.strictEqual(run('dsbPolling'),null);
assert.strictEqual(elements['dsb-btn-gen-all'].style.display,'');
assert.strictEqual(elements['dsb-btn-regen-all'].style.display,'');
assert.strictEqual(elements['dsb-btn-cancel'].style.display,'none');
assert.strictEqual(run('dsbRows[0].text'),'B');
assert.strictEqual(posts.length,0);
""")

    def test_running_or_starting_batch_keeps_project_and_poller(self):
        self.run_scenario(r"""
context.showToast=()=>{};
for(const duringFlush of [false,true]){
 run("dsbCurrentProject='A';dsbRows=[{text:'A'}];dsbBatchRunning=false;");
 elements['dsb-project-select']={value:'B'};
 let cancelled=0;context.cancelled=()=>cancelled++;run('dsbPolling=cancelled;');
 context.API.get=async()=>{throw Error('must not load another project');};
 if(duringFlush){
  let resolve;context.flush=()=>new Promise(done=>resolve=done);run('dsbSaveRowsQueue={flush};');
  const pending=context.dsbOnProjectChange();run('dsbBatchRunning=true;');resolve();await pending;
 }else{run('dsbSaveRowsQueue=null;dsbBatchRunning=true;');await context.dsbOnProjectChange();}
 assert.strictEqual(elements['dsb-project-select'].value,'A');assert.strictEqual(run('dsbCurrentProject'),'A');
 assert.strictEqual(run('dsbRows[0].text'),'A');assert.strictEqual(cancelled,0);assert.strictEqual(run('dsbBatchRunning'),true);
}
""")

    def test_invalid_import_types_preserve_rows_and_schedule_no_save(self):
        self.run_scenario(r"""
const errors = [];
context.showToast = (...args) => errors.push(args);
let reader;
context.FileReader = class { constructor() { reader=this; } readAsText() {} };
run("dsbCurrentProject='A'; dsbRows=[{text:'existing',emotion:'warm',seed:0,status:'done',audio_url:'/existing.wav'}];");
const before=plain(run('dsbRows'));
for (const data of [{}, [null], [1], ['text'], [[]], [true],
    [{text:123}], [{text:false}], [{text:{}}], [{emotion:12}],
    [{instruct:[]}], [{seed:true}], [{seed:{}}],
    [{text:'valid first'},{text:123}]]) {
    const event={target:{files:[{}],value:'dataset.json'}};
    context.dsbImport(event);
    await reader.onload({target:{result:JSON.stringify(data)}});
    assert.deepStrictEqual(plain(run('dsbRows')),before,`Invalid import ${JSON.stringify(data)} must preserve rows`);
    assert.strictEqual(timers.size,0);
    assert.strictEqual(posts.length,0);
    assert.strictEqual(errors.at(-1)[1],'error');
    assert.strictEqual(event.target.value,'');
}
""")

    def test_sample_delete_cancel_and_changed_owner_preserve_rows(self):
        self.run_scenario(r"""
run("dsbCurrentProject='A';dsbRows=[{text:'keep',status:'done',audio_url:'/keep.wav'}];");
context.showConfirm=async()=>false;
await context.dsbRemoveRow(0);
assert.strictEqual(run('dsbRows.length'),1);assert.strictEqual(timers.size,0);
let resolve;context.showConfirm=()=>new Promise(done=>resolve=done);
const pending=context.dsbRemoveRow(0);
run("dsbCurrentProject='B';dsbRows=[{text:'other'}];");resolve(true);await pending;
assert.strictEqual(run('dsbRows[0].text'),'other');assert.strictEqual(timers.size,0);
context.showConfirm=async()=>true;await context.dsbRemoveRow(0);
assert.strictEqual(run('dsbRows.length'),0);assert.strictEqual(timers.size,1);
""")

    def test_import_cancel_and_project_switch_leave_audio_and_rows_untouched(self):
        self.run_scenario(r"""
let reader;context.FileReader=class{constructor(){reader=this;}readAsText(){}};
run("dsbCurrentProject='A';dsbRows=[{text:'keep',status:'done',audio_url:'/keep.wav'}];");
const before=plain(run('dsbRows'));context.showConfirm=async()=>false;
context.dsbImport({target:{files:[{}],value:'file'}});
await reader.onload({target:{result:'[{"text":"new"}]'}});
assert.deepStrictEqual(plain(run('dsbRows')),before);assert.strictEqual(timers.size,0);
context.showConfirm=async()=>true;context.dsbImport({target:{files:[{}],value:'file'}});
run("dsbCurrentProject='B';dsbRows=[{text:'B'}];");
await reader.onload({target:{result:'[{"text":"new"}]'}});
assert.strictEqual(run('dsbRows[0].text'),'B');assert.strictEqual(timers.size,0);
""")

    def test_import_confirmation_refuses_changed_rows_or_new_batch(self):
        self.run_scenario(r"""
let reader;context.FileReader=class{constructor(){reader=this;}readAsText(){}};
context.showToast=()=>{};
for(const change of ['edit','batch']){
 run("dsbCurrentProject='A';dsbRows=[{text:'keep',status:'done',audio_url:'/keep.wav'}];dsbBatchRunning=false;");
 let resolve;context.showConfirm=()=>new Promise(done=>resolve=done);
 context.dsbImport({target:{files:[{}],value:'file'}});
 const pending=reader.onload({target:{result:'[{"text":"new"}]'}});
 if(change==='edit'){run("dsbRows[0].text='edited';");}else{run('dsbBatchRunning=true;');}
 const before=plain(run('dsbRows'));resolve(true);await pending;
 assert.deepStrictEqual(plain(run('dsbRows')),before);assert.strictEqual(timers.size,0);
}
""")

    def test_valid_import_preserves_strings_seed_zero_and_export_round_trip(self):
        self.run_scenario(r"""
let reader;
context.FileReader=class {constructor(){reader=this;} readAsText(){} };
run("dsbCurrentProject='A'; dsbRows=[{text:'old'}];");
context.showConfirm=async()=>true;
const data=[{text:'line',emotion:'calm',seed:0},
    {text:'legacy line',instruct:'warm',seed:'42'},
    {text:'optional'}, {text:null,emotion:null,seed:null}];
context.dsbImport({target:{files:[{}],value:'dataset.json'}});
await reader.onload({target:{result:JSON.stringify(data)}});
assert.deepStrictEqual(plain(run('dsbRows')),[
    {text:'line',emotion:'calm',seed:0,status:'pending',audio_url:null},
    {text:'legacy line',emotion:'warm',seed:'42',status:'pending',audio_url:null},
    {text:'optional',emotion:'',seed:'',status:'pending',audio_url:null},
    {text:'',emotion:'',seed:'',status:'pending',audio_url:null}]);
assert.strictEqual(timers.size,1);
await flushTimers();
assert.strictEqual(posts.length,1);
assert.strictEqual(posts[0].url,'/api/dataset_builder/update_rows');
assert.strictEqual(posts[0].data.name,'A');
assert.strictEqual(posts[0].data.rows[0].seed,0);
let exported, clickCount=0;
context.Blob=class {constructor(parts){exported=JSON.parse(parts[0]);}};
context.URL={createObjectURL:()=>'/blob',revokeObjectURL:()=>{}};
context.document.createElement=()=>({click:()=>{clickCount++;}});
context.dsbExport();
assert.deepStrictEqual(exported,[{emotion:'calm',text:'line',seed:0},
    {emotion:'warm',text:'legacy line',seed:42},
    {emotion:'',text:'optional'}, {emotion:'',text:''}]);
assert.strictEqual(clickCount,1);
""")

    def test_optimize_failure_and_failed_status_refresh_leave_retry_enabled(self):
        self.run_scenario(r"""
const start=source.indexOf('async function refreshLmStudioStatus()');
const end=source.indexOf('function reattachTaskActivity(',start);
assert(start>=0 && end>start);run(source.slice(start,end));
context.showToast=()=>{};
context.API.post=async()=>{throw new Error('optimization failed');};
context.API.get=async()=>{throw new Error('status unavailable');};
const toggle=context.document.getElementById('lmstudio-optimize-toggle');
toggle.checked=true;toggle.disabled=false;
await context.toggleLmStudioOptimize();
assert.strictEqual(toggle.disabled,false);
assert.strictEqual(toggle.checked,false);
assert.strictEqual(elements['lmstudio-status-badge'].textContent,'Status unavailable');
// Confirmed lack of the CLI retains its existing disabled control.
context.API.get=async()=>({available:false});
await context.toggleLmStudioOptimize();
assert.strictEqual(toggle.disabled,true);
// Successful optimization and status still restore authoritative values.
context.API.post=async()=>({});
context.API.get=async()=>({available:true,loaded:true,optimized:true,context_length:8000,parallel:1});
toggle.checked=true;
await context.toggleLmStudioOptimize();
assert.strictEqual(toggle.disabled,false);assert.strictEqual(toggle.checked,true);
assert.match(elements['lmstudio-status-badge'].textContent,/On \(ctx 8000, parallel 1\)/);
""")

    def test_delete_blocks_active_batch_before_confirmation_and_after_delayed_confirmation(self):
        self.run_scenario(r"""
let deletes=0,confirmations=0,resolveConfirm;
const warnings=[];
context.showToast=(...args)=>warnings.push(args);
context.showConfirm=async()=>{confirmations++;return true;};
context.fetch=async()=>{deletes++;return{ok:true};};
context.API._handleError=async()=>{};
context.API.get=async()=>[];
run("dsbCurrentProject='A';dsbRows=[{text:'keep',status:'done',audio_url:'/old.wav'}];dsbBatchRunning=true;");
const before=plain(run('dsbRows'));
await context.dsbDeleteProject();
assert.strictEqual(deletes,0);
assert.strictEqual(confirmations,0);
assert.strictEqual(run('dsbCurrentProject'),'A');
assert.deepStrictEqual(plain(run('dsbRows')),before);
run('dsbBatchRunning=false;');
context.showConfirm=()=>new Promise(resolve=>{confirmations++;resolveConfirm=resolve;});
const pending=context.dsbDeleteProject();
assert.strictEqual(confirmations,1);
run('dsbBatchRunning=true;');
resolveConfirm(true);await pending;
assert.strictEqual(deletes,0);
assert.strictEqual(run('dsbCurrentProject'),'A');
assert.deepStrictEqual(plain(run('dsbRows')),before);
assert.strictEqual(warnings.length,2);
""")

    def test_idle_delete_still_uses_encoded_project_and_clears_only_after_success(self):
        self.run_scenario(r"""
const deletes=[],warnings=[];
context.showToast=(...args)=>warnings.push(args);
context.showConfirm=async()=>true;
context.fetch=async(url,request)=>{deletes.push({url,method:request.method});return{ok:true};};
context.API._handleError=async()=>{};
context.API.get=async()=>[];
run("dsbCurrentProject='A B';dsbRows=[{text:'remove'}];dsbBatchRunning=false;");
await context.dsbDeleteProject();
assert.deepStrictEqual(deletes,[{url:'/api/dataset_builder/A%20B',method:'DELETE'}]);
assert.strictEqual(run('dsbCurrentProject'),'');
assert.deepStrictEqual(plain(run('dsbRows')),[]);
assert.strictEqual(elements['dsb-form-area'].style.display,'none');
assert.strictEqual(elements['dsb-btn-delete-project'].style.display,'none');
assert.deepStrictEqual(warnings,[]);
""")

    def test_preparer_start_uses_shared_http_error_handling_for_non_json_failures(self):
        self.run_scenario(r"""
const path=require('path');
const core=fs.readFileSync(path.join(path.dirname(process.argv[1]),'app-core.js'),'utf8');
const apiStart=core.indexOf('const API = {'),apiEnd=core.indexOf('// --- Setup Tab ---',apiStart);
assert(apiStart>=0 && apiEnd>apiStart);run(core.slice(apiStart,apiEnd));
const start=source.indexOf('let prepBatchQueue ='),end=source.indexOf('// List/download the dataset ZIPs',start);
assert(start>=0 && end>start);run(source.slice(start,end));
const audio={name:'fixture.wav'};
elements['prep-batch-mode']={checked:false};
elements['prep-audio-file']={files:[audio]};
elements['prep-source-file']={files:[]};
context.FormData=class{constructor(){this.fields=[];}append(key,value){this.fields.push([key,value]);}};
context.getNumFieldValue=(_id,fallback)=>fallback;
const toasts=[],polls=[],requests=[];
context.showToast=(...args)=>toasts.push(args);
context._pollPreparerLogs=task=>polls.push(task);
const cases=[
 {status:503,statusText:'Service Unavailable',body:'<html>proxy unavailable</html>',expected:'Service Unavailable'},
 {status:502,statusText:'Bad Gateway',body:'',expected:'Bad Gateway'},
 {status:429,statusText:'Too Many Requests',body:'rate limited',expected:'Too Many Requests'},
 {status:422,statusText:'Unprocessable Content',body:JSON.stringify({detail:'fixture denied'}),expected:'fixture denied'},
 {status:400,statusText:'Bad Request',body:JSON.stringify({detail:{message:'a structured error'}}),expected:'a structured error'}];
for(const failure of cases){
 context.fetch=async(url,request)=>{
  requests.push(url);
  assert.strictEqual(request.method,'POST');
  assert.strictEqual(request.body.fields.find(([key])=>key==='audio_file')[1],audio);
  return new Response(failure.body,{status:failure.status,statusText:failure.statusText});
 };
 await context.startPreparer();
 assert(toasts.at(-1)[0].includes('Check preparer task status and output files'));assert(toasts.at(-1)[0].includes('Details: '+failure.expected));assert.strictEqual(toasts.at(-1)[1],'error');
 assert.strictEqual(elements['btn-prep-start'].disabled,false);
 assert.strictEqual(elements['btn-prep-cancel'].style.display,'none');
 assert.strictEqual(polls.length,0);
}
context.fetch=async()=>new Response(JSON.stringify({status:'started'}),{status:200});
await context.startPreparer();
assert.deepStrictEqual(polls,['preparer']);
assert.strictEqual(elements['btn-prep-start'].disabled,true);
assert.strictEqual(elements['btn-prep-cancel'].style.display,'inline-block');
assert.strictEqual(toasts.length,cases.length);
assert.deepStrictEqual(requests,Array(cases.length).fill('/api/preparer/start'));
""")

    def test_batch_preparer_keeps_file_objects_and_sends_actual_multipart_bytes(self):
        self.run_scenario(r"""
const path=require('path');
const {File}=require('buffer');
const core=fs.readFileSync(path.join(path.dirname(process.argv[1]),'app-core.js'),'utf8');
const apiStart=core.indexOf('const API = {'),apiEnd=core.indexOf('// --- Setup Tab ---',apiStart);
run(core.slice(apiStart,apiEnd));
const start=source.indexOf('let prepBatchQueue ='),end=source.indexOf('// List/download the dataset ZIPs',start);
assert(start>=0 && end>start);run(source.slice(start,end));
context.document.createElement=()=>({innerHTML:''});
elements['prep-batch-queue-body']={innerHTML:'',appendChild(){}};
elements['prep-batch-mode']={checked:true};
elements['prep-batch-files']={files:[new File(['local first bytes'],'first #.wav'),new File(['local second bytes'],'二.wav')]};
context.FormData=FormData;
context.getNumFieldValue=(_id,fallback)=>fallback;
const toasts=[],polls=[],requests=[];
context.showToast=(...args)=>toasts.push(args);
context._pollPreparerLogs=task=>polls.push(task);
context.onPrepBatchFilesChange();
const selected=elements['prep-batch-files'].files;
assert.strictEqual(run('prepBatchQueue[0].audio'),selected[0]);
assert.strictEqual(run('prepBatchQueue[1].audio'),selected[1]);
context.fetch=async(url,request)=>{
 requests.push(url);
 assert.strictEqual(request.method,'POST');
 assert(request.body instanceof FormData);
 const files=request.body.getAll('audio_files');
 assert.deepStrictEqual(files.map(file=>file.name),['first #.wav','二.wav']);
 assert.deepStrictEqual(await Promise.all(files.map(async file=>Buffer.from(await file.arrayBuffer()).toString())),['local first bytes','local second bytes']);
 const config=JSON.parse(request.body.get('config_json'));
 assert.deepStrictEqual(config.tasks,[{audio_filename:'first #.wav',output_filename:'voice_dataset_first #.zip'},
                                     {audio_filename:'二.wav',output_filename:'voice_dataset_二.zip'}]);
 // Changing the picker after the request was built cannot replace its captured bytes.
 elements['prep-batch-files'].files=[new File(['replacement'],'other.wav')];
 context.onPrepBatchFilesChange();
 return new Response(JSON.stringify({status:'started',task_count:2}),{status:200});
};
await context.startPreparer();
assert.deepStrictEqual(requests,['/api/preparer/batch/upload_start']);
assert.deepStrictEqual(polls,['batch_preparer']);
assert.strictEqual(posts.length,0,'batch must not post names as a JSON-only request');
assert.strictEqual(toasts.length,1);assert(toasts[0][0].includes('finish'));assert.strictEqual(run('prepBatchQueue[0].audio'),selected[0]);
// The preceding accepted run has completed before testing a later start failure.
run('_applyPreparerControls(null)');
// Restore the original selection and exercise a plain-text proxy failure.
elements['prep-batch-files'].files=selected;context.onPrepBatchFilesChange();
context.fetch=async()=>new Response('proxy limit',{status:413,statusText:'Payload Too Large'});
await context.startPreparer();
assert.match(toasts.at(-1)[0],/Batch preparer start is unconfirmed.*output files.*Payload Too Large/);
assert.strictEqual(elements['btn-prep-start'].disabled,false);
assert.strictEqual(elements['btn-prep-cancel'].style.display,'none');
assert.strictEqual(polls.length,1,'failure must not start a status poll');
""")

    def test_dataset_poll_logs_append_preserve_history_and_reject_old_owner(self):
        self.run_scenario(r'''
let poll;context._startPolling=(key,fetch,options)=>{poll=options;return ()=>{};};
run("dsbCurrentProject='Book';dsbBatchRunning=true;");context.dsbStartPolling('Book');
const el=context.document.getElementById('dsb-logs');let text='',replaces=0,appends=0;
Object.defineProperty(el,'innerText',{get:()=>text,set:value=>{text=value;replaces++;}});
el.appendChild=node=>{text+=node.textContent;appends++;};el.scrollHeight=1000;el.clientHeight=100;
const tick=logs=>poll.onTick({running:true,run_id:'one',logs,samples:[]});
tick(['first','second']);assert.strictEqual(el.scrollTop,1000);el.scrollTop=200;
tick(['first','second','third']);assert.strictEqual(el.scrollTop,200);assert.strictEqual(appends,1);assert.strictEqual(replaces,1);
tick(['first','second','third']);assert.strictEqual(appends,1);assert.strictEqual(replaces,1);
tick(['second','third','fourth']);assert.strictEqual(text,'second\nthird\nfourth');assert.strictEqual(el.scrollTop,200);
tick([]);assert.strictEqual(text,'','empty run logs clear stale text');
run("dsbCurrentProject='Other';");tick(['old owner']);assert.strictEqual(text,'','stale owner cannot repaint logs');
''')

    def run_scenario(self, scenario):
        setup = r'''
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const core = fs.readFileSync(process.argv[2], 'utf8');
const queueStart = core.indexOf('function createSerializedSaveQueue(');
const queueEnd = core.indexOf('// --- API Helpers ---', queueStart);
assert(queueStart >= 0 && queueEnd > queueStart);
const end = source.indexOf('// Persist on input changes');
assert(end > 0);
const elements = {};
const posts = [];
const timers = new Map();
let nextTimer = 1;
const context = {
    localStorage: {removeItem() {}},
    document: {createTextNode: text => ({textContent:text}), getElementById(id) {
        if (!elements[id]) {
            elements[id] = {value: '', style: {}, innerHTML: '', innerText: '', scrollHeight:0, clientHeight:0, scrollTop:0, appendChild(node) { this.innerText += node.textContent; }};
        }
        return elements[id];
    }},
    API: {post: async (url, data) => { posts.push({url, data}); return {status:'ok'}; }},
    console: {error() {}},
    showToast() { throw new Error('unexpected toast'); },
    _toastSaveError() { throw new Error('unexpected save error'); },
    escapeHtml: value => String(value),
    setTimeout(callback) { const id = nextTimer++; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); }
};
context.window = context;
vm.createContext(context);vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),context);
vm.runInContext(core.slice(queueStart, queueEnd), context);
const escapeStart=core.indexOf('function escapeHtml('),escapeEnd=core.indexOf('// Parse a numeric input',escapeStart);
vm.runInContext(core.slice(escapeStart,escapeEnd),context);
const failureStart=core.indexOf('function isTaskFailed(');vm.runInContext(core.slice(failureStart,core.indexOf('// --- Desktop notifications ---',failureStart)),context);
const outcomeStart=core.indexOf('function getTaskCompletionOutcome(');vm.runInContext(core.slice(outcomeStart,core.indexOf('function notifyJobDone(',outcomeStart)),context);
const logsStart=core.indexOf('function getTaskLogUpdate(');
vm.runInContext(core.slice(logsStart,core.indexOf('// --- Setup Tab ---',logsStart)),context);
vm.runInContext(source.slice(0, end), context);
const run = code => vm.runInContext(code, context);
// Rendering is unrelated to request ownership; progress uses its actual renderer.
run('dsbRenderTable = () => {};');
const plain = value => JSON.parse(JSON.stringify(value));
async function flushTimers() {
    for (const [id, callback] of [...timers]) {
        timers.delete(id);
        await callback();
    }
    await new Promise(resolve => setImmediate(resolve));
}
'''
        script = setup + "\n(async () => {\n" + scenario + r'''
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", script, str(SOURCE), str(SOURCE.with_name("app-core.js"))],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_debounced_metadata_and_rows_stay_with_originating_project(self):
        self.run_scenario(r'''
run("dsbCurrentProject = 'A'; dsbRows = [{emotion:'warm',text:' first ',seed:0}];");
context.document.getElementById('dsb-description').value = 'voice A';
context.document.getElementById('dsb-global-seed').value = '0';
run('dsbSaveForm(); dsbSaveRows();');
run("dsbRows[0].text = 'unscheduled mutation'; dsbCurrentProject = 'B'; dsbRows = [{emotion:'cold',text:'second',seed:2}];");
elements['dsb-description'].value = 'voice B';
elements['dsb-global-seed'].value = '2';
await flushTimers();
assert.deepStrictEqual(plain(posts), [
    {url:'/api/dataset_builder/update_meta',data:{name:'A',description:'voice A',global_seed:'0'}},
    {url:'/api/dataset_builder/update_rows',data:{name:'A',rows:[{emotion:'warm',text:'first',seed:0}]}}
]);
// Same-project debounce still sends the latest scheduled edit only.
posts.length = 0;
run('dsbSaveForm(); dsbSaveRows();');
elements['dsb-description'].value = 'voice B edited';
run("dsbRows[0].text = 'second edited'; dsbSaveForm(); dsbSaveRows();");
await flushTimers();
assert.strictEqual(posts.length, 2);
assert.strictEqual(posts[0].data.name, 'B');
assert.strictEqual(posts[0].data.description, 'voice B edited');
assert.strictEqual(posts[1].data.rows[0].text, 'second edited');
''')

    def test_reference_choices_follow_indices_and_project_without_count_change(self):
        self.run_scenario(r'''
run("dsbCurrentProject = 'A'; dsbRows = []; dsbUpdateProgress();");
assert.match(elements['dsb-ref-select'].innerHTML, /No completed samples/);
run("dsbRows = [{status:'done',text:'A'}, {status:'pending',text:'unused'}]; dsbUpdateProgress();");
assert.match(elements['dsb-ref-select'].innerHTML, /value="0"/);
run("dsbCurrentProject = 'B'; dsbRows = [{status:'pending'}, {status:'done',text:'B'}]; dsbUpdateProgress();");
assert.match(elements['dsb-ref-select'].innerHTML, /value="1"/);
assert.doesNotMatch(elements['dsb-ref-select'].innerHTML, /value="0"/);
// Replacing a completed index at the same count within one project also refreshes.
run("dsbRows = [{status:'done',text:'B moved'}, {status:'pending'}]; dsbUpdateProgress();");
assert.match(elements['dsb-ref-select'].innerHTML, /value="0"/);
// Same index in a different project must show that project's label.
run("dsbCurrentProject = 'C'; dsbRows = [{status:'done',text:'C current'}]; dsbUpdateProgress();");
assert.match(elements['dsb-ref-select'].innerHTML, /C current/);
''')

    def test_generation_responses_ignore_switched_removed_and_replaced_rows(self):
        self.run_scenario(r'''
for (const fail of [false, true]) {
    for (const change of ['project', 'remove', 'replace', 'shift', 'text', 'emotion', 'seed']) {
        run("dsbCurrentProject = 'A'; dsbRows = [{emotion:'',text:'first',seed:''}, {emotion:'',text:'second',seed:''}];");
        context.document.getElementById('dsb-description').value = 'voice';
        let resolve, reject;
        context.API.post = () => new Promise((yes, no) => { resolve = yes; reject = no; });
        const pending = context.dsbGenSample(0);
        assert.strictEqual(run('dsbRows[0].status'), 'generating');
        if (change === 'project') {
            run("dsbCurrentProject = 'B'; dsbRows = [{text:'other',status:'pending'}];");
        } else if (change === 'remove') {
            run('dsbRows = [];');
        } else if (change === 'replace') {
            run("dsbRows[0] = {text:'replacement',status:'pending'};");
        } else if (['text', 'emotion', 'seed'].includes(change)) {
            context.dsbUpdateRow(0, change, 'edited');
        } else {
            run('dsbRows.shift();');
        }
        const before = plain(run('dsbRows'));
        if (fail) { reject(new Error('generation failed')); }
        else { resolve({audio_url:'/A/old.wav'}); }
        await pending;
        assert.deepStrictEqual(plain(run('dsbRows')), before, change);
    }
}
// Current row still accepts success and error results. Stale results above must not toast.
const toasts = [];
context.showToast = (...args) => toasts.push(args);
for (const fail of [false, true]) {
    run("dsbCurrentProject = 'A'; dsbRows = [{emotion:'',text:'first',seed:''}];");
    context.API.post = async () => {
        if (fail) { throw new Error('generation failed'); }
        return {audio_url:'/A/current.wav'};
    };
    await context.dsbGenSample(0);
    assert.strictEqual(run('dsbRows[0].status'), fail ? 'error' : 'done');
    if (!fail) { assert.strictEqual(run('dsbRows[0].audio_url'), '/A/current.wav'); }
    assert.strictEqual(toasts.length, fail ? 1 : 0);
    if (fail) {
        assert(run('dsbRows[0].error').includes('Check the sample audio and dataset task status'));assert(run('dsbRows[0].error').includes('Details: generation failed'));
        assert.strictEqual(toasts[0][0],run('dsbRows[0].error'));assert.strictEqual(toasts[0][1],'error');
    }
}
''')

    def test_refresh_failure_keeps_loaded_rows_but_failed_switch_disarms_saves(self):
        self.run_scenario(r"""
context.showToast=()=>{};run("dsbCurrentProject='A';");
context.API.get=async()=>({description:'voice A',samples:[{text:'A saved',status:'done',audio_url:'/A.wav'}]});
await context.dsbLoadProject('A');const before=plain(run('dsbRows'));
context.API.get=async()=>{throw Error('offline');};await context.dsbLoadProject('A');
assert.strictEqual(run('dsbCurrentProject'),'A');assert.deepStrictEqual(plain(run('dsbRows')),before);
assert.match(elements['dsb-project-load-status'].innerHTML,/last loaded version/);assert.match(elements['dsb-project-load-status'].innerHTML,/Retry/);
run("dsbCurrentProject='B';");await context.dsbLoadProject('B');
assert.strictEqual(run('dsbCurrentProject'),'');assert.deepStrictEqual(plain(run('dsbRows')),[]);
context.dsbSaveRows();assert.strictEqual(timers.size,0);assert.strictEqual(posts.length,0);
assert.match(elements['dsb-project-load-status'].innerHTML,/Could not load dataset/);
""")

    def test_late_project_loads_cannot_overwrite_a_new_selection_or_reload(self):
        self.run_scenario(r'''
for (const fail of [false, true]) {
    for (const reload of [false, true]) {
        run("dsbCurrentProject = 'A'; dsbRows = [];");
        let resolve, reject;
        context.API.get = () => new Promise((yes,no) => {resolve=yes;reject=no;});
        const oldLoad = context.dsbLoadProject('A');
        await new Promise(resolve => setImmediate(resolve));
        run("dsbCurrentProject = 'B';");
        context.API.get = async () => ({description:'B current',samples:[{text:'B'}]});
        await context.dsbLoadProject('B');
        if (reload) {
            run("dsbCurrentProject = 'A';");
            context.API.get = async () => ({description:'A new',samples:[{text:'A new'}]});
            await context.dsbLoadProject('A');
        }
        const before = plain(run('dsbRows'));
        const description = elements['dsb-description'].value;
        if (fail) { reject(new Error('old project offline')); }
        else { resolve({description:'A old',samples:[{text:'A old'}]}); }
        await oldLoad;
        assert.deepStrictEqual(plain(run('dsbRows')), before);
        assert.strictEqual(elements['dsb-description'].value, description);
        assert.strictEqual(run('dsbCurrentProject'), reload ? 'A' : 'B');
    }
}
''')

    def test_project_switch_stops_old_poll_and_old_callbacks_ignore_current_rows(self):
        self.run_scenario(r'''
let stopped = 0;
let callbacks;
context._startPolling = (_key,_fetch,handlers) => { callbacks=handlers; return () => {stopped++;}; };
run("dsbCurrentProject = 'A'; dsbRows = [{text:'A',status:'generating'}]; dsbBatchRunning = true;");
context.dsbStartPolling('A');
const oldCallbacks = callbacks;
// A completed batch allows switching; late responses still belong to A.
run('dsbBatchRunning = false;');
elements['dsb-project-select'] = {value:'B'};
context.API.get = async () => ({samples:[{text:'B',status:'pending'}],running:false});
await context.dsbOnProjectChange();
assert.strictEqual(stopped, 1);
assert.strictEqual(run('dsbBatchRunning'), false);
const before = plain(run('dsbRows'));
oldCallbacks.onTick({running:true,samples:[{status:'done',audio_url:'/A.wav'}],logs:['old logs']});
oldCallbacks.onDone();
assert.deepStrictEqual(plain(run('dsbRows')), before);
assert.strictEqual(run('dsbBatchRunning'), false);
assert.strictEqual(stopped, 1);
// Starting another poll still cancels the previous poll once.
run("dsbCurrentProject = 'B';");
context.dsbStartPolling('B');
context.dsbStartPolling('B');
assert.strictEqual(stopped, 2);
''')

    def test_material_row_edit_clears_stale_audio_and_reference_eligibility(self):
        self.run_scenario(r'''
run('dsbRenderTable = () => dsbUpdateProgress();');
run("dsbCurrentProject = 'A';");
for (const [field,value] of [['text','new text'],['emotion','new emotion'],['seed','4']]) {
    run("dsbRows = [{text:'old text',emotion:'old emotion',seed:'3',status:'done',audio_url:'/old.wav'}];");
    run('dsbUpdateProgress();');
    context.dsbUpdateRow(0,field,value);
    assert.strictEqual(run('dsbRows[0].status'), 'pending');
    assert.strictEqual(run('dsbRows[0].audio_url'), null);
    assert.match(elements['dsb-ref-select'].innerHTML, /No completed samples/);
}
run("dsbRows = [{text:'same',emotion:'warm',seed:'3',status:'done',audio_url:'/current.wav'}];");
context.dsbUpdateRow(0,'text','same');
assert.strictEqual(run('dsbRows[0].status'), 'done');
assert.strictEqual(run('dsbRows[0].audio_url'), '/current.wav');
''')


if __name__ == "__main__":
    unittest.main()
