"""The Script tab's own progress bar (#2042), driven by the status payload.

The bar lives next to the activity row on the Script tab and reads the fraction
the server already embeds in `/api/status/<task>` (`eta.fraction`). It has no
fraction to draw in two cases - the task has not reported one yet, and the
speaker alias-merge stage, which runs after the last review batch - so both go
indeterminate rather than showing a stalled 100% (#2037).
"""
from pathlib import Path
import subprocess
import unittest

CORE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'

HARNESS = r"""
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');

function classes() {
    const set = new Set();
    return {add: (...n) => n.forEach(x => set.add(x)), remove: (...n) => n.forEach(x => set.delete(x)),
            contains: n => set.has(n)};
}
function node() {
    return {style: {}, classList: classes(), attributes: {}, textContent: '', innerText: '',
            hidden: false,
            setAttribute(n, v) { this.attributes[n] = String(v); },
            removeAttribute(n) { delete this.attributes[n]; }};
}
const wrap = node(), label = node(), value = node(), track = node(), fill = node(), activity = node();
track.querySelector = selector => selector === '.tp-fill' ? fill : null;
track.closest = selector => selector === '.tp-wrap' ? wrap : null;
wrap.querySelector = selector => selector === '.tp-label' ? label
    : selector === '.tp-value' ? value : null;
const elements = {'script-progress': wrap, 'script-progress-bar': track, 'script-activity': activity,
                  'editor-tab': node()};

const ctx = {window: null, console, currentTabName: 'script', Date,
             document: {getElementById: id => elements[id] || (elements[id] = node())},
             t: (key, english, values = {}) => String(english).replace(/\{(\w+)\}/g,
                 (m, name) => Object.hasOwn(values, name) ? String(values[name]) : m),
             API: {get: async () => ({running: false, logs: []})},
             _startPolling: (key, fetch, options) => { ctx.poll = {key, fetch, options}; },
             createTaskLogRenderer: () => status => ({changed: true}),
             syncPauseButton: () => {}, syncSnapshotButton: () => {}, renderActivity: () => {},
             renderManualRequest: () => {}, notifyJobDone: () => {}, loadFinalAudio: () => {},
             loadChunks: () => {}, getTaskCompletionOutcome: () => 'finished'};
ctx.window = ctx;
vm.createContext(ctx);

function slice(startNeedle, endNeedle) {
    const start = source.indexOf(startNeedle);
    const end = endNeedle ? source.indexOf(endNeedle, start) : source.length;
    assert(start >= 0 && end > start, 'missing slice ' + startNeedle);
    return source.slice(start, end);
}
vm.runInContext(slice('// --- Textured progress bars (#2042) ---', '// --- End textured progress bars ---'), ctx);
vm.runInContext(slice('function getTaskLabel(', 'function taskStatusLabel('), ctx);
vm.runInContext(slice('async function pollLogs('), ctx);
const tick = status => ctx.poll.options.onTick(status);
const done = status => ctx.poll.options.onDone(status);
fill.style.width = '';
"""


class ScriptProgressTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(['node', '-e', HARNESS + '\n' + code, str(CORE)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_the_bar_is_hidden_until_the_script_task_runs(self):
        self.run_js(r"""
wrap.hidden = true;
ctx.pollLogs('review', 'script-logs', null, 'script-activity');
tick({running: false, logs: []});
assert.strictEqual(wrap.hidden, true, 'idle hides the bar');
assert.strictEqual(fill.style.width, '', 'and never paints a fraction');
done({running: false, logs: []});
assert.strictEqual(wrap.hidden, true);
""")

    def test_a_reported_fraction_drives_the_bar(self):
        self.run_js(r"""
ctx.pollLogs('review', 'script-logs', null, 'script-activity');
tick({running: true, logs: ['Reviewing batch 3/10'], eta: {fraction: 0.42, eta_seconds: 90}});
assert.strictEqual(wrap.hidden, false);
assert.strictEqual(fill.style.width, '42%');
assert.strictEqual(track.ariaValueNow, '42');
assert.strictEqual(track.ariaValueMin, '0');
assert.strictEqual(track.ariaValueMax, '100');
assert.strictEqual(track.ariaValueText, 'Script review: 42%');
assert.strictEqual(label.textContent, 'Script review');
assert.strictEqual(value.textContent, '42%');
assert(!track.classList.contains('tp-indeterminate'));
""")

    def test_no_fraction_yet_is_indeterminate(self):
        self.run_js(r"""
for (const eta of [null, undefined, {}, {fraction: null}, {fraction: 'x'}]) {
    const bar = track;
    ctx.pollLogs('script', 'script-logs', null, 'script-activity');
    tick({running: true, logs: ['Starting script generation'], eta});
    assert(bar.classList.contains('tp-indeterminate'), 'eta ' + JSON.stringify(eta));
    assert.strictEqual(fill.style.width, '', 'no fill without a fraction');
    assert.strictEqual(bar.attributes['aria-valuenow'], undefined, 'no aria-valuenow');
    assert.strictEqual(bar.ariaValueText, 'Script generation: progress is not reported yet');
}
""")

    def test_the_alias_merge_stage_is_indeterminate_even_at_fraction_one(self):
        self.run_js(r"""
ctx.pollLogs('review', 'script-logs', null, 'script-activity');
// The merge runs after the last batch, so the fraction is 1.0 while the
// single merge call has no known length: a determinate bar would sit at 100%.
// core._compute_eta reports the stage as eta.progress; the bar reads that
// verdict rather than re-scanning the log tail itself.
tick({running: true, logs: ['Progress: 10/10', 'Merging duplicate names: 14 groups'],
      eta: {fraction: 1, eta_seconds: null, progress: 'Merging duplicate names: 14 groups'}});
assert(track.classList.contains('tp-indeterminate'));
assert.strictEqual(fill.style.width, '');
assert.strictEqual(track.attributes['aria-valuenow'], undefined);
""")

    def test_a_fraction_beside_a_non_merge_progress_label_stays_determinate(self):
        self.run_js(r"""
ctx.pollLogs('review', 'script-logs', null, 'script-activity');
tick({running: true, logs: ['Merging duplicate names: 3 groups', 'Reviewing batch 6/10'],
      eta: {fraction: 0.6, eta_seconds: 40, progress: '6/10'}});
assert(!track.classList.contains('tp-indeterminate'), 'the server named the current stage');
assert.strictEqual(track.ariaValueNow, '60');
assert.strictEqual(fill.style.width, '60%');
""")

    def test_finishing_hides_the_bar_and_clears_the_indeterminate_state(self):
        self.run_js(r"""
ctx.pollLogs('review', 'script-logs', null, 'script-activity');
tick({running: true, logs: [], eta: {fraction: null}});
assert(track.classList.contains('tp-indeterminate'));
done({running: false, logs: ['Review completed successfully']});
assert.strictEqual(wrap.hidden, true);
""")

    def test_only_the_script_activity_row_drives_the_script_bar(self):
        self.run_js(r"""
// A different pane's activity row must not touch the Script tab's bar.
ctx.pollLogs('audio', 'audio-logs', null, 'audio-activity');
fill.style.width = 'unchanged';
tick({running: true, logs: [], eta: {fraction: 0.5}});
assert.strictEqual(fill.style.width, 'unchanged');
assert.strictEqual(wrap.hidden, false, 'the script wrap is not the other pane\'s');
""")
