const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const states = ['teen', 'adult', 'elderly', 'teen'].map((age, i) => ({version_id: `state_${i}`,
    speaker: 'ARTHUR "<&', age_group: age, gender: 'male', from_entry: i * 10,
    segment_start: i * 10, segment_end: (i + 1) * 10, state_number: i + 1, source_sha256: 's'.repeat(64)}));
const name = states[0].speaker;
const versions = Object.fromEntries(states.map(state => [state.version_id, {type: 'clone',
    ref_audio: `${state.version_id}.wav`, ref_text: state.age_group, description: state.age_group,
    seed: state.state_number, persona_state: state, candidates: []}]));
const base = {type: 'clone', ref_audio: 'base.wav', ref_text: 'base text', description: 'base persona', seed: 99, versions};
const voice = {name, config: base, persona_states: states};
const posts = [], cards = [];
let filter = 'all', pauseFlush = null;
const context = {window: null, document: {
    querySelectorAll: () => cards,
    getElementById: id => id === 'voices-state-filter' ? {value: filter} : {checked: false}
}, AVAILABLE_VOICES: ['Ryan'], BUILTIN_LORAS: [],
    getLibraryVoiceReference: () => null, getTraitBadgeHtml: () => '', getVoiceCandidateMarkup: () => '',
    renderStyleTimeline: () => '', ensembleMembersMarkup: () => '', getLoraModelsById: () => new Map(),
    _voiceCardsBookToken: 'b'.repeat(64), _voiceSaveSnapshot: {book_token: 'b'.repeat(64)},
    flushVoiceSaves: async () => { if (pauseFlush) { await pauseFlush; } },
    API: {post: async (path, body) => {posts.push({path, body}); return {}; }},
    loadVoices: async () => {}, showToast: () => {}, showActionError: (...args) => {throw new Error(args.join(' '));},
};
context.window = context;
context._voicesNames = [name]; context._voicesByName = {[name]: voice};
vm.createContext(context);
function load(first, last) {
    const start = source.indexOf(first); assert(start >= 0, first);
    const end = source.indexOf(last, start); assert(end > start, last);
    vm.runInContext(source.slice(start, end), context);
}
load('function escapeHtml(', '// Parse a numeric input');
load('function getVoiceCardMetadata(', '// Suggest members');
load('function collectVoiceConfig()', 'function onVoiceReadyChange(');
load('function onToggleHideReady()', 'let _voiceStatusClearTimer');
load('window.removeStatePersona =', 'window.regeneratePersona =');
load('window.selectVoiceCandidate =', 'async function applyConfirmedVoiceRemoval(');
load('window.suggestMoreVoices =', 'function renderVoiceSuggestions(');
const html = context.getStateVoiceCardsMarkup([voice]);
assert.equal((html.match(/data-version="state_/g) || []).length, 4);
assert(html.includes('ARTHUR &quot;&lt;&amp;'));
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(ids).size, ids.length, 'duplicate controls cannot target state cards correctly');
assert.equal((html.match(/State seed/g) || []).length, 4);
const reversed = Object.fromEntries(Object.entries(states[0]).reverse());
assert(context.isPersonaStateCurrent(reversed, states[0]), 'property ordering is not source identity');
const pending = context.getStateVoiceCardsMarkup([{...voice, config: {...base, versions: {}}}]);
assert(pending.includes('State persona needs generation for the current script'));
assert(/onclick="regeneratePersona\(this\)"(?![^>]*disabled)/.test(pending), 'pending state can be generated');
assert(/class="form-check-input voice-type"[^>]*disabled/.test(pending), 'pending forms cannot invent a saved state');
function card(version) {
    const data = version ? versions[version] : base;
    const fields = {'.alias-select': {value: ''}, '.voice-type:checked': {value: 'clone'},
        '.ref-text': {value: data.ref_text}, '.ref-audio': {value: data.ref_audio},
        '.persona-description': {value: data.description}, '.voice-ready': {checked: false}};
    if (version) { fields['.voice-seed'] = {value: String(data.seed)}; }
    return {dataset: {voice: name, version: version || '', hasStates: '1'}, fields, style: {},
        isConnected: true, querySelector: selector => fields[selector] || null};
}
cards.push(card(null), ...states.map(state => card(state.version_id)));
cards[1].fields['.ref-audio'].value = 'edited-teen.wav';
cards[1].fields['.persona-description'].value = 'edited teen persona';
cards[1].fields['.voice-seed'].value = '0';
const collected = context.collectVoiceConfig()[name];
assert.equal(collected.ref_audio, 'base.wav');
assert.equal(collected.versions.state_0.ref_audio, 'edited-teen.wav');
assert.equal(collected.versions.state_0.description, 'edited teen persona');
assert.equal(collected.versions.state_0.seed, '0');
assert.equal(collected.versions.state_1.ref_audio, 'state_1.wav');
assert.equal(base.versions.state_0.ref_audio, 'state_0.wav', 'form collection must not mutate the server snapshot');
cards.push({dataset: {voice: 'OTHER', hasStates: '0'}, style: {}});
filter = 'states'; context.onToggleHideReady(); assert.equal(cards.at(-1).style.display, 'none'); assert.equal(cards[1].style.display, '');
filter = 'single'; context.onToggleHideReady(); assert.equal(cards[1].style.display, 'none'); assert.equal(cards.at(-1).style.display, '');
let finished = false;
process.on('beforeExit', () => assert(finished, 'async assertions must finish'));
(async () => {
    const button = {closest: () => cards[2]};
    await context.selectVoiceCandidate(button, 'adult-candidate');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].body.version_id, 'state_1'); assert.equal(posts[0].body.book_token, 'b'.repeat(64));
    context.confirmIfRemote = async () => true;
    context.API.post = async (path, body) => {
        posts.push({path, body});
        return path === '/api/suggest_voices' ? {suggestions: {[name]: {type: 'lora', ranked_adapter_ids: ['ranked-a', 'ranked-b']}}} : {};
    };
    await context.suggestMoreVoices({disabled: false, closest: () => cards[2]});
    const pool = posts.filter(post => post.path.endsWith('/candidates'));
    assert.deepEqual(pool.map(post => post.body.candidate_id), ['ranked-a', 'ranked-b']);
    assert(pool.every(post => post.body.version_id === 'state_1'));
    context.showConfirm = async () => true;
    const deletes = [];
    context.API.del = async path => {deletes.push(path);};
    await context.removeStatePersona(button);
    assert.equal(deletes.length, 1);
    assert(deletes[0].includes('/versions/state_1?book_token='));
    assert.equal(base.ref_audio, 'base.wav');
    context.showConfirm = async () => false;
    await context.removeStatePersona(button);
    assert.equal(deletes.length, 1, 'declined removal cannot send a request');
    const before = posts.length;
    let release; pauseFlush = new Promise(resolve => {release = resolve;});
    const action = context.postVoiceTarget(button, '/candidate', {});
    context._voiceSaveSnapshot = {book_token: 'c'.repeat(64)}; release();
    await assert.rejects(action, /book or state card changed/);
    assert.equal(posts.length, before);
    finished = true;
    console.log('Actual state-card rendering, collection, filters and scoped candidate handlers passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
