"""Script run ownership through the registered listeners and real pollers."""
import subprocess
import unittest

from tests.test_rescore6_replays import CORE, SETUP, WORKBENCH


ACK_ORDER = r"""
const retryAck = delay(), generateAck = delay();
const acknowledgements = {retry: retryAck, generate: generateAck};
context.API.post = (url, body) => {
    effects.posts.push([url, body]);
    return url.endsWith('/retry') ? retryAck.p : generateAck.p;
};
statuses = {script: live};
await context.refreshScriptRecovery();
const actions = {retry: context.retryScriptGeneration()};
await settle();
actions.generate = gen.listeners.click();
await settle();
assert.deepStrictEqual(effects.posts.map(([url]) => url),
    ['/api/generate_script/retry', '/api/generate_script']);

const first = FIRST, second = first === 'retry' ? 'generate' : 'retry';
acknowledgements[first].yes({});
await actions[first];
await settle();
const firstToken = context._scriptRecoveryAcceptedRun;

const oldStatus = delay();
statuses.script = {...live, ownership_unconfirmed: true};
let statusReads = 0;
context.API.get = url => url === '/api/status/script' && ++statusReads === 2
    ? oldStatus.p : initialGet(url);
await context.reattachRunningPollers();
await settle();
assert.strictEqual(statusReads, 2, 'dispatcher started an observer with a held status');
context.API.get = initialGet;

statuses.script = {...done, logs: ['FIRST DONE']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(jobs().length, 1);

statuses.script = {...live, logs: ['SECOND LIVE']};
acknowledgements[second].yes({});
await actions[second];
await settle();
const secondToken = context._scriptRecoveryAcceptedRun;
const secondLog = el('script-logs').innerText;
const secondSnapshot = el('btn-snapshot-script').style.display;
assertBusy();

statuses.script = {...done, logs: ['SECOND DONE']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(jobs().length, 2);

context.API.post = initialPost;
statuses.script = {...live, logs: ['THIRD LIVE']};
await gen.listeners.click();
await settle();
const thirdToken = context._scriptRecoveryAcceptedRun;
assertBusy();
oldStatus.yes({...done, logs: ['STALE RETRY TERMINAL']});
await settle();
assertBusy();
assert.strictEqual(el('script-logs').innerText, 'THIRD LIVE');
assert.strictEqual(context.getScriptAdmissionByToken(thirdToken).state, 'accepted');
assert.strictEqual(new Set([firstToken, secondToken, thirdToken]).size, 3);
assert.strictEqual(jobs().length, 2);

statuses.script = {...done, logs: ['THIRD DONE']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(jobs().length, 3);
assert.strictEqual(el('script-logs').innerText, 'THIRD DONE');
assert.strictEqual(context.getScriptAdmissionByToken(thirdToken).state, 'delivered');
assert.strictEqual(gen.disabled, false);
assert.strictEqual(el('btn-cancel-script').style.display, 'none');
assert.strictEqual(el('btn-pause-script').style.display, 'none');
assert.strictEqual(secondLog, 'SECOND LIVE');
assert.strictEqual(secondSnapshot, 'inline-block');
"""


OBSERVED_AFTER_REFUSAL = r"""
const refused = delay();
context.API.post = (url, body) => {
    effects.posts.push([url, body]);
    return refused.p;
};
statuses = {script: {...live, ownership_unconfirmed: true}};
const generate = gen.listeners.click();
await settle();
await context.reattachRunningPollers();
await settle();
const provisional = context._scriptRecoveryRunToken;
assert(run("taskStartClaims.has('script')"));
const observedTerminal = {...done, logs: ['OBSERVED DONE']};
if (TERMINAL_FIRST) {
    statuses.script = observedTerminal;
    await tick();
    await settle();
    assert.strictEqual(jobs().length, 0, 'POST outcome is still pending');
}
refused.no(Object.assign(Error('409 refused'), {status: 409}));
await generate;
await settle();
assert.strictEqual(jobs().length, 0);
assert.match(el('upload-status').innerHTML, /start was not confirmed/);
assertBusy();
assert.notStrictEqual(context._scriptRecoveryAcceptedRun, provisional);
assert(run("taskStartClaims.has('script')"));

statuses.script = observedTerminal;
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(el('script-logs').textContent, 'OBSERVED DONE');
assert.strictEqual(jobs().length, 1);
assert.strictEqual(context.getScriptAdmissionByToken(provisional).state, 'rejected');
assert.deepStrictEqual(effects.releases, ['script']);
assert(!run("taskStartClaims.has('script')"));
assert.strictEqual(gen.disabled, false);
"""


PENDING_GENERATE_OBSERVER = r"""
const refused = delay(), oldStatus = delay();
context.API.post = (url, body) => {
    effects.posts.push([url, body]);
    return refused.p;
};
statuses = {script: {...live, ownership_unconfirmed: OBSERVER_UNCONFIRMED}};
const generate = gen.listeners.click();
await settle();
let statusReads = 0;
context.API.get = url => url === '/api/status/script' && ++statusReads === 2
    ? oldStatus.p : initialGet(url);
await context.reattachRunningPollers();
await settle();
assert.strictEqual(statusReads, 2);
context.API.get = initialGet;
"""


PRE_ACK_STATUS = PENDING_GENERATE_OBSERVER + r"""
const preAckTerminal = {...done, logs: ['PRE-ACK TERMINAL']};
const observed = Object.values(context._scriptAdmissions).find(record => record.source === 'observe');
assert.strictEqual(observed.claimId, live.task_claim_id);
assert.strictEqual(observed.state, 'provisional');
assert.strictEqual(context._scriptRecoveryAcceptedRun || 0, 0);
statuses.script = {...live, task_claim_id: 'b'.repeat(32), logs: ['GENERATE LIVE']};
refused.yes({task_claim_id: statuses.script.task_claim_id});
await generate;
await settle();
const acceptedToken = context._scriptRecoveryAcceptedRun;
assertBusy();
assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
oldStatus.yes(preAckTerminal);
await settle();
assertBusy();
assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
assert.strictEqual(el('script-logs').innerText, 'GENERATE LIVE');
assert.notStrictEqual(el('script-logs').textContent, 'PRE-ACK TERMINAL');
assert.strictEqual(context.getScriptAdmissionByToken(acceptedToken).state, 'accepted');
assert.deepStrictEqual(jobs(), []);
assert.deepStrictEqual(effects.releases, ['script']);
assert(!run("taskStartClaims.has('script')"), 'acceptance detached the pre-ACK observer');

statuses.script = {...done, logs: ['GENERATE DONE']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(el('script-logs').innerText, 'GENERATE DONE');
assert.deepStrictEqual(jobs(), [['job', 'script', 'finished']]);
assert.strictEqual(context.getScriptAdmissionByToken(acceptedToken).state, 'delivered');
assert.strictEqual(gen.disabled, false);
assert.strictEqual(el('btn-snapshot-script').style.display, 'none');
"""


REPLACEMENT_AFTER_REFUSAL = PENDING_GENERATE_OBSERVER + r"""
if (CHANGE_SELECTION) { selectBook('book-B'); await settle(); }
refused.no(Object.assign(Error('409 refused'), {status: 409}));
await generate;
await settle();
assertBusy();
assert(run("taskStartClaims.has('script')"));
await gen.listeners.click();
assert.deepStrictEqual(effects.posts.map(([url]) => url), ['/api/generate_script']);
assert.deepStrictEqual(jobs(), []);
const observedToken = context._scriptRecoveryAcceptedRun;

statuses.script = {...live, logs: ['EXTERNAL LIVE']};
oldStatus.yes(statuses.script);
await settle();
assertBusy();
assert.strictEqual(context._scriptRecoveryAcceptedRun, observedToken);
assert.strictEqual(el('script-logs').textContent, 'EXTERNAL LIVE');
assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
assert(run("taskStartClaims.has('script')"));
assert.deepStrictEqual(effects.releases, []);

statuses.script = {...done, logs: ['EXTERNAL FINISHED']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(el('script-logs').textContent, 'EXTERNAL FINISHED');
assert.deepStrictEqual(jobs(), [['job', 'script', 'finished']]);
assert.deepStrictEqual(effects.releases, ['script']);
assert(!run("taskStartClaims.has('script')"));
assert.strictEqual(gen.disabled, false);
assert.strictEqual(el('btn-snapshot-script').style.display, 'none');
assert.strictEqual(context.getScriptAdmissionByToken(observedToken).state, 'delivered');

statuses.script = {...live, logs: ['REPLACEMENT LIVE']};
context.API.post = initialPost;
await gen.listeners.click();
await settle();
assertBusy();
assert.notStrictEqual(context._scriptRecoveryAcceptedRun, observedToken);
assert.strictEqual(el('script-logs').innerText, 'REPLACEMENT LIVE');

statuses.script = {...done, logs: ['REPLACEMENT DONE']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(el('script-logs').innerText, 'REPLACEMENT DONE');
assert.deepStrictEqual(effects.releases, ['script']);
assert.strictEqual(jobs().length, 2);
assert.strictEqual(gen.disabled, false);
"""


