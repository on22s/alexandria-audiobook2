// Casting library (#1532): browse, review and cast designed voices across books.
// Every value from the catalogue (LLM-written descriptions included) renders as text, never HTML.
(() => {
    'use strict';
    const el = id => document.getElementById('casting-' + id);
    const PAGE = 48, TOP_TONES = 24, TOP_TEXTURES = 16;
    const STATUSES = ['unreviewed', 'shortlist', 'callback', 'reject'];
    const PRIORITIES = ['none', 'low', 'medium', 'high'];
    const FIELDS = ['book', 'gender', 'age', 'tone', 'texture', 'status', 'priority', 'tag', 'source', 'name',
                    'collection', 'stars', 'circles'];
    const DEFAULTS = {status: 'unreviewed', priority: 'none', notes: '', tags: [], stars: [], circles: [],
                      collections: []};
    const STATUS_LABEL = {unreviewed: 'Unreviewed', shortlist: 'Shortlist', callback: 'Callback', reject: 'Reject'};
    const STATUS_CLASS = {unreviewed: 'bg-secondary', shortlist: 'bg-success', callback: 'bg-warning text-dark', reject: 'bg-danger'};
    const HEALTH = {missing_audio: 'Missing audio', no_description: 'No description', duplicate_names: 'Duplicate names', no_sample: 'No sample line', skipped: 'Skipped folders'};
    const GENDERS = ['Male', 'Female', 'Ambiguous / mixed', 'Not specified'];
    const AGES = ['Child', 'Teen', 'Young Adult', 'Adult', 'Older / Elderly', 'Ancient / Ageless', 'Not specified'];
    let s = {voices: [], marks: {}, skipped: [], view: 'cards', shown: PAGE, chips: new Set(), epoch: 0, bound: false,
             speakers: [], review: null, playing: null, queue: null, variant: '', collections: [], shared: [],
             collectionEpoch: 0, research: null, playSeconds: 8, playFull: false, playToken: 0, timer: null};

    // ---- pure helpers (also used by the tests) ----
    function parseQuery(text) {
        const terms = [], words = [], unknown = [];
        for (const raw of String(text || '').match(/-?"[^"]*"|\S+/g) || []) {
            const negate = raw.startsWith('-') && raw.length > 1;
            const token = (negate ? raw.slice(1) : raw).replace(/"/g, '').trim().toLowerCase();
            if (!token) { continue; }
            const at = token.indexOf(':');
            if (at > 0) {
                const field = token.slice(0, at), value = token.slice(at + 1);
                if (FIELDS.includes(field) && value) { terms.push({field, value, negate}); continue; }
                unknown.push(field);
            }
            words.push({value: token, negate});
        }
        return {terms, words, unknown};
    }
    function markOf(marks, id) {
        const stored = marks[id] || {};
        return {...DEFAULTS, ...stored, tags: [...(stored.tags || [])], stars: [...(stored.stars || [])],
                circles: [...(stored.circles || [])], collections: [...(stored.collections || [])]};
    }
    function fieldValues(voice, mark, field) {
        const t = voice.tags || {};
        const values = {book: [voice.book_id], gender: [t.gender], age: t.ages, tone: t.tones, texture: t.textures,
            status: [mark.status], priority: [mark.priority], tag: mark.tags, source: [voice.source], name: [voice.name],
            collection: mark.collections, stars: mark.stars, circles: mark.circles}[field];
        return (values || []).filter(v => typeof v === 'string').map(v => v.toLowerCase());
    }
    const chipKey = (field, value) => JSON.stringify([String(field), String(value)]);
    function chipPair(key) {
        try {
            const pair = JSON.parse(key);
            return Array.isArray(pair) && pair.length === 2 ? {field: String(pair[0]), value: String(pair[1])} : null;
        } catch (error) {
            return null;
        }
    }
    // "male" must not match "female", but "young" may match "young adult".
    const matchesValue = (values, value) => values.some(v => v.startsWith(value));
    function voiceMatches(voice, mark, query, picks) {
        for (const {field, value, negate} of query.terms) {
            if (matchesValue(fieldValues(voice, mark, field), value) === negate) { return false; }
        }
        const haystack = [voice.name, voice.description, voice.ref_text].join(' ').toLowerCase();
        for (const {value, negate} of query.words) {
            if (haystack.includes(value) === negate) { return false; }
        }
        for (const [field, value] of Object.entries(picks.selects || {})) {
            if (value && !fieldValues(voice, mark, field).includes(value.toLowerCase())) { return false; }
        }
        for (const chip of picks.chips || []) {
            const pair = chipPair(chip);
            if (!pair) { continue; }
            if (!fieldValues(voice, mark, pair.field).includes(pair.value.toLowerCase())) { return false; }
        }
        if (s.research && !s.research.matches(voice, mark)) { return false; }
        return true;
    }
    function filterVoices(voices, marks, text, picks = {}) {
        const query = parseQuery(text);
        const groups = getNameGroups(voices);
        return voices.filter(v => voiceMatches(v, markOf(marks, v.id), query, picks)
            && (!picks.variant || getNameKey(v.name) === picks.variant)
            && (!picks.health || getHealthFlags(v, groups).includes(picks.health)));
    }
    const getNameKey = name => String(name || '').trim().toLowerCase().replace(/\s+/g, ' ');
    function getNameGroups(voices) {
        const groups = new Map();
        for (const voice of voices) {
            const key = getNameKey(voice.name);
            if (!groups.has(key)) { groups.set(key, []); }
            groups.get(key).push(voice);
        }
        return groups;
    }
    function getHealthFlags(voice, groups) {
        return [!voice.audio_present && 'missing_audio', !String(voice.description || '').trim() && 'no_description',
            (groups.get(getNameKey(voice.name)) || []).length > 1 && 'duplicate_names',
            !String(voice.ref_text || '').trim() && 'no_sample'].filter(Boolean);
    }
    function getHealthCounts(voices, skipped) {
        const counts = Object.fromEntries(Object.keys(HEALTH).map(key => [key, 0])), groups = getNameGroups(voices);
        for (const voice of voices) {
            for (const flag of getHealthFlags(voice, groups)) { counts[flag]++; }
        }
        counts.skipped = skipped.length;
        return counts;
    }
    function getCoverage(voices, field) {
        const columns = field === 'gender' ? GENDERS : AGES;
        const totals = new Map(), indices = new Map(columns.map((value, index) => [value.toLowerCase(), index]));
        for (const voice of voices) {
            if (!totals.has(voice.book_id)) { totals.set(voice.book_id, columns.map(() => 0)); }
            for (const value of new Set(fieldValues(voice, {}, field))) {
                if (indices.has(value)) { totals.get(voice.book_id)[indices.get(value)]++; }
            }
        }
        return {columns, rows: [...totals.keys()].sort().map(book => ({book, counts: totals.get(book)}))};
    }
    function countValues(voices, marks, field) {
        const counts = new Map();
        for (const v of voices) {
            for (const value of new Set(fieldValues(v, markOf(marks, v.id), field))) { counts.set(value, (counts.get(value) || 0) + 1); }
        }
        return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    }
    function sortVoices(voices, marks, mode) {
        const rank = {status: v => STATUSES.indexOf(markOf(marks, v.id).status),
                      priority: v => -PRIORITIES.indexOf(markOf(marks, v.id).priority),
                      book: v => (v.book_id || '').toLowerCase()}[mode];
        return [...voices].sort((a, b) => {
            const ra = rank ? rank(a) : 0, rb = rank ? rank(b) : 0;
            return (ra < rb ? -1 : ra > rb ? 1 : 0) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id);
        });
    }
    const previewUrl = url => url ? url.split('/').map(encodeURIComponent).join('/') : '';

    // ---- DOM ----
    function node(tag, text, cls) {
        const n = document.createElement(tag);
        if (text !== undefined && text !== null) { n.textContent = String(text); }
        if (cls) { n.className = cls; }
        return n;
    }
    function button(text, cls, action, label) {
        const b = node('button', text, cls);
        b.type = 'button';
        if (label) { b.setAttribute('aria-label', label); }
        b.addEventListener('click', action);
        return b;
    }
    function select(options, value, label, onChange) {
        const sel = node('select', undefined, 'form-select form-select-sm w-auto');
        sel.setAttribute('aria-label', label);
        for (const [val, text] of options) { const o = node('option', text); o.value = val; sel.append(o); }
        sel.value = value;
        sel.addEventListener('change', () => onChange(sel.value));
        return sel;
    }
    const status = text => { el('summary').textContent = text; };
    const playbackStatus = text => { const control = el('playback-status'); if (control) { control.textContent = text; } };
    const loadStatus = text => { el('load-status').textContent = text; };
    function picks() {
        return {selects: {book: el('book').value, gender: el('gender').value, age: el('age').value, status: el('status').value},
                chips: [...s.chips], health: el('health').value, variant: s.variant};
    }
    function visible() {
        return sortVoices(filterVoices(s.voices, s.marks, el('search').value, picks()), s.marks, el('sort').value);
    }
    function fillSelect(id, values, anyLabel) {
        const sel = el(id), current = sel.value;
        const first = node('option', anyLabel); first.value = '';
        sel.replaceChildren(first);
        for (const value of values) { const o = node('option', value); o.value = value; sel.append(o); }
        sel.value = values.includes(current) ? current : '';
    }
    function renderFilters() {
        fillSelect('book', [...new Set(s.voices.map(v => v.book_id))].sort(), 'All books');
        fillSelect('gender', [...new Set(s.voices.map(v => v.tags.gender))], 'Any');
        fillSelect('age', [...new Set(s.voices.flatMap(v => v.tags.ages))], 'Any');
        fillSelect('status', STATUSES, 'Any');
        const selected = el('health').value;
        const counts = getHealthCounts(s.voices, s.skipped);
        el('health').replaceChildren(...[['', 'All data health'], ...Object.entries(HEALTH).map(([key, label]) =>
            [key, label + ' (' + counts[key] + ')'])].map(([value, text]) => {
            const option = node('option', text); option.value = value; return option;
        }));
        el('health').value = selected;
        el('health-counts').replaceChildren(node('span', 'Whole library: ', 'text-muted'),
            ...Object.entries(HEALTH).map(([key, label]) => node('span', label + ': ' + counts[key], 'badge bg-secondary me-1')));
    }
    function renderCoverage(list) {
        const wrap = el('coverage'); wrap.replaceChildren();
        for (const field of ['gender', 'age']) {
            const {columns, rows} = getCoverage(list, field), table = node('table', undefined, 'table table-sm table-bordered');
            const header = node('tr');
            for (const label of ['Book / ' + field, ...columns]) { header.append(node('th', label)); }
            const head = node('thead'); head.append(header); table.append(head);
            const body = node('tbody'), combinations = [];
            for (const row of rows) {
                const tr = node('tr'); tr.append(node('th', row.book));
                row.counts.forEach((count, index) => {
                    tr.append(node('td', count)); combinations.push({book: row.book, value: columns[index], count});
                });
                body.append(tr);
            }
            table.append(body); wrap.append(table);
            combinations.sort((left, right) => left.count - right.count || left.book.localeCompare(right.book) || left.value.localeCompare(right.value));
            wrap.append(node('p', combinations.length ? 'Thinnest combinations (up to 12):' : 'No matching voices.', 'small text-muted'));
            for (const combo of combinations.slice(0, 12)) {
                wrap.append(node('span', combo.book + ' / ' + combo.value + ': ' + combo.count, 'badge bg-light text-dark border me-1 mb-2'));
            }
        }
        wrap.append(node('p', 'Keyword tags, not audio measurements. Ages can overlap; missing combinations count as zero within books in this view.', 'small text-muted'));
    }
    function renderChips(list) {
        if (s.research) { s.research.renderFilters(list); return; }
        for (const [field, id, top] of [['tone', 'tones', TOP_TONES], ['texture', 'textures', TOP_TEXTURES]]) {
            const counts = countValues(list, s.marks, field);
            const shown = counts.slice(0, top).map(([v]) => v);
            for (const chip of s.chips) {
                const pair = chipPair(chip);
                if (pair && pair.field === field && !shown.includes(pair.value)) { shown.push(pair.value); }
            }
            el(id).replaceChildren(...shown.map(value => {
                const key = chipKey(field, value), on = s.chips.has(key);
                const count = (counts.find(([v]) => v === value) || [value, 0])[1];
                const chip = button(`${value} ${count}`, 'btn btn-sm ' + (on ? 'btn-secondary' : 'btn-outline-secondary'),
                    () => { if (on) { s.chips.delete(key); } else { s.chips.add(key); } s.shown = PAGE; render(); });
                chip.setAttribute('aria-pressed', String(on));
                return chip;
            }));
        }
    }
    function tagBadges(voice, extra) {
        const wrap = node('div', undefined, 'mb-2' + (extra ? ' ' + extra : ''));
        const t = voice.tags;
        for (const [text, cls] of [[t.gender, 'bg-info text-dark'], ...t.ages.map(a => [a, 'bg-light text-dark border']),
                                   ...t.tones.slice(0, 4).map(x => [x, 'bg-light text-dark border']),
                                   ...t.textures.slice(0, 2).map(x => [x, 'bg-light text-secondary border'])]) {
            if (text && text !== 'Not specified') { wrap.append(node('span', text, 'badge me-1 ' + cls)); }
        }
        return wrap;
    }
    function playButton(voice) {
        const b = button('', 'btn btn-sm btn-outline-primary', () => togglePlay(voice, b), 'Play ' + voice.name);
        b.append(node('i', undefined, 'fas fa-play'));
        b.dataset.castingPlay = voice.id;
        b.dataset.castingFocus = 'play:' + voice.id;
        if (!voice.audio_present) { b.disabled = true; b.setAttribute('title', 'This voice has no preview audio'); }
        return b;
    }
    function markControls(voice) {
        const mark = markOf(s.marks, voice.id), box = node('div', undefined, 'd-flex flex-wrap gap-1 align-items-center');
        const statusSelect = select(STATUSES.map(v => [v, STATUS_LABEL[v]]), mark.status, 'Review status for ' + voice.name,
                          value => saveMark(voice, {status: value}));
        statusSelect.dataset.castingFocus = 'status:' + voice.id;
        box.append(statusSelect);
        const prioritySelect = select(PRIORITIES.map(v => [v, v === 'none' ? 'No priority' : v[0].toUpperCase() + v.slice(1) + ' priority']),
                          mark.priority, 'Priority for ' + voice.name, value => saveMark(voice, {priority: value}));
        prioritySelect.dataset.castingFocus = 'priority:' + voice.id;
        box.append(prioritySelect);
        const notes = node('textarea', mark.notes, 'form-control form-control-sm mt-1');
        notes.setAttribute('rows', '2'); notes.setAttribute('maxlength', '2000');
        notes.setAttribute('aria-label', 'Notes for ' + voice.name); notes.setAttribute('placeholder', 'Notes');
        notes.value = mark.notes;
        notes.dataset.castingFocus = 'notes:' + voice.id;
        notes.addEventListener('change', () => saveMark(voice, {notes: notes.value}));
        return [box, notes];
    }
    function castControls(voice) {
        const box = node('div', undefined, 'input-group input-group-sm mt-2');
        const sel = node('select', undefined, 'form-select');
        sel.setAttribute('aria-label', 'Character to use ' + voice.name + ' for');
        const first = node('option', s.speakers.length ? 'Use for character…' : 'No characters in the active book');
        first.value = ''; sel.append(first);
        for (const name of s.speakers) { const o = node('option', name); o.value = name; sel.append(o); }
        const use = button('Use', 'btn btn-outline-success', () => castVoice(voice, sel.value, use), 'Use ' + voice.name + ' for the chosen character');
        use.disabled = !voice.audio_present || !s.speakers.length;
        box.append(sel, use);
        return box;
    }
    function card(voice, groups) {
        const col = node('div', undefined, 'col-md-6 col-xl-4');
        const c = node('div', undefined, 'card h-100'), body = node('div', undefined, 'card-body d-flex flex-column');
        const head = node('div', undefined, 'd-flex justify-content-between align-items-start mb-1');
        const title = node('div');
        if (s.research) { head.append(s.research.selectionBox(voice)); }
        title.append(node('span', voice.book_id, 'badge bg-secondary me-2'), node('strong', voice.name));
        head.append(title, playButton(voice));
        const mark = markOf(s.marks, voice.id);
        const tagNode = tagBadges(voice, 'casting-tags');
        tagNode.dataset.castingVisibility = 'tags';
        body.append(head, node('div', STATUS_LABEL[mark.status], 'badge mb-2 ' + STATUS_CLASS[mark.status]), tagNode);
        const descriptionNode = node('p', voice.description || 'No description.', 'small mb-1 casting-clamp casting-description');
        descriptionNode.dataset.castingVisibility = 'description';
        body.append(descriptionNode);
        if (voice.ref_text) {
            const sampleNode = node('p', '“' + voice.ref_text + '”', 'small text-muted fst-italic mb-2 casting-sample');
            sampleNode.dataset.castingVisibility = 'sample';
            body.append(sampleNode);
        }
        const foot = node('div', undefined, 'mt-auto');
        const count = (groups.get(getNameKey(voice.name)) || []).length;
        foot.append(button(count + ' variants', 'btn btn-sm btn-outline-secondary mb-2', () => {
            el('search').value = ''; s.chips.clear();
            for (const id of ['book', 'gender', 'age', 'status', 'health']) { el(id).value = ''; }
            s.variant = getNameKey(voice.name); rerender();
        }));
        foot.append(...markControls(voice));
        const assignment = node('details', undefined, 'mt-2');
        assignment.append(node('summary', 'Use this voice', 'small'), castControls(voice), collectionControls(voice));
        foot.append(assignment);
        if (s.research) { foot.append(s.research.cardMarkControls(voice)); }
        body.append(foot);
        c.append(body); col.append(c);
        return col;
    }
    function table(list) {
        const wrap = node('div', undefined, 'table-responsive'), t = node('table', undefined, 'table table-sm table-hover align-middle mb-0');
        const head = node('thead'), hr = node('tr');
        const headings = s.research ? ['', '', 'Name', 'Book', 'Gender', 'Age', 'Tone', 'Status', 'Use for', 'Marks']
            : ['', 'Name', 'Book', 'Gender', 'Age', 'Tone', 'Status', 'Use for'];
        for (const h of headings) { hr.append(node('th', h)); }
        head.append(hr);
        const tbody = node('tbody');
        for (const v of list) {
            const tr = node('tr'), mark = markOf(s.marks, v.id);
            if (s.research) { const pick = node('td'); pick.append(s.research.selectionBox(v)); tr.append(pick); }
            const cells = [playButton(v), node('strong', v.name), v.book_id, v.tags.gender, v.tags.ages.join(', '),
                           v.tags.tones.slice(0, 4).join(', ')];
            for (const cell of cells) { const td = node('td'); if (typeof cell === 'string') { td.textContent = cell; } else { td.append(cell); } tr.append(td); }
            const st = node('td');
            const statusSelect = select(STATUSES.map(x => [x, STATUS_LABEL[x]]), mark.status, 'Review status for ' + v.name,
                                        value => saveMark(v, {status: value}));
            statusSelect.dataset.castingFocus = 'status:' + v.id;
            st.append(statusSelect);
            const use = node('td'); use.append(castControls(v));
            tr.append(st, use);
            if (s.research) { const marks = node('td'); marks.append(s.research.tableMarkControls(v)); tr.append(marks); }
            tbody.append(tr);
        }
        t.append(head, tbody); wrap.append(t);
        return wrap;
    }
    function captureFocus() {
        const active = document.activeElement;
        return active && active.dataset ? String(active.dataset.castingFocus || '') : '';
    }
    function restoreFocus(key) {
        if (!key) { return; }
        for (const candidate of document.querySelectorAll('[data-casting-focus]')) {
            if (candidate && candidate.dataset && candidate.dataset.castingFocus === key
                && typeof candidate.focus === 'function') {
                candidate.focus(); return;
            }
        }
    }
    function render() {
        const list = visible(), page = s.research ? s.research.pageSlice(list) : list.slice(0, s.shown);
        const focus = captureFocus();
        renderChips(list);
        renderCoverage(list);
        el('skipped').replaceChildren();
        if (el('health').value === 'skipped') {
            el('skipped').append(node('p', s.skipped.length + ' skipped folders (not searchable as voices).', 'text-warning'));
            for (const entry of s.skipped) { el('skipped').append(node('div', entry.id + ': ' + entry.reason)); }
        }
        const reviewed = s.voices.filter(v => markOf(s.marks, v.id).status !== 'unreviewed').length;
        const unknown = parseQuery(el('search').value).unknown;
        status(`Showing ${page.length} of ${list.length} (${s.voices.length} voices, ${reviewed} reviewed)`
               + (unknown.length ? `. Unknown field: ${unknown.join(', ')}` : ''));
        if (!s.voices.length) {
            el('results').replaceChildren(node('p', 'No designed voices yet. Generate personas in the Voices step, or save voices in the Designer.', 'text-muted mb-0'));
        } else if (!list.length) {
            el('results').replaceChildren(node('p', 'No voices match these filters.', 'text-muted mb-0'));
        } else if (s.view === 'table') {
            el('results').replaceChildren(table(page));
        } else {
            const row = node('div', undefined, 'row g-3');
            const groups = getNameGroups(s.voices);
            row.append(...page.map(voice => card(voice, groups)));
            el('results').replaceChildren(row);
        }
        const more = el('more');
        if (more && !s.research) { more.hidden = list.length <= s.shown; }
        if (s.variant) { el('summary').textContent += ' | Name group: ' + s.variant; }
        restoreFocus(focus);
        if (s.research) { s.research.afterRender(list, page); }
    }

    function collectionControls(voice) {
        const box = node('div', undefined, 'input-group input-group-sm mt-2');
        const name = node('input', undefined, 'form-control');
        name.value = voice.name; name.setAttribute('maxlength', '200');
        name.setAttribute('aria-label', 'Saved cast member name for ' + voice.name);
        const add = button('Add to saved cast', 'btn btn-outline-secondary', async () => {
            const cast = el('collection').value, member = name.value.trim();
            if (!cast || !member) { showToast('Choose a saved cast and member name first.', 'info'); return; }
            add.disabled = true;
            try {
                await API.post('/api/voice_library/catalog_member', {cast, voice_id: voice.id, name: member});
                showToast('Added ' + member + ' to ' + cast + '.', 'success');
                s.collectionEpoch++; el('collection-review').replaceChildren();
            } catch (error) { showActionError('Add catalogue voice to saved cast', error); }
            finally { add.disabled = !voice.audio_present; }
        });
        add.disabled = !voice.audio_present; box.append(name, add); return box;
    }
    async function loadCollections() {
        const epoch = s.epoch, library = await API.get('/api/voice_library');
        if (epoch !== s.epoch) { return; }
        s.collections = library.casts || []; s.shared = library.shared || [];
        fillSelect('collection', s.collections.map(cast => cast.name), 'Choose a saved cast');
    }
    async function createCollection() {
        const name = el('collection-name').value.trim();
        if (!name) { showToast('Enter a saved cast name.', 'info'); return; }
        el('create-collection').disabled = true;
        try {
            await API.post('/api/voice_library/casts', {name});
            await loadCollections(); el('collection').value = name; el('collection-name').value = '';
            s.collectionEpoch++; el('collection-review').replaceChildren();
        } catch (error) { showActionError('Create saved cast', error); }
        finally { el('create-collection').disabled = false; }
    }
    async function reviewCollection() {
        const cast = el('collection').value;
        if (!cast) { showToast('Choose a saved cast first.', 'info'); return; }
        const epoch = ++s.collectionEpoch;
        const current = () => epoch === s.collectionEpoch && cast === el('collection').value;
        const panel = el('collection-review'); panel.replaceChildren(node('p', 'Matching active book...'));
        try {
            await loadCollections();
            if (!current()) { return; }
            const matched = await API.post('/api/voice_library/match', {name: cast});
            if (!current()) { return; }
            const snapshot = await API.get('/api/voice_config/snapshot');
            if (!current()) { return; }
            if (snapshot.book_token !== matched.book_token) { throw new Error('The active book changed; review the saved cast again.'); }
            const pool = new Map(s.shared.map(member => [member.key, member]));
            for (const member of (s.collections.find(item => item.name === cast)?.members || [])) {
                if (!member.generic || member.book_id === snapshot.book_id) { pool.set(member.key, member); }
            }
            const rows = [], table = node('table', undefined, 'table table-sm');
            const head = node('tr'); head.append(node('th', 'Character'), node('th', 'Saved cast voice (skip unless confirmed)')); table.append(head);
            for (const proposal of matched.proposals) {
                const row = node('tr'); row.append(node('td', proposal.character));
                const cell = node('td');
                const target = select([['', 'Skip'], ...[...pool].map(([key, member]) => [key, member.name])],
                    proposal.match?.key || '', 'Saved cast voice for ' + proposal.character, () => {});
                rows.push({character: proposal.character, target}); cell.append(target);
                if (proposal.match && !proposal.match.exact) { cell.append(node('span', 'Fuzzy guess: review', 'badge bg-warning text-dark')); }
                row.append(cell); table.append(row);
            }
            const apply = button('Apply reviewed mappings', 'btn btn-sm btn-success', async () => {
                const mapping = Object.fromEntries(rows.filter(row => row.target.value).map(row => [row.character, row.target.value]));
                if (!Object.keys(mapping).length) { showToast('Choose at least one mapping.', 'info'); return; }
                apply.disabled = true;
                try {
                    if (!await showConfirm('Replace voices for the selected characters in the active book?',
                        {title: 'Apply saved cast?', actionLabel: 'Apply reviewed mappings'})) { return; }
                    if (!current()) { return; }
                    const result = await API.post('/api/voice_library/apply', {cast, mapping, book_token: matched.book_token});
                    if (current()) { panel.replaceChildren(node('p', 'Applied ' + result.count + ' voices.')); }
                    for (const warning of result.warnings || []) { showToast(warning, 'warning'); }
                    if (current() && typeof loadVoices === 'function') { await loadVoices(); }
                } catch (error) { showActionError('Apply collection', error); }
                finally { apply.disabled = false; }
            });
            panel.replaceChildren(table, apply);
        } catch (error) {
            if (current()) { panel.replaceChildren(node('p', 'Could not match this collection. Try again.', 'text-warning')); showActionError('Review collection', error); }
        }
    }

    // ---- actions ----
    async function saveMark(voice, patch) {
        if (s.research) { return s.research.saveMark(voice, patch); }
        try {
            const result = await API.post('/api/casting/marks', {voice_id: voice.id, ...patch});
            s.marks[voice.id] = result.mark;
            render();
            if (s.review) { renderReview(); }
        } catch (error) {
            showActionError('Save casting mark', error, 'Reload the casting library and try again.');
            render();
        }
    }
    async function loadSpeakers() {
        try {
            const snapshot = await API.get('/api/voice_config/snapshot');
            s.speakers = (snapshot.voices || []).map(v => v.name).filter(n => typeof n === 'string');
            return snapshot;
        } catch (error) {
            s.speakers = [];
            return null;
        }
    }
    async function castVoice(voice, speaker, control) {
        if (!speaker) { showToast('Choose a character first.', 'info'); return; }
        control.disabled = true;
        try {
            if (!await showConfirm(`Use ${voice.name} (${voice.book_id}) for ${speaker}? ${speaker}'s current voice is kept as a saved candidate.`,
                                   {title: 'Use this voice?', actionLabel: 'Use voice'})) { return; }
            const snapshot = await loadSpeakers();
            if (!snapshot || !s.speakers.includes(speaker)) { throw new Error(`${speaker} is no longer in the active book. Reload and try again.`); }
            const token = snapshot.book_token;
            const built = await API.get(`/api/casting/clone_config?voice_id=${encodeURIComponent(voice.id)}&speaker=${encodeURIComponent(speaker)}`);
            const candidate = 'casting-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
            const base = `/api/voices/${encodeURIComponent(speaker)}/candidates`;
            await API.post(base, {candidate_id: candidate, config: built.config, book_token: token});
            await API.post(`${base}/${encodeURIComponent(candidate)}/select`, {book_token: token});
            showToast(`${speaker} now uses ${voice.name}. The previous voice is kept as a saved candidate in the Voices step.`, 'success');
        } catch (error) {
            showActionError('Use voice', error, 'Reload the Voices step and the casting library, then try again.');
        } finally {
            control.disabled = !voice.audio_present || !s.speakers.length;
        }
    }
    function stopPlayback() {
        if (s.playing) { s.playing.audio.pause?.(); s.playing.button?.replaceChildren(node('i', undefined, 'fas fa-play')); }
        s.playing = null;
        playbackStatus('');
    }
    function haltAudio() {
        s.playToken++;
        if (s.timer !== null) { clearTimeout(s.timer); s.timer = null; }
        stopPlayback();
    }
    function togglePlay(voice, playButtonEl) {
        const same = s.playing && s.playing.id === voice.id;
        if (s.queue) { stopQueuePlayback(); }
        haltAudio();
        if (same || !voice.audio_present || typeof Audio === 'undefined') { return; }
        const token = s.playToken;
        const audio = new Audio(previewUrl(voice.preview_url));
        if (typeof stopDesignedVoicePlayback === 'function') { stopDesignedVoicePlayback(audio); }
        window._designedPlaybackAudio = audio;
        s.playing = {id: voice.id, audio, button: playButtonEl};
        playbackStatus('Playing ' + voice.name);
        playButtonEl?.replaceChildren(node('i', undefined, 'fas fa-stop'));
        audio.addEventListener('ended', () => {
            if (token !== s.playToken) { return; }
            if (s.playing?.audio === audio) { stopPlayback(); }
        });
        audio.play().catch(error => {
            if (token !== s.playToken) { return; }
            stopPlayback(); showActionError('Play voice', error);
        });
    }
    function stopQueuePlayback() {
        s.queue = null;
        haltAudio();
        const playAllButton = el('play-all');
        if (playAllButton) { playAllButton.textContent = 'Play all'; }
    }
    function audioVoices() {
        return visible().filter(v => v.audio_present);
    }
    function startQueueItem() {
        const q = s.queue;
        if (!q) { return; }
        if (q.index < 0) { q.index = 0; }
        if (q.index >= q.list.length) { stopQueuePlayback(); return; }
        if (typeof Audio === 'undefined') {
            stopQueuePlayback();
            showToast('This browser cannot play preview audio.', 'info');
            return;
        }
        const voice = q.list[q.index];
        haltAudio();
        const token = s.playToken;
        const audio = new Audio(previewUrl(voice.preview_url));
        if (typeof stopDesignedVoicePlayback === 'function') { stopDesignedVoicePlayback(audio); }
        window._designedPlaybackAudio = audio;
        s.playing = {id: voice.id, audio, button: null};
        playbackStatus(`Playing ${voice.name} (${q.index + 1} of ${q.list.length})`);
        audio.addEventListener('pause', () => {
            if (!audio.ended && token === s.playToken && s.queue === q) { stopQueuePlayback(); }
        });
        audio.addEventListener('error', () => {
            if (token !== s.playToken || s.queue !== q) { return; }
            stopQueuePlayback();
            showToast('Preview playback failed. The queue has stopped.', 'warning');
        });
        audio.addEventListener('ended', () => {
            if (token !== s.playToken || s.queue !== q) { return; }
            advanceQueue(1);
        });
        audio.play().then(() => {
            if (token !== s.playToken || s.queue !== q) { return; }
            if (s.playFull) { return; }
            s.timer = setTimeout(() => {
                if (token !== s.playToken || s.queue !== q) { return; }
                s.timer = null;
                advanceQueue(1);
            }, Math.max(1, Math.min(120, s.playSeconds)) * 1000);
        }).catch(error => {
            if (token !== s.playToken || s.queue !== q) { return; }
            stopQueuePlayback();
            showActionError('Play voice', error);
        });
    }
    function advanceQueue(delta) {
        if (!s.queue) { return; }
        s.queue.index += delta;
        if (s.queue.index >= s.queue.list.length) { stopQueuePlayback(); return; }
        if (s.queue.index < 0) { s.queue.index = 0; }
        startQueueItem();
    }
    function playAll() {
        if (s.queue) { stopQueuePlayback(); return; }
        const list = audioVoices();
        if (!list.length) { showToast('No voices with audio match these filters.', 'info'); return; }
        s.queue = {list, index: 0};
        const playAllButton = el('play-all');
        if (playAllButton) { playAllButton.textContent = 'Stop'; }
        startQueueItem();
    }
    function playRandom() {
        const list = audioVoices();
        if (!list.length) { showToast('No voices with audio match these filters.', 'info'); return; }
        stopQueuePlayback();
        s.queue = {list, index: Math.floor(Math.random() * list.length)};
        const playAllButton = el('play-all');
        if (playAllButton) { playAllButton.textContent = 'Stop'; }
        startQueueItem();
    }
    function setPlaybackConfig(seconds, full) {
        const value = Number(seconds);
        s.playSeconds = Number.isFinite(value) && value >= 1 && value <= 120 ? Math.round(value) : 8;
        s.playFull = Boolean(full);
        stopQueuePlayback();
    }

    // ---- review ----
    function renderReview() {
        const r = s.review, voice = r.list[r.index];
        if (!voice) { return; }
        const mark = markOf(s.marks, voice.id), body = el('review-body');
        const head = node('div', undefined, 'mb-2');
        head.append(node('span', voice.book_id, 'badge bg-secondary me-2'), node('span', STATUS_LABEL[mark.status], 'badge ' + STATUS_CLASS[mark.status]));
        body.replaceChildren(head, node('h3', voice.name, 'mb-2'), tagBadges(voice),
                             node('p', voice.description || 'No description.', 'mb-2'));
        if (voice.ref_text) { body.append(node('p', '“' + voice.ref_text + '”', 'text-muted fst-italic')); }
        if (!voice.audio_present) { body.append(node('p', 'This voice has no preview audio.', 'text-warning small')); }
        el('review-notes').value = mark.notes;
        el('review-position').textContent = `${r.index + 1} of ${r.list.length}`;
        const done = r.list.filter(v => markOf(s.marks, v.id).status !== 'unreviewed').length;
        el('review-progress').style.width = `${Math.round(100 * done / r.list.length)}%`;
        el('review-prev').disabled = r.index === 0;
        el('review-next').disabled = r.index >= r.list.length - 1;
    }
    function moveReview(delta) {
        if (!s.review) { return; }
        stopPlayback();
        s.review.index = Math.max(0, Math.min(s.review.list.length - 1, s.review.index + delta));
        renderReview();
    }
    async function reviewStatus(value) {
        if (!s.review) { return; }
        const voice = s.review.list[s.review.index];
        const result = await saveMark(voice, {status: value});
        if (result && !result.ok) { return; }
        if (s.review && s.review.index < s.review.list.length - 1) { moveReview(1); }
    }
    function openReview() {
        const list = visible();
        if (!list.length) { showToast('No voices match these filters.', 'info'); return; }
        const firstOpen = list.findIndex(v => markOf(s.marks, v.id).status === 'unreviewed');
        s.review = {list, index: firstOpen < 0 ? 0 : firstOpen};
        renderReview();
        const modal = el('review-modal');
        if (window.bootstrap?.Modal) { window.bootstrap.Modal.getOrCreateInstance(modal).show(); }
    }
    function onReviewKey(event) {
        if (!s.review || event.target?.tagName?.toLowerCase() === 'textarea') { return; }
        const key = String(event.key || '').toLowerCase();
        const actions = {s: () => reviewStatus('shortlist'), c: () => reviewStatus('callback'), r: () => reviewStatus('reject'),
                         arrowright: () => moveReview(1), arrowleft: () => moveReview(-1),
                         ' ': () => togglePlay(s.review.list[s.review.index], null)};
        if (actions[key]) { event.preventDefault?.(); actions[key](); }
    }

    function rerender() {
        s.shown = PAGE;
        if (s.research) { s.research.filtersChanged(); } else { stopQueuePlayback(); }
        render();
    }
    function createResearch() {
        if (s.research || !window.CastingResearch || typeof window.CastingResearch.create !== 'function') { return; }
        s.research = window.CastingResearch.create({
            state: () => s,
            visible,
            render,
            helpers: {markOf: id => markOf(s.marks, id), fieldValues, countValues},
            getMark: id => markOf(s.marks, id),
            confirm: (message, optionsList) => typeof showConfirm === 'function'
                ? showConfirm(message, optionsList) : Promise.resolve(false),
            stopPlayback: stopQueuePlayback,
            afterSave: () => { if (s.review) { renderReview(); } },
            playback: {seconds: () => s.playSeconds, full: () => s.playFull, set: setPlaybackConfig,
                       next: () => advanceQueue(1), prev: () => advanceQueue(-1), stop: stopQueuePlayback,
                       random: playRandom},
        });
        s.research.mount();
    }
    function bind() {
        if (s.bound) { return; }
        s.bound = true;
        createResearch();
        for (const id of ['search']) { el(id).addEventListener('input', rerender); }
        for (const id of ['book', 'gender', 'age', 'status', 'sort', 'health']) { el(id).addEventListener('change', rerender); }
        el('create-collection').addEventListener('click', createCollection);
        el('match-collection').addEventListener('click', reviewCollection);
        el('collection').addEventListener('change', () => { s.collectionEpoch++; el('collection-review').replaceChildren(); });
        el('refresh').addEventListener('click', () => window.loadCastingLibrary());
        el('more').addEventListener('click', () => { if (!s.research) { s.shown += PAGE; render(); } });
        el('clear').addEventListener('click', () => {
            el('search').value = ''; for (const id of ['book', 'gender', 'age', 'status']) { el(id).value = ''; }
            el('health').value = ''; s.variant = ''; s.chips.clear();
            if (s.research) { s.research.clearFilters(); }
            rerender();
        });
        for (const [id, view] of [['view-cards', 'cards'], ['view-table', 'table']]) {
            el(id).addEventListener('click', () => {
                s.view = view;
                el('view-cards').className = 'btn btn-outline-secondary' + (view === 'cards' ? ' active' : '');
                el('view-table').className = 'btn btn-outline-secondary' + (view === 'table' ? ' active' : '');
                el('view-cards').setAttribute('aria-pressed', String(view === 'cards'));
                el('view-table').setAttribute('aria-pressed', String(view === 'table'));
                render();
            });
        }
        el('play-all').addEventListener('click', playAll);
        el('review').addEventListener('click', openReview);
        el('review-prev').addEventListener('click', () => moveReview(-1));
        el('review-next').addEventListener('click', () => moveReview(1));
        el('review-play').addEventListener('click', () => s.review && togglePlay(s.review.list[s.review.index], null));
        el('review-save-notes').addEventListener('click', () => s.review && saveMark(s.review.list[s.review.index], {notes: el('review-notes').value}));
        for (const b of el('review-modal').querySelectorAll('[data-casting-status]')) {
            b.addEventListener('click', () => reviewStatus(b.dataset.castingStatus));
        }
        el('review-modal').addEventListener('keydown', onReviewKey);
        el('review-modal').addEventListener('hidden.bs.modal', () => { stopQueuePlayback(); s.review = null; });
        document.addEventListener?.('visibilitychange', () => { if (document.hidden) { stopQueuePlayback(); } });
        window.addEventListener?.('pagehide', () => stopQueuePlayback());
        const tab = document.getElementById('casting-tab');
        if (tab && typeof MutationObserver === 'function') {
            const observer = new MutationObserver(() => {
                if (tab.hidden || tab.style.display === 'none') { stopQueuePlayback(); }
            });
            observer.observe(tab, {attributes: true, attributeFilter: ['style', 'hidden']});
        }
    }

    window.loadCastingLibrary = async () => {
        bind();
        const epoch = ++s.epoch;
        stopQueuePlayback();
        if (s.research) { s.research.beforeLoad(); }
        s.collectionEpoch++; el('collection-review').replaceChildren();
        loadStatus('');
        try {
            const [library] = await Promise.all([API.get('/api/casting/voices'), loadSpeakers(), loadCollections().catch(error => {
                if (epoch === s.epoch) { showActionError('Load collections', error); }
            })]);
            if (epoch !== s.epoch) { return; }
            s.voices = library.voices || [];
            s.marks = library.marks || {};
            s.skipped = library.skipped || [];
            if (s.skipped.length) { loadStatus(`${s.skipped.length} voice folder(s) could not be read and are not shown.`); }
            renderFilters();
            if (s.research) { s.research.afterLoad(library); }
            render();
        } catch (error) {
            if (epoch !== s.epoch) { return; }
            loadStatus('The casting library could not be loaded. Check that Alexandria is still running, then use Refresh.');
            showActionError('Load casting library', error);
        }
    };
    window.CastingLibrary = {parseQuery, filterVoices, countValues, sortVoices, markOf, previewUrl, onReviewKey,
                             getNameKey, getNameGroups, getHealthCounts, getCoverage, chipKey, chipPair,
                             state: () => s};
})();
