/* Edits the existing shared lexicon. Auditions are disposable and never replace book audio. */
let pronunciationRevision = null;
let pronunciationEpoch = 0;
let pronunciationBusy = false;
let pronunciationDirty = false;
let pronunciationLines = [];
let pronunciationBook = null;
let pronunciationAudioUrl = null;

function getPronunciationBook() {
    return chunkSnapshotBook === currentBookFilename ? chunkSnapshotBookToken : null;
}

function resetPronunciationAudio() {
    const player = document.getElementById('pronunciation-player');
    player.pause(); player.removeAttribute('src'); player.load(); player.hidden = true;
    if (pronunciationAudioUrl) { URL.revokeObjectURL(pronunciationAudioUrl); pronunciationAudioUrl = null; }
}

function onPronunciationAuditionEdit() {
    pronunciationEpoch++;
    resetPronunciationAudio();
}

function resetPronunciationBook() {
    pronunciationEpoch++;
    pronunciationBook = null; pronunciationLines = [];
    for (const node of document.querySelectorAll('#pronunciation-rows .pronunciation-count')) { node.textContent = 'Preview again'; }
    resetPronunciationAudio();
    document.getElementById('pronunciation-status').textContent = 'Book changed. Shared drafts are retained; preview counts and voices again.';
    resetOccurrencePronunciation();
}

function onPronunciationEdit() {
    pronunciationEpoch++;
    pronunciationDirty = true;
    pronunciationLines = []; pronunciationBook = null;
    resetPronunciationAudio();
    for (const node of document.querySelectorAll('#pronunciation-rows .pronunciation-count')) { node.textContent = 'Preview again'; }
    document.getElementById('pronunciation-status').textContent = 'Unsaved changes. Preview counts and audition before saving shared rules.';
    resetOccurrencePronunciation();
}

function addPronunciationRow(original = '', spoken = '', dirty = true) {
    if (dirty && !pronunciationRevision) { showToast('Load shared rules before adding spellings.', 'warning'); return; }
    const row = document.createElement('tr');
    for (const [key, value, max] of [['original', original, 128], ['spoken', spoken, 256]]) {
        const cell = row.insertCell();
        const input = document.createElement('textarea'); input.rows = 2; input.className = `form-control form-control-sm pronunciation-${key}`;
        input.title = key === 'original' ? 'Exact case-sensitive spelling to match in future speech generation.' : 'Spoken replacement for this spelling; leave blank to make this entry inactive.';
        input.value = value; input.pronunciationLoadedValue = input.value; input.pronunciationRawValue = value; input.maxLength = max; input.setAttribute('aria-label', key === 'original' ? 'Original spelling' : 'Spoken replacement');
        input.addEventListener('input', onPronunciationEdit); cell.appendChild(input);
    }
    const count = row.insertCell(); count.className = 'pronunciation-count'; count.textContent = 'Not measured';
    const cell = row.insertCell(); const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline-danger btn-sm'; button.textContent = 'Remove'; button.title = 'Remove this spelling from the unsaved draft; save shared rules to apply the removal.';
    button.addEventListener('click', () => { row.remove(); onPronunciationEdit(); }); cell.appendChild(button);
    document.getElementById('pronunciation-rows').appendChild(row);
    if (dirty) { onPronunciationEdit(); }
}

function getPronunciationFieldValue(input) {
    return input.value === input.pronunciationLoadedValue ? input.pronunciationRawValue : input.value;
}

function getPronunciationNames() {
    const pairs = []; const seen = new Set();
    for (const row of document.getElementById('pronunciation-rows').children) {
        const original = getPronunciationFieldValue(row.querySelector('.pronunciation-original'));
        const spoken = getPronunciationFieldValue(row.querySelector('.pronunciation-spoken'));
        if (!original.trim()) { throw new Error('Each row needs an original spelling. Remove unused rows.'); }
        if (seen.has(original)) { throw new Error('Each original spelling must appear only once.'); }
        seen.add(original); pairs.push([original, spoken]);
    }
    return Object.fromEntries(pairs);
}

