// Review saved facts; reuse the Editor's original controls, drafts and audio.
const EDITOR_ISSUE_LABELS = {unknown: 'Unknown speaker', failed: 'Failed render', missing: 'Missing audio file', drift: 'Voice drift', outliers: 'Acoustic outliers', speech_rejected: 'Rejected speech attempt', speech_review: 'Speech review needed', speech_unavailable: 'Speech check unavailable', speech_difference: 'Speech content difference', speech_historical: 'Historical speech evidence needs recheck', speech_not_checked: 'No current speech evidence'};
const EDITOR_DRIFT_LABELS = {not_rendered: 'Voice check not applicable before rendering', unmeasured: 'Voice check not measured', unavailable: 'Voice check unavailable or failed', flagged: 'Voice drift flagged', measured: 'Voice check measured; no drift flag'};
const EDITOR_OUTLIER_LABELS = {not_rendered: 'Not rendered', unmeasured: 'Not checked', unavailable: 'Incomplete, stale or unavailable; not clean', flagged: 'Acoustic outlier: listen before deciding', measured: 'Acoustic proxies measured; no outlier flag, not quality certification'};
let editorOutlierBook = null;

function editorIssueLabel(key) {
    return {
        unknown: t('editor.review.issue.unknown', 'Unknown speaker'), failed: t('editor.review.issue.failed', 'Failed render'),
        missing: t('editor.review.issue.missing', 'Missing audio file'), drift: t('editor.review.issue.drift', 'Voice drift'),
        outliers: t('editor.review.issue.outliers', 'Acoustic outliers'), speech_rejected: t('editor.review.issue.speech-rejected', 'Rejected speech attempt'),
        speech_review: t('editor.review.issue.speech-review', 'Speech review needed'), speech_unavailable: t('editor.review.issue.speech-unavailable', 'Speech check unavailable'),
        speech_difference: t('editor.review.issue.speech-difference', 'Speech content difference'), speech_historical: t('editor.review.issue.speech-historical', 'Historical speech evidence needs recheck'),
        speech_not_checked: t('editor.review.issue.speech-not-checked', 'No current speech evidence')}[key] || key;
}

function editorDriftLabel(key) {
    return {
        not_rendered: t('editor.review.drift.not-rendered', 'Voice check not applicable before rendering'), unmeasured: t('editor.review.drift.unmeasured', 'Voice check not measured'),
        unavailable: t('editor.review.drift.unavailable', 'Voice check unavailable or failed'), flagged: t('editor.review.drift.flagged', 'Voice drift flagged'),
        measured: t('editor.review.drift.measured', 'Voice check measured; no drift flag')}[key] || key;
}

function editorOutlierLabel(key) {
    return {
        not_rendered: t('editor.review.outlier.not-rendered', 'Not rendered'), unmeasured: t('editor.review.outlier.unmeasured', 'Not checked'),
        unavailable: t('editor.review.outlier.unavailable', 'Incomplete, stale or unavailable; not clean'), flagged: t('editor.review.outlier.flagged-label', 'Acoustic outlier: listen before deciding'),
        measured: t('editor.review.outlier.measured', 'Acoustic proxies measured; no outlier flag, not quality certification')}[key] || key;
}

function editorSpeechLabel(key) {
    return {
        not_checked: t('editor.review.speech.not-checked', 'No current speech check'), unavailable: t('editor.review.speech.unavailable', 'Speech check unavailable'),
        review: t('editor.review.speech.review', 'Transcript or acoustic differences need listening review'),
        no_content_candidate: t('editor.review.speech.no-content-candidate', 'No material content candidate found; listening review still needed'),
        corroborated_content_difference: t('editor.review.speech.corroborated-difference', 'Two ASR passes confirmed material missing or extra speech'),
        review_confirmation_unavailable: t('editor.review.speech.confirmation-unavailable', 'Suspicious speech; stronger confirmation unavailable'),
        review_short_acoustic_unavailable: t('editor.review.speech.short-acoustic-unavailable', 'Short-utterance acoustic check incomplete')}[key] || key;
}

