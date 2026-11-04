// Review saved facts; reuse the Editor's original controls, drafts and audio.
const EDITOR_ISSUE_LABELS = {unknown: 'Unknown speaker', failed: 'Failed render', missing: 'Missing audio file', drift: 'Voice drift', outliers: 'Acoustic outliers', speech_rejected: 'Rejected speech attempt', speech_review: 'Speech review needed', speech_unavailable: 'Speech check unavailable', speech_difference: 'Speech content difference', speech_historical: 'Historical speech evidence needs recheck', speech_not_checked: 'No current speech evidence'};
const EDITOR_DRIFT_LABELS = {not_rendered: 'Voice check not applicable before rendering', unmeasured: 'Voice check not measured', unavailable: 'Voice check unavailable or failed', flagged: 'Voice drift flagged', measured: 'Voice check measured; no drift flag'};
const EDITOR_OUTLIER_LABELS = {not_rendered: 'Not rendered', unmeasured: 'Not checked', unavailable: 'Incomplete, stale or unavailable; not clean', flagged: 'Acoustic outlier: listen before deciding', measured: 'Acoustic proxies measured; no outlier flag, not quality certification'};
let editorOutlierBook = null;

function getEditorOutlierText(fact) {
    const outlier = fact?.outlier;
    if (!outlier) { return 'Outlier evidence is unavailable. Refresh review.'; }
    const lines = [EDITOR_OUTLIER_LABELS[outlier.status] || EDITOR_OUTLIER_LABELS.unavailable];
    for (const [metric, record] of Object.entries(outlier.metrics || {})) {
        const value = Number.isFinite(record?.value) ? record?.value.toFixed(2) : 'unavailable';
        const z = Number.isFinite(record?.z) ? record?.z.toFixed(2) : 'unavailable';
        const median = Number.isFinite(record?.median) ? record.median.toFixed(2) : 'unavailable';
        const spread = Number.isFinite(record?.spread) ? record.spread.toFixed(2) : 'unavailable';
        lines.push(`${editorReviewData?.outlier_labels?.[metric] || metric}: ${value}; median ${median}; spread ${spread}; robust z ${z}${record?.flagged === true ? ' (flagged)' : ''}`);
    }
    if (outlier.error) { lines.push(outlier.error); }
    if (editorReviewData?.outlier_error) { lines.push(editorReviewData.outlier_error); }
    return lines.join('\n');
}

function attachEditorOutlierCheck(state) {
    const book = state.book_token;
    const button = document.getElementById('btn-outlier-check');
    const cancel = document.getElementById('btn-outlier-cancel');
    const status = document.getElementById('editor-outlier-status');
    editorOutlierBook = /^[0-9a-f]{64}$/.test(book || '') ? book : null;
    button.disabled = true;
    cancel.disabled = !editorOutlierBook;
    status.textContent = 'Measuring CPU acoustic proxies; no audio is changed. Partial results are not published.';
    _startPolling('logs:outlier_check', () => API.get('/api/status/outlier_check'), {
        doneCheck: current => !current.running,
        onTick: current => {
            if (current.book_token !== book) { return; }
            if (current.progress) {
                status.textContent = `${current.progress.done} / ${current.progress.total} takes scanned (experimental proxies).`;
            }
        },
        onDone: async current => {
            button.disabled = false;
            cancel.disabled = true;
            editorOutlierBook = null;
            if (book !== chunkSnapshotBookToken || current.book_token !== book) { return; }
            status.textContent = (current.logs || []).slice(-1)[0] || 'Scan ended without a report; not measured.';
            await loadChunks(true);
        }
    });
}

async function runOutlierCheck() {
    const button = document.getElementById('btn-outlier-check');
    const cancel = document.getElementById('btn-outlier-cancel');
    const status = document.getElementById('editor-outlier-status');
    if (button.disabled) { return; }
    if (isEditorEditPending()) {
        status.textContent = 'Save or discard pending edits before checking acoustic outliers.';
        return;
    }
    if (!/^[0-9a-f]{64}$/.test(chunkSnapshotBookToken || '')) {
        status.textContent = 'Load the selected book in Editor before checking acoustic outliers.';
        return;
    }
    const book = chunkSnapshotBookToken;
    button.disabled = true;
    try {
        await API.post('/api/chunks/outlier_check', {expected_book_token: book});
        attachEditorOutlierCheck({book_token: book});
    } catch (error) {
        button.disabled = false;
        cancel.disabled = true;
        status.textContent = 'Outlier start is unconfirmed. Check task status before retrying; no quality conclusion.';
        showActionError('Outlier check failed', error, 'Refresh the selected book and inspect outlier task status before retrying.', 'warning');
    }
}

