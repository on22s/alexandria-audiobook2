"""Manual feedback follows the current request, not old errors or responses."""
from pathlib import Path
import subprocess
import unittest


STATIC = Path(__file__).resolve().parent.parent / 'static'
SETUP = r'''
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const elements = new Map();
function element(id) {
    const node = {id, value: '', textContent: '', disabled: false, hidden: true,
        children: [], attributes: {}, listeners: {}, removed: false,
        appendChild(child) {this.children.push(child);},
        append(...children) {this.children.push(...children);},
        setAttribute(name, value) {this.attributes[name] = value;},
        addEventListener(name, callback) {this.listeners[name] = callback;},
        remove() {this.removed = true; elements.delete(this.id);}
    };
    elements.set(id, node);
    return node;
}
for (const id of ['manual-llm-panel', 'manual-llm-title', 'manual-llm-hint',
    'manual-llm-prompt', 'manual-llm-reply', 'manual-llm-submit',
    'manual-llm-status', 'manual-llm-retained-replies', 'toast-container']) {
    element(id);
}
elements.get('toast-container').insertAdjacentHTML = (position, html) => {
    assert.equal(position, 'beforeend');
    const toast = element(/id="([^"]+)"/.exec(html)[1]);
    toast.html = html;
};
const toastInstances = new Map();
class Toast {
    constructor(node) {this.node = node; toastInstances.set(node, this);}
    show() {this.node.shown = true;}
    dispose() {this.disposed = true;}
    static getInstance(node) {return toastInstances.get(node);}
}
let pending;
const context = {
    window: {}, document: {getElementById: id => elements.get(id), createElement: element},
    console: {error() {}}, bootstrap: {Toast}, Date,
    t: (key, fallback) => fallback, escapeHtml: text => text,
    notifyJobDone() {}, API: {
        get: async () => ({pending}), post: async () => ({accepted: true})
    }
};
vm.createContext(context);
function section(start, end) {
    const offset = source.indexOf(start), finish = source.indexOf(end, offset);
    assert(offset >= 0 && finish > offset, start);
    return source.slice(offset, finish);
}
vm.runInContext(section('let toastSequence =', 'function showActionError(') +
    section('let _manualShown =', 'let finalAudioRequest ='), context);
async function request(id, sequence, hint = '') {
    pending = {id, sequence, stage_hint: hint, messages: []};
    await context.renderManualRequest({running: true, manual_request: {id}}, 'review');
    elements.get('manual-llm-reply').value = 'synthetic valid reply';
}
function visibleToasts() {
    return [...elements.values()].filter(node => node.shown && !node.removed);
}
function assertSuccess() {
    assert.equal(elements.get('manual-llm-hint').textContent, '');
    assert.match(elements.get('manual-llm-status').textContent, /sent.*pipeline will validate/);
    assert(!visibleToasts().some(node => node.html.includes('role="alert"')));
    assert(visibleToasts().some(node => node.html.includes('bg-success') &&
        node.html.includes('role="status"') && node.html.includes('aria-live="polite"')));
}
function deferredPost() {
    let resolve, reject;
    context.API.post = () => new Promise((yes, no) => {resolve = yes; reject = no;});
    return {resolve: () => resolve({accepted: true}), reject: () => reject(Error('offline'))};
}
'''


class ManualReplyFeedbackTests(unittest.TestCase):
    def run_js(self, body):
        script = SETUP + r'''
let finished = false;
process.on('beforeExit', () => assert(finished, 'all async assertions must finish'));
(async () => {
''' + body + r'''
})().then(() => {finished = true;}).catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run(
            ['node', '-e', script, str(STATIC / 'js/app-core.js')],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_validation_hint_and_submission_error_clear_on_success(self):
        self.run_js(r'''
await request('retry', 2, 'Warning: synthetic invalid entries');
context.API.post = async () => {throw Error('offline');};
await context.submitManualReply();
assert(visibleToasts().some(node => node.html.includes('role="alert"')));
context.showToast('Unrelated task failure', 'error');
context.API.post = async () => ({accepted: true});
await context.submitManualReply();
const unrelated = visibleToasts().find(node => node.html.includes('Unrelated task failure'));
assert(unrelated, 'must preserve unrelated feedback');
unrelated.remove();
assertSuccess();
assert.equal(elements.get('manual-llm-reply').value, 'synthetic valid reply');
''')

    def test_success_is_replaced_by_new_error(self):
        self.run_js(r'''
await request('first', 1);
await context.submitManualReply();
await request('retry', 2, 'Warning: new invalid entries');
context.API.post = async () => {throw Error('offline');};
await context.submitManualReply();
assert.match(elements.get('manual-llm-status').textContent, /Could not confirm/);
assert.match(elements.get('manual-llm-hint').textContent, /new invalid entries/);
assert(visibleToasts().some(node => node.html.includes('role="alert"')));
assert(!visibleToasts().some(node => node.html.includes('bg-success')));
assert.equal(elements.get('manual-llm-submit').disabled, false);
''')

    def test_late_failure_cannot_replace_newer_success(self):
        self.run_js(r'''
await request('old', 1);
const old = deferredPost();
const submitted = context.submitManualReply();
await request('new', 2);
context.API.post = async () => ({accepted: true});
await context.submitManualReply();
assertSuccess();
old.reject(); await submitted;
assertSuccess();
assert.equal(elements.get('manual-llm-submit').disabled, true);
''')

    def test_late_success_cannot_replace_newer_error(self):
        self.run_js(r'''
await request('old', 1);
const old = deferredPost();
const submitted = context.submitManualReply();
await request('new', 2, 'Warning: current invalid entries');
context.API.post = async () => {throw Error('current failure');};
await context.submitManualReply();
old.resolve(); await submitted;
assert.match(elements.get('manual-llm-status').textContent, /Could not confirm/);
assert.match(elements.get('manual-llm-hint').textContent, /current invalid entries/);
assert(visibleToasts().some(node => node.html.includes('role="alert"')));
assert(!visibleToasts().some(node => node.html.includes('bg-success')));
assert.equal(elements.get('manual-llm-reply').disabled, false);
''')

    def test_old_finally_cannot_unlock_current_submission(self):
        self.run_js(r'''
await request('old', 1);
const old = deferredPost();
const oldSubmit = context.submitManualReply();
await request('new', 2);
const current = deferredPost();
const currentSubmit = context.submitManualReply();
old.resolve(); await oldSubmit;
let duplicates = 0;
context.API.post = async () => {duplicates++;};
await context.submitManualReply();
assert.equal(duplicates, 0, 'old completion must not release current request lock');
current.resolve(); await currentSubmit;
assertSuccess();
''')

    def test_completion_invalidates_late_success(self):
        self.run_js(r'''
await request('old', 1, 'Warning: old validation');
const old = deferredPost();
const submitted = context.submitManualReply();
await context.renderManualRequest({running: false}, 'review');
old.resolve(); await submitted;
assert.match(elements.get('manual-llm-status').textContent, /no longer waiting/);
assert.equal(elements.get('manual-llm-hint').textContent, '');
assert.equal(visibleToasts().length, 0);
''')
