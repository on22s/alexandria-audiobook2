// Casting library (#1532): browse, review and cast designed voices across books.
// Every value from the catalogue (LLM-written descriptions included) renders as text, never HTML.
(() => {
    'use strict';
    const el = id => document.getElementById('casting-' + id);
    const PAGE = 48, TOP_TONES = 24, TOP_TEXTURES = 16;
    const STATUSES = ['unreviewed', 'shortlist', 'callback', 'reject'];
    const PRIORITIES = ['none', 'low', 'medium', 'high'];
    const FIELDS = ['book', 'gender', 'age', 'tone', 'texture', 'status', 'priority', 'tag', 'source', 'name'];
    const STATUS_LABEL = {unreviewed: 'Unreviewed', shortlist: 'Shortlist', callback: 'Callback', reject: 'Reject'};
    const STATUS_CLASS = {unreviewed: 'bg-secondary', shortlist: 'bg-success', callback: 'bg-warning text-dark', reject: 'bg-danger'};
    const HEALTH = {missing_audio: 'Missing audio', no_description: 'No description', duplicate_names: 'Duplicate names', no_sample: 'No sample line', skipped: 'Skipped folders'};
    const GENDERS = ['Male', 'Female', 'Ambiguous / mixed', 'Not specified'];
    const AGES = ['Child', 'Teen', 'Young Adult', 'Adult', 'Older / Elderly', 'Ancient / Ageless', 'Not specified'];
    let s = {voices: [], marks: {}, skipped: [], view: 'cards', shown: PAGE, chips: new Set(), epoch: 0, bound: false,
             speakers: [], review: null, playing: null, queue: null, variant: '', collections: [], shared: [], collectionEpoch: 0};

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
        return {status: 'unreviewed', priority: 'none', notes: '', tags: [], ...(marks[id] || {})};
    }
    function fieldValues(voice, mark, field) {
        const t = voice.tags || {};
        const values = {book: [voice.book_id], gender: [t.gender], age: t.ages, tone: t.tones, texture: t.textures,
            status: [mark.status], priority: [mark.priority], tag: mark.tags, source: [voice.source], name: [voice.name]}[field];
        return (values || []).filter(v => typeof v === 'string').map(v => v.toLowerCase());
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
            const [field, value] = chip.split(':');
            if (!fieldValues(voice, mark, field).includes(value)) { return false; }
        }
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
        for (const [field, id, top] of [['tone', 'tones', TOP_TONES], ['texture', 'textures', TOP_TEXTURES]]) {
            const counts = countValues(list, s.marks, field);
            const shown = counts.slice(0, top).map(([v]) => v);
            for (const chip of s.chips) { const [f, v] = chip.split(':'); if (f === field && !shown.includes(v)) { shown.push(v); } }
            el(id).replaceChildren(...shown.map(value => {
                const key = field + ':' + value, on = s.chips.has(key);
                const count = (counts.find(([v]) => v === value) || [value, 0])[1];
                const chip = button(`${value} ${count}`, 'btn btn-sm ' + (on ? 'btn-secondary' : 'btn-outline-secondary'),
                    () => { if (on) { s.chips.delete(key); } else { s.chips.add(key); } s.shown = PAGE; render(); });
                chip.setAttribute('aria-pressed', String(on));
                return chip;
            }));
        }
    }
    function tagBadges(voice) {
        const wrap = node('div', undefined, 'mb-2');
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
        if (!voice.audio_present) { b.disabled = true; b.setAttribute('title', 'This voice has no preview audio'); }
        return b;
    }
    function markControls(voice) {
        const mark = markOf(s.marks, voice.id), box = node('div', undefined, 'd-flex flex-wrap gap-1 align-items-center');
        box.append(select(STATUSES.map(v => [v, STATUS_LABEL[v]]), mark.status, 'Review status for ' + voice.name,
                          value => saveMark(voice, {status: value})));
        box.append(select(PRIORITIES.map(v => [v, v === 'none' ? 'No priority' : v[0].toUpperCase() + v.slice(1) + ' priority']),
                          mark.priority, 'Priority for ' + voice.name, value => saveMark(voice, {priority: value})));
        const notes = node('textarea', mark.notes, 'form-control form-control-sm mt-1');
        notes.setAttribute('rows', '2'); notes.setAttribute('maxlength', '2000');
        notes.setAttribute('aria-label', 'Notes for ' + voice.name); notes.setAttribute('placeholder', 'Notes');
        notes.value = mark.notes;
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
        title.append(node('span', voice.book_id, 'badge bg-secondary me-2'), node('strong', voice.name));
        head.append(title, playButton(voice));
        const mark = markOf(s.marks, voice.id);
        body.append(head, node('div', STATUS_LABEL[mark.status], 'badge mb-2 ' + STATUS_CLASS[mark.status]), tagBadges(voice));
        body.append(node('p', voice.description || 'No description.', 'small mb-1 casting-clamp'));
        if (voice.ref_text) { body.append(node('p', '“' + voice.ref_text + '”', 'small text-muted fst-italic mb-2')); }
        const foot = node('div', undefined, 'mt-auto');
        const count = (groups.get(getNameKey(voice.name)) || []).length;
        foot.append(button(count + ' variants', 'btn btn-sm btn-outline-secondary mb-2', () => {
            el('search').value = ''; s.chips.clear();
            for (const id of ['book', 'gender', 'age', 'status', 'health']) { el(id).value = ''; }
            s.variant = getNameKey(voice.name); s.shown = PAGE; render();
        }));
        foot.append(...markControls(voice), castControls(voice), collectionControls(voice));
        body.append(foot);
        c.append(body); col.append(c);
        return col;
    }
    function table(list) {
        const wrap = node('div', undefined, 'table-responsive'), t = node('table', undefined, 'table table-sm table-hover align-middle mb-0');
        const head = node('thead'), hr = node('tr');
        for (const h of ['', 'Name', 'Book', 'Gender', 'Age', 'Tone', 'Status', 'Use for']) { hr.append(node('th', h)); }
        head.append(hr);
        const tbody = node('tbody');
        for (const v of list) {
            const tr = node('tr'), mark = markOf(s.marks, v.id);
            const cells = [playButton(v), node('strong', v.name), v.book_id, v.tags.gender, v.tags.ages.join(', '),
                           v.tags.tones.slice(0, 4).join(', ')];
            for (const cell of cells) { const td = node('td'); if (typeof cell === 'string') { td.textContent = cell; } else { td.append(cell); } tr.append(td); }
            const st = node('td');
            st.append(select(STATUSES.map(x => [x, STATUS_LABEL[x]]), mark.status, 'Review status for ' + v.name, value => saveMark(v, {status: value})));
            const use = node('td'); use.append(castControls(v));
            tr.append(st, use);
            tbody.append(tr);
        }
        t.append(head, tbody); wrap.append(t);
        return wrap;
    }
    function render() {
        const list = visible(), page = list.slice(0, s.shown);
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
        el('more').hidden = list.length <= s.shown;
        if (s.variant) { el('summary').textContent += ' | Name group: ' + s.variant; }
    }

    function collectionControls(voice) {
        const box = node('div', undefined, 'input-group input-group-sm mt-2');
        const name = node('input', undefined, 'form-control');
        name.value = voice.name; name.setAttribute('maxlength', '200');
        name.setAttribute('aria-label', 'Collection member name for ' + voice.name);
        const add = button('Add to collection', 'btn btn-outline-secondary', async () => {
            const cast = el('collection').value, member = name.value.trim();
            if (!cast || !member) { showToast('Choose a collection and member name first.', 'info'); return; }
            add.disabled = true;
            try {
                await API.post('/api/voice_library/catalog_member', {cast, voice_id: voice.id, name: member});
                showToast('Added ' + member + ' to ' + cast + '.', 'success');
                s.collectionEpoch++; el('collection-review').replaceChildren();
            } catch (error) { showActionError('Add catalogue voice to collection', error); }
            finally { add.disabled = !voice.audio_present; }
        });
        add.disabled = !voice.audio_present; box.append(name, add); return box;
    }
    async function loadCollections() {
        const epoch = s.epoch, library = await API.get('/api/voice_library');
        if (epoch !== s.epoch) { return; }
        s.collections = library.casts || []; s.shared = library.shared || [];
        fillSelect('collection', s.collections.map(cast => cast.name), 'Choose a collection');
    }
    async function createCollection() {
        const name = el('collection-name').value.trim();
        if (!name) { showToast('Enter a collection name.', 'info'); return; }
        el('create-collection').disabled = true;
        try {
            await API.post('/api/voice_library/casts', {name});
            await loadCollections(); el('collection').value = name; el('collection-name').value = '';
            s.collectionEpoch++; el('collection-review').replaceChildren();
        } catch (error) { showActionError('Create collection', error); }
        finally { el('create-collection').disabled = false; }
    }
    async function reviewCollection() {
        const cast = el('collection').value;
        if (!cast) { showToast('Choose a collection first.', 'info'); return; }
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
            if (snapshot.book_token !== matched.book_token) { throw new Error('The active book changed; review the collection again.'); }
            const pool = new Map(s.shared.map(member => [member.key, member]));
            for (const member of (s.collections.find(item => item.name === cast)?.members || [])) {
                if (!member.generic || member.book_id === snapshot.book_id) { pool.set(member.key, member); }
            }
            const rows = [], table = node('table', undefined, 'table table-sm');
            const head = node('tr'); head.append(node('th', 'Character'), node('th', 'Collection voice (skip unless confirmed)')); table.append(head);
            for (const proposal of matched.proposals) {
                const row = node('tr'); row.append(node('td', proposal.character));
                const cell = node('td');
                const target = select([['', 'Skip'], ...[...pool].map(([key, member]) => [key, member.name])],
                    proposal.match?.key || '', 'Collection voice for ' + proposal.character, () => {});
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
                        {title: 'Apply collection?', actionLabel: 'Apply reviewed mappings'})) { return; }
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
    }
    function togglePlay(voice, playButtonEl, onEnd) {
        const same = s.playing && s.playing.id === voice.id;
        stopPlayback();
        if (same || !voice.audio_present || typeof Audio === 'undefined') { return; }
        const audio = new Audio(previewUrl(voice.preview_url));
        if (typeof stopDesignedVoicePlayback === 'function') { stopDesignedVoicePlayback(audio); }
        window._designedPlaybackAudio = audio;
        s.playing = {id: voice.id, audio, button: playButtonEl};
        playButtonEl?.replaceChildren(node('i', undefined, 'fas fa-stop'));
        audio.addEventListener('ended', () => { if (s.playing?.audio === audio) { stopPlayback(); } onEnd?.(); });
        audio.play().catch(error => { stopPlayback(); showActionError('Play voice', error); });
    }
    function playAll() {
        if (s.queue) { s.queue = null; stopPlayback(); el('play-all').textContent = 'Play all'; return; }
        const list = visible().filter(v => v.audio_present);
        if (!list.length) { showToast('No voices with audio match these filters.', 'info'); return; }
        s.queue = {list, index: 0};
        el('play-all').textContent = 'Stop';
        const step = () => {
            if (!s.queue || s.queue.index >= s.queue.list.length) { s.queue = null; el('play-all').textContent = 'Play all'; return; }
            const voice = s.queue.list[s.queue.index++];
            status(`Playing ${voice.name} (${s.queue.index} of ${s.queue.list.length})`);
            togglePlay(voice, null, step);
        };
        step();
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
        await saveMark(voice, {status: value});
        if (s.review.index < s.review.list.length - 1) { moveReview(1); }
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

    function bind() {
        if (s.bound) { return; }
        s.bound = true;
        const rerender = () => { s.shown = PAGE; render(); };
        for (const id of ['search']) { el(id).addEventListener('input', rerender); }
        for (const id of ['book', 'gender', 'age', 'status', 'sort', 'health']) { el(id).addEventListener('change', rerender); }
        el('create-collection').addEventListener('click', createCollection);
        el('match-collection').addEventListener('click', reviewCollection);
        el('collection').addEventListener('change', () => { s.collectionEpoch++; el('collection-review').replaceChildren(); });
        el('refresh').addEventListener('click', () => window.loadCastingLibrary());
        el('more').addEventListener('click', () => { s.shown += PAGE; render(); });
        el('clear').addEventListener('click', () => {
            el('search').value = ''; for (const id of ['book', 'gender', 'age', 'status']) { el(id).value = ''; }
            el('health').value = ''; s.variant = ''; s.chips.clear(); rerender();
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
        el('review-modal').addEventListener('hidden.bs.modal', () => { stopPlayback(); s.review = null; });
    }

    window.loadCastingLibrary = async () => {
        bind();
        const epoch = ++s.epoch;
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
            render();
        } catch (error) {
            if (epoch !== s.epoch) { return; }
            loadStatus('The casting library could not be loaded. Check that Alexandria is still running, then use Refresh.');
            showActionError('Load casting library', error);
        }
    };
    window.CastingLibrary = {parseQuery, filterVoices, countValues, sortVoices, markOf, previewUrl, onReviewKey,
                             getNameKey, getNameGroups, getHealthCounts, getCoverage,
                             state: () => s};
})();
