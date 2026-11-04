const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
const root = process.argv[2], existing = fs.readFileSync(path.join(root, 'app/tests/speaker_review_ui_fixture.js'), 'utf8');
const stop = existing.indexOf('\nconst root=process.argv[2]'); assert(stop > 0);
const {makeWindow, Element} = new Function('require', existing.slice(0, stop) + '\nreturn {makeWindow, Element};')(require);
for (const property of ['innerHTML', 'outerHTML']) {
    Object.defineProperty(Element.prototype, property, {set() { throw new Error('Forbidden HTML sink: ' + property); }});
}
Element.prototype.insertAdjacentHTML = () => { throw new Error('Forbidden HTML insertion'); };
const source = fs.readFileSync(path.join(root, 'app/static/js/app-casting.js'), 'utf8');
const research = fs.readFileSync(path.join(root, 'app/static/js/app-casting-research.js'), 'utf8');

const HTML = `
<div class="casting-workspace" id="casting-workspace">
  <div id="casting-tab">
    <div id="casting-refresh"></div>
    <div id="casting-research">
      <input id="casting-search" type="search">
      <select id="casting-book"></select>
      <select id="casting-gender"></select>
      <select id="casting-age"></select>
      <select id="casting-status"></select>
      <select id="casting-health"></select>
      <div id="casting-tones"></div>
      <div id="casting-textures"></div>
      <div id="casting-health-counts"></div>
      <select id="casting-sort"><option value="name">Name</option><option value="book">Book</option><option value="status">Status</option><option value="priority">Priority</option></select>
      <button id="casting-view-cards" type="button"></button>
      <button id="casting-view-table" type="button"></button>
      <button id="casting-play-all" type="button">Play all</button>
      <button id="casting-review" type="button">Review</button>
      <button id="casting-clear" type="button">Clear filters</button>
      <span id="casting-summary"></span>
      <span id="casting-playback-status"></span>
      <div id="casting-skipped"></div>
      <div id="casting-coverage"></div>
      <select id="casting-collection"></select>
      <input id="casting-collection-name">
      <button id="casting-create-collection" type="button"></button>
      <button id="casting-match-collection" type="button"></button>
      <div id="casting-collection-review"></div>
      <div id="casting-load-status"></div>
    </div>
    <div id="casting-research-toolbar"></div>
    <div id="casting-results"></div>
    <button id="casting-more" type="button" hidden>Load more</button>
    <div id="casting-pagination"></div>
    <details id="casting-activity"><summary>Casting activity</summary></details>
  </div>
  <div id="casting-review-modal">
    <div id="casting-review-position"></div>
    <div id="casting-review-progress"></div>
    <div id="casting-review-body"></div>
    <textarea id="casting-review-notes"></textarea>
    <button id="casting-review-save-notes" type="button"></button>
    <button id="casting-review-prev" type="button"></button>
    <button id="casting-review-next" type="button"></button>
    <button id="casting-review-play" type="button"></button>
    <button id="casting-review-shortlist" data-casting-status="shortlist" type="button"></button>
    <button id="casting-review-callback" data-casting-status="callback" type="button"></button>
    <button id="casting-review-reject" data-casting-status="reject" type="button"></button>
  </div>
</div>`;

const DEFAULTS = {status: 'unreviewed', priority: 'none', notes: '', tags: [], tag_colors: {}, stars: [], circles: [], collections: []};
const normalize = mark => {
    const full = {...DEFAULTS, ...(mark || {})};
    for (const key of ['tags', 'stars', 'circles', 'collections']) { full[key] = [...(full[key] || [])]; }
    full.tag_colors = {...(full.tag_colors && typeof full.tag_colors === 'object' && !Array.isArray(full.tag_colors) ? full.tag_colors : {})};
    return full;
};
const tags = (gender, ages, tones, textures = []) => ({gender, ages, tones, textures});
const VOICES = [
    {id: 'persona/dh/abel', source: 'persona', book_id: 'DH', name: 'ABEL', description: 'Calm, academic.',
     ref_text: 'Hello.', preview_url: '/designed_voices/persona/dh/abel/preview.wav', audio_present: true,
     tags: tags('Male', ['Adult'], ['calm', 'academic'], ['smooth'])},
    {id: 'persona/dh/aichel', source: 'persona', book_id: 'DH', name: 'AICHEL', description: 'Warm, polite.',
     ref_text: 'A pleasure.', preview_url: '/designed_voices/persona/dh/aichel/preview.wav', audio_present: true,
     tags: tags('Female', ['Adult'], ['warm', 'calm'], ['rough'])},
    {id: 'persona/lotm/cain', source: 'persona', book_id: 'LOTM', name: '<img src=x onerror=alert(1)>',
     description: '<script>alert(2)</script> cold', ref_text: '', preview_url: null, audio_present: false,
     tags: tags('Male', ['Young Adult'], ['cold'])},
    {id: 'designer/desk1', source: 'designer', book_id: 'Desk', name: 'DORA', description: 'Cold and dry.',
     ref_text: 'Fine.', preview_url: '/designed_voices/designer/desk1/preview.wav', audio_present: true,
     tags: tags('Female', ['Adult'], ['cold'])},
    {id: 'persona/dh/echo', source: 'persona', book_id: 'DH', name: 'ECHO', description: 'Warm teen.',
     ref_text: 'Hi.', preview_url: '/designed_voices/persona/dh/echo/preview.wav', audio_present: true,
     tags: tags('Female', ['Teen'], ['warm'], ['smooth'])},
];
const INITIAL_MARKS = {
    'persona/dh/abel': {status: 'shortlist'},
    'persona/lotm/cain': {tags: ['vip:gold', 'ensemble']},
    'designer/desk1': {tags: ['vip']},
    'persona/dh/echo': {status: 'callback', tags: ['keep'], stars: ['red', 'blue'], circles: ['blue'],
                        collections: ['Leads']},
};
const NOTES_SECRET = 'Synthetic secret notes';

let assertions = 0;
const plain = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const ok = (value, message = 'expected a truthy value') => { assert.ok(value, message); assertions++; };
const eq = (a, b, message = 'values differ') => { assert.deepStrictEqual(plain(a), plain(b), message); assertions++; };
const includes = (haystack, needle, message) => { assert.ok(String(haystack).includes(needle), message || ('missing ' + needle + ' in ' + haystack)); assertions++; };

function makeStore(initial = {}) {
    return {
        data: {...initial},
        getItem(key) { return Object.prototype.hasOwnProperty.call(this.data, key) ? this.data[key] : null; },
        setItem(key, value) { this.data[key] = String(value); },
        removeItem(key) { delete this.data[key]; },
    };
}

