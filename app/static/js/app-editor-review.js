// Review saved facts; reuse the Editor's original controls, drafts and audio.
const EDITOR_ISSUE_LABELS = {unknown: 'Unknown speaker', failed: 'Failed render', missing: 'Missing audio file', drift: 'Voice drift', speech_rejected: 'Rejected speech attempt', speech_review: 'Speech review needed', speech_unavailable: 'Speech check unavailable', speech_difference: 'Speech content difference'};
const EDITOR_DRIFT_LABELS = {not_rendered: 'Voice check not applicable before rendering', unmeasured: 'Voice check not measured', unavailable: 'Voice check unavailable or failed', flagged: 'Voice drift flagged', measured: 'Voice check measured; no drift flag'};
const EDITOR_SPEECH_LABELS = {not_checked: 'No current speech check', unavailable: 'Speech check unavailable', review: 'Transcript or acoustic differences need listening review', no_content_candidate: 'No material content candidate found; listening review still needed', corroborated_content_difference: 'Two ASR passes confirmed material missing or extra speech', review_confirmation_unavailable: 'Suspicious speech; stronger confirmation unavailable', review_short_acoustic_unavailable: 'Short-utterance acoustic check incomplete'};
let editorReviewData = null;
let editorReviewFresh = false;
let editorReviewRequest = 0;
let editorReviewPending = null;
let editorReviewSelection = null;
let editorReviewLimit = 50;
let editorReviewListSignature = null;

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

function renderEditorLineInspector() {
    const target = getEditorReviewTarget();
    document.getElementById('editor-line-empty').hidden = !!target;
    document.getElementById('editor-line-content').hidden = !target;
    document.querySelectorAll('#chunks-table-body .editor-selected-line').forEach(row => row.classList.remove('editor-selected-line'));
    if (!target) { editorReviewSelection = null; window.applyWordTimingInspector?.(); window.applySpeechVerificationInspector?.(); return; }
    const {chunk, row} = target;
    row.classList.add('editor-selected-line');
    const selectors = {speaker: 'select', text: '.chunk-text', instruct: '.chunk-instruct', pause_after: '.chunk-pause-after'};
    const values = Object.fromEntries(Object.entries(selectors).map(([key, selector]) => [key, row.querySelector(selector)?.value ?? String(chunk[key] ?? '')]));
    const dirty = Object.keys(selectors).some(key => String(values[key]) !== String(chunk[key] ?? ''));
    const set = (id, text) => { const node = document.getElementById(id); if (node.textContent !== text) { node.textContent = text; } };
    set('editor-line-title', `Line ${chunk.id + 1}`);
    set('editor-line-state', `${chunk.status || 'Status unavailable'} · ${dirty ? 'Unsaved row edits shown; review facts describe the saved line.' : 'Current row values shown.'}`);
    set('editor-line-speaker', values.speaker || 'Unknown speaker');
    set('editor-line-text', values.text);
    set('editor-line-instruct', values.instruct || 'No delivery instruction');
    set('editor-line-pause', values.pause_after === '' ? 'Use the configured default' : `${values.pause_after} ms`);
    set('editor-line-error', typeof chunk.error === 'string' && chunk.error ? chunk.error : 'No render error recorded.');
    const fact = editorReviewFresh && editorReviewData?.rows.find(item => item.uid === chunk.uid);
    renderEditorSpeechEvidence(chunk,fact);
    set('editor-line-quality', fact ? `${fact.audio === 'present' ? 'Nonempty audio file present (quality not certified)' : fact.audio === 'missing' ? 'Saved audio file is missing, empty or outside the project' : 'Audio not rendered'} · ${EDITOR_DRIFT_LABELS[fact.drift]}` : 'Review facts are unavailable or stale. Refresh review.');
    const drift = chunk.drift;
    set('editor-line-drift-detail', drift && typeof drift === 'object' ? [Number.isFinite(drift.score) ? `Recorded similarity ${drift.score}` : '', Number.isFinite(drift.threshold) ? `threshold ${drift.threshold}` : '', typeof drift.reference === 'string' ? `reference: ${drift.reference}` : '', typeof drift.error === 'string' ? `check error: ${drift.error}` : ''].filter(Boolean).join(' · ') : 'No recorded voice-check details.');
    const sources = chunk.source_entry_indices;
    set('editor-line-source', Array.isArray(sources) && sources.length && sources.every(value => Number.isSafeInteger(value) && value >= 0) ? `${editorReviewFresh ? 'Saved mapping' : 'Last loaded mapping; refresh review'}: ${sources.map(value => value + 1).join(', ')}` : 'Source-entry mapping not recorded in the loaded chunk.');
    const audio = row.querySelector('.chunk-audio');
    document.getElementById('editor-line-play').disabled = !audio?.getAttribute('src') || (fact && fact.audio === 'missing');
    document.getElementById('editor-line-generate').disabled = chunk.status === 'generating';
    window.applyWordTimingInspector?.();
    window.applySpeechVerificationInspector?.();
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
    const current=receipt && receipt.book_token === chunkSnapshotBookToken && receipt.uid === chunk.uid
        && receipt.audio_revision === chunk.audio_revision && receipt.status === fact.speech;
    const rejected=chunk.speech_verification_failure;
    const recheck=chunk.speech_verification_recheck;
    const incomplete=recheck && fact.speech_recheck === recheck.status && recheck.book_token === chunkSnapshotBookToken
        && recheck.uid === chunk.uid && recheck.audio_revision === chunk.audio_revision;
    const failure=fact.speech_attempt === 'rejected' && rejected?.book_token === chunkSnapshotBookToken
        && rejected.uid === chunk.uid && rejected.status === 'corroborated_content_difference';
    attempt.textContent=failure ? 'A later attempt was rejected for confirmed missing or extra speech. The prior take was kept. Listen, review the line, and generate it again.' : '';
    if (incomplete) { attempt.textContent+=' Latest recheck incomplete or unavailable; the existing selected-take evidence is retained.'; }
    expected.textContent=current && receipt.expected_text ? `Exact input sent for this take: ${receipt.expected_text}` : 'Exact synthesis input is not available for a current selected-take check.';
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
        failure ? format(rejected,'Rejected attempt; this is not the selected take') : '',
        incomplete ? format(recheck,'Latest incomplete recheck; earlier selected-take evidence retained') : ''].filter(Boolean).join('\n\n');
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
            && (row.speech === undefined || Object.hasOwn(EDITOR_SPEECH_LABELS,row.speech))
            && (row.speech_attempt == null || row.speech_attempt === 'rejected')
            && (row.speech_recheck == null || Object.hasOwn(EDITOR_SPEECH_LABELS,row.speech_recheck));
    });
}

