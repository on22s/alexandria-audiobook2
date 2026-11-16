const AVAILABLE_VOICES = ['Ryan'], BUILTIN_LORAS = [];
const voices = [
    {name: 'Alice " æ—¥æœ¬èªž', config: {type: 'custom', voice: 'Ryan', candidates: [
        {candidate_id: 'sample', name: 'Sample', type: 'custom', voice: 'Ryan'}]}},
    {name: 'Alice " æ—¥æœ¬èªž', version_id: 'manual-one', config: {type: 'custom', voice: 'Ryan'}},
    {name: 'Bob', config: {type: 'custom', voice: 'Ryan'}},
];
window._voicesByName = Object.fromEntries(voices.filter(voice => !voice.version_id).map(voice => [voice.name, voice]));
window._voicesNames = voices.map(voice => voice.name);
function ensureVoiceRosterCache() { return {book: 'synthetic'}; }
function renderStyleTimeline() { return ''; }
function ensembleMembersMarkup() { return ''; }
function getLibraryVoiceReference() { return null; }
function showToast() {}
function showActionError(...args) { throw Error(args.join(' ')); }
async function showConfirm() { return true; }
const posts = [];
let currentBookFilename = 'synthetic';
let _voiceSaveSnapshot = null, _voiceRecoveryDrafts = [], revision = 0;
const voiceSaveQueue = {flush: async () => {}, getRevision: () => 0, isDirty: () => false};
const API = {get: async path => path === '/api/voice_config/snapshot'
    ? {revision: String(++revision).padStart(64, '0'), book_token: (currentBookFilename === 'synthetic' ? 'b' : 'c').repeat(64), config: {}, voices}
    : []};