async function loadPronunciationPanel() {
    if (pronunciationBusy) { return; }
    pronunciationBusy = true;
    const epoch = ++pronunciationEpoch;
    try {
        if (pronunciationDirty && !await showConfirm('Discard unsaved pronunciation edits and reload shared rules?', {title: 'Reload pronunciation?', actionLabel: 'Reload'})) { return; }
        const result = await API.get('/api/pronunciation');
        if (epoch !== pronunciationEpoch) { return; }
        document.getElementById('pronunciation-rows').replaceChildren();
        for (const [original, spoken] of Object.entries(result.names)) { addPronunciationRow(original, spoken, false); }
        pronunciationRevision = result.revision;
        for (const id of ['pronunciation-add', 'pronunciation-preview', 'pronunciation-save']) { document.getElementById(id).disabled = false; }
        pronunciationDirty = false; pronunciationLines = []; pronunciationBook = null;
        resetOccurrencePronunciation();
        resetPronunciationAudio();
        document.getElementById('pronunciation-status').textContent = 'Shared rules loaded. Matching is case-sensitive; blank replacements are inactive. Preview counts for the current book.';
    } catch (error) { showActionError('Could not load pronunciation', error, 'Keep unsaved edits. Repair malformed files before trying again.', 'error'); }
    finally { pronunciationBusy = false; }
}

async function previewPronunciationPanel() {
    if (pronunciationBusy) { return; }
    pronunciationBusy = true;
    const epoch = ++pronunciationEpoch; const book = getPronunciationBook();
    try {
        if (!pronunciationRevision) { throw new Error('Load shared rules first.'); }
        if (!book) { throw new Error('Refresh Editor first.'); }
        const names = getPronunciationNames();
        await ensureEditorRenderSnapshot();
        const result = await API.post('/api/pronunciation/preview', {names, expected_book_token: book});
        if (epoch !== pronunciationEpoch || book !== getPronunciationBook() || result.book_token !== book) { return; }
        pronunciationLines = result.lines; pronunciationBook = book;
        const select = document.getElementById('pronunciation-line'); select.replaceChildren();
        for (const row of result.lines) { select.add(new Option(`Line ${row.id + 1} · ${row.speaker || 'Unknown speaker'}`, String(row.id))); }
        resetOccurrencePronunciation();
        for (const row of document.getElementById('pronunciation-rows').children) {
            const original = getPronunciationFieldValue(row.querySelector('.pronunciation-original'));
            row.querySelector('.pronunciation-count').textContent = String(Object.hasOwn(result.counts, original) ? result.counts[original] : 0);
        }
        document.getElementById('pronunciation-status').textContent = `${pronunciationDirty ? 'Unsaved draft' : 'Current rules'}: active substitutions counted in saved script text. Choose a line voice and enter a short spelling below to audition. Counts do not certify pronunciation.`;
    } catch (error) { if (epoch === pronunciationEpoch) { showActionError('Could not preview pronunciation', error, 'Refresh Editor and retry the current draft.', 'error'); } }
    finally { pronunciationBusy = false; }
}

async function savePronunciationPanel() {
    if (pronunciationBusy) { return; }
    pronunciationBusy = true;
    const epoch = pronunciationEpoch;
    try {
        if (!pronunciationRevision) { throw new Error('Load shared rules first.'); }
        const names = getPronunciationNames();
        if (!await showConfirm('Save these pronunciation rules for all books? Future renders will use them. Existing audio stays unchanged.', {title: 'Save shared pronunciation?', actionLabel: 'Save shared rules'})) { return; }
        if (epoch !== pronunciationEpoch) { throw new Error('The draft changed during confirmation. Review and save again.'); }
        const result = await API.post('/api/pronunciation/save', {names, revision: pronunciationRevision});
        pronunciationRevision = result.revision;
        pronunciationDirty = epoch !== pronunciationEpoch;
        document.getElementById('pronunciation-status').textContent = pronunciationDirty ? 'The earlier draft was saved. Newer edits remain unsaved.' : 'Shared rules saved. Existing audio is unchanged; regenerate selected lines when ready.';
    } catch (error) { showActionError('Could not save pronunciation', error, 'Keep your draft. If another editor saved first, reconcile it before reloading.', 'error'); }
    finally { pronunciationBusy = false; }
}

