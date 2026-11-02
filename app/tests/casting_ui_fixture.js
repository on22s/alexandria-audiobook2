const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
const root = process.argv[2], existing = fs.readFileSync(path.join(root, 'app/tests/speaker_review_ui_fixture.js'), 'utf8');
// Reuse the existing dependency-free test DOM. These checks are not browser QA.
const stop = existing.indexOf('\nconst root=process.argv[2]'); assert(stop > 0);
const {makeWindow} = new Function('require', existing.slice(0, stop) + '\nreturn {makeWindow, Element};')(require);
const html = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8');
const start = html.indexOf('<div id="casting-tab"'); assert(start > 0);
const end = html.indexOf('<!-- Preparer Tab -->', start); assert(end > start);
const source = fs.readFileSync(path.join(root, 'app/static/js/app-casting.js'), 'utf8');
const settle = async () => { for (let i = 0; i < 8; i++) { await new Promise(resolve => setImmediate(resolve)); } };
const TOKEN = 'a'.repeat(64);
const tags = (gender, ages, tones, textures = []) => ({gender, ages, tones, textures});
const VOICES = [
    {id: 'persona/dh_1/abel_1', source: 'persona', book_id: 'DH', name: 'ABEL', description: 'Young male; calm, academic.',
     ref_text: 'Hello.', preview_url: '/designed_voices/persona/dh_1/abel 1/preview.wav', audio_present: true,
     tags: tags('Male', ['Young Adult'], ['calm', 'academic'])},
    {id: 'persona/dh_1/aichel_1', source: 'persona', book_id: 'DH', name: 'AICHEL', description: 'Female; warm, polite.',
     ref_text: 'A pleasure.', preview_url: '/designed_voices/persona/dh_1/aichel_1/preview.wav', audio_present: true,
     tags: tags('Female', ['Adult'], ['warm', 'polite'], ['smooth'])},
    {id: 'persona/lotm_1/x_1', source: 'persona', book_id: 'LOTM', name: '<img src=x onerror=alert(1)>',
     description: '<script>alert(2)</script> calm', ref_text: '', preview_url: null, audio_present: false,
     tags: tags('Not specified', ['Not specified'], ['calm'])},
];
let assertions = 0;
// Values from the page's vm realm have other prototypes; compare their JSON shape.
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const ok = (value, message = 'expected a truthy value') => { assert.ok(value, message); assertions++; };
const eq = (a, b, message = 'values differ') => { assert.deepStrictEqual(plain(a), plain(b), message); assertions++; };

function setup({snapshotVoices = [{name: 'ADAM'}, {name: 'EVE'}], confirm = true} = {}) {
    const w = makeWindow(html.slice(start, end));
    const calls = [], errors = [], toasts = [];
    const marks = {'persona/dh_1/abel_1': {status: 'shortlist'}};
    w.API = {
        get: async url => {
            calls.push(['GET', url]);
            if (url === '/api/casting/voices') { return {voices: JSON.parse(JSON.stringify(VOICES)), marks: {...marks}, skipped: []}; }
            if (url === '/api/voice_config/snapshot') { return {book_token: TOKEN, voices: snapshotVoices}; }
            if (url.startsWith('/api/casting/clone_config')) { return {config: {type: 'clone', ref_audio: 'designed_voices/x.wav'}}; }
            throw new Error('unexpected GET ' + url);
        },
        post: async (url, body) => {
            calls.push(['POST', url, body]);
            if (url === '/api/casting/marks') { const {voice_id, ...patch} = body; return {voice_id, mark: patch}; }
            return {status: 'ok'};
        },
    };
    w.showToast = (message, type) => toasts.push([type, message]);
    w.showActionError = (action, error) => errors.push([action, String(error?.message || error)]);
    w.showConfirm = async () => confirm;
    w.stopDesignedVoicePlayback = () => {};
    w.Audio = class { constructor(src) { this.src = src; w.played.push(src); } play() { return Promise.resolve(); } pause() {} addEventListener() {} };
    w.played = [];
    w.eval(source);
    return {w, calls, errors, toasts, get: id => w.document.getElementById('casting-' + id)};
}
const cards = env => env.get('results').querySelectorAll('strong');
const input = (el, value) => { el.value = value; el.dispatchEvent({type: 'input'}); };