function getEditorOutlierText(fact) {
    const outlier = fact?.outlier;
    if (!outlier) { return t('editor.review.outlier.unavailable-refresh', 'Outlier evidence is unavailable. Refresh review.'); }
    const unavailable = t('editor.review.outlier.value-unavailable', 'unavailable');
    const lines = [editorOutlierLabel(Object.hasOwn(EDITOR_OUTLIER_LABELS, outlier.status) ? outlier.status : 'unavailable')];
    for (const [metric, record] of Object.entries(outlier.metrics || {})) {
        const value = Number.isFinite(record?.value) ? record?.value.toFixed(2) : unavailable;
        const z = Number.isFinite(record?.z) ? record?.z.toFixed(2) : unavailable;
        const median = Number.isFinite(record?.median) ? record.median.toFixed(2) : unavailable;
        const spread = Number.isFinite(record?.spread) ? record.spread.toFixed(2) : unavailable;
        lines.push(t('editor.review.outlier.metric', '{metric}: {value}; median {median}; spread {spread}; robust z {z}{flagged}', {
            metric: editorReviewData?.outlier_labels?.[metric] || metric, value, median, spread, z,
            flagged: record?.flagged === true ? ' ' + t('editor.review.outlier.flagged', '(flagged)') : ''}));
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
    status.textContent = t('editor.review.outlier.measuring', 'Measuring CPU acoustic proxies; no audio is changed. Partial results are not published.');
    _startPolling('logs:outlier_check', () => API.get('/api/status/outlier_check'), {
        doneCheck: current => !current.running,
        onTick: current => {
            if (current.book_token !== book) { return; }
            if (current.progress) {
                status.textContent = t('editor.review.outlier.progress', '{done} / {total} takes scanned (experimental proxies).', {done: current.progress.done, total: current.progress.total});
            }
        },
        onDone: async current => {
            button.disabled = false;
            cancel.disabled = true;
            editorOutlierBook = null;
            if (book !== chunkSnapshotBookToken || current.book_token !== book) { return; }
            status.textContent = (current.logs || []).slice(-1)[0] || t('editor.review.outlier.no-report', 'Scan ended without a report; not measured.');
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
        status.textContent = t('editor.review.outlier.drafts', 'Save or discard pending edits before checking acoustic outliers.');
        return;
    }
    if (!/^[0-9a-f]{64}$/.test(chunkSnapshotBookToken || '')) {
        status.textContent = t('editor.review.outlier.load-book', 'Load the selected book in Editor before checking acoustic outliers.');
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
        status.textContent = t('editor.review.outlier.unconfirmed', 'Outlier start is unconfirmed. Check task status before retrying; no quality conclusion.');
        showActionError(t('editor.review.outlier.failed', 'Outlier check failed'), error, t('editor.review.outlier.failed-recovery', 'Refresh the selected book and inspect outlier task status before retrying.'), 'warning');
    }
}

async function cancelOutlierCheck() {
    if (!editorOutlierBook) { return; }
    try {
        await API.post('/api/chunks/outlier_check/cancel', {expected_book_token: editorOutlierBook});
        document.getElementById('editor-outlier-status').textContent = t('editor.review.outlier.cancellation-requested', 'Cancellation requested; waiting for the scan to stop.');
    } catch (error) {
        showActionError(t('editor.review.outlier.cancel-unconfirmed', 'Outlier cancellation unconfirmed'), error, t('editor.review.outlier.cancel-recovery', 'Check outlier task status before retrying.'), 'warning');
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
    const message = t('editor.review.preview.idle', 'Read-only preview of the saved line. No model is loaded. Refresh after line, rule, voice or settings changes.');
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
        document.getElementById('editor-line-input-status').textContent = t('editor.review.preview.drafts', 'Save or discard pending line edits before previewing saved input.');
        return;
    }
    const selection = {...editorReviewSelection}, revision = chunkSnapshotRevision;
    const request = editorSpeechPreviewRequest;
    editorSpeechPreview = {uid: selection.uid, book: selection.book, revision};
    const status = document.getElementById('editor-line-input-status');
    editorSpeechPreviewBusy = true;
    document.getElementById('editor-line-input-preview').disabled = true;
    status.textContent = t('editor.review.preview.reading', 'Reading saved speech preparation; no generation starts.');
    const isCurrent = () => request === editorSpeechPreviewRequest && revision === chunkSnapshotRevision
        && selection.uid === editorReviewSelection?.uid && selection.book === editorReviewSelection?.book
        && !!getEditorReviewTarget(selection) && !isEditorEditPending();
    try {
        const result = await API.post('/api/pronunciation/occurrences', {
            expected_book_token: selection.book, expected_uid: selection.uid, index: target.chunk.id
        });
        if (!isCurrent()) { return; }
        if (result.preview?.kind !== 'current_settings_preview') {
            throw new Error(t('editor.review.preview.protocol', 'Preview protocol unavailable. Reload the page to match the server version.'));
        }
        if (result.book_token !== selection.book || result.uid !== selection.uid
                || result.preview.authored_text !== (target.chunk.text ?? '')) {
            throw new Error(t('editor.review.preview.changed', 'Saved line changed. Refresh Editor before inspecting its input.'));
        }
        status.textContent = t('editor.review.preview.snapshot', 'Snapshot at last preview, not the input recorded for a take. May be outdated: refresh after line, rule, voice or settings changes. Historical transformation traces are not reconstructed.');
        if (result.preview.warnings?.length) { status.textContent = joinUiSentences([status.textContent, t('editor.review.preview.warnings', 'Preparation warnings: {warnings}', {warnings: result.preview.warnings.join('; ')})]); }
        document.getElementById('editor-line-input-details').textContent = JSON.stringify({
            authored_text: result.preview.authored_text, prepared_text: result.preview.text,
            transformations: result.preview.transformations, pronunciation_decisions: result.preview.decisions,
            warnings: result.preview.warnings,
            context: result.context, settings_source: result.context_source, rules_revision: result.revision
        }, null, 2);
    } catch (error) {
        if (isCurrent()) { status.textContent = t('editor.review.preview.unavailable', 'Speech preparation unavailable: {detail}', {detail: error.message}); }
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
    if (!target) { showToast(t('editor.review.line-changed', 'This line changed. Refresh Editor before opening the finding.'), 'warning'); return false; }
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
    set('editor-line-title', t('editor.review.inspector.line', 'Line {line}', {line: chunk.id + 1}));
    set('editor-line-state', t('editor.review.inspector.state', '{status} · {values}', {
        status: chunk.status || t('editor.review.inspector.status-unavailable', 'Status unavailable'),
        values: dirty ? t('editor.review.inspector.dirty', 'Unsaved row edits shown; review facts describe the saved line.') : t('editor.review.inspector.current', 'Current row values shown.')}));
    set('editor-line-speaker', values.speaker || t('take.excerpt.speaker-unknown', 'Speaker unknown'));
    set('editor-line-text', values.text);
    set('editor-line-instruct', values.instruct || t('editor.review.inspector.no-delivery', 'No delivery instruction'));
    set('editor-line-pause', values.pause_after === '' ? t('editor.review.inspector.default-pause', 'Use the configured default') : t('editor.review.inspector.pause-ms', '{pause} ms', {pause: values.pause_after}));
    set('editor-line-error', typeof chunk.error === 'string' && chunk.error ? chunk.error : t('editor.review.inspector.no-error', 'No render error recorded.'));
    const fact = isEditorReviewSnapshotCurrent()
        && editorReviewData.rows.find(item => item.uid === chunk.uid);
    renderEditorSpeechEvidence(chunk,fact);
    renderEditorRecordedInput(chunk, fact);
    const outlierNode = document.getElementById('editor-line-outlier');
    if (outlierNode) { outlierNode.textContent = getEditorOutlierText(fact); }
    set('editor-line-quality', fact ? t('editor.review.inspector.quality', '{audio} · {drift}', {audio: fact.audio === 'present' ? t('editor.review.inspector.audio-present', 'Nonempty audio file present (quality not certified)') : fact.audio === 'missing' ? t('editor.review.inspector.audio-missing', 'Saved audio file is missing, empty or outside the project') : t('editor.review.inspector.audio-none', 'Audio not rendered'), drift: editorDriftLabel(fact.drift)}) : t('editor.review.inspector.stale', 'Review facts are unavailable or stale. Refresh review.'));
    const drift = chunk.drift;
    const driftParts = drift && typeof drift === 'object' ? [
        Number.isFinite(drift.score) ? t('editor.review.drift.similarity', 'Recorded similarity {score}', {score: drift.score}) : '',
        Number.isFinite(drift.threshold) ? t('editor.review.drift.threshold', 'threshold {threshold}', {threshold: drift.threshold}) : '',
        typeof drift.reference === 'string' ? t('editor.review.drift.reference', 'reference: {reference}', {reference: drift.reference}) : '',
        typeof drift.error === 'string' ? t('editor.review.drift.error', 'check error: {error}', {error: drift.error}) : ''].filter(Boolean) : [];
    set('editor-line-drift-detail', driftParts.length ? driftParts.join(' · ') : t('editor.review.drift.none', 'No recorded voice-check details.'));
    const sources = chunk.source_entry_indices;
    set('editor-line-source', Array.isArray(sources) && sources.length && sources.every(value => Number.isSafeInteger(value) && value >= 0)
        ? t('editor.review.inspector.source-mapping', '{kind}: {entries}', {kind: editorReviewFresh ? t('editor.review.inspector.source-saved', 'Saved mapping') : t('editor.review.inspector.source-loaded', 'Last loaded mapping; refresh review'), entries: sources.map(value => value + 1).join(', ')})
        : t('editor.review.inspector.source-none', 'Source-entry mapping not recorded in the loaded chunk.'));
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
        summary.textContent=t('editor.review.speech.stale', 'Speech evidence is unavailable or stale. Refresh review.');
        attempt.textContent='';expected.textContent='';details.textContent='';return;
    }
    summary.textContent=editorSpeechLabel(Object.hasOwn(EDITOR_SPEECH_LABELS, fact.speech) ? fact.speech : 'not_checked');
    const receipt=chunk.speech_verification;
    const historical=chunk.speech_verification_historical;
    const historicalReason=chunk.speech_verification_historical_reason;
    const current=receipt && receipt.book_token === chunkSnapshotBookToken && receipt.uid === chunk.uid
        && receipt.audio_revision === chunk.audio_revision && receipt.status === fact.speech;
    if (!current && historical) {
        summary.textContent = historicalReason === 'legacy'
            ? t('editor.review.speech.historical.legacy', 'Historical speech evidence saved before audio ctime was recorded. Recheck to renew it; no byte change has been detected.')
            : historicalReason === 'platform'
                ? t('editor.review.speech.historical.platform', 'Historical speech evidence; this host cannot verify audio byte freshness, so recheck is unavailable here.')
                : historicalReason === 'unavailable'
                    ? t('editor.review.speech.historical.unavailable', 'Historical speech evidence; the audio file is currently unavailable or unreadable.')
                    : historicalReason === 'changed'
                        ? t('editor.review.speech.historical.changed', 'Historical speech evidence; the current audio file no longer matches its recorded filesystem identity. Recheck verifies the bytes, or regenerate before relying on this result.')
                        : t('editor.review.speech.historical.unknown', 'Historical speech evidence; its freshness could not be classified. Recheck is unavailable until the reason is known.');
    }
    const rejected=chunk.speech_verification_failure;
    const recheck=chunk.speech_verification_recheck;
    const incomplete=recheck && fact.speech_recheck === recheck.status && recheck.book_token === chunkSnapshotBookToken
        && recheck.uid === chunk.uid && recheck.audio_revision === chunk.audio_revision;
    const failure=fact.speech_attempt === 'rejected' && rejected?.book_token === chunkSnapshotBookToken
        && rejected.uid === chunk.uid && rejected.status === 'corroborated_content_difference';
    attempt.textContent=failure ? t('editor.review.speech.rejected', 'A later attempt was rejected for confirmed missing or extra speech. The prior take was kept. Listen, review the line, and generate it again.') : '';
    if (incomplete) { attempt.textContent = joinUiSentences([attempt.textContent, t('editor.review.speech.recheck-incomplete', 'Latest recheck incomplete or unavailable; the existing selected-take evidence is retained.')]); }
    expected.textContent=current && receipt.expected_text ? t('editor.review.speech.expected-current', 'Exact input sent for this take: {text}', {text: receipt.expected_text})
        : historical?.expected_text ? t('editor.review.speech.expected-historical', 'Historical exact input sent for this take: {text}', {text: historical.expected_text})
        : t('editor.review.speech.expected-unavailable', 'Exact synthesis input is not available for a current selected-take check.');
    const format=(value,label)=>{
        const notes=[label, editorSpeechLabel(value.status), value.reason || '',
            Number.isFinite(value.seconds) ? t('editor.review.speech.added-time', 'Added check time: {seconds} seconds', {seconds: value.seconds.toFixed(2)}) : ''];
        for (const name of ['primary','confirmation']) {
            const pass=value[name];if (!pass) { continue; }
            notes.push(t('editor.review.speech.pass-counts', '{pass}: {expected} expected tokens, {heard} heard, {unmatched_expected} unmatched expected, {unmatched_observed} unmatched heard.', {pass: name, expected: pass.expected_words, heard: pass.observed_words, unmatched_expected: pass.unmatched_expected, unmatched_observed: pass.unmatched_observed}));
            for (const difference of pass.differences || []) {
                notes.push(t('editor.review.speech.difference', '{kind}: expected “{expected}”; heard “{observed}”{ambiguity}', {kind: difference.kind, expected: difference.expected, observed: difference.observed, ambiguity: difference.ambiguity?.length ? ` (${difference.ambiguity.join(', ')})` : ''}));
            }
            if (pass.diagnostics_abbreviated) { notes.push(t('editor.review.speech.differences-abbreviated', 'Showing {shown} of {total} differences; counts cover the full bounded comparison.', {shown: pass.differences.length, total: pass.difference_count})); }
        }
        notes.push(t('editor.review.speech.identities', 'Saved identities and bounds:'), JSON.stringify(value,null,2));
        return notes.filter(Boolean).join('\n');
    };
    details.textContent=[current ? format(receipt, t('editor.review.speech.selected-take', 'Selected take')) : '',
        !current && historical ? format(historical, t('editor.review.speech.historical-take', 'Historical saved take ({reason})', {reason: historicalReason || t('editor.review.speech.unverified', 'unverified')})) : '',
        failure ? format(rejected, t('editor.review.speech.rejected-label', 'Rejected attempt; this is not the selected take')) : '',
        incomplete ? format(recheck, t('editor.review.speech.recheck-label', 'Latest incomplete recheck; earlier selected-take evidence retained')) : ''].filter(Boolean).join('\n\n');
}

function renderEditorRecordedInput(chunk, fact) {
    const status = document.getElementById('editor-line-recorded-status');
    const binding = document.getElementById('editor-line-recorded-binding');
    const decisions = document.getElementById('editor-line-recorded-pronunciation');
    if (!status || !binding || !decisions) { return; }
    binding.textContent = ''; decisions.textContent = '';
    if (!fact) { status.textContent = t('editor.review.recorded.stale', 'Saved review facts are unavailable or stale. Refresh review before inspecting recorded input.'); return; }
    const states = {
        current: t('editor.review.recorded.state.current', 'Exact input recorded for the selected take, not reconstructed from current settings.'),
        historical: t('editor.review.recorded.state.historical', 'Recorded input is historical; the selected audio bytes are not currently attested. See the speech-evidence freshness reason.'),
        stale: t('editor.review.recorded.state.stale', 'Saved input evidence is stale and is not shown as proof for this take.'),
        invalid: t('editor.review.recorded.state.invalid', 'Saved input evidence is malformed or incomplete; unavailable, not proof.'),
        not_recorded: t('editor.review.recorded.state.not-recorded', 'No exact input was recorded for this take. Speech checks may have been off. Current settings cannot reconstruct historical proof.')
    };
    const reasons = {
        other_line: t('editor.review.recorded.reason.other-line', 'The record belongs to another book or line.'), other_take: t('editor.review.recorded.reason.other-take', 'The record belongs to another take revision.'),
        inputs_changed: t('editor.review.recorded.reason.inputs-changed', 'Saved text, pronunciation rules, delivery or voice revision changed.'),
        unverifiable: t('editor.review.recorded.reason.unverifiable', 'The record cannot be tied to the selected audio.')
    };
    status.textContent = Object.hasOwn(states, chunk.recorded_input_state) ? states[chunk.recorded_input_state]
        : t('editor.review.recorded.state.unavailable', 'Recorded-input state unavailable. Reload to match the server version.');
    if (Object.hasOwn(reasons, chunk.recorded_input_reason)) { status.textContent = joinUiSentences([status.textContent, reasons[chunk.recorded_input_reason]]); }
    if (!chunk.audio_path) { status.textContent = joinUiSentences([t('editor.review.recorded.no-audio', 'No selected audio file. Retained metadata follows, not current audio proof.'), status.textContent]); }
    if (chunk.speech_verification_failure) { status.textContent = joinUiSentences([status.textContent, t('editor.review.recorded.rejected-separate', 'Rejected-attempt evidence is separate from the selected take.')]); }
    if (['current', 'historical'].includes(chunk.recorded_input_state)) {
        const receipt = chunk.recorded_input_state === 'current' ? chunk.speech_verification : chunk.speech_verification_historical;
        if (!receipt || receipt.book_token !== chunkSnapshotBookToken || receipt.uid !== chunk.uid
                || receipt.audio_revision !== chunk.audio_revision) {
            status.textContent = t('editor.review.recorded.identity-mismatch', 'Recorded input identity does not match this selection. Refresh review.'); return;
        }
        binding.textContent = t('editor.review.recorded.binding', 'Book {book} · line {uid} · selected take {take} · audio revision {audio_revision}', {book: receipt.book_token, uid: receipt.uid, take: chunk.take ?? t('take.meta.unknown', 'unknown'), audio_revision: receipt.audio_revision}) + '\n'
            + t('editor.review.recorded.snapshot', 'Current review snapshot {revision} (not a recorded capture revision; capture revision unavailable)', {revision: chunkSnapshotRevision}) + '\n'
            + t('editor.review.recorded.digests', 'Saved generation-inputs digest {digest} · exact input SHA-256 {sha}', {digest: receipt.input_digest, sha: receipt.expected_sha256}) + '\n'
            + t('editor.review.recorded.audio', 'Audio SHA-256 {sha} · language {language} · capture {provenance}', {sha: receipt.audio?.sha256 || t('editor.review.outlier.value-unavailable', 'unavailable'), language: receipt.language, provenance: receipt.input_provenance}) + '\n'
            + t('editor.review.recorded.digest-scope', 'The input digest binds saved line fields and voice revision, not every engine setting. Historical engine settings are unavailable in this receipt.');
    }
    const pronunciation = chunk.pronunciation_receipt;
    if (chunk.recorded_pronunciation_state !== 'recorded' || !pronunciation
            || pronunciation.audio_revision !== chunk.audio_revision) {
        decisions.textContent = chunk.recorded_pronunciation_state === 'not_recorded'
            ? t('editor.review.pronunciation.not-recorded', 'No occurrence decisions recorded for this take; this is not proof that no transformations occurred.')
            : t('editor.review.pronunciation.unavailable', 'Recorded occurrence decisions unavailable ({state}). Refresh after changing the saved line.', {state: chunk.recorded_pronunciation_state || t('take.meta.unknown', 'unknown')});
        return;
    }
    decisions.textContent = t('editor.review.pronunciation.recorded', 'Recorded occurrence decisions (not recomputed):') + '\n'
        + pronunciation.decisions.map(item => t('editor.review.pronunciation.decision', '{rule}: {applied} · {reason}', {rule: item.id || t('editor.review.pronunciation.unidentified', 'Unidentified rule'), applied: item.applied ? t('editor.review.pronunciation.applied', 'applied once') : t('editor.review.pronunciation.not-applied', 'not applied'), reason: item.reason})).join('\n')
        + '\n' + t('editor.review.pronunciation.context', 'Recorded context: {context}', {context: JSON.stringify(pronunciation.context)}) + '\n' + t('editor.review.pronunciation.authored-sha', 'Authored text SHA-256 {sha}', {sha: pronunciation.text_sha256})
        + '\n' + t('editor.review.pronunciation.prepared-sha', 'Prepared-text SHA-256 {sha}', {sha: pronunciation.prepared_sha256}) + '\n' + t('editor.review.pronunciation.speech-sha', 'Speech-text SHA-256 {sha}', {sha: pronunciation.speech_sha256});
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
    catch (error) { showActionError(t('editor.review.play.failed', 'Could not play this line'), error, t('editor.review.play.recovery', 'Refresh review and check the saved audio file.')); }
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
    if (binding) { binding.textContent = editorReviewData ? t('editor.review.queue.binding', '{kind} · book {book} · revision {revision}', {kind: current ? t('editor.review.queue.binding-saved', 'Saved snapshot') : t('editor.review.queue.binding-stale', 'Stale or pending-edit snapshot'), book: editorReviewData.book_token, revision: editorReviewData.revision}) : t('editor.review.queue.binding-none', 'Book/revision evidence has not been loaded.'); }
    if (!current) {
        status.textContent = t('editor.review.queue.stale', 'Saved review facts are unavailable or out of date. Refresh review; any displayed findings are from an earlier reading.');
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
    const issueCounts = Object.keys(EDITOR_ISSUE_LABELS).map(key => t('editor.review.queue.issue-count', '{count} {label}', {count: rows.filter(row => row.issues.includes(key)).length, label: editorIssueLabel(key).toLowerCase()})).join(' · ');
    status.textContent = t('editor.review.queue.summary', '{lines} lines needing review ({saved} with saved findings; others lack current evidence) · {issues}. Voice checks: {unmeasured} unmeasured, {unavailable} unavailable. Acoustic checks: {outlier_unmeasured} unmeasured, {outlier_unavailable} incomplete/unavailable. Audio file presence does not certify sound quality.', {
        lines: rows.filter(row => row.issues.length).length,
        saved: editorReviewData.rows.filter(row => row.issues.length).length,
        issues: issueCounts, unmeasured, unavailable,
        outlier_unmeasured: rows.filter(row => !row.outlier || row.outlier.status === 'unmeasured').length,
        outlier_unavailable: rows.filter(row => row.outlier?.status === 'unavailable' || row.outlier?.missing?.length).length});
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
    if (!matches.length) { list.textContent = t('editor.review.queue.empty', 'No saved findings in this view. This does not establish that all lines are correct or checked.'); return; }
    for (const row of shown) {
        const item = document.createElement('div'); item.className = 'editor-quality-row';
        const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline-secondary btn-sm';
        button.textContent = t('editor.review.queue.inspect', 'Inspect line {line} · {speaker}', {line: row.id + 1, speaker: chunkNames.get(row.uid)});
        const book = editorReviewData.book_token;
        const revision = editorReviewData.revision;
        const isCurrent = () => isEditorReviewSnapshotCurrent() && book === chunkSnapshotBookToken
            && revision === chunkSnapshotRevision && revision === editorReviewData?.revision
            && chunkSnapshotBook === currentBookFilename;
        const requireCurrent = () => {
            if (isCurrent()) { return true; }
            renderEditorQualityInbox();
            showToast(t('editor.review.queue.changed', 'Review snapshot changed or edits are pending. Save or discard edits, then refresh review.'), 'warning');
            return false;
        };
        button.addEventListener('click', () => { if (requireCurrent()) { selectEditorReviewLine(row.uid, book); } });
        const note = document.createElement('div'); note.className = 'small mt-1'; note.textContent = row.issues.map(issue => editorIssueLabel(issue)).join(' · ');
        const chunk = chunksByUid.get(row.uid);
        const identity = document.createElement('p'); identity.className = 'small mt-1 mb-1';
        identity.textContent = t('editor.review.queue.identity', 'Line UID {uid} · selected take {take} · audio revision {revision} · {speech}', {uid: row.uid, take: chunk?.take ?? t('take.meta.unknown', 'unknown'), revision: chunk?.audio_revision || t('editor.review.queue.not-recorded', 'not recorded'), speech: editorSpeechLabel(Object.hasOwn(EDITOR_SPEECH_LABELS, row.speech) ? row.speech : 'not_checked')});
        const takes = document.createElement('button'); takes.type = 'button'; takes.className = 'btn btn-outline-primary btn-sm';
        takes.textContent = t('editor.review.queue.takes', 'Review retained takes');
        takes.title = t('editor.review.queue.takes-title', 'Read-only comparison with saved candidates. Audition and explicit checked restoration stay separate.');
        takes.addEventListener('click', () => {
            if (!requireCurrent()) { return; }
            const target = getEditorReviewTarget({uid: row.uid, book});
            if (!target || getEditorReviewRowState(target).dirty) { showToast(t('editor.review.queue.drafts', 'Save or discard pending edits before comparing takes.'), 'warning'); return; }
            if (!selectEditorReviewLine(row.uid, book)) { return; }
            const details = document.getElementById('editor-take-details');
            if (!details) { showToast(t('editor.review.queue.no-controls', 'Take comparison controls are unavailable. Reload the page.'), 'warning'); return; }
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