async function auditionPronunciation(kind) {
    if (pronunciationBusy) { return; }
    pronunciationBusy = true;
    const epoch = pronunciationEpoch; const book = getPronunciationBook();
    try {
        const row = pronunciationLines.find(row => String(row.id) === document.getElementById('pronunciation-line').value);
        if (!row || !book || book !== pronunciationBook) { throw new Error('Preview counts for the current book and choose a line voice first.'); }
        const text = document.getElementById('pronunciation-audition-original').value;
        const replacement = kind === 'spoken' ? document.getElementById('pronunciation-audition-spoken').value : null;
        if (!text.trim() || (replacement !== null && !replacement.trim())) { throw new Error('Enter a short spelling or replacement to audition.'); }
        resetPronunciationAudio();
        document.getElementById('pronunciation-status').textContent = 'Generating a pronunciation audition. Follow audio progress in Activity; existing book audio is unchanged.';
        const response = await fetch('/api/pronunciation/audition', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({expected_book_token: book, index: row.id, expected_uid: row.uid, text, replacement})});
        if (!response.ok) { const data = await response.json(); throw new Error(typeof data.detail === 'string' ? data.detail : `Audition refused (${response.status}).`); }
        const blob = await response.blob();
        if (epoch !== pronunciationEpoch || book !== getPronunciationBook()) { return; }
        pronunciationAudioUrl = URL.createObjectURL(blob);
        const player = document.getElementById('pronunciation-player'); player.src = pronunciationAudioUrl; player.hidden = false;
        document.getElementById('pronunciation-status').textContent = `${kind === 'original' ? 'Original spelling' : 'Replacement'} audition ready. Press Play to listen; saved respellings are excluded; the proposed rule uses the same speech preparation as rendering.`;
    } catch (error) {
        if (epoch === pronunciationEpoch) {
            document.getElementById('pronunciation-status').textContent = 'Audition failed or was refused. Check audio task status before trying again.';
            showActionError('Could not audition pronunciation', error, 'Check assigned voice, TTS configuration and current audio task.', 'error');
        }
    } finally { pronunciationBusy = false; }
}

// Candidate coordinates come from speech preparation on the server. Never
// reinterpret browser UTF-16 selections or search for a stale rule's new home.
let occurrencePronunciationState = null;
let occurrencePronunciationRules = [];
let occurrencePronunciationEpoch = 0;
let occurrencePronunciationBusy = false;
let occurrencePronunciationDirty = false;

function getOccurrencePronunciationTarget() {
    const book = getPronunciationBook();
    const row = pronunciationLines.find(item => String(item.id) === document.getElementById('pronunciation-line').value);
    if (!pronunciationRevision || pronunciationDirty || !book || book !== pronunciationBook || !row) {
        throw new Error('Load saved shared rules, preview counts for this book, and choose a line first.');
    }
    return {expected_book_token: book, index: row.id, expected_uid: row.uid};
}

function isOccurrencePronunciationTarget(target) {
    try { return JSON.stringify(getOccurrencePronunciationTarget()) === JSON.stringify(target); }
    catch { return false; }
}

function isOccurrencePronunciationBound() {
    return occurrencePronunciationState && isOccurrencePronunciationTarget(occurrencePronunciationState.target);
}

function setOccurrencePronunciationStatus(message) {
    const node = document.getElementById('pronunciation-occurrence-status');
    if (node) { node.textContent = message; }
}

function updateOccurrencePronunciationButtons() {
    const bound = isOccurrencePronunciationBound();
    for (const action of ['load', 'find', 'preview', 'save']) {
        const node = document.getElementById(`pronunciation-occurrence-${action}`);
        if (node) { node.disabled = occurrencePronunciationBusy || (action !== 'load' && (!bound || (action === 'save' && !occurrencePronunciationDirty))); }
    }
}

function clearOccurrencePronunciationPreview() {
    const text = document.getElementById('pronunciation-occurrence-text');
    if (text) { text.hidden = true; text.textContent = ''; }
}

function onOccurrenceQueryEdit() {
    occurrencePronunciationEpoch++;
    document.getElementById('pronunciation-occurrence-candidates')?.replaceChildren();
}

