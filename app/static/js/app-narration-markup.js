let narrationMarkupPreview = null;
let narrationMarkupBusy = false;
let narrationMarkupApplying = false;
let narrationMarkupEpoch = 0;

function getNarrationMarkupContext() {
    const target = getEditorReviewTarget();
    if (!target || isEditorEditPending() || getEditorReviewRowState(target).dirty) { return null; }
    return {uid: target.chunk.uid, id: target.chunk.id, book: chunkSnapshotBookToken,
        revision: chunkSnapshotRevision,
        text: JSON.stringify([target.chunk.text, target.chunk.pause_after, target.chunk.speaker, target.chunk.instruct])};
}

function isNarrationMarkupCurrent(context) {
    return context && JSON.stringify(context) === JSON.stringify(getNarrationMarkupContext());
}

function cancelNarrationMarkup() {
    if (narrationMarkupApplying) { return; }
    narrationMarkupEpoch++;
    narrationMarkupPreview = null;
    document.getElementById('editor-line-markup-preview').textContent = '';
    document.getElementById('editor-line-markup-status').textContent = 'Preview discarded. Saved text, pauses and audio are unchanged.';
    applyNarrationMarkupInspector();
}

function applyNarrationMarkupInspector() {
    const context = getNarrationMarkupContext();
    if (narrationMarkupPreview && !isNarrationMarkupCurrent(narrationMarkupPreview.context)) {
        narrationMarkupEpoch++;
        narrationMarkupPreview = null;
        document.getElementById('editor-line-markup-preview').textContent = '';
        document.getElementById('editor-line-markup-status').textContent = 'Line or book changed. Repeat the markup preview.';
    }
    const report = narrationMarkupPreview?.report;
    const decision = document.getElementById('editor-line-markup-choice');
    decision.hidden = !report?.conflict;
    decision.disabled = narrationMarkupBusy;
    document.getElementById('editor-line-markup-check').disabled = !context || narrationMarkupBusy;
    document.getElementById('editor-line-markup-apply').disabled = narrationMarkupBusy || !report?.changed
        || (report.conflict && !['keep', 'replace'].includes(decision.value));
    document.getElementById('editor-line-markup-cancel').disabled = narrationMarkupApplying;
}

async function previewNarrationMarkup() {
    if (narrationMarkupBusy) { return; }
    const context = getNarrationMarkupContext();
    if (!context) { return; }
    const epoch = ++narrationMarkupEpoch;
    narrationMarkupPreview = {context};
    narrationMarkupBusy = true;
    applyNarrationMarkupInspector();
    const status = document.getElementById('editor-line-markup-status');
    status.textContent = 'Reading saved line; no generation or application is started.';
    try {
        const report = await API.post('/api/chunks/' + context.id + '/narration-markup/preview', {
            expected_book_token: context.book, expected_uid: context.uid});
        if (epoch !== narrationMarkupEpoch || !isNarrationMarkupCurrent(context)) { return; }
        if (report.book_token !== context.book || report.uid !== context.uid || !/^[a-f0-9]{64}$/.test(report.binding)
                || typeof report.spoken_text !== 'string' || typeof report.original_text !== 'string'
                || !Array.isArray(report.spans)) { throw new Error('Markup preview is unavailable; refresh Editor.'); }
        narrationMarkupPreview = {context, report};
        const details = {original: report.original_text, spoken: report.spoken_text,
            proposed_pause_ms: report.pause_after, existing_pause_ms: report.existing_pause,
            source_spans: report.spans, previous_application: report.previous_application};
        document.getElementById('editor-line-markup-preview').textContent = JSON.stringify(details, null, 2);
        document.getElementById('editor-line-markup-choice').value = '';
        status.textContent = !report.changed ? 'No markup transformation to apply. Saved text stays literal.'
            : report.conflict ? 'Conflict: choose whether to keep your saved pause or replace it. Nothing has changed.'
                : 'Preview only. Apply requires confirmation; changed text will need a new take.';
    } catch (error) {
        if (epoch === narrationMarkupEpoch && isNarrationMarkupCurrent(context)) { status.textContent = error.message || 'Markup preview failed.'; }
    } finally {
        narrationMarkupBusy = false;
        applyNarrationMarkupInspector();
    }
}

async function applyNarrationMarkup() {
    const saved = narrationMarkupPreview;
    if (narrationMarkupBusy || !saved?.report?.changed || !isNarrationMarkupCurrent(saved.context)) { return; }
    const decision = saved.report.conflict ? document.getElementById('editor-line-markup-choice').value : 'apply';
    if (!['apply', 'keep', 'replace'].includes(decision) || (saved.report.conflict && decision === 'apply')) { return; }
    const epoch = narrationMarkupEpoch;
    narrationMarkupBusy = true;
    applyNarrationMarkupInspector();
    try {
        if (!await showConfirm('Apply this preview to the saved Editor line? ' + (decision === 'keep' ? 'Keep the existing pause. ' : 'Use the proposed pause, if any. ')
            + 'The original text is retained in the application receipt. Existing take bytes stay on disk, but this changed line needs regeneration. Script and source are not rewritten. No audio is generated now.',
            {title: 'Apply opt-in pause markup?', actionLabel: 'Apply preview'})) { return; }
        if (epoch !== narrationMarkupEpoch || !isNarrationMarkupCurrent(saved.context)) { return; }
        narrationMarkupApplying = true;
        applyNarrationMarkupInspector();
        await API.post('/api/chunks/' + saved.context.id + '/narration-markup/apply', {
            expected_book_token: saved.context.book, expected_uid: saved.context.uid,
            binding: saved.report.binding, decision});
        const current = getNarrationMarkupContext();
        if (!current || current.book !== saved.context.book || current.uid !== saved.context.uid) { return; }
        narrationMarkupPreview = null;
        document.getElementById('editor-line-markup-status').textContent = 'Applied once. Original text retained; audio generation is still your explicit choice.';
        await loadChunks(true);
    } catch (error) {
        if (epoch === narrationMarkupEpoch && isNarrationMarkupCurrent(saved.context)) {
            document.getElementById('editor-line-markup-status').textContent = (error.message || 'Application could not be confirmed.') + ' Refresh Editor and inspect the saved receipt before retrying.';
        }
    } finally {
        narrationMarkupApplying = false;
        narrationMarkupBusy = false;
        applyNarrationMarkupInspector();
    }
}

window.applyNarrationMarkupInspector = applyNarrationMarkupInspector;
