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
    return {chapters: chapter === '' ? null : [Number(chapter)],
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
    // Each panel's preview carries the reader of its own controls.
    const getFilters = preview && preview.getFilters ? preview.getFilters : getEditorRepairFilters;
    if (!preview || preview.book_token !== chunkSnapshotBookToken
            || JSON.stringify(preview.filters) !== JSON.stringify(getFilters())) {
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

/* Generate by chapter (#1921): render chosen chapters, skipping lines that
   already have audio unless Regenerate is ticked. Uses the repair preview for
   the exact rows, so admission, confirmation and polling are the shared ones. */
let editorChapterBusy = false;
let editorChapterMapRequest = 0;
const getEditorChapterElement = name => document.getElementById('editor-chapter-' + name);
const EDITOR_CHAPTER_COLORS = {done: 'success', partial: 'warning', pending: 'secondary', failed: 'danger'};

// -> one entry per chapter: lines counted are non-empty ones, as rendering counts them.
function getEditorChapterProgress(chapters, chunks) {
    const byId = new Map(chunks.map(chunk => [chunk.id, chunk]));
    return chapters.map(chapter => {
        let total = 0, done = 0, failed = 0;
        for (let id = chapter.first; id <= chapter.last; id += 1) {
            const chunk = byId.get(id);
            if (!chunk || !(chunk.text || '').trim()) { continue; }
            total += 1;
            if (chunk.status === 'done') { done += 1; }
            if (chunk.status === 'error') { failed += 1; }
        }
        const state = failed ? 'failed' : (total && done === total ? 'done' : (done ? 'partial' : 'pending'));
        return {number: chapter.index + 1, title: chapter.title, total, done, failed, state};
    });
}

function renderEditorChapterMap(progress) {
    getEditorChapterElement('map').replaceChildren(...progress.map(chapter => {
        const square = document.createElement('span');
        square.className = 'bg-' + EDITOR_CHAPTER_COLORS[chapter.state];
        square.setAttribute('title', t('editor.chapters.square', 'Chapter {number}: {title} ({done} of {total} lines generated)', chapter));
        return square;
    }));
    const count = state => progress.filter(chapter => chapter.state === state).length;
    getEditorChapterElement('summary').textContent = t('editor.chapters.summary',
        '{done} of {total} chapters fully generated, {partial} partly generated, {failed} with failed lines.',
        {done: count('done'), total: progress.length, partial: count('partial'), failed: count('failed')});
}

async function getEditorChapterOptions() {
    const book = chunkSnapshotBookToken;
    const options = await API.get(`/api/chunks/repair_options?expected_book_token=${encodeURIComponent(book)}`);
    if (!book || options.book_token !== book || book !== chunkSnapshotBookToken) {
        throw new Error(t('editor.chapters.book-changed', 'The book changed. Try again.'));
    }
    return options;
}

async function refreshEditorChapterMap() {
    const request = ++editorChapterMapRequest;
    try {
        const chunks = await ensureEditorRenderSnapshot();
        const options = await getEditorChapterOptions();
        if (request !== editorChapterMapRequest) { return; }
        renderEditorChapterMap(getEditorChapterProgress(options.chapters, chunks));
    } catch (error) {
        if (request === editorChapterMapRequest) {
            getEditorChapterElement('summary').textContent = t('editor.chapters.map-failed', 'Could not load chapter progress. Refresh Editor and open this panel again.');
        }
    }
}
window.refreshEditorChapterMap = refreshEditorChapterMap;

async function generateEditorChapters() {
    if (editorChapterBusy || isRenderingAll) { return; }
    editorChapterBusy = true;
    const input = getEditorChapterElement('selection');
    const status = getEditorChapterElement('status');
    try {
        const chapters = parseChapterSelection(input.value);
        if (!chapters) { throw new Error(t('editor.chapters.enter', 'Enter chapter numbers, for example 1-3, 5.')); }
        await ensureEditorRenderSnapshot();
        const book = chunkSnapshotBookToken;
        const options = await getEditorChapterOptions();
        const missing = chapters.find(number => number >= options.chapters.length);
        if (missing !== undefined) {
            throw new Error(t('editor.chapters.out-of-range', 'This book has {count} chapters, so chapter {chapter} does not exist.', {count: options.chapters.length, chapter: missing + 1}));
        }
        if (!options.speakers.length) { throw new Error(t('editor.chapters.no-lines', 'This book has no lines to render.')); }
        const getFilters = () => ({chapters: parseChapterSelection(input.value), speakers: options.speakers});
        const filters = getFilters();
        const result = await API.post('/api/chunks/repair_preview', {expected_book_token: book, expected_options_revision: options.options_revision, ...filters});
        if (result.book_token !== book || book !== chunkSnapshotBookToken) {
            throw new Error(t('editor.chapters.book-changed', 'The book changed. Try again.'));
        }
        const regenerate = getEditorChapterElement('regenerate').checked;
        const pending = result.rows.filter(row => row.status !== 'done').length;
        if (!regenerate && !pending) {
            status.textContent = t('editor.chapters.all-done', 'Every line in these chapters already has audio. Tick Regenerate to replace it.');
            return;
        }
        status.textContent = t('editor.chapters.starting', 'Rendering {count} lines from {chapters} chapters.', {count: regenerate ? result.rows.length : pending, chapters: new Set(chapters).size});
        await startRender(regenerate, {...result, filters, getFilters});
    } catch (error) {
        status.textContent = t('editor.chapters.failed', 'Chapters were not started.');
        showActionError(t('editor.chapters.error', 'Could not generate chapters'), error, t('editor.chapters.recovery', 'Check the chapter numbers and refresh Editor, then try again.'), 'error');
    } finally {
        editorChapterBusy = false;
    }
}
