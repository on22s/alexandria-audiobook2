const fs = require('fs'), vm = require('vm'), assert = require('assert');
const core = fs.readFileSync(process.argv[2], 'utf8'), scripts = fs.readFileSync(process.argv[3], 'utf8');
const snapshot = JSON.parse(process.argv[4]);
const voice = snapshot.voices.find(row => row.name === 'ARTHUR'), target = voice.persona_states[1];
const elements = {}, posts = [], errors = [];
for (const id of ['persona-recovery-speaker', 'persona-recovery-state', 'persona-recovery-json', 'persona-recovery-panel',
    'persona-recovery-status', 'design-preview-container', 'design-status', 'design-voice-name', 'design-source-name', 'design-description', 'design-sample-text', 'design-alias-select']) {
    elements[id] = {value: '', dataset: {}, style: {}, focus() {}};
}
const fields = {'.voice-type:checked': {value: 'design'}, '.design-description': {value: 'Edited state design'},
    '.persona-description': {value: 'Original hidden persona'}, '.ref-text': {value: 'State text'}, '.ref-audio': {value: 'state.wav'},
    '.voice-ready': {checked: false}, '.alias-select': {value: ''}};
const card = {dataset: {voice: 'ARTHUR', version: target.version_id}, querySelector: selector => fields[selector] || null};
const baseFields = {...fields, '.voice-type:checked': {value: 'clone'}, '.ref-audio': {value: 'base.wav'}, '.persona-description': {value: 'base'}};
const base = {dataset: {voice: 'ARTHUR', version: ''}, querySelector: selector => baseFields[selector] || null};
const context = {ensureVoiceRosterOptions:()=>true,window: null, document: {getElementById: id => elements[id] || null,
    querySelectorAll: () => [base, card], querySelector: () => ({click() {}})},
    _voiceCardsBookToken: snapshot.book_token, _voiceSaveSnapshot: snapshot,
    getLoraModelsById: () => new Map(), API: {post: async (path, body) => posts.push({path, body})},
    flushVoiceSaves: async () => {}, loadVoices: async () => {}, showToast() {},
    showActionError: (message, error) => errors.push(error.message), getActionErrorMessage: (message, error) => error.message,
    ensureDesignerEditsDiscardable: () => true, resetDesignerForm() {}, markDesignerFormClean() {}};
context.window = context; context._voicesByName = {ARTHUR: voice}; vm.createContext(context);
function load(source, start, end) {
    const a = source.indexOf(start), b = source.indexOf(end, a); assert(a >= 0 && b > a);
    vm.runInContext(source.slice(a, b), context);
}
load(core, 'function escapeHtml(', '// Parse a numeric input');
load(core, 'function getVoiceCardMetadata(', 'function createVoiceCard(');
load(core, 'function collectVoiceConfig()', 'function onVoiceReadyChange(');
load(core, 'function renderPersonaRecoveryTargets()', 'window.copyPersonaPrompt =');
load(scripts, 'window.openVoiceDesignEditor =', 'window.onDesignedVoiceSelect =');
assert.equal(context.collectVoiceConfig().ARTHUR.versions[target.version_id].description, 'Edited state design');
fields['.voice-type:checked'].value = 'clone'; fields['.persona-description'].value = 'Edited clone persona';
assert.equal(context.collectVoiceConfig().ARTHUR.versions[target.version_id].description, 'Edited clone persona');
fields['.voice-type:checked'].value = 'design';
let finished = false; process.on('beforeExit', () => assert(finished));
(async () => {
    await context.openVoiceDesignEditor({closest: () => card});
    assert.equal(elements['design-description'].value, 'Edited state design');
    assert.equal(elements['design-source-name'].dataset.version, target.version_id);
    // The base Designer path must also ignore the inactive clone textarea.
    baseFields['.voice-type:checked'] = {value: 'design'};
    await context.openVoiceDesignEditor({closest: () => base});
    assert.equal(elements['design-description'].value, 'Edited state design');
    context.renderPersonaRecoveryTargets();
    assert.equal(elements['persona-recovery-state'].value, '__base__', 'empty initial speaker shows base-only choices');
    elements['persona-recovery-speaker'].value = 'ARTHUR';
    elements['persona-recovery-json'].value = JSON.stringify({description: 'Recovered adult state voice.', ref_text: 'Adult state line.'});
    context.renderPersonaRecoveryTargets();
    assert.equal(elements['persona-recovery-state'].value, '', 'multi-state recovery needs explicit selection');
    await context.recoverPersona(); assert.equal(posts.length, 0); assert.match(errors.pop(), /Choose/);
    context.openStatePersonaRecovery({closest: () => card});
    assert.equal(elements['persona-recovery-state'].value, target.version_id);
    assert.equal(elements['persona-recovery-panel'].open, true);
    await context.recoverPersona(true);
    assert.equal(posts.length, 1); assert.equal(posts[0].body.state_version, target.version_id);
    assert.equal(posts[0].body.book_token, snapshot.book_token); assert.equal(posts[0].body.resume, true);
    let release; context.flushVoiceSaves = () => new Promise(resolve => {release = resolve;});
    const pending = context.recoverPersona();
    context._voiceSaveSnapshot = {...snapshot, book_token: '0'.repeat(64)}; release(); await pending;
    assert.equal(posts.length, 1, 'book switch while flushing cannot recover another book');
    assert.match(errors.pop(), /book or recovery target changed/);
    context._voiceCardsBookToken = '0'.repeat(64); context.renderPersonaRecoveryTargets();
    assert.equal(elements['persona-recovery-state'].value, '', 'book switch clears the prior target choice');
    finished = true;
    console.log(JSON.stringify(posts[0]));
})().catch(error => {console.error(error); process.exitCode = 1;});
