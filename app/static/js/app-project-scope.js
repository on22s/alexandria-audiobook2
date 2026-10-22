// Explicit source-scope edits reuse existing book/task admission on the server.
let sourceScopeEpoch = 0, sourceScopeBusy = false, sourceScopePreview = null;
let sourceScopeUndo = null, sourceScopeNextOffset = null;
const sourceScopeElement = id => document.getElementById('source-scope-' + id);

function updateSourceScopeControls() {
    for (const id of ['find', 'next', 'entry', 'preview', 'apply', 'undo', 'discard', 'search']) {
        const el = sourceScopeElement(id);
        if (el) { el.disabled = sourceScopeBusy; }
    }
    sourceScopeElement('next').disabled = sourceScopeBusy || sourceScopeNextOffset === null;
    sourceScopeElement('preview').disabled = sourceScopeBusy || !sourceScopeElement('entry').value;
    sourceScopeElement('apply').disabled = sourceScopeBusy || !sourceScopePreview || !!sourceScopeUndo?.available;
    sourceScopeElement('undo').disabled = sourceScopeBusy || !sourceScopeUndo?.can_undo;
    sourceScopeElement('discard').disabled = sourceScopeBusy || !sourceScopeUndo?.available;
}

function invalidateSourceScopePreview() {
    sourceScopeEpoch += 1; sourceScopePreview = null;
    sourceScopeElement('review').replaceChildren(); updateSourceScopeControls();
}

function resetSourceScopeBook() {
    invalidateSourceScopePreview(); sourceScopeUndo = null; sourceScopeNextOffset = null;
    sourceScopeElement('entry').replaceChildren(); sourceScopeElement('entry').value = '';
    sourceScopeElement('undo-status').textContent = '';
    sourceScopeElement('feedback').textContent = 'Book changed. Refresh the saved entries and undo status.';
    updateSourceScopeControls();
    if (sourceScopeElement('panel').open && !sourceScopeBusy) { refreshSourceScope(); }
}

function onSourceScopePanelToggle() {
    invalidateSourceScopePreview();
    if (sourceScopeElement('panel').open) { refreshSourceScope(); }
}

async function refreshSourceScope(offset = 1) {
    if (sourceScopeBusy) { return; }
    invalidateSourceScopePreview(); const epoch = sourceScopeEpoch;
    const selection = window._existingUploadSelectionRequest;
    sourceScopeBusy = true; sourceScopeUndo = null; updateSourceScopeControls();
    sourceScopeElement('feedback').textContent = 'Reading saved entries and undo status…';
    try {
        const search = encodeURIComponent(sourceScopeElement('search').value.trim());
        const [choices, undo] = await Promise.all([API.get(`/api/project/scope/entries?search=${search}&offset=${offset}`), API.get('/api/project/scope/undo')]);
        if (epoch !== sourceScopeEpoch || selection !== window._existingUploadSelectionRequest) { return; }
        const select = sourceScopeElement('entry'); select.replaceChildren();
        const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Choose the first entry to keep'; select.appendChild(placeholder);
        for (const row of choices.entries) {
            const option = document.createElement('option'); option.value = String(row.start_entry); option.textContent = row.label; select.appendChild(option);
        }
        select.value = ''; sourceScopeNextOffset = choices.next_offset; sourceScopeUndo = undo;
        sourceScopeElement('undo-status').textContent = undo.available
            ? (undo.can_undo ? 'An exact undo is available. Undo or discard it before applying another scope.' : `Undo is preserved but cannot apply: ${undo.reason} You can inspect the project or explicitly discard that undo record.`)
            : 'No scope undo is stored.';
        sourceScopeElement('feedback').textContent = `${choices.entries.length} choices shown from ${choices.total_entries} saved entries. Boundaries inside a saved chunk will be refused.`;
    } catch (error) {
        sourceScopeElement('feedback').textContent = error.message;
    } finally {
        sourceScopeBusy = false; updateSourceScopeControls();
    }
}

function loadNextSourceScopeEntries() {
    if (sourceScopeNextOffset !== null) { refreshSourceScope(sourceScopeNextOffset); }
}

function renderSourceScopeRows(label, rows, describe) {
    const details = document.createElement('details'), summary = document.createElement('summary');
    summary.textContent = `${label} (${rows.length})`; details.appendChild(summary);
    const content = document.createElement('div'), navigation = document.createElement('div');
    const previous = document.createElement('button'), next = document.createElement('button'), range = document.createElement('span');
    previous.type = next.type = 'button'; previous.textContent = 'Previous 50'; next.textContent = 'Next 50';
    previous.className = next.className = 'btn btn-sm btn-outline-secondary m-1';
    let offset = 0;
    const paint = () => {
        content.replaceChildren();
        for (const row of rows.slice(offset, offset + 50)) { const p = document.createElement('p'); p.textContent = describe(row); content.appendChild(p); }
        previous.disabled = offset === 0; next.disabled = offset + 50 >= rows.length;
        range.textContent = rows.length ? `${offset + 1}–${Math.min(offset + 50, rows.length)}` : 'No entries';
    };
    previous.onclick = () => { offset = Math.max(0, offset - 50); paint(); };
    next.onclick = () => { offset += 50; paint(); };
    navigation.appendChild(previous); navigation.appendChild(range); navigation.appendChild(next);
    details.appendChild(content); details.appendChild(navigation); paint();
    return details;
}