function setup({voices = VOICES, marks = INITIAL_MARKS, activity = [], confirm = true, storage = {},
                activityExtra = []} = {}) {
    const browserWindow = makeWindow(HTML);
    const calls = [], errors = [], toasts = [], timers = [], cleared = [], observers = [], audios = [];
    const serverMarks = {};
    for (const [id, mark] of Object.entries(marks)) { serverMarks[id] = normalize(mark); }
    const serverActivity = activityExtra.map((event, index) => ({id: 'event' + index, at: '2026-11-02T09:00:0' + index + '.000Z',
        action: event.action, count: event.count, ...(event.notes ? {notes: event.notes} : {})}));
    for (const event of activity) { serverActivity.push(event); }
    const store = makeStore(storage);
    const env = {w: browserWindow, calls, errors, toasts, timers, cleared, observers, audios, serverMarks, store, tabObservers: [],
                 audioMode: 'deferred', failNextBatch: false, confirm, batchGate: null,
                 voices: JSON.parse(JSON.stringify(voices))};
    browserWindow.localStorage = store;
    browserWindow.MutationObserver = class {
        constructor(callback) { this.callback = callback; env.tabObservers.push(this); }
        observe() {}
    };
    browserWindow.API = {
        get: async url => {
            calls.push(['GET', url]);
            if (url === '/api/casting/voices') {
                const visible = {};
                for (const voice of env.voices) { visible[voice.id] = JSON.parse(JSON.stringify(serverMarks[voice.id] || {})); }
                return {voices: JSON.parse(JSON.stringify(env.voices)), marks: visible, skipped: [],
                        activity: JSON.parse(JSON.stringify(serverActivity))};
            }
            if (url === '/api/voice_config/snapshot') { return {book_token: 'token', voices: [{name: 'ADAM'}, {name: 'EVE'}]}; }
            if (url === '/api/voice_library') { return {casts: [], shared: []}; }
            if (url.startsWith('/api/casting/clone_config')) { return {config: {type: 'clone', ref_audio: 'x.wav'}}; }
            throw new Error('unexpected GET ' + url);
        },
        post: async (url, body) => {
            calls.push(['POST', url, JSON.parse(JSON.stringify(body))]);
            if (url === '/api/casting/marks/batch') {
                if (env.batchGate) { await env.batchGate; }
                if (env.failNextBatch) {
                    env.failNextBatch = false;
                    throw new Error('network down');
                }
                const changes = body.changes;
                if (!Array.isArray(changes) || !changes.length || changes.length > 1000) { throw new Error('bad batch'); }
                const seen = new Set(), staged = {};
                for (const change of changes) {
                    if (seen.has(change.voice_id)) { throw new Error('duplicate voice'); }
                    seen.add(change.voice_id);
                    const patch = change.patch, expected = change.expected;
                    if (JSON.stringify(Object.keys(patch).sort()) !== JSON.stringify(Object.keys(expected).sort())) {
                        const error = new Error('expected must contain exactly the keys being changed');
                        error.status = 400;
                        throw error;
                    }
                    const current = normalize(serverMarks[change.voice_id]);
                    for (const key of Object.keys(expected)) {
                        if (JSON.stringify(current[key]) !== JSON.stringify(expected[key])) {
                            const error = new Error(change.voice_id + ' changed since it was read (' + key + ')');
                            error.status = 409;
                            throw error;
                        }
                    }
                    staged[change.voice_id] = {...(staged[change.voice_id] || current), ...patch};
                }
                for (const [id, mark] of Object.entries(staged)) { serverMarks[id] = normalize(mark); }
                const event = {id: 'event-' + (serverActivity.length + 1), at: '2026-11-03T10:00:0' + (serverActivity.length % 9) + '.000Z',
                               action: body.action, count: changes.length};
                serverActivity.push(event);
                if (serverActivity.length > 100) { serverActivity.shift(); }
                return {marks: JSON.parse(JSON.stringify(staged)), activity: JSON.parse(JSON.stringify(serverActivity))};
            }
            if (url === '/api/casting/marks') {
                const {voice_id, ...patch} = body;
                serverMarks[voice_id] = normalize({...(serverMarks[voice_id] || {}), ...patch});
                return {voice_id, mark: JSON.parse(JSON.stringify(serverMarks[voice_id]))};
            }
            if (url === '/api/voice_library/match') { return {book_token: 'token', proposals: []}; }
            if (url === '/api/voice_library/apply') { return {count: 1, warnings: []}; }
            return {status: 'ok'};
        },
    };
    browserWindow.showToast = (message, type) => toasts.push([type, message]);
    browserWindow.showActionError = (action, error, recovery) => errors.push([action, String(error && error.message || error), recovery || '']);
    browserWindow.showConfirm = async (message, options) => {
        assert.equal(typeof message, 'string');
        assert.equal(typeof options, 'object');
        return env.confirm;
    };
    browserWindow.stopDesignedVoicePlayback = () => {};
    browserWindow.setTimeout = (fn, ms) => { const id = timers.length + 1; timers.push({id, fn, ms, cancelled: false}); return id; };
    browserWindow.clearTimeout = id => { cleared.push(id); const timer = timers.find(item => item.id === id); if (timer) { timer.cancelled = true; } };
    browserWindow.IntersectionObserver = class {
        constructor(callback) { this.callback = callback; this.disconnected = false; this.observed = 0; observers.push(this); }
        observe() { this.observed++; }
        disconnect() { this.disconnected = true; }
    };
    browserWindow.Audio = class {
        constructor(src) {
            this.src = src;
            this.events = {};
            this.paused = false;
            this.ended = false;
            this.playCalls = 0;
            this.resolvePlay = null;
            audios.push(this);
        }
        addEventListener(name, callback) { (this.events[name] = this.events[name] || []).push(callback); }
        pause() { this.paused = true; }
        play() {
            this.playCalls++;
            if (env.audioMode === 'reject') { return Promise.reject(new Error('blocked by the browser')); }
            if (env.audioMode === 'resolve') { return Promise.resolve(); }
            return new Promise(resolve => { this.resolvePlay = resolve; });
        }
        emit(name) { for (const callback of this.events[name] || []) { callback(); } }
    };
    browserWindow.eval(research);
    browserWindow.eval(source);
    return env;
}

const settle = async () => { for (let index = 0; index < 16; index++) { await new Promise(resolve => setImmediate(resolve)); } };
const $ = (env, id) => env.w.document.getElementById('casting-' + id);
const input = (node, value) => { node.value = value; node.dispatchEvent({type: 'input'}); };
const change = async (env, node, value) => {
    if (node.type === 'checkbox') { node.checked = value; } else { node.value = String(value); }
    node.dispatchEvent({type: 'change'});
    await settle();
};
const click = async (env, node) => { node.click(); await settle(); };
const labels = env => $(env, 'results').querySelectorAll('strong').map(node => node.textContent);
const cardCheckbox = (env, id) => $(env, 'results').querySelectorAll('input').find(node => node.dataset.castingSelect === id);
const selectByLabel = (env, label) => $(env, 'results').querySelectorAll('select').find(node => node.attrs['aria-label'] === label);
const chip = (env, hostId, value) => $(env, hostId).querySelectorAll('button').find(node => node.textContent.startsWith(value + ' '));
const markButton = (env, id, shape, color) => $(env, 'results').querySelectorAll('button')
    .find(node => node.dataset.castingMark === id && node.dataset.shape === shape && node.dataset.color === color);
const batchCalls = env => env.calls.filter(call => call[1] === '/api/casting/marks/batch');
const firstBatch = env => batchCalls(env)[0];