async function cancelOutlierCheck() {
    if (!editorOutlierBook) { return; }
    try {
        await API.post('/api/chunks/outlier_check/cancel', {expected_book_token: editorOutlierBook});
        document.getElementById('editor-outlier-status').textContent = 'Cancellation requested; waiting for the scan to stop.';
    } catch (error) {
        showActionError('Outlier cancellation unconfirmed', error, 'Check outlier task status before retrying.', 'warning');
    }
}
const EDITOR_SPEECH_LABELS = {not_checked: 'No current speech check', unavailable: 'Speech check unavailable', review: 'Transcript or acoustic differences need listening review', no_content_candidate: 'No material content candidate found; listening review still needed', corroborated_content_difference: 'Two ASR passes confirmed material missing or extra speech', review_confirmation_unavailable: 'Suspicious speech; stronger confirmation unavailable', review_short_acoustic_unavailable: 'Short-utterance acoustic check incomplete'};
let editorReviewData = null;
let editorReviewFresh = false;
let editorReviewRequest = 0;
let editorReviewPending = null;
let editorReviewSelection = null;
let editorReviewLimit = 50;
let editorReviewListSignature = null;
let editorSpeechPreview = null;
let editorSpeechPreviewRequest = 0;
let editorSpeechPreviewBusy = false;

function resetEditorSpeechPreview() {
    editorSpeechPreviewRequest++;
    editorSpeechPreview = null;
    const status = document.getElementById('editor-line-input-status');
    const message = 'Read-only preview of the saved line. No model is loaded. Refresh after line, rule, voice or settings changes.';
    if (status.textContent !== message) { status.textContent = message; }
    const details = document.getElementById('editor-line-input-details');
    if (details.textContent !== '') { details.textContent = ''; }
}

async function previewEditorSpeechInput() {
    if (editorSpeechPreviewBusy) { return; }
    resetEditorSpeechPreview();
    const target = getEditorReviewTarget();
    if (!target) { return; }
    if (getEditorReviewRowState(target).dirty || isEditorEditPending()) {
        document.getElementById('editor-line-input-status').textContent = 'Save or discard pending line edits before previewing saved input.';
        return;
    }
    const selection = {...editorReviewSelection}, revision = chunkSnapshotRevision;
    const request = editorSpeechPreviewRequest;
    editorSpeechPreview = {uid: selection.uid, book: selection.book, revision};
    const status = document.getElementById('editor-line-input-status');
    editorSpeechPreviewBusy = true;
    document.getElementById('editor-line-input-preview').disabled = true;
    status.textContent = 'Reading saved speech preparation; no generation starts.';
    const isCurrent = () => request === editorSpeechPreviewRequest && revision === chunkSnapshotRevision
        && selection.uid === editorReviewSelection?.uid && selection.book === editorReviewSelection?.book
        && !!getEditorReviewTarget(selection) && !isEditorEditPending();
    try {
        const result = await API.post('/api/pronunciation/occurrences', {
            expected_book_token: selection.book, expected_uid: selection.uid, index: target.chunk.id
        });
        if (!isCurrent()) { return; }
        if (result.preview?.kind !== 'current_settings_preview') {
            throw new Error('Preview protocol unavailable. Reload the page to match the server version.');
        }
        if (result.book_token !== selection.book || result.uid !== selection.uid
                || result.preview.authored_text !== (target.chunk.text ?? '')) {
            throw new Error('Saved line changed. Refresh Editor before inspecting its input.');
        }
        status.textContent = 'Snapshot at last preview, not the input recorded for a take. May be outdated: refresh after line, rule, voice or settings changes. Historical transformation traces are not reconstructed.';
        if (result.preview.warnings?.length) { status.textContent += ' Preparation warnings: ' + result.preview.warnings.join('; '); }
        document.getElementById('editor-line-input-details').textContent = JSON.stringify({
            authored_text: result.preview.authored_text, prepared_text: result.preview.text,
            transformations: result.preview.transformations, pronunciation_decisions: result.preview.decisions,
            warnings: result.preview.warnings,
            context: result.context, settings_source: result.context_source, rules_revision: result.revision
        }, null, 2);
    } catch (error) {
        if (isCurrent()) { status.textContent = 'Speech preparation unavailable: ' + error.message; }
    } finally {
        editorSpeechPreviewBusy = false;
        if (request === editorSpeechPreviewRequest && !isCurrent()) { resetEditorSpeechPreview(); }
        const current = getEditorReviewTarget();
        document.getElementById('editor-line-input-preview').disabled = !current
            || getEditorReviewRowState(current).dirty || isEditorEditPending();
    }
}

