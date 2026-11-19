"""Execute checkpoint visibility and confirmation with the real vanilla JS."""
from pathlib import Path
import json
import subprocess
import unittest

STATIC = Path(__file__).resolve().parents[1] / 'static'


class ReviewStartOverUiTests(unittest.TestCase):
    def test_post_discard_errors_reach_review_log_and_toast_in_all_locales(self):
        source = (STATIC / 'js' / 'app-core.js').read_text(encoding='utf-8')
        formatters = source[source.index('        function showActionError('):
                            source.index('        function showConfirm(')]
        handlers = []
        for name in ('startReview', 'startContextualReview'):
            start = source.index('        async function ' + name + '(')
            handlers.append(source[start:source.index('\n        }', start) + 10])
        catalogs = {locale: json.loads((STATIC / 'i18n' / f'ui.{locale}.json').read_text(encoding='utf-8'))
                    for locale in ('en', 'zh-CN', 'ja', 'es-419')}
        harness = r'''
const assert = require('assert');
let catalog, refusal, toast, done;
const translateUiMessage = (key, fallback) => catalog[key] || fallback;
const t = (key, fallback, values = {}) => Object.entries(values).reduce(
 (text, [name, value]) => text.replaceAll('{' + name + '}', value), translateUiMessage(key, fallback));
const showToast = text => { toast = text; };
const _onReviewDone = state => { done = state; };
const confirmIfRemote = async () => true;
const _disableReviewButtons = () => {};
const _showReviewControls = () => {};
const _isReviewDedupeChecked = () => false;
const _isReviewForceChecked = () => false;
const applyReviewContextWindow = () => 4;
const getReviewContextMode = () => 'entries';
const getReviewContextValue = () => 4;
const document = {getElementById: () => ({value: 4})};
const API = {post: async () => { throw refusal; }};
'''
        checks = r'''
(async () => {
 for (const messages of Object.values(catalogs)) {
  catalog = messages;
  for (const key of ['error.review.restart_reservation_lost',
                    'error.review.restart_receipt_discard_failed',
                    'error.review.restart_start_failed', 'error.review.restart_discard_failed']) {
   assert.equal(typeof catalog[key], 'string', key);
   refusal = Object.assign(new Error(catalogs.en[key]), {status: 409, messageKey: key});
   for (const start of [startReview, startContextualReview]) {
    toast = done = null;
    await start();
    assert.equal(done.status, 'failed');
    assert.equal(done.logs[0], catalog[key]);
    assert(toast.includes(catalog[key]));
   }
  }
 }
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        code = 'const catalogs = ' + json.dumps(catalogs, ensure_ascii=True) + ';\n'
        result = subprocess.run(['node'], input=code + harness + formatters + '\n'.join(handlers) + checks,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_checkpoint_visibility_and_confirmation_are_required_in_each_mode(self):
        source = (STATIC / 'js' / 'app-core.js').read_text(encoding='utf-8')
        start = source.index('        let reviewRestartSnapshot =')
        end = source.index('        function _showReviewControls', start)
        harness = r'''
const assert = require('assert');
let currentBookFilename = 'synthetic.txt';
let selected = [];
const nodes = new Map();
const document = {
 getElementById(id) { if (!nodes.has(id)) { nodes.set(id, {style: {}, checked: false, disabled: false}); } return nodes.get(id); },
 querySelectorAll() { return selected.map(name => ({dataset: {name}})); }
};
let data = {checkpoints: [], book_token: 'a'.repeat(64)};
const API = {get: async () => data};
const t = (_key, fallback) => fallback;
let confirmed = false, confirmationCount = 0, onConfirm = () => {};
const showConfirm = async (text, options) => {
 assert(text.includes('script') && text.includes('edits') && text.includes('batch 1'));
 assert.equal(options.danger, true); assert.equal(options.actionLabel, 'Start over');
 confirmationCount++; onConfirm(); return confirmed;
};
const showActionError = () => {};
'''
        checks = r'''
