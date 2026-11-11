/* Chapter selection feeds the existing batch renderer, including its admission and polling. */
let editorRepairPreview = null;
let editorRepairBook = null;
let editorRepairOptionsRevision = null;
let editorRepairRequest = 0;
let editorRepairBusy = false;

function resetEditorRepairPreview() {
    editorRepairRequest++;
    editorRepairPreview = null;
    document.getElementById('editor-repair-start').disabled = true;
    document.getElementById('editor-repair-lines').replaceChildren();
    document.getElementById('editor-repair-status').textContent = t('editor.repair.selection-changed', 'Selection changed. Preview again before rendering.');
}

function getEditorRepairFilters() {
    const chapter = document.getElementById('editor-repair-chapter').value;
    return {chapter: chapter === '' ? null : Number(chapter),
        speakers: Array.from(document.getElementById('editor-repair-speakers').selectedOptions, option => option.value)};
}

async function loadEditorRepairOptions() {
    resetEditorRepairPreview();
    const request = editorRepairRequest;
    const book = chunkSnapshotBookToken;
    try {
        if (!book || chunkSnapshotBook !== currentBookFilename) { throw new Error(t('editor.repair.refresh-first', 'Refresh Editor first.')); }
        const result = await API.get(`/api/chunks/repair_options?expected_book_token=${encodeURIComponent(book)}`);
        if (request !== editorRepairRequest || book !== chunkSnapshotBookToken || result.book_token !== book) { return; }
        editorRepairBook = book;
        editorRepairOptionsRevision = result.options_revision;
        const chapters = document.getElementById('editor-repair-chapter');
        chapters.replaceChildren(new Option(t('editor.repair.entire-book', 'Entire book'), ''));
        for (const chapter of result.chapters) {
            chapters.add(new Option(`${chapter.index + 1}. ${chapter.title}`, String(chapter.index)));
        }
        const speakers = document.getElementById('editor-repair-speakers');
        speakers.replaceChildren();
        for (const speaker of result.speakers) { speakers.add(new Option(speaker || t('editor.repair.unknown-speaker', '(Unknown speaker)'), speaker)); }
        document.getElementById('editor-repair-note').textContent = result.grouping_note;
        document.getElementById('editor-repair-status').textContent = t('editor.repair.choose', 'Choose speakers and preview. Loading options does not start generation.');
    } catch (error) {
        if (request === editorRepairRequest) { showActionError(t('editor.repair.load-error', 'Could not load repair options'), error, t('editor.repair.load-recovery', 'Refresh Editor and load the current options again.'), 'error'); }
    }
}

async function previewEditorRepair() {
    if (editorRepairBusy) { return; }
    resetEditorRepairPreview();
    const request = editorRepairRequest;
    const book = editorRepairBook;
    const filters = getEditorRepairFilters();
    editorRepairBusy = true;
    try {
        if (!book || book !== chunkSnapshotBookToken) { throw new Error(t('editor.repair.load-first', 'Load current chapters and speakers first.')); }
        if (!filters.speakers.length) { throw new Error(t('editor.repair.select-speaker', 'Select at least one speaker.')); }
        await ensureEditorRenderSnapshot();
        const result = await API.post('/api/chunks/repair_preview', {expected_book_token: book, expected_options_revision: editorRepairOptionsRevision, ...filters});
        if (request !== editorRepairRequest || book !== chunkSnapshotBookToken || result.book_token !== book
                || JSON.stringify(filters) !== JSON.stringify(getEditorRepairFilters())) { return; }
        editorRepairPreview = {...result, filters};
        const list = document.getElementById('editor-repair-lines');
        for (const row of result.rows) {
            const item = document.createElement('li');
            item.textContent = t('editor.repair.line', 'Line {line} · {speaker} · {status}: {text}', {
                line: row.id + 1,
                speaker: row.speaker || row.type || t('editor.repair.unknown', 'Unknown'),
                status: row.status || t('editor.repair.pending', 'pending'),
                text: row.text});
            list.appendChild(item);
        }
        document.getElementById('editor-repair-start').disabled = !result.rows.length;
        document.getElementById('editor-repair-status').textContent = t('editor.repair.preview-summary', '{lines} non-empty lines selected; {rendered} have saved audio paths that may be replaced. Review every selected line before continuing.', {lines: result.rows.length, rendered: result.rendered_count});
    } catch (error) {
        if (request === editorRepairRequest) {
            document.getElementById('editor-repair-status').textContent = t('editor.repair.preview-failed', 'Preview failed. Refresh and preview again.');
            showActionError(t('editor.repair.preview-error', 'Could not preview repair'), error, t('editor.repair.preview-recovery', 'Check your selection and refresh Editor.'), 'error');
        }
    } finally { editorRepairBusy = false; }
}

function getEditorRepairSelection(chunks, preview) {
    if (!preview || preview.book_token !== chunkSnapshotBookToken
            || JSON.stringify(preview.filters) !== JSON.stringify(getEditorRepairFilters())) {
        throw new Error(t('editor.repair.selection-changed-again', 'Repair selection changed. Preview again.'));
    }
    const rowsById = new Map(chunks.map(chunk => [chunk.id, chunk]));
    return preview.rows.map(selected => {
        const row = rowsById.get(selected.id);
        if (!row || row.uid !== selected.uid || ['text', 'speaker', 'instruct', 'pause_after'].some(key => row[key] !== selected[key])) {
            throw new Error(t('editor.repair.line-changed', 'A selected line changed. Preview again.'));
        }
        return row;
    });
}

async function generateEditorRepair() {
    if (editorRepairBusy || !editorRepairPreview || isRenderingAll) { return; }
    editorRepairBusy = true;
    document.getElementById('editor-repair-start').disabled = true;
    try { await startRender(true, editorRepairPreview); }
    finally { editorRepairBusy = false; resetEditorRepairPreview(); }
}