function renderEditorQualityInbox(resetLimit = false) {
    if (resetLimit) { editorReviewLimit = 50; }
    const status = document.getElementById('editor-quality-status');
    const list = document.getElementById('editor-quality-list');
    const more = document.getElementById('editor-quality-more');
    if (!editorReviewFresh) {
        status.textContent = 'Saved review facts are unavailable or out of date. Refresh review; any displayed findings are from an earlier reading.';
        list.querySelectorAll('button').forEach(button => { button.disabled = true; });
        more.hidden = true;
        return;
    }
    const filter = document.getElementById('editor-quality-filter').value;
    const rows = editorReviewData.rows;
    const matches = rows.filter(row => row.issues.length && (filter === 'all' || row.issues.includes(filter)));
    const unmeasured = rows.filter(row => row.drift === 'unmeasured').length;
    const unavailable = rows.filter(row => row.drift === 'unavailable').length;
    status.textContent = `${rows.filter(row => row.issues.length).length} lines with saved findings · ${Object.entries(EDITOR_ISSUE_LABELS).map(([key, label]) => `${rows.filter(row => row.issues.includes(key)).length} ${label.toLowerCase()}`).join(' · ')}. Voice checks: ${unmeasured} unmeasured, ${unavailable} unavailable. Audio file presence does not certify sound quality.`;
    const shown = matches.slice(0, editorReviewLimit);
    more.hidden = shown.length === matches.length;
    const chunkNames = new Map(cachedChunks.map(chunk => [chunk.uid, chunk.speaker || chunk.type || 'Unknown speaker']));
    const signature = JSON.stringify([editorReviewData.book_token, filter, shown.map(row => [row.id, row.uid, row.issues, chunkNames.get(row.uid)])]);
    if (signature === editorReviewListSignature) { list.querySelectorAll('button').forEach(button => { button.disabled = false; }); return; }
    editorReviewListSignature = signature;
    list.replaceChildren();
    if (!matches.length) { list.textContent = 'No saved findings in this view. This does not establish that all lines are correct or checked.'; return; }
    for (const row of shown) {
        const item = document.createElement('div'); item.className = 'editor-quality-row';
        const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline-secondary btn-sm';
        button.textContent = `Inspect line ${row.id + 1} · ${chunkNames.get(row.uid)}`;
        const book = editorReviewData.book_token;
        button.addEventListener('click', () => { if (editorReviewFresh) { selectEditorReviewLine(row.uid, book); } });
        const note = document.createElement('div'); note.className = 'small mt-1'; note.textContent = row.issues.map(issue => EDITOR_ISSUE_LABELS[issue]).join(' · ');
        item.append(button, note); list.appendChild(item);
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