function resetOccurrencePronunciation() {
    onOccurrenceQueryEdit();
    clearOccurrencePronunciationPreview();
    if (!occurrencePronunciationDirty) {
        occurrencePronunciationState = null; occurrencePronunciationRules = [];
        document.getElementById('pronunciation-occurrence-rules')?.replaceChildren();
    }
    setOccurrencePronunciationStatus(occurrencePronunciationDirty ? 'Unsaved line rules retained. Return to their book and line to continue, or load another line to discard them.' : 'Preview shared counts to choose a line, then load its rules.');
    updateOccurrencePronunciationButtons();
}

function onPronunciationLineEdit() {
    onPronunciationAuditionEdit();
    resetOccurrencePronunciation();
}

function onOccurrencePronunciationEdit() {
    occurrencePronunciationEpoch++;
    occurrencePronunciationDirty = true;
    clearOccurrencePronunciationPreview();
    setOccurrencePronunciationStatus('Unsaved line rules. Review and approve the intended occurrences, then preview before saving.');
    updateOccurrencePronunciationButtons();
}

function occurrencePronunciationField(parent, label, value, maxLength, onInput) {
    const wrapper = document.createElement('label'); wrapper.className = 'form-label d-block small'; wrapper.textContent = label;
    const input = document.createElement('textarea'); input.rows = 1; input.className = 'form-control form-control-sm';
    input.value = value; input.maxLength = maxLength; input.title = label;
    input.setAttribute('aria-label', label);
    input.addEventListener('input', () => { onInput(input.value); onOccurrencePronunciationEdit(); });
    wrapper.appendChild(input); parent.appendChild(wrapper); return input;
}

function renderOccurrencePronunciationRules() {
    const container = document.getElementById('pronunciation-occurrence-rules');
    if (!container) { return; }
    container.replaceChildren();
    for (const rule of occurrencePronunciationRules) {
        const card = document.createElement('div'); card.className = 'border rounded p-2 mb-2';
        const heading = document.createElement('p'); heading.className = 'small mb-1'; heading.textContent = `Occurrence: ${rule.original}`; card.appendChild(heading);
        const stateLabel = document.createElement('label'); stateLabel.className = 'form-label small'; stateLabel.textContent = 'Review state ';
        const state = document.createElement('select'); state.className = 'form-select form-select-sm'; state.title = 'Only approved rules change generated speech.';
        state.setAttribute('aria-label', 'Review state');
        for (const value of ['draft', 'approved', 'rejected']) { state.add(new Option(value[0].toUpperCase() + value.slice(1), value)); }
        state.value = rule.state;
        state.addEventListener('change', () => { rule.state = state.value; onOccurrencePronunciationEdit(); });
        stateLabel.appendChild(state); card.appendChild(stateLabel);
        const spoken = occurrencePronunciationField(card, 'Spoken replacement (or fallback for a phonetic hint)', rule.replacement ?? rule.fallback ?? '', 512, value => {
            if (rule.phonetic_hint) { if (value.trim()) { rule.fallback = value; } else { delete rule.fallback; } }
            else { rule.replacement = value; }
        });
        const details = document.createElement('details');
        const summary = document.createElement('summary'); summary.textContent = 'Optional restrictions and review notes'; summary.title = 'Limit this occurrence to a speaker, assigned voice, language or engine mode.'; details.appendChild(summary);
        occurrencePronunciationField(details, 'Phonetic hint (plain-text engines require a spoken fallback)', rule.phonetic_hint ?? '', 512, value => {
            if (value.trim()) { rule.phonetic_hint = value; delete rule.replacement; if (spoken.value.trim()) { rule.fallback = spoken.value; } }
            else { delete rule.phonetic_hint; delete rule.fallback; rule.replacement = spoken.value; }
        });
        occurrencePronunciationField(details, 'Review note (does not change speech)', rule.note ?? '', 512, value => { rule.note = value; });
        for (const [key, label] of [['speaker', 'This source speaker'], ['voice_sha256', 'This exact assigned voice or ensemble'], ['language', 'This language'], ['engine_mode', 'This engine mode']]) {
            const current = occurrencePronunciationState?.context[key];
            const wrapper = document.createElement('label'); wrapper.className = 'd-block small';
            const check = document.createElement('input'); check.type = 'checkbox'; check.className = 'form-check-input me-1'; check.title = label;
            check.checked = Object.hasOwn(rule.restrictions ?? {}, key); check.disabled = !current && !check.checked;
            check.addEventListener('change', () => {
                rule.restrictions ??= {};
                if (check.checked && current) { rule.restrictions[key] = current; } else { delete rule.restrictions[key]; }
                check.checked = Object.hasOwn(rule.restrictions, key);
                check.disabled = !current && !check.checked;
                updateDescription();
                onOccurrencePronunciationEdit();
            });
            wrapper.appendChild(check);
            const description = document.createElement('span');
            const updateDescription = () => { description.textContent = label + (key !== 'voice_sha256' ? `: ${rule.restrictions?.[key] ?? current ?? 'unavailable'}` : ' (including narrator grouping)'); };
            updateDescription(); wrapper.appendChild(description); details.appendChild(wrapper);
        }
        card.appendChild(details);
        const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn btn-outline-danger btn-sm mt-2'; remove.textContent = 'Remove rule'; remove.title = 'Remove this rule from the draft; save line rules to apply the removal.';
        remove.addEventListener('click', () => { occurrencePronunciationRules = occurrencePronunciationRules.filter(item => item !== rule); onOccurrencePronunciationEdit(); renderOccurrencePronunciationRules(); }); card.appendChild(remove);
        container.appendChild(card);
    }
}