function getEditorReviewTarget(selection = editorReviewSelection) {
    if (!selection || selection.book !== chunkSnapshotBookToken || chunkSnapshotBook !== currentBookFilename) { return null; }
    const matches = cachedChunks.filter(chunk => chunk.uid === selection.uid);
    if (matches.length !== 1) { return null; }
    const chunk = matches[0];
    const row = document.querySelector(`#chunks-table-body tr[data-id="${chunk.id}"]`);
    if (!row || row.dataset.uid !== chunk.uid || row.dataset.bookToken !== selection.book) { return null; }
    return {chunk, row};
}

function selectEditorReviewLine(uid, book, reveal = true) {
    const selection = {uid, book};
    const target = getEditorReviewTarget(selection);
    if (!target) { showToast('This line changed. Refresh Editor before opening the finding.', 'warning'); return false; }
    editorReviewSelection = selection;
    if (reveal) {
        scrollToChunkRow(target.chunk.id);
        document.getElementById('editor-line-details').open = true;
    }
    renderEditorLineInspector();
    if (reveal) {
        const inspector = document.querySelector('.editor-line-inspector');
        document.getElementById('editor-line-title').focus({preventScroll: true});
        if (getComputedStyle(inspector).position === 'static') { inspector.scrollIntoView({block: 'start', behavior: 'instant'}); }
    }
    return true;
}

function selectEditorReviewLineById(id, reveal = true) {
    const chunk = cachedChunks.find(chunk => chunk.id === id);
    return !!chunk && selectEditorReviewLine(chunk.uid, chunkSnapshotBookToken, reveal);
}

function getEditorReviewRowState({chunk, row}) {
    const selectors = {speaker: 'select', text: '.chunk-text', instruct: '.chunk-instruct', pause_after: '.chunk-pause-after'};
    const values = Object.fromEntries(Object.entries(selectors).map(([key, selector]) => [key, row.querySelector(selector)?.value ?? String(chunk[key] ?? '')]));
    const dirty = Object.keys(selectors).some(key => String(getEditorFieldValue(key, values[key], chunk[key])) !== String(chunk[key] ?? ''));
    return {values, dirty};
}

