"""Deterministic production-handler schedule matrix for script recovery lifecycle."""
import itertools
import json
from pathlib import Path
import subprocess
import unittest

import tests.test_script_recovery_read_feedback_js as feedback
from tests import test_script_recovery_read_feedback_js as feedback

MATRIX = [
    ('initiator', ['generate', 'retry', 'startover', 'hydrate', 'observer']),
    ('state', ['idle', 'provisional', 'accepted', 'delivered', 'restoring']),
    ('order', ['ack-first', 'status-first', 'terminal-first', 'detail-first']),
    ('outcome', ['success', 'conflict', 'transport', 'throw', 'reject']),
    ('selection', ['same', 'AB', 'ABA', 'samename', 'declined']),
    ('action', ['none', 'pause', 'cancel', 'queued']),
    ('identity', ['match', 'mismatch', 'absent']),
]

NAMED = [
    ('accepted-retry-then-generate-reject', dict(initiator='retry', state='accepted', outcome='conflict', order='ack-first', selection='same')),
    ('generate-reject-then-accepted-retry', dict(initiator='retry', state='provisional', outcome='conflict', order='status-first', selection='same')),
    ('hydration-pre-ack', dict(initiator='hydrate', state='provisional', order='terminal-first', selection='same')),
    ('hydration-post-ack', dict(initiator='hydrate', state='accepted', order='status-first', selection='same')),
    ('hydration-post-terminal', dict(initiator='hydrate', state='delivered', order='terminal-first', selection='same')),
    ('duplicate-generate', dict(initiator='generate', state='provisional', order='ack-first', selection='same', action='queued')),
    ('queued-restart-delayed-ack', dict(initiator='startover', state='provisional', order='status-first', selection='same', action='queued')),
    ('lone-unconfirmed-observer', dict(initiator='observer', state='accepted', order='terminal-first', selection='same')),
    ('multiple-observers-terminal-orders', dict(initiator='observer', state='accepted', order='detail-first', selection='same')),
    ('delayed-live-after-terminal', dict(initiator='hydrate', state='delivered', order='detail-first', selection='same')),
    ('restoration-detail-barrier', dict(initiator='retry', state='restoring', order='detail-first', selection='same')),
    ('throwing-cleanup', dict(initiator='observer', state='accepted', order='terminal-first', selection='same', outcome='throw')),
    ('selection-crossing-success', dict(initiator='generate', state='accepted', order='ack-first', selection='AB')),
    ('selection-crossing-error', dict(initiator='generate', state='provisional', order='status-first', selection='ABA', outcome='conflict')),
    ('unconfirmed-competing-admission', dict(initiator='observer', state='provisional', order='terminal-first', selection='samename')),
    ('declined-switch-during-ack', dict(initiator='generate', state='provisional', order='ack-first', selection='declined')),
]


def build_rows():
    rows = []
    defaults = {name: values[0] for name, values in MATRIX}
    for name, row in NAMED:
        rows.append(dict(defaults, **row, name=name))
    for (left, left_values), (right, right_values) in itertools.combinations(MATRIX, 2):
        for a, b in zip(left_values, right_values):
            row = dict(defaults, name=f'{left}:{a}|{right}:{b}')
            row[left] = a
            row[right] = b
            rows.append(row)
    for index, (name, values) in enumerate(MATRIX):
        for value in values:
            row = dict(defaults, name=f'{name}:{value}')
            row[name] = value
            rows.append(row)
    for target in (5, 4, 3, 2, 1):
        for a, b, c in itertools.product(
                MATRIX[0][1][:target], MATRIX[2][1][:target], MATRIX[4][1][:target]):
            row = dict(defaults, name=f'3way:{a}:{b}:{c}', initiator=a, order=b, selection=c)
            rows.append(row)
    unique, seen = [], set()
    for row in rows:
        key = tuple(row[name] for name, _ in MATRIX)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    covered = {tuple(row[name] for name, _ in MATRIX) for row in unique}
    for (left, left_values), (right, right_values) in itertools.combinations(MATRIX, 2):
        for a, b in itertools.product(left_values, right_values):
            needed = False
            for row in unique:
                if row[left] == a and row[right] == b:
                    needed = True
                    break
            if needed:
                continue
            row = dict(defaults, name=f'pair:{left}:{a}|{right}:{b}')
            row[left] = a
            row[right] = b
            unique.append(row)
    return unique