DIRECT_START_OVER = r"""
coreSlice('async function cancelTask(', 'const scriptCancellationRequests =');
const terminalLogs = [];
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, state) => {
    notify(task, id, outcome, state);
    terminalLogs.push(state.logs);
};
statuses = {script: {...live, logs: ['FIRST LIVE']}};
await gen.listeners.click();
await settle();
assertBusy();
const first = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    statuses.script = url.endsWith('/cancel')
        ? {...done, logs: ['CANCELLED FIRST']}
        : {...live, logs: ['REPLACEMENT LIVE']};
    return {};
};
await fresh.listeners.click();
await forwarded;
await settle();
const second = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
assert.notStrictEqual(first.token, second.token);
assert.deepStrictEqual(effects.posts.map(([url, body]) => [url, body.start_over]), [
    ['/api/generate_script', false],
    ['/api/generate_script/cancel', undefined],
    ['/api/generate_script', true],
]);
assert.strictEqual(first.state, 'delivered');
assert.deepStrictEqual(terminalLogs, [['CANCELLED FIRST']]);
assert.strictEqual(second.state, 'accepted');
assertBusy();
assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
assert.strictEqual(el('script-logs').innerText, 'REPLACEMENT LIVE');
assert(!run("taskStartClaims.has('script')"));

statuses.script = {...done, logs: ['REPLACEMENT TERMINAL']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(second.state, 'delivered');
assert.strictEqual(el('script-logs').innerText, 'REPLACEMENT TERMINAL');
assert.deepStrictEqual(terminalLogs, [['CANCELLED FIRST'], ['REPLACEMENT TERMINAL']]);
assert.strictEqual(jobs().length, 2);
assert.strictEqual(gen.disabled, false);
assert.strictEqual(el('btn-snapshot-script').style.display, 'none');
"""


REFUSED_DIRECT_RESTART = r"""
coreSlice('async function cancelTask(', 'const scriptCancellationRequests =');
const terminalLogs = [];
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, state) => {
    notify(task, id, outcome, state);
    terminalLogs.push(state.logs);
};
statuses = {script: {...live, logs: ['ORIGINAL LIVE']}};
await gen.listeners.click();
await settle();
const original = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
assert.strictEqual(el('script-logs').innerText, 'ORIGINAL LIVE');
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (url.endsWith('/cancel')) {
        statuses.script = {...done, logs: ['CONFIRMED STOP']};
        return {};
    }
    throw Object.assign(Error('409 refused replacement'), {status: 409});
};
await fresh.listeners.click();
await forwarded;
await settle();
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.deepStrictEqual(effects.posts.map(([url, body]) => [url, body.start_over]), [
    ['/api/generate_script', false],
    ['/api/generate_script/cancel', undefined],
    ['/api/generate_script', true],
]);
assert.strictEqual(original.state, 'delivered');
assert.deepStrictEqual(terminalLogs, [['CONFIRMED STOP']]);
assert.strictEqual(el('script-logs').innerText, 'CONFIRMED STOP');
assert.strictEqual(gen.disabled, false);
assert.strictEqual(el('btn-snapshot-script').style.display, 'none');
assert(!run("taskStartClaims.has('script')"));
"""


OVERLAPPING_DIRECT_RESTARTS = r"""
coreSlice('async function cancelTask(', 'const scriptCancellationRequests =');
const terminalLogs = [];
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, state) => {
    notify(task, id, outcome, state);
    terminalLogs.push(state.logs);
};
statuses = {script: {...live, logs: ['ORIGINAL']}};
await gen.listeners.click();
await settle();

const pendingCancel = delay(), stopCheck = delay();
const firstConsent = delay(), secondConsent = delay();
let confirmations = 0, stopRead = false;
context.showConfirm = () => (++confirmations === 1 ? firstConsent : secondConsent).p;
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (url.endsWith('/cancel')) {
        await pendingCancel.p;
        stopRead = true;
        return {};
    }
    statuses.script = {...live, task_claim_id: 'b'.repeat(32), logs: ['REPLACEMENT LIVE']};
    return {};
};
context.API.get = url => url === '/api/status/script' && stopRead ? stopCheck.p : initialGet(url);
const first = fresh.listeners.click();
await settle();
const second = fresh.listeners.click();
await settle();
assert.strictEqual(confirmations, 2, 'both registered listeners opened consent');
firstConsent.yes(true);
await settle();
assert.strictEqual(effects.posts.filter(([url]) => url.endsWith('/cancel')).length, 1);
if (!SECOND_AFTER_REPLACEMENT) {
    secondConsent.yes(true);
    await settle();
    assert.strictEqual(effects.posts.filter(([url]) => url.endsWith('/cancel')).length, 1,
        'second confirmed listener cannot send a cancel while the first is in flight');
    await second;
}
assertBusy();
pendingCancel.yes({});
await settle();

const stopped = {...done, logs: ['FIRST END']};
stopRead = false;
stopCheck.yes(stopped);
await first;
await forwarded;
await settle();
const replacement = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
assert.strictEqual(replacement.state, 'accepted');
assertBusy();
assert.deepStrictEqual(terminalLogs, [['FIRST END']]);
assert.strictEqual(el('script-logs').innerText, 'REPLACEMENT LIVE');

if (SECOND_AFTER_REPLACEMENT) {
    let disabled = gen.disabled;
    const writes = [];
    Object.defineProperty(gen, 'disabled', {
        get: () => disabled,
        set(value) { disabled = value; writes.push(value); },
    });
    secondConsent.yes(true);
    await second;
    await settle();
    assert(!writes.includes(false), 'late consent must never enable Generate');
}

assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: 'a'.repeat(32)}],
    ['/api/generate_script', {first_person_narrator: null, start_over: true, expected_restart_claim_id: 'a'.repeat(32)}],
]);
assert.strictEqual(context._scriptRecoveryAcceptedRun, replacement.token);
assert.strictEqual(replacement.state, 'accepted');
assertBusy();
assert.strictEqual(el('script-logs').innerText, 'REPLACEMENT LIVE');
assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
assert.deepStrictEqual(terminalLogs, [['FIRST END']]);
assert(!run("taskStartClaims.has('script')"));

statuses.script = {...done, logs: ['REPLACEMENT END']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(replacement.state, 'delivered');
assert.strictEqual(el('script-logs').innerText, 'REPLACEMENT END');
assert.deepStrictEqual(terminalLogs, [['FIRST END'], ['REPLACEMENT END']]);
assert.strictEqual(gen.disabled, false);
"""


RELOAD_RESTART_CONSENT = r"""
coreSlice('async function cancelTask(', 'const scriptCancellationRequests =');
statuses = {script: {...live, logs: ['ORIGINAL LIVE']}};
const confirmation = delay(), statusList = delay(), cancelAck = delay(), stopReply = delay();
const stoppedStatus = {...done, logs: ['ORIGINAL STOP']};
let holdStopRead = false, stopReadStarted = false;
context.API.get = url => {
    if (url === '/api/status') { return statusList.p; }
    if (url === '/api/status/script' && holdStopRead) {
        holdStopRead = false;
        stopReadStarted = true;
        return stopReply.p;
    }
    return initialGet(url);
};
const hydration = context.reattachRunningPollers();
await settle();
let prompts = 0;
context.showConfirm = () => { prompts++; return confirmation.p; };
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (url.endsWith('/cancel')) {
        if (REATTACH === 'cancel') { await cancelAck.p; }
        if (REATTACH === 'stop') { holdStopRead = true; }
        else if (CHANGE !== 'run') { statuses.script = stoppedStatus; }
        return {};
    }
    statuses.script = {...live, task_claim_id: 'b'.repeat(32), logs: ['REPLACEMENT LIVE']};
    return {task_claim_id: statuses.script.task_claim_id};
};
const restart = fresh.listeners.click();
await settle();
assert.strictEqual(prompts, 1);
assert.strictEqual(context._scriptRecoveryRunToken || 0, 0);
if (REATTACH !== 'consent') {
    confirmation.yes(true);
    await settle();
    assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
        ['/api/generate_script/cancel', {expected_claim_id: live.task_claim_id}],
    ]);
    assert.strictEqual(stopReadStarted, REATTACH === 'stop');
    assert.strictEqual(context._scriptRecoveryRunToken || 0, 0);
}
if (CHANGE === 'run') {
    statuses.script = {...live, task_claim_id: 'b'.repeat(32), logs: ['OTHER LIVE']};
}
statusList.yes(statuses);
await hydration;
await settle();
const observed = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
assert(observed && observed.state === 'accepted');
assertBusy();
if (CHANGE === 'book') { selectBook('book-B'); await settle(); }
if (CHANGE === 'selection') { selectBook('book-B'); selectBook('book-A'); await settle(); }
if (REATTACH === 'consent') { confirmation.yes(true); }
if (REATTACH === 'cancel') { cancelAck.yes({}); }
if (REATTACH === 'stop') {
    if (CHANGE !== 'run') { statuses.script = stoppedStatus; }
    stopReply.yes(stoppedStatus);
}
await restart;
await forwarded;
await settle();
if (CHANGE) {
    assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), REATTACH === 'consent'
        ? [] : [['/api/generate_script/cancel', {expected_claim_id: live.task_claim_id}]]);
    assert.strictEqual(context._scriptRecoveryAcceptedRun, observed.token);
    assert.strictEqual(observed.state, 'accepted');
    assert.deepStrictEqual(jobs(), []);
    assertBusy();
} else {
    assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
        ['/api/generate_script/cancel', {expected_claim_id: 'a'.repeat(32)}],
        ['/api/generate_script', {first_person_narrator: null, start_over: true, expected_restart_claim_id: 'a'.repeat(32)}],
    ]);
    assert.strictEqual(observed.state, 'delivered');
    assert.strictEqual(el('script-logs').innerText, 'REPLACEMENT LIVE');
    assertBusy();
    assert.strictEqual(jobs().length, 1);
    assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
    assert(!run("taskStartClaims.has('script')"));
    statuses.script = {...done, logs: ['REPLACEMENT END']};
    for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
    assert.strictEqual(el('script-logs').innerText, 'REPLACEMENT END');
    assert.strictEqual(jobs().length, 2);
    assert.strictEqual(el('btn-snapshot-script').style.display, 'none');
    assert.strictEqual(gen.disabled, false);
}
"""