function renderEditorLineInspector() {
    const target = getEditorReviewTarget();
    document.getElementById('editor-line-empty').hidden = !!target;
    document.getElementById('editor-line-content').hidden = !target;
    document.querySelectorAll('#chunks-table-body .editor-selected-line').forEach(row => row.classList.remove('editor-selected-line'));
    if (!target) { editorReviewSelection = null; resetEditorSpeechPreview(); window.applyWordTimingInspector?.(); window.applySpeechVerificationInspector?.(); window.applyTakeHistoryInspector?.(); window.applyNarrationMarkupInspector?.(); return; }
    const {chunk, row} = target;
    row.classList.add('editor-selected-line');
    const {values, dirty} = getEditorReviewRowState(target);
    const pending = isEditorEditPending();
    if (dirty || pending || !editorSpeechPreview || editorSpeechPreview.uid !== chunk.uid
            || editorSpeechPreview.book !== chunkSnapshotBookToken || editorSpeechPreview.revision !== chunkSnapshotRevision) {
        resetEditorSpeechPreview();
    }
    document.getElementById('editor-line-input-preview').disabled = dirty || pending || editorSpeechPreviewBusy;
    const set = (id, text) => { const node = document.getElementById(id); if (node.textContent !== text) { node.textContent = text; } };
    set('editor-line-title', `Line ${chunk.id + 1}`);
    set('editor-line-state', `${chunk.status || 'Status unavailable'} · ${dirty ? 'Unsaved row edits shown; review facts describe the saved line.' : 'Current row values shown.'}`);
    set('editor-line-speaker', values.speaker || 'Unknown speaker');
    set('editor-line-text', values.text);
    set('editor-line-instruct', values.instruct || 'No delivery instruction');
    set('editor-line-pause', values.pause_after === '' ? 'Use the configured default' : `${values.pause_after} ms`);
    set('editor-line-error', typeof chunk.error === 'string' && chunk.error ? chunk.error : 'No render error recorded.');
    const fact = isEditorReviewSnapshotCurrent()
        && editorReviewData.rows.find(item => item.uid === chunk.uid);
    renderEditorSpeechEvidence(chunk,fact);
    renderEditorRecordedInput(chunk, fact);
    const outlierNode = document.getElementById('editor-line-outlier');
    if (outlierNode) { outlierNode.textContent = getEditorOutlierText(fact); }
    set('editor-line-quality', fact ? `${fact.audio === 'present' ? 'Nonempty audio file present (quality not certified)' : fact.audio === 'missing' ? 'Saved audio file is missing, empty or outside the project' : 'Audio not rendered'} · ${EDITOR_DRIFT_LABELS[fact.drift]}` : 'Review facts are unavailable or stale. Refresh review.');
    const drift = chunk.drift;
    set('editor-line-drift-detail', drift && typeof drift === 'object' ? [Number.isFinite(drift.score) ? `Recorded similarity ${drift.score}` : '', Number.isFinite(drift.threshold) ? `threshold ${drift.threshold}` : '', typeof drift.reference === 'string' ? `reference: ${drift.reference}` : '', typeof drift.error === 'string' ? `check error: ${drift.error}` : ''].filter(Boolean).join(' · ') : 'No recorded voice-check details.');
    const sources = chunk.source_entry_indices;
    set('editor-line-source', Array.isArray(sources) && sources.length && sources.every(value => Number.isSafeInteger(value) && value >= 0) ? `${editorReviewFresh ? 'Saved mapping' : 'Last loaded mapping; refresh review'}: ${sources.map(value => value + 1).join(', ')}` : 'Source-entry mapping not recorded in the loaded chunk.');
    const audio = row.querySelector('.chunk-audio');
    document.getElementById('editor-line-play').disabled = !audio?.getAttribute('src') || (fact && fact.audio === 'missing');
    document.getElementById('editor-line-generate').disabled = chunk.status === 'generating';
    window.applyWordTimingInspector?.();
    window.applySpeechVerificationInspector?.(); window.applyTakeHistoryInspector?.(); window.applyNarrationMarkupInspector?.();
}

function renderEditorSpeechEvidence(chunk, fact) {
    const summary=document.getElementById('editor-line-speech');
    const attempt=document.getElementById('editor-line-speech-attempt');
    const expected=document.getElementById('editor-line-speech-expected');
    const details=document.getElementById('editor-line-speech-evidence');
    if (!fact) {
        summary.textContent='Speech evidence is unavailable or stale. Refresh review.';
        attempt.textContent='';expected.textContent='';details.textContent='';return;
    }
    summary.textContent=EDITOR_SPEECH_LABELS[fact.speech] || EDITOR_SPEECH_LABELS.not_checked;
    const receipt=chunk.speech_verification;
    const historical=chunk.speech_verification_historical;
    const historicalReason=chunk.speech_verification_historical_reason;
    const current=receipt && receipt.book_token === chunkSnapshotBookToken && receipt.uid === chunk.uid
        && receipt.audio_revision === chunk.audio_revision && receipt.status === fact.speech;
    if (!current && historical) {
        summary.textContent = historicalReason === 'legacy'
            ? 'Historical speech evidence saved before audio ctime was recorded. Recheck to renew it; no byte change has been detected.'
            : historicalReason === 'platform'
                ? 'Historical speech evidence; this host cannot verify audio byte freshness, so recheck is unavailable here.'
                : historicalReason === 'unavailable'
                    ? 'Historical speech evidence; the audio file is currently unavailable or unreadable.'
                    : historicalReason === 'changed'
                        ? 'Historical speech evidence; the current audio file no longer matches its recorded filesystem identity. Recheck verifies the bytes, or regenerate before relying on this result.'
                        : 'Historical speech evidence; its freshness could not be classified. Recheck is unavailable until the reason is known.';
    }
    const rejected=chunk.speech_verification_failure;
    const recheck=chunk.speech_verification_recheck;
    const incomplete=recheck && fact.speech_recheck === recheck.status && recheck.book_token === chunkSnapshotBookToken
        && recheck.uid === chunk.uid && recheck.audio_revision === chunk.audio_revision;
    const failure=fact.speech_attempt === 'rejected' && rejected?.book_token === chunkSnapshotBookToken
        && rejected.uid === chunk.uid && rejected.status === 'corroborated_content_difference';
    attempt.textContent=failure ? 'A later attempt was rejected for confirmed missing or extra speech. The prior take was kept. Listen, review the line, and generate it again.' : '';
    if (incomplete) { attempt.textContent+=' Latest recheck incomplete or unavailable; the existing selected-take evidence is retained.'; }
    expected.textContent=current && receipt.expected_text ? `Exact input sent for this take: ${receipt.expected_text}`
        : historical?.expected_text ? `Historical exact input sent for this take: ${historical.expected_text}`
        : 'Exact synthesis input is not available for a current selected-take check.';
    const format=(value,label)=>{
        const notes=[label,EDITOR_SPEECH_LABELS[value.status],value.reason || '',
            Number.isFinite(value.seconds) ? `Added check time: ${value.seconds.toFixed(2)} seconds` : ''];
        for (const name of ['primary','confirmation']) {
            const pass=value[name];if (!pass) { continue; }
            notes.push(`${name}: ${pass.expected_words} expected tokens, ${pass.observed_words} heard, ${pass.unmatched_expected} unmatched expected, ${pass.unmatched_observed} unmatched heard.`);
            for (const difference of pass.differences || []) {
                notes.push(`${difference.kind}: expected “${difference.expected}”; heard “${difference.observed}”${difference.ambiguity?.length ? ` (${difference.ambiguity.join(', ')})` : ''}`);
            }
            if (pass.diagnostics_abbreviated) { notes.push(`Showing ${pass.differences.length} of ${pass.difference_count} differences; counts cover the full bounded comparison.`); }
        }
        notes.push('Saved identities and bounds:',JSON.stringify(value,null,2));
        return notes.filter(Boolean).join('\n');
    };
    details.textContent=[current ? format(receipt,'Selected take') : '',
        !current && historical ? format(historical,`Historical saved take (${historicalReason || 'unverified'})`) : '',
        failure ? format(rejected,'Rejected attempt; this is not the selected take') : '',
        incomplete ? format(recheck,'Latest incomplete recheck; earlier selected-take evidence retained') : ''].filter(Boolean).join('\n\n');
}