function showOccurrencePronunciationPreview(result, prefix) {
    const text = document.getElementById('pronunciation-occurrence-text');
    if (text) { text.textContent = result.preview.text; text.hidden = false; }
    const decisions = result.preview.decisions.map(item => {
        if (item.applied) { return item.reason === 'applied_phonetic_fallback' ? 'Applied spoken fallback' : 'Applied respelling'; }
        if (item.reason?.startsWith('scope_mismatch:')) { return 'Bypassed: restriction does not match'; }
        const labels = {draft: 'Draft: not applied', rejected: 'Rejected: not applied', unsupported_phonetic_hint: 'Bypassed: no spoken fallback', overlapping_approved_spans: 'Bypassed: approved occurrences overlap', duplicate_rule_id: 'Bypassed: duplicate rule', stale_chunk_text: 'Bypassed: line text changed', stale_preparation: 'Bypassed: speech preparation changed', stale_span: 'Bypassed: occurrence changed', wrong_chunk: 'Bypassed: different line'};
        return labels[item.reason] ?? 'Bypassed: rule needs review';
    });
    const source = {active: 'active engine', configured: 'saved settings', unknown: 'unavailable engine settings'}[result.context_source] ?? 'current settings';
    setOccurrencePronunciationStatus(`${prefix} Preview uses ${source}. ${decisions.join('; ')}${decisions.length ? '.' : ''}${result.audio_will_be_detached ? ' Saving will make this line pending; any previous audio file is retained.' : ''}`);
}

async function loadOccurrencePronunciation() {
    if (occurrencePronunciationBusy) { return; }
    occurrencePronunciationBusy = true; updateOccurrencePronunciationButtons();
    const epoch = ++occurrencePronunciationEpoch;
    try {
        const target = getOccurrencePronunciationTarget();
        if (occurrencePronunciationDirty && !await showConfirm('Discard unsaved line rules and load the chosen line?', {title: 'Load line rules?', actionLabel: 'Load'})) { return; }
        if (epoch !== occurrencePronunciationEpoch || !isOccurrencePronunciationTarget(target)) { return; }
        await ensureEditorRenderSnapshot();
        if (epoch !== occurrencePronunciationEpoch || !isOccurrencePronunciationTarget(target)) { return; }
        const result = await API.post('/api/pronunciation/occurrences', target);
        if (epoch !== occurrencePronunciationEpoch || !isOccurrencePronunciationTarget(target)) { return; }
        occurrencePronunciationState = {...result, target}; occurrencePronunciationRules = result.rules; occurrencePronunciationDirty = false;
        document.getElementById('pronunciation-occurrence-candidates')?.replaceChildren();
        renderOccurrencePronunciationRules(); showOccurrencePronunciationPreview(result, 'Line rules loaded.');
    } catch (error) { if (epoch === occurrencePronunciationEpoch) { showActionError('Could not load line pronunciation', error, 'Keep your draft. Refresh Editor if the book or line changed.', 'error'); } }
    finally { occurrencePronunciationBusy = false; updateOccurrencePronunciationButtons(); }
}