function renderSourceScopePreview(plan) {
    const area = sourceScopeElement('review'); area.replaceChildren();
    const append = text => { const p = document.createElement('p'); p.textContent = text; area.appendChild(p); };
    append(`${plan.source_name}: remove words ${plan.source_range.removed_start_word + 1}–${plan.source_range.retained_start_word}; keep ${plan.source_range.retained_start_word + 1}–${plan.source_range.end_word}.`);
    append(`${plan.removed_uids.length} chunks leave the active scope; ${plan.retained.length} stay, including ${plan.retained.filter(row => row.audio_verified).length} audio files checked for integrity and ${plan.history_versions} saved take versions. Listening and speech-content checks remain separate.`);
    append(plan.notice);
    for (const [label, rows] of [['Removed text', plan.removed_script], ['Retained text', plan.retained]]) {
        area.appendChild(renderSourceScopeRows(label, rows, row => `${row.speaker || 'Unassigned'}${row.uid ? ' · ' + row.uid : ''}: ${row.text}`));
    }
    area.appendChild(renderSourceScopeRows('Retained voice assignments', plan.voices, row => `${row.uid}: ${Object.entries(row.effective_voices).map(([name, voice]) => `${name}: ${voice.voice || voice.type || 'Unassigned'}${voice.character_style ? ' · ' + voice.character_style : ''}`).join('; ')}`));
    area.appendChild(renderSourceScopeRows('Preserved files outside active scope', plan.preserved_files, row => `${row.path}: ${row.exists ? 'kept on disk' : 'already missing'}`));
}

async function previewSourceScope() {
    if (sourceScopeBusy || !sourceScopeElement('entry').value) { return; }
    invalidateSourceScopePreview(); const epoch = sourceScopeEpoch, selection = window._existingUploadSelectionRequest;
    sourceScopeBusy = true; updateSourceScopeControls(); sourceScopeElement('feedback').textContent = 'Verifying source, voices, audio and history…';
    try {
        await flushVoiceSaves();
        const plan = await API.post('/api/project/scope/preview', {start_entry: Number(sourceScopeElement('entry').value)});
        if (epoch !== sourceScopeEpoch || selection !== window._existingUploadSelectionRequest) { return; }
        sourceScopePreview = plan; renderSourceScopePreview(plan); sourceScopeElement('feedback').textContent = 'Review the removed and retained text before applying.';
    } catch (error) { sourceScopeElement('feedback').textContent = error.message; }
    finally { sourceScopeBusy = false; updateSourceScopeControls(); }
}

async function runSourceScopeMutation(action) {
    if (sourceScopeBusy) { return; }
    const preview = sourceScopePreview, undo = sourceScopeUndo, selection = window._existingUploadSelectionRequest;
    if (action === 'apply' && (!preview || undo?.available)) { return; }
    if (action !== 'apply' && (!undo?.available || action === 'undo' && !undo.can_undo)) { return; }
    sourceScopeBusy = true; updateSourceScopeControls(); let committed = false, outcome = '';
    try {
        await flushVoiceSaves();
        if (action !== 'discard' && !await ensureCastListEditsDiscardable()) { return; }
        if (selection !== window._existingUploadSelectionRequest) { return; }
        const payload = {expected_revision: action === 'apply' ? preview.revision : undo.revision};
        if (action === 'apply') { payload.start_entry = preview.start_entry; }
        await API.post('/api/project/scope/' + (action === 'discard' ? 'discard_undo' : action), payload); committed = true;
        invalidateSourceScopePreview();
        if (selection !== window._existingUploadSelectionRequest) { return; }
        await refreshVoiceMetadata(); await loadChunks(true);
        outcome = action === 'apply' ? 'Scope applied. Rebuild older exports before downloading.' : action === 'undo' ? 'Previous scope restored.' : 'Undo discarded; current scope kept.';
    } catch (error) {
        invalidateSourceScopePreview(); outcome = committed ? `Change saved. Refresh the book views: ${error.message}` : error.message;
    } finally { sourceScopeBusy = false; updateSourceScopeControls(); }
    if (selection === window._existingUploadSelectionRequest) {
        await refreshSourceScope();
        if (outcome) { sourceScopeElement('feedback').textContent = outcome; }
    }
}

function applySourceScope() { return runSourceScopeMutation('apply'); }
function undoSourceScope() { return runSourceScopeMutation('undo'); }
function discardSourceScopeUndo() { return runSourceScopeMutation('discard'); }