function renderEditorRecordedInput(chunk, fact) {
    const status = document.getElementById('editor-line-recorded-status');
    const binding = document.getElementById('editor-line-recorded-binding');
    const decisions = document.getElementById('editor-line-recorded-pronunciation');
    if (!status || !binding || !decisions) { return; }
    binding.textContent = ''; decisions.textContent = '';
    if (!fact) { status.textContent = 'Saved review facts are unavailable or stale. Refresh review before inspecting recorded input.'; return; }
    const states = {
        current: 'Exact input recorded for the selected take, not reconstructed from current settings.',
        historical: 'Recorded input is historical; the selected audio bytes are not currently attested. See the speech-evidence freshness reason.',
        stale: 'Saved input evidence is stale and is not shown as proof for this take.',
        invalid: 'Saved input evidence is malformed or incomplete; unavailable, not proof.',
        not_recorded: 'No exact input was recorded for this take. Speech checks may have been off. Current settings cannot reconstruct historical proof.'
    };
    const reasons = {
        other_line: 'The record belongs to another book or line.', other_take: 'The record belongs to another take revision.',
        inputs_changed: 'Saved text, pronunciation rules, delivery or voice revision changed.',
        unverifiable: 'The record cannot be tied to the selected audio.'
    };
    status.textContent = Object.hasOwn(states, chunk.recorded_input_state) ? states[chunk.recorded_input_state]
        : 'Recorded-input state unavailable. Reload to match the server version.';
    if (Object.hasOwn(reasons, chunk.recorded_input_reason)) { status.textContent += ' ' + reasons[chunk.recorded_input_reason]; }
    if (!chunk.audio_path) { status.textContent = 'No selected audio file. Retained metadata follows, not current audio proof. ' + status.textContent; }
    if (chunk.speech_verification_failure) { status.textContent += ' Rejected-attempt evidence is separate from the selected take.'; }
    if (['current', 'historical'].includes(chunk.recorded_input_state)) {
        const receipt = chunk.recorded_input_state === 'current' ? chunk.speech_verification : chunk.speech_verification_historical;
        if (!receipt || receipt.book_token !== chunkSnapshotBookToken || receipt.uid !== chunk.uid
                || receipt.audio_revision !== chunk.audio_revision) {
            status.textContent = 'Recorded input identity does not match this selection. Refresh review.'; return;
        }
        binding.textContent = `Book ${receipt.book_token} · line ${receipt.uid} · selected take ${chunk.take ?? 'unknown'} · audio revision ${receipt.audio_revision}\n`
            + `Current review snapshot ${chunkSnapshotRevision} (not a recorded capture revision; capture revision unavailable)\n`
            + `Saved generation-inputs digest ${receipt.input_digest} · exact input SHA-256 ${receipt.expected_sha256}\n`
            + `Audio SHA-256 ${receipt.audio?.sha256 || 'unavailable'} · language ${receipt.language} · capture ${receipt.input_provenance}\n`
            + 'The input digest binds saved line fields and voice revision, not every engine setting. Historical engine settings are unavailable in this receipt.';
    }
    const pronunciation = chunk.pronunciation_receipt;
    if (chunk.recorded_pronunciation_state !== 'recorded' || !pronunciation
            || pronunciation.audio_revision !== chunk.audio_revision) {
        decisions.textContent = chunk.recorded_pronunciation_state === 'not_recorded'
            ? 'No occurrence decisions recorded for this take; this is not proof that no transformations occurred.'
            : `Recorded occurrence decisions unavailable (${chunk.recorded_pronunciation_state || 'unknown'}). Refresh after changing the saved line.`;
        return;
    }
    decisions.textContent = 'Recorded occurrence decisions (not recomputed):\n'
        + pronunciation.decisions.map(item => `${item.id || 'Unidentified rule'}: ${item.applied ? 'applied once' : 'not applied'} · ${item.reason}`).join('\n')
        + `\nRecorded context: ${JSON.stringify(pronunciation.context)}\nAuthored text SHA-256 ${pronunciation.text_sha256}`
        + `\nPrepared-text SHA-256 ${pronunciation.prepared_sha256}\nSpeech-text SHA-256 ${pronunciation.speech_sha256}`;
}