async function findPronunciationOccurrences() {
    if (occurrencePronunciationBusy) { return; }
    occurrencePronunciationBusy = true; updateOccurrencePronunciationButtons();
    const epoch = ++occurrencePronunciationEpoch;
    try {
        if (!isOccurrencePronunciationBound()) { throw new Error('Load rules for this line first.'); }
        const target = occurrencePronunciationState.target;
        const query = document.getElementById('pronunciation-audition-original').value;
        if (!query.trim()) { throw new Error('Enter the original spelling above.'); }
        const result = await API.post('/api/pronunciation/occurrences/candidates', {...target, revision: occurrencePronunciationState.revision, query});
        if (epoch !== occurrencePronunciationEpoch || !isOccurrencePronunciationTarget(target)) { return; }
        const container = document.getElementById('pronunciation-occurrence-candidates'); container.replaceChildren();
        for (const candidate of result.candidates) {
            const line = document.createElement('div'); line.className = 'border rounded p-2 mb-1 small';
            line.style.whiteSpace = 'pre-wrap';
            for (const [tag, text] of [['span', candidate.before], ['mark', candidate.anchor.original], ['span', candidate.after]]) {
                const node = document.createElement(tag); node.textContent = text; line.appendChild(node);
            }
            const add = document.createElement('button'); add.type = 'button'; add.className = 'btn btn-outline-secondary btn-sm ms-2'; add.textContent = 'Add draft'; add.title = 'Use the spoken replacement above for this exact occurrence, then review its state.';
            add.addEventListener('click', () => {
                if (!isOccurrencePronunciationTarget(target) || !isOccurrencePronunciationBound()) { return; }
                const replacement = document.getElementById('pronunciation-audition-spoken').value;
                if (!replacement.trim()) { showToast('Enter a spoken replacement above first.', 'warning'); return; }
                if (occurrencePronunciationRules.length >= 32) { showToast('This line already has 32 rules. Remove an unused rule first.', 'warning'); return; }
                occurrencePronunciationRules.push({...candidate.anchor, id: crypto.randomUUID(), state: 'draft', replacement});
                onOccurrencePronunciationEdit(); renderOccurrencePronunciationRules();
            }); line.appendChild(add); container.appendChild(line);
        }
        setOccurrencePronunciationStatus(`${result.candidates.length} exact matches.${result.truncated ? ' More matches exist; use a longer spelling to narrow the search.' : ''} Add the intended occurrence, then review and preview its rule.`);
    } catch (error) { if (epoch === occurrencePronunciationEpoch) { showActionError('Could not find occurrences', error, 'Keep your draft and check the chosen line.', 'error'); } }
    finally { occurrencePronunciationBusy = false; updateOccurrencePronunciationButtons(); }
}

async function previewOccurrencePronunciation() {
    if (occurrencePronunciationBusy) { return; }
    occurrencePronunciationBusy = true; updateOccurrencePronunciationButtons();
    const epoch = ++occurrencePronunciationEpoch;
    try {
        if (!isOccurrencePronunciationBound()) { throw new Error('Load rules for this line first.'); }
        const target = occurrencePronunciationState.target;
        const result = await API.post('/api/pronunciation/occurrences/preview', {...target, revision: occurrencePronunciationState.revision, rules: JSON.parse(JSON.stringify(occurrencePronunciationRules))});
        if (epoch !== occurrencePronunciationEpoch || !isOccurrencePronunciationTarget(target)) { return; }
        showOccurrencePronunciationPreview(result, 'Line preview ready.');
    } catch (error) { if (epoch === occurrencePronunciationEpoch) { showActionError('Could not preview line pronunciation', error, 'Keep your draft. Remove stale or overlapping rules before saving.', 'error'); } }
    finally { occurrencePronunciationBusy = false; updateOccurrencePronunciationButtons(); }
}

