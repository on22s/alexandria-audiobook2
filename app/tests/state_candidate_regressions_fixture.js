const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const posts = [], toasts = [];
const elements = {'advanced-persona-toggle': {checked: true}, 'voices-scope': {value: 'all', dataset: {}, options: [{}, {}]},
    'voices-keep-wrap': {style: {display: ''}}, 'voices-keep-in-library': {checked: true},
    'voices-keep-cast': {}, 'persona-status': {}, 'persona-batch-size': {value: 40},
    'btn-cancel-personas': {style: {}}};
const ctx = {window: null, document: {getElementById: id => elements[id]},
    API: {post: async (path, body) => {posts.push({path, body}); return {}; }},
    showToast: (...args) => toasts.push(args), claimTaskStart: () => true, releaseTaskStart: () => {},
    confirmIfRemote: async () => true, getPersonaContextLines: () => 10, pollPersonaStatus: () => {},
    getCurrentBookName: () => 'My book', showActionError: (...args) => {throw new Error(args);}};
ctx.window = ctx;
ctx._voicesByName = {ARTHUR: {name: 'ARTHUR', persona_pending: false, persona_states_pending: true},
    BOB: {name: 'BOB', persona_pending: false, persona_states_pending: false}};
vm.createContext(ctx);
function load(start, end) {
    const a = source.indexOf(start), b = source.indexOf(end, a);
    assert(a >= 0 && b > a);
    vm.runInContext(source.slice(a, b), ctx);
}
load('function _voicesScopeState()', 'async function cancelPersonas()');
(async () => {
    await ctx.generatePersonas();
    const backup = posts.find(p => p.path === '/api/voice_library/save');
    assert.deepEqual(Array.from(backup.body.characters).sort(), ['ARTHUR', 'BOB']);
    assert.equal(posts.find(p => p.path === '/api/generate_personas').body.new_only, false);
    delete ctx._voicesByName.BOB;
    ctx.refreshVoicesScope();
    assert.equal(elements['voices-scope'].options[1].textContent, 'All characters (regenerate 1)');
    assert.equal(elements['voices-scope'].value, 'new');
    elements['voices-scope'].value = 'all';
    ctx.onVoicesScopeChange(true);
    assert.equal(elements['voices-keep-wrap'].style.display, '', 'all-pending states still offer a backup');
    // A pending base with saved versions also has assets to protect.
    ctx._voicesByName.ARTHUR.persona_pending = true;
    ctx._voicesByName.ARTHUR.config = {versions: {adult: {ref_audio: 'saved.wav'}}};
    assert.deepEqual(Array.from(ctx._voicesScopeState().have), ['ARTHUR']);
    posts.length = 0; toasts.length = 0;
    ctx._voiceCardsBookToken = 'a'.repeat(64);
    ctx._voiceSaveSnapshot = {book_token: ctx._voiceCardsBookToken};
    ctx.flushVoiceSaves = async () => {};
    ctx.loadVoices = async () => {};
    ctx.API.post = async (path, body) => {
        posts.push({path, body});
        return {method: 'none', suggestions: {}, message: 'No characters found in script.'};
    };
    load('async function ensureStateCardCurrent(', 'async function sendVoiceTarget(');
    load('function getSuggestionCandidateConfig(', 'async function suggestVoices(');
    load('window.suggestMoreVoices =', 'function renderVoiceSuggestions()');
    const card = {dataset: {voice: 'ARTHUR', version: 'state'}, isConnected: true};
    const button = {disabled: false, closest: () => card};
    await ctx.suggestMoreVoices(button);
    assert.equal(posts.length, 1, 'empty results never write candidates');
    assert.deepEqual(toasts, [['No characters found in script.', 'warning']]);
    assert.equal(button.disabled, false);
    console.log('backup membership and empty-result behavior verified');
})().catch(error => {console.error(error); process.exitCode = 1;});
