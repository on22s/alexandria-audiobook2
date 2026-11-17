"""Exercise the native review controls and existing approval transport."""
from pathlib import Path
import subprocess
import unittest

STATIC = Path(__file__).resolve().parent.parent / 'static'
HARNESS = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[1] + '/js/app-core.js', 'utf8');
let saved = null, blocked = false, failure = false, reloads = 0, errors = [], posts = [];
const classes = new Set(), toggle = {checked: true}, config = {ready: true};
const card = {dataset: {voice: 'Fixture'}, isConnected: true};
const button = {closest: () => card, disabled: false};
const context = {
    window: {_voicesByName: {Fixture: {config}}},
    document: {getElementById: id => id === 'voices-show-reviews' ? toggle : {
        classList: {toggle: (name, on) => on ? classes.add(name) : classes.delete(name)}}},
    localStorage: {
        getItem: key => {assert.equal(key, 'alex-voice-reviews-shown'); if (blocked) {throw Error('blocked');} return saved;},
        setItem: (key, value) => {assert.equal(key, 'alex-voice-reviews-shown'); if (blocked) {throw Error('blocked');} saved = value;}
    },
    API: {post: async (path, payload) => {
        posts.push({path, payload}); if (failure) {throw Error('offline');}
        Object.assign(card.dataset.version ? config.versions[card.dataset.version] : config, payload);
    }},
    loadVoices: async () => {reloads++;}, showToast() {}, showActionError: (...args) => errors.push(args),
    _voiceCardsBookToken: 'fixture-token', _voiceSaveSnapshot: {book_token: 'fixture-token'}, flushVoiceSaves: async () => {}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1] + '/js/app-i18n.js', 'utf8'), context);
function load(start, end) {
    const first = source.indexOf(start), last = source.indexOf(end, first);
    assert(first >= 0 && last > first); vm.runInContext(source.slice(first, last), context);
}
load('function escapeHtml(', '// Parse a numeric input');
load('function getVoiceCardMetadata(', 'function createVoiceCard(');
load('window.setVoiceApproval =', 'window.editPersonaVoiceAudit =');
const markup = () => context.getVoiceReviewButtonsMarkup(config);
const parsed = () => [...markup().matchAll(/<button[^>]*data-review-status="([^"]+)" data-review-side="([^"]+)" aria-pressed="([^"]+)"[^>]*>([\s\S]*?)<\/button>/g)];
'''


class VoiceReviewButtonsJsTests(unittest.TestCase):
    def run_js(self, assertions):
        result = subprocess.run(['node', '-e', HARNESS + assertions, str(STATIC)],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_row_major_order_and_accessible_default_labels(self):
        self.run_js(r'''
assert.deepEqual(parsed().map(match => [match[1], match[2], match[3]]), [
 ['approved','persona','false'], ['approved','voice','false'],
 ['reviewed','persona','false'], ['reviewed','voice','false'],
 ['rejected','persona','false'], ['rejected','voice','false']]);
assert(markup().includes('Review persona')); assert(markup().includes('Review voice'));
assert(!markup().includes('Mark persona reviewed'));
''')

    def test_each_action_toggles_and_clears_without_changing_ready_or_other_side(self):
        self.run_js(r'''
(async () => {
 for (const side of ['persona','voice']) {
  const other = side === 'persona' ? 'voice' : 'persona'; config[other + '_status'] = 'approved';
  for (const status of ['approved','reviewed','rejected']) {
   config[side + '_status'] = 'unreviewed';
   await context.window.setVoiceApproval(button, side + '_status', status);
   assert.equal(config[side + '_status'], status);
   const selected = parsed().filter(match => match[2] === side && match[3] === 'true');
   assert.equal(selected.length, 1); assert.equal(selected[0][1], status);
   assert(selected[0][4].includes((side === 'persona' ? 'Persona ' : 'Voice ') + status));
   await context.window.setVoiceApproval(button, side + '_status', status);
   assert.equal(config[side + '_status'], 'unreviewed');
   assert.equal(parsed().filter(match => match[2] === side && match[3] === 'true').length, 0);
   assert.equal(config[other + '_status'], 'approved'); assert.equal(config.ready, true);
   assert.equal(button.disabled, false);
  }
 }
 assert.equal(posts.length, 12); assert.equal(reloads, 12);
 assert(posts.every(post => post.path === '/api/voices/Fixture/approval'));
})().catch(error => {console.error(error); process.exitCode = 1;});
''')

    def test_exclusivity_state_version_scope_and_failure_preserve_saved_state(self):
        self.run_js(r'''
(async () => {
 config.persona_status = 'approved'; config.versions = {adult: {persona_status: 'approved'}};
 card.dataset.version = 'adult';
 await context.window.setVoiceApproval(button, 'persona_status', 'approved');
 assert.equal(config.versions.adult.persona_status, 'unreviewed'); assert.equal(config.persona_status, 'approved');
 assert.equal(posts[0].payload.version_id, 'adult'); assert.equal(posts[0].payload.book_token, 'fixture-token');
 failure = true;
 await context.window.setVoiceApproval(button, 'persona_status', 'rejected');
 assert.equal(config.versions.adult.persona_status, 'unreviewed'); assert.equal(errors.length, 1);
 assert.equal(reloads, 1); assert.equal(button.disabled, false);
 button.disabled = true; await context.window.setVoiceApproval(button, 'voice_status', 'reviewed');
 assert.equal(posts.length, 2);
})().catch(error => {console.error(error); process.exitCode = 1;});
''')

    def test_global_visibility_survives_reload_and_storage_refusal(self):
        self.run_js(r'''
context.restoreVoiceReviewVisibility(); assert.equal(toggle.checked, true);
context.setVoiceReviewVisibility(false); assert.equal(saved, 'false'); assert(classes.has('voice-reviews-hidden'));
toggle.checked = true; classes.clear(); context.restoreVoiceReviewVisibility();
assert.equal(toggle.checked, false); assert(classes.has('voice-reviews-hidden'));
context.setVoiceReviewVisibility(true); context.restoreVoiceReviewVisibility();
assert.equal(toggle.checked, true); assert(!classes.has('voice-reviews-hidden'));
blocked = true; context.setVoiceReviewVisibility(false); assert(classes.has('voice-reviews-hidden'));
context.restoreVoiceReviewVisibility(); assert.equal(toggle.checked, true);
''')

    def test_state_labels_are_translated_in_every_catalog(self):
        self.run_js(r'''
for (const locale of ['en','zh-CN','ja','es-419']) {
 const catalog = JSON.parse(fs.readFileSync(process.argv[1] + '/i18n/ui.' + locale + '.json','utf8'));
 context.uiMessageMarkup = (key, fallback) => {assert(key in catalog, key); return catalog[key];};
 for (const side of ['persona','voice']) {
  for (const status of ['approved','reviewed','rejected']) {
   config[side + '_status'] = status;
   assert(markup().includes(catalog['voice.card.' + side + '-' + status]));
  }
 }
}
''')