(async () => {
    // Query parsing: fields, negation, quoted words, and unknown fields reported, not silently used.
    let env = setup();
    const q = env.w.CastingLibrary.parseQuery('gender:female tone:calm -status:reject hello "two words" foo:bar');
    eq(q.terms, [{field: 'gender', value: 'female', negate: false}, {field: 'tone', value: 'calm', negate: false},
                 {field: 'status', value: 'reject', negate: true}]);
    eq(q.words.map(word => word.value), ['hello', 'two words', 'foo:bar']);
    eq(q.unknown, ['foo']);
    eq(env.w.CastingLibrary.previewUrl('/designed_voices/persona/dh_1/abel 1/preview.wav'),
       '/designed_voices/persona/dh_1/abel%201/preview.wav');

    // Load: every voice rendered, marks applied, speakers listed.
    await env.w.loadCastingLibrary(); await settle();
    eq(cards(env).length, 3);
    ok(env.get('summary').textContent.startsWith('Showing 3 of 3'));
    // Markup injection: catalogue text stays text.
    ok(cards(env).some(n => n.textContent === '<img src=x onerror=alert(1)>'));
    eq(env.get('results').querySelectorAll('img').length, 0);
    eq(env.get('results').querySelectorAll('script').length, 0);

    // Filters: "male" must not match "female"; negation; free words; selects; chips.
    input(env.get('search'), 'gender:male'); eq(cards(env).map(n => n.textContent), ['ABEL']);
    input(env.get('search'), 'gender:female'); eq(cards(env).map(n => n.textContent), ['AICHEL']);
    input(env.get('search'), '-gender:female'); eq(cards(env).length, 2);
    input(env.get('search'), 'academic'); eq(cards(env).map(n => n.textContent), ['ABEL']);
    input(env.get('search'), 'status:shortlist'); eq(cards(env).map(n => n.textContent), ['ABEL']);
    input(env.get('search'), 'nosuch:thing'); ok(env.get('summary').textContent.includes('Unknown field: nosuch'));
    input(env.get('search'), '');
    env.get('book').value = 'LOTM'; env.get('book').dispatchEvent({type: 'change'}); eq(cards(env).length, 1);
    env.get('book').value = ''; env.get('book').dispatchEvent({type: 'change'});
    const calmChip = env.get('tones').querySelectorAll('button').find(b => b.textContent.startsWith('calm '));
    ok(calmChip && calmChip.textContent === 'calm 2', 'chip shows its count');
    calmChip.click(); eq(cards(env).length, 2);
    env.get('clear').click(); eq(cards(env).length, 3);

    // Status change posts one mark and updates the view.
    const statusSelect = env.get('results').querySelectorAll('select').find(s => s.attrs['aria-label'] === 'Review status for AICHEL');
    statusSelect.value = 'callback'; statusSelect.dispatchEvent({type: 'change'}); await settle();
    eq(env.calls.at(-1), ['POST', '/api/casting/marks', {voice_id: 'persona/dh_1/aichel_1', status: 'callback'}]);
    eq(env.w.CastingLibrary.state().marks['persona/dh_1/aichel_1'], {status: 'callback'});

    // Use for character: fresh snapshot, server-built config, existing candidate add then select, both book-fenced.
    env.calls.length = 0;
    const castSelect = env.get('results').querySelectorAll('select').find(s => s.attrs['aria-label'] === 'Character to use ABEL for');
    castSelect.value = 'EVE';
    const use = castSelect.parentElement.querySelector('button');
    use.click(); await settle();
    eq(env.errors, []);
    eq(env.calls.map(c => c.slice(0, 2)), [
        ['GET', '/api/voice_config/snapshot'],
        ['GET', '/api/casting/clone_config?voice_id=persona%2Fdh_1%2Fabel_1&speaker=EVE'],
        ['POST', '/api/voices/EVE/candidates'],
        ['POST', `/api/voices/EVE/candidates/${env.calls[2][2].candidate_id}/select`]]);
    eq(env.calls[2][2].book_token, TOKEN);
    eq(env.calls[2][2].config, {type: 'clone', ref_audio: 'designed_voices/x.wav'});
    eq(env.calls[3][2], {book_token: TOKEN});
    ok(/^casting-[a-z0-9-]+$/.test(env.calls[2][2].candidate_id) && env.calls[2][2].candidate_id.length <= 80);
    // A voice without audio cannot be cast.
    const muteUse = env.get('results').querySelectorAll('select')
        .find(s => s.attrs['aria-label'] === 'Character to use <img src=x onerror=alert(1)> for').parentElement.querySelector('button');
    ok(muteUse.disabled);

    // A character gone from the book is refused before any write; a declined confirm writes nothing.
    env = setup({snapshotVoices: [{name: 'ADAM'}]});
    await env.w.loadCastingLibrary(); await settle();
    env.w.CastingLibrary.state().speakers = ['ADAM', 'EVE'];
    let sel = env.get('results').querySelectorAll('select').find(s => s.attrs['aria-label'] === 'Character to use ABEL for');
    env.calls.length = 0; sel.value = 'EVE'; sel.parentElement.querySelector('button').click(); await settle();
    ok(!env.calls.some(c => c[0] === 'POST'), 'no write for a missing character');
    ok(env.errors.length === 1 && env.errors[0][1].includes('no longer in the active book'));
    env = setup({confirm: false});
    await env.w.loadCastingLibrary(); await settle();
    sel = env.get('results').querySelectorAll('select').find(s => s.attrs['aria-label'] === 'Character to use ABEL for');
    env.calls.length = 0; sel.value = 'ADAM'; sel.parentElement.querySelector('button').click(); await settle();
    eq(env.calls, []);

    // Review over the filtered list (book DH: ABEL, AICHEL): starts at the first unreviewed voice;
    // keys mark and advance; typing in the notes box never marks.
    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    input(env.get('search'), 'book:dh');
    env.get('review').click(); await settle();
    let r = env.w.CastingLibrary.state().review;
    eq(r.list.map(v => v.name), ['ABEL', 'AICHEL']);
    eq(r.index, 1);  // ABEL is already shortlisted
    ok(env.get('review-body').querySelectorAll('h3')[0].textContent === 'AICHEL');
    env.calls.length = 0;
    env.get('review-modal').dispatchEvent({type: 'keydown', key: 'r', preventDefault() {}}); await settle();
    eq(env.calls[0], ['POST', '/api/casting/marks', {voice_id: 'persona/dh_1/aichel_1', status: 'reject'}]);
    eq(env.w.CastingLibrary.state().review.index, 1);  // last voice: stays put
    env.calls.length = 0;
    env.w.CastingLibrary.onReviewKey({key: 's', target: {tagName: 'TEXTAREA'}}); await settle();
    eq(env.calls, []);
    env.get('review-modal').dispatchEvent({type: 'keydown', key: 'ArrowLeft', preventDefault() {}}); await settle();
    eq(env.w.CastingLibrary.state().review.index, 0);
    env.get('review-modal').dispatchEvent({type: 'keydown', key: ' ', preventDefault() {}}); await settle();
    eq(env.w.played.at(-1), '/designed_voices/persona/dh_1/abel%201/preview.wav');
    env.get('review-modal').dispatchEvent({type: 'keydown', key: 'c', preventDefault() {}}); await settle();
    eq(env.calls.at(-1), ['POST', '/api/casting/marks', {voice_id: 'persona/dh_1/abel_1', status: 'callback'}]);
    eq(env.w.CastingLibrary.state().review.index, 1);  // marked, then advanced

    console.log(JSON.stringify({passed: true, assertions}));
})().catch(error => { console.error(error); process.exit(1); });