QUEUED_START_OVER = r"""
statuses = {script: live};
const previousStatus = delay();
const allowGenerate = delay();
context.confirmIfRemote = () => allowGenerate.p;
const rejectedGenerate = gen.listeners.click();
await acceptRetry();
const restoredToken = context._scriptRecoveryAcceptedRun;
statuses.script = {...live, ownership_unconfirmed: true};
let statusReads = 0;
context.API.get = url => url === '/api/status/script' && ++statusReads === 2
    ? previousStatus.p : initialGet(url);
await context.reattachRunningPollers();
await settle();
assert.strictEqual(statusReads, 2);
context.API.get = initialGet;
context.API.post = async () => { throw Object.assign(Error('409 refused'), {status: 409}); };
allowGenerate.yes(true);
await rejectedGenerate;
await settle();
assert.strictEqual(context._scriptRecoveryAcceptedRun, restoredToken);
assertBusy();
statuses.script = {...done, logs: ['RESTORED RETRY DONE']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(context.getScriptAdmissionByToken(restoredToken).state, 'delivered');
assert.strictEqual(jobs().length, 1);

const firstAck = delay();
effects.posts.length = 0;
statuses.script = {...done, logs: []};
context.API.get = initialGet;
context.confirmIfRemote = async () => true;
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (effects.posts.length === 1) {
        await firstAck.p;
    } else if (url.endsWith('/cancel')) {
        statuses.script = STOP_CONFIRMED ? {...done, logs: ['INTERRUPTED']} : INITIAL_STATUS;
    } else if (effects.posts.length === 3) {
        statuses.script = {...live, task_claim_id: 'b'.repeat(32), logs: ['FRESH RUN']};
    }
    return {task_claim_id: effects.posts.length === 1 ? ADMITTED_CLAIM : statuses.script.task_claim_id};
};
const generate = gen.listeners.click();
await settle();
await fresh.listeners.click();
await forwarded;
assert(context._scriptQueuedRestart, 'Start Over queued behind Generate');
statuses.script = live;
firstAck.yes({});
await settle();
for (let i = 0; i < 24 && context._scriptGeneratePending; i++) {
    if (timers.length) { await tick(); }
    await settle();
}
await generate;
await forwarded;
await settle();

const admissions = Object.values(context._scriptAdmissions);
assert.strictEqual(new Set(admissions.map(record => record.token)).size, admissions.length);
if (STOP_CONFIRMED) {
    assert.deepStrictEqual(effects.posts.map(([url, body]) => [url, body.start_over]), [
        ['/api/generate_script', false],
        ['/api/generate_script/cancel', undefined],
        ['/api/generate_script', true],
    ]);
    assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts[1][1])),
        {expected_claim_id: ADMITTED_CLAIM});
    assertBusy();
    assert.strictEqual(el('script-logs').innerText, 'FRESH RUN');
    assert.strictEqual(admissions.at(-2).state, 'delivered');
    const noticesBeforeStale = jobs().length;
    previousStatus.yes({...done, logs: ['STALE PRE-ROLLBACK TERMINAL']});
    await settle();
    assertBusy();
    assert.strictEqual(el('script-logs').innerText, 'FRESH RUN');
    assert.strictEqual(jobs().length, noticesBeforeStale);
    statuses.script = {...done, logs: ['FRESH DONE']};
    for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
    assert.strictEqual(admissions.at(-1).state, 'delivered');
    assert.strictEqual(jobs().length, noticesBeforeStale + 1);
    assert.strictEqual(gen.disabled, false);
} else if (ADMITTED_CLAIM === INITIAL_STATUS.task_claim_id && ADMITTED_CLAIM) {
    assert.deepStrictEqual(effects.posts.map(([url, body]) => [url, body.start_over]), [
        ['/api/generate_script', false],
        ['/api/generate_script/cancel', undefined],
    ]);
    assert(effects.notifications.some(notice => notice[0] === 'action'
        && notice[1] === 'Start over could not be completed'));
    assertBusy();
} else {
    assert.deepStrictEqual(effects.posts.map(([url]) => url), ['/api/generate_script']);
    assert.strictEqual(admissions.at(-1).state, 'accepted');
    assert(effects.notifications.some(notice => notice[0] === 'action'
        && notice[1] === 'Start over could not be completed'));
    assertBusy();
}
"""


RESTART_SEES_NEW_OWNER = r"""
const originalClaim = live.task_claim_id, replacementClaim = 'b'.repeat(32);
const originalAck = delay(), delayedStop = delay();
let holdStop = false, stopRead = false;
const ends = [];
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, state) => {
    notify(task, id, outcome, state);
    ends.push(state.logs.at(-1));
};
statuses = {script: RESTART === 'direct' ? {...live, logs: ['A LIVE']} : {...done, logs: []}};
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (effects.posts.length === 1) {
        if (RESTART === 'queued') { await originalAck.p; }
        return {task_claim_id: originalClaim};
    }
    if (url.endsWith('/cancel')) {
        assert.strictEqual(body.expected_claim_id, originalClaim);
        holdStop = true;
        return {};
    }
    throw Object.assign(Error('Unexpected replacement POST'), {status: 409});
};
context.API.get = url => {
    if (holdStop && url === '/api/status/script') {
        holdStop = false;
        stopRead = true;
        return delayedStop.p;
    }
    return initialGet(url);
};
const generate = gen.listeners.click();
await settle();
const restart = fresh.listeners.click();
await settle();
if (RESTART === 'queued') {
    await restart;
    await forwarded;
    assert(context._scriptQueuedRestart);
    statuses.script = {...live, logs: ['A LIVE']};
    originalAck.yes({});
    await settle();
}
assert(stopRead);
const original = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
assert.strictEqual(original.claimId, originalClaim);
assert.strictEqual(original.state, 'accepted');
assertBusy();
context.reattachTaskActivity('script', [], 'script-logs', null,
    {...live, ownership_unconfirmed: true});
await settle();
assert(run("taskStartClaims.has('script')"));
assert.deepStrictEqual(effects.releases, []);

statuses.script = {...live, task_claim_id: replacementClaim, logs: ['B LIVE']};
await context.reattachRunningPollers();
await settle();
const current = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
assert.strictEqual(el('script-logs').innerText, 'B LIVE');
delayedStop.yes({...done, logs: ['A STOP']});
await generate;
await restart;
await forwarded;
await settle();
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: originalClaim}],
]);
assert.strictEqual(current.claimId, replacementClaim);
assert.strictEqual(current.state, 'accepted');
assert.strictEqual(context._scriptRecoveryAcceptedRun, current.token);
assert.notStrictEqual(current.token, original.token);
assertBusy();
assert.strictEqual(el('script-logs').innerText, 'B LIVE');
assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
assert.deepStrictEqual(ends, ['A STOP']);
assert(!run("taskStartClaims.has('script')"));
assert.deepStrictEqual(effects.releases, ['script']);

statuses.script = {...done, logs: ['B STOP']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(current.state, 'delivered');
assert.strictEqual(el('script-logs').innerText, 'B STOP');
assert.deepStrictEqual(ends, ['A STOP', 'B STOP']);
assert.deepStrictEqual(effects.releases, ['script']);
assert.strictEqual(el('btn-snapshot-script').style.display, 'none');
assert.strictEqual(gen.disabled, false);
"""


STOP_BEFORE_GENERATE_ACK_SETUP = r"""
const originalClaim = live.task_claim_id, replacementClaim = 'b'.repeat(32);
const originalAck = delay(), ends = [];
const holdStopResponse = typeof HOLD_STOP_RESPONSE !== 'undefined' && HOLD_STOP_RESPONSE;
const originalStop = delay();
let stoppedStatus;
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, state) => {
    notify(task, id, outcome, state);
    ends.push(state.logs.at(-1));
};
statuses = {script: {...live, start_time: 101, logs: ['A LIVE']}};
let startReplacement = () => {
    assert.strictEqual(el('script-logs').innerText, 'A STOP');
    assert.deepStrictEqual(ends, ['A STOP']);
    statuses.script = {...live, task_claim_id: replacementClaim, logs: ['B LIVE']};
    return {task_claim_id: replacementClaim};
};
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (effects.posts.length === 1) {
        await originalAck.p;
        return {task_claim_id: originalClaim};
    }
    if (url.endsWith('/cancel')) {
        statuses.script = {...done, start_time: 101, logs: ['A STOP']};
        return {};
    }
    return startReplacement();
};
const generate = gen.listeners.click();
await settle();
const original = context.getScriptAdmissionByToken(context._scriptRecoveryRunToken);
assert.strictEqual(original.state, 'provisional');
if (holdStopResponse) {
    context.API.get = url => {
        if (url === '/api/status/script' && !statuses.script.running && !stoppedStatus) {
            stoppedStatus = statuses.script;
            return originalStop.p;
        }
        return initialGet(url);
    };
}
const restart = fresh.listeners.click();
if (!holdStopResponse) { await restart; }
await settle();
if (holdStopResponse) { assert.strictEqual(stoppedStatus?.logs[0], 'A STOP'); }
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: originalClaim}],
]);
assert.deepStrictEqual(ends, []);
assertBusy();
"""


