"""Manual persona requests must be reachable from Voices, including after reload."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest


STATIC = Path(__file__).resolve().parent.parent / "static"


class _PanelParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.divs = []
        self.parents = []

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            attrs = dict(attrs)
            if attrs.get("id") == "manual-llm-panel":
                self.parents.append(list(self.divs))
            self.divs.append(attrs)

    def handle_endtag(self, tag):
        if tag == "div" and self.divs:
            self.divs.pop()


class ManualPersonaUITests(unittest.TestCase):
    def test_shared_panel_is_outside_hidden_tab_bodies(self):
        parser = _PanelParser()
        parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(1, len(parser.parents))
        self.assertFalse(any("tab-content" in parent.get("class", "").split()
                             for parent in parser.parents[0]), parser.parents)

    def test_persona_actions_renderer_and_reload(self):
        script = r'''
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const core = fs.readFileSync(process.argv[1], 'utf8');
const workbench = fs.readFileSync(process.argv[2], 'utf8');
const slice = (source, start, end) => source.slice(source.indexOf(start), source.indexOf(end));
const elements = {};
for (const id of ['manual-llm-panel', 'manual-llm-title', 'manual-llm-hint',
                 'manual-llm-prompt', 'manual-llm-reply', 'manual-llm-submit',
                 'voices-logs', 'script-logs', 'persona-status', 'btn-cancel-personas']) {
    elements[id] = {value: '', hidden: true, disabled: false, style: {}, textContent: ''};
}
let poll, pending, copied, failSubmit = false, runningTask = 'persona';
const posts = [], statusFetches = [];let registryFetches=0;
const context = {
    currentIsRemote:false, failoverIsRemote:false,
    window: {prompt: () => 'teen'}, console,
    document: {getElementById: id => elements[id] || null},
    API: {
        get: async path => {
            if (path === '/api/status') {registryFetches++;return {persona:{running:runningTask==='persona'},script:{running:runningTask==='script'}};}
            if (path === '/api/manual_llm/pending') { return {pending}; }
            if (path.startsWith('/api/status/')) {
                const task = path.split('/').pop(); statusFetches.push(task);
                return {running: task === runningTask, logs: []};
            }
            return [];
        },
        post: async (path, body) => {
            posts.push({path, body});
            if (failSubmit && path.endsWith('/response')) { throw new Error('offline'); }
        }
    },
    _startPolling: (key, fetch, options) => { poll = {key, fetch, ...options}; },
    showToast() {}, loadVoices: async () => {}, copyToClipboard: async text => {copied = text;},
    getPersonaContextLines: () => 8, voicesScopeIsNew: () => true,
    keepCurrentVoicesIfAsked: async () => true,
    scriptBatchPoller: null,
    syncPauseButton() {}, syncSnapshotButton() {}, renderActivity() {}, notifyJobDone() {}
};
vm.createContext(context);
vm.runInContext(
    slice(core, 'async function confirmIfRemote(', '// navigator.clipboard') +
    slice(core, 'const taskStartButtons =', '// --- API Helpers ---') +
    slice(core, 'function isTaskFailed(', '// --- Desktop notifications ---') +
    slice(core, 'function getTaskLogUpdate(', '// --- Setup Tab ---') +
    slice(core, 'async function generatePersonas()', 'async function cancelPersonas()') +
    slice(core, 'async function pollPersonaStatus()', '// --- Voices Tab ---') +
    slice(core, 'window.regeneratePersona =', 'window.selectVoiceCandidate =') +
    slice(core, 'function pollScriptLogs(', '// Manual transport') +
    core.slice(core.indexOf('let _manualShown =')) +
    slice(workbench, 'function reattachTaskActivity(', '// Init'), context);
const request = (id, sequence) => ({id, sequence, stage_hint: 'Persona',
    messages: [{role: 'user', content: 'Describe Alice'}]});
const status = id => ({running: true, logs: [], manual_request: {id}});
const button = {closest: () => ({dataset: {voice: 'Alice'}})};
const settle = async () => { for (let i = 0; i < 5; i++) { await Promise.resolve(); } };

(async () => {
    for (const start of [() => context.generatePersonas(),
                         () => context.window.regeneratePersona(button),
                         () => context.window.generateAgeVersion(button)]) {
        poll = null;
        await start();
        assert(poll && poll.key === 'persona', 'each action must start persona polling');
        assert.strictEqual(posts.at(-1).path, '/api/generate_personas');
        pending = request('one', 1);
        await poll.onTick(status('one')); await settle();
        assert.strictEqual(elements['manual-llm-panel'].hidden, false, 'persona tick must render prompt');
        await context.copyManualPrompt();
        assert.strictEqual(copied, 'USER:\nDescribe Alice');
        elements['manual-llm-reply'].value = 'typed answer';
        await poll.onTick(status('one')); await settle();
        assert.strictEqual(elements['manual-llm-reply'].value, 'typed answer');
        failSubmit = true;
        await context.submitManualReply();
        assert.strictEqual(elements['manual-llm-reply'].value, 'typed answer');
        assert.strictEqual(elements['manual-llm-reply'].disabled, false);
        failSubmit = false;
        await context.submitManualReply();
        assert.strictEqual(posts.at(-1).body.id, 'one');
        assert.strictEqual(posts.at(-1).body.content, 'typed answer');
        pending = request('two', 2);
        await poll.onTick(status('two')); await settle();
        assert.strictEqual(elements['manual-llm-title'].textContent, 'request 2');
        assert.strictEqual(elements['manual-llm-reply'].value, '');
        assert.strictEqual(elements['manual-llm-reply'].disabled, false);
        await poll.onDone({running: false, logs: ['Cancelled']}); await settle();
        assert.strictEqual(elements['manual-llm-panel'].hidden, true, 'completion/cancellation must hide panel');
    }
    assert.strictEqual(posts.filter(p => p.path === '/api/generate_personas').at(-1).body.age_group, 'teen');
    poll = null;
    await context.reattachRunningPollers();
    assert(registryFetches>0, 'reload must query actual task registry for persona running state');
    assert(poll && poll.key === 'persona', 'reload must resume persona polling');
    pending = request('reload', 3);
    await poll.onTick(status('reload')); await settle();
    assert.strictEqual(elements['manual-llm-panel'].hidden, false);
    // A different task ending must not clear the persona reply.
    await context.renderManualRequest({running: false}, 'script');
    assert.strictEqual(elements['manual-llm-panel'].hidden, false);
    await poll.onDone({running: false, logs: []});
    // Script generation continues to use the same renderer and controls.
    runningTask = 'script';
    await context.reattachRunningPollers();
    assert(poll && poll.key === 'logs:script', 'reload must restore script manual polling too');
    pending = request('script', 4);
    await poll.onTick(status('script')); await settle();
    assert.strictEqual(elements['manual-llm-title'].textContent, 'request 4');
    poll.onDone({running: false, logs: []}); await settle();
    assert.strictEqual(elements['manual-llm-panel'].hidden, true);
    // A delayed old pending fetch cannot resurrect a completed request.
    let resolve;
    context.API.get = () => new Promise(r => {resolve = r;});
    const delayed = context.renderManualRequest(status('late'), 'persona');
    await context.renderManualRequest({running: false}, 'persona');
    resolve({pending: request('late', 5)});
    await delayed;
    assert.strictEqual(elements['manual-llm-panel'].hidden, true);
    // Two in-flight fetches for the same request must not erase a typed reply.
    const resolvers = [];
    context.API.get = () => new Promise(r => {resolvers.push(r);});
    const first = context.renderManualRequest(status('duplicate'), 'persona');
    const second = context.renderManualRequest(status('duplicate'), 'persona');
    resolvers[0]({pending: request('duplicate', 6)}); await first;
    elements['manual-llm-reply'].value = 'keep this';
    resolvers[1]({pending: request('duplicate', 6)}); await second;
    assert.strictEqual(elements['manual-llm-reply'].value, 'keep this');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run([
            "node", "-e", script, str(STATIC / "js/app-core.js"),
            str(STATIC / "js/app-workbench.js")], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
