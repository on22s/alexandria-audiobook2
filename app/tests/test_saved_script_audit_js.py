"""Actual saved-script audit UI with out-of-order HTTP completions."""
from pathlib import Path
import json
from script_preflight import audit_script
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / "static/js/app-scripts.js"


class SavedScriptAuditJsTests(unittest.TestCase):
    def run_scenario(self, scenario):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let finished = false;
process.on('beforeExit', () => { assert(finished, 'scenario must reach its final assertion'); });
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('let _savedScriptAuditRequest =');
const end = source.indexOf('async function saveScript()', start);
const panel = {style:{}, className:'', content:'',
    set textContent(value) { this.content = value; }, get textContent() { return this.content; },
    set innerHTML(value) { this.content = value; }, get innerHTML() { return this.content; }};
const requests = [];
const context = {document:{getElementById: id => {
    assert.strictEqual(id, 'saved-script-preflight'); return panel;
}}, escapeHtml: value => String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;'),
API:{post: (url, payload) => new Promise((resolve,reject) => {
    assert.strictEqual(JSON.stringify(payload), '{}'); requests.push({url,resolve,reject});
})}};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const clean = {counts:{blocking:0,manual_review:0}, findings:[]};
const risk = {counts:{blocking:1,manual_review:2},findings:[{code:'nonprose_speech_risk',
    entry_numbers:[3],details:{categories:['symbols'],normalized_preview:'<danger>',validation:{status:'needs_review'}}}]};
(async () => {
""" + scenario + r"""
finished = true;
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(["node", "-e", script, str(SOURCE)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_old_success_cannot_replace_new_pending_or_finished_audit(self):
        self.run_scenario(r"""
const first = context.auditSavedScript('old & book');
const second = context.auditSavedScript('new/book');
assert.strictEqual(requests[0].url, '/api/scripts/old%20%26%20book/preflight');
assert.strictEqual(requests[1].url, '/api/scripts/new%2Fbook/preflight');
assert.strictEqual(panel.textContent, 'Auditing new/book…');
requests[0].resolve(clean); await first;
assert.strictEqual(panel.textContent, 'Auditing new/book…');
requests[1].resolve(risk); await second;
assert.match(panel.innerHTML, /new\/book/);
assert.match(panel.innerHTML, /1 blocking, 2 manual review/);
assert.match(panel.innerHTML, /Entry 3/);
assert.match(panel.innerHTML, /&lt;danger>/);
assert.match(panel.className, /alert-danger/);
const third = context.auditSavedScript('third');
const fourth = context.auditSavedScript('fourth');
requests[3].resolve(clean); await fourth;
const painted = panel.content, className = panel.className;
requests[2].resolve(risk); await third;
assert.strictEqual(panel.content, painted);
assert.strictEqual(panel.className, className);
assert.match(panel.innerHTML, /fourth/);
""")

    def test_old_failure_cannot_replace_new_audit_and_new_failure_remains_visible(self):
        self.run_scenario(r"""
const old = context.auditSavedScript('old');
const current = context.auditSavedScript('current');
requests[1].resolve(clean); await current;
const painted = panel.content;
requests[0].reject(new Error('old request failed')); await old;
assert.strictEqual(panel.content, painted);
assert.match(panel.className, /alert-success/);
const older = context.auditSavedScript('older');
const latest = context.auditSavedScript('latest');
requests[3].reject(new Error('latest unavailable')); await latest;
assert.strictEqual(panel.textContent, 'Audit failed: latest unavailable');
assert.match(panel.className, /alert-danger/);
requests[2].resolve(clean); await older;
assert.strictEqual(panel.textContent, 'Audit failed: latest unavailable');
""")

    def test_reauditing_same_name_still_rejects_an_older_response(self):
        self.run_scenario(r"""
const first = context.auditSavedScript('same');
const second = context.auditSavedScript('same');
requests[1].resolve(risk); await second;
const painted = panel.content;
requests[0].resolve(clean); await first;
assert.strictEqual(panel.content, painted);
assert.match(panel.innerHTML, /1 blocking/);
assert.strictEqual(panel.style.display, 'block');
""")

    def test_actual_backend_nonrisk_findings_all_reach_the_ui_with_entries(self):
        report = audit_script([
            {"speaker": "", "text": "Ordinary prose.", "instruct": ""},
            {"speaker": "NARRATOR", "text": "", "instruct": "Calm."},
            {"speaker": "NARRATOR", "text": "Copyright Notice", "instruct": "Calm."},
        ])
        self.assertNotIn("nonprose_speech_risk", {f["code"] for f in report["findings"]})
        self.run_scenario("const report = " + json.dumps(report) + r""";
const run = context.auditSavedScript('<book>');
requests[0].resolve(report); await run;
assert.match(panel.className, /alert-danger/);
for (const item of report.findings) {
    assert(panel.innerHTML.includes(context.escapeHtml(item.message)), item.code);
    assert(panel.innerHTML.includes('Entry ' + item.entry_numbers.join(', ')), item.code);
}
assert.match(panel.innerHTML, /&lt;book>/);
assert(!panel.innerHTML.includes('No non-prose speech risks detected'));
""")

    def test_new_manual_review_code_and_missing_message_are_visible_and_escaped(self):
        self.run_scenario(r"""
const run = context.auditSavedScript('book');
requests[0].resolve({counts:{blocking:0,manual_review:2},findings:[
    {severity:'manual_review',code:'new_server_finding',message:'Review <unsafe> & text.',entry_numbers:[4,7]},
    {severity:'manual_review',code:'<fallback>',entry_numbers:[]}
]}); await run;
assert.match(panel.className, /alert-warning/);
assert.match(panel.innerHTML, /Review &lt;unsafe> &amp; text/);
assert.match(panel.innerHTML, /Entry 4, 7/);
assert.match(panel.innerHTML, /&lt;fallback>/);
assert(!panel.innerHTML.includes('<unsafe>'));
const cleanRun = context.auditSavedScript('clean');
requests[1].resolve(clean); await cleanRun;
assert.match(panel.className, /alert-success/);
assert.match(panel.innerHTML, /No findings detected/);
""")