STOP_BEFORE_GENERATE_ACK_AFTER_STOP = r"""
originalAck.yes({});
await generate;
await forwarded;
await settle();
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: originalClaim}],
    ['/api/generate_script', {first_person_narrator: null, start_over: true, expected_restart_claim_id: originalClaim}],
]);
const current = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
assert.strictEqual(current.claimId, replacementClaim);
assert.strictEqual(current.state, 'accepted');
assert.strictEqual(original.state, 'delivered');
assertBusy();
assert.strictEqual(el('script-logs').innerText, 'B LIVE');
assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
assert.deepStrictEqual(ends, ['A STOP']);
assert(!run("taskStartClaims.has('script')"));

statuses.script = {...done, logs: ['B STOP']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.strictEqual(current.state, 'delivered');
assert.deepStrictEqual(ends, ['A STOP', 'B STOP']);
assert.strictEqual(el('script-logs').innerText, 'B STOP');
assert.strictEqual(gen.disabled, false);
"""


STOP_BEFORE_GENERATE_ACK = STOP_BEFORE_GENERATE_ACK_SETUP + STOP_BEFORE_GENERATE_ACK_AFTER_STOP


BETWEEN_STOP_POLLS = r"""
const ack = delay(), ends = [], reads = [];
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, state) => {
    ends.push(state.logs.at(-1));
    notify(task, id, outcome, state);
};
statuses = {script: originalLive};
context.API.get = async url => {
    const response = await initialGet(url);
    if (url === '/api/status/script') { reads.push(response); }
    return response;
};
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (effects.posts.length === 1) {
        await ack.p;
        return {task_claim_id: originalLive.task_claim_id};
    }
    if (url.endsWith('/cancel')) {
        statuses.script = originalStopping;
        return {};
    }
    statuses.script = {...originalLive, task_claim_id: 'c'.repeat(32), start_time: 303, logs: ['C LIVE']};
    return {task_claim_id: statuses.script.task_claim_id};
};
const generate = gen.listeners.click();
await settle();
const original = context.getScriptAdmissionByToken(context._scriptRecoveryRunToken);
const restart = fresh.listeners.click();
await settle();
assert.strictEqual(timers.length, 1);
assert.strictEqual(reads.at(-1), originalStopping);
// B's terminal is seen without a preceding confirmed live B sample.
statuses.script = originalStopping;
if (ackFirst) { ack.yes(); await generate; await settle(); }
const liveLog = el('script-logs').innerText;
statuses.script = terminal;
await tick();
await restart;
if (!ackFirst) { ack.yes(); await generate; }
await forwarded;
await settle();
for (let i = 0; timers.length && i < 16 && !shouldRestart; i++) { await tick(); await settle(); }
const wire = [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: originalLive.task_claim_id}],
];
if (shouldRestart) {
    wire.push(['/api/generate_script', {
        first_person_narrator: null, start_over: true, expected_restart_claim_id: originalLive.task_claim_id,
    }]);
}
const notices = effects.notifications.filter(note => note[0] === 'action');
log({ackFirst, terminal: [terminal.start_time, terminal.task_claim_id, terminal.logs.at(-1)],
    posts: effects.posts, ends, log: el('script-logs').innerText, notices});
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), wire);
assert(reads.includes(terminal));
assert(!liveLog.includes('B LIVE'));
assert(!el('script-logs').innerText.includes('B TERMINAL'));
assert.strictEqual(notices.length, shouldRestart ? 0 : 1);
assert.deepStrictEqual(ends, shouldRestart ? ['A STOP'] : []);
if (shouldRestart) {
    assert.strictEqual(original.state, 'delivered');
    assertBusy();
    assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
    assert.strictEqual(el('script-logs').innerText, 'C LIVE');
    statuses.script = {...terminal, start_time: 303, task_claim_id: null, logs: ['C STOP']};
    for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
    assert.deepStrictEqual(ends, ['A STOP', 'C STOP']);
    assert.strictEqual(el('script-logs').innerText, 'C STOP');
    assert.strictEqual(gen.disabled, false);
} else {
    assert.strictEqual(original.state, 'superseded');
    assert.strictEqual(original.restartStop, undefined);
    assert.strictEqual(gen.disabled, false);
    assert.strictEqual(el('btn-pause-script').style.display, 'none');
    assert.strictEqual(el('btn-cancel-script').style.display, 'none');
    assert.strictEqual(el('btn-snapshot-script').style.display, 'none');
}
assert(!run("taskStartClaims.has('script')"));
"""


HELD_SCRIPT_STOP = r"""
const ack = delay(), heldStop = delay(), ends = [], notices = [], reads = [];
let stopRead = false;
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, state) => {
    ends.push(state.logs.at(-1));
    notify(task, id, outcome, state);
};
context.showToast = (message, type) => notices.push([type, message]);
context.showActionError = (message, error, recovery, type) => notices.push([type, message]);
statuses = {script: a};
context.API.get = async url => {
    const status = await initialGet(url);
    if (url === '/api/status/script') {
        if (!stopRead && status === aEnd) {
            stopRead = true;
            await heldStop.p;
        }
        reads.push(status);
    }
    return status;
};
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (effects.posts.length === 1) {
        await ack.p;
        return {task_claim_id: (ackOriginal ? a : b).task_claim_id};
    }
    if (url.endsWith('/cancel')) {
        statuses.script = aEnd;
        return {};
    }
    statuses.script = {...b, task_claim_id: 'c'.repeat(32), start_time: 303, logs: ['C LIVE']};
    return {task_claim_id: statuses.script.task_claim_id};
};

const generate = gen.listeners.click();
await settle();
const admission = context.getScriptAdmissionByToken(context._scriptRecoveryRunToken);
const restart = fresh.listeners.click();
await settle();
assert(stopRead);
statuses.script = b;
if (stopBeforeAck) {
    heldStop.yes();
    await restart;
    assert(context._scriptQueuedRestart);
}
if (ackBeforeTerminal) { ack.yes(); await generate; await settle(); }
const snapshot = el('btn-snapshot-script');
const cancel = el('btn-cancel-script');
const pause = el('btn-pause-script');
const whenLive = {
    log: el('script-logs').innerText,
    controls: [gen.disabled, cancel.style.display, pause.style.display, snapshot.style.display],
};
statuses.script = bEnd;
if (!ackBeforeTerminal) { ack.yes(); await generate; await settle(); }
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
const whenTerminal = {state: admission.state, log: el('script-logs').innerText, ends: [...ends]};
if (!stopBeforeAck) { heldStop.yes(); await restart; }
await forwarded;
await settle();

const finalLog = el('script-logs').innerText;
log({ackOriginal, ackBeforeTerminal, stopBeforeAck, whenLive, whenTerminal, finalLog, ends, notices,
    posts: effects.posts});
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: a.task_claim_id}],
]);
assert(reads.includes(aEnd) && reads.includes(bEnd));
assert.strictEqual(admission.claimId, (ackOriginal ? a : b).task_claim_id);
assert.strictEqual(admission.state, 'delivered');
const adopted = ackOriginal && ackBeforeTerminal;
const finalOwner = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
if (adopted) {
    assert.notStrictEqual(finalOwner.token, admission.token);
    assert.strictEqual(finalOwner.claimId, b.task_claim_id);
    assert.strictEqual(finalOwner.state, 'delivered');
} else {
    assert.strictEqual(finalOwner.token, admission.token);
}
assert.deepStrictEqual(ends, adopted ? [bEnd.logs.at(-1), aEnd.logs.at(-1)]
    : [(ackOriginal ? aEnd : bEnd).logs.at(-1)]);
assert.strictEqual(notices.filter(([type, message]) => type === 'warning'
    && /not stopped yet|Start over could not be completed/.test(message)).length, 1);
if (ackOriginal) {
    assert.strictEqual(whenTerminal.state, 'superseded');
    assert.deepStrictEqual(whenTerminal.ends, adopted ? [bEnd.logs.at(-1)] : []);
    assert.strictEqual(finalLog, (adopted ? bEnd : aEnd).logs.at(-1));
} else {
    assert.strictEqual(whenTerminal.state, 'delivered');
    assert.deepStrictEqual(whenTerminal.ends, [bEnd.logs.at(-1)]);
    assert.strictEqual(finalLog, bEnd.logs.at(-1));
    if (ackBeforeTerminal) {
        assert.strictEqual(whenLive.log, b.logs.at(-1));
        assert.deepStrictEqual(whenLive.controls, [true, 'inline-block', 'inline-block', 'inline-block']);
    }
}
assert.strictEqual(context._scriptRecoveryRunActive, false);
assert.strictEqual(gen.disabled, false);
for (const control of [cancel, pause, snapshot]) { assert.strictEqual(control.style.display, 'none'); }
assert(!run("taskStartClaims.has('script')"));
"""


