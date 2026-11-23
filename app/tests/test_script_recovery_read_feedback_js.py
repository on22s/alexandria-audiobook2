"""Failed recovery reads stay explicit, owned, and recoverable without a blind retry."""
from pathlib import Path
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'


class ScriptRecoveryReadFeedbackJsTests(unittest.TestCase):
    def run_scenario(self, scenario):
        setup = r'''
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const elements = {}, toasts = [], posts = [], copied = [], confirms = [], keys = [], clicked = [], polls = [];
let getReply = null, getError = null;
const element = id => elements[id] || (elements[id] = {
    style: {display: 'none'}, innerHTML: '', textContent: '', value: '', disabled: false,
    classList: {add() {}, remove() {}}, click() { clicked.push(id); }
});
const deferred = () => {
    let resolve, reject;
    const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
    return {promise, resolve, reject};
};
const flush = async () => { for (let i = 0; i < 4; i++) { await new Promise(done => setImmediate(done)); } };
const context = {
    document: {getElementById: element},
    currentBookFilename: 'book-A',
    console: {debug() {}, error() {}},
    describeApiError: error => (error && error.message) || String(error),
    showToast: (...args) => toasts.push(args),
    copyToClipboard: async (text, subject) => copied.push({text, subject}),
    showConfirm: async () => { confirms.push(true); return true; },
    formatUiSafetyText: (key, english) => english,
    scriptBatchPoller: null,
    pollScriptLogs: (task, done, token) => polls.push([task, status => {
        if (task === 'script') { context.onScriptTaskTerminal(status, token); }
        done?.(status);
    }]),
    _resetPauseBtn: () => {},
    ensureCastListEditsDiscardable: async () => true,
    loadCastList: async () => {},
    clearCastListEditor: () => {},
    t: (key, english, values = {}) => {
        keys.push(key);
        return String(english).replace(/\{(\w+)\}/g,
            (match, name) => Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match);
    },
    escapeHtml: value => String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
    API: {
        get: async () => { if (getError) { throw getError; } return getReply; },
        post: async (url, data) => { posts.push({url, data}); return {resolution: 'accepted'}; }
    }
};
context.window = context;
vm.createContext(context);
const run = code => vm.runInContext(code, context);
function load(start, end) {
    const at = source.indexOf(start), stop = source.indexOf(end, at);
    assert(at >= 0 && stop > at, 'missing source slice ' + start);
    run(source.slice(at, stop));
}
load('function showActionError(', 'function formatServerValues(');
load('function applyCurrentBookFilename(', 'function enqueueBookSelection(');
load('function enqueueBookSelection(', 'function getCurrentBookName(');
load('window.selectExistingScriptUpload =', 'window.openRetainedDocxDetails =');
load('function scriptRecoveryContextMatches(', 'async function refreshScriptRecovery()');
load('async function refreshScriptRecovery()', '// A fenced run cannot resume');
load('function renderCastChangedRecovery(', '// Failed-request recovery panel');
load('function renderScriptRecovery(detail)', 'async function onCopyRecoveryPrompt()');
load('async function onCopyRecoveryPrompt()', 'function renderRecoveryFindings(');
load('function renderRecoveryFindings(', 'async function onInjectRecoverySegmentation(');
load('async function onInjectRecoverySegmentation()', 'async function onSkipRecoveryChunk()');
load('async function onSkipRecoveryChunk()', 'window.retryScriptGeneration =');
load('window.retryScriptGeneration =', '// --- Script Batch Mode ---');
function reset() {
    toasts.length = posts.length = copied.length = confirms.length = keys.length = clicked.length = polls.length = 0;
    for (const id of Object.keys(elements)) { delete elements[id]; }
    context.currentBookFilename = 'book-A';
    context._bookSelectionEpoch = 0;
    context._selectedUploadIdentity = null;
    context._scriptRecoveryBookEpoch = 0;
    context._scriptRecoveryGeneration = 0;
    context._scriptRecoveryPending = false;
    context._scriptRecoveryRunActive = false;
    context._scriptRetryGeneration = 0;
    context._scriptRetryAction = null;
    context._scriptRecoveryDetail = undefined;
    context._scriptRecoveryConfirmed = context.makeScriptRecoveryStatus(0, false);
    context.showConfirm = async () => { confirms.push(true); return true; };
    context.ensureCastListEditsDiscardable = async () => true;
    element('existing-upload-select').value = 'book-B.txt';
    context.API.get = async () => { if (getError) { throw getError; } return getReply; };
    context.API.post = async (url, data) => { posts.push({url, data}); return {resolution: 'accepted'}; };
    element('btn-retry-script').style.display = 'inline-block';
    element('btn-gen-script').disabled = true;
    element('script-recovery-panel').innerHTML = '<div>previous recovery card</div>';
    getError = getReply = null;
}
const detail = () => ({failed_chunk: 3, chunks_total: 10, failed_pass: 'segment', chunks_done: 2,
    source: 'a source line', last_error: {category: 'timeout'}, attempts: [],
    prompt: {system: 'SYS', user: 'USER'}});
const retryPosts = () => posts.filter(entry => entry.url === '/api/generate_script/retry');
const confirmRecoverable = () => {
    context._scriptRecoveryConfirmed = context.makeScriptRecoveryStatus(context._scriptRecoveryGeneration || 0, true);
};
'''
        result = subprocess.run(
            ['node', '-e', setup + r'''
let finished=false,sentinelChecks=0;process.on('beforeExit',()=>{if(!finished&&sentinelChecks++<5){setImmediate(()=>{});return;}assert(finished,'scenario never reached its completion sentinel');});
(async () => {
''' + scenario + r'''
finished=true;
})().catch(error => { console.error(error); process.exitCode = 1; });
''', str(SOURCE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_failed_read_shows_explicit_unknown_state_without_offering_retry(self):
        self.run_scenario(r'''
reset();
const stale = {failed_chunk: 7, failed_pass: 'segment', prompt: {system: 'S', user: 'U'}};
context._scriptRecoveryDetail = stale;
getError = Error('offline read failed');
await context.refreshScriptRecovery();
const panel = element('script-recovery-panel');
assert.strictEqual(element('btn-retry-script').style.display, 'none');
assert.notStrictEqual(panel.style.display, 'none');
assert(panel.innerHTML.includes('Recovery status could not be read'));
assert(panel.innerHTML.includes('does not mean the run is unrecoverable'));
assert(panel.innerHTML.includes('Check recovery status'));
assert(panel.innerHTML.includes('onclick="refreshScriptRecovery()"'));
assert(!panel.innerHTML.includes('Reload the page'));
assert(!panel.innerHTML.includes('Script generation needs your help'));
assert(panel.innerHTML.includes('role="status"'));
assert.strictEqual(context._scriptRecoveryDetail, null);
assert.strictEqual(posts.length, 0);
assert.strictEqual(toasts.length, 1);
assert.strictEqual(toasts[0][1], 'warning');
assert(toasts[0][0].includes('Recovery status could not be read'));
assert(toasts[0][0].includes('Retry stays unavailable until the status is confirmed'));
assert(toasts[0][0].includes('Use Check recovery status'));
assert(toasts[0][0].includes('offline read failed'));
assert.strictEqual(element('btn-gen-script').disabled, true);
assert(keys.includes('script.recovery.status-unreadable'));
assert(keys.includes('script.recovery.status-unreadable-check'));
assert(keys.includes('script.recovery.status-unreadable-detail'));
assert(keys.includes('script.recovery.status-unreadable-recovery'));
''')

    def test_failed_read_escapes_transport_detail_and_keeps_preview_and_paste_inert(self):
        self.run_scenario(r'''
reset();
getError = Error('<img src=x onerror="boom()"> could not be read');
await context.refreshScriptRecovery();
const panel = element('script-recovery-panel');
assert(!panel.innerHTML.includes('<img'));
assert(panel.innerHTML.includes('&lt;img'));
element('script-recovery-inject').value = '[{"type":"SPOKEN","text":"x"}]';
await context.onCopyRecoveryPrompt();
await context.onInjectRecoverySegmentation();
await context.onSkipRecoveryChunk();
assert.strictEqual(posts.length, 0);
assert.strictEqual(copied.length, 0);
assert.strictEqual(confirms.length, 0);
''')

    def test_check_recovery_status_control_rereads_and_restores_controls_after_connection_returns(self):
        self.run_scenario(r'''
reset();
let reads = 0;
context.API.get = async path => {
    assert.strictEqual(path, '/api/generate_script/recovery?include_detail=true');
    reads++;
    if (reads === 1) { throw Error('connection lost'); }
    return {recoverable: true, detail: detail(), failed_pass: 'segment'};
};
await context.refreshScriptRecovery();
const panel = element('script-recovery-panel');
assert(panel.innerHTML.includes('Check recovery status'));
assert.strictEqual(element('btn-retry-script').style.display, 'none');
const onclick = panel.innerHTML.match(/onclick="([^"]+)"/);
assert(onclick, 'the card must carry an actionable control');
assert.strictEqual(onclick[1], 'refreshScriptRecovery()');
await run(onclick[1]);
assert.strictEqual(reads, 2);
assert(panel.innerHTML.includes('Script generation needs your help'));
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
assert.strictEqual(context._scriptRecoveryDetail.failed_chunk, 3);
assert.strictEqual(posts.length, 0, 'a status check must never mutate');
''')

    def test_confirmed_absence_stays_distinct_from_failed_read(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: false};
await context.refreshScriptRecovery();
assert.strictEqual(element('btn-retry-script').style.display, 'none');
assert.strictEqual(element('script-recovery-panel').style.display, 'none');
assert.strictEqual(element('script-recovery-panel').innerHTML, '');
assert.strictEqual(toasts.length, 0);
''')

    def test_recoverable_read_keeps_retry_detail_and_paste_controls(self):
        self.run_scenario(r'''
reset();
const d = detail();
getReply = {recoverable: true, detail: d, failed_pass: 'segment'};
await context.refreshScriptRecovery();
const panel = element('script-recovery-panel');
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
assert.strictEqual(panel.style.display, '');
assert(panel.innerHTML.includes('Script generation needs your help'));
assert(panel.innerHTML.includes('id="script-recovery-inject"'));
assert(panel.innerHTML.includes('Retry this chunk'));
assert.strictEqual(context._scriptRecoveryDetail, d);
assert.strictEqual(toasts.length, 1);
assert.strictEqual(toasts[0][1], 'warning');
assert(toasts[0][0].includes('resume from its checkpoint at segment'));
''')

    def test_start_over_required_keeps_cast_change_guidance(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: false, start_over_required: true, detail: 'Cast changed for this run'};
await context.refreshScriptRecovery();
assert.strictEqual(element('btn-retry-script').style.display, 'none');
const panel = element('script-recovery-panel');
assert(panel.innerHTML.includes('Approved cast changed'));
assert(panel.innerHTML.includes('Start over'));
assert(panel.innerHTML.includes('Cast changed for this run'));
assert.strictEqual(toasts.length, 1);
assert.strictEqual(toasts[0][1], 'warning');
''')

    def test_a_later_successful_read_clears_the_unknown_state(self):
        self.run_scenario(r'''
reset();
getError = Error('offline');
await context.refreshScriptRecovery();
assert(element('script-recovery-panel').innerHTML.includes('could not be read'));
getError = null;
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
await context.refreshScriptRecovery();
const panel = element('script-recovery-panel');
assert(panel.innerHTML.includes('Script generation needs your help'));
assert(!panel.innerHTML.includes('could not be read'));
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
''')

    def test_existing_selection_guard_still_blocks_a_changed_book_fallback(self):
        self.run_scenario(r'''
reset();
context._scriptRecoveryDetail = {failed_chunk: 7, failed_pass: 'segment'};
context.showConfirm = async () => { context.currentBookFilename = 'book-B'; return true; };
await context.onSkipRecoveryChunk();
assert.strictEqual(posts.length, 0);
''')

    def test_held_book_a_read_cannot_paint_after_confirmed_switch_to_book_b(self):
        self.run_scenario(r'''
reset();
const pending = [];
context.API.get = async path => {
    if (path.includes('include_detail=true')) { const call = deferred(); pending.push(call); return call.promise; }
    return {recoverable: true};
};
const a = context.refreshScriptRecovery();
await flush();
assert.strictEqual(pending.length, 1);
run('applyCurrentBookFilename("book-B")');
assert.strictEqual(context.currentBookFilename, 'book-B');
await flush();
assert.strictEqual(pending.length, 2);
const b = context.refreshScriptRecovery();
await flush();
assert.strictEqual(pending.length, 3);
pending[1].resolve({recoverable: false});
pending[2].reject(Error('B unavailable'));
await b;
await flush();
const panel = element('script-recovery-panel');
assert(panel.innerHTML.includes('Check recovery status'));
pending[0].resolve({recoverable: true, detail: detail(), failed_pass: 'segment'});
await a;
assert(panel.innerHTML.includes('Check recovery status'));
assert(!panel.innerHTML.includes('Script generation needs your help'));
assert.strictEqual(element('btn-retry-script').style.display, 'none');
assert.strictEqual(context._scriptRecoveryDetail, null);
assert(!toasts.some(args => String(args[0]).includes('resume from its checkpoint')));
assert.strictEqual(posts.length, 0);
''')

    def test_old_failure_after_newer_success_does_not_erase_valid_controls(self):
        self.run_scenario(r'''
reset();
const pending = [];
context.API.get = async path => {
    if (path.includes('include_detail=true')) { const call = deferred(); pending.push(call); return call.promise; }
    return {recoverable: true};
};
const old = context.refreshScriptRecovery();
await flush();
const fresh = context.refreshScriptRecovery();
await flush();
const freshDetail = detail();
pending[1].resolve({recoverable: true, detail: freshDetail, failed_pass: 'segment'});
await fresh;
const panel = element('script-recovery-panel');
assert(panel.innerHTML.includes('Script generation needs your help'));
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
pending[0].reject(Error('old read failed'));
await old;
assert(panel.innerHTML.includes('Script generation needs your help'));
assert.strictEqual(context._scriptRecoveryDetail, freshDetail);
assert.strictEqual(toasts.filter(args => String(args[0]).includes('resume from its checkpoint')).length, 1);
assert.strictEqual(posts.length, 0);
''')

    def test_same_book_overlap_and_same_name_reload_invalidate_stale_reads(self):
        self.run_scenario(r'''
reset();
const pending = [];
context.API.get = async path => {
    if (path.includes('include_detail=true')) { const call = deferred(); pending.push(call); return call.promise; }
    return {recoverable: true};
};
const first = context.refreshScriptRecovery();
await flush();
const second = context.refreshScriptRecovery();
await flush();
const newer = detail();
pending[1].resolve({recoverable: true, detail: newer, failed_pass: 'segment'});
await second;
pending[0].resolve({recoverable: false});
await first;
assert.strictEqual(context._scriptRecoveryDetail, newer);
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
run('applyCurrentBookFilename("book-A")');
await flush();
assert.strictEqual(pending.length, 3);
pending[2].resolve({recoverable: false});
await flush();
assert.strictEqual(context._scriptRecoveryDetail, null);
assert.strictEqual(element('script-recovery-panel').innerHTML, '');
assert.strictEqual(element('btn-retry-script').style.display, 'none');
assert(context._scriptRecoveryGeneration >= 3);
''')

    def test_switch_back_to_the_original_book_still_needs_a_fresh_read(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
await context.refreshScriptRecovery();
assert(context._scriptRecoveryDetail);
run('applyCurrentBookFilename("book-B")');
run('applyCurrentBookFilename("book-A")');
assert.strictEqual(context._scriptRecoveryDetail, null);
await flush();
assert.strictEqual(context._scriptRecoveryDetail.failed_chunk, 3);
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
''')

    def test_retry_with_unconfirmed_or_stale_metadata_never_posts(self):
        self.run_scenario(r'''
reset();
confirmRecoverable();
let meta;
context.API.get = async path => {
    if (path.includes('include_detail=true')) { throw Error('offline'); }
    meta = deferred();
    return meta.promise;
};
const retry = context.window.retryScriptGeneration();
await flush();
meta.resolve({recoverable: false});
await retry;
assert.strictEqual(retryPosts().length, 0);
assert(toasts.some(args => String(args[0]).includes('no longer confirmed')));
''')

    def test_retry_with_recoverable_true_on_a_stale_generation_is_suppressed(self):
        self.run_scenario(r'''
reset();
confirmRecoverable();
const meta = deferred();
context.API.get = async path => {
    if (path.includes('include_detail=true')) { return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return meta.promise;
};
const retry = context.window.retryScriptGeneration();
await flush();
await context.refreshScriptRecovery();
meta.resolve({recoverable: true});
await retry;
assert.strictEqual(retryPosts().length, 0);
''')

    def test_apply_ack_failed_refresh_suppresses_in_flight_retry_without_mutations(self):
        self.run_scenario(r'''
reset();
context._scriptRecoveryDetail = detail();
element('script-recovery-inject').value = '[{"type":"NARRATOR","text":"x"}]';
const detailReads = [], meta = deferred();
context.API.get = async path => {
    if (path.includes('include_detail=true')) { const call = deferred(); detailReads.push(call); return call.promise; }
    return meta.promise;
};
const apply = context.onInjectRecoverySegmentation();
await flush();
assert.strictEqual(posts.filter(entry => entry.url === '/api/generate_script/inject').length, 1);
assert.strictEqual(detailReads.length, 1);
const retry = context.window.retryScriptGeneration();
await flush();
detailReads[0].reject(Error('connection lost'));
await apply;
meta.resolve({recoverable: true});
await retry;
assert.strictEqual(retryPosts().length, 0);
assert.strictEqual(posts.length, 1);
''')

    def test_fresh_recoverable_retry_posts_and_legacy_confirmation_is_rechecked(self):
        self.run_scenario(r'''
reset();
confirmRecoverable();
const tokens = {checkpoint_token: 'c'.repeat(64), book_token: 'b'.repeat(64)};
context.API.get = async path => {
    if (path.includes('include_detail=true')) { throw Error('offline'); }
    return {recoverable: true, legacy_confirmation: tokens};
};
await context.window.retryScriptGeneration();
assert.strictEqual(retryPosts().length, 1);
assert.strictEqual(retryPosts()[0].data.legacy_checkpoint_token, tokens.checkpoint_token);
assert.strictEqual(retryPosts()[0].data.legacy_book_token, tokens.book_token);
assert.strictEqual(confirms.length, 1);
posts.length = 0;
context.showConfirm = async () => { run('applyCurrentBookFilename("book-B")'); return true; };
await context.window.retryScriptGeneration();
assert.strictEqual(retryPosts().length, 0);
''')

    def test_retry_after_apply_ack_never_posts_and_pending_actions_are_inert(self):
        self.run_scenario(r'''
for (const order of ['detail-first', 'meta-first']) {
 reset();
 confirmRecoverable();
 const detailReads = [];
 context.API.get = async path => {
    if (path.includes('include_detail=true')) { const call = deferred(); detailReads.push(call); return call.promise; }
    return deferred().promise;
 };
 context._scriptRecoveryDetail = detail();
 element('script-recovery-inject').value = '[{"type":"NARRATOR","text":"x"}]';
 const apply = context.onInjectRecoverySegmentation();
 await flush();
 assert.strictEqual(posts.filter(entry => entry.url === '/api/generate_script/inject').length, 1);
 assert.strictEqual(detailReads.length, 1);
 assert.strictEqual(context._scriptRecoveryPending, true);
 assert.strictEqual(context._scriptRecoveryDetail, null);
 await context.onCopyRecoveryPrompt();
 await context.onInjectRecoverySegmentation();
 await context.onSkipRecoveryChunk();
 assert.strictEqual(copied.length, 0);
 assert.strictEqual(confirms.length, 0);
 assert.strictEqual(posts.filter(entry => entry.url === '/api/generate_script/inject').length, 1);
 assert.strictEqual(element('btn-retry-script').style.display, 'none');
 const retry = context.window.retryScriptGeneration();
 await flush();
 if (order === 'detail-first') {
    detailReads[0].resolve({recoverable: true, detail: detail(), failed_pass: 'segment'});
    await apply;
 } else {
    detailReads[0].resolve({recoverable: true, detail: detail(), failed_pass: 'segment'});
 }
 await retry;
 assert.strictEqual(retryPosts().length, 0, order);
 assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
}
''')

    def test_stale_book_a_rejection_does_not_warn_or_read_after_book_b_success(self):
        self.run_scenario(r'''
reset();
const reads = [];
let meta;
context.API.get = async path => {
    reads.push(path);
    if (path.includes('include_detail=true')) { return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    meta = deferred();
    return meta.promise;
};
await context.refreshScriptRecovery();
const retry = context.window.retryScriptGeneration();
await flush();
run('applyCurrentBookFilename("book-B")');
await context.refreshScriptRecovery();
const panel = element('script-recovery-panel');
const bDetail = context._scriptRecoveryDetail;
const readsBefore = reads.length, toastsBefore = toasts.length;
meta.reject(Error('book A metadata rejected'));
await retry;
assert.strictEqual(toasts.length, toastsBefore);
assert.strictEqual(reads.length, readsBefore);
assert.strictEqual(context._scriptRecoveryDetail, bDetail);
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
assert.strictEqual(retryPosts().length, 0);
''')

    def test_absence_and_fence_clear_detail_so_fallback_and_copy_stay_inert(self):
        self.run_scenario(r'''
reset();
for (const reply of [{recoverable: false}, {recoverable: false, start_over_required: true, detail: 'fenced'}]) {
    getReply = reply;
    await context.refreshScriptRecovery();
    assert.strictEqual(context._scriptRecoveryDetail, null);
    const postsBefore = posts.length, confirmsBefore = confirms.length, copiedBefore = copied.length;
    await context.onCopyRecoveryPrompt();
    await context.onInjectRecoverySegmentation();
    await context.onSkipRecoveryChunk();
    assert.strictEqual(posts.length, postsBefore);
    assert.strictEqual(confirms.length, confirmsBefore);
    assert.strictEqual(copied.length, copiedBefore);
}
''')

    def test_declined_selector_keeps_held_detail_read_and_valid_retry(self):
        self.run_scenario(r'''
reset();
const reads = [];
context.API.get = async path => {
    if (path.includes('include_detail=true')) { const call = deferred(); reads.push(call); return call.promise; }
    return {recoverable: true};
};
const a = context.refreshScriptRecovery();
await flush();
assert.strictEqual(context._scriptRecoveryPending, true);
let discard;
context.ensureCastListEditsDiscardable = () => { discard = deferred(); return discard.promise; };
const selection = context.window.selectExistingScriptUpload();
await flush();
discard.resolve(false);
await selection;
assert.strictEqual(context.currentBookFilename, 'book-A');
reads[0].resolve({recoverable: true, detail: detail(), failed_pass: 'segment'});
await a;
const panel = element('script-recovery-panel');
assert(panel.innerHTML.includes('Script generation needs your help'));
assert(!panel.innerHTML.includes('Checking recovery status'));
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
assert.strictEqual(context._scriptRecoveryConfirmed.recoverable, true);
context.API.get = async path => {
    if (path.includes('include_detail=true')) { throw Error('offline'); }
    return {recoverable: true};
};
await context.window.retryScriptGeneration();
assert.strictEqual(retryPosts().length, 1);
''')

    def test_accepted_selector_switch_invalidates_a_held_detail_read(self):
        self.run_scenario(r'''
reset();
const reads = [];
context.API.get = async path => {
    if (path.includes('include_detail=true')) { const call = deferred(); reads.push(call); return call.promise; }
    return {recoverable: true};
};
const a = context.refreshScriptRecovery();
await flush();
let discard;
context.ensureCastListEditsDiscardable = () => { discard = deferred(); return discard.promise; };
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/uploads/select') {
        return {stored_filename: 'book-B.txt', book_identity_token: 'b'.repeat(64)};
    }
    return {resolution: 'accepted'};
};
const selection = context.window.selectExistingScriptUpload();
await flush();
discard.resolve(true);
await selection;
assert.strictEqual(context.currentBookFilename, 'book-B.txt');
reads[1].resolve({recoverable: false});
await flush();
reads[0].resolve({recoverable: true, detail: detail(), failed_pass: 'segment'});
await a;
assert.strictEqual(element('btn-retry-script').style.display, 'none');
assert.strictEqual(context._scriptRecoveryDetail, null);
assert(!element('script-recovery-panel').innerHTML.includes('needs your help'));
''')

    def test_late_a_retry_rejection_keeps_b_toolbar_disabled_and_one_post(self):
        self.run_scenario(r'''
reset();
confirmRecoverable();
const metaA = deferred();
context.API.get = async path => {
    if (path.includes('include_detail=true')) { return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return metaA.promise;
};
const retryA = context.window.retryScriptGeneration();
await flush();
run('applyCurrentBookFilename("book-B")');
await context.refreshScriptRecovery();
const postB = deferred();
context.API.get = async path => {
    if (path.includes('include_detail=true')) { return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return {recoverable: true};
};
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script/retry') { return postB.promise; }
    return {resolution: 'accepted'};
};
const retryB = context.window.retryScriptGeneration();
await flush();
assert.strictEqual(retryPosts().length, 1);
assert.strictEqual(element('btn-retry-script').disabled, true);
const toastsBefore = toasts.length;
metaA.reject(Error('book A rejected'));
await retryA;
assert.strictEqual(toasts.length, toastsBefore);
assert.strictEqual(element('btn-retry-script').disabled, true);
assert.strictEqual(retryPosts().length, 1);
postB.resolve({});
await retryB;
polls[0][1]();
await flush();
assert.strictEqual(element('btn-retry-script').disabled, false);
''')

    def test_late_a_retry_ack_does_not_hide_b_retry_controls(self):
        self.run_scenario(r'''
reset();
confirmRecoverable();
const postA = deferred();
context.API.get = async path => {
    if (path.includes('include_detail=true')) { return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return {recoverable: true};
};
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script/retry') { return postA.promise; }
    return {resolution: 'accepted'};
};
const retryA = context.window.retryScriptGeneration();
await flush();
assert.strictEqual(retryPosts().length, 1);
run('applyCurrentBookFilename("book-B")');
await context.refreshScriptRecovery();
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
postA.resolve({});
await retryA;
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
assert.strictEqual(element('btn-cancel-script').style.display, 'none');
''')

    def test_late_a_apply_and_fallback_rejections_do_not_repaint_b(self):
        self.run_scenario(r'''
reset();
context._scriptRecoveryDetail = detail();
const failApply = deferred(), failSkip = deferred();
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script/inject') { return failApply.promise; }
    if (url === '/api/generate_script/skip') { return failSkip.promise; }
    return {resolution: 'accepted'};
};
element('script-recovery-inject').value = '[{"type":"NARRATOR","text":"x"}]';
const apply = context.onInjectRecoverySegmentation();
await flush();
run('applyCurrentBookFilename("book-B")');
await context.refreshScriptRecovery();
const findings = element('script-recovery-findings');
const before = findings.innerHTML;
failApply.reject(Error('book A apply rejected'));
await apply;
assert.strictEqual(findings.innerHTML, before);
context._scriptRecoveryDetail = detail();
const skip = context.onSkipRecoveryChunk();
await flush();
run('applyCurrentBookFilename("book-C")');
await context.refreshScriptRecovery();
const beforeSkip = findings.innerHTML;
failSkip.reject(Error('book A fallback rejected'));
await skip;
assert.strictEqual(findings.innerHTML, beforeSkip);
''')

    def test_in_panel_retry_double_press_keeps_first_admission(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
await context.refreshScriptRecovery();
const panel = element('script-recovery-panel');
assert(panel.innerHTML.includes('onclick="retryScriptGeneration()"'));
const postA = deferred();
context.API.get = async path => {
    if (path.includes('include_detail=true')) { return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return {recoverable: true};
};
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script/retry') { return postA.promise; }
    return {resolution: 'accepted'};
};
const first = run('retryScriptGeneration()');
await flush();
assert.strictEqual(retryPosts().length, 1);
assert.strictEqual(element('btn-retry-script').disabled, true);
const second = run('retryScriptGeneration()');
await second;
assert.strictEqual(retryPosts().length, 1);
assert.strictEqual(polls.length, 0);
postA.resolve({});
await first;
polls[0][1]();
await flush();
assert.strictEqual(retryPosts().length, 1);
assert.strictEqual(polls.length, 1);
assert.strictEqual(element('btn-cancel-script').style.display, 'none');
assert.strictEqual(element('btn-retry-script').disabled, false);
assert(!toasts.some(args => String(args[0]).includes('was not confirmed')));
''')

    def test_toolbar_retry_double_press_keeps_first_admission(self):
        self.run_scenario(r'''
reset();
confirmRecoverable();
const postA = deferred();
context.API.get = async path => path.includes('include_detail=true')
    ? {recoverable: true, detail: detail(), failed_pass: 'segment'} : {recoverable: true};
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script/retry') { return postA.promise; }
    return {resolution: 'accepted'};
};
const first = context.window.retryScriptGeneration();
await flush();
const second = context.window.retryScriptGeneration();
await second;
assert.strictEqual(retryPosts().length, 1);
assert.strictEqual(polls.length, 0);
postA.resolve({});
await first;
polls[0][1]();
await flush();
assert.strictEqual(retryPosts().length, 1);
assert.strictEqual(polls.length, 1);
assert.strictEqual(element('btn-cancel-script').style.display, 'none');
''')

    def test_later_press_after_first_settlement_is_admitted_again(self):
        self.run_scenario(r'''
reset();
confirmRecoverable();
let count = 0;
const postA = deferred();
context.API.get = async path => path.includes('include_detail=true')
    ? {recoverable: true, detail: detail(), failed_pass: 'segment'} : {recoverable: true};
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script/retry') { return count++ === 0 ? postA.promise : {resolution: 'accepted'}; }
    return {resolution: 'accepted'};
};
const first = context.window.retryScriptGeneration();
await flush();
const overlap = context.window.retryScriptGeneration();
await overlap;
assert.strictEqual(retryPosts().length, 1);
postA.resolve({});
await first;
polls[0][1]();
await flush();
const later = context.window.retryScriptGeneration();
await later;
assert.strictEqual(retryPosts().length, 2);
''')

    def test_b_toolbar_is_enabled_while_stale_a_metadata_is_pending(self):
        self.run_scenario(r'''
reset();
confirmRecoverable();
const metaA = deferred();
context.API.get = async path => {
    if (path.includes('include_detail=true')) { return {recoverable: true, detail: null}; }
    return metaA.promise;
};
const retryA = context.window.retryScriptGeneration();
await flush();
assert.strictEqual(element('btn-retry-script').disabled, true);
let discard;
context.ensureCastListEditsDiscardable = () => { discard = deferred(); return discard.promise; };
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/uploads/select') {
        return {stored_filename: 'book-B.txt', book_identity_token: 'b'.repeat(64)};
    }
    return {resolution: 'accepted'};
};
const selection = context.window.selectExistingScriptUpload();
await flush();
discard.resolve(true);
await selection;
await context.refreshScriptRecovery();
assert.strictEqual(context.currentBookFilename, 'book-B.txt');
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
assert.strictEqual(element('btn-retry-script').disabled, false);
metaA.reject(Error('book A metadata rejected'));
await retryA;
assert.strictEqual(element('btn-retry-script').disabled, false);
assert.strictEqual(retryPosts().length, 0);
''')

    def test_current_preflight_rejection_reports_and_rereads(self):
        self.run_scenario(r'''
reset();
const d = detail();
getReply = {recoverable: true, detail: d, failed_pass: 'segment'};
await context.refreshScriptRecovery();
const meta = deferred();
let detailReads = 0;
context.API.get = async path => {
    if (path.includes('include_detail=true')) { detailReads++; return {recoverable: true, detail: d, failed_pass: 'segment'}; }
    return meta.promise;
};
const retry = context.window.retryScriptGeneration();
await flush();
const applyAck = deferred();
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script/inject') { return applyAck.promise; }
    return {resolution: 'accepted'};
};
const apply = context.onInjectRecoverySegmentation();
await flush();
applyAck.resolve({});
await apply;
await flush();
const panel = element('script-recovery-panel');
assert(panel.innerHTML.includes('Script generation needs your help'));
const readsBefore = detailReads, toastsBefore = toasts.length;
meta.reject(Error('current preflight metadata'));
await retry;
await flush();
assert(detailReads > readsBefore);
assert(toasts.length > toastsBefore);
assert(panel.innerHTML.includes('Script generation needs your help'));
assert.strictEqual(retryPosts().length, 0);
assert.strictEqual(polls.length, 0);
''')

    def test_shared_poll_terminal_clears_run_and_reads_recovery_once(self):
        self.run_scenario(r'''
reset();
delete context.pollScriptLogs;
load('const _pollGen =', '// Manual transport (#593)');
load('async function pollLogs(', '// --- Script tab progress');
load('function _resetPauseBtn(', '// Same "unknown error"');
load('function _makePauseResumeHandler(', '// #600:');
load('window.cancelScript =', 'function scriptRecoveryContextMatches(');
const timers = [], details = [];
let st = {running: true, logs: []}, response = {recoverable: true, detail: detail()};
const pause = element('btn-pause-script');
pause.classList = {contains: () => false, add() {}, remove() {}};
Object.assign(context, {setTimeout: f => timers.push(f), createTaskLogRenderer: () => () => {},
    renderManualRequest() {}, renderScriptTabProgress() {}, notifyJobDone() {},
    currentTabName: 'script', getTaskLabel: name => name});
context.API.get = async path => {
    if (path === '/api/status') { return {script: st}; }
    if (path.startsWith('/api/status/')) { return st; }
    if (path.includes('include_detail')) { details.push(context.currentBookFilename); return response; }
    return {recoverable: true};
};
context.API.post = async (url, data) => { posts.push({url, data}); return {resolution: 'accepted'}; };
const tick = async () => { const callback = timers.shift(); await callback(); await flush(); };
getReply = response;
await context.refreshScriptRecovery();
await context.retryScriptGeneration();
await flush();
assert(context._scriptRecoveryRunActive);
assert.strictEqual(timers.length, 1);
assert.strictEqual(element('script-recovery-panel').innerHTML, '');
st = {running: false, status: 'failed', logs: []};
await tick();
assert(!context._scriptRecoveryRunActive);
assert.strictEqual(details.length, 2);
assert.strictEqual(element('btn-cancel-script').style.display, 'none');
assert.strictEqual(element('btn-retry-script').disabled, false);
assert.strictEqual(retryPosts().length, 1);
''')

    def test_idle_same_name_reselection_reads_once_for_accepted_selection(self):
        self.run_scenario(r'''
reset();
const details = [];
context.API.get = async path => {
    if (path.includes('include_detail')) { details.push(context.currentBookFilename); return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return {recoverable: true};
};
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/uploads/select') { return {stored_filename: data.filename, book_identity_token: 'a'.repeat(64)}; }
    return {resolution: 'accepted'};
};
await context.refreshScriptRecovery();
const before = details.length;
element('existing-upload-select').value = 'book-A';
await context.selectExistingScriptUpload();
for (let i = 0; i < 12; i++) { await new Promise(done => setImmediate(done)); }
assert.strictEqual(details.length, before + 1);
assert.strictEqual(details.at(-1), 'book-A');
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
assert.strictEqual(element('btn-retry-script').disabled, false);
''')

    def test_run_tokens_isolate_stale_observers_and_terminal_reads(self):
        self.run_scenario(r'''
reset();
delete context.pollScriptLogs;
load('const _pollGen =', '// Manual transport (#593)');
load('async function pollLogs(', '// --- Script tab progress');
load('function _resetPauseBtn(', '// Same "unknown error"');
load('function _makePauseResumeHandler(', '// #600:');
load('window.cancelScript =', 'function scriptRecoveryContextMatches(');
const timers = [], details = [];
let st = {running: true, logs: []};
const pause = element('btn-pause-script');
pause.classList = {contains: () => false, add() {}, remove() {}};
Object.assign(context, {setTimeout: f => timers.push(f), createTaskLogRenderer: () => () => {},
    renderManualRequest() {}, renderScriptTabProgress() {}, notifyJobDone() {},
    currentTabName: 'script', getTaskLabel: name => name});
context.API.get = async path => {
    if (path === '/api/status') { return {script: st}; }
    if (path.startsWith('/api/status/')) { return st; }
    if (path.includes('include_detail')) { details.push(context.currentBookFilename); return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return {recoverable: true};
};
context.API.post = async (url, data) => { posts.push({url, data}); return {resolution: 'accepted'}; };
const tick = async () => { const callback = timers.shift(); await callback(); await flush(); };
await context.refreshScriptRecovery();
await context.retryScriptGeneration();
await flush();
const firstToken = context._scriptRecoveryRunToken;
assert(firstToken > 0);
assert.strictEqual(element('btn-gen-script').disabled, true);
assert.strictEqual(element('btn-pause-script').style.display, 'inline-block');
const oldObserverToken = firstToken;
const replacementToken = run('reserveScriptRecoveryRun("generate")');
run('confirmScriptRecoveryRun(' + replacementToken + ', "generate")');
assert(replacementToken > oldObserverToken);
genBtnRestore: {
    const gen = element('btn-gen-script');
    gen.disabled = false;
    element('btn-pause-script').style.display = 'none';
    element('btn-cancel-script').style.display = 'none';
}
run('refreshScriptRetryToolbar()');
assert.strictEqual(element('btn-gen-script').disabled, false);
context.onScriptTaskTerminal({running: false, status: 'failed', logs: []}, oldObserverToken);
await flush();
assert.strictEqual(element('btn-gen-script').disabled, false);
assert.strictEqual(element('btn-pause-script').style.display, 'none');
assert(context._scriptRecoveryRunActive);
context.onScriptTaskTerminal({running: false, status: 'failed', logs: []}, replacementToken);
await flush();
assert(!context._scriptRecoveryRunActive);
assert.strictEqual(element('btn-gen-script').disabled, false);
assert.strictEqual(element('btn-pause-script').style.display, 'none');
assert.strictEqual(details.length, 2);
''')

    def test_terminal_retires_pending_detail_and_reattach_arms_run(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
await context.refreshScriptRecovery();
await context.retryScriptGeneration();
await flush();
const token = context._scriptRecoveryRunToken;
const pending = [];
context.API.get = async path => {
    if (path.includes('include_detail')) { const call = deferred(); pending.push(call); return call.promise; }
    return {recoverable: true};
};
const stale = context.refreshScriptRecovery();
await flush();
const generationBefore = context._scriptRecoveryGeneration;
assert.strictEqual(pending.length, 1);
context.onScriptTaskTerminal({running: false, status: 'failed', logs: []}, token);
await flush();
assert(context._scriptRecoveryGeneration > generationBefore);
assert.strictEqual(pending.length, 2, 'terminal schedules exactly one fresh terminal read');
pending[0].resolve({recoverable: false});
pending[1].resolve({recoverable: true, detail: detail(), failed_pass: 'segment'});
await stale;
await flush();
assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
assert.strictEqual(element('btn-retry-script').disabled, false);
''')

    def test_preflight_after_generate_and_panel_toolbar_parity(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
await context.refreshScriptRecovery();
const meta = deferred();
context.API.get = async path => {
    if (path.includes('include_detail')) { return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return meta.promise;
};
const retry = context.window.retryScriptGeneration();
await flush();
const panelRetry = () => element('script-recovery-retry');
assert.strictEqual(panelRetry().disabled, true);
assert.strictEqual(element('btn-retry-script').disabled, true);
const generateToken = run('reserveScriptRecoveryRun("generate")');
run('confirmScriptRecoveryRun(' + generateToken + ', "generate")');
meta.resolve({recoverable: true});
await retry;
assert.strictEqual(retryPosts().length, 0);
assert.strictEqual(element('btn-retry-script').disabled, true);
assert.strictEqual(panelRetry().disabled, true);
context.onScriptTaskTerminal({running: false, status: 'failed', logs: []}, generateToken);
await flush();
assert.strictEqual(element('btn-retry-script').disabled, false);
assert.strictEqual(panelRetry().disabled, false);
''')

    def test_start_over_reserves_token_before_post_ack_and_ignores_stale_terminal(self):
        self.run_scenario(r'''
reset();
const gen = element('btn-gen-script');
gen.disabled = false;
element('file-upload').files = [];
let generate;
gen.addEventListener = (name, cb) => { generate = cb; };
Object.assign(context, {getLoadedScriptSourceFilename: () => 'book-A',
    ensureScriptStartConfirmed: async () => true, _scriptStartOver: true,
    _isStripFrontMatterChecked: () => false, _isLiteralSourcePreservingChecked: () => false});
load("document.getElementById('btn-gen-script').addEventListener('click',", '// Pause is SIGSTOP');
const ackBarrier = deferred();
const post = context.API.post;
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script') { await ackBarrier.promise; }
    return {resolution: 'accepted'};
};
const starting = generate();
await flush();
const reservedToken = context._scriptRecoveryRunToken;
assert(reservedToken > 0);
assert(context._scriptRecoveryRunActive);
assert.strictEqual(gen.disabled, true);
assert.strictEqual(element('btn-pause-script').style.display, 'inline-block');
context.onScriptTaskTerminal({running: false, status: 'failed', logs: []}, reservedToken - 1);
await flush();
assert(context._scriptRecoveryRunActive);
assert.strictEqual(gen.disabled, true);
assert.strictEqual(element('btn-pause-script').style.display, 'inline-block');
ackBarrier.resolve({});
await starting;
assert.strictEqual(context._scriptRecoveryRunToken, reservedToken);
assert.strictEqual(gen.disabled, true);
assert.strictEqual(element('btn-pause-script').style.display, 'inline-block');
context.onScriptTaskTerminal({running: false, status: 'failed', logs: []}, reservedToken);
await flush();
assert(!context._scriptRecoveryRunActive);
assert.strictEqual(gen.disabled, false);
''')

    def test_competing_generate_suppresses_retry_and_terminal_is_once(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
await context.refreshScriptRecovery();
const meta = deferred();
context.API.get = async path => {
    if (path.includes('include_detail')) { return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return meta.promise;
};
const retry = context.window.retryScriptGeneration();
await flush();
const gen = element('btn-gen-script');
gen.disabled = false;
element('file-upload').files = [];
let generate;
gen.addEventListener = (name, cb) => { generate = cb; };
Object.assign(context, {getLoadedScriptSourceFilename: () => 'book-A',
    ensureScriptStartConfirmed: async () => true, _scriptStartOver: false,
    _isStripFrontMatterChecked: () => false, _isLiteralSourcePreservingChecked: () => false});
load("document.getElementById('btn-gen-script').addEventListener('click',", '// Pause is SIGSTOP');
const start = generate();
await flush();
const generateClaim = context._scriptGenerationClaim;
const toastsBefore = toasts.length;
meta.reject(Error('stale retry metadata'));
await retry;
await start;
assert.strictEqual(retryPosts().length, 0);
assert.strictEqual(toasts.length, toastsBefore);
assert.strictEqual(context._scriptGenerationClaim, generateClaim);
const token = context._scriptRecoveryRunToken;
context.onScriptTaskTerminal({running: false, status: 'failed', logs: []}, token);
await flush();
const afterFirst = context._scriptRecoveryGeneration;
context.onScriptTaskTerminal({running: false, status: 'failed', logs: []}, token);
await flush();
assert.strictEqual(context._scriptRecoveryGeneration, afterFirst);
''')

    def test_hydration_never_adopts_a_pending_reservation(self):
        self.run_scenario(r'''
reset();
const reserved = run('reserveScriptRecoveryRun("generate")');
assert(reserved > 0);
const observed = run('observeScriptRecoveryRun()');
assert(observed > reserved);
assert.strictEqual(run('observeScriptRecoveryRun()'), observed);
assert.strictEqual(context.getScriptAdmissionByToken(observed).state, 'provisional');
assert.strictEqual(context._scriptRecoveryRunToken, reserved);
context.onScriptTaskTerminal({running: false, status: 'failed', logs: []}, observed);
await flush();
assert(context._scriptRecoveryRunActive);
assert.strictEqual(context._scriptRecoveryRunToken, reserved);
assert.strictEqual(run('confirmScriptRecoveryRun(' + reserved + ', "generate")'), true);
''')

    def test_terminal_cleanup_notification_and_read_are_exactly_once(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
await context.refreshScriptRecovery();
const token = run('reserveScriptRecoveryRun("retry")');
run('confirmScriptRecoveryRun(' + token + ', "retry")');
let cleanups = 0;
context.registerScriptTerminalCleanup(token, () => { cleanups++; });
const state = {running: false, status: 'failed', logs: []};
context.onScriptTaskTerminal(state, token, 'observer');
await flush();
context.onScriptTaskTerminal(state, token, 'reattach-script');
await flush();
assert.strictEqual(cleanups, 1);
assert(!context._scriptRecoveryRunActive);
assert.strictEqual(context._scriptRecoveryGeneration, 3);
''')

    def test_rejected_generate_rolls_back_claim_so_pending_retry_can_post(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
await context.refreshScriptRecovery();
const meta = deferred();
context.API.get = async path => {
    if (path.includes('include_detail')) { return {recoverable: true, detail: detail(), failed_pass: 'segment'}; }
    return meta.promise;
};
const retry = context.window.retryScriptGeneration();
await flush();
const gen = element('btn-gen-script');
gen.disabled = false;
element('file-upload').files = [];
let generate;
gen.addEventListener = (name, cb) => { generate = cb; };
Object.assign(context, {getLoadedScriptSourceFilename: () => 'book-A',
    ensureScriptStartConfirmed: async () => true, _scriptStartOver: false,
    _isStripFrontMatterChecked: () => false, _isLiteralSourcePreservingChecked: () => false});
load("document.getElementById('btn-gen-script').addEventListener('click',", '// Pause is SIGSTOP');
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script') { throw Error('409 refused'); }
    return {resolution: 'accepted'};
};
await generate();
await flush();
assert(!context._scriptRecoveryRunActive);
meta.resolve({recoverable: true});
await retry;
assert.strictEqual(retryPosts().length, 1);
assert.strictEqual(polls.length, 1);
assert.strictEqual(element('btn-pause-script').style.display, 'inline-block');
''')

    def test_accepted_retry_survives_later_generate_rejection(self):
        self.run_scenario(r'''
reset();
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
await context.refreshScriptRecovery();
const retryAck = deferred();
context.API.get = async path => path.includes('include_detail')
    ? {recoverable: true, detail: detail(), failed_pass: 'segment'} : {recoverable: true};
context.API.post = async (url, data) => {
    posts.push({url, data});
    if (url === '/api/generate_script/retry') { await retryAck.promise; }
    if (url === '/api/generate_script') { throw Error('409 refused'); }
    return {resolution: 'accepted'};
};
const retry = context.window.retryScriptGeneration();
await flush();
retryAck.resolve({});
await retry;
assert.strictEqual(retryPosts().length, 1);
assert.strictEqual(polls.length, 1);
const acceptedToken = context._scriptRecoveryAcceptedRun;
const gen = element('btn-gen-script');
gen.disabled = false;
element('file-upload').files = [];
let generate;
gen.addEventListener = (name, cb) => { generate = cb; };
Object.assign(context, {getLoadedScriptSourceFilename: () => 'book-A',
    ensureScriptStartConfirmed: async () => true, _scriptStartOver: false,
    _isStripFrontMatterChecked: () => false, _isLiteralSourcePreservingChecked: () => false});
load("document.getElementById('btn-gen-script').addEventListener('click',", '// Pause is SIGSTOP');
await generate();
await flush();
assert.strictEqual(context._scriptRecoveryAcceptedRun, acceptedToken);
assert(context._scriptRecoveryRunActive);
assert.strictEqual(polls.length, 1);
assert.strictEqual(element('btn-pause-script').style.display, 'inline-block');
''')

    def test_duplicate_generate_submission_is_refused_while_first_pending(self):
        self.run_scenario(r'''
reset();
const ackBarrier = deferred();
context.API.post = async (url, data) => { posts.push({url, data}); await ackBarrier.promise; return {}; };
const gen = element('btn-gen-script');
gen.disabled = false;
element('file-upload').files = [];
let generate;
gen.addEventListener = (name, cb) => { generate = cb; };
Object.assign(context, {getLoadedScriptSourceFilename: () => 'book-A',
    ensureScriptStartConfirmed: async () => true, _scriptStartOver: false,
    _isStripFrontMatterChecked: () => false, _isLiteralSourcePreservingChecked: () => false});
load("document.getElementById('btn-gen-script').addEventListener('click',", '// Pause is SIGSTOP');
const first = generate();
await flush();
const second = generate();
await second;
assert.strictEqual(posts.filter(entry => entry.url === '/api/generate_script').length, 1);
ackBarrier.resolve({});
await first;
''')

    def test_observer_terminal_orders_have_exactly_once_effects(self):
        self.run_scenario(r'''
const orders = [];
for (const first of ['observer', 'live', 'passive', 'terminal']) {
 for (const second of ['observer', 'live', 'passive', 'terminal']) {
  for (const third of ['observer', 'live']) { orders.push([first, second, third]); }
 }
}
assert.strictEqual(orders.length, 32);
for (const order of orders) {
 reset();
 getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
 await context.refreshScriptRecovery();
 const token = run('reserveScriptRecoveryRun("retry")');
 run('confirmScriptRecoveryRun(' + token + ', "retry")');
 let logs = 0, notifications = 0, releases = 0;
 context.registerScriptTerminalCleanup(token, state => {
     releases++; logs += (state && state.logs || []).length; notifications++;
 });
 const detailReadsBefore = reads => reads;
 const generationBefore = context._scriptRecoveryGeneration;
 for (const source of order) {
     context.onScriptTaskTerminal({running: false, status: 'failed', logs: ['terminal log']}, token, source);
     await flush();
 }
 assert.strictEqual(releases, 1, order.join(','));
 assert.strictEqual(notifications, 1, order.join(','));
 assert.strictEqual(logs, 1, order.join(','));
 assert(context._scriptRecoveryGeneration >= generationBefore + 1);
 assert(!context._scriptRecoveryRunActive);
}
''')


    def test_active_run_fences_late_read_and_click_until_polling_ends(self):
        self.run_scenario(r'''
for (const lateDetail of [null, detail()]) {
 reset();
 getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
 await context.refreshScriptRecovery();
 element('script-recovery-inject').value = '[{"type":"NARRATOR","text":"x"}]';
 const applyAck = deferred(), lateRead = deferred(), retryAck = deferred();
 context.API.get = async path => path.includes('include_detail=true') ? lateRead.promise : {recoverable: true};
 context.API.post = async (url, data) => {
     posts.push({url, data});
     if (url === '/api/generate_script/inject') { return applyAck.promise; }
     if (url === '/api/generate_script/retry') { return retryAck.promise; }
     return {resolution: 'accepted'};
 };
 const apply = context.onInjectRecoverySegmentation();
 await flush();
 const retry = run('retryScriptGeneration()');
 await flush();
 assert.strictEqual(retryPosts().length, 1);
 applyAck.resolve({});
 await flush();
 retryAck.resolve({});
 await retry;
 assert.strictEqual(polls.length, 1);
 assert.strictEqual(element('btn-cancel-script').style.display, 'inline-block');
 assert.strictEqual(element('btn-retry-script').style.display, 'none');
 const during = context.window.retryScriptGeneration();
 await during;
 assert.strictEqual(retryPosts().length, 1);
 lateRead.resolve({recoverable: true, detail: lateDetail});
 await apply;
 await flush();
 assert.strictEqual(element('btn-retry-script').style.display, 'none');
 assert.strictEqual(context._scriptRecoveryDetail, null);
 await context.onCopyRecoveryPrompt();
 assert.strictEqual(copied.length, 0);
 polls[0][1]();
 await flush();
 assert.strictEqual(element('btn-cancel-script').style.display, 'none');
 assert.strictEqual(element('btn-retry-script').style.display, 'inline-block');
 const next = context.window.retryScriptGeneration();
 await next;
 assert.strictEqual(retryPosts().length, 2);
}
''')


    def test_apply_refresh_does_not_displace_a_committed_retry_action(self):
        self.run_scenario(r'''
for (const order of ['apply-first', 'retry-first']) {
 for (const secondClick of [false, true]) {
  reset();
  const d = detail();
  getReply = {recoverable: true, detail: d, failed_pass: 'segment'};
  await context.refreshScriptRecovery();
  assert.strictEqual(context._scriptRecoveryDetail, d);
  element('script-recovery-inject').value = '[{"type":"NARRATOR","text":"x"}]';
  const applyAck = deferred(), retryAck = deferred();
  context.API.get = async path => {
      if (path.includes('include_detail=true')) { return {recoverable: true, detail: null}; }
      return {recoverable: true};
  };
  context.API.post = async (url, data) => {
      posts.push({url, data});
      if (url === '/api/generate_script/inject') { return applyAck.promise; }
      if (url === '/api/generate_script/retry') { return retryAck.promise; }
      return {resolution: 'accepted'};
  };
  const apply = context.onInjectRecoverySegmentation();
  await flush();
  assert.strictEqual(posts.filter(entry => entry.url === '/api/generate_script/inject').length, 1);
  const retry = run('retryScriptGeneration()');
  await flush();
  assert.strictEqual(retryPosts().length, 1);
  if (secondClick) {
      const again = context.window.retryScriptGeneration();
      await again;
      assert.strictEqual(retryPosts().length, 1);
  }
  const settleApply = async () => { applyAck.resolve({resolution: 'accepted'}); await apply; await flush(); };
  const settleRetry = async () => { retryAck.resolve({}); await retry; };
  if (order === 'apply-first') { await settleApply(); await settleRetry(); }
  else { await settleRetry(); await settleApply(); }
  polls[0][1]();
  await flush();
  assert.strictEqual(retryPosts().length, 1, order + ' ' + secondClick);
  assert.strictEqual(polls.length, 1, order + ' ' + secondClick);
  assert.strictEqual(element('btn-cancel-script').style.display, 'none');
  assert.strictEqual(element('btn-retry-script').disabled, false);
  assert(!toasts.some(args => String(args[0]).includes('was not confirmed')));
 }
}
''')


    def test_production_handler_schedule_matrix(self):
        self.run_scenario(r'''
const html = fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]), '../index.html'), 'utf8');
const toolbar = () => run(html.match(/<button[^>]*id="btn-retry-script"[^>]*onclick="([^"]+)"/)[1]);
const retry = () => element('btn-retry-script');
const panel = () => element('script-recovery-panel');
const panelClick = name => { assert(panel().style.display !== 'none'); return run(name + '()'); };
const ok = () => ({recoverable: true, detail: detail()});
for (const result of ['failed', 'absent', 'null', 'valid'])
 for (const order of ['detail-first', 'ack-first', 'ack-before-apply'])
  for (const click of [false, true]) {
   reset(); getReply = ok(); await context.refreshScriptRecovery();
   element('script-recovery-inject').value = '[]';
   const a = deferred(), b = deferred(), c = deferred(); let reads = 0, metas = 0;
   context.API.get = async path => { if (path.includes('include_detail')) { reads++; return c.promise; } metas++; return {recoverable: true}; };
   context.API.post = async (url, data) => { posts.push({url, data}); return url.endsWith('/inject') ? a.promise : b.promise; };
   const apply = panelClick('onInjectRecoverySegmentation');
   const action = toolbar();
   await flush();
   assert.strictEqual(posts.length, 2);
   const ack = async () => { b.resolve({}); await action; };
   if (order === 'ack-before-apply') { await ack(); }
   a.resolve({}); await flush();
   if (order === 'ack-first') { await ack(); }
   if (result === 'failed' && reads) { c.reject(Error('late')); }
   else { c.resolve(result === 'valid' ? ok() : result === 'absent' ? {recoverable: false} : {recoverable: true, detail: null}); }
   await apply;
   if (order === 'detail-first') { await ack(); }
   assert.strictEqual(reads, order === 'ack-before-apply' ? 0 : 1);
   assert.strictEqual(metas, 1);
   assert.strictEqual(retry().style.display, 'none');
   assert.strictEqual(retry().disabled, true);
   assert.strictEqual(polls.length, 1);
   assert.strictEqual(element('btn-cancel-script').style.display, 'inline-block');
   assert.strictEqual(element('btn-pause-script').style.display, 'inline-block');
   if (click) {
    await toolbar();
    if (panel().innerHTML.includes('onclick="retryScriptGeneration()"')) {
     await panelClick('retryScriptGeneration');
     await panelClick('onInjectRecoverySegmentation');
     await panelClick('onSkipRecoveryChunk');
     await panelClick('onCopyRecoveryPrompt');
    }
    assert.strictEqual(posts.length, 2);
    assert.strictEqual(copied.length, 0);
   }
  }
''')


if __name__ == '__main__':
    unittest.main()