function focusEditorReviewField(field) {
    const selectors = {speaker: 'select', text: '.chunk-text', instruct: '.chunk-instruct', pause_after: '.chunk-pause-after'};
    if (!Object.hasOwn(selectors, field)) { return; }
    const target = getEditorReviewTarget();
    if (!target) { resetEditorReview(); return; }
    scrollToChunkRow(target.chunk.id);
    if (!target.row.classList.contains('expanded')) { toggleChunkExpand(target.row.querySelector('.chunk-toggle-btn')); }
    const control = target.row.querySelector(selectors[field]);
    control?.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    control?.focus({preventScroll: true});
}

async function playEditorReviewLine() {
    const target = getEditorReviewTarget();
    if (!target || document.getElementById('editor-line-play').disabled) { return; }
    const audio = target.row.querySelector('.chunk-audio');
    if (!audio) { return; }
    const selection = {...editorReviewSelection};
    const source = audio.getAttribute('src');
    stopSequence();
    try {
        await audio.play();
        const current = getEditorReviewTarget(selection);
        if (!current || current.row.querySelector('.chunk-audio') !== audio || audio.getAttribute('src') !== source) { audio.pause(); }
    }
    catch (error) { showActionError('Could not play this line', error, 'Refresh review and check the saved audio file.'); }
}

function generateEditorReviewLine() {
    const target = getEditorReviewTarget();
    if (!target || target.chunk.status === 'generating') { return; }
    return generateChunk(target.chunk.id);
}

function isEditorReviewDataValid(data) {
    if (!data || !/^[a-f0-9]{64}$/.test(data.revision) || data.book_token !== chunkSnapshotBookToken || data.revision !== chunkSnapshotRevision || !Array.isArray(data.rows) || data.rows.length !== cachedChunks.length) { return false; }
    const expected = new Map(cachedChunks.map(chunk => [chunk.uid, chunk.id]));
    if (expected.size !== cachedChunks.length || new Set(cachedChunks.map(chunk => chunk.id)).size !== cachedChunks.length) { return false; }
    const seen = new Set();
    return data.rows.every(row => {
        if (!row || typeof row.uid !== 'string' || !row.uid || seen.has(row.uid) || !Number.isSafeInteger(row.id) || row.id < 0) { return false; }
        seen.add(row.uid);
        return expected.get(row.uid) === row.id
            && Array.isArray(row.issues) && new Set(row.issues).size === row.issues.length
            && row.issues.every(issue => Object.hasOwn(EDITOR_ISSUE_LABELS, issue))
            && ['present', 'missing', 'not_rendered'].includes(row.audio) && Object.hasOwn(EDITOR_DRIFT_LABELS, row.drift)
            && (row.outlier === undefined || Object.hasOwn(EDITOR_OUTLIER_LABELS,row.outlier?.status))
            && (row.speech === undefined || Object.hasOwn(EDITOR_SPEECH_LABELS,row.speech))
            && (row.speech_attempt == null || row.speech_attempt === 'rejected')
            && (row.speech_recheck == null || Object.hasOwn(EDITOR_SPEECH_LABELS,row.speech_recheck));
    });
}

