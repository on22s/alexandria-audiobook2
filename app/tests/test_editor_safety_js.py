"""Behavioral checks for alias loading, cast refresh, and row edit flushing."""

from pathlib import Path
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "static/js/app-core.js"


class EditorSafetyJsTests(unittest.TestCase):
    def test_voice_change_label_counts_transitions_and_preserves_type_values(self):
        self.run_scenario(r"""
load('const AVAILABLE_VOICES =', 'let _voiceResourcesRefreshedAt');
context.renderStyleTimeline=()=>'';context.ensembleMembersMarkup=()=>'';
const state = {gender:'female',age_group:'adult'};
for (const [count, label] of [[2,'Voice changes (1 change)'],[3,'Voice changes (2 changes)']]) {
    const html=context.createVoiceCard({name:'Alice 日本語',config:{},traits:{states:Array(count).fill(state)}},0);
    assert(html.includes(label));assert(!html.includes(`${count} states`));
    assert(html.includes('Review which voice is used before and after each change.'));
    for (const type of ['custom','builtin_lora','clone','lora','design','ensemble']) {
        assert(html.includes(`value="${type}"`));
    }
    for (const label of ['Custom voice','Built-in LoRA voice','Clone voice','LoRA voice','Voice Design','Character ensemble']) {
        assert(html.includes(`>${label}</label>`));
    }
}
for (const states of [[],[state]]) {
    const html=context.createVoiceCard({name:'Alice',config:{},traits:{states}},0);
    assert(!html.includes('Voice changes ('));
}
""")

    def test_failed_batch_start_refreshes_optimistic_rows_but_not_unsaved_edits(self):
        self.run_scenario(r"""
run('let isRenderingAll=false;');
load('window.cancelRender =', "document.getElementById('btn-merge').addEventListener");
let refreshed=0,painted=false,polled=0;
const badge={className:'badge bg-secondary',innerText:'pending'};
context.document.querySelector=()=>({classList:{add:()=>painted=true},querySelector:()=>badge});
context.refreshDeliveryReview=async()=>{};
context._startPolling=()=>polled++;
context.loadChunks=async force=>{assert.strictEqual(force,true);refreshed++;badge.innerText='pending';};
context.ensureEditorRenderSnapshot=async()=>[{id:1,text:'Pending',status:'pending'}];
context.API.post=async()=>{throw Error('GPU busy');};
let completed=false;process.on('beforeExit',()=>assert(completed,'batch refusal assertions must finish'));
for(const start of [context.renderAll,context.renderBatchFast]){
 await start();assert.strictEqual(badge.innerText,'pending');assert.strictEqual(polled,0);
 assert.strictEqual(elements['btn-cancel-render'].style.display,'none');
}
assert.strictEqual(refreshed,2);assert.strictEqual(painted,true);
context.ensureEditorRenderSnapshot=async()=>{throw Error('unsaved edit flush failed');};
await context.renderBatchFast();assert.strictEqual(refreshed,2,'failed save must preserve unsaved edits without reload');
completed=true;
""")

    def test_custom_voice_options_preserve_a_saved_voice_outside_the_builtin_list(self):
        self.run_scenario(r'''
load('const AVAILABLE_VOICES =', 'let _voiceResourcesRefreshedAt');
context.renderStyleTimeline = () => '';
context.ensembleMembersMarkup = () => '';
function selectedValue(config) {
    const html = context.createVoiceCard({name:'ALICE',config}, 0);
    const options = html.match(/<select class="form-select voice-select"[^>]*>([\s\S]*?)<\/select>/)[1];
    const selected = options.match(/<option value="([^"]*)" selected>/);
    return selected ? selected[1] : options.match(/<option value="([^"]*)"/)[1];
}
assert.strictEqual(selectedValue({type:'custom',voice:'Ryan_v2'}), 'Ryan_v2');
assert.strictEqual(selectedValue({type:'custom',voice:'Ryan'}), 'Ryan');
assert.strictEqual(selectedValue({type:'custom'}), 'Aiden');
const html = context.createVoiceCard({name:'ALICE',config:{voice:'Ryan'}}, 0);
assert.strictEqual((html.match(/value="Ryan"/g) || []).length, 1);
''')

    def test_suggested_candidates_refresh_cards_and_autosave_metadata_without_losing_edits(self):
        self.run_scenario(r'''
load('const AVAILABLE_VOICES =', 'let _voiceResourcesRefreshedAt');
load('async function suggestVoices(characterNames', 'window.suggestMoreVoices =');
load('function collectVoiceConfig()', 'function onVoiceReadyChange');
context.console.debug = () => {};
context.renderVoiceSuggestions = () => {};
const saved = {innerHTML:''};
const inputs = {
    '.voice-type:checked':{value:'custom'},
    '.voice-select':{value:'Ryan_v2'},
    '.character-style':{value:'unsaved style'},
};
const card = {dataset:{voice:'ALICE'},querySelector: selector =>
    selector === '.saved-voice-candidates' ? saved : (inputs[selector] || null)};
context.document.querySelectorAll = selector => {
    assert.strictEqual(selector, '.voice-card'); return [card];
};
context._voicesByName = {ALICE:{name:'ALICE',config:{candidates:[]}}};
let complete = 0, voiceReads = 0, releaseFirst;
const first = new Promise(resolve => { releaseFirst = resolve; });
const candidates = [{candidate_id:'first',rank:1},{candidate_id:'second',rank:2}];
context.API.get = async url => {
    if (url === '/api/lora/models') { return []; }
    assert.strictEqual(url, '/api/voice_config/snapshot');
    assert.strictEqual(complete, 2, 'metadata read must wait for every candidate save');
    voiceReads++;
    return {revision:'0'.repeat(64),book_token:'b'.repeat(64),config:{},voices:[{name:'ALICE',config:{voice:'Aiden',character_style:'old style',candidates}}]};
};
context.API.post = async (url, payload) => {
    if (url === '/api/suggest_voices') {
        return {suggestions:{ALICE:{adapter_id:'first',ranked_adapter_ids:['first','second']}}};
    }
    assert.strictEqual(url, '/api/voices/ALICE/candidates');
    if (payload.candidate_id === 'first') { await first; }
    else { releaseFirst(); }
    complete++;
    return {candidates:[]}; // Individual responses are intentionally stale/out of order.
};
await context.suggestVoices(['ALICE']);
assert.strictEqual(voiceReads, 1);
assert.deepStrictEqual(plain(context._voicesByName.ALICE.config.candidates), candidates);
assert.match(saved.innerHTML, /first/);
assert.match(saved.innerHTML, /second/);
assert.match(saved.innerHTML, /selectVoiceCandidate/);
const collected = plain(context.collectVoiceConfig());
assert.deepStrictEqual(collected.ALICE.candidates, candidates);
assert.strictEqual(collected.ALICE.voice, 'Ryan_v2');
assert.strictEqual(collected.ALICE.character_style, 'unsaved style');
assert.strictEqual(elements['btn-suggest-voices'].disabled, false);
context.API.get = async () => { throw new Error('refresh unavailable'); };
await context.suggestVoices(['ALICE']);
assert.match(elements['suggest-status'].innerHTML, /refresh unavailable/);
assert.strictEqual(elements['btn-suggest-voices'].disabled, false);
''')

    def test_clone_card_identity_uses_the_library_directory_and_exact_filename(self):
        self.run_scenario(r'''
load('const AVAILABLE_VOICES =', 'let _voiceResourcesRefreshedAt');
context.renderStyleTimeline = () => '';
context.ensembleMembersMarkup = () => '';
context._cloneVoicesCache = [
    {id:'short',filename:'voice.wav',name:'Short'},
    {id:'long',filename:'my_voice.wav',name:'Long'}];
context._designedVoicesCache = [{id:'designed',filename:'my_voice.wav',name:'Designed'}];
function check(path, expected, readOnly, deletable) {
    const html = context.createVoiceCard({name:'ALICE',config:{type:'clone',ref_audio:path}}, 0);
    const options = html.match(/<select class="form-select designed-voice-select"[^>]*>([\s\S]*?)<\/select>/)[1];
    const selected = [...options.matchAll(/<option value="([^"]*)" selected>/g)].map(m => m[1]);
    assert.deepStrictEqual(selected, expected ? [expected] : []);
    assert.strictEqual(/class="form-control ref-audio"[^>]*readonly/.test(html), readOnly);
    const display = html.match(/class="btn btn-sm btn-outline-danger clone-delete-btn"[^>]*style="display:([^";]*)/)[1];
    assert.strictEqual(display, deletable ? 'inline-block' : 'none');
}
check('clone_voices/my_voice.wav', 'clone:long', true, true);
check('designed_voices/my_voice.wav', 'design:designed', true, false);
check('./clone_voices/my_voice.wav', 'clone:long', true, true);
check('clone_voices\\my_voice.wav', 'clone:long', true, true);
check('my_voice.wav', '__manual__', false, false);
check('other/clone_voices/my_voice.wav', '__manual__', false, false);
check('clone_voices/my_voice.wav.backup', '__manual__', false, false);
check('', '', false, false);
''')

    def test_batch_mode_refuses_single_book_start_over(self):
        self.run_scenario(r"""
let freshClick,generated=0,probes=0,confirmations=0;
context.document.getElementById('script-batch-mode');
elements['btn-gen-script-fresh']={style:{},addEventListener:(_event,fn)=>freshClick=fn};
elements['btn-gen-script']={disabled:true,click:()=>generated++};
context.currentBookFilename='A';context.API.get=async()=>{probes++;return{running:false};};
context.showConfirm=async()=>{confirmations++;return true;};
load('let _scriptStartOver =',"document.getElementById('btn-gen-script').addEventListener");
elements['script-batch-mode'].checked=true;
await freshClick();assert.strictEqual(generated,0);assert.strictEqual(probes,0);assert.strictEqual(confirmations,0);assert.strictEqual(await run('_scriptStartOver'),false);
context.renderBookPreflightVisibility();assert.strictEqual(elements['btn-gen-script-fresh'].style.display,'none');
elements['script-batch-mode'].checked=false;context.renderBookPreflightVisibility();assert.strictEqual(elements['btn-gen-script-fresh'].style.display,'');
await freshClick();assert.strictEqual(generated,1);assert.strictEqual(confirmations,1);
""")

    def test_async_script_confirmations_refuse_changed_targets(self):
        self.run_scenario(r'''
let freshClick,generated=0,cancelled=0;elements['btn-gen-script-fresh']={addEventListener:(_event,fn)=>freshClick=fn};elements['btn-gen-script']={disabled:true,click:()=>generated++};context.currentBookFilename='A';context.API.get=async()=>({running:false});context.cancelTask=async()=>cancelled++;load('let _scriptStartOver =',"document.getElementById('btn-gen-script').addEventListener");let confirmations=0;context.API.get=async()=>{throw Error('offline');};context.showConfirm=async()=>{confirmations++;return true;};await freshClick();assert.strictEqual(confirmations,0);assert.strictEqual(generated,0);assert.strictEqual(cancelled,0);assert.strictEqual(await run('_scriptStartOver'),false);assert.match(toasts.at(-1)[0],/status could not be checked/);context.API.get=async()=>({running:false});context.showConfirm=async()=>false;await freshClick();assert.strictEqual(generated,0);context.showConfirm=async()=>{context.currentBookFilename='B';return true;};await freshClick();assert.strictEqual(generated,0);assert.strictEqual(cancelled,0);assert.strictEqual(await run('_scriptStartOver'),false);
load('async function onSkipRecoveryChunk()', 'window.retryScriptGeneration =');context.refreshScriptRecovery=async()=>{};context.renderRecoveryFindings=()=>{};context.window._scriptRecoveryDetail={failed_chunk:1,failed_pass:'attribute'};context.showConfirm=async()=>false;await context.onSkipRecoveryChunk();assert.strictEqual(posts.length,0);context.showConfirm=async()=>{context.window._scriptRecoveryDetail.failed_chunk=2;return true;};await context.onSkipRecoveryChunk();assert.strictEqual(posts.length,0);context.showConfirm=async()=>true;await context.onSkipRecoveryChunk();assert.strictEqual(posts.length,1);assert.strictEqual(posts[0].data.chunk,2);
''')

    def test_cancelled_age_prompt_creates_no_version(self):
        self.run_scenario(r"""
load('window.addVoiceVersion =', 'window.saveNarratorStrategy =');
const button = {closest: () => ({dataset:{voice:'Alice / Smith'}})};
let refreshes = 0;
context.loadVoices = async () => { refreshes++; };
context.currentBookFilename = 'book.txt';
for (let cancellation = 0; cancellation < 3; cancellation++) {
    context.showPresetEditor = async options => { assert.strictEqual(options.description, 'adult'); return null; };
    await context.addVoiceVersion(button);
    assert.strictEqual(posts.length, 0);
    assert.strictEqual(refreshes, 0);
    assert.strictEqual(toasts.length, 0);
}
for (const [answer, expected] of [['', 'adult'], [' teen ', 'teen']]) {
    context.showPresetEditor = async () => ({name:'younger', description:answer.trim()});
    await context.addVoiceVersion(button);
    assert.deepStrictEqual(plain(posts.at(-1)), {
        url:'/api/voices/Alice%20%2F%20Smith/versions',
        data:{version_id:'younger',age_group:expected}});
}
assert.strictEqual(refreshes, 2);
""")

    def test_individual_personas_share_custom_context_parser(self):
        self.run_scenario(r"""
load('function getPersonaContextLines()', 'function onPersonaContextChange()');
load('window.regeneratePersona =', 'window.selectVoiceCandidate =');
const button = {closest: () => ({dataset:{voice:'Alice'}})};
context.showPresetEditor = async () => ({name:'teen'});
context.currentBookFilename = 'book.txt';
let polls = 0;
context.pollPersonaStatus = () => { polls++; };
const select = context.document.getElementById('persona-context-lines');
const custom = context.document.getElementById('persona-context-custom');
for (const [selection, customValue, expected] of [
    ['custom','37',37], ['custom','201',200], ['custom','-1',1],
    ['custom','',10], ['custom','garbage',10], ['20','37',20]]) {
    select.value = selection; custom.value = customValue;
    for (const action of [context.regeneratePersona, context.generateAgeVersion]) {
        await action(button);
        const sent = plain(posts.at(-1));
        assert.strictEqual(sent.url, '/api/generate_personas');
        assert.strictEqual(sent.data.context_lines, expected);
        assert.strictEqual(sent.data.speaker, 'Alice');
        assert.strictEqual(sent.data.advanced, false);
    }
}
assert.strictEqual(polls, 12);
assert.strictEqual(posts.at(-1).data.age_group, 'teen');
""")

    def test_drift_toast_does_not_treat_ten_or_hundred_flags_as_zero(self):
        self.run_scenario(r"""
load('async function runDriftCheck(indices)', 'function updateChunkRow(chunk)');
context.API.post = async () => ({measured:true});
let poll, reloads = 0;
context._startPolling = (key, getter, options) => { poll = options; };
context.loadChunks = async force => { assert.strictEqual(force, true); reloads++; };
for (const count of [0, 1, 10, 20, 100, 101]) {
    await context.runDriftCheck([2]);
    assert.strictEqual(elements['btn-drift-check'].disabled, true);
    const log = `Checked 120 chunk(s) at threshold 0.65: ${count} flagged.`;
    await poll.onDone({logs:[log]});
    assert.strictEqual(toasts.at(-1)[0], 'Voice check: ' + log);
    assert.strictEqual(toasts.at(-1)[1], count === 0 ? 'success' : 'warning');
    assert.strictEqual(elements['btn-drift-check'].disabled, false);
}
await context.runDriftCheck();
await poll.onDone({logs:['NOT MEASURED: encoder unavailable']});
assert.deepStrictEqual(toasts.at(-1), ['NOT MEASURED: encoder unavailable','warning']);
assert.strictEqual(reloads, 7);
""")

    def test_start_over_does_not_restart_after_stop_status_error(self):
        self.run_scenario(r"""
let freshClick, now = 0, cancelCalls = 0, generated = 0;
elements['btn-gen-script-fresh'] = {addEventListener: (event, handler) => {
    assert.strictEqual(event, 'click'); freshClick = handler;
}};
elements['btn-gen-script'] = {disabled:true,click: () => { generated++; }};
context.Date = {now: () => now};
context.setTimeout = (callback, delay) => { now += delay; callback(); };
context.showConfirm = async () => true;
context.currentBookFilename = 'book.txt';
context._resetPauseBtn = () => {};
context.cancelTask = async (url, options) => { cancelCalls++; options.onSuccess(); };
load('let _scriptStartOver =', "document.getElementById('btn-gen-script').addEventListener");
const responses = [{running:true}, new Error('status unavailable')];
context.API.get = async () => {
    const response = responses.shift();
    if (response instanceof Error) { throw response; }
    assert(response, 'unexpected extra status read'); return response;
};
await freshClick();
assert.strictEqual(cancelCalls, 1);
assert.strictEqual(generated, 0, 'status error cannot authorize a replacement run');
assert.strictEqual(elements['btn-gen-script'].disabled, true);
assert.strictEqual(toasts.at(-1)[1], 'warning');
assert.strictEqual(await run('_scriptStartOver'), false);
// A later attempt succeeds only with a confirmed terminal status.
responses.push({running:true}, {running:true}, {running:false});
await freshClick();
assert.strictEqual(generated, 1);
assert.strictEqual(await run('_scriptStartOver'), true);
// Consistently running responses reach the existing bounded timeout.
context.API.get = async () => ({running:true});
now = 0;
assert.strictEqual(await context.waitForScriptToStop(1000), false);
assert.strictEqual(now, 1000);
""")

    def test_voice_change_refreshes_actual_card_timeline_and_handles_missing_cached_config(self):
        self.run_scenario(r"""
load('const AVAILABLE_VOICES =', 'let _voiceResourcesRefreshedAt');
load('let _voiceResourcesRefreshedAt', 'window.selectVoiceVersion =');
load('function renderStyleTimeline(name, config)', 'function collectVoiceConfig()');
context.ensembleMembersMarkup=()=>'';
for(const name of ['loadCastLibrary','refreshVoicesScope','updateNarratorPreviewFields','renderReadyCount','onToggleHideReady','saveVoicesDebounced']){
 context[name]=()=>{};
}
context.loadCastLibrary=async()=>{};
context.document.querySelector=selector=>({querySelector:()=>({value:'ALICE'})});
let promptValue='Older and warmer',gets=0;
context.showPresetEditor=async()=>promptValue===null?null:{name:promptValue};
context.currentBookFilename='book.txt';
let server={name:'ALICE',config:{type:'custom',voice:'Ryan',style_timeline:[]}};
context.API.get=async url=>{gets++;return url==='/api/voice_config/snapshot'?{revision:'0'.repeat(64),book_token:'b'.repeat(64),config:{},voices:[plain(server)]}:[];};
context.API.post=async(url,data)=>{
 posts.push({url,data});
 server.config.style_timeline=data.character_style?[{from_index:data.from_index,character_style:data.character_style}]:[];
 return {style_timeline:plain(server.config.style_timeline)};
};
for(const cached of [{name:'ALICE',config:{type:'custom',voice:'Ryan'}},{name:'ALICE'}]){
 context._voicesByName={ALICE:cached};
 const before=gets;
 promptValue='Older and warmer';
 await context.voiceChangesHere(2);
 assert(gets>before,'must refresh server state and visible card');
 assert.match(elements['voices-list'].innerHTML,/Changes:/);
 assert.match(elements['voices-list'].innerHTML,/from line 3: Older and warmer/);
 assert.deepStrictEqual(plain(context._voicesByName.ALICE.config.style_timeline),[{from_index:2,character_style:'Older and warmer'}]);
 assert.strictEqual(toasts.at(-1)[1],'success');
 assert.deepStrictEqual(plain(posts.at(-1)),{url:'/api/voices/ALICE/style_timeline',data:{from_index:2,character_style:'Older and warmer'}});
 promptValue='';
 await context.voiceChangesHere(2);
 assert.doesNotMatch(elements['voices-list'].innerHTML,/Changes:/);
 assert.deepStrictEqual(plain(context._voicesByName.ALICE.config.style_timeline),[]);
 assert.match(toasts.at(-1)[0],/removed/);
}
const previousPosts=posts.length,previousGets=gets,previousHtml=elements['voices-list'].innerHTML;
promptValue=null;
await context.voiceChangesHere(2);
assert.strictEqual(posts.length,previousPosts);
assert.strictEqual(gets,previousGets);
assert.strictEqual(elements['voices-list'].innerHTML,previousHtml);
promptValue='A new style';
context.API.post=async()=>{throw new Error('save refused');};
await context.voiceChangesHere(2);
assert.strictEqual(gets,previousGets);
assert.strictEqual(elements['voices-list'].innerHTML,previousHtml);
assert(toasts.at(-1)[0].includes('saved change point before saving again')); assert(toasts.at(-1)[0].includes('Details: save refused')); assert.strictEqual(toasts.at(-1)[1], 'error');
""")

    def test_cancel_request_result_reaches_public_wrapper_and_failed_requests_do_not_reset_controls(self):
        self.run_scenario(r"""
load('async function cancelTask(url,', 'function _toastSaveError(');
load('window.cancelScript =', 'window.pauseResumeScript');
const resets=[];
context._resetPauseBtn=id=>resets.push(id);
let resolvePost,rejectPost;
context.API.post=(url,data)=>{
 posts.push({url,data});
 return new Promise((resolve,reject)=>{resolvePost=resolve;rejectPost=reject;});
};
const accepted=context.cancelScript();
assert.deepStrictEqual(resets,[]);
assert.strictEqual(elements['btn-cancel-script'].disabled,true);
assert.match(elements['script-cancellation-status'].textContent,/Requesting cancellation/);
assert.strictEqual(await context.cancelScript(),false);
assert.strictEqual(posts.length,1);
resolvePost({status:'cancel_requested'});
assert.strictEqual(await accepted,true);
assert.match(elements['script-cancellation-status'].textContent,/Waiting for the worker to stop/);
assert.strictEqual(elements['btn-cancel-script'].disabled,true);
assert.deepStrictEqual(resets,['btn-pause-script']);
assert.deepStrictEqual(plain(posts.at(-1)),{url:'/api/generate_script/cancel',data:{}});
context.clearScriptCancellation('script');
const failed=context.cancelScript();
rejectPost(new Error('server refused'));
assert.strictEqual(await failed,false);
assert.strictEqual(elements['btn-cancel-script'].disabled,false);
assert.match(elements['script-cancellation-status'].textContent,/not confirmed/);
assert.deepStrictEqual(resets,['btn-pause-script']);
assert.strictEqual(toasts.at(-1)[1],'warning');assert(toasts.at(-1)[0].includes('Cancellation was not confirmed'));assert(toasts.at(-1)[0].includes('current task state'));assert(toasts.at(-1)[0].includes('server refused'));
context.API.post=async()=>{throw new Error('rate limit');};
let success=0;
assert.strictEqual(await context.cancelTask('/custom/cancel',{onSuccess:()=>success++,errorMessage:e=>'Stop rejected: '+e.message,toastType:'error'}),false);
assert.strictEqual(success,0);
assert.deepStrictEqual(toasts.at(-1),['Stop rejected: rate limit','error']);
context.API.post=async()=>({status:'not_running'});
assert.strictEqual(await context.cancelTask('/idle/cancel'),true);
assert.strictEqual(success,0);
// Terminal polling beats a late acknowledgement; it must not lock the next run.
let lateResolve;context.API.post=()=>new Promise(resolve=>lateResolve=resolve);
const late=context.cancelScript();context.clearScriptCancellation('script');
lateResolve({});assert.strictEqual(await late,true);
assert.strictEqual(elements['btn-cancel-script'].disabled,false);
assert.strictEqual(elements['script-cancellation-status'].hidden,true);
context._batchPauseResume=()=>{};
load('let scriptBatchStartOperation =', 'window.pauseResumeBatchScript');
let batchResolve;context.API.post=()=>new Promise(resolve=>batchResolve=resolve);
const batch=context.cancelBatchScript();assert.strictEqual(elements['btn-cancel-batch-script'].disabled,true);
assert.strictEqual(await context.cancelBatchScript(),false);
batchResolve({});assert.strictEqual(await batch,true);
assert.match(elements['script-cancellation-status'].textContent,/Cancelling batch script generation/);
let terminal;context.syncSnapshotButton=()=>{};context.pollLogs=(_task,_el,done)=>terminal=done;
load('function pollScriptLogs(', '// Manual transport');
context.pollScriptLogs('script');terminal({running:false});
assert.strictEqual(elements['btn-cancel-batch-script'].disabled,true,'single-book completion must not release batch cancellation');
let batchPoll;
context.createTaskLogRenderer=()=>()=>{};context._startPolling=(_task,_fetch,options)=>batchPoll=options;
context.renderManualRequest=()=>{};context._showTaskRecoveryPanel=()=>{};context.notifyJobDone=()=>{};context.loadSavedScripts=()=>{};
load('function _pollScriptBatchLogs()', '// --- Single review');
context._pollScriptBatchLogs();batchPoll.onDone({running:false,tasks:[]});
assert.strictEqual(elements['btn-cancel-batch-script'].disabled,false);
assert.strictEqual(elements['script-cancellation-status'].hidden,true);
// Actual single-book poll callback releases the request even before its response.
let lastResolve;context.API.post=()=>new Promise(resolve=>lastResolve=resolve);
const last=context.cancelScript();context.pollScriptLogs('script');terminal({running:false});
lastResolve({});await last;assert.strictEqual(elements['btn-cancel-script'].disabled,false);
assert.strictEqual(elements['script-cancellation-status'].hidden,true);
// A true result acknowledges the HTTP request; it does not assert the task has exited.
""")

    def test_drift_start_is_disabled_before_post_and_recovers_on_every_terminal_path(self):
        self.run_scenario(r"""
load('async function runDriftCheck(indices)', 'function updateChunkRow(chunk)');
const btn = elements['btn-drift-check'] = {disabled:false};
let resolvePost, rejectPost, poll, calls = 0, reloads = 0;
context.API.post = (url, payload) => {
    assert.strictEqual(url, '/api/chunks/drift_check');
    calls++;
    return new Promise((resolve,reject)=>{resolvePost=resolve;rejectPost=reject;});
};
context._startPolling = (key,get,options) => { assert.strictEqual(key,'logs:drift_check');poll=options; };
context.loadChunks = async force => {assert.strictEqual(force,true);reloads++;};
const first = context.runDriftCheck([2]);
assert.strictEqual(btn.disabled,true,'Disable before the HTTP request settles');
await context.runDriftCheck([9]);
assert.strictEqual(calls,1,'A second click must not issue another start request');
resolvePost({measured:true});
await first;
assert.strictEqual(btn.disabled,true);
await context.runDriftCheck();
assert.strictEqual(calls,1,'Remain disabled while the accepted task is polling');
await poll.onDone({logs:['Checked 1 chunk(s): 0 flagged.']});
assert.strictEqual(btn.disabled,false);
assert.strictEqual(reloads,1);
const unavailable = context.runDriftCheck();
assert.strictEqual(btn.disabled,true);
resolvePost({measured:false});
await unavailable;
assert.strictEqual(btn.disabled,false);
assert.strictEqual(toasts.at(-1)[1],'warning');
const rejected = context.runDriftCheck();
assert.strictEqual(btn.disabled,true);
rejectPost(new Error('fixture busy'));
await rejected;
assert.strictEqual(btn.disabled,false);
assert(toasts.at(-1)[0].includes('Check the Voice Lab interpreter'));assert(toasts.at(-1)[0].includes('Details: fixture busy'));assert.strictEqual(toasts.at(-1)[1],'error');
const retry = context.runDriftCheck();
resolvePost({measured:true});
await retry;
await poll.onDone({logs:['Checked 1 chunk(s): 1 flagged.']});
assert.strictEqual(btn.disabled,false);
assert.strictEqual(calls,4);
// Automatic callers may have no visible button.
elements['btn-drift-check']=null;
const hidden = context.runDriftCheck([7]);
resolvePost({measured:true});
await hidden;
await poll.onDone({logs:['Checked 1 chunk(s): 0 flagged.']});
assert.strictEqual(calls,5);
""")

    def run_scenario(self, scenario):
        setup = r'''
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const elements = {};
const posts = [];
const toasts = [];
let bannersRemoved = 0;
const context = {
    currentBookFilename: 'fixture-book',
    invalidateEditorIntegrity() {}, refreshEditorIntegrity: async () => {},
    performance: {now: () => 0},
    currentIsRemote:false, failoverIsRemote:false,
    document: {
        getElementById(id) {
            if (id === 'voice-save-drafts') { return null; }
            if (!elements[id]) { elements[id] = {innerHTML:'',style:{},textContent:'',dataset:{},addEventListener(){}}; }
            return elements[id];
        },
        querySelectorAll(selector) {
            if (selector === '.voice-suggestion') {
                return [{remove() { bannersRemoved++; }}];
            }
            if (selector === '#nick-alias-rows .nick-alias-row') {
                return [{querySelector: sel => ({value:sel === '.nick-alias' ? ' nickname ' : ' character '})}];
            }
            throw new Error('Unexpected selector: ' + selector);
        }
    },
    API: {
        get: async () => ({OLD:'CHARACTER'}),
        post: async (url, data) => { posts.push({url,data}); return {count:1}; }
    },
    escapeHtml: value => String(value),
    showToast: (...args) => toasts.push(args),
    console: {error() {}, log() {}},
    renderCastMembers() {}, setTimeout:()=>1, clearTimeout(){}, addEventListener(){},
    localStorage:{getItem(){return null;},setItem(){},removeItem(){}}
};
context.window = context;
vm.createContext(context);vm.runInContext(source.slice(source.indexOf('function showActionError('),source.indexOf('function showConfirm(')),context);
const run = code => vm.runInContext(code, context);
function load(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert(start >= 0 && end > start);
    run(source.slice(start, end));
}
const plain = value => JSON.parse(JSON.stringify(value));
load('// Voice roster dropdowns:', '// End voice roster dropdowns.');
load('function escapeHtml(', '// Parse a numeric input');load('async function confirmIfRemote(', '// navigator.clipboard');
load('function createSerializedSaveQueue(', '// --- API Helpers ---');
if(source.includes('function getLoraModelsById(')){load('function getLoraModelsById(', 'async function suggestVoices(');}
load('let _voiceStatusClearTimer =', '// Auto-save on any change inside the voices list');
run('let cachedChunks=[];');
'''
        result = subprocess.run(
            ["node", "-e", setup + "\n(async () => {\n" + scenario + r'''
})().catch(error => { console.error(error); process.exitCode = 1; });
''', str(SOURCE)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_failed_alias_refresh_preserves_edits_and_disarms_save_until_retry(self):
        self.run_scenario(r'''
// Include the load-state declaration when present, without requiring it on baseline.
const stateMarker = source.includes('let characterAliasesLoaded')
    ? 'let characterAliasesLoaded' : 'async function loadCharacterAliases(show)';
load(stateMarker, '// One-line "N changes:');
await context.loadCharacterAliases(true);
assert.match(elements['nickname-aliases-panel'].innerHTML, /CHARACTER/);
elements['nickname-aliases-panel'].innerHTML = 'unsaved alias form';
context.API.get = async () => { throw new Error('offline'); };
await context.loadCharacterAliases(true);
assert.strictEqual(elements['nickname-aliases-panel'].innerHTML, 'unsaved alias form');
await context.saveCharacterAliases();
assert.strictEqual(posts.length, 0, 'failed load must not submit a whole-map replacement');
assert(toasts.some(args => String(args[0]).includes('load')));
context.API.get = async () => ({});
await context.loadCharacterAliases(true);
assert.match(elements['nickname-aliases-panel'].innerHTML, /No aliases yet/);
await context.saveCharacterAliases();
assert.deepStrictEqual(plain(posts), [{url:'/api/character_aliases',data:{nickname:'character'}}]);
''')

    def test_cast_refresh_clears_suggestions_only_when_selected_cast_changes(self):
        self.run_scenario(r'''
load('function clearVoiceSuggestions()', '// --- Series Cast:');
load('async function loadCastLibrary()', 'function getSelectedCastObj()');
context._selectedCast = 'old';
context._voiceSuggestions = {CHARACTER:{adapter_id:'old-adapter'}};
context.API.get = async () => ({casts:[{name:'replacement'}],current_characters:[]});
await context.loadCastLibrary();
assert.strictEqual(context._selectedCast, 'replacement');
assert.deepStrictEqual(plain(context._voiceSuggestions), {});
assert.strictEqual(bannersRemoved, 1);
assert.strictEqual(elements['btn-apply-all-suggestions'].style.display, 'none');
context._voiceSuggestions = {CHARACTER:{adapter_id:'keep'}};
await context.loadCastLibrary();
assert.strictEqual(context._voiceSuggestions.CHARACTER.adapter_id, 'keep');
assert.strictEqual(bannersRemoved, 1);
context.API.get = async () => ({casts:[],current_characters:[]});
await context.loadCastLibrary();
assert.strictEqual(context._selectedCast, '');
assert.deepStrictEqual(plain(context._voiceSuggestions), {});
assert.strictEqual(bannersRemoved, 2);
''')

    def test_partial_alias_rows_block_whole_map_save_but_blank_rows_allow_clear(self):
        self.run_scenario(r'''
load('let characterAliasesLoaded', '// One-line "N changes:');
await context.loadCharacterAliases(true);
for (const [alias, canonical] of [['NAME',''],['','NAME']]) {
    context.document.querySelectorAll = () => [
        {querySelector: sel => ({value:sel === '.nick-alias' ? 'valid' : 'TARGET'})},
        {querySelector: sel => ({value:sel === '.nick-alias' ? alias : canonical})}
    ];
    await context.saveCharacterAliases();
    assert.strictEqual(posts.length, 0, 'partial row must block replacement, not silently disappear');
}
assert(toasts.some(args => String(args[0]).includes('alias')));
context.document.querySelectorAll = () => [{querySelector: () => ({value:'  '})}];
await context.saveCharacterAliases();
assert.deepStrictEqual(plain(posts), [{url:'/api/character_aliases',data:{}}]);
''')

    def test_export_failures_do_not_download_and_explicit_success_does(self):
        self.run_scenario(r'''
const marker = source.includes('function isExportComplete(')
    ? 'function isExportComplete(' : 'window.exportAudacity =';
load(marker, '// --- Chapter-by-chapter export ---');
load('window.exportM4B =', '// --- Polling Logic ---');
let poll;
let downloads = 0;
context._startPolling = (task, get, callbacks) => { poll = callbacks; };
context.setTimeout = () => 1;
context.document.createElement = () => ({click() { downloads++; }});
context.document.body = {appendChild() {},removeChild() {}};
for (const [name,id] of [['exportAudacity','audacity-status'],['exportM4B','m4b-status']]) {
    for (const message of ['Export failed: incomplete audio', 'Export error: could not complete', 'Export failed: previous Export complete: stale']) {
        await context[name]();
        const before = downloads;
        poll.onDone({running:false,logs:[message],result:{status:'failed',message}});
        assert.strictEqual(downloads, before, name + ': failed export must not download');
        assert.match(elements[id].innerHTML, /text-danger/);
    }
    await context[name]();
    const before = downloads;
    poll.onDone({running:false,logs:['Starting export...', 'Export complete: saved'],result:{status:'done',message:'Export complete: saved'}});
    assert.strictEqual(downloads, before + 1);
    assert.match(elements[id].innerHTML, /Done!/);
    await context[name]();
    const afterSuccess = downloads;
    poll.onDone({running:false,logs:['Export complete: saved']});
    assert.strictEqual(downloads, afterSuccess, name + ': missing structured result must not download');
}
''')

    def test_old_undo_toast_cannot_restore_latest_deletion(self):
        self.run_scenario(r'''
load('let _lastDeleted =', 'window.stopOthers =');
const toastNodes = [];
context.Date = {now:() => 1};
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.loadChunks = async () => {};
context.fetch = async url => ({json:async () => ({deleted:{speaker:url,text:url},undo_token:"receipt"+url})});
context.API._handleError = async () => {};
context.bootstrap = {Toast:class {
    constructor() {}
    show() {}
    static getInstance() { return {hide() {}}; }
}};
context.document.createElement = () => ({
    set innerHTML(value) {
        const id = value.match(/id="([^"]+)"/)[1];
        const node = {id,addEventListener() {},remove() {},querySelector: () => ({addEventListener() {}})};
        this.firstElementChild = node;
        toastNodes.push(node);
    }
});
elements['toast-container'] = {appendChild() {}};
await context.deleteChunk(1);
await context.deleteChunk(2);
await context.undoDeleteChunk(toastNodes[0].id);
assert.strictEqual(posts.length, 0, 'old toast must not restore another deletion');
await context.undoDeleteChunk(toastNodes[1].id);
assert.deepStrictEqual(plain(posts), [{url:'/api/chunks/restore',data:{chunk:{speaker:'/api/chunks/2',text:'/api/chunks/2'},at_index:2,undo_token:'receipt/api/chunks/2'}}]);
await context.deleteChunk(3);
let release;
context.API.post = (url,data) => {
    posts.push({url,data});
    return new Promise(resolve => { release = resolve; });
};
const restoring = context.undoDeleteChunk(toastNodes[2].id);
await Promise.resolve();
const countBeforeDoubleClick = posts.length;
await context.undoDeleteChunk(toastNodes[2].id);
assert.strictEqual(posts.length, countBeforeDoubleClick, 'pending undo is claimed once');
await context.deleteChunk(4);
release();
await restoring;
context.API.post = async (url,data) => { posts.push({url,data}); };
await context.undoDeleteChunk(toastNodes[3].id);
assert.strictEqual(posts.length, 3, 'an earlier restore must preserve the newer undo');
assert.strictEqual(posts[2].data.at_index, 4);
''')

    def test_row_save_includes_speaker_select_and_waits_for_post(self):
        self.run_scenario(r'''
load('const pendingChunkEdits =', 'window.generateChunk =');
function control(field, value) {
    return {value, getAttribute: () => `updateChunk(7, '${field}', this.value)`};
}
const inputs = [control('text','line'),control('pause_after','')];
const speaker = control('speaker','NEW SPEAKER');
context.document.querySelector = () => ({querySelectorAll(selector) {
    return selector.split(',').map(s => s.trim()).includes('select') ? [...inputs,speaker] : inputs;
}});
let release;
context.API.post = (url,data) => {
    posts.push({url,data});
    return new Promise(resolve => { release = resolve; });
};
let completed = false;
const pending = context.saveRowEdits(7).then(() => { completed = true; });
await Promise.resolve();
assert.deepStrictEqual(plain(posts), [{url:'/api/chunks/7',data:{text:'line',pause_after:null,speaker:'NEW SPEAKER'}}]);
assert.strictEqual(completed, false);
release();
await pending;
assert.strictEqual(completed, true);
''')


if __name__ == "__main__":
    unittest.main()
