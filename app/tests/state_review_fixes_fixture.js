// #1040 review fixes, run against the real app-core.js: T7 (unmappable state rows),
// T15 (no silent stale returns), T16 (one suggestion -> candidate converter), T26 (stale marker).
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const posts = [], deletes = [], toasts = [], errors = [];
const token = 'a'.repeat(64);
const ctx = {window: null, currentBookFilename: 'book.json', pendingVoiceStateSaves: new Set(),
    _voiceCardsBookToken: token, _voiceSaveSnapshot: {book_token: token},
    API: {post: async (path, body) => { posts.push({path, body}); return {}; },
          del: async path => { deletes.push(path); return {}; }},
    flushVoiceSaves: async () => {}, loadVoices: async () => {}, showConfirm: async () => true,
    showToast: (...args) => toasts.push(args), showActionError: (...args) => errors.push(args),
    claimTaskStart: () => true, releaseTaskStart: () => {}, confirmIfRemote: async () => true,
    getPersonaContextLines: () => 8, pollPersonaStatus: () => {},
    getLoraModelsById: () => new Map([['known', {path: 'lora_models/known', description: 'warm', gender: 'female'}]])};
ctx.window = ctx;
vm.createContext(ctx);
function load(start, end) {
    const a = source.indexOf(start), b = source.indexOf(end, a);
    assert(a >= 0 && b > a, start);
    vm.runInContext(source.slice(a, b), ctx);
}
load('function escapeHtml(', '// Parse a numeric input');
load('async function ensureStateCardCurrent(', 'function getStateVoiceCardsMarkup(');
load('function getSuggestionCandidateConfig(', 'async function suggestVoices(');
load('window.regeneratePersona =', 'window.generateAgeVersion =');
load('window.suggestMoreVoices =', 'function renderVoiceSuggestions(');
load('async function applyVoiceStateSave(', 'async function clearVoiceStates(');

(async () => {
    // T26: the stale lock keeps only marked controls, whatever their handler is called.
    const markup = '<button data-stale-allowed onclick="renamedRecovery(this)">a</button>'
        + '<button onclick="regeneratePersona(this)">b</button><input class="x"><select class="y"></select>';
    const locked = ctx.disableStaleCardControls(markup);
    assert(/<button data-stale-allowed onclick="renamedRecovery\(this\)">/.test(locked), 'marked control stays enabled');
    assert(/regeneratePersona\(this\)" disabled>/.test(locked), 'an unmarked button is locked even with a familiar handler');
    assert(/<input class="x" disabled>/.test(locked) && /<select class="y" disabled>/.test(locked));

    // T1 in the UI: a stale card with a saved version can be removed; a pending one (nothing saved) cannot.
    load('function getVoiceCardMetadata(', '// Suggest members');
    Object.assign(ctx, {AVAILABLE_VOICES: ['Ryan'], BUILTIN_LORAS: [], getLibraryVoiceReference: () => null,
        getTraitBadgeHtml: () => '', getVoiceCandidateMarkup: () => '', renderStyleTimeline: () => '',
        ensembleMembersMarkup: () => ''});
    const state = {version_id: 'state_' + 'c'.repeat(24), speaker: 'R', age_group: 'child', gender: 'male',
        from_entry: 0, segment_start: 0, segment_end: 5, state_number: 1, current: false};
    const removeTag = html => { const i = html.indexOf('onclick="removeStatePersona(this)"'); return html.slice(html.lastIndexOf('<button', i), html.indexOf('>', i) + 1); };
    const staleSaved = ctx.getStateVoiceCardsMarkup([{name: 'R', persona_states: [state],
        config: {versions: {[state.version_id]: {type: 'custom', persona_state: {...state, segment_sha256: '0'}}}}}]);
    assert(!/ disabled>$/.test(removeTag(staleSaved)), 'a stale saved state can be removed from its card');
    const pendingCard = ctx.getStateVoiceCardsMarkup([{name: 'R', persona_states: [state], config: {}}]);
    assert(/ disabled>$/.test(removeTag(pendingCard)), 'a pending state has nothing to remove');

    // T16: one converter; an unknown adapter path is null, never guessed.
    const unknown = ctx.getSuggestionCandidateConfig({type: 'lora'}, 'mystery', 0);
    assert.strictEqual(unknown.adapter_path, null);
    assert.deepStrictEqual([unknown.rank, unknown.source], [1, 'auto_suggest']);
    assert.strictEqual(ctx.getSuggestionCandidateConfig({}, 'known', 2).adapter_path, 'lora_models/known');

    // T16 on a state card: rank/source kept, one request per candidate, version-scoped.
    const card = {dataset: {voice: 'MIRA', version: 'state_' + 'b'.repeat(24)}, isConnected: true};
    ctx.API.post = async (path, body) => {
        posts.push({path, body});
        return path === '/api/suggest_voices'
            ? {suggestions: {MIRA: {ranked_adapter_ids: ['known', 'mystery'], type: 'lora', character_style: 'young'}}} : {};
    };
    await ctx.suggestMoreVoices({disabled: false, closest: () => card});
    const saved = posts.filter(p => p.path.endsWith('/candidates'));
    assert.strictEqual(saved.length, 2);
    assert.deepStrictEqual(saved.map(p => [p.body.config.rank, p.body.config.source, p.body.config.adapter_path]),
        [[1, 'auto_suggest', 'lora_models/known'], [2, 'auto_suggest', null]]);
    assert(saved.every(p => p.body.version_id === card.dataset.version && p.body.book_token === token));
    assert.strictEqual(saved[0].body.config.character_style, 'young');

    // T15: a book change during the remote-cost dialog is reported, never a silent return.
    errors.length = 0; posts.length = 0;
    ctx.confirmIfRemote = async () => { ctx._voiceSaveSnapshot = {book_token: 'c'.repeat(64)}; return true; };
    await ctx.regeneratePersona({closest: () => card});
    assert.strictEqual(posts.length, 0, 'nothing is sent for a stale card');
    assert.strictEqual(errors.length, 1, 'the user is told');
    assert(/Reload Voices/.test(String(errors[0][1].message)));
    ctx._voiceSaveSnapshot = {book_token: token};
    ctx.confirmIfRemote = async () => true;

    // T7: an unmappable state row is skipped and reported; the others still apply.
    posts.length = 0; toasts.length = 0;
    ctx._voicesByName = {MIRA: {config: {versions: {}}}};
    ctx._voiceStateSuggestions = {MIRA: {states: []}};
    const rows = [{dataset: {fromIndex: '', age: 'young_child'}, querySelector: () => ({value: 'version:state_x'})},
                  {dataset: {fromIndex: '7', age: 'adult'}, querySelector: () => ({value: 'version:state_y'})}];
    const timelineCard = {dataset: {voice: 'MIRA'}, isConnected: true, querySelectorAll: selector =>
        selector === '.voice-state-row' ? rows : []};
    const button = {disabled: false, innerHTML: 'Apply', closest: () => timelineCard, setAttribute() {}, removeAttribute() {}};
    await ctx.applyVoiceStates(button);
    const timeline = posts.find(p => p.path.endsWith('/version_timeline'));
    assert(timeline, 'the mappable state is applied');
    assert.strictEqual(JSON.stringify(timeline.body.points), JSON.stringify([{from_index: 7, version_id: 'state_y'}]));
    assert(toasts.some(([message, kind]) => kind === 'warning' && /young child/.test(message)), 'the skipped state is named');
    console.log('state review fixes verified');
})().catch(error => { console.error(error); process.exitCode = 1; });