LIVE_SUCCESSOR_DURING_HELD_STOP = r"""
const ack = delay(), cancelAck = delay(), hold = delay(), ends = [], reads = [];
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, data) => {
    ends.push(data.logs.at(-1));
    notify(task, id, outcome, data);
};
let held = false;
statuses = {script: a};
context.API.get = async url => {
    const data = await initialGet(url);
    if (url === '/api/status/script') {
        if (!held && data === aEnd) {
            held = true;
            await hold.p;
        }
        reads.push(data.logs.at(-1));
    }
    return data;
};
context.API.post = async (url, payload) => {
    effects.posts.push([url, payload]);
    if (!reload && effects.posts.length === 1) {
        await ack.p;
        return {task_claim_id: a.task_claim_id};
    }
    if (url.endsWith('/cancel')) {
        assert([a.task_claim_id, b.task_claim_id].includes(payload.expected_claim_id));
        if (payload.expected_claim_id === a.task_claim_id) {
            if (reload) { await cancelAck.p; }
            statuses.script = aEnd;
        } else {
            statuses.script = bEnd;
        }
        return {};
    }
    statuses.script = {...b, task_claim_id: 'c'.repeat(32), start_time: 303, logs: ['C LIVE']};
    return {task_claim_id: statuses.script.task_claim_id};
};
let generate;
if (!reload) {
    generate = gen.listeners.click();
    await settle();
    if (accepted) { ack.yes(); await generate; await settle(); }
}
const restart = fresh.listeners.click();
await settle();
if (reload) {
    await context.reattachRunningPollers();
    await settle();
    cancelAck.yes();
    await settle();
}
const original = context.getScriptAdmissionByToken(context._scriptRecoveryRunToken);
assert(held);
statuses.script = b;
if (!reload && !accepted) { ack.yes(); await generate; await settle(); }
for (let i = 0; i < 16 && timers.length && !reads.includes('B LIVE'); i++) { await tick(); await settle(); }
assert(reads.includes('B LIVE'));
const successor = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
const whenLive = el('script-logs').innerText;
if (!freshRestart) { statuses.script = bEnd; }
if (pollTerminal) {
    for (let i = 0; i < 16 && timers.length && !reads.includes(bEnd.logs.at(-1)); i++) { await tick(); await settle(); }
    assert(reads.includes(bEnd.logs.at(-1)));
}
hold.yes();
await restart;
await forwarded;
await settle();
const firstDelivery = [...ends];
context.onScriptTaskTerminal(aEnd, original.token, 'start-over');
const wire = [
    ...(reload ? [] : [['/api/generate_script', {first_person_narrator: null, start_over: false}]]),
    ['/api/generate_script/cancel', {expected_claim_id: a.task_claim_id}],
];
log({accepted, reload, pollTerminal, freshRestart, reads, ends, firstDelivery, posts: effects.posts,
    log: el('script-logs').innerText, live: whenLive, successor: successor.claimId});
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), wire);
assert.notStrictEqual(successor.token, original.token);
assert.strictEqual(successor.claimId, b.task_claim_id);
assert.strictEqual(whenLive, 'B LIVE');
assert.deepStrictEqual(firstDelivery, pollTerminal
    ? [bEnd.logs.at(-1), aEnd.logs.at(-1)] : [aEnd.logs.at(-1)]);
assert.strictEqual(original.state, 'delivered');
if (freshRestart) {
    coreSlice('const PAUSE_BUTTON_FOR_TASK =', 'const ACTIVITY_MARKER =');
    const pauseClasses = new Set();
    el('btn-pause-script').classList = {
        add: name => pauseClasses.add(name),
        remove: name => pauseClasses.delete(name),
        contains: name => pauseClasses.has(name),
    };
    for (let i = 0; i < 5; i++) {
        statuses.script = {...b, paused: i % 2 === 0, logs: ['B LIVE', 'B tick ' + i]};
        await tick();
        await settle();
        assert.strictEqual(context._scriptRecoveryAcceptedRun, successor.token);
        assert.strictEqual(el('script-logs').innerText, 'B LIVE\nB tick ' + i);
        assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
        assert.strictEqual(el('btn-pause-script').classList.contains('btn-outline-success'), i % 2 === 0);
        assertBusy();
    }
    await fresh.listeners.click();
    await forwarded;
    await settle();
    wire.push(['/api/generate_script/cancel', {expected_claim_id: b.task_claim_id}]);
    wire.push(['/api/generate_script', {
        first_person_narrator: null, start_over: true, expected_restart_claim_id: b.task_claim_id,
    }]);
    log({posts: effects.posts, ends, log: el('script-logs').innerText});
    assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), wire);
    assert.deepStrictEqual(ends, [aEnd.logs.at(-1), bEnd.logs.at(-1)]);
    assert.strictEqual(el('script-logs').innerText, 'C LIVE');
    assertBusy();
    statuses.script = {...done, logs: ['C STOP']};
} else {
    assert.strictEqual(el('script-logs').innerText, pollTerminal ? bEnd.logs.at(-1) : 'B LIVE');
}
for (let i = 0; i < 16 && timers.length; i++) { await tick(); await settle(); }
assert.deepStrictEqual(ends, pollTerminal ? [bEnd.logs.at(-1), aEnd.logs.at(-1)]
    : [aEnd.logs.at(-1), bEnd.logs.at(-1), ...(freshRestart ? ['C STOP'] : [])]);
assert.strictEqual(el('script-logs').innerText, freshRestart ? 'C STOP' : bEnd.logs.at(-1));
assert.strictEqual(context._scriptRecoveryRunActive, false);
assert.strictEqual(gen.disabled, false);
for (const id of ['btn-cancel-script', 'btn-pause-script', 'btn-snapshot-script']) {
    assert.strictEqual(el(id).style.display, 'none');
}
assert(!run("taskStartClaims.has('script')"));
"""


PENDING_SUCCESSOR_DURING_STOP = r"""
const hold = delay(), ends = [];
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, status) => {
    ends.push(status.logs.at(-1));
    notify(task, id, outcome, status);
};
let held = false;
statuses = {script: a};
context.API.get = async url => {
    const status = await initialGet(url);
    if (url === '/api/status/script' && status === aEnd && !held) {
        held = true;
        await hold.p;
    }
    return status;
};
context.API.post = async (url, payload) => {
    effects.posts.push([url, payload]);
    if (url.endsWith('/cancel')) {
        statuses.script = payload.expected_claim_id === a.task_claim_id ? aEnd : bEnd;
        return {};
    }
    if (!payload.start_over) { return {task_claim_id: a.task_claim_id}; }
    statuses.script = {...live, task_claim_id: 'c'.repeat(32), start_time: 303, logs: ['C LIVE']};
    return {task_claim_id: statuses.script.task_claim_id};
};
await gen.listeners.click();
await settle();
const original = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
const restart = fresh.listeners.click();
await settle();
assert(held);
statuses.script = pending;
await tick();
await settle();
const scriptLog = el('script-logs');
const workerControls = ['btn-pause-script', 'btn-cancel-script', 'btn-snapshot-script'].map(el);
const whilePending = {
    owner: context._scriptRecoveryAcceptedRun,
    log: scriptLog.innerText,
    busy: gen.disabled,
    controls: workerControls.map(button => button.style.display),
};
if (stopFirst) { hold.yes(); await restart; }
statuses.script = b || bEnd;
await tick();
await settle();
if (!stopFirst) { hold.yes(); await restart; }
await forwarded;
await settle();
const wire = [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: a.task_claim_id}],
];
log({stopFirst, refused: !b, whilePending, ends, log: scriptLog.innerText, posts: effects.posts});
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), wire);
assert.strictEqual(whilePending.owner, original.token);
assert.strictEqual(whilePending.log, 'A LIVE');
assert.strictEqual(whilePending.busy, true);
assert.deepStrictEqual(whilePending.controls, ['none', 'none', 'none']);
assert.deepStrictEqual(ends, ['A STOP']);
assert.strictEqual(original.state, 'delivered');
if (b) {
    assert.strictEqual(context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun).claimId, b.task_claim_id);
    assert.strictEqual(scriptLog.innerText, 'B LIVE');
    assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
    assertBusy();
    await fresh.listeners.click();
    await forwarded;
    await settle();
    wire.push(['/api/generate_script/cancel', {expected_claim_id: b.task_claim_id}]);
    wire.push(['/api/generate_script', {
        first_person_narrator: null, start_over: true, expected_restart_claim_id: b.task_claim_id,
    }]);
    log({freshB: effects.posts, ends, log: scriptLog.innerText});
    assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), wire);
    assert.deepStrictEqual(ends, ['A STOP', bEnd.logs.at(-1)]);
    assertBusy();
    assert.strictEqual(scriptLog.innerText, 'C LIVE');
} else {
    assert.strictEqual(scriptLog.innerText, 'A STOP');
    assert.strictEqual(gen.disabled, false);
    assert.deepStrictEqual(workerControls.map(button => button.style.display), ['none', 'none', 'none']);
}
"""