(async () => {
 await refreshReviewRestart();
 assert.equal(document.getElementById('btn-review-fresh').style.display, 'none');
 assert.equal(await confirmReviewRestart(), null);
 assert.equal(confirmationCount, 0);
 data.checkpoints = [{book: '(active script)'}];
 await refreshReviewRestart();
 assert.notEqual(document.getElementById('btn-review-fresh').style.display, 'none');
 assert.notEqual(document.getElementById('btn-review-contextual-fresh').style.display, 'none');
 document.getElementById('btn-review-script').disabled = true;
 syncReviewRestartButtons();
 assert.equal(document.getElementById('btn-review-fresh').style.display, 'none');
 document.getElementById('btn-review-script').disabled = false;
 syncReviewRestartButtons();
 assert.equal(await confirmReviewRestart(), null);
 confirmed = true;
 assert.deepEqual(await confirmReviewRestart(), {start_over: true, book_token: data.book_token});
 onConfirm = () => { currentBookFilename = 'changed.txt'; };
 await assert.rejects(confirmReviewRestart(), /changed/);
 onConfirm = () => {};
 document.getElementById('review-batch-mode').checked = true;
 selected = ['saved'];
 await refreshReviewRestart();
 assert.equal(document.getElementById('btn-review-batch-fresh').style.display, 'none');
 data.checkpoints = [{book: 'saved'}];
 await refreshReviewRestart();
 assert.notEqual(document.getElementById('btn-review-batch-fresh').style.display, 'none');
 assert.equal(document.getElementById('btn-review-fresh').style.display, 'none');
 assert.equal((await confirmReviewRestart()).book_token, data.book_token);
 onConfirm = () => { selected = ['other']; };
 await assert.rejects(confirmReviewRestart(), /changed/);
 data.checkpoints = [];
 await refreshReviewRestart();
 assert.equal(document.getElementById('btn-review-batch-fresh').style.display, 'none');
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run(['node', '-e', harness + source[start:end] + checks],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_failure_message_reaches_the_panel_and_only_resume_refusal_offers_start_over(self):
        """A fatal review's first-line message must be the panel text; only the
        resume refusal may reveal the existing Start over control."""
        source = (STATIC / 'js' / 'app-core.js').read_text(encoding='utf-8')
        failed = source[source.index('        function isTaskFailed(status) {'):
                        source.index('        // --- Desktop notifications ---')]
        done = source[source.index('        function _onReviewDone(status) {'):
                      source.index('        async function startReview(')]
        harness = r'''
const assert = require('assert');
const nodes = new Map();
const document = {getElementById(id) {
  if (!nodes.has(id)) { nodes.set(id, {style: {}, innerHTML: '', textContent: '', title: ''}); }
  return nodes.get(id);
}};
const escapeHtml = String;
const t = (key, fallback, values = {}) => Object.entries(values).reduce(
  (text, [name, value]) => text.replaceAll('{' + name + '}', value), fallback);
const refreshReviewRestart = () => {};
const _showReviewControls = () => {};
const _disableReviewButtons = () => {};
'''
        checks = r'''
const resume = 'Saved checkpoint no longer matches this batching.';
const panel = document.getElementById('review-recovery-panel');
const fresh = document.getElementById('btn-review-contextual-fresh');
const plainFresh = document.getElementById('btn-review-fresh');
_onReviewDone({status: 'failed', logs: ['Task review failed with return code 1.'],
  failure_message: resume, failure_kind: 'resume_misaligned'});
assert.equal(panel.style.display, '');
assert(panel.innerHTML.includes(resume), 'the failure message, not the generic last log, is shown');
assert.equal(fresh.style.display, '', 'the kind offers Start over even with unrelated wording');
assert.equal(plainFresh.style.display, '', 'the plain review Start over is offered too');

panel.innerHTML = ''; fresh.style.display = 'none'; plainFresh.style.display = 'none';
_onReviewDone({status: 'failed', logs: ['Task review failed with return code 1.'],
  failure_message: 'Please start the review over: checkpoint write failed'});
assert(panel.innerHTML.includes('checkpoint write failed'));
assert.equal(fresh.style.display, 'none', 'old English wording alone must not offer Start over');
assert.equal(plainFresh.style.display, 'none');

panel.innerHTML = '';
_onReviewDone({status: 'failed', logs: ['Task review failed with return code 1.'],
  failure_message: 'disk full', failure_kind: 'write_failed'});
assert.equal(fresh.style.display, 'none', 'an unrelated kind must not offer Start over');
panel.innerHTML = '';
_onReviewDone({status: 'failed', logs: ['Task review failed with return code 1.']});
assert(panel.innerHTML.includes('Task review failed with return code 1.'), 'the last log remains the fallback');
'''
        result = subprocess.run(['node', '-e', harness + failed + '\n' + done + '\n' + checks],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_batch_status_row_shows_each_book_failure_message(self):
        """Per-book fatal messages are plain text in that book's status row."""
        source = (STATIC / 'js' / 'app-core.js').read_text(encoding='utf-8')
        body = source[source.index('        function pollReviewBatch() {'):
                      source.index('        // --- Persona Generation ---')]
        harness = r'''
const assert = require('assert');
let tick;
const statusCell = {innerHTML: '', title: '', insertAdjacentText(position, text) { this.innerHTML += text; }};
const document = {getElementById: id => id === 'rb-status-book' ? statusCell : {style: {}}, querySelector: () => ({id: 'rb-check-book'})};
const CSS = {escape: value => value};
const createTaskLogRenderer = () => () => {};
const _startPolling = (task, fetch, options) => { tick = options.onTick; };
const syncPauseButton = () => {};
const _formatBookStats = () => 'stats';
const _updateReviewBatchTotals = () => {};
const _showTaskRecoveryPanel = () => {};
const notifyJobDone = () => {};
const taskStatusLabel = status => status;
const escapeHtml = String;
const t = (key, fallback) => fallback;
'''
        checks = r'''
pollReviewBatch();
tick({running: false, tasks: [{name: 'book', status: 'failed', failure_message: 'Start over: checkpoint mismatch'}]});
assert(statusCell.innerHTML.includes('Start over: checkpoint mismatch'), 'the book failure message is visible in its row');
assert(statusCell.title.includes('Start over: checkpoint mismatch'), 'and in the row tooltip');
'''
        result = subprocess.run(['node', '-e', harness + body + '\n' + checks],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_restart_translations_survive_utf8_round_trip(self):
        for locale, expected in [('en', 'Start over'), ('zh-CN', '\u91cd\u65b0\u5f00\u59cb'),
                                 ('ja', '\u6700\u521d\u304b\u3089\u3084\u308a\u76f4\u3059'),
                                 ('es-419', 'Empezar de nuevo')]:
            with self.subTest(locale=locale):
                catalog = json.loads((STATIC / 'i18n' / f'ui.{locale}.json').read_text(encoding='utf-8'))
                self.assertEqual(expected, catalog['script.review.start-over'])
                self.assertTrue(catalog['script.review.restart-confirm'].strip())