async function loadCastLibrary() {}
function renderVoiceDrafts() {}
function refreshVoicesScope() {}
function updateNarratorPreviewFields() {}
function renderReadyCount() {}
function onToggleHideReady() {}
function renderVoiceSuggestions() {}
function saveVoicesDebounced() { throw Error('Unexpected default save'); }
async function postVoiceTarget(button, path, payload) { posts.push({path, payload}); await Promise.resolve(); }
async function deleteVoiceTarget() { await Promise.resolve(); }
const container = document.getElementById('voices-list');
document.querySelectorAll('.tab-content').forEach(tab => { tab.style.display = 'none'; });
document.getElementById('voices-tab').style.display = 'block';
function check(value, message) { if (!value) { throw Error(message); } }
const turn = () => new Promise(resolve => setTimeout(resolve, 0));
const details = () => container.querySelector('.voice-card-advanced');
async function clickAction(selector) {
    const button = details().querySelector(selector);
    check(button, 'action must exist: ' + selector);
    button.focus();
    button.click();
    await turn();
    check(details().open, '#2011: async refresh collapsed the open section: ' + selector);
    check(document.activeElement.getAttribute('onclick') === button.getAttribute('onclick')
        || button.hasAttribute('data-voice-focus-key') && document.activeElement.getAttribute('data-voice-focus-key') === button.getAttribute('data-voice-focus-key'),
        'clicked control must retain focus');
}
(async () => {
    localStorage.clear();
    await loadVoices();
    details().open = true;
    await turn();
    await clickAction('[onclick*="setVoiceApproval"]');
    await clickAction('[onclick*="favoriteVoiceCandidate"]');
    await clickAction('[onclick*="selectVoiceCandidate"]');
    check(posts.length === 3 && loadVoices.publishedGeneration === 4, 'real action handlers must save and refresh');
    const result = {actions: true};
    localStorage.clear();
    delete window._voiceSectionStates;
    await loadVoices();
    check([...container.querySelectorAll('.voice-card-advanced')].every(node => node.open), 'new sections default open');
    container.querySelectorAll('.voice-card-advanced')[1].open = false;
    await turn();
    await loadVoices();
    check(details().open && !container.querySelectorAll('.voice-card-advanced')[1].open, 'voice and version keys must be independent');
    result.defaults = true;
    document.getElementById('voices-collapse-sections').click();
    await turn();
    check([...container.querySelectorAll('.voice-card-advanced')].every(node => !node.open), 'collapse all');
    await loadVoices();
    check([...container.querySelectorAll('.voice-card-advanced')].every(node => !node.open), 'collapse all survives refresh');
    document.getElementById('voices-expand-sections').click();
    await turn();
    check([...container.querySelectorAll('.voice-card-advanced')].every(node => node.open), 'expand all');
    result.bulk = true;
    details().open = false;
    await turn();
    currentBookFilename = 'another-book';
    await loadVoices();
    check(details().open, 'same-named voice in another book must default open');
    container.querySelectorAll('.voice-card-advanced')[1].open = false;
    await turn();
    currentBookFilename = 'synthetic';
    await loadVoices();
    check(!details().open && container.querySelectorAll('.voice-card-advanced')[1].open, 'returning to a book restores only its own states');
    currentBookFilename = 'another-book';
    await loadVoices();
    check(details().open && !container.querySelectorAll('.voice-card-advanced')[1].open, 'outgoing cards must not write into the incoming book');
    container.replaceChildren();
    delete window._voiceSectionStates;
    await loadVoices();
    check(details().open && !container.querySelectorAll('.voice-card-advanced')[1].open, 'book isolation survives a fresh in-memory session');
    currentBookFilename = 'synthetic';
    await loadVoices();
    result.books = true;
    const removedVoice = voices.pop();
    container.querySelectorAll('.voice-card-advanced')[2].open = false;
    await turn();
    await loadVoices();
    voices.push(removedVoice);
    await loadVoices();
    check(container.querySelectorAll('.voice-card-advanced')[2].open, 'removed voices must not leave state for a later name reuse');
    result.pruning = true;
    details().open = false;
    await turn();
    delete window._voiceSectionStates;
    await loadVoices();
    check(!details().open && container.querySelectorAll('.voice-card-advanced')[1].open, 'fresh session restores per-section state');
    details().open = true;
    await turn();
    Object.defineProperty(window, 'localStorage', {configurable: true, get() { throw Error('storage blocked'); }});
    document.getElementById('voices-collapse-sections').click();
    await turn();
    await loadVoices();
    check(!details().open, 'in-memory fallback survives blocked storage');
    document.getElementById('voices-expand-sections').click();
    await turn();
    await loadVoices();
    check(details().open, 'blocked storage remains usable');
    check(details().querySelector('summary').getAttribute('aria-expanded') === 'true', 'expanded aria state');
    result.persistence = true;
    details().open = false;
    await loadVoices();
    check(!details().open, 'capture must beat the queued native toggle event');
    details().open = true;
    details().querySelector('summary').focus();
    await loadVoices();
    check(document.activeElement === details().querySelector('summary'), 'summary focus survives refresh');
    document.querySelectorAll('.voice-card-advanced').forEach(node => { node.open = false; });
    container.replaceChildren();
    await turn();
    await loadVoices();
    check(details().open, 'detached toggle events must not overwrite state');
    result.detached = true;
    const luminance = color => {
        const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(value => {
            value /= 255;
            return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
        });
        return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
    };
    for (const theme of THEMES) {
        applyTheme(theme.key);
        for (const button of document.querySelectorAll('.voice-section-control')) {
            const rect = button.getBoundingClientRect();
            check(rect.width >= 44 && rect.height >= 44, theme.key + ': touch target');
            const foreground = luminance(getComputedStyle(button).color);
            const background = luminance(getComputedStyle(button.closest('.card-header')).backgroundColor);
            check((Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05) >= 4.5, theme.key + ': contrast');
        }
    }
    result.themes = true;
    const output = document.createElement('pre');
    output.id = 'voice-disclosure-results';
    output.textContent = JSON.stringify(result);
    document.body.append(output);
})().catch(error => {
    const output = document.createElement('pre');
    output.id = 'voice-disclosure-results';
    output.textContent = JSON.stringify({error: error.stack});
    document.body.append(output);
});