SUCCESSOR_AFTER_REFUSED_RESTART = r"""
const stopResponse = delay(), restartResponse = delay(), ends = [];
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, status) => {
    ends.push([status.worker_claim_id, status.logs.at(-1)]);
    notify(task, id, outcome, status);
};
let heldStop = false;
context.API.get = async url => {
    const status = await initialGet(url);
    if (url === '/api/status/script' && status === aEnd && !heldStop) {
        heldStop = true;
        await stopResponse.p;
    }
    return status;
};
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (url.endsWith('/cancel')) { statuses.script = aEnd; return {}; }
    if (!body.start_over) { return {task_claim_id: a.task_claim_id}; }
    if (mode === 'refusal-pending') { await restartResponse.p; }
    throw Object.assign(new Error(typeof reply.detail === 'string' ? reply.detail : JSON.stringify(reply.detail)), {status: code});
};
statuses = {script: a};
await gen.listeners.click();
await settle();
const original = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
const restart = fresh.listeners.click();
await settle();
assert(heldStop);
const liveFirst = mode !== 'terminal';
statuses.script = liveFirst ? b : bEnd;
stopResponse.yes();
await restart;
if (mode === 'refusal-pending') {
    await tick();
    await settle();
    assert.strictEqual(el('script-logs').innerText, 'A STOP');
    assert.deepStrictEqual(ends, [[a.task_claim_id, 'A STOP']]);
    restartResponse.yes();
}
await forwarded;
await settle();
const wire = [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: a.task_claim_id}],
    ['/api/generate_script', {
        first_person_narrator: null, start_over: true, expected_restart_claim_id: a.task_claim_id,
    }],
];
const logText = el('script-logs');
const controls = ['btn-pause-script', 'btn-cancel-script', 'btn-snapshot-script'].map(el);
const view = () => ({log: logText.innerText, busy: gen.disabled, controls: controls.map(el => el.style.display)});
const drain = async () => {
    for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
};
assert.strictEqual(original.state, 'delivered');
assert.deepStrictEqual(ends, [[a.task_claim_id, 'A STOP']]);
assert.strictEqual(logText.innerText, 'A STOP');
if (mode === 'selection') { selectBook('another-book.txt'); }
if (mode === 'reserved') {
    statuses.script = pending;
    await drain();
    assert.deepStrictEqual(view(), {log: 'A STOP', busy: true, controls: ['none', 'none', 'none']});
    statuses.script = b;
}
if (mode === 'unverified') {
    statuses.script = {...b, worker_claim_id: a.task_claim_id, logs: ['UNVERIFIED']};
    await drain();
    assert.strictEqual(logText.innerText, 'A STOP');
    assert.deepStrictEqual(ends, [[a.task_claim_id, 'A STOP']]);
    assert.strictEqual(context._scriptRecoveryAcceptedRun, 0);
    statuses.script = b;
}
await drain();
const owner = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
const first = view();
statuses.script = bEnd;
await drain();
log({mode, wire: effects.posts, first, final: view(), ends});
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), wire);
if (mode === 'selection') {
    assert.deepStrictEqual(ends, [[a.task_claim_id, 'A STOP']]);
    assert.notStrictEqual(logText.innerText, bEnd.logs.at(-1));
} else {
    assert.strictEqual(owner.claimId, b.worker_claim_id);
    assert.notStrictEqual(owner.token, original.token);
    if (liveFirst) {
        assert.deepStrictEqual(first, {log: 'B LIVE', busy: true,
            controls: ['inline-block', 'inline-block', 'inline-block']});
    }
    assert.strictEqual(owner.state, 'delivered');
    assert.deepStrictEqual(ends, [[a.task_claim_id, 'A STOP'], [b.worker_claim_id, bEnd.logs.at(-1)]]);
    assert.deepStrictEqual(view(), {log: bEnd.logs.join('\n'), busy: false, controls: ['none', 'none', 'none']});
    assert(!run("taskStartClaims.has('script')"));
}
"""


TERMINAL_DURING_RESTART_ADMISSION = r"""
const heldStop = delay(), admission = delay(), ends = [];
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, status) => {
    ends.push([status.worker_claim_id, status.logs.at(-1)]);
    notify(task, id, outcome, status);
};
let held = false, replacementPosted = false;
let readsOfB = 0;
context.API.get = async url => {
    const ordinaryPoll = new Error().stack.includes('fetchStatus');
    const status = await initialGet(url);
    if (url === '/api/status/script') {
        if (!held && status === aEnd) { held = true; await heldStop.p; }
        if (status === bEnd && ordinaryPoll) { readsOfB++; }
    }
    return status;
};
const accepted = mode.startsWith('accept');
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (url.endsWith('/cancel')) { statuses.script = aEnd; return {}; }
    if (!body.start_over) { return {task_claim_id: a.task_claim_id}; }
    replacementPosted = true;
    await admission.p;
    if (accepted) { return ack; }
    throw Object.assign(new Error(refusal.detail), {status: 409});
};
statuses = {script: a};
await gen.listeners.click();
await settle();
const restart = fresh.listeners.click();
await settle();
assert(held);
heldStop.yes();
await restart;
await settle();
assert(replacementPosted);
const drain = async () => {
    for (let i = 0; timers.length && i < 12; i++) { await tick(); await settle(); }
};
if (mode === 'select-before') { selectBook('another-book.txt'); }
statuses.script = bEnd;
await drain();
assert(readsOfB >= 1);
const aDone = [a.worker_claim_id, 'A STOP'];
const bDone = [bEnd.worker_claim_id, bEnd.logs.at(-1)];
const beforeC = ends.slice();
const heldLog = el('script-logs').innerText;
if (mode === 'select-after') { selectBook('another-book.txt'); }
const finalStatus = mode === 'accept-same' ? bEnd : cEnd;
statuses.script = finalStatus;
admission.yes();
await forwarded;
await drain();
const owner = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
const finalLog = el('script-logs').innerText;
log({mode, readsOfB, beforeC, ends, heldLog, finalLog, owner: owner?.claimId, posts: effects.posts});
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: a.task_claim_id}],
    ['/api/generate_script', {first_person_narrator: null, start_over: true, expected_restart_claim_id: a.task_claim_id}],
]);
assert.strictEqual(heldLog, 'A STOP');
assert.deepStrictEqual(beforeC, mode === 'select-before' ? [aDone] : [aDone, bDone]);
if (mode.startsWith('select')) {
    assert.deepStrictEqual(ends, beforeC);
    assert.strictEqual(finalLog, 'A STOP');
} else {
    assert.deepStrictEqual(ends, mode === 'accept-same' ? [aDone, bDone]
        : [aDone, bDone, [cEnd.worker_claim_id, cEnd.logs.at(-1)]]);
    assert.strictEqual(owner.claimId, finalStatus.worker_claim_id);
    assert.strictEqual(owner.state, 'delivered');
    assert.strictEqual(finalLog, finalStatus.logs.join('\n'));
    assert.strictEqual(gen.disabled, false);
    for (const id of ['btn-pause-script', 'btn-cancel-script', 'btn-snapshot-script']) {
        assert.strictEqual(el(id).style.display, 'none');
    }
}
"""


PENDING_WORKER_OUTCOMES = r"""
const hold = delay(), ends = [];
const heldStop = mode.includes('held');
const seesReservation = ['reload', 'ordinary', 'held'].includes(mode);
const seesWorker = mode === 'reload' || mode.endsWith('-live');
const sameWorker = mode === 'same';
const notify = context.notifyJobDone;
context.notifyJobDone = (task, id, outcome, status) => {
    ends.push([status.worker_claim_id || null, status.logs.at(-1)]);
    notify(task, id, outcome, status);
};
let held = false;
context.API.get = async url => {
    const status = await initialGet(url);
    if (heldStop && url === '/api/status/script' && status === aEnd && !held) {
        held = true;
        await hold.p;
    }
    return status;
};
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    if (url.endsWith('/cancel')) { statuses.script = aEnd; return {}; }
    assert(!body.start_over);
    return {task_claim_id: a.task_claim_id};
};
const wire = [];
let original, restart;
if (mode === 'reload') {
    statuses = {script: pending};
    await context.reattachRunningPollers();
    await settle();
} else {
    statuses = {script: a};
    await gen.listeners.click();
    await settle();
    original = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
    wire.push(['/api/generate_script', {first_person_narrator: null, start_over: false}]);
    if (heldStop) {
        restart = fresh.listeners.click();
        await settle();
        assert(held);
        wire.push(['/api/generate_script/cancel', {expected_claim_id: a.task_claim_id}]);
    }
    if (seesReservation) {
        statuses.script = pending;
        await tick();
        await settle();
    }
}
const logText = el('script-logs');
const controls = ['btn-pause-script', 'btn-cancel-script', 'btn-snapshot-script'].map(el);
const pendingView = {busy: gen.disabled, log: logText.innerText, controls: controls.map(el => el.style.display)};
let liveView;
if (seesWorker) {
    statuses.script = b;
    await tick();
    await settle();
    const id = context._scriptRecoveryAcceptedRun;
    const owner = context.getScriptAdmissionByToken(context._scriptRecoveryRunToken);
    liveView = {id, token: owner.token, state: owner.state, log: logText.innerText, claim: owner.claimId,
        controls: controls.map(el => el.style.display)};
}
if (heldStop && stopFirst) { hold.yes(); await restart; }
statuses.script = sameWorker ? aEnd : bEnd;
await tick();
await settle();
if (heldStop && !stopFirst) { hold.yes(); await restart; }
await settle();
const final = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
log({mode, stopFirst, pendingView, liveView, ends, state: final?.state, claim: final?.claimId,
    finalLog: logText.innerText, active: context._scriptRecoveryRunActive, posts: effects.posts});
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), wire);
assert.strictEqual(pendingView.busy, true);
if (seesReservation) { assert.deepStrictEqual(pendingView.controls, ['none', 'none', 'none']); }
assert.strictEqual(pendingView.log, mode === 'reload' ? '' : 'A LIVE');
if (seesWorker) {
    assert.strictEqual(liveView.state, 'accepted');
    assert.strictEqual(liveView.id, liveView.token);
    assert.strictEqual(liveView.claim, b.task_claim_id);
    assert.strictEqual(liveView.log, 'B LIVE');
    assert.deepStrictEqual(liveView.controls, ['inline-block', 'inline-block', 'inline-block']);
}
assert.strictEqual(final.claimId, (sameWorker ? a : b).task_claim_id);
assert.strictEqual(final.state, 'delivered');
if (original && !sameWorker) {
    assert.notStrictEqual(original.token, final.token);
    assert.strictEqual(original.state, heldStop ? 'delivered' : 'superseded');
}
const bTerminal = [b.task_claim_id, bEnd.logs.at(-1)];
const aTerminal = [aEnd.worker_claim_id || null, aEnd.logs.at(-1)];
assert.deepStrictEqual(ends, heldStop
    ? (stopFirst ? [aTerminal, bTerminal] : [bTerminal, aTerminal]) : [sameWorker ? aTerminal : bTerminal]);
assert.strictEqual(logText.innerText, (sameWorker ? aEnd : bEnd).logs.join('\n'));
assert.strictEqual(gen.disabled, false);
assert.strictEqual(context._scriptRecoveryRunActive, false);
assert.deepStrictEqual(controls.map(el => el.style.display), ['none', 'none', 'none']);
"""