function isEditorReviewSnapshotCurrent() {
    return editorReviewFresh && editorReviewData?.book_token === chunkSnapshotBookToken
        && editorReviewData?.revision === chunkSnapshotRevision && chunkSnapshotBook === currentBookFilename
        && !isEditorEditPending();
}

function renderEditorQualityInbox(resetLimit = false) {
    if (resetLimit) { editorReviewLimit = 50; }
    const status = document.getElementById('editor-quality-status');
    const list = document.getElementById('editor-quality-list');
    const more = document.getElementById('editor-quality-more');
    const binding = document.getElementById('editor-quality-binding');
    const current = isEditorReviewSnapshotCurrent();
    if (binding) { binding.textContent = editorReviewData ? `${current ? 'Saved snapshot' : 'Stale or pending-edit snapshot'} · book ${editorReviewData.book_token} · revision ${editorReviewData.revision}` : 'Book/revision evidence has not been loaded.'; }
    if (!current) {
        status.textContent = 'Saved review facts are unavailable or out of date. Refresh review; any displayed findings are from an earlier reading.';
        list.querySelectorAll('button').forEach(button => { button.disabled = true; });
        more.hidden = true;
        return;
    }
    const filter = document.getElementById('editor-quality-filter').value;
    const rows = editorReviewData.rows.map(row => ({...row, issues: [...row.issues,
        ...(row.audio === 'present' && (!row.speech || row.speech === 'not_checked') && !row.issues.includes('speech_historical') ? ['speech_not_checked'] : [])]}));
    const matches = rows.filter(row => filter === 'all_lines' || (row.issues.length && (filter === 'all' || row.issues.includes(filter))));
    const unmeasured = rows.filter(row => row.drift === 'unmeasured').length;
    const unavailable = rows.filter(row => row.drift === 'unavailable').length;
    status.textContent = `${rows.filter(row => row.issues.length).length} lines needing review (${editorReviewData.rows.filter(row => row.issues.length).length} with saved findings; others lack current evidence) · ${Object.entries(EDITOR_ISSUE_LABELS).map(([key, label]) => `${rows.filter(row => row.issues.includes(key)).length} ${label.toLowerCase()}`).join(' · ')}. Voice checks: ${unmeasured} unmeasured, ${unavailable} unavailable. Acoustic checks: ${rows.filter(row => !row.outlier || row.outlier.status === 'unmeasured').length} unmeasured, ${rows.filter(row => row.outlier?.status === 'unavailable' || row.outlier?.missing?.length).length} incomplete/unavailable. Audio file presence does not certify sound quality.`;
    if (editorReviewData.outlier_error) { status.textContent += ' ' + editorReviewData.outlier_error; }
    const shown = matches.slice(0, editorReviewLimit);
    more.hidden = shown.length === matches.length;
    const chunkNames = new Map(cachedChunks.map(chunk => [chunk.uid, chunk.speaker || chunk.type || 'Unknown speaker']));
    const chunksByUid = new Map(cachedChunks.map(chunk => [chunk.uid, chunk]));
    const signature = JSON.stringify([editorReviewData.book_token, editorReviewData.revision, filter,
        shown.map(row => [row.id, row.uid, row.issues, row.speech, chunkNames.get(row.uid), chunksByUid.get(row.uid)?.audio_revision,
            chunksByUid.get(row.uid)?.take])]);
    if (signature === editorReviewListSignature) { list.querySelectorAll('button').forEach(button => { button.disabled = false; }); return; }
    editorReviewListSignature = signature;
    list.replaceChildren();
    if (!matches.length) { list.textContent = 'No saved findings in this view. This does not establish that all lines are correct or checked.'; return; }
    for (const row of shown) {
        const item = document.createElement('div'); item.className = 'editor-quality-row';
        const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline-secondary btn-sm';
        button.textContent = `Inspect line ${row.id + 1} · ${chunkNames.get(row.uid)}`;
        const book = editorReviewData.book_token;
        const revision = editorReviewData.revision;
        const isCurrent = () => isEditorReviewSnapshotCurrent() && book === chunkSnapshotBookToken
            && revision === chunkSnapshotRevision && revision === editorReviewData?.revision
            && chunkSnapshotBook === currentBookFilename;
        const requireCurrent = () => {
            if (isCurrent()) { return true; }
            renderEditorQualityInbox();
            showToast('Review snapshot changed or edits are pending. Save or discard edits, then refresh review.', 'warning');
            return false;
        };
        button.addEventListener('click', () => { if (requireCurrent()) { selectEditorReviewLine(row.uid, book); } });
        const note = document.createElement('div'); note.className = 'small mt-1'; note.textContent = row.issues.map(issue => EDITOR_ISSUE_LABELS[issue]).join(' · ');
        const chunk = chunksByUid.get(row.uid);
        const identity = document.createElement('p'); identity.className = 'small mt-1 mb-1';
        identity.textContent = `Line UID ${row.uid} · selected take ${chunk?.take ?? 'unknown'} · audio revision ${chunk?.audio_revision || 'not recorded'} · ${EDITOR_SPEECH_LABELS[row.speech] || EDITOR_SPEECH_LABELS.not_checked}`;
        const takes = document.createElement('button'); takes.type = 'button'; takes.className = 'btn btn-outline-primary btn-sm';
        takes.textContent = 'Review retained takes';
        takes.title = 'Read-only comparison with saved candidates. Audition and explicit checked restoration stay separate.';
        takes.addEventListener('click', () => {
            if (!requireCurrent()) { return; }
            const target = getEditorReviewTarget({uid: row.uid, book});
            if (!target || getEditorReviewRowState(target).dirty) { showToast('Save or discard pending edits before comparing takes.', 'warning'); return; }
            if (!selectEditorReviewLine(row.uid, book)) { return; }
            const details = document.getElementById('editor-take-details');
            if (!details) { showToast('Take comparison controls are unavailable. Reload the page.', 'warning'); return; }
            details.open = true;
            void loadEditorTakeSnapshots(false, 0);
        });
        item.append(button, note, identity, takes); list.appendChild(item);
    }
}

