(() => {
    'use strict';
    const COLORS = ['red', 'amber', 'green', 'blue', 'purple'];
    const TAG_COLOR_CLASS = {red: 'bg-danger text-white', amber: 'bg-warning text-dark', green: 'bg-success text-white',
                             blue: 'bg-primary text-white', purple: 'casting-tag-purple text-white'};
    const NEUTRAL_TAG_CLASS = 'casting-tag-neutral';
    const SHAPES = ['stars', 'circles'];
    const SHAPE_LABEL = {stars: 'Star', circles: 'Circle'};
    const COLOR_LABEL = {red: 'red', amber: 'amber', green: 'green', blue: 'blue', purple: 'purple'};
    const FILTER_FIELDS = ['tone', 'texture', 'tag', 'collection', 'star', 'circle'];
    const FIELD_LABEL = {tone: 'tone', texture: 'texture', tag: 'tag', collection: 'research collection',
                         star: 'star', circle: 'circle'};
    const MAX_TAGS = 20, MAX_TAG_LENGTH = 40, MAX_COLLECTIONS = 20, MAX_COLLECTION_LENGTH = 40;
    const MAX_SELECTION = 1000, MAX_VIEWS = 100, MAX_UNDO = 20, MAX_ACTION = 100;
    const PAGE_SIZES = [24, 48, 96], DEFAULT_PAGE = 48, DEFAULT_SECONDS = 8;
    const MAX_TONE_CHIPS = 24, MAX_TEXTURE_CHIPS = 16, MAX_VALUE_CHIPS = 20;
    const PREFS_KEY = 'castingResearchPrefsV1', VIEWS_KEY = 'castingResearchViewsV1';
    const SORTS = ['name', 'book', 'status', 'priority'];
    const DEFAULTS = {status: 'unreviewed', priority: 'none', notes: '', tags: [], tag_colors: {}, stars: [],
                      circles: [], collections: []};

    const store = () => {
        try { return window.localStorage || null; } catch (error) { return null; }
    };
    function readStored(key, fallback) {
        const localStorage = store();
        if (!localStorage) { return fallback; }
        try {
            const raw = localStorage.getItem(key);
            if (raw === null || raw === undefined || raw === '') { return fallback; }
            const value = JSON.parse(raw);
            return value === null || value === undefined ? fallback : value;
        } catch (error) {
            return fallback;
        }
    }
    function writeStored(key, value) {
        const localStorage = store();
        if (!localStorage) { return false; }
        try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch (error) { return false; }
    }
    const node = (tag, text, cls) => {
        const element = document.createElement(tag);
        if (text !== undefined && text !== null) { element.textContent = String(text); }
        if (cls) { element.className = cls; }
        return element;
    };
    const clone = value => Array.isArray(value) ? [...value]
        : value && typeof value === 'object' ? {...value} : value;
    const cloneValues = values => [...(values || [])];
    const bounded = (text, limit) => String(text === undefined || text === null ? '' : text).slice(0, limit);
    function copyTagColors(value) {
        const colors = {};
        if (!value || typeof value !== 'object' || Array.isArray(value)) { return colors; }
        for (const [tag, color] of Object.entries(value)) {
            if (typeof tag === 'string' && tag && COLORS.includes(color)) {
                Object.defineProperty(colors, tag, {value: color, enumerable: true, writable: true, configurable: true});
            }
        }
        return colors;
    }
    function tagColorOf(mark, tag) {
        const colors = mark && mark.tag_colors;
        if (!colors || typeof colors !== 'object' || Array.isArray(colors)
            || !Object.prototype.hasOwnProperty.call(colors, tag)) { return ''; }
        return COLORS.includes(colors[tag]) ? colors[tag] : '';
    }
    const tagColorClass = color => TAG_COLOR_CLASS[color] || NEUTRAL_TAG_CLASS;
    const normalize = mark => {
        const full = {...DEFAULTS, ...(mark || {})};
        full.tags = cloneValues(full.tags);
        full.tag_colors = copyTagColors(full.tag_colors);
        full.stars = cloneValues(full.stars);
        full.circles = cloneValues(full.circles);
        full.collections = cloneValues(full.collections);
        return full;
    };
    const chipKey = (field, value) => JSON.stringify([String(field), String(value)]);
    function chipPair(key) {
        try {
            const pair = JSON.parse(key);
            return Array.isArray(pair) && pair.length === 2 ? {field: String(pair[0]), value: String(pair[1])} : null;
        } catch (error) {
            return null;
        }
    }
    const stateNames = {off: 'Off', include: 'Include', exclude: 'Exclude'};
    const chipClasses = {off: 'btn-outline-secondary', include: 'btn-secondary', exclude: 'btn-danger'};
    const chipIcons = {off: 'fa-circle-notch', include: 'fa-check', exclude: 'fa-ban'};

    function workspace() {
        if (typeof document.getElementById === 'function') {
            const byId = document.getElementById('casting-workspace');
            if (byId) { return byId; }
        }
        if (typeof document.querySelector === 'function') { return document.querySelector('.casting-workspace'); }
        return null;
    }
    function setClass(target, name, on) {
        if (!target) { return; }
        const parts = String(target.className || '').split(/\s+/).filter(part => part && part !== name);
        if (on) { parts.push(name); }
        target.className = parts.join(' ');
    }
    function captureFocus() {
        const active = document.activeElement;
        return active && active.dataset ? String(active.dataset.castingFocus || '') : '';
    }
    function restoreFocus(key) {
        if (!key || typeof document.querySelectorAll !== 'function') { return; }
        for (const candidate of document.querySelectorAll('[data-casting-focus]')) {
            if (candidate && candidate.dataset && candidate.dataset.castingFocus === key
                && typeof candidate.focus === 'function') {
                candidate.focus();
                return;
            }
        }
    }
    function notify(kind, message) {
        if (typeof showToast === 'function') { showToast(message, kind); }
    }
    function actionFailure(action, error, recovery) {
        if (typeof showActionError === 'function') { showActionError(action, error, recovery); }
    }

    function create(options) {
        const state = options.state;
        const visible = options.visible;
        const render = options.render;
        const getMark = options.getMark || (options.helpers && options.helpers.markOf)
            || (id => ({...DEFAULTS, ...(state().marks[id] || {})}));
        const confirmAction = typeof options.confirm === 'function' ? options.confirm
            : (typeof showConfirm === 'function' ? showConfirm : null);
        const playback = options.playback || {};
        const prefs = readPrefs();

        const st = {
            mounted: false, selection: new Set(), chips: new Map(), circlePending: new Set(),
            circleCommitted: new Set(), circleSnapshots: new Map(), pageMode: 'load', pageSize: prefs.pageSize, loaded: prefs.pageSize,
            pageIndex: 0, activity: [], undo: [], redo: [], saving: false, needsReload: false,
            views: readViews(), chain: Promise.resolve(), observer: null, observerSentinel: null,
        };
        const ui = {};

        function readPrefs() {
            const defaults = {description: true, sample: true, tags: true, focus: false,
                              pageSize: DEFAULT_PAGE, seconds: DEFAULT_SECONDS, full: false,
                              lockStars: false, lockCircles: false};
            const raw = readStored(PREFS_KEY, {});
            if (!raw || typeof raw !== 'object' || Array.isArray(raw)) { return defaults; }
            const prefs = {...defaults};
            for (const key of ['description', 'sample', 'tags', 'focus', 'full', 'lockStars', 'lockCircles']) {
                if (typeof raw[key] === 'boolean') { prefs[key] = raw[key]; }
            }
            if (PAGE_SIZES.includes(raw.pageSize)) { prefs.pageSize = raw.pageSize; }
            if (Number.isInteger(raw.seconds) && raw.seconds >= 1 && raw.seconds <= 120) { prefs.seconds = raw.seconds; }
            return prefs;
        }
        function savePrefs() {
            if (!writeStored(PREFS_KEY, prefs)) { notify('info', 'Browser storage is unavailable; this preference lasts for this page only.'); }
        }
        function applyWorkspacePrefs() {
            const wrapper = workspace();
            setClass(wrapper, 'casting-hide-description', !prefs.description);
            setClass(wrapper, 'casting-hide-sample', !prefs.sample);
            setClass(wrapper, 'casting-hide-tags', !prefs.tags);
            setClass(wrapper, 'casting-focus', prefs.focus);
            applyCardVisibility();
        }
        function applyCardVisibility() {
            if (typeof document.querySelectorAll !== 'function') { return; }
            for (const [key, mark] of [['description', 'description'], ['sample', 'sample'], ['tags', 'tags']]) {
                for (const part of document.querySelectorAll('[data-casting-visibility="' + mark + '"]')) {
                    if (part) { part.hidden = !prefs[key]; }
                }
            }
        }

        function valuesFor(voice, mark, field) {
            if (field === 'tone') { return ((voice.tags || {}).tones || []); }
            if (field === 'texture') { return ((voice.tags || {}).textures || []); }
            if (field === 'tag') { return mark.tags || []; }
            if (field === 'collection') { return mark.collections || []; }
            if (field === 'star') { return mark.stars || []; }
            if (field === 'circle') { return mark.circles || []; }
            return [];
        }
        function valueSet(voice, mark, field) {
            return new Set(valuesFor(voice, mark, field).map(value => String(value).toLowerCase()));
        }
        function matches(voice, mark) {
            const sets = new Map();
            for (const [key, chipState] of st.chips) {
                const pair = chipPair(key);
                if (!pair) { continue; }
                if (pair.field === 'circle' && chipState === 'exclude') { continue; }
                if (!sets.has(pair.field)) { sets.set(pair.field, valueSet(voice, mark, pair.field)); }
                const present = sets.get(pair.field).has(pair.value.toLowerCase());
                if (chipState === 'include' && !present) { return false; }
                if (chipState === 'exclude' && present) { return false; }
            }
            for (const ids of st.circleSnapshots.values()) {
                if (ids.has(voice.id)) { return false; }
            }
            return true;
        }
        function circleChipState(color) {
            const key = chipKey('circle', color);
            const stateName = st.chips.get(key) || 'off';
            if (stateName === 'exclude' && !st.circlePending.has(color)) { return 'off'; }
            return stateName;
        }
        function cycleChip(field, value) {
            const key = chipKey(field, value);
            const current = field === 'circle' ? circleChipState(value) : (st.chips.get(key) || 'off');
            const next = {off: 'include', include: 'exclude', exclude: 'off'}[current];
            if (next === 'off') { st.chips.delete(key); } else { st.chips.set(key, next); }
            if (field === 'circle') {
                if (next === 'exclude') {
                    st.circlePending.add(value);
                } else {
                    st.circlePending.delete(value);
                }
                if (next === 'include') { st.circleCommitted.delete(value); st.circleSnapshots.delete(value); }
            }
            filtersChanged();
            render();
        }
        function filtersChanged() {
            if (typeof playback.stop === 'function') { playback.stop(); }
            st.pageIndex = 0;
            st.loaded = st.pageSize;
            pruneSelection();
            syncToolbar(visible());
            syncPagination(visible());
        }
        function clearFilters() {
            st.chips.clear();
            st.circlePending.clear();
            st.circleCommitted.clear();
            st.circleSnapshots.clear();
            st.selection.clear();
            st.pageIndex = 0;
            st.loaded = st.pageSize;
            if (typeof playback.stop === 'function') { playback.stop(); }
            syncToolbar(visible());
            syncPagination(visible());
        }
        function refreshCircles() {
            captureCircleExclusions(st.circlePending);
            filtersChanged();
            render();
        }
        function captureCircleExclusions(colors) {
            st.circleCommitted = new Set(colors);
            st.circleSnapshots = new Map([...colors].map(color => [color, new Set(
                state().voices.filter(voice => (getMark(voice.id).circles || []).includes(color)).map(voice => voice.id))]));
        }
        function pendingIndicator() {
            const staged = new Set([...st.circlePending, ...st.circleCommitted]);
            const changed = [...staged].some(color => st.circlePending.has(color) !== st.circleCommitted.has(color)
                || state().voices.some(voice => (getMark(voice.id).circles || []).includes(color)
                    !== Boolean(st.circleSnapshots.get(color)?.has(voice.id))));
            if (changed) {
                return 'Circle exclusions staged: ' + [...staged].sort().join(', ') + '. Refresh results to apply.';
            }
            if (st.circleCommitted.size) {
                return 'Circle exclusions active: ' + [...st.circleCommitted].sort().join(', ') + '.';
            }
            return '';
        }

        function chipButton(field, value, count) {
            const stateName = field === 'circle' ? circleChipState(value) : (st.chips.get(chipKey(field, value)) || 'off');
            const chip = node('button', undefined, 'btn btn-sm casting-filter ' + chipClasses[stateName]);
            chip.type = 'button';
            chip.dataset.chip = chipKey(field, value);
            chip.dataset.state = stateName;
            chip.dataset.castingFocus = 'chip:' + chipKey(field, value);
            chip.append(node('i', undefined, 'fas ' + chipIcons[stateName] + ' me-1'), node('span', value + ' ' + count));
            const label = stateNames[stateName] + ' ' + FIELD_LABEL[field] + ' ' + value
                + (count === 1 ? ' (1 voice)' : ' (' + count + ' voices)');
            chip.setAttribute('aria-label', label);
            chip.setAttribute('title', label);
            chip.setAttribute('aria-pressed', String(stateName === 'include'));
            chip.addEventListener('click', () => cycleChip(field, value));
            return chip;
        }
        function countChips(list, field) {
            const counts = new Map();
            for (const voice of list) {
                for (const value of new Set(valuesFor(voice, getMark(voice.id), field))) {
                    const key = String(value).toLowerCase();
                    const entry = counts.get(key);
                    if (entry) { entry[1]++; } else { counts.set(key, [String(value), 1]); }
                }
            }
            return [...counts.values()].sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
        }
        function renderGroup(host, field, title, list, limit) {
            if (!host) { return; }
            const focus = captureFocus();
            const registry = field === 'collection' ? (state().voices || list) : list;
            const counts = countChips(registry, field);
            const shown = counts.slice(0, limit);
            for (const [key] of st.chips) {
                const pair = chipPair(key);
                if (!pair || pair.field !== field) { continue; }
                if (!shown.some(([value]) => String(value).toLowerCase() === pair.value.toLowerCase())) {
                    shown.push([pair.value, 0]);
                }
            }
            if (field === 'stars' || field === 'star') {
                for (const color of COLORS) {
                    if (!shown.some(([value]) => value === color)) { shown.push([color, 0]); }
                }
            }
            if (field === 'circle') {
                for (const color of COLORS) {
                    if (!shown.some(([value]) => value === color)) { shown.push([color, 0]); }
                }
            }
            const wrapper = node('div', undefined, 'mb-2');
            if (title) { wrapper.append(node('div', title, 'small text-muted mb-1')); }
            const row = node('div', undefined, 'd-flex flex-wrap gap-1');
            row.append(...shown.map(([value, count]) => chipButton(field, value, count)));
            wrapper.append(row);
            host.replaceChildren(wrapper);
            restoreFocus(focus);
        }
        function renderFilters(list) {
            const defs = [['tone', 'tones', 'Tone', MAX_TONE_CHIPS], ['texture', 'textures', 'Texture', MAX_TEXTURE_CHIPS]];
            for (const [field, id, title, limit] of defs) {
                const external = document.getElementById('casting-' + id);
                const host = external || null;
                if (host) {
                    renderGroup(host, field, '', list, limit);
                } else if (ui.filterHost) {
                    let group = document.getElementById('casting-research-filter-' + field);
                    if (!group) {
                        group = node('div');
                        group.id = 'casting-research-filter-' + field;
                        ui.filterHost.append(group);
                    }
                    renderGroup(group, field, title, list, limit);
                }
            }
            if (!ui.filterHost) { return; }
            for (const [field, title, limit] of [['tag', 'Custom tags', MAX_VALUE_CHIPS],
                                                 ['collection', 'Research collections', MAX_VALUE_CHIPS],
                                                 ['star', 'Stars', COLORS.length]]) {
                let group = document.getElementById('casting-research-filter-' + field);
                if (!group) {
                    group = node('div');
                    group.id = 'casting-research-filter-' + field;
                    ui.filterHost.append(group);
                }
                renderGroup(group, field, title, list, limit);
            }
            let circles = document.getElementById('casting-research-filter-circle');
            if (!circles) {
                circles = node('div');
                circles.id = 'casting-research-filter-circle';
                ui.filterHost.append(circles);
            }
            renderGroup(circles, 'circle', 'Circles (exclude is staged)', list, COLORS.length);
            let pending = document.getElementById('casting-research-circle-pending');
            if (!pending) {
                pending = node('p', undefined, 'small text-warning mb-2');
                pending.id = 'casting-research-circle-pending';
                pending.setAttribute('role', 'status');
                ui.filterHost.append(pending);
            }
            pending.textContent = pendingIndicator();
            let boxes = document.getElementById('casting-research-circle-excludes');
            if (!boxes) {
                boxes = node('div', undefined, 'mb-2');
                boxes.id = 'casting-research-circle-excludes';
                ui.filterHost.append(boxes);
            }
            const focus = captureFocus();
            boxes.replaceChildren(...COLORS.map(color => {
                const label = node('label', undefined, 'form-check form-check-inline small mb-0');
                const box = node('input', undefined, 'form-check-input');
                box.type = 'checkbox';
                box.id = 'casting-research-circle-' + color;
                box.checked = st.circlePending.has(color);
                box.dataset.castingFocus = 'circle-exclude:' + color;
                box.setAttribute('aria-label', 'Exclude ' + color + ' circles (takes effect after Refresh results)');
                box.addEventListener('change', () => setCircleExclusion(color, box.checked));
                label.append(box, node('span', 'Exclude ' + color, 'ms-1'));
                return label;
            }));
            restoreFocus(focus);
        }
        function setCircleExclusion(color, checked) {
            const key = chipKey('circle', color);
            if (checked) {
                st.circlePending.add(color);
                st.chips.set(key, 'exclude');
            } else {
                st.circlePending.delete(color);
                if ((st.chips.get(key) || 'off') === 'exclude') { st.chips.delete(key); }
            }
            filtersChanged();
            render();
        }

        function pruneSelection() {
            const ids = new Set(visible().map(voice => voice.id));
            for (const id of [...st.selection]) {
                if (!ids.has(id)) { st.selection.delete(id); }
            }
        }
        function isSelected(id) { return st.selection.has(id); }
        function toggleSelection(id, checked) {
            const want = checked === undefined ? !st.selection.has(id) : Boolean(checked);
            if (want) {
                if (st.selection.size >= MAX_SELECTION) {
                    notify('warning', 'Up to ' + MAX_SELECTION + ' voices can be selected at once.');
                    return false;
                }
                st.selection.add(id);
            } else {
                st.selection.delete(id);
            }
            syncToolbar(visible());
            return want;
        }
        function selectFiltered() {
            const list = visible();
            if (list.length > MAX_SELECTION) {
                notify('warning', 'Selecting ' + list.length + ' filtered voices is over the ' + MAX_SELECTION
                    + '-voice limit. Narrow the filters.');
                return false;
            }
            st.selection = new Set(list.map(voice => voice.id));
            render();
            return true;
        }
        function selectShownPage() {
            const list = visible(), page = pageSlice(list);
            const next = new Set(st.selection);
            for (const voice of page) { next.add(voice.id); }
            if (next.size > MAX_SELECTION) {
                notify('warning', 'Selecting this page would exceed ' + MAX_SELECTION + ' voices. Clear some selections first.');
                return false;
            }
            st.selection = next;
            render();
        }
        function invertSelection() {
            const list = visible();
            const next = new Set(list.filter(voice => !st.selection.has(voice.id)).map(voice => voice.id));
            if (next.size > MAX_SELECTION) {
                notify('warning', 'Inverting would select more than ' + MAX_SELECTION + ' voices. Narrow the filters.');
                return false;
            }
            st.selection = next;
            render();
            return true;
        }
        function clearSelection() {
            st.selection.clear();
            render();
        }

        function queueWrite(task) {
            const epoch = state().epoch;
            const guarded = () => {
                if (state().epoch !== epoch || st.needsReload) { return {ok: false, stale: true}; }
                return task();
            };
            const run = st.chain.then(guarded, guarded);
            st.chain = run.catch(() => {});
            return run;
        }
        function expectedFor(id, fields) {
            const mark = getMark(id);
            const expected = {};
            for (const field of fields) { expected[field] = clone(mark[field]); }
            return expected;
        }
        function applyServerMarks(marks) {
            if (!marks || typeof marks !== 'object') { return; }
            for (const [id, mark] of Object.entries(marks)) {
                if (mark && typeof mark === 'object') { state().marks[id] = normalize(mark); }
            }
        }
        function pushUndo(action, changes) {
            st.undo.push({action, changes: changes.map(change => ({voice_id: change.voice_id,
                                                                  patch: {...change.patch}, before: clone(change.expected)}))});
            if (st.undo.length > MAX_UNDO) { st.undo.shift(); }
            st.redo = [];
        }
        function isConflict(error) {
            return Boolean(error) && (error.status === 409
                || /changed since|conflict|409/i.test(String(error.message || error)));
        }
        function handleWriteError(action, error) {
            st.needsReload = true;
            if (isConflict(error)) {
                notify('warning', action + ' was refused because the library changed elsewhere. Reload the casting library to reconcile.');
                return;
            }
            st.needsReload = true;
            notify('warning', action + ' may not have been saved. Reload the casting library to reconcile before trying again.');
            actionFailure(action, error, 'The casting library may be out of date. Reload it before saving again.');
        }
        function applyBulk(action, ids, patchFactory, opts = {}) {
            if (!ids.length) {
                notify('info', 'Select at least one voice first.');
                return Promise.resolve({ok: false});
            }
            if (st.needsReload) {
                notify('warning', 'Reload the casting library to reconcile before saving again.');
                return Promise.resolve({ok: false});
            }
            const epoch = state().epoch;
            return queueWrite(async () => {
                const changes = [];
                for (const id of ids) {
                    const mark = getMark(id);
                    const patch = patchFactory(mark, id);
                    if (!patch) { continue; }
                    const keys = Object.keys(patch);
                    if (!keys.length) { continue; }
                    if (keys.some(field => SHAPES.includes(field) && shapeLocked(field))) {
                        notify('info', 'Unlock the affected marks before changing them.');
                        return {ok: false, locked: true};
                    }
                    changes.push({voice_id: id, patch: {...patch}, expected: expectedFor(id, keys)});
                }
                if (!changes.length) {
                    notify('info', 'Nothing would change for the selected voices.');
                    return {ok: false, empty: true};
                }
                st.saving = true;
                syncToolbar(visible());
                try {
                    const result = await API.post('/api/casting/marks/batch',
                        {changes, action: bounded(action, MAX_ACTION)});
                    if (state().epoch !== epoch) { requireReconciliation(); return {ok: false, stale: true}; }
                    applyServerMarks(result.marks);
                    pruneSelection();
                    st.activity = activityList(result.activity);
                    if (opts.undo !== false) { pushUndo(bounded(action, MAX_ACTION), changes); }
                    st.needsReload = false;
                    render();
                    if (typeof options.afterSave === 'function') { options.afterSave(); }
                    return {ok: true};
                } catch (error) {
                    if (state().epoch !== epoch) { return {ok: false, stale: true}; }
                    handleWriteError('Save casting marks', error);
                    return {ok: false, error};
                } finally {
                    if (state().epoch === epoch) {
                        st.saving = false;
                        syncToolbar(visible());
                    }
                }
            });
        }
        function saveMark(voice, patch) {
            const keys = Object.keys(patch || {});
            const action = (keys.includes('notes') ? 'Save notes' : 'Set ' + (keys.length ? keys.join(', ') : 'mark'))
                + ' for one voice';
            return applyBulk(action, [voice.id], () => ({...patch}));
        }
        function requireReconciliation() {
            st.needsReload = true;
            notify('warning', 'A save finished after the library reloaded. Reload again to reconcile before editing.');
            syncToolbar(visible());
        }
        function undoMove(direction) {
            const from = direction === 'undo' ? st.undo : st.redo;
            const to = direction === 'undo' ? st.redo : st.undo;
            if (!from.length || st.saving || st.needsReload) { return Promise.resolve(); }
            const epoch = state().epoch;
            return queueWrite(async () => {
                const entry = from[from.length - 1];
                if (!entry) { return; }
                if (entry.changes.some(change => SHAPES.some(shape => Object.hasOwn(change.patch, shape) && shapeLocked(shape)))) {
                    notify('info', 'Unlock the affected marks before undoing or redoing this change.');
                    return;
                }
                const changes = entry.changes.map(change => {
                    const patch = direction === 'undo' ? change.before : change.patch;
                    const expected = direction === 'undo' ? change.patch : change.before;
                    return {voice_id: change.voice_id, patch: clone(patch), expected: clone(expected)};
                });
                st.saving = true;
                syncToolbar(visible());
                try {
                    const result = await API.post('/api/casting/marks/batch',
                        {changes, action: bounded((direction === 'undo' ? 'Undo: ' : 'Redo: ') + entry.action, MAX_ACTION)});
                    if (state().epoch !== epoch) { requireReconciliation(); return; }
                    applyServerMarks(result.marks);
                    st.activity = activityList(result.activity);
                    from.pop();
                    to.push(entry);
                    if (to.length > MAX_UNDO) { to.shift(); }
                    render();
                } catch (error) {
                    if (state().epoch !== epoch) { return; }
                    handleWriteError(direction === 'undo' ? 'Undo casting change' : 'Redo casting change', error);
                } finally {
                    if (state().epoch === epoch) {
                        st.saving = false;
                        syncToolbar(visible());
                    }
                }
            });
        }
        function shapeLocked(shape) { return shape === 'stars' ? prefs.lockStars : prefs.lockCircles; }
        function writesBlocked() { return st.saving || st.needsReload; }
        function blockedNotice() {
            if (st.needsReload) { notify('warning', 'Reload the casting library to reconcile before saving again.'); }
            return true;
        }
        function toggleShapeColor(voice, shape, color) {
            if (shapeLocked(shape)) {
                notify('info', SHAPE_LABEL[shape] + ' marks are locked. Unlock them in the research toolbar first.');
                return Promise.resolve({ok: false});
            }
            return applyBulk('Toggle ' + shape.slice(0, -1) + ' ' + color, [voice.id], mark => {
                if (shapeLocked(shape)) { notify('info', 'Unlock these marks before changing them.'); return null; }
                const values = mark[shape] || [];
                return {[shape]: values.includes(color) ? values.filter(value => value !== color) : [...values, color]};
            });
        }
        function tagPatchWithout(mark, value) {
            const colors = copyTagColors(mark.tag_colors), keys = Object.keys(colors)
                .filter(tag => tag.toLowerCase() === value.toLowerCase());
            if (!keys.length) { return null; }
            for (const key of keys) { delete colors[key]; }
            return colors;
        }
        function changeMembership(voice, field, value, adding) {
            const action = (adding ? 'Add ' : 'Remove ') + (field === 'tags' ? 'tag' : 'research collection membership');
            return applyBulk(action, [voice.id], mark => {
                const values = mark[field] || [];
                const has = values.some(item => item.toLowerCase() === value.toLowerCase());
                if (adding === has) { return null; }
                const limit = field === 'tags' ? MAX_TAGS : MAX_COLLECTIONS;
                if (adding && values.length >= limit) {
                    notify('info', 'This voice already has the maximum ' + limit + ' ' + field + '.');
                    return null;
                }
                const patch = {[field]: adding ? [...values, value] : values.filter(item => item.toLowerCase() !== value.toLowerCase())};
                if (field === 'tags' && !adding) {
                    const colors = tagPatchWithout(mark, value);
                    if (colors) { patch.tag_colors = colors; }
                }
                return patch;
            });
        }
        function setTagColor(voice, tag, color) {
            if (color && !COLORS.includes(color)) {
                notify('info', 'That tag colour is not in the casting palette.');
                return Promise.resolve({ok: false});
            }
            return applyBulk('Set tag colour', [voice.id], mark => {
                if (!(mark.tags || []).includes(tag)) {
                    notify('info', 'That tag is no longer on this voice.');
                    return null;
                }
                const colors = copyTagColors(mark.tag_colors);
                if (color) {
                    Object.defineProperty(colors, tag, {value: color, enumerable: true, writable: true, configurable: true});
                } else {
                    delete colors[tag];
                }
                return {tags: [...mark.tags], tag_colors: colors};
            });
        }

        function makeButton(id, text, cls, action, label) {
            const button = node('button', undefined, cls || 'btn btn-sm btn-outline-secondary');
            button.type = 'button';
            button.id = 'casting-research-' + id;
            button.append(node('span', text));
            if (label) { button.setAttribute('aria-label', label); }
            button.addEventListener('click', action);
            return button;
        }
        function makeSelect(id, optionsList, value, label, onChange) {
            const select = node('select', undefined, 'form-select form-select-sm w-auto');
            select.id = 'casting-research-' + id;
            select.setAttribute('aria-label', label);
            fillOptions(select, optionsList);
            select.value = value;
            select.addEventListener('change', () => onChange(select.value));
            return select;
        }
        function fillOptions(select, optionsList) {
            select.replaceChildren(...optionsList.map(([value, text]) => {
                const option = node('option', text);
                option.value = value;
                return option;
            }));
        }
        function makeInput(id, type, label, value, maxlength) {
            const input = node('input', undefined, type === 'number' ? 'form-control form-control-sm w-auto' : 'form-control form-control-sm');
            input.id = 'casting-research-' + id;
            input.type = type;
            input.setAttribute('aria-label', label);
            if (value !== undefined) { input.value = String(value); }
            if (maxlength) { input.setAttribute('maxlength', String(maxlength)); }
            return input;
        }
        function makeCheck(id, label, checked) {
            const labelNode = node('label', undefined, 'form-check form-check-inline small mb-0');
            const input = node('input', undefined, 'form-check-input');
            input.id = 'casting-research-' + id;
            input.type = 'checkbox';
            input.checked = Boolean(checked);
            labelNode.append(input, node('span', label, 'ms-1'));
            return {labelNode, input};
        }
        function buildToolbar(host) {
            const selection = node('div', undefined, 'casting-research-row mb-2');
            ui.selectionCount = node('span', 'No voices selected', 'small text-muted');
            ui.selectionCount.id = 'casting-research-selection-count';
            ui.selectionCount.setAttribute('role', 'status');
            ui.selectionCount.setAttribute('aria-live', 'polite');
            ui.selectFiltered = makeButton('select-filtered', 'Select all filtered', 'btn btn-sm btn-outline-secondary', selectFiltered);
            ui.selectPage = makeButton('select-page', 'Select shown page', 'btn btn-sm btn-outline-secondary', selectShownPage);
            ui.invert = makeButton('invert', 'Invert selection', 'btn btn-sm btn-outline-secondary', invertSelection);
            ui.clearSelection = makeButton('clear-selection', 'Clear selection', 'btn btn-sm btn-outline-secondary', clearSelection);
            selection.append(ui.selectionCount, ui.selectFiltered, ui.selectPage, ui.invert, ui.clearSelection);
            host.append(selection);

            const bulk = node('div', undefined, 'casting-research-row casting-bulkbar mb-2');
            bulk.append(node('span', 'Bulk change', 'small fw-bold'));
            ui.bulkStatus = makeSelect('bulk-status',
                [['', 'Status: no change'], ...['unreviewed', 'shortlist', 'callback', 'reject'].map(value => [value, value[0].toUpperCase() + value.slice(1)])],
                '', 'Bulk review status', value => {
                    if (!value) { return; }
                    const action = 'Set status for selected voices';
                    applyBulk(action, [...st.selection], () => ({status: value}));
                    ui.bulkStatus.value = '';
                });
            ui.bulkPriority = makeSelect('bulk-priority',
                [['', 'Priority: no change'], ...['none', 'low', 'medium', 'high'].map(value => [value, value === 'none' ? 'No priority' : value[0].toUpperCase() + value.slice(1)])],
                '', 'Bulk priority', value => {
                    if (!value) { return; }
                    applyBulk('Set priority for selected voices', [...st.selection], () => ({priority: value}));
                    ui.bulkPriority.value = '';
                });
            bulk.append(ui.bulkStatus, ui.bulkPriority);
            ui.bulkTag = makeInput('bulk-tag', 'text', 'Tag for bulk change', '', MAX_TAG_LENGTH);
            ui.bulkTag.setAttribute('placeholder', 'Tag');
            ui.bulkTagAdd = makeButton('bulk-tag-add', 'Add tag', 'btn btn-sm btn-outline-success', () => bulkTag('add'));
            ui.bulkTagRemove = makeButton('bulk-tag-remove', 'Remove tag', 'btn btn-sm btn-outline-danger', () => bulkTag('remove'));
            bulk.append(ui.bulkTag, ui.bulkTagAdd, ui.bulkTagRemove);
            ui.bulkCollection = makeInput('bulk-collection', 'text', 'Research collection for bulk change', '', MAX_COLLECTION_LENGTH);
            ui.bulkCollection.setAttribute('placeholder', 'Research collection');
            ui.bulkCollectionAdd = makeButton('bulk-collection-add', 'Add to collection', 'btn btn-sm btn-outline-success', () => bulkCollection('add'));
            ui.bulkCollectionRemove = makeButton('bulk-collection-remove', 'Remove from collection', 'btn btn-sm btn-outline-danger', () => bulkCollection('remove'));
            bulk.append(ui.bulkCollection, ui.bulkCollectionAdd, ui.bulkCollectionRemove);
            host.append(bulk);

            const marksBulk = node('div', undefined, 'casting-research-row mb-2');
            marksBulk.append(node('span', 'Marks', 'small fw-bold'));
            ui.bulkShape = makeSelect('bulk-shape', [['stars', 'Stars'], ['circles', 'Circles']], 'stars', 'Shape for bulk marks', () => syncToolbar(visible()));
            ui.bulkColor = makeSelect('bulk-color', COLORS.map(color => [color, COLOR_LABEL[color]]), 'red', 'Colour for bulk marks', () => {});
            ui.bulkMarkAdd = makeButton('bulk-mark-add', 'Add mark', 'btn btn-sm btn-outline-success', () => bulkMark('add'));
            ui.bulkMarkRemove = makeButton('bulk-mark-remove', 'Remove mark', 'btn btn-sm btn-outline-danger', () => bulkMark('remove'));
            marksBulk.append(ui.bulkShape, ui.bulkColor, ui.bulkMarkAdd, ui.bulkMarkRemove);
            ui.clearShape = makeSelect('clear-shape', [['stars', 'Stars'], ['circles', 'Circles'], ['both', 'Both shapes']], 'stars', 'Shape to clear', () => syncToolbar(visible()));
            ui.clearColor = makeSelect('clear-color', [['all', 'All colours'], ...COLORS.map(color => [color, COLOR_LABEL[color]])], 'all', 'Colour to clear', () => {});
            ui.clearMarks = makeButton('clear-marks', 'Clear marks…', 'btn btn-sm btn-outline-warning', clearMarks);
            marksBulk.append(ui.clearShape, ui.clearColor, ui.clearMarks);
            ui.undo = makeButton('undo', 'Undo', 'btn btn-sm btn-outline-secondary', () => undoMove('undo'));
            ui.redo = makeButton('redo', 'Redo', 'btn btn-sm btn-outline-secondary', () => undoMove('redo'));
            marksBulk.append(ui.undo, ui.redo);
            host.append(marksBulk);

            const locks = node('div', undefined, 'casting-research-row mb-2');
            ui.lockStars = makeCheck('lock-stars', 'Lock star marks', prefs.lockStars);
            ui.lockCircles = makeCheck('lock-circles', 'Lock circle marks', prefs.lockCircles);
            ui.lockStars.input.addEventListener('change', () => {
                prefs.lockStars = Boolean(ui.lockStars.input.checked);
                savePrefs();
                syncToolbar(visible());
                render();
            });
            ui.lockCircles.input.addEventListener('change', () => {
                prefs.lockCircles = Boolean(ui.lockCircles.input.checked);
                savePrefs();
                syncToolbar(visible());
                render();
            });
            locks.append(ui.lockStars.labelNode, ui.lockCircles.labelNode);
            host.append(locks);

            const playbackRow = node('div', undefined, 'casting-research-row mb-2');
            playbackRow.append(node('span', 'Playback', 'small fw-bold'));
            ui.seconds = makeInput('play-seconds', 'number', 'Seconds per voice', prefs.seconds);
            ui.seconds.setAttribute('min', '0');
            ui.seconds.setAttribute('max', '120');
            ui.seconds.setAttribute('step', '1');
            ui.seconds.addEventListener('change', () => {
                const value = Number(ui.seconds.value);
                if (!Number.isInteger(value) || value < 0 || value > 120) {
                    ui.seconds.value = prefs.full ? '0' : String(prefs.seconds);
                    notify('info', 'Use 0 for full samples or a whole number of seconds from 1 to 120.');
                    return;
                }
                if (value === 0) {
                    prefs.full = true;
                    savePrefs();
                    setPlayback(prefs.seconds, true);
                    syncToolbar(visible());
                    return;
                }
                prefs.seconds = value;
                prefs.full = false;
                savePrefs();
                setPlayback(value, false);
                syncToolbar(visible());
            });
            ui.full = makeCheck('play-full', 'Full samples', prefs.full);
            ui.full.input.addEventListener('change', () => {
                prefs.full = Boolean(ui.full.input.checked);
                savePrefs();
                setPlayback(prefs.seconds, prefs.full);
                syncToolbar(visible());
            });
            ui.prev = makeButton('play-prev', 'Previous', 'btn btn-sm btn-outline-secondary', () => playback.prev && playback.prev(), 'Previous voice in the playback queue');
            ui.next = makeButton('play-next', 'Next', 'btn btn-sm btn-outline-secondary', () => playback.next && playback.next(), 'Next voice in the playback queue');
            ui.stop = makeButton('play-stop', 'Stop', 'btn btn-sm btn-outline-secondary', () => playback.stop && playback.stop(), 'Stop the playback queue');
            ui.random = makeButton('play-random', 'Random', 'btn btn-sm btn-outline-secondary', () => playback.random && playback.random(), 'Play a random filtered voice with audio');
            playbackRow.append(ui.seconds, node('span', 's per voice', 'small text-muted'), ui.full.labelNode,
                               ui.prev, ui.next, ui.stop, ui.random);
            host.append(playbackRow);

            const viewRow = node('div', undefined, 'casting-research-row mb-2');
            ui.showDescription = makeCheck('show-description', 'Description', prefs.description);
            ui.showSample = makeCheck('show-sample', 'Sample line', prefs.sample);
            ui.showTags = makeCheck('show-tags', 'Tags', prefs.tags);
            ui.focus = makeCheck('focus', 'Focus cards', prefs.focus);
            for (const [control, key] of [[ui.showDescription, 'description'], [ui.showSample, 'sample'],
                                          [ui.showTags, 'tags'], [ui.focus, 'focus']]) {
                control.input.addEventListener('change', () => {
                    prefs[key] = Boolean(control.input.checked);
                    savePrefs();
                    applyWorkspacePrefs();
                });
            }
            viewRow.append(ui.showDescription.labelNode, ui.showSample.labelNode, ui.showTags.labelNode, ui.focus.labelNode);
            host.append(viewRow);

            const viewsRow = node('div', undefined, 'casting-research-row mb-2');
            ui.viewName = makeInput('view-name', 'text', 'Saved view name', '', 60);
            ui.viewName.setAttribute('placeholder', 'View name');
            ui.viewSelect = makeSelect('view', [['', 'Saved views']], '', 'Saved view', () => {});
            ui.viewSave = makeButton('view-save', 'Save view', 'btn btn-sm btn-outline-primary', saveView);
            ui.viewApply = makeButton('view-apply', 'Apply view', 'btn btn-sm btn-outline-secondary', () => applyView(ui.viewSelect.value));
            ui.viewDelete = makeButton('view-delete', 'Delete view', 'btn btn-sm btn-outline-secondary', deleteView);
            viewsRow.append(ui.viewName, ui.viewSave, ui.viewSelect, ui.viewApply, ui.viewDelete);
            host.append(viewsRow);
        }

        function setPlayback(seconds, full) {
            if (typeof playback.set === 'function') { playback.set(seconds, full); }
        }
        function bulkTag(mode) {
            const raw = String(ui.bulkTag.value || '').trim();
            if (!raw) { notify('info', 'Type a tag first.'); return; }
            if (raw.length > MAX_TAG_LENGTH) { notify('info', 'Tags are limited to ' + MAX_TAG_LENGTH + ' characters.'); return; }
            const ids = [...st.selection];
            if (!ids.length) { notify('info', 'Select at least one voice first.'); return; }
            applyBulk(mode === 'add' ? 'Add tag' : 'Remove tag', ids, mark => {
                const tags = mark.tags || [];
                if (mode === 'add') {
                    if (tags.some(tag => tag.toLowerCase() === raw.toLowerCase())) { return null; }
                    if (tags.length >= MAX_TAGS) { return null; }
                    return {tags: [...tags, raw]};
                }
                if (!tags.some(tag => tag.toLowerCase() === raw.toLowerCase())) { return null; }
                const patch = {tags: tags.filter(tag => tag.toLowerCase() !== raw.toLowerCase())};
                const colors = tagPatchWithout(mark, raw);
                if (colors) { patch.tag_colors = colors; }
                return patch;
            });
        }
        function bulkCollection(mode) {
            const raw = String(ui.bulkCollection.value || '').trim();
            if (!raw) { notify('info', 'Type a research collection name first.'); return; }
            if (raw.length > MAX_COLLECTION_LENGTH) {
                notify('info', 'Research collection names are limited to ' + MAX_COLLECTION_LENGTH + ' characters.');
                return;
            }
            const ids = [...st.selection];
            if (!ids.length) { notify('info', 'Select at least one voice first.'); return; }
            applyBulk(mode === 'add' ? 'Add research collection membership' : 'Remove research collection membership', ids, mark => {
                const names = mark.collections || [];
                if (mode === 'add') {
                    if (names.some(name => name.toLowerCase() === raw.toLowerCase())) { return null; }
                    if (names.length >= MAX_COLLECTIONS) { return null; }
                    return {collections: [...names, raw]};
                }
                if (!names.some(name => name.toLowerCase() === raw.toLowerCase())) { return null; }
                return {collections: names.filter(name => name.toLowerCase() !== raw.toLowerCase())};
            });
        }
        function bulkMark(mode) {
            const shape = ui.bulkShape.value, color = ui.bulkColor.value;
            if (shapeLocked(shape)) {
                notify('info', SHAPE_LABEL[shape] + ' marks are locked. Unlock them first.');
                return;
            }
            applyBulk((mode === 'add' ? 'Add ' : 'Remove ') + shape.slice(0, -1) + ' ' + color, [...st.selection], mark => {
                const values = mark[shape] || [];
                const has = values.includes(color);
                if (mode === 'add') { return has ? null : {[shape]: [...values, color]}; }
                return has ? {[shape]: values.filter(value => value !== color)} : null;
            });
        }
        function clearMarks() {
            const shapes = ui.clearShape.value === 'both' ? SHAPES : [ui.clearShape.value];
            const color = ui.clearColor.value;
            for (const shape of shapes) {
                if (shapeLocked(shape)) {
                    notify('info', SHAPE_LABEL[shape] + ' marks are locked. Unlock them before clearing.');
                    return;
                }
            }
            const ids = [...st.selection];
            if (!ids.length) { notify('info', 'Select at least one voice first.'); return; }
            if (!confirmAction) { return; }
            const scope = (color === 'all' ? 'all colours' : COLOR_LABEL[color] || color) + ' of ' + shapes.map(shape => shape).join(' and ');
            confirmAction('Clear ' + scope + ' on ' + ids.length + ' selected voice(s)? Tags, status, priority, notes and research collections are kept.',
                {title: 'Clear marks only?', actionLabel: 'Clear marks'}).then(answer => {
                if (!answer) { return; }
                applyBulk('Clear marks (' + scope + ')', ids, mark => {
                    const patch = {};
                    for (const shape of shapes) {
                        const values = mark[shape] || [];
                        const next = color === 'all' ? [] : values.filter(value => value !== color);
                        if (next.length !== values.length) { patch[shape] = next; }
                    }
                    return Object.keys(patch).length ? patch : null;
                });
            });
        }

        function readViews() {
            const raw = readStored(VIEWS_KEY, []);
            if (!Array.isArray(raw)) { return []; }
            const views = [];
            for (const item of raw.slice(0, MAX_VIEWS)) {
                const view = sanitizeView(item);
                if (view) { views.push(view); }
            }
            return views;
        }
        function sanitizeView(item) {
            if (!item || typeof item !== 'object' || Array.isArray(item)) { return null; }
            const name = typeof item.name === 'string' ? item.name.trim().slice(0, 60) : '';
            if (!name) { return null; }
            const view = {name, search: '', variant: '', selects: {}, sort: 'name', chips: [], circleExclude: [],
                          pageSize: DEFAULT_PAGE, seconds: DEFAULT_SECONDS, full: false,
                          description: true, sample: true, tags: true, focus: false};
            if (typeof item.search === 'string') { view.search = item.search.slice(0, 500); }
            if (typeof item.variant === 'string') { view.variant = item.variant.slice(0, 500); }
            if (item.selects && typeof item.selects === 'object' && !Array.isArray(item.selects)) {
                for (const key of ['book', 'gender', 'age', 'status', 'health']) {
                    if (typeof item.selects[key] === 'string') { view.selects[key] = item.selects[key].slice(0, 80); }
                }
            }
            if (SORTS.includes(item.sort)) { view.sort = item.sort; }
            if (Array.isArray(item.chips)) {
                for (const chip of item.chips.slice(0, 200)) {
                    if (!chip || typeof chip !== 'object') { continue; }
                    const field = String(chip.field || ''), value = String(chip.value || ''), stateName = String(chip.state || '');
                    if (!FILTER_FIELDS.includes(field) || !value || !['include', 'exclude'].includes(stateName)) { continue; }
                    if (value.length > 80) { continue; }
                    view.chips.push({field, value, state: stateName});
                }
            }
            if (Array.isArray(item.circleExclude)) {
                view.circleExclude = item.circleExclude.filter(color => COLORS.includes(color)).slice(0, COLORS.length);
            }
            if (PAGE_SIZES.includes(item.pageSize)) { view.pageSize = item.pageSize; }
            if (Number.isInteger(item.seconds) && item.seconds >= 1 && item.seconds <= 120) { view.seconds = item.seconds; }
            if (typeof item.full === 'boolean') { view.full = item.full; }
            for (const key of ['description', 'sample', 'tags', 'focus']) {
                if (typeof item[key] === 'boolean') { view[key] = item[key]; }
            }
            return view;
        }
        function captureView(name) {
            const search = document.getElementById('casting-search');
            const sort = document.getElementById('casting-sort');
            const selects = {};
            for (const key of ['book', 'gender', 'age', 'status', 'health']) {
                const control = document.getElementById('casting-' + key);
                if (control) { selects[key] = String(control.value || ''); }
            }
            return {
                name: name.trim().slice(0, 60),
                search: bounded(search ? search.value : '', 500),
                variant: bounded(state().variant || '', 500),
                selects,
                sort: sort && SORTS.includes(sort.value) ? sort.value : 'name',
                chips: [...st.chips].map(([key, stateName]) => {
                    const pair = chipPair(key);
                    return pair ? {field: pair.field, value: pair.value, state: stateName} : null;
                }).filter(Boolean),
                circleExclude: [...st.circlePending].sort(),
                pageSize: st.pageSize,
                seconds: prefs.seconds,
                full: prefs.full,
                description: prefs.description,
                sample: prefs.sample,
                tags: prefs.tags,
                focus: prefs.focus,
            };
        }
        function saveView() {
            const name = String(ui.viewName.value || '').trim();
            if (!name) { notify('info', 'Name the view first.'); return; }
            const view = sanitizeView(captureView(name));
            if (!view) { notify('info', 'That view name cannot be saved.'); return; }
            const views = readViews();
            const at = views.findIndex(item => item.name === view.name);
            if (at >= 0) {
                views[at] = view;
            } else {
                if (views.length >= MAX_VIEWS) {
                    notify('warning', 'Up to ' + MAX_VIEWS + ' saved views are supported. Delete one first.');
                    return;
                }
                views.push(view);
            }
            if (!writeStored(VIEWS_KEY, views)) {
                notify('warning', 'Browser storage is unavailable; the view could not be saved.');
                return;
            }
            st.views = views;
            ui.viewName.value = '';
            syncViews();
            notify('success', 'Saved view ' + view.name + '.');
        }
        function applyView(name) {
            const view = st.views.find(item => item.name === name);
            if (!view) { notify('info', 'Choose a saved view first.'); return; }
            if (typeof playback.stop === 'function') { playback.stop(); }
            const search = document.getElementById('casting-search');
            if (search) { search.value = view.search; }
            for (const [key, value] of Object.entries(view.selects)) {
                const control = document.getElementById('casting-' + key);
                if (control) { control.value = value; }
            }
            const sort = document.getElementById('casting-sort');
            if (sort) { sort.value = view.sort; }
            st.chips.clear();
            for (const chip of view.chips) { st.chips.set(chipKey(chip.field, chip.value), chip.state); }
            st.circlePending = new Set(view.circleExclude);
            captureCircleExclusions(view.circleExclude);
            state().variant = view.variant;
            st.pageSize = view.pageSize;
            st.loaded = view.pageSize;
            st.pageIndex = 0;
            prefs.seconds = view.seconds;
            prefs.full = view.full;
            prefs.pageSize = view.pageSize;
            prefs.description = view.description;
            prefs.sample = view.sample;
            prefs.tags = view.tags;
            prefs.focus = view.focus;
            savePrefs();
            applyWorkspacePrefs();
            setPlayback(view.seconds, view.full);
            pruneSelection();
            render();
        }
        function deleteView() {
            const name = ui.viewSelect.value;
            if (!name) { notify('info', 'Choose a saved view first.'); return; }
            const views = readViews().filter(item => item.name !== name);
            if (!writeStored(VIEWS_KEY, views)) {
                notify('warning', 'Browser storage is unavailable; the view could not be deleted.');
                return;
            }
            st.views = views;
            syncViews();
        }
        function syncViews() {
            if (!ui.viewSelect) { return; }
            const current = ui.viewSelect.value;
            const options = [['', 'Saved views'], ...st.views.map(view => [view.name, view.name])];
            fillOptions(ui.viewSelect, options);
            ui.viewSelect.value = st.views.some(view => view.name === current) ? current : '';
        }

        function syncToolbar(list) {
            if (!ui.selectionCount) { return; }
            const page = pageSlice(list);
            const scope = st.selection.size
                ? st.selection.size + ' selected of ' + list.length + ' filtered (' + page.length + ' shown)'
                : 'No voices selected';
            ui.selectionCount.textContent = scope;
            const blocked = writesBlocked();
            const count = st.selection.size;
            for (const control of [ui.selectFiltered, ui.selectPage, ui.invert, ui.clearSelection]) {
                control.disabled = blocked;
            }
            for (const control of [ui.bulkStatus, ui.bulkPriority, ui.bulkTag, ui.bulkTagAdd, ui.bulkTagRemove,
                                   ui.bulkCollection, ui.bulkCollectionAdd, ui.bulkCollectionRemove,
                                   ui.bulkShape, ui.bulkColor, ui.bulkMarkAdd, ui.bulkMarkRemove,
                                   ui.clearShape, ui.clearColor, ui.clearMarks]) {
                if (control) { control.disabled = blocked || !count; }
            }
            if (ui.bulkMarkAdd) { ui.bulkMarkAdd.disabled = blocked || !count || shapeLocked(ui.bulkShape.value); }
            if (ui.bulkMarkRemove) { ui.bulkMarkRemove.disabled = blocked || !count || shapeLocked(ui.bulkShape.value); }
            if (ui.clearMarks) {
                const shapes = ui.clearShape.value === 'both' ? SHAPES : [ui.clearShape.value];
                ui.clearMarks.disabled = blocked || !count || shapes.some(shapeLocked);
            }
            ui.undo.disabled = blocked || !st.undo.length;
            ui.redo.disabled = blocked || !st.redo.length;
            if (ui.lockStars) { ui.lockStars.input.checked = prefs.lockStars; }
            if (ui.lockCircles) { ui.lockCircles.input.checked = prefs.lockCircles; }
            if (ui.seconds) {
                ui.seconds.value = prefs.full ? '0' : String(prefs.seconds);
                ui.seconds.setAttribute('min', '0');
            }
            if (ui.full) { ui.full.input.checked = prefs.full; }
            for (const [control, key] of [[ui.showDescription, 'description'], [ui.showSample, 'sample'],
                                          [ui.showTags, 'tags'], [ui.focus, 'focus']]) {
                if (control) { control.input.checked = prefs[key]; }
            }
            if (ui.pageSize) { ui.pageSize.value = String(st.pageSize); }
            syncViews();
        }

        function pageSlice(list) {
            if (st.pageMode === 'all') { return list.slice(0, MAX_SELECTION); }
            if (st.pageMode === 'pages') {
                st.pageIndex = Math.max(0, Math.min(st.pageIndex, pageCount(list) - 1));
                const start = st.pageIndex * st.pageSize;
                return list.slice(start, start + st.pageSize);
            }
            return list.slice(0, Math.min(st.loaded, MAX_SELECTION));
        }
        function pageCount(list) { return Math.max(1, Math.ceil(list.length / st.pageSize)); }
        function setPageMode(value) {
            if (value === 'all') {
                const list = visible();
                if (list.length > MAX_SELECTION) {
                    guard('Show all is limited to ' + MAX_SELECTION + ' voices. Narrow the filters.');
                    syncPagination(list);
                    return;
                }
                st.pageMode = 'all';
                disconnectObserver();
            } else if (value === 'pages') {
                st.pageMode = 'pages';
                st.pageIndex = 0;
                disconnectObserver();
            } else {
                st.pageMode = value === 'auto' ? 'auto' : 'load';
                st.loaded = Math.max(st.pageSize, Math.min(st.loaded, MAX_SELECTION));
                if (st.pageMode !== 'auto') { disconnectObserver(); }
            }
            render();
        }
        function guard(text) {
            if (ui.guard) { ui.guard.textContent = text; }
            if (text) { notify('warning', text); }
        }
        function castingActive() {
            const tab = document.getElementById('casting-tab');
            if (typeof document.hidden === 'boolean' && document.hidden) { return false; }
            if (!tab) { return true; }
            return !(tab.hidden || (tab.style && tab.style.display === 'none'));
        }
        function loadMore() {
            if (!['load', 'auto'].includes(st.pageMode)) { return; }
            const list = visible();
            const cap = Math.min(list.length, MAX_SELECTION);
            if (st.loaded >= cap) {
                if (list.length > MAX_SELECTION) { guard('More voices remain, but paging is limited to ' + MAX_SELECTION + ' at a time.'); }
                return;
            }
            st.loaded = Math.min(st.loaded + st.pageSize, cap);
            if (st.loaded >= MAX_SELECTION && list.length > MAX_SELECTION) {
                guard('Showing the first ' + MAX_SELECTION + ' voices. Narrow the filters to see the rest.');
            }
            render();
        }
        function showAll() {
            const list = visible();
            if (list.length > MAX_SELECTION) {
                guard('Show all is limited to ' + MAX_SELECTION + ' voices. Narrow the filters.');
                return;
            }
            st.pageMode = 'all';
            disconnectObserver();
            render();
        }
        function changePage(delta) {
            if (st.pageMode !== 'pages') { return; }
            const total = pageCount(visible());
            st.pageIndex = Math.max(0, Math.min(total - 1, st.pageIndex + delta));
            render();
        }
        function disconnectObserver() {
            if (st.observer && typeof st.observer.disconnect === 'function') { st.observer.disconnect(); }
            st.observer = null;
            st.observerSentinel = null;
        }
        function updateObserver(list) {
            const sentinel = ui.sentinel;
            if (!sentinel || typeof IntersectionObserver !== 'function') { return; }
            const cap = Math.min(list.length, MAX_SELECTION);
            const active = st.pageMode === 'auto' && st.loaded < cap;
            if (!active) { disconnectObserver(); return; }
            if (st.observer) { return; }
            st.observer = new IntersectionObserver(entries => {
                if (!castingActive()) { return; }
                for (const entry of entries || []) {
                    if (entry && entry.isIntersecting) { loadMore(); }
                }
            }, {rootMargin: '200px'});
            st.observerSentinel = sentinel;
            st.observer.observe(sentinel);
        }
        function syncPagination(list) {
            if (!ui.pagination) { return; }
            const page = pageSlice(list);
            const total = pageCount(list);
            const start = st.pageMode === 'pages' ? st.pageIndex * st.pageSize : 0;
            ui.pageStatus.textContent = list.length
                ? 'Showing ' + (start + 1) + '–' + (start + page.length) + ' of ' + list.length
                    + (st.pageMode === 'pages' ? ' (page ' + (st.pageIndex + 1) + ' of ' + total + ')' : '')
                : 'Nothing to show';
            const blocked = st.saving;
            ui.pagePrev.disabled = blocked || st.pageMode !== 'pages' || st.pageIndex <= 0;
            ui.pageNext.disabled = blocked || st.pageMode !== 'pages' || st.pageIndex >= total - 1;
            ui.loadMore.hidden = !['load', 'auto'].includes(st.pageMode) || list.length <= st.loaded;
            ui.loadMore.disabled = blocked;
            ui.showAll.hidden = st.pageMode === 'all';
            ui.showAll.disabled = blocked || list.length === 0;
            if (list.length > MAX_SELECTION) {
                ui.showAll.setAttribute('title', 'Show all is limited to ' + MAX_SELECTION + ' voices; narrow the filters');
            }
            ui.pageSize.disabled = blocked;
            ui.mode.disabled = blocked;
            if (ui.mode.value !== st.pageMode) { ui.mode.value = st.pageMode; }
            if (st.pageMode === 'all' && list.length > MAX_SELECTION && ui.guard) {
                ui.guard.textContent = 'Showing the first ' + MAX_SELECTION + ' voices. Narrow the filters to see the rest.';
            }
            updateObserver(list);
        }

        function activityList(value) {
            if (!Array.isArray(value)) { return []; }
            return value.filter(event => event && typeof event === 'object').slice(-100).map(event => ({
                at: typeof event.at === 'string' ? event.at : '',
                action: typeof event.action === 'string' ? event.action : '',
                count: Number.isInteger(event.count) && event.count >= 0 ? event.count : 0,
            }));
        }
        function renderActivity() {
            const host = document.getElementById('casting-research-activity-list');
            if (!host) { return; }
            host.replaceChildren();
            if (!st.activity.length) {
                host.append(node('p', 'No casting activity yet.', 'small text-muted mb-0'));
                return;
            }
            for (const event of [...st.activity].reverse()) {
                const at = event.at.replace('T', ' ').slice(0, 19);
                host.append(node('p', at + ' · ' + event.action + ' · ' + event.count + ' voice(s)', 'small mb-1'));
            }
        }

        function markButton(voice, shape, color, symbolsOnly) {
            const mark = getMark(voice.id);
            const on = (mark[shape] || []).includes(color);
            const label = SHAPE_LABEL[shape] + ' ' + COLOR_LABEL[color] + ' for ' + voice.name + ': ' + (on ? 'on' : 'off');
            const button = node('button', undefined, 'btn btn-sm btn-outline-secondary casting-mark');
            button.type = 'button';
            button.dataset.castingMark = voice.id;
            button.dataset.shape = shape;
            button.dataset.color = color;
            button.dataset.castingFocus = 'mark:' + voice.id + ':' + shape + ':' + color;
            const icon = node('i', undefined, 'fas ' + (shape === 'stars' ? 'fa-star' : 'fa-circle') + (symbolsOnly ? '' : ' me-1'));
            icon.setAttribute('aria-hidden', 'true');
            button.append(icon);
            if (!symbolsOnly) { button.append(node('span', COLOR_LABEL[color])); }
            button.setAttribute('aria-label', label);
            button.setAttribute('title', label);
            button.setAttribute('aria-pressed', String(on));
            button.disabled = shapeLocked(shape);
            button.addEventListener('click', () => toggleShapeColor(voice, shape, color));
            return button;
        }
        function markRow(voice, shape, symbolsOnly) {
            const row = node('div', undefined, 'd-flex flex-wrap align-items-center gap-1 mb-1');
            if (!symbolsOnly) { row.append(node('span', shape === 'stars' ? 'Stars' : 'Circles', 'small text-muted me-1')); }
            row.append(...COLORS.map(color => markButton(voice, shape, color, symbolsOnly)));
            return row;
        }
        function removalBadge(voice, field, value, actionLabel) {
            const badge = node('span', undefined, 'badge bg-light text-dark border me-1 mb-1');
            badge.append(node('span', value));
            const remove = node('button', undefined, 'btn btn-sm btn-link p-0 ms-1');
            remove.type = 'button';
            remove.setAttribute('aria-label', actionLabel);
            remove.append(node('i', undefined, 'fas fa-times'));
            remove.addEventListener('click', () => {
                changeMembership(voice, field, value, false);
            });
            badge.append(remove);
            return badge;
        }
        function tagEditor(voice) {
            const mark = getMark(voice.id), wrap = node('div', undefined, 'mt-1');
            wrap.dataset.castingVisibility = 'tags';
            const badges = node('div', undefined, 'd-flex flex-wrap');
            for (const tag of mark.tags || []) {
                const cell = node('span', undefined, 'd-inline-flex align-items-center gap-1 me-2 mb-1 casting-tag-editor');
                const color = tagColorOf(mark, tag);
                const badge = node('span', undefined, 'badge casting-custom-tag ' + tagColorClass(color));
                badge.append(node('span', tag));
                const remove = node('button', undefined, 'btn btn-sm btn-link p-0 ms-1'
                    + (color === 'amber' ? ' text-dark' : color ? ' text-white' : ''));
                remove.type = 'button';
                remove.setAttribute('aria-label', 'Remove tag ' + tag + ' from ' + voice.name);
                remove.append(node('i', undefined, 'fas fa-times'));
                remove.addEventListener('click', () => { changeMembership(voice, 'tags', tag, false); });
                badge.append(remove);
                const picker = node('select', undefined, 'form-select form-select-sm w-auto');
                picker.setAttribute('aria-label', 'Colour for tag ' + tag + ' on ' + voice.name);
                for (const [value, text] of [['', 'Neutral'],
                                             ...COLORS.map(name => [name, name[0].toUpperCase() + name.slice(1)])]) {
                    const option = node('option', text);
                    option.value = value;
                    picker.append(option);
                }
                picker.value = color || '';
                picker.dataset.castingFocus = 'tag-color:' + voice.id + ':' + tag;
                picker.disabled = st.needsReload;
                picker.addEventListener('change', async () => {
                    const value = picker.value;
                    picker.value = tagColorOf(getMark(voice.id), tag);
                    await setTagColor(voice, tag, value);
                });
                cell.append(badge, picker);
                badges.append(cell);
            }
            wrap.append(node('div', 'Custom tags', 'small text-muted'), badges);
            const row = node('div', undefined, 'input-group input-group-sm mt-1');
            const input = node('input', undefined, 'form-control');
            input.type = 'text';
            input.maxLength = MAX_TAG_LENGTH;
            input.setAttribute('maxlength', String(MAX_TAG_LENGTH));
            input.setAttribute('aria-label', 'New tag for ' + voice.name);
            input.setAttribute('placeholder', 'Add tag');
            input.dataset.castingFocus = 'tag:' + voice.id;
            const add = node('button', 'Add tag', 'btn btn-outline-secondary');
            add.type = 'button';
            add.setAttribute('aria-label', 'Add tag to ' + voice.name);
            add.addEventListener('click', () => {
                const value = String(input.value || '').trim();
                if (!value) { notify('info', 'Type a tag first.'); return; }
                if (value.length > MAX_TAG_LENGTH) { notify('info', 'Tags are limited to ' + MAX_TAG_LENGTH + ' characters.'); return; }
                const tags = getMark(voice.id).tags || [];
                if (tags.some(tag => tag.toLowerCase() === value.toLowerCase())) { notify('info', 'That tag is already on this voice.'); return; }
                if (tags.length >= MAX_TAGS) { notify('info', 'A voice can carry up to ' + MAX_TAGS + ' tags.'); return; }
                changeMembership(voice, 'tags', value, true);
            });
            row.append(input, add);
            wrap.append(row);
            return wrap;
        }
        function collectionEditor(voice) {
            const mark = getMark(voice.id), wrap = node('div', undefined, 'mt-1');
            const badges = node('div', undefined, 'd-flex flex-wrap');
            for (const name of mark.collections || []) {
                badges.append(removalBadge(voice, 'collections', name,
                    'Remove ' + voice.name + ' from research collection ' + name));
            }
            wrap.append(node('div', 'Research collections', 'small text-muted'), badges);
            const row = node('div', undefined, 'input-group input-group-sm mt-1');
            const input = node('input', undefined, 'form-control');
            input.type = 'text';
            input.maxLength = MAX_COLLECTION_LENGTH;
            input.setAttribute('maxlength', String(MAX_COLLECTION_LENGTH));
            input.setAttribute('aria-label', 'Research collection for ' + voice.name);
            input.setAttribute('placeholder', 'Add to research collection');
            input.dataset.castingFocus = 'collection:' + voice.id;
            const add = node('button', 'Add', 'btn btn-outline-secondary');
            add.type = 'button';
            add.setAttribute('aria-label', 'Add ' + voice.name + ' to a research collection');
            add.addEventListener('click', () => {
                const value = String(input.value || '').trim();
                if (!value) { notify('info', 'Type a research collection name first.'); return; }
                if (value.length > MAX_COLLECTION_LENGTH) {
                    notify('info', 'Research collection names are limited to ' + MAX_COLLECTION_LENGTH + ' characters.');
                    return;
                }
                const names = getMark(voice.id).collections || [];
                if (names.some(name => name.toLowerCase() === value.toLowerCase())) {
                    notify('info', 'That research collection already holds this voice.');
                    return;
                }
                if (names.length >= MAX_COLLECTIONS) { notify('info', 'A voice can be in up to ' + MAX_COLLECTIONS + ' research collections.'); return; }
                changeMembership(voice, 'collections', value, true);
            });
            row.append(input, add);
            wrap.append(row);
            return wrap;
        }
        function cardMarkControls(voice) {
            const wrap = node('div', undefined, 'mt-2 border-top pt-2');
            const organization = node('details', undefined, 'mt-2');
            organization.append(node('summary', 'Tags & research collections', 'small'), tagEditor(voice), collectionEditor(voice));
            wrap.append(markRow(voice, 'stars', true), markRow(voice, 'circles', true), organization);
            return wrap;
        }
        function tableMarkControls(voice) {
            const wrap = node('div', undefined, 'small');
            wrap.append(markRow(voice, 'stars'), markRow(voice, 'circles'));
            const tags = (getMark(voice.id).tags || []);
            if (tags.length) { wrap.append(node('div', tags.join(', '), 'text-muted')); }
            return wrap;
        }
        function selectionBox(voice) {
            const box = node('input', undefined, 'form-check-input casting-select');
            box.type = 'checkbox';
            box.checked = isSelected(voice.id);
            box.dataset.castingSelect = voice.id;
            box.dataset.castingFocus = 'select:' + voice.id;
            box.setAttribute('aria-label', 'Select ' + voice.name + ' for bulk actions');
            box.style.width = '1.5rem';
            box.style.height = '1.5rem';
            box.addEventListener('change', () => { toggleSelection(voice.id, box.checked); box.checked = isSelected(voice.id); });
            return box;
        }

        function buildPagination(host) {
            const row = node('div', undefined, 'd-flex flex-wrap gap-2 align-items-center mt-3');
            ui.pageStatus = node('span', undefined, 'small text-muted');
            ui.pageStatus.id = 'casting-research-page-status';
            ui.pageStatus.setAttribute('role', 'status');
            ui.pageStatus.setAttribute('aria-live', 'polite');
            ui.pageSize = makeSelect('page-size', PAGE_SIZES.map(size => [String(size), size + ' per page']),
                String(st.pageSize), 'Voices per page', value => {
                    st.pageSize = Number(value);
                    st.loaded = st.pageSize;
                    st.pageIndex = 0;
                    prefs.pageSize = st.pageSize;
                    savePrefs();
                    render();
                });
            ui.mode = makeSelect('mode', [['load', 'Load more'], ['auto', 'Auto-load on scroll'], ['pages', 'Pages'], ['all', 'Show all']],
                st.pageMode, 'Paging mode', value => setPageMode(value));
            ui.pagePrev = makeButton('page-prev', 'Previous page', 'btn btn-sm btn-outline-secondary', () => changePage(-1));
            ui.pageNext = makeButton('page-next', 'Next page', 'btn btn-sm btn-outline-secondary', () => changePage(1));
            ui.loadMore = makeButton('load-more', 'Load more', 'btn btn-sm btn-outline-primary', loadMore);
            ui.showAll = makeButton('show-all', 'Show all', 'btn btn-sm btn-outline-secondary', showAll);
            ui.guard = node('span', undefined, 'small text-warning');
            ui.guard.id = 'casting-research-guard';
            ui.guard.setAttribute('role', 'status');
            ui.sentinel = node('div', undefined, 'casting-research-sentinel');
            ui.sentinel.id = 'casting-research-sentinel';
            row.append(ui.pageStatus, ui.pageSize, ui.mode, ui.pagePrev, ui.pageNext, ui.loadMore, ui.showAll,
                       ui.guard, ui.sentinel);
            host.replaceChildren(row);
            ui.pagination = host;
        }
        function mount() {
            if (st.mounted) { return; }
            st.mounted = true;
            setPlayback(prefs.seconds, prefs.full);
            applyWorkspacePrefs();
            const toolbar = document.getElementById('casting-research-toolbar');
            if (toolbar) { buildToolbar(toolbar); }
            const research = document.getElementById('casting-research');
            if (research) {
                ui.filterHost = node('div', undefined, 'casting-research-filters');
                ui.filterHost.id = 'casting-research-filters';
                research.append(ui.filterHost);
                const refresh = makeButton('refresh-circles', 'Refresh results', 'btn btn-sm btn-outline-primary mt-1',
                    refreshCircles, 'Apply the staged circle exclusions');
                research.append(refresh);
            }
            const activity = document.getElementById('casting-activity');
            if (activity) {
                let list = document.getElementById('casting-research-activity-list');
                if (!list) {
                    list = node('div');
                    list.id = 'casting-research-activity-list';
                    activity.append(list);
                }
            }
            const pagination = document.getElementById('casting-pagination');
            if (pagination) { buildPagination(pagination); }
            syncToolbar(visible());
            syncPagination(visible());
            renderActivity();
        }
        function beforeLoad() {
            if (typeof playback.stop === 'function') { playback.stop(); }
            disconnectObserver();
            st.pageIndex = 0;
            st.loaded = st.pageSize;
            st.saving = false;
            st.needsReload = true;
        }
        function afterLoad(library) {
            st.needsReload = false;
            st.activity = activityList(library && library.activity);
            st.views = readViews();
            pruneSelection();
            syncToolbar(visible());
            syncPagination(visible());
            renderActivity();
        }
        function afterRender(list, page) {
            if (!st.mounted) { mount(); }
            applyCardVisibility();
            syncToolbar(list);
            syncPagination(list);
            renderActivity();
        }

        return {
            mount, beforeLoad, afterLoad, afterRender, matches, renderFilters, pageSlice,
            filtersChanged, clearFilters, saveMark, isSelected, toggleSelection, selectionBox,
            cardMarkControls, tableMarkControls,
            state: () => st, prefs: () => prefs,
            cycleChip, chipKey, pageCount,
        };
    }

    window.CastingResearch = {create, COLORS, SHAPES, FILTER_FIELDS, PAGE_SIZES, DEFAULT_PAGE, DEFAULT_SECONDS,
        MAX_TAGS, MAX_TAG_LENGTH, MAX_COLLECTIONS, MAX_COLLECTION_LENGTH, MAX_SELECTION, MAX_VIEWS, MAX_UNDO,
        DEFAULTS, normalize, chipKey, chipPair, tagColorOf, tagColorClass, TAG_COLOR_CLASS};
})();
