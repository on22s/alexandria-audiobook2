"""Execute checkpoint visibility and confirmation with the real vanilla JS."""
from pathlib import Path
import subprocess
import unittest

STATIC = Path(__file__).resolve().parents[1] / 'static'


class ReviewStartOverUiTests(unittest.TestCase):
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

    def test_restart_translations_survive_utf8_round_trip(self):
        import json
        for locale, expected in [('en', 'Start over'), ('zh-CN', '\u91cd\u65b0\u5f00\u59cb'),
                                 ('ja', '\u6700\u521d\u304b\u3089\u3084\u308a\u76f4\u3059'),
                                 ('es-419', 'Empezar de nuevo')]:
            with self.subTest(locale=locale):
                catalog = json.loads((STATIC / 'i18n' / f'ui.{locale}.json').read_text(encoding='utf-8'))
                self.assertEqual(expected, catalog['script.review.start-over'])
                self.assertTrue(catalog['script.review.restart-confirm'].strip())