ROWS = build_rows()


class ScriptRecoveryMatrixTests(unittest.TestCase):
    def test_matrix(self):
        results = []
        for row in ROWS:
            results.append(self._run_row(row))
        target = Path.home() / '.cache/aa2-2241-matrix'
        target.mkdir(parents=True, exist_ok=True)
        artifact = target / 'results.json'
        artifact.write_text(json.dumps({
            'rows': len(ROWS),
            'passes': sum(1 for result in results if result['ok']),
            'rows_detail': results,
            'failures': [result for result in results if not result['ok']],
        }, indent=2), encoding='utf-8')
        self.assertTrue(all(result['ok'] for result in results),
                        f"matrix failures: {[r['name'] for r in results if not r['ok']]}")
        self.assertGreaterEqual(len(ROWS), 111)

    def test_selection_fence_bundle_mutation(self):
        source = Path(feedback.SOURCE).read_text(encoding='utf-8')
        bundle = [
            ('function scriptRecoveryContextMatches(context) {',
             'function scriptRecoveryContextMatches(context) { return true;'),
            ('function scriptRecoverySelectionMatches(selection) {',
             'function scriptRecoverySelectionMatches(selection) { return true;'),
            ('function isScriptRecoveryDetailCurrent(detail, book, generation) {',
             'function isScriptRecoveryDetailCurrent(detail, book, generation) { return true;'),
            ('current: () => scriptRecoveryContextMatches(context) && window._scriptRecoveryGeneration === generation,',
             'current: () => true,'),
        ]
        target = Path.home() / '.cache/aa2-2241-matrix'
        mutant = target / 'mutant-selection-bundle.js'
        mutated = source
        for original, replacement in bundle:
            self.assertIn(original, mutated)
            mutated = mutated.replace(original, replacement, 1)
        mutant.write_text(mutated, encoding='utf-8')
        original_source = feedback.SOURCE
        feedback.SOURCE = mutant
        try:
            result = unittest.TestResult()
            unittest.TestSuite([feedback.ScriptRecoveryReadFeedbackJsTests(
                'test_accepted_selector_switch_invalidates_a_held_detail_read')]).run(result)
            self.assertTrue(result.failures or result.errors,
                            'selection fence bundle mutation was not detected')
        finally:
            feedback.SOURCE = original_source

    def test_mutation_detection(self):
        source = Path(feedback.SOURCE).read_text(encoding='utf-8')
        mutations = [
            ('terminal coordinator',
             'window.onScriptTaskTerminal = deliverScriptTaskTerminal;',
             'window.onScriptTaskTerminal = () => {};'),
            ('generate admission',
             "const runToken = window.reserveScriptRecoveryRun('generate');",
             "const runToken = 0;"),
        ]
        target = Path.home() / '.cache/aa2-2241-matrix'
        target.mkdir(parents=True, exist_ok=True)
        for label, original, replacement in mutations:
            self.assertIn(original, source)
            mutant = target / f'mutant-{label.split()[0]}.js'
            mutant.write_text(source.replace(original, replacement, 1), encoding='utf-8')
            original_source = feedback.SOURCE
            feedback.SOURCE = mutant
            try:
                name = {
                    'terminal': 'test_shared_poll_terminal_clears_run_and_reads_recovery_once',
                    'generate': 'test_start_over_reserves_token_before_post_ack_and_ignores_stale_terminal',
                }[label.split()[0]]
                result = unittest.TestResult()
                unittest.TestSuite([feedback.ScriptRecoveryReadFeedbackJsTests(name)]).run(result)
                self.assertTrue(result.failures or result.errors,
                                f'{label} mutation was not detected')
            finally:
                feedback.SOURCE = original_source

    def _run_row(self, row):
        expected = {
            'postAttempts': (1 if row['initiator'] in ('generate', 'startover') else
                             0 if row['initiator'] in ('hydrate', 'observer') else
                             0 if row['state'] == 'restoring' else 1),
            'observerAttachments': 0 if row['initiator'] in ('hydrate', 'observer') else 1,
            'unhandledRejections': 0,
        }
        row = dict(row, expected=expected)
        setup = r'''
const row = ROWSPEC;
const trace = [];
const mark = (kind, detail) => trace.push({n: trace.length + 1, kind,
 detail, epoch: context._bookSelectionEpoch, admission: context._scriptRecoveryAcceptedRun || 0});
const failures = [];
process.on('unhandledRejection', reason => failures.push('unhandled:' + String(reason)));
reset();
getReply = {recoverable: true, detail: detail(), failed_pass: 'segment'};
let acceptedPosts = 0, terminalEffects = 0;
context.API.get = async path => {
    mark('dispatch:get', path);
    if (path.includes('include_detail')) {
        return {recoverable: row.state === 'delivered' ? false : true, detail: detail(), failed_pass: 'segment'};
    }
    if (path === '/api/status/script') {
        return {running: row.state === 'accepted', status: 'running', logs: ['live'],
            book_token: row.identity === 'match' ? 'a'.repeat(64)
                : row.identity === 'mismatch' ? 'b'.repeat(64) : null};
    }
    return {recoverable: true};
};
const originalPoll = context.pollScriptLogs;
context.pollScriptLogs = (...args) => { mark('observer:attach', args[0]); return originalPoll(...args); };
context.API.post = async (url, data) => {
    mark('dispatch:post', url);
    posts.push({url, data});
    if (url === '/api/generate_script/cancel') { return {status: 'ok'}; }
    if (row.outcome === 'conflict') { throw Object.assign(Error('409 conflict'), {status: 409}); }
    if (row.outcome === 'transport') { throw Error('transport offline'); }
    if (row.outcome === 'throw') { throw new Error('sync throw'); }
    if (row.outcome === 'reject') { await Promise.reject(Error('async reject')); }
    return {resolution: 'accepted'};
};
const gen = element('btn-gen-script');
gen.disabled = false;
element('file-upload').files = [];
let generate;
gen.addEventListener = (name, cb) => { generate = cb; };
Object.assign(context, {getLoadedScriptSourceFilename: () => 'book-A',
    ensureScriptStartConfirmed: async () => true, _scriptStartOver: row.initiator === 'startover',
    _isStripFrontMatterChecked: () => false, _isLiteralSourcePreservingChecked: () => false});
load("document.getElementById('btn-gen-script').addEventListener('click',", '// Pause is SIGSTOP');
load('function _resetPauseBtn(', '// Same "unknown error"');
load('function _makePauseResumeHandler(', '// #600:');
load('window.cancelScript =', 'function scriptRecoveryContextMatches(');
await context.refreshScriptRecovery();
if (row.selection === 'AB' || row.selection === 'ABA' || row.selection === 'samename') {
    run('applyCurrentBookFilename("' + (row.selection === 'samename' ? 'book-A' : 'book-B') + '")');
    await flush();
    if (row.selection === 'ABA') {
        run('applyCurrentBookFilename("book-A")');
        await flush();
    }
}
if ((row.state === 'provisional' || row.state === 'accepted')
    && (row.initiator === 'hydrate' || row.initiator === 'observer')) {
    const provisional = run('reserveScriptRecoveryRun("' + row.initiator + '")');
    if (row.state === 'accepted') { run('confirmScriptRecoveryRun(' + provisional + ', "' + row.initiator + '")'); }
}
if (row.initiator === 'generate' || row.initiator === 'startover') {
    const pending = generate();
    if (row.order === 'status-first') { await context.API.get('/api/status/script'); }
    await pending;
} else if (row.initiator === 'retry') {
    const pending = context.window.retryScriptGeneration();
    if (row.order === 'detail-first') { await context.refreshScriptRecovery(); }
    await pending;
} else if (row.initiator === 'hydrate') {
    const token = run('observeScriptRecoveryRun()');
    if (token) {
        context.registerScriptTerminalCleanup(token, () => { terminalEffects++; });
        context.onScriptTaskTerminal({running: false, status: 'failed', logs: ['terminal']}, token, 'hydrate');
    }
    await flush();
} else if (row.initiator === 'observer') {
    const token = run('reserveScriptRecoveryRun("observer")');
    run('confirmScriptRecoveryRun(' + token + ', "observer")');
    context.registerScriptTerminalCleanup(token, () => { terminalEffects++; });
    for (const source of ['observer', 'live', 'observer']) {
        context.onScriptTaskTerminal({running: false, status: 'failed', logs: ['terminal']}, token, source);
    }
    await flush();
    terminalEffects += 1;
}
if (row.action === 'cancel') { await context.cancelScript(); }
if (row.action === 'pause') { await context.pauseResumeScript(); }
assert.deepStrictEqual(failures, []);
if (row.initiator === 'generate' || row.initiator === 'startover') {
 assert(posts.some(p => p.url === '/api/generate_script'), 'generate POST must be attempted');
}
if (row.initiator === 'retry' && row.state !== 'restoring'
    && row.state !== 'delivered' && row.order !== 'detail-first') {
 assert(posts.some(p => p.url === '/api/generate_script/retry'), 'retry POST must be attempted');
}
if ((row.initiator === 'generate' || row.initiator === 'startover' || row.initiator === 'retry')
    && row.outcome === 'success' && row.state !== 'delivered' && row.state !== 'restoring'
    && row.order !== 'detail-first') {
 assert(polls.length >= 1, 'accepted task must be observed');
}
assert(posts.length <= 3);
assert(polls.length <= 2);
assert(terminalEffects <= 2);
if (context._scriptRecoveryRunActive || context._scriptRecoveryRestoring) {
 assert.strictEqual(element('btn-retry-script').disabled, true);
}
'''
        result_log = """
mark('settlement:final', {posts: posts.length, polls: polls.length});
console.log('RESULT ' + JSON.stringify({trace: trace, expected: row.expected || null,
 posts: posts.length,
 postUrls: posts.map(p => p.url), polls: polls.length,
 copied: copied.length,
 runActive: !!context._scriptRecoveryRunActive,
 accepted: context._scriptRecoveryAcceptedRun || 0,
 retryDisabled: element('btn-retry-script').disabled,
 cancelDisplay: element('btn-cancel-script').style.display,
 pauseDisplay: element('btn-pause-script').style.display,
 genDisabled: element('btn-gen-script').disabled}));
"""
        scenario = setup.replace('ROWSPEC', json.dumps(row)).replace(
            'assert.deepStrictEqual(failures, []);', result_log + '\nassert.deepStrictEqual(failures, []);', 1)
        observed = {}
        case = feedback.ScriptRecoveryReadFeedbackJsTests()
        original = feedback.subprocess.run
        def capture(args, **kwargs):
            completed = original(args, **kwargs)
            for line in completed.stdout.splitlines():
                if line.startswith('RESULT '):
                    observed.update(json.loads(line[len('RESULT '):]))
            return completed
        feedback.subprocess.run = capture
        try:
            case.run_scenario(scenario)
            return dict(row, ok=True, observed=observed, error='')
        except AssertionError as error:
            return dict(row, ok=False, observed=observed, error=str(error)[:400])
        finally:
            feedback.subprocess.run = original


if __name__ == '__main__':
    unittest.main()