SAVED_STOP_BEFORE_FOREIGN_TERMINAL = STOP_BEFORE_GENERATE_ACK_SETUP + r"""
assert.strictEqual(original.restartStop?.start_time, 101);
statuses.script = {...done, task_claim_id: null, start_time: 202, logs: ['B TERMINAL']};
originalAck.yes();
await generate;
await settle();
log({posts: effects.posts, ends, log: el('script-logs').innerText});
assert.strictEqual(effects.posts.length, 2);
assert.strictEqual(el('script-logs').innerText, 'A STOP');
assert.deepStrictEqual(ends, ['A STOP']);
assert.strictEqual(effects.notifications.filter(note => note[0] === 'action').length, 1);
assert.strictEqual(gen.disabled, false);
assert(!run("taskStartClaims.has('script')"));
"""


SAME_CLAIM_DURING_STOP = STOP_BEFORE_GENERATE_ACK_SETUP + r"""
statuses.script = {...live, start_time: 101, logs: ['A LIVE']};
await context.reattachRunningPollers();
await settle();
assert.strictEqual(context._scriptRecoveryRunToken, original.token);
const observed = Object.values(context._scriptAdmissions).find(record => record.source === 'observe');
assert.strictEqual(observed?.claimId, originalClaim);
assert(run("taskStartClaims.has('script')"));
statuses.script = stoppedStatus;
originalStop.yes(stoppedStatus);
await restart;
""" + STOP_BEFORE_GENERATE_ACK_AFTER_STOP + r"""
assert.deepStrictEqual(effects.releases, ['script']);
"""


EXTERNAL_OWNER_BEFORE_GENERATE_ACK = STOP_BEFORE_GENERATE_ACK_SETUP + r"""
startReplacement = () => {
    const thirdClaim = 'c'.repeat(32);
    statuses.script = {...live, task_claim_id: thirdClaim, logs: ['LOCAL C LIVE']};
    return {task_claim_id: thirdClaim};
};
statuses.script = {...live, task_claim_id: replacementClaim, start_time: 202, logs: ['EXTERNAL B LIVE']};
await context.reattachRunningPollers();
await settle();
assertBusy();
const observed = Object.values(context._scriptAdmissions).find(record => record.claimId === replacementClaim);
assert(observed && observed.source === 'observe');
const finished = {...done, start_time: 202, logs: ['EXTERNAL B FINISHED']};
const foreignTerminal = typeof FOREIGN_TERMINAL !== 'undefined' && FOREIGN_TERMINAL;
const stopReply = foreignTerminal ? finished : stoppedStatus;
if (EXTERNAL_FINISHED) {
    statuses.script = finished;
    for (let i = 0; timers.length && i < 8; i++) { await tick(); await settle(); }
}

if (holdStopResponse && !ACK_BEFORE_STOP_RESPONSE) {
    originalStop.yes(stopReply);
    await restart;
}
originalAck.yes({});
await generate;
if (holdStopResponse && ACK_BEFORE_STOP_RESPONSE) {
    originalStop.yes(stopReply);
    await restart;
}
await forwarded;
await settle();
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: originalClaim}],
]);
assert.strictEqual(original.claimId, originalClaim);
assert.strictEqual(original.state, foreignTerminal ? 'accepted' : 'delivered');
assert.strictEqual(context._scriptRecoveryAcceptedRun, observed.token);
assert.strictEqual(observed.claimId, replacementClaim);
if (EXTERNAL_FINISHED) {
    assert.deepStrictEqual(ends, foreignTerminal ? ['EXTERNAL B FINISHED']
        : ['EXTERNAL B FINISHED', 'A STOP']);
} else {
    assert.strictEqual(statuses.script.task_claim_id, replacementClaim);
    assert.strictEqual(observed.state, 'accepted');
    assert.strictEqual(el('script-logs').innerText, 'EXTERNAL B LIVE');
    assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
    assertBusy();
    assert.deepStrictEqual(ends, ['A STOP']);
    statuses.script = finished;
    for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
    assert.deepStrictEqual(ends, ['A STOP', 'EXTERNAL B FINISHED']);
}
assert.strictEqual(observed.state, 'delivered');
assert.strictEqual(el('script-logs').innerText, 'EXTERNAL B FINISHED');
assert.strictEqual(gen.disabled, false);
for (const id of ['btn-cancel-script', 'btn-pause-script', 'btn-snapshot-script']) {
    assert.strictEqual(el(id).style.display, 'none');
}
assert(!run("taskStartClaims.has('script')"));
"""


GENERATE_ACK_BEFORE_EXTERNAL_REATTACH = STOP_BEFORE_GENERATE_ACK_SETUP + r"""
const external = {...live, task_claim_id: replacementClaim, start_time: 202, logs: ['EXTERNAL B LIVE']};
if (!DUPLICATE_STOP) { statuses.script = external; }
originalAck.yes({});
await generate;
await settle();
assert.strictEqual(original.claimId, originalClaim);
if (DUPLICATE_STOP) {
    // The ordinary log poll finishes A before the held Start Over read returns.
    assert.strictEqual(original.state, 'delivered');
    assert.deepStrictEqual(ends, ['A STOP']);
} else {
    assert.strictEqual(original.state, 'superseded');
    assert.deepStrictEqual(ends, []);
}

statuses.script = external;
await context.reattachRunningPollers();
await settle();
const observed = context.getScriptAdmissionByToken(context._scriptRecoveryAcceptedRun);
assert.strictEqual(observed.claimId, replacementClaim);
assert.strictEqual(observed.state, 'accepted');
assert.strictEqual(el('script-logs').innerText, 'EXTERNAL B LIVE');
assertBusy();

context.reattachTaskActivity('script', [], 'script-logs', null, {...external, ownership_unconfirmed: true});
await settle();
assert(run("taskStartClaims.has('script')"));
assert.deepStrictEqual(effects.releases, []);
const finishExternal = async () => {
    statuses.script = {...done, start_time: 202, logs: ['EXTERNAL B FINISHED']};
    for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
};
if (EXTERNAL_FINISHED) { await finishExternal(); }

originalStop.yes(stoppedStatus);
await restart;
await settle();
assert.deepStrictEqual(JSON.parse(JSON.stringify(effects.posts)), [
    ['/api/generate_script', {first_person_narrator: null, start_over: false}],
    ['/api/generate_script/cancel', {expected_claim_id: originalClaim}],
]);
assert.strictEqual(original.state, 'delivered');
assert.strictEqual(context._scriptRecoveryAcceptedRun, observed.token);
assert.strictEqual(observed.state, EXTERNAL_FINISHED ? 'delivered' : 'accepted');
if (EXTERNAL_FINISHED) {
    assert.strictEqual(el('script-logs').innerText, 'EXTERNAL B FINISHED');
    assert.strictEqual(gen.disabled, false);
    assert(!run("taskStartClaims.has('script')"));
} else {
    assertBusy();
    assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
    assert.strictEqual(el('script-logs').innerText, 'EXTERNAL B LIVE');
    assert(run("taskStartClaims.has('script')"), 'late A completion must leave the B claim held');
    assert.deepStrictEqual(effects.releases, []);
    assert.deepStrictEqual(ends, ['A STOP']);
    await finishExternal();
}
assert.strictEqual(observed.state, 'delivered');
assert.strictEqual(el('script-logs').innerText, 'EXTERNAL B FINISHED');
assert.strictEqual(gen.disabled, false);
for (const id of ['btn-cancel-script', 'btn-pause-script', 'btn-snapshot-script']) {
    assert.strictEqual(el(id).style.display, 'none');
}
assert.deepStrictEqual(effects.releases, ['script']);
assert(!run("taskStartClaims.has('script')"));
assert.deepStrictEqual(ends, EXTERNAL_FINISHED && !DUPLICATE_STOP
    ? ['EXTERNAL B FINISHED', 'A STOP'] : ['A STOP', 'EXTERNAL B FINISHED']);
"""