async function saveOccurrencePronunciation() {
    if (occurrencePronunciationBusy) { return; }
    occurrencePronunciationBusy = true; updateOccurrencePronunciationButtons();
    const epoch = ++occurrencePronunciationEpoch;
    try {
        if (!isOccurrencePronunciationBound()) { throw new Error('Load rules for this line first.'); }
        const state = occurrencePronunciationState;
        const result = await API.post('/api/pronunciation/occurrences/save', {...state.target, revision: state.revision, rules: JSON.parse(JSON.stringify(occurrencePronunciationRules))});
        // Advance the saved revision even when newer local edits must be kept.
        if (occurrencePronunciationState !== state) { return; }
        occurrencePronunciationState = {...result, target: state.target};
        if (!isOccurrencePronunciationTarget(state.target)) { return; }
        if (epoch === occurrencePronunciationEpoch) {
            occurrencePronunciationRules = result.rules; occurrencePronunciationDirty = false;
            renderOccurrencePronunciationRules(); showOccurrencePronunciationPreview(result, result.audio_detached ? 'Saved. This line is pending; any prior audio file is retained.' : 'Saved. Attached audio is unchanged.');
        } else { setOccurrencePronunciationStatus('The earlier line draft was saved. Newer local edits remain unsaved.'); }
        if (result.audio_detached && typeof loadChunks === 'function') { await loadChunks(); }
    } catch (error) { showActionError('Could not save line pronunciation', error, 'Your draft is retained. Finish active work, or reconcile changes in another editor before reloading.', 'error'); }
    finally { occurrencePronunciationBusy = false; updateOccurrencePronunciationButtons(); }
}

window.addEventListener('pagehide', resetPronunciationAudio);

/* Slash-date order for the active book (#1546). Shown only when the book has a
   date like 1/2/1900 that reads differently in US and UK order, or a saved choice. */
let dateOrderBook = null;
let dateOrderBusy = false;

function renderDateOrder(result) {
    const row = document.getElementById('date-order-row');
    const ambiguous = result.evidence.detail.ambiguous_dates || 0;
    dateOrderBook = result.book_token;
    row.hidden = !ambiguous && result.choice === 'auto';
    const select = document.getElementById('date-order-choice');
    select.disabled = false;
    select.value = result.choice;
    const names = {mdy: 'US order (month/day)', dmy: 'UK order (day/month)'};
    const sources = {book_dates: "the book's own dates", spelling: "the book's spelling"};
    let text;
    if (result.choice !== 'auto') {
        text = `Read in ${names[result.choice]}, as you chose.`;
    } else if (result.order) {
        text = `Auto: read in ${names[result.order]}, judged from ${sources[result.evidence.source]}. Choose US or UK to override.`;
    } else {
        text = `This book has ${ambiguous} date${ambiguous === 1 ? '' : 's'} like 1/2/1900 and doesn't show which order it uses. Choose US or UK; until then they are read as written.`;
    }
    document.getElementById('date-order-status').textContent = text;
}

async function loadDateOrder() {
    if (dateOrderBusy) { return; }
    dateOrderBusy = true;
    try { renderDateOrder(await API.get('/api/pronunciation/date_order')); }
    catch (error) {
        // The row is hidden until a load succeeds, so show it for the message and
        // keep the choice unusable: there is no current book token to save against.
        dateOrderBook = null;
        document.getElementById('date-order-choice').disabled = true;
        document.getElementById('date-order-row').hidden = false;
        const detail = error && error.message ? ` ${error.message}` : '';
        document.getElementById('date-order-status').textContent = `Could not read the date order for this book.${detail}`;
    }
    finally { dateOrderBusy = false; }
}

async function saveDateOrder() {
    if (dateOrderBusy || !dateOrderBook) { return; }
    dateOrderBusy = true;
    const choice = document.getElementById('date-order-choice').value;
    let failed = false;
    try { renderDateOrder(await API.post('/api/pronunciation/date_order', {choice, expected_book_token: dateOrderBook})); }
    catch (error) {
        failed = true;
        showActionError('Could not save the date order', error, 'Reload the book or wait for audio generation to finish, then try again.', 'error');
    }
    finally { dateOrderBusy = false; }
    if (failed) { await loadDateOrder(); }
}