function showMoreEditorFindings() { editorReviewLimit += 50; renderEditorQualityInbox(); }

function markEditorReviewStale() {
    editorReviewRequest++;
    editorReviewFresh = false;
    renderEditorQualityInbox();
    renderEditorLineInspector();
}

async function refreshEditorReview() {
    const request = ++editorReviewRequest;
    const book = chunkSnapshotBookToken, revision = chunkSnapshotRevision;
    editorReviewFresh = false;
    renderEditorQualityInbox();
    renderEditorLineInspector();
    if (!book || !revision || chunkSnapshotBook !== currentBookFilename) { return; }
    if (editorReviewPending) { await editorReviewPending.catch(() => {}); }
    if (request !== editorReviewRequest) { return; }
    const pending = API.get('/api/chunks/quality?expected_book_token=' + encodeURIComponent(book));
    editorReviewPending = pending;
    try {
        const data = await pending;
        if (request !== editorReviewRequest || book !== chunkSnapshotBookToken || revision !== chunkSnapshotRevision || chunkSnapshotBook !== currentBookFilename) { return; }
        if (!isEditorReviewDataValid(data)) { throw new Error('Review snapshot changed or was incomplete'); }
        editorReviewData = data;
        editorReviewFresh = true;
        renderEditorQualityInbox();
        renderEditorLineInspector();
    } catch (error) {
        if (request === editorReviewRequest) { markEditorReviewStale(); }
    } finally { if (editorReviewPending === pending) { editorReviewPending = null; } }
}

function resetEditorReview() {
    editorReviewRequest++;
    editorReviewData = null; editorReviewFresh = false; editorReviewSelection = null; editorReviewListSignature = null;
    document.getElementById('editor-quality-list').replaceChildren();
    renderEditorQualityInbox(); renderEditorLineInspector();
}

window.refreshEditorReview = refreshEditorReview;
window.markEditorReviewStale = markEditorReviewStale;
window.resetEditorReview = resetEditorReview;
const editorReviewRows = document.getElementById('chunks-table-body');
editorReviewRows.addEventListener('focusin', event => {
    const row = event.target.closest('tr[data-id]');
    if (row) { selectEditorReviewLineById(Number(row.dataset.id), false); }
});
editorReviewRows.addEventListener('input', () => { renderEditorLineInspector(); });
editorReviewRows.addEventListener('play', event => {
    if (event.target.classList.contains('chunk-audio')) { selectEditorReviewLineById(Number(event.target.dataset.id), false); }
}, true);