RESTART_REFUSES_UNVERIFIED_OWNER = r"""
const notes = [];
context.showToast = (message, type) => notes.push([message, type]);
statuses = {script: {...live, logs: ['A LIVE']}};
await gen.listeners.click();
await settle();
const accepted = context._scriptRecoveryAcceptedRun;
const notConfirmed = {
    ...live,
    task_claim_id: ISSUE === 'changed' ? 'b'.repeat(32)
        : ISSUE === 'missing' ? undefined : live.task_claim_id,
    ownership_unconfirmed: ISSUE === 'unconfirmed',
};
if (!CHANGE_DURING_CONSENT) { statuses.script = notConfirmed; }
let confirmations = 0;
context.showConfirm = async () => {
    confirmations++;
    if (CHANGE_DURING_CONSENT) { statuses.script = notConfirmed; }
    return true;
};
context.API.post = async (url, body) => {
    effects.posts.push([url, body]);
    throw Object.assign(Error('Unexpected POST'), {status: 409});
};
await fresh.listeners.click();
await forwarded;
await settle();
assert.strictEqual(confirmations, 1);
assert.deepStrictEqual(effects.posts.map(([url]) => url), ['/api/generate_script']);
assert.strictEqual(notes.length, 1);
assert.strictEqual(notes[0][1], 'warning');
assert.match(notes[0][0], /Check the Script task state|has not stopped yet/);
assert.strictEqual(context._scriptRecoveryAcceptedRun, accepted);
assert.strictEqual(el('script-logs').innerText, 'A LIVE');
assert.strictEqual(el('btn-snapshot-script').style.display, 'inline-block');
assert.deepStrictEqual(jobs(), []);
assertBusy();
"""


class ScriptRunOwnershipTests(unittest.TestCase):
    def run_schedule(self, code):
        script = SETUP + r"""
const jobs = () => effects.notifications.filter(notice => notice[0] === 'job');
context.showActionError = (message, error) => effects.notifications.push(['action', message, error.message]);
const assertBusy = () => {
    assert.strictEqual(gen.disabled, true, 'Generate stays disabled while its run is active');
    assert.strictEqual(context._scriptRecoveryRunActive, true);
    assert.strictEqual(el('btn-cancel-script').style.display, 'inline-block');
    assert.strictEqual(el('btn-pause-script').style.display, 'inline-block');
};
(async () => {
""" + code + r"""
    assert.deepStrictEqual(unhandled, []);
    console.log('Schedule finished');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            ['node', '-e', script, str(CORE), str(WORKBENCH)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('', result.stderr)
        self.assertIn('Schedule finished', result.stdout, result.stderr)
        return result.stdout

    def test_retry_ack_first_stale_observer_cannot_finish_third_run(self):
        self.run_schedule(ACK_ORDER.replace('FIRST', "'retry'", 1))

    def test_generate_ack_first_stale_observer_cannot_finish_third_run(self):
        self.run_schedule(ACK_ORDER.replace('FIRST', "'generate'", 1))

    def test_refused_generate_observer_reports_its_own_completion(self):
        for terminal_first in ('false', 'true'):
            with self.subTest(terminal_first=terminal_first):
                self.run_schedule(f'const TERMINAL_FIRST = {terminal_first};\n'
                                  + OBSERVED_AFTER_REFUSAL)

    def test_pre_ack_observer_cannot_finish_acknowledged_generate(self):
        for unconfirmed in ('false', 'true'):
            with self.subTest(ownership_unconfirmed=unconfirmed):
                self.run_schedule(f'const OBSERVER_UNCONFIRMED = {unconfirmed};\n' + PRE_ACK_STATUS)

    def test_refused_generate_keeps_observed_run_until_its_terminal(self):
        for unconfirmed in ('false', 'true'):
            for change_selection in ('false', 'true'):
                with self.subTest(ownership_unconfirmed=unconfirmed, change_selection=change_selection):
                    self.run_schedule(f'const OBSERVER_UNCONFIRMED = {unconfirmed};\n'
                                      f'const CHANGE_SELECTION = {change_selection};\n'
                                      + REPLACEMENT_AFTER_REFUSAL)

    def test_direct_start_over_delivers_confirmed_stop_before_replacement(self):
        self.run_schedule(DIRECT_START_OVER)

    def test_direct_start_over_retains_stop_log_when_replacement_is_refused(self):
        self.run_schedule(REFUSED_DIRECT_RESTART)

    def test_overlapping_confirmations_send_one_cancel_and_one_replacement(self):
        for second_after_replacement in ('false', 'true'):
            with self.subTest(second_after_replacement=second_after_replacement):
                self.run_schedule(f'const SECOND_AFTER_REPLACEMENT = {second_after_replacement};\n'
                                  + OVERLAPPING_DIRECT_RESTARTS)

    def test_reload_consent_survives_reattachment_to_the_same_run(self):
        for phase in ('consent', 'cancel', 'stop'):
            with self.subTest(phase=phase):
                self.run_schedule(f"const CHANGE = '', REATTACH = '{phase}';\n" + RELOAD_RESTART_CONSENT)

    def test_reload_consent_cannot_cancel_a_new_run_or_changed_selection(self):
        for change in ('run', 'book', 'selection'):
            with self.subTest(change=change):
                self.run_schedule(f'const CHANGE = "{change}", REATTACH = "consent";\n' + RELOAD_RESTART_CONSENT)

    def test_reload_restart_blocks_stale_stop_during_cancel_or_poll(self):
        for phase, change in (('cancel', 'run'), ('stop', 'run'), ('stop', 'book')):
            with self.subTest(phase=phase, change=change):
                self.run_schedule(f'const CHANGE = "{change}", REATTACH = "{phase}";\n'
                                  + RELOAD_RESTART_CONSENT)

    def test_queued_start_over_waits_for_stop_and_delivers_both_runs(self):
        self.run_schedule('const STOP_CONFIRMED = true, INITIAL_STATUS = live, '
                          'ADMITTED_CLAIM = live.task_claim_id;\n' + QUEUED_START_OVER)

    def test_queued_start_over_keeps_controls_if_stop_is_unconfirmed(self):
        for status in ('live', '{...done, ownership_unconfirmed: true, task_claim_id: live.task_claim_id}'):
            with self.subTest(status=status):
                self.run_schedule(f'const STOP_CONFIRMED = false, INITIAL_STATUS = {status}, '
                                  'ADMITTED_CLAIM = live.task_claim_id;\n'
                                  + QUEUED_START_OVER)

    def test_queued_start_over_cannot_cancel_an_unproven_owner(self):
        for claim in ('undefined', "'b'.repeat(32)"):
            with self.subTest(claim=claim):
                self.run_schedule('const STOP_CONFIRMED = false, INITIAL_STATUS = live, '
                                  f'ADMITTED_CLAIM = {claim};\n' + QUEUED_START_OVER)

    def test_live_reattachment_replaces_old_owner_during_restart(self):
        for path in ('direct', 'queued'):
            with self.subTest(path=path):
                self.run_schedule(f"const RESTART = '{path}';\n" + RESTART_SEES_NEW_OWNER)

    def test_start_over_remembers_confirmed_stop_until_generate_acknowledges(self):
        self.run_schedule(STOP_BEFORE_GENERATE_ACK)

    def test_queued_restart_rechecks_the_identity_of_a_saved_stop(self):
        self.run_schedule(SAVED_STOP_BEFORE_FOREIGN_TERMINAL)

    def test_external_owner_invalidates_saved_restart_before_original_ack(self):
        for timing in ('ack_only', 'stop_response', 'stop_and_ack'):
            for finished in ('false', 'true'):
                with self.subTest(timing=timing, external_finished=finished):
                    hold = 'false' if timing == 'ack_only' else 'true'
                    ack_first = 'true' if timing == 'stop_and_ack' else 'false'
                    self.run_schedule(f'const EXTERNAL_FINISHED = {finished}, HOLD_STOP_RESPONSE = {hold}, '
                                      f'ACK_BEFORE_STOP_RESPONSE = {ack_first};\n'
                                      + EXTERNAL_OWNER_BEFORE_GENERATE_ACK)

    def test_same_owner_can_reattach_while_its_stop_response_is_pending(self):
        self.run_schedule('const HOLD_STOP_RESPONSE = true;\n' + SAME_CLAIM_DURING_STOP)

    def test_a_different_terminal_snapshot_cannot_complete_the_old_admission(self):
        self.run_schedule(
            'const EXTERNAL_FINISHED = true, HOLD_STOP_RESPONSE = true, '
            'ACK_BEFORE_STOP_RESPONSE = false, FOREIGN_TERMINAL = true;\n'
            + EXTERNAL_OWNER_BEFORE_GENERATE_ACK)

    def test_late_stop_completes_an_acknowledged_owner_after_reattachment(self):
        for finished in ('false', 'true'):
            for duplicate in ('false', 'true'):
                with self.subTest(external_finished=finished, duplicate_stop=duplicate):
                    self.run_schedule(f'const HOLD_STOP_RESPONSE = true, EXTERNAL_FINISHED = {finished}, '
                                      f'DUPLICATE_STOP = {duplicate};\n'
                                      + GENERATE_ACK_BEFORE_EXTERNAL_REATTACH)

    def test_confirmed_restart_refuses_an_unverified_owner_once(self):
        for issue in ('changed', 'missing', 'unconfirmed'):
            for during_consent in ('false', 'true'):
                with self.subTest(issue=issue, during_consent=during_consent):
                    self.run_schedule(f"const ISSUE = '{issue}', CHANGE_DURING_CONSENT = {during_consent};\n"
                                      + RESTART_REFUSES_UNVERIFIED_OWNER)

    def test_review_and_nicknames_report_independent_completions(self):
        self.run_schedule(r"""
statuses = {script: done, review: live, nicknames: live};
await context.reattachRunningPollers();
await settle();
statuses.review = {...done, logs: ['REVIEW DONE']};
statuses.nicknames = {...done, logs: ['NICKNAMES DONE']};
for (let i = 0; timers.length && i < 16; i++) { await tick(); await settle(); }
assert.deepStrictEqual(jobs().map(notice => notice[1]).sort(), ['nicknames', 'review']);
""")


if __name__ == '__main__':
    unittest.main()
