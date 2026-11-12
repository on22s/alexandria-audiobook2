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
    sourceScopeElement('feedback').textContent = t('scope.book-changed', 'Book changed. Refresh the saved entries and undo status.');
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
    sourceScopeElement('feedback').textContent = t('scope.reading', 'Reading saved entries and undo status…');
    try {
        const search = encodeURIComponent(sourceScopeElement('search').value.trim());
        // One after the other: both reads take the book and file locks without
        // waiting, so two at once always refused one of them (#1919).
        const choices = await API.get(`/api/project/scope/entries?search=${search}&offset=${offset}`);
        const undo = await API.get('/api/project/scope/undo');
        if (epoch !== sourceScopeEpoch || selection !== window._existingUploadSelectionRequest) { return; }
        const select = sourceScopeElement('entry'); select.replaceChildren();
        const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = t('scope.choose-first-entry', 'Choose the first entry to keep'); select.appendChild(placeholder);
        for (const row of choices.entries) {
            const option = document.createElement('option'); option.value = String(row.start_entry); option.textContent = row.label; select.appendChild(option);
        }
        select.value = ''; sourceScopeNextOffset = choices.next_offset; sourceScopeUndo = undo;
        if (undo.available) {
            sourceScopeElement('undo-status').textContent = undo.can_undo
                ? formatUiSafetyText('safety.scope.undo.available', 'An exact undo is available. Undo or discard it before applying another scope.')
                : formatUiSafetyText('safety.scope.undo.preserved', 'Undo is preserved but cannot apply: {reason} You can inspect the project or explicitly discard that undo record.', {reason: undo.reason});
        } else {
            sourceScopeElement('undo-status').textContent = t('scope.undo.none', 'No scope undo is stored.');
        }
        sourceScopeElement('feedback').textContent = t('scope.choices', '{shown} choices shown from {total} saved entries. Boundaries inside a saved chunk will be refused.', {shown: choices.entries.length, total: choices.total_entries});
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
    summary.textContent = t('scope.rows-heading', '{label} ({count})', {label, count: rows.length}); details.appendChild(summary);
    const content = document.createElement('div'), navigation = document.createElement('div');
    const previous = document.createElement('button'), next = document.createElement('button'), range = document.createElement('span');
    previous.type = next.type = 'button';
    previous.textContent = t('scope.rows.previous', 'Previous 50');
    next.textContent = t('scope.rows.next', 'Next 50');
    previous.className = next.className = 'btn btn-sm btn-outline-secondary m-1';
    let offset = 0;
    const paint = () => {
        content.replaceChildren();
        for (const row of rows.slice(offset, offset + 50)) { const p = document.createElement('p'); p.textContent = describe(row); content.appendChild(p); }
        previous.disabled = offset === 0; next.disabled = offset + 50 >= rows.length;
        range.textContent = rows.length ? `${offset + 1}–${Math.min(offset + 50, rows.length)}` : t('scope.rows.none', 'No entries');
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
    append(t('scope.preview.remove', '{source}: remove words {from}–{to}; keep {keepFrom}–{keepTo}.', {
        source: plan.source_name,
        from: plan.source_range.removed_start_word + 1,
        to: plan.source_range.retained_start_word,
        keepFrom: plan.source_range.retained_start_word + 1,
        keepTo: plan.source_range.end_word,
    }));
    append(t('scope.preview.counts', '{removed} chunks leave the active scope; {retained} stay, including {audio} audio files checked for integrity and {versions} saved take versions. Listening and speech-content checks remain separate.', {
        removed: plan.removed_uids.length,
        retained: plan.retained.length,
        audio: plan.retained.filter(row => row.audio_verified).length,
        versions: plan.history_versions,
    }));
    append(plan.notice);
    for (const [label, rows] of [[t('scope.rows.removed', 'Removed text'), plan.removed_script], [t('scope.rows.retained', 'Retained text'), plan.retained]]) {
        area.appendChild(renderSourceScopeRows(label, rows, row => t('scope.rows.text', '{speaker}{uid}: {text}', {
            speaker: row.speaker || t('scope.unassigned', 'Unassigned'),
            uid: row.uid ? ' · ' + row.uid : '',
            text: row.text,
        })));
    }
    area.appendChild(renderSourceScopeRows(t('scope.rows.voices', 'Retained voice assignments'), plan.voices, row => t('scope.rows.voice-entry', '{uid}: {assignments}', {
        uid: row.uid,
        assignments: Object.entries(row.effective_voices).map(([name, voice]) => t('scope.rows.voice-assignment', '{name}: {voice}{style}', {
            name,
            voice: voice.voice || voice.type || t('scope.unassigned', 'Unassigned'),
            style: voice.character_style ? ' · ' + voice.character_style : '',
        })).join('; '),
    })));
    area.appendChild(renderSourceScopeRows(t('scope.rows.preserved', 'Preserved files outside active scope'), plan.preserved_files, row => row.exists
        ? t('scope.rows.file-kept', '{path}: kept on disk', {path: row.path})
        : t('scope.rows.file-missing', '{path}: already missing', {path: row.path})));
}

async function previewSourceScope() {
    if (sourceScopeBusy || !sourceScopeElement('entry').value) { return; }
    invalidateSourceScopePreview(); const epoch = sourceScopeEpoch, selection = window._existingUploadSelectionRequest;
    sourceScopeBusy = true; updateSourceScopeControls(); sourceScopeElement('feedback').textContent = t('scope.verifying', 'Verifying source, voices, audio and history…');
    try {
        await flushVoiceSaves();
        const plan = await API.post('/api/project/scope/preview', {start_entry: Number(sourceScopeElement('entry').value)});
        if (epoch !== sourceScopeEpoch || selection !== window._existingUploadSelectionRequest) { return; }
        sourceScopePreview = plan; renderSourceScopePreview(plan); sourceScopeElement('feedback').textContent = t('scope.review-before-apply', 'Review the removed and retained text before applying.');
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
        outcome = action === 'apply'
            ? formatUiSafetyText('safety.scope.applied', 'Scope applied. Rebuild older exports before downloading.')
            : action === 'undo'
                ? t('scope.outcome.undo', 'Previous scope restored.')
                : t('scope.outcome.discard', 'Undo discarded; current scope kept.');
    } catch (error) {
        invalidateSourceScopePreview();
        outcome = committed
            ? formatUiSafetyText('safety.scope.saved-unrefreshed', 'Change saved. Refresh the book views: {detail}', {detail: error.message})
            : error.message;
    } finally { sourceScopeBusy = false; updateSourceScopeControls(); }
    if (selection === window._existingUploadSelectionRequest) {
        await refreshSourceScope();
        if (outcome) { sourceScopeElement('feedback').textContent = outcome; }
    }
}

function applySourceScope() { return runSourceScopeMutation('apply'); }
function undoSourceScope() { return runSourceScopeMutation('undo'); }
function discardSourceScopeUndo() { return runSourceScopeMutation('discard'); }
