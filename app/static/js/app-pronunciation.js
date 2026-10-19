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
}

function onPronunciationEdit() {
    pronunciationEpoch++;
    pronunciationDirty = true;
    pronunciationLines = []; pronunciationBook = null;
    resetPronunciationAudio();
    for (const node of document.querySelectorAll('#pronunciation-rows .pronunciation-count')) { node.textContent = 'Preview again'; }
    document.getElementById('pronunciation-status').textContent = 'Unsaved changes. Preview counts and audition before saving shared rules.';
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

window.addEventListener('pagehide', resetPronunciationAudio);