(async () => {
    let env = setup();
    await env.w.loadCastingLibrary(); await settle();
    eq(labels(env).length, 5, 'every voice renders');
    ok($(env, 'research-toolbar').children.length > 0, 'research toolbar mounted');
    ok($(env, 'pagination').children.length > 0, 'pagination mounted');
    includes($(env, 'research-activity-list').textContent, 'No casting activity yet');
    ok(!String($(env, 'workspace').className).includes('casting-focus'));
    ok(!String($(env, 'workspace').className).includes('casting-hide-description'));
    const descriptionNode = $(env, 'results').querySelectorAll('p').find(node => String(node.className).includes('casting-description'));
    ok(descriptionNode, 'description hides through its casting-description class');
    ok($(env, 'results').querySelectorAll('p').some(node => String(node.className).includes('casting-sample')));
    ok($(env, 'results').querySelectorAll('div').some(node => String(node.className).includes('casting-tags')));

    let calm = chip(env, 'tones', 'calm');
    eq(calm.dataset.state, 'off');
    includes(calm.attrs['aria-label'], 'Off tone calm');
    await click(env, calm);
    calm = chip(env, 'tones', 'calm');
    eq(calm.dataset.state, 'include'); eq(calm.attrs['aria-pressed'], 'true');
    includes(calm.attrs['aria-label'], 'Include tone calm');
    eq(labels(env).sort(), ['ABEL', 'AICHEL']);
    await click(env, calm);
    calm = chip(env, 'tones', 'calm');
    eq(calm.dataset.state, 'exclude'); eq(calm.attrs['aria-pressed'], 'false');
    includes(calm.attrs['aria-label'], 'Exclude tone calm');
    eq(labels(env).sort(), ['DORA', 'ECHO', '<img src=x onerror=alert(1)>'].sort());
    await click(env, calm);
    eq(chip(env, 'tones', 'calm').dataset.state, 'off');
    eq(labels(env).length, 5);

    let tagChip = chip(env, 'research-filter-tag', 'vip:gold');
    ok(tagChip, 'colon tag has its own chip');
    eq(tagChip.textContent, 'vip:gold 1');
    await click(env, tagChip);
    eq(labels(env).sort(), ['<img src=x onerror=alert(1)>']);
    await click(env, chip(env, 'research-filter-tag', 'vip:gold'));
    await click(env, chip(env, 'research-filter-tag', 'vip:gold'));
    eq(labels(env).length, 5);

    await click(env, chip(env, 'tones', 'cold'));
    eq(labels(env).sort(), ['DORA', '<img src=x onerror=alert(1)>'].sort());
    let vipChip = chip(env, 'research-filter-tag', 'vip');
    await click(env, vipChip);
    await click(env, chip(env, 'research-filter-tag', 'vip'));
    eq(chip(env, 'research-filter-tag', 'vip').dataset.state, 'exclude');
    eq(labels(env).sort(), ['<img src=x onerror=alert(1)>']);
    await click(env, $(env, 'clear'));
    eq(labels(env).length, 5);

    let blue = chip(env, 'research-filter-circle', 'blue');
    await click(env, blue);
    eq(labels(env).sort(), ['ECHO']);
    await click(env, chip(env, 'research-filter-circle', 'blue'));
    eq(chip(env, 'research-filter-circle', 'blue').dataset.state, 'exclude');
    eq(labels(env).length, 5, 'staged exclusion does not hide cards yet');
    includes($(env, 'research-circle-pending').textContent, 'Refresh results');
    input($(env, 'search'), 'ABEL');
    eq(labels(env).sort(), ['ABEL'], 'other filters still apply while exclusions are pending');
    input($(env, 'search'), '');
    await click(env, $(env, 'research-refresh-circles'));
    eq(labels(env).length, 4, 'refresh applies the staged circle exclusion');
    ok(!labels(env).includes('ECHO'));
    includes($(env, 'research-circle-pending').textContent, 'blue');
    await click(env, chip(env, 'research-filter-circle', 'blue'));
    await click(env, $(env, 'research-refresh-circles'));
    eq(labels(env).length, 5, 'clearing the staged exclusion and refreshing restores the voice');
    eq($(env, 'research-circle-blue').checked, false);
    await change(env, $(env, 'research-circle-blue'), true);
    eq(chip(env, 'research-filter-circle', 'blue').dataset.state, 'exclude');
    eq(labels(env).length, 5, 'the checkbox stages without hiding a card');
    includes($(env, 'research-circle-pending').textContent, 'Refresh results');
    await change(env, $(env, 'research-circle-blue'), false);
    eq($(env, 'research-circle-blue').checked, false);

    await click(env, $(env, 'research-select-filtered'));
    includes($(env, 'research-selection-count').textContent, '5 selected of 5 filtered');
    await change(env, $(env, 'research-bulk-priority'), 'high');
    let batch = firstBatch(env);
    ok(batch, 'bulk priority posts one batch');
    eq(batch[2].changes.length, 5);
    eq(batch[2].changes[0].expected, {priority: 'none'}, 'expected uses canonical defaults');
    eq(Object.keys(batch[2].changes[0].patch), ['priority']);
    includes(batch[2].action, 'priority');
    eq(env.serverMarks['persona/dh/abel'].priority, 'high');
    ok(!$(env, 'research-undo').disabled, 'successful batch enables undo');
    await click(env, $(env, 'research-undo'));
    eq(batchCalls(env).at(-1)[2].action, 'Undo: Set priority for selected voices');
    eq(env.serverMarks['persona/dh/abel'].priority, 'none');
    ok(!$(env, 'research-redo').disabled);
    await click(env, $(env, 'research-redo'));
    eq(env.serverMarks['persona/dh/abel'].priority, 'high');
    await click(env, $(env, 'research-clear-selection'));
    includes($(env, 'research-selection-count').textContent, 'No voices selected');
    await click(env, $(env, 'research-select-page'));
    includes($(env, 'research-selection-count').textContent, '5 selected');
    await click(env, $(env, 'research-invert'));
    includes($(env, 'research-selection-count').textContent, 'No voices selected');
    await change(env, cardCheckbox(env, 'persona/dh/abel'), true);
    includes($(env, 'research-selection-count').textContent, '1 selected of 5 filtered');
    eq(env.w.CastingLibrary.state().research.state().selection.size, 1);

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    await click(env, $(env, 'research-select-filtered'));
    env.serverMarks['persona/dh/abel'].status = 'reject';
    const before = JSON.stringify(env.serverMarks);
    await change(env, $(env, 'research-bulk-status'), 'shortlist');
    eq(env.serverMarks['persona/dh/abel'].status, 'reject', 'conflicting batch wrote nothing');
    for (const id of Object.keys(env.serverMarks)) {
        eq(env.serverMarks[id].status === 'shortlist', false, 'no partial batch write for ' + id);
        }
    ok($(env, 'research-undo').disabled, 'a refused batch creates no undo entry');
    ok(env.toasts.some(([type, message]) => /Reload|reload/.test(message)), 'conflict explains how to reconcile');
    includes(before, '"reject"');

        env = setup();
        await env.w.loadCastingLibrary(); await settle();
    await change(env, cardCheckbox(env, 'persona/dh/echo'), true);
    await change(env, $(env, 'research-clear-shape'), 'stars');
    await change(env, $(env, 'research-clear-color'), 'red');
    env.confirm = false;
    let beforeCalls = batchCalls(env).length;
    await click(env, $(env, 'research-clear-marks'));
    eq(batchCalls(env).length, beforeCalls, 'declined clear writes nothing');
    env.confirm = true;
    await click(env, $(env, 'research-clear-marks'));
    batch = batchCalls(env).at(-1);
    eq(batch[2].changes.length, 1);
    eq(batch[2].changes[0].patch, {stars: ['blue']});
    eq(batch[2].changes[0].expected, {stars: ['red', 'blue']});
    eq(env.serverMarks['persona/dh/echo'].stars, ['blue']);
    eq(env.serverMarks['persona/dh/echo'].circles, ['blue']);
    eq(env.serverMarks['persona/dh/echo'].tags, ['keep']);
    eq(env.serverMarks['persona/dh/echo'].collections, ['Leads']);
    eq(env.serverMarks['persona/dh/echo'].status, 'callback');

    const starButton = markButton(env, 'persona/dh/echo', 'stars', 'blue');
    ok(!starButton.disabled);
    await change(env, $(env, 'research-lock-stars'), true);
    ok(markButton(env, 'persona/dh/echo', 'stars', 'blue').disabled, 'locked star cards are disabled');
    ok($(env, 'research-bulk-mark-add').disabled, 'locked stars block the bulk mark add');
    ok($(env, 'research-clear-marks').disabled, 'locked stars block clearing star marks');
    beforeCalls = batchCalls(env).length;
    markButton(env, 'persona/dh/echo', 'stars', 'blue').click();
    await settle();
    eq(batchCalls(env).length, beforeCalls, 'a locked star control cannot write');
    ok(!markButton(env, 'persona/dh/echo', 'circles', 'blue').disabled, 'circle controls stay available');
    const lockedStorage = {...env.store.data};
    let locked = setup({storage: lockedStorage});
    await locked.w.loadCastingLibrary(); await settle();
    ok($(locked, 'research-lock-stars').checked, 'lock preference survives a reload');
    ok(markButton(locked, 'persona/dh/echo', 'stars', 'blue').disabled);

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    for (const shape of ['stars', 'circles']) {
        for (const color of ['red', 'amber', 'green', 'blue', 'purple']) {
            const control = markButton(env, 'persona/dh/abel', shape, color);
            eq(control.textContent, '', 'card marks display symbols only');
            eq(control.parentElement.querySelectorAll('span').length, 0, 'card mark rows omit English headings');
            includes(control.attrs['aria-label'], (shape === 'stars' ? 'Star' : 'Circle') + ' ' + color + ' for ABEL: off');
            eq(control.attrs.title, control.attrs['aria-label']);
            eq(control.attrs['aria-pressed'], 'false');
            const icon = control.querySelectorAll('i')[0];
            includes(icon.className, shape === 'stars' ? 'fa-star' : 'fa-circle');
            eq(icon.attrs['aria-hidden'], 'true');
        }
    }
    await click(env, markButton(env, 'persona/dh/abel', 'stars', 'green'));
    batch = batchCalls(env).at(-1);
    eq(batch[2].changes[0].patch, {stars: ['green']});
    eq(batch[2].changes[0].expected, {stars: []});
    includes(markButton(env, 'persona/dh/abel', 'stars', 'green').attrs['aria-label'], 'Star green for ABEL: on');
    await click(env, markButton(env, 'persona/dh/abel', 'stars', 'green'));
    eq(env.serverMarks['persona/dh/abel'].stars, []);
    await click(env, $(env, 'view-table'));
    eq(markButton(env, 'persona/dh/abel', 'stars', 'green').textContent, 'green', 'table controls retain their labels');
    await click(env, $(env, 'view-cards'));
    eq(markButton(env, 'persona/dh/abel', 'stars', 'green').textContent, '', 'card controls remain symbol-only after changing views');
    const tagInput = $(env, 'results').querySelectorAll('input').find(node => node.attrs['aria-label'] === 'New tag for ABEL');
    tagInput.value = 'scene:one';
    const tagAdd = $(env, 'results').querySelectorAll('button').find(node => node.attrs['aria-label'] === 'Add tag to ABEL');
    await click(env, tagAdd);
    eq(env.serverMarks['persona/dh/abel'].tags, ['scene:one']);
    includes($(env, 'results').textContent, 'scene:one');
    const removeTag = $(env, 'results').querySelectorAll('button').find(node => node.attrs['aria-label'] === 'Remove tag scene:one from ABEL');
    await click(env, removeTag);
    eq(env.serverMarks['persona/dh/abel'].tags, []);

    await change(env, cardCheckbox(env, 'persona/dh/abel'), true);
    $(env, 'research-bulk-collection').value = 'Leads';
    await click(env, $(env, 'research-bulk-collection-add'));
    eq(env.serverMarks['persona/dh/abel'].collections, ['Leads']);
    await click(env, chip(env, 'research-filter-collection', 'Leads'));
    eq(labels(env).sort(), ['ABEL', 'ECHO'], 'research collection chips filter by membership');
    const removeMember = $(env, 'results').querySelectorAll('button').find(node => node.attrs['aria-label'] === 'Remove ABEL from research collection Leads');
    await click(env, removeMember);
    eq(env.serverMarks['persona/dh/abel'].collections, []);
    ok(!env.calls.some(call => call[1] === '/api/voice_library/apply'), 'research collections never apply saved casts');
    await click(env, $(env, 'clear'));
    eq(labels(env).length, 5);

    // Card status/priority bar, custom tag badges beside voice tags, persistent tag colours.
    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    let bar = $(env, 'results').querySelector('[data-casting-status-bar="persona/dh/abel"]');
    ok(bar, 'the card renders a status bar below the name');
    eq(bar.children.map(node => node.textContent), ['Shortlist', 'No priority']);
    ok(bar.children.every(node => !String(node.className).includes('flex-fill')), 'Bootstrap auto-basis must not override the equal-width rule');
    includes(bar.children[0].className, 'bg-success');
    includes(bar.children[1].className, 'bg-secondary');
    eq(env.w.CastingLibrary.state().marks['persona/dh/abel'].tag_colors, {}, 'old marks default to neutral colours');
    let colourInput = $(env, 'results').querySelectorAll('input').find(node => node.attrs['aria-label'] === 'New tag for ABEL');
    colourInput.value = 'scene:one';
    let colourAdd = $(env, 'results').querySelectorAll('button').find(node => node.attrs['aria-label'] === 'Add tag to ABEL');
    await click(env, colourAdd);
    let colourCustom = $(env, 'results').querySelector('[data-casting-custom-tag="scene:one"]');
    ok(colourCustom, 'the custom tag appears as a badge on the card');
    ok(String(colourCustom.className).includes('casting-tag-neutral'), 'a tag without a colour uses theme tokens');
    ok(colourCustom.parentElement.children.some(node => node.textContent.includes('Male')),
       'custom tags sit beside the original voice tags');
    let colourPicker = selectByLabel(env, 'Colour for tag scene:one on ABEL');
    ok(colourPicker, 'the Tags & collection editor offers a colour for each tag');
    colourPicker.focus();
    await change(env, colourPicker, 'red');
    batch = batchCalls(env).at(-1);
    eq(batch[2].changes[0].patch, {tags: ['scene:one'], tag_colors: {'scene:one': 'red'}});
    eq(batch[2].changes[0].expected, {tags: ['scene:one'], tag_colors: {}});
    let focusedDetails = env.w.document.activeElement.parentElement;
    while (focusedDetails && focusedDetails.tagName.toUpperCase() !== 'DETAILS') { focusedDetails = focusedDetails.parentElement; }
    ok(focusedDetails && focusedDetails.open, 'restored colour focus must be inside an open editor');
    eq(env.serverMarks['persona/dh/abel'].tag_colors, {'scene:one': 'red'});
    includes($(env, 'results').querySelector('[data-casting-custom-tag="scene:one"]').className, 'bg-danger');
    eq(selectByLabel(env, 'Colour for tag scene:one on ABEL').value, 'red');
    eq(env.serverMarks['persona/dh/echo'].stars, ['red', 'blue'], 'other voices keep their marks');
    await env.w.loadCastingLibrary(); await settle();
    includes($(env, 'results').querySelector('[data-casting-custom-tag="scene:one"]').className, 'bg-danger',
             'the tag colour survives a reload');
    eq(selectByLabel(env, 'Colour for tag scene:one on ABEL').value, 'red');
    await click(env, $(env, 'research-undo'));
    eq(env.serverMarks['persona/dh/abel'].tag_colors, {});
    ok(String($(env, 'results').querySelector('[data-casting-custom-tag="scene:one"]').className).includes('casting-tag-neutral'),
       'undo restores the neutral colour');
    await click(env, $(env, 'research-redo'));
    eq(env.serverMarks['persona/dh/abel'].tag_colors, {'scene:one': 'red'});

    const colourRemove = $(env, 'results').querySelectorAll('button')
        .find(node => node.attrs['aria-label'] === 'Remove tag scene:one from ABEL');
    await click(env, colourRemove);
    batch = batchCalls(env).at(-1);
    eq(batch[2].changes[0].patch, {tags: [], tag_colors: {}}, 'removing a tag removes its colour in the same write');
    eq(batch[2].changes[0].expected, {tags: ['scene:one'], tag_colors: {'scene:one': 'red'}});
    eq(env.serverMarks['persona/dh/abel'].tags, []);
    eq(env.serverMarks['persona/dh/abel'].tag_colors, {});
    ok(!$(env, 'results').querySelector('[data-casting-custom-tag="scene:one"]'), 'the removed tag badge disappears');
    await click(env, $(env, 'research-undo'));
    eq(env.serverMarks['persona/dh/abel'].tags, ['scene:one']);
    eq(env.serverMarks['persona/dh/abel'].tag_colors, {'scene:one': 'red'});
    includes($(env, 'results').querySelector('[data-casting-custom-tag="scene:one"]').className, 'bg-danger',
             'undo restores the removed tag and its colour');
    await click(env, $(env, 'research-redo'));
    eq(env.serverMarks['persona/dh/abel'].tags, []);
    eq(env.serverMarks['persona/dh/abel'].tag_colors, {});

    env.serverMarks['persona/dh/abel'].tags = ['scene:one'];
    const beforeTagConflict = batchCalls(env).length;
    await click(env, $(env, 'research-undo'));
    eq(batchCalls(env).length, beforeTagConflict + 1, 'a conflicted tag undo is not retried');
    eq(env.serverMarks['persona/dh/abel'].tag_colors, {}, 'the conflicting undo did not write a stale colour');
    ok($(env, 'research-undo').disabled, 'tag conflicts require reconciliation');
    ok(env.toasts.some(([type, message]) => /changed elsewhere/.test(message)));

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    await change(env, cardCheckbox(env, 'persona/dh/echo'), true);
    await change(env, selectByLabel(env, 'Colour for tag keep on ECHO'), 'green');
    eq(env.serverMarks['persona/dh/echo'].tag_colors, {'keep': 'green'});
    $(env, 'research-bulk-tag').value = 'keep';
    await click(env, $(env, 'research-bulk-tag-remove'));
    batch = batchCalls(env).at(-1);
    eq(batch[2].changes[0].patch, {tags: [], tag_colors: {}},
       'bulk tag removal clears that tag colour in the same write');
    eq(env.serverMarks['persona/dh/echo'].tags, []);
    eq(env.serverMarks['persona/dh/echo'].tag_colors, {});
    await click(env, $(env, 'research-undo'));
    eq(env.serverMarks['persona/dh/echo'].tags, ['keep']);
    eq(env.serverMarks['persona/dh/echo'].tag_colors, {'keep': 'green'});

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    const statusSelect = selectByLabel(env, 'Review status for ABEL');
    await change(env, statusSelect, 'callback');
    batch = batchCalls(env).at(-1);
    eq(batch[2].changes[0].patch, {status: 'callback'});
    eq(batch[2].changes[0].expected, {status: 'shortlist'});
    eq(env.serverMarks['persona/dh/abel'].status, 'callback');
    await click(env, $(env, 'research-undo'));
    eq(env.serverMarks['persona/dh/abel'].status, 'shortlist');

    await click(env, $(env, 'research-select-filtered'));
    await change(env, $(env, 'research-bulk-priority'), 'high');
    await click(env, $(env, 'research-undo'));
    env.serverMarks['persona/dh/abel'].status = 'reject';
    await click(env, $(env, 'research-redo'));
    eq(env.serverMarks['persona/dh/abel'].priority, 'high');
    eq(env.serverMarks['persona/dh/abel'].status, 'reject', 'unrelated external field is preserved');
    eq(env.w.CastingLibrary.state().marks['persona/dh/abel'].status, 'reject');
    env.serverMarks['persona/dh/abel'].priority = 'low';
    const beforeConflict = batchCalls(env).length;
    await click(env, $(env, 'research-undo'));
    eq(batchCalls(env).length, beforeConflict + 1, 'a conflicted undo is not retried');
    eq(env.serverMarks['persona/dh/abel'].priority, 'low');
    ok($(env, 'research-undo').disabled, 'conflicted writes require reconciliation before retry');
    ok(env.w.CastingLibrary.state().research.state().undo.length > 0, 'the conflicting entry is retained');
    ok(env.toasts.some(([type, message]) => /changed elsewhere/.test(message)));

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    await change(env, cardCheckbox(env, 'persona/dh/abel'), true);
    env.failNextBatch = true;
    await change(env, $(env, 'research-bulk-status'), 'shortlist');
    eq(batchCalls(env).length, 1, 'a network failure is not retried');
    eq(env.serverMarks['persona/dh/abel'].status, 'shortlist', 'the failed write changed nothing');
    ok(env.errors.some(entry => /Reload/.test(entry[2]) || /Reload/.test(entry[1])), 'recovery tells the user to reload');
    ok($(env, 'research-undo').disabled, 'failed write adds no undo entry');
    await change(env, $(env, 'research-bulk-status'), 'shortlist');
    eq(batchCalls(env).length, 1, 'writes stay blocked until reload');
    await env.w.loadCastingLibrary(); await settle();
    await change(env, cardCheckbox(env, 'persona/dh/abel'), true);
    await change(env, $(env, 'research-bulk-status'), 'shortlist');
    eq(env.serverMarks['persona/dh/abel'].status, 'shortlist', 'reload clears the reconcile block');
    eq(batchCalls(env).length, 2, 'the retried write is a single new request');

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    await change(env, cardCheckbox(env, 'persona/dh/abel'), true);
    env.batchGate = new Promise(resolve => { env.releaseBatch = resolve; });
    await change(env, $(env, 'research-bulk-status'), 'callback');
    await env.w.loadCastingLibrary(); await settle();
    env.releaseBatch(); await settle();
    eq(env.w.CastingLibrary.state().marks['persona/dh/abel'].status, 'shortlist',
       'a stale batch reply cannot repaint the reloaded library');
    ok($(env, 'research-undo').disabled, 'a stale batch reply adds no undo entry');

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    await change(env, cardCheckbox(env, 'persona/dh/abel'), true);
    env.batchGate = new Promise(resolve => { env.releaseBatch = resolve; });
    await change(env, $(env, 'research-bulk-priority'), 'high');
    await change(env, $(env, 'research-bulk-status'), 'callback');
    eq(batchCalls(env).length, 1, 'the second write waits for the first reply');
    eq(env.serverMarks['persona/dh/abel'].priority, 'none', 'nothing is written before the first reply');
    env.releaseBatch(); await settle();
    eq(batchCalls(env).length, 2);
    eq(env.serverMarks['persona/dh/abel'].priority, 'high');
    eq(env.serverMarks['persona/dh/abel'].status, 'callback');
    const secondBatch = batchCalls(env).at(-1);
    eq(Object.keys(secondBatch[2].changes[0].patch), ['status'], 'the queued write re-reads field-specific expectations');
    eq(secondBatch[2].changes[0].expected, {status: 'shortlist'});

    const many = [];
    for (let index = 0; index < 60; index++) {
        many.push({id: 'designer/voice' + index, source: 'designer', book_id: 'Book', name: 'Voice ' + String(index).padStart(2, '0'),
                   description: 'Synthetic.', ref_text: 'Line.', preview_url: '/v/' + index + '.wav', audio_present: true,
                   tags: tags('Female', ['Adult'], ['calm'])});
    }
    env = setup({voices: many});
    await env.w.loadCastingLibrary(); await settle();
    eq(labels(env).length, 48, 'default page size is 48');
    includes($(env, 'research-page-status').textContent, 'Showing 1–48 of 60');
    await change(env, cardCheckbox(env, 'designer/voice0'), true);
    await change(env, $(env, 'research-mode'), 'pages');
    eq(labels(env).length, 48);
    await click(env, $(env, 'research-page-next'));
    eq(labels(env).length, 12);
    includes($(env, 'research-page-status').textContent, 'page 2 of 2');
    includes($(env, 'research-selection-count').textContent, '1 selected', 'selection survives pagination');
    await click(env, $(env, 'research-page-prev'));
    eq(labels(env).length, 48);
    await change(env, $(env, 'research-page-size'), '24');
    eq(labels(env).length, 24);
    await click(env, $(env, 'research-show-all'));
    eq(labels(env).length, 60);
    await change(env, $(env, 'research-mode'), 'load');
    eq(labels(env).length, 24);
    await click(env, $(env, 'research-load-more'));
    eq(labels(env).length, 48);
    eq(env.observers.length, 0, 'Load more never starts automatic scrolling');
    await change(env, $(env, 'research-mode'), 'auto');
    const observer = env.observers.at(-1);
    ok(observer && !observer.disconnected, 'Auto-load observes an explicit sentinel');
    observer.callback([{isIntersecting: true}]);
    await settle();
    eq(labels(env).length, 60);
    ok(env.observers.at(-1).disconnected || observer.disconnected, 'observer disconnects when everything is loaded');
    let hiddenEnv = setup({voices: many});
    await hiddenEnv.w.loadCastingLibrary(); await settle();
    await change(hiddenEnv, $(hiddenEnv, 'research-mode'), 'auto');
    const hiddenObserver = hiddenEnv.observers.at(-1);
    ok(hiddenObserver && !hiddenObserver.disconnected);
    $(hiddenEnv, 'tab').style.display = 'none';
    hiddenObserver.callback([{isIntersecting: true}]);
    await settle();
    eq(labels(hiddenEnv).length, 48, 'a hidden tab does not auto-load');
    $(hiddenEnv, 'tab').style.display = '';
    hiddenObserver.callback([{isIntersecting: true}]);
    await settle();
    eq(labels(hiddenEnv).length, 60, 'an active tab auto-loads one page');
    await env.w.loadCastingLibrary(); await settle();
    eq(labels(env).length, 24);
    await change(env, $(env, 'research-page-size'), '96');
    eq(labels(env).length, 60);

    const huge = [];
    for (let index = 0; index < 1001; index++) {
        huge.push({id: 'designer/huge' + index, source: 'designer', book_id: 'Book', name: 'Huge ' + index,
                   description: 'Synthetic.', ref_text: '', preview_url: '/h/' + index + '.wav', audio_present: false,
                   tags: tags('Male', ['Adult'], [])});
    }
    env = setup({voices: huge});
    await env.w.loadCastingLibrary(); await settle();
    await click(env, $(env, 'research-select-filtered'));
    includes($(env, 'research-selection-count').textContent, 'No voices selected', 'select-all above the cap is refused');
    ok(env.toasts.some(([type, message]) => /1000/.test(message)), 'the refusal names the limit');
    await click(env, $(env, 'research-show-all'));
    includes($(env, 'research-guard').textContent, '1000', 'show all reports its guard instead of dropping voices');

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    await click(env, $(env, 'play-all'));
    eq(env.audios.length, 1);
    includes($(env, 'playback-status').textContent, 'Playing ABEL');
    eq(env.timers.length, 0, 'no timer before playback is confirmed');
    env.audios[0].resolvePlay(); await settle();
    eq(env.timers.length, 1);
    eq(env.timers[0].ms, 8000, 'default duration is 8 seconds');
    const firstAudio = env.audios[0];
    env.timers[0].fn(); await settle();
    eq(env.audios.length, 2, 'timer advances the queue');
    includes($(env, 'playback-status').textContent, 'AICHEL');
    env.audios[1].resolvePlay(); await settle();
    firstAudio.emit('ended'); await settle();
    eq(env.audios.length, 2, 'an ended event from a stale audio cannot advance the new item');
    await change(env, $(env, 'research-play-seconds'), '120');
    eq(env.w.CastingLibrary.state().playSeconds, 120);
    eq(env.w.CastingLibrary.state().queue, null, 'changing the duration stops the queue');
    await click(env, $(env, 'play-all'));
    await settle();
    env.audios.at(-1).resolvePlay(); await settle();
    const longTimer = env.timers.at(-1);
    ok(longTimer && !longTimer.cancelled);
    eq(longTimer.ms, 120000, 'the configured duration drives the timer');
    await click(env, $(env, 'play-all'));
    eq(env.w.CastingLibrary.state().queue, null);
    eq($(env, 'play-all').textContent, 'Play all');
    ok(env.cleared.length >= 1, 'stopping clears the pending timer');
    const timersBeforeLate = env.timers.length;
    env.audios.at(-1).resolvePlay(); await settle();
    eq(env.timers.length, timersBeforeLate, 'a late play success cannot restart the queue');
    await change(env, $(env, 'research-play-seconds'), '500');
    includes(env.toasts.at(-1)[1], '0 for full samples');
    eq($(env, 'research-play-seconds').value, '120', 'invalid durations revert');
    await change(env, $(env, 'research-play-seconds'), '0');
    eq(env.w.CastingLibrary.state().playFull, true, '0 seconds selects full samples');
    eq($(env, 'research-play-seconds').value, '0');
    await change(env, $(env, 'research-play-seconds'), '3');
    eq(env.w.CastingLibrary.state().playSeconds, 3);
    eq(env.w.CastingLibrary.state().playFull, false);
    await change(env, $(env, 'research-play-full'), true);
    await click(env, $(env, 'play-all'));
    await settle();
    const timersBeforeFull = env.timers.length;
    env.audios.at(-1).resolvePlay(); await settle();
    eq(env.timers.length, timersBeforeFull, 'full samples wait for the ended event');
    const fullAudio = env.audios.at(-1);
    const audiosBeforeEnded = env.audios.length;
    fullAudio.ended = true;
    fullAudio.paused = true;
    fullAudio.emit('pause'); await settle();
    ok(env.w.CastingLibrary.state().queue, 'natural completion preserves the queue until ended');
    fullAudio.emit('ended'); await settle();
    eq(env.audios.length, audiosBeforeEnded + 1, 'full samples still advance on ended');
    const pausedAudio = env.audios.at(-1);
    pausedAudio.emit('pause'); await settle();
    eq(env.w.CastingLibrary.state().queue, null, 'an external pause before completion stops the queue');
    pausedAudio.emit('ended'); await settle();
    eq(env.audios.length, audiosBeforeEnded + 1, 'ended after an external pause cannot restart the queue');
    await click(env, $(env, 'play-all'));
    await click(env, $(env, 'play-all'));
    await change(env, $(env, 'research-play-full'), false);
    env.audioMode = 'reject';
    const timersBeforeReject = env.timers.length;
    await click(env, $(env, 'play-all'));
    await settle();
    includes(JSON.stringify(env.errors), 'Play voice');
    eq(env.w.CastingLibrary.state().queue, null, 'a rejected play() stops the queue');
    eq(env.timers.length, timersBeforeReject, 'a rejected play() schedules nothing');
    env.audioMode = 'deferred';
    await click(env, $(env, 'play-all'));
    eq(env.audios.at(-1).src, '/designed_voices/persona/dh/abel/preview.wav');
    env.audios.at(-1).resolvePlay(); await settle();
    const pendingTimer = env.timers.at(-1);
    ok(pendingTimer && !pendingTimer.cancelled);
    await click(env, $(env, 'results').querySelectorAll('button').find(node => node.dataset.castingPlay === 'persona/dh/aichel'));
    eq(env.w.CastingLibrary.state().queue, null, 'a card preview stops the queue');
    input($(env, 'search'), 'ECHO');
    await settle();
    ok(pendingTimer.cancelled, 'changing filters cancels the queue timer');
    eq(env.w.CastingLibrary.state().queue, null);
    input($(env, 'search'), '');
    await click(env, $(env, 'research-play-random'));
    const randomQueue = env.w.CastingLibrary.state().queue;
    ok(randomQueue && ['persona/dh/abel', 'persona/dh/aichel', 'designer/desk1', 'persona/dh/echo']
        .includes(randomQueue.list[randomQueue.index].id), 'random picks from filtered audio voices');
    eq(randomQueue.list.length, 4);
    await click(env, $(env, 'play-all'));

    env = setup({storage: {castingResearchViewsV1: '{not json'}});
    await env.w.loadCastingLibrary(); await settle();
    eq($(env, 'research-view').querySelectorAll('option').length, 1, 'corrupt saved views are ignored');
    input($(env, 'search'), 'calm');
    await click(env, chip(env, 'tones', 'calm'));
    $(env, 'research-view-name').value = '<img src=x onerror=alert(3)> focus';
    await click(env, $(env, 'research-view-save'));
    ok(Object.keys(env.store.data).includes('castingResearchViewsV1'));
    const saved = JSON.parse(env.store.data.castingResearchViewsV1);
    eq(saved.length, 1);
    eq(saved[0].name, '<img src=x onerror=alert(3)> focus');
    eq(saved[0].chips, [{field: 'tone', value: 'calm', state: 'include'}]);
    eq($(env, 'research-view').querySelectorAll('option').at(-1).textContent, '<img src=x onerror=alert(3)> focus');
    eq($(env, 'research-toolbar').querySelectorAll('img').length, 0, 'view names render as text');
    eq($(env, 'research-toolbar').querySelectorAll('script').length, 0);
    input($(env, 'search'), '');
    await click(env, $(env, 'clear'));
    await click(env, $(env, 'play-all'));
    $(env, 'research-view').value = '<img src=x onerror=alert(3)> focus';
    await click(env, $(env, 'research-view-apply'));
    eq($(env, 'search').value, 'calm');
    eq(chip(env, 'tones', 'calm').dataset.state, 'include');
    eq(env.w.CastingLibrary.state().queue, null, 'applying a view stops the queue');
    const manyViews = [];
    for (let index = 0; index < 100; index++) {
        manyViews.push({name: 'View ' + index, search: '', selects: {}, sort: 'name', chips: [], circleExclude: [],
                        pageSize: 48, seconds: 8, full: false});
    }
    env.store.data.castingResearchViewsV1 = JSON.stringify(manyViews);
    $(env, 'research-view-name').value = 'One too many';
    await click(env, $(env, 'research-view-save'));
    includes(env.toasts.at(-1)[1], '100');
    eq(JSON.parse(env.store.data.castingResearchViewsV1).length, 100);

    env = setup({activityExtra: [{action: 'Set status <script>alert(9)</script>', count: 2, notes: NOTES_SECRET},
                                 {action: 'Clear marks', count: 1}]});
    await env.w.loadCastingLibrary(); await settle();
    const activityText = $(env, 'research-activity-list').textContent;
    includes(activityText, 'Clear marks');
    includes(activityText, 'Set status <script>alert(9)</script>');
    ok(!activityText.includes(NOTES_SECRET), 'activity never shows notes');
    eq($(env, 'research-activity-list').querySelectorAll('script').length, 0);
    ok(activityText.indexOf('Clear marks') < activityText.indexOf('Set status'), 'newest activity renders first');
    await change(env, cardCheckbox(env, 'persona/dh/abel'), true);
    await change(env, $(env, 'research-bulk-status'), 'shortlist');
    includes($(env, 'research-activity-list').textContent, '2026-11-03 10:00:');

    const descriptionPart = $(env, 'results').querySelector('[data-casting-visibility="description"]');
    const tagsPart = $(env, 'results').querySelector('[data-casting-visibility="tags"]');
    ok(descriptionPart && !descriptionPart.hidden);
    await change(env, $(env, 'research-show-description'), false);
    ok(String($(env, 'workspace').className).includes('casting-hide-description'));
    ok($(env, 'results').querySelector('[data-casting-visibility="description"]').hidden, 'the description is hidden');
    await change(env, $(env, 'research-show-tags'), false);
    ok(String($(env, 'workspace').className).includes('casting-hide-tags'));
    ok($(env, 'results').querySelector('[data-casting-visibility="tags"]').hidden, 'tags are hidden');
    ok(tagsPart);
    await change(env, $(env, 'research-focus'), true);
    ok(String($(env, 'workspace').className).includes('casting-focus'));
    const prefStorage = {...env.store.data};
    let prefsEnv = setup({storage: prefStorage});
    await prefsEnv.w.loadCastingLibrary(); await settle();
    ok(String($(prefsEnv, 'workspace').className).includes('casting-hide-description'));
    ok(String($(prefsEnv, 'workspace').className).includes('casting-focus'));
    ok(!$(prefsEnv, 'research-show-description').checked);
    env = setup();
    Object.defineProperty(env.w, 'localStorage', {get() { throw new Error('blocked'); }});
    await env.w.loadCastingLibrary(); await settle();
    eq(labels(env).length, 5, 'guarded storage failure still renders the library');

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
    await click(env, $(env, 'view-table'));
    const headers = $(env, 'results').querySelectorAll('th').map(node => node.textContent);
    includes(headers.join('|'), 'Marks');
    ok($(env, 'results').querySelectorAll('input').some(node => node.dataset.castingSelect === 'persona/dh/abel'));
    ok($(env, 'results').querySelectorAll('button').some(node => node.dataset.castingMark === 'persona/dh/echo'));
    await change(env, cardCheckbox(env, 'persona/dh/abel'), true);
    includes($(env, 'research-selection-count').textContent, '1 selected');
    await click(env, markButton(env, 'persona/dh/abel', 'circles', 'purple'));
    eq(env.serverMarks['persona/dh/abel'].circles, ['purple']);
    includes(headers.join('|'), 'Status & prio', 'the table names both status and priority');
    let tablePriority = selectByLabel(env, 'Priority for ABEL');
    ok(tablePriority, 'the table offers a priority dropdown');
    const prioCell = tablePriority.parentElement.parentElement;
    eq(prioCell.dataset.castingStatusPrio, 'persona/dh/abel');
    eq(prioCell.children[0].children.map(node => node.tagName), ['select', 'select']);
    eq(prioCell.children[0].children[0].value, 'shortlist', 'status stays first in the stacked cell');
    eq(tablePriority.value, 'none', 'priority starts at the readable no-priority value');
    tablePriority.focus();
    await change(env, tablePriority, 'high');
    let tableBatch = batchCalls(env).at(-1);
    eq(tableBatch[2].changes[0].patch, {priority: 'high'});
    eq(tableBatch[2].changes[0].expected, {priority: 'none'});
    eq(env.serverMarks['persona/dh/abel'].priority, 'high');
    eq(env.w.document.activeElement && env.w.document.activeElement.dataset.castingFocus,
       'priority:persona/dh/abel', 'the table priority keeps the same focus contract');
    const tableStatus = selectByLabel(env, 'Review status for ABEL');
    await change(env, tableStatus, 'callback');
    eq(env.serverMarks['persona/dh/abel'].status, 'callback');
    eq(env.serverMarks['persona/dh/abel'].priority, 'high', 'status saves do not overwrite priority');

    await click(env, $(env, 'view-cards'));
    const cardBar = $(env, 'results').querySelector('[data-casting-status-bar="persona/dh/abel"]');
    eq(cardBar.children.map(node => node.textContent), ['Callback', 'High priority']);
    includes(cardBar.children[0].className, 'bg-warning');
    includes(cardBar.children[1].className, 'bg-danger');
    const focusSelect = selectByLabel(env, 'Review status for ABEL');
    focusSelect.focus();
    await click(env, markButton(env, 'persona/dh/abel', 'stars', 'red'));
    eq(env.w.document.activeElement && env.w.document.activeElement.dataset.castingFocus,
       'status:persona/dh/abel', 'focus returns to the same field after a re-render');
    selectByLabel(env, 'Priority for ABEL').focus();
    await click(env, markButton(env, 'persona/dh/abel', 'stars', 'blue'));
    eq(env.w.document.activeElement && env.w.document.activeElement.dataset.castingFocus,
       'priority:persona/dh/abel', 'card priority focus survives a re-render');

    env = setup({marks: {'persona/dh/abel': {tags: ['<img src=x onerror=alert(4)>'], collections: ['<i>bad</i>'],
                                               stars: ['red'],
                                               tag_colors: JSON.parse('{"<img src=x onerror=alert(4)>":"<script>alert(5)</script>","__proto__":"red"}')}}});
    await env.w.loadCastingLibrary(); await settle();
    eq($(env, 'results').querySelectorAll('img').length, 0);
    eq($(env, 'results').querySelectorAll('script').length, 0);
    includes($(env, 'results').textContent, '<img src=x onerror=alert(4)>');
    const evilTag = $(env, 'results').querySelectorAll('[data-casting-custom-tag]')[0];
    ok(evilTag, 'a hostile tag name still renders as a badge');
    includes(evilTag.textContent, '<img src=x onerror=alert(4)>');
    ok(String(evilTag.className).includes('casting-tag-neutral'), 'a colour outside the palette falls back to neutral');
    eq(selectByLabel(env, 'Colour for tag <img src=x onerror=alert(4)> on ABEL').value, '');
    ok(Object.prototype.hasOwnProperty.call(env.w.CastingLibrary.markOf(
           env.w.CastingLibrary.state().marks, 'persona/dh/abel').tag_colors, '__proto__'),
       'a hostile tag_colours key stays an own data property');
    const evilStar = markButton(env, 'persona/dh/abel', 'stars', 'red');
    includes(evilStar.attrs['aria-label'], 'Star red for ABEL: on');

    {
        env = setup({storage: {castingResearchPrefsV1: JSON.stringify({seconds: 15, full: false})}});
        await env.w.loadCastingLibrary(); await settle();
        eq(env.w.CastingLibrary.state().playSeconds, 15, 'persisted duration reaches the audition owner');
        await click(env, $(env, 'play-all'));
        env.audios.at(-1).resolvePlay(); await settle();
        eq(env.timers.at(-1).ms, 15000);
        includes($(env, 'playback-status').textContent, 'Playing');
        $(env, 'tab').style.display = 'none';
        env.tabObservers[0].callback(); await settle();
        eq(env.w.CastingLibrary.state().queue, null, 'leaving the application tab cancels the queue');
        env = setup({storage: {castingResearchPrefsV1: JSON.stringify({seconds: 15, full: true})}});
        await env.w.loadCastingLibrary(); await settle();
        eq(env.w.CastingLibrary.state().playFull, true);
        await click(env, $(env, 'play-all'));
        env.audios.at(-1).resolvePlay(); await settle();
        eq(env.timers.length, 0, 'restored full samples never schedule a cutoff');
        env.audios.at(-1).emit('error'); await settle();
        eq(env.w.CastingLibrary.state().queue, null, 'a media error stops continuous playback');

        env = setup({storage: {castingResearchViewsV1: JSON.stringify([{name: 'Research', description: false,
            sample: false, tags: false, focus: true, pageSize: 24, seconds: 15, full: false}])}});
        await env.w.loadCastingLibrary(); await settle();
        env.w.CastingLibrary.state().variant = 'absent name';
        $(env, 'research-view').value = 'Research';
        await click(env, $(env, 'research-view-apply'));
        eq(env.w.CastingLibrary.state().variant, '', 'a view clears unrelated name-group filters');
        eq($(env, 'research-show-description').checked, false);
        eq($(env, 'research-show-sample').checked, false);
        eq($(env, 'research-show-tags').checked, false);
        eq($(env, 'research-focus').checked, true, 'the focus exit control reflects restored state');
        eq($(env, 'research-page-size').value, '24');

        env = setup({voices: many});
        await env.w.loadCastingLibrary(); await settle();
        await change(env, $(env, 'status'), 'unreviewed');
        await change(env, $(env, 'research-mode'), 'pages');
        await click(env, $(env, 'research-page-next'));
        await click(env, $(env, 'research-select-page'));
        await change(env, $(env, 'research-bulk-status'), 'reject');
        eq(labels(env).length, 48, 'shrinking results clamps to a populated page');
        eq(env.w.CastingLibrary.state().research.state().selection.size, 0, 'voices that leave the filter are deselected');
        includes($(env, 'research-page-status').textContent, 'page 1 of 1');

        env = setup();
        await env.w.loadCastingLibrary(); await settle();
        await change(env, $(env, 'research-circle-blue'), true);
        await click(env, $(env, 'research-refresh-circles'));
        eq(labels(env).length, 4);
        await click(env, markButton(env, 'persona/dh/abel', 'circles', 'blue'));
        ok(labels(env).includes('ABEL'), 'an active circle exclusion does not remove a newly marked card');
        includes($(env, 'research-circle-pending').textContent, 'Refresh results');
        await click(env, $(env, 'research-refresh-circles'));
        ok(!labels(env).includes('ABEL'), 'Refresh snapshots the newly excluded membership');

        env = setup({marks: {}});
        await env.w.loadCastingLibrary(); await settle();
        env.batchGate = new Promise(resolve => { env.releaseBatch = resolve; });
        const tagInput = $(env, 'results').querySelectorAll('input').find(control => control.attrs['aria-label'] === 'New tag for ABEL');
        const addTag = tagInput.parentElement.querySelector('button');
        tagInput.value = 'private:a'; await click(env, addTag);
        tagInput.value = 'private:b'; await click(env, addTag);
        const collectionInput = $(env, 'results').querySelectorAll('input').find(control => control.attrs['aria-label'] === 'Research collection for ABEL');
        const addCollection = collectionInput.parentElement.querySelector('button');
        collectionInput.value = 'Private one'; await click(env, addCollection);
        collectionInput.value = 'Private two'; await click(env, addCollection);
        await click(env, markButton(env, 'persona/dh/abel', 'stars', 'red'));
        await click(env, markButton(env, 'persona/dh/abel', 'stars', 'blue'));
        env.releaseBatch(); await settle();
        eq(env.serverMarks['persona/dh/abel'].tags, ['private:a', 'private:b'], 'rapid tag additions do not clobber');
        eq(env.serverMarks['persona/dh/abel'].collections, ['Private one', 'Private two']);
        eq(env.serverMarks['persona/dh/abel'].stars, ['red', 'blue'], 'rapid colored marks are queued, not dropped');
        for (const call of batchCalls(env)) {
            ok(!/private/i.test(call[2].action), 'activity stores controlled labels, not membership text');
    }

    env = setup();
    await env.w.loadCastingLibrary(); await settle();
        await change(env, selectByLabel(env, 'Review status for ABEL'), 'callback');
        env.serverMarks['persona/dh/abel'].status = 'reject';
        await env.w.loadCastingLibrary(); await settle();
        await click(env, $(env, 'research-undo'));
        eq(env.serverMarks['persona/dh/abel'].status, 'reject', 'undo cannot overwrite a later external same-field edit');

        env = setup({voices: huge});
        await env.w.loadCastingLibrary(); await settle();
        const selection = env.w.CastingLibrary.state().research.state().selection;
        for (const voice of huge.slice(0, 1000)) { selection.add(voice.id); }
        const extraBox = env.w.CastingLibrary.state().research.selectionBox(huge[1000]);
        await change(env, extraBox, true);
        eq(extraBox.checked, false, 'a refused selection cannot leave a false checked state');
        eq(selection.size, 1000);
    }

    {
        env = setup();
        await env.w.loadCastingLibrary(); await settle();
        env.batchGate = new Promise(resolve => { env.releaseBatch = resolve; });
        await change(env, selectByLabel(env, 'Review status for ABEL'), 'callback');
        await change(env, selectByLabel(env, 'Priority for ABEL'), 'high');
        env.failNextBatch = true;
        env.releaseBatch(); await settle();
        eq(batchCalls(env).length, 1, 'an uncertain failure cancels already queued writes');
        eq(env.serverMarks['persona/dh/abel'].priority, 'none');

        env = setup();
        await env.w.loadCastingLibrary(); await settle();
        env.batchGate = new Promise(resolve => { env.releaseBatch = resolve; });
        await change(env, selectByLabel(env, 'Review status for ABEL'), 'callback');
        env.w.CastingLibrary.state().research.saveMark(VOICES[4], {stars: []});
        await change(env, $(env, 'research-lock-stars'), true);
        env.releaseBatch(); await settle();
        eq(batchCalls(env).length, 1, 'locking before execution refuses queued mark changes');
        eq(env.serverMarks['persona/dh/echo'].stars, ['red', 'blue']);

        env = setup({voices: huge});
        await env.w.loadCastingLibrary(); await settle();
        const researchState = env.w.CastingLibrary.state().research.state();
        for (const voice of huge.slice(2)) { researchState.selection.add(voice.id); }
        researchState.pageMode = 'pages';
        researchState.pageIndex = 0;
        await click(env, $(env, 'research-select-page'));
        eq(researchState.selection.size, 999, 'shown-page selection over the cap is refused atomically');
        ok(env.toasts.some(([type, message]) => message.includes('exceed 1000')));

        env = setup();
        await env.w.loadCastingLibrary(); await settle();
        env.batchGate = new Promise(resolve => { env.releaseBatch = resolve; });
        env.serverMarks['persona/dh/abel'].status = 'reject';
        await change(env, selectByLabel(env, 'Review status for ABEL'), 'callback');
        env.w.CastingLibrary.state().research.saveMark(VOICES[4], {stars: []});
        env.releaseBatch(); await settle();
        eq(batchCalls(env).length, 1, 'a write queued behind a refused save is not sent');
        ok(env.toasts.some(([type, message]) => message.includes('queued casting change was not saved')),
           'the dropped queued write tells the user');

        env = setup();
        await env.w.loadCastingLibrary(); await settle();
        await click(env, $(env, 'research-select-filtered'));
        await change(env, $(env, 'research-bulk-status'), 'shortlist');
        const undoState = env.w.CastingLibrary.state().research.state();
        ok(undoState.selection.size > 1, 'bulk change keeps its selection');
        await change(env, $(env, 'status'), 'shortlist');
        const shortlisted = undoState.selection.size;
        await click(env, $(env, 'research-undo'));
        await settle();
        ok(shortlisted > 0, 'the shortlist filter still shows the selected voices');
        eq([...undoState.selection], ['persona/dh/abel'], 'undo drops voices that left the filter from the selection');

        env = setup();
        await env.w.loadCastingLibrary(); await settle();
        const researchFilters = env.w.CastingLibrary.state().research.state();
        await click(env, $(env, 'research-filter-tag').querySelectorAll('button')[0]);
        ok(researchFilters.chips.size > 0, 'a research chip is active');
        $(env, 'results').querySelectorAll('button').find(button => / variants$/.test(button.textContent)).click();
        await settle();
        eq(researchFilters.chips.size, 0, 'opening variants clears research filters that would hide the group');
    }

    console.log(JSON.stringify({passed: true, assertions}));
})().catch(error => { console.error(error); process.exit(1); });
