/* Single-narrator checklist support (#1724): read-only source confirmation
   plus the compact Voices single-voice action. Navigation stays in
   app-guides.js; sample/render/export/readiness stay on existing controls.
   This file never starts generation, never writes the literal checkbox,
   and never plays or approves audio. */
let _singleVoiceSourcePreview = null;
let _singleVoiceSourceBinding = null;
let _singleVoiceBusy = false;
let _singleVoiceGeneration = 0;
let _singleVoiceSpeakersKey = null;

function getSingleVoiceLiteralChecked() {
    // One policy for literal mode: the canonical accessor only. app-core.js
    // loads before this file, so it is always present; no DOM fallback that
    // could silently disagree with it (#1724 C053).
    return _isLiteralSourcePreservingChecked();
}

function getSingleVoiceSourceBinding(preview) {
    return {book: currentBookFilename || '', epoch: window._bookSelectionEpoch ?? null,
        token: preview?.book_token || '', sha: preview?.source_sha256 || '',
        literal: getSingleVoiceLiteralChecked()};
}

function isSingleVoiceSourceConfirmed() {
    if (!_singleVoiceSourceBinding || !_singleVoiceSourcePreview) { return false; }
    const current = getSingleVoiceSourceBinding(_singleVoiceSourcePreview);
    return _singleVoiceSourceBinding.book === current.book
        && _singleVoiceSourceBinding.epoch === current.epoch
        && _singleVoiceSourceBinding.token === current.token
        && _singleVoiceSourceBinding.sha === current.sha
        && _singleVoiceSourceBinding.literal === current.literal;
}

// One book/epoch freshness check for every single-voice action (#1724 C076).
function singleVoiceBookEpochCurrent(book, epoch) {
    return book === currentBookFilename && epoch === window._bookSelectionEpoch;
}

// Logical book identity is the operation token: snapshot.book_identity_token
// (== source-preview book_token), NOT the voice snapshot book_token (a
// script+voices digest). The selected-upload object is authoritative when
// present; saved-script books fall back to the already rendered snapshot's
// identity token. Capture BEFORE every await.
function singleVoiceCaptureIdentity() {
    const selection = window._selectedUploadIdentity || null;
    const snapshotToken = (typeof _voiceSaveSnapshot !== 'undefined' && _voiceSaveSnapshot)
        ? (_voiceSaveSnapshot.book_identity_token || null) : null;
    const identityToken = selection ? (selection.token || null)
        : (snapshotToken || null);
    return {book: currentBookFilename, epoch: window._bookSelectionEpoch ?? null,
        selection, identityToken, generation: _singleVoiceGeneration};
}

function singleVoiceIdentityCurrent(identity) {
    if (!identity) { return false; }
    if (identity.generation !== _singleVoiceGeneration) { return false; }
    if (!singleVoiceBookEpochCurrent(identity.book, identity.epoch)) { return false; }
    if (identity.selection && window._selectedUploadIdentity !== identity.selection) { return false; }
    if (identity.selection && window._bookSelectionEpoch !== identity.epoch) { return false; }
    if (identity.identityToken) {
        // Once a snapshot for this tab is rendered, its identity must match;
        // before any snapshot exists only the selection object frames it.
        const currentSnapshotToken = (typeof _voiceSaveSnapshot !== 'undefined' && _voiceSaveSnapshot)
            ? (_voiceSaveSnapshot.book_identity_token || null) : null;
        if (currentSnapshotToken && currentSnapshotToken !== identity.identityToken) { return false; }
        if (identity.selection && window._selectedUploadIdentity
                && window._selectedUploadIdentity.token !== identity.identityToken) { return false; }
    }
    return true;
}

function singleVoiceRefreshOpts(identity) {
    if (!identity) { return {}; }
    if (identity.selection) { return {expectedSelection: identity.selection}; }
    if (identity.identityToken) { return {expectedIdentityToken: identity.identityToken}; }
    return {};
}

// A save error is a DEFINITE refusal only when the server answered with a
// documented validation/CAS status (400/409/422): those responses guarantee
// no commit. Transport failures, timeouts, aborts and 5xx (proxy/app errors
// that may follow a committed write) are UNKNOWN outcomes. The actionable
// detail is always kept in the displayed message.
function singleVoiceIsDefiniteSaveRefusal(error) {
    const status = error && typeof error.status === 'number' ? error.status : null;
    return status === 400 || status === 409 || status === 422;
}

function singleVoiceSaveErrorDetail(error) {
    return (error && (error.message || error.detail)) || 'Voice save failed.';
}

function renderSingleVoiceSourcePreview() {
    const box = document.getElementById('source-preview-text');
    const meta = document.getElementById('source-preview-meta');
    const confirmBox = document.getElementById('source-preview-confirm');
    if (!box || !meta) { return; }
    if (!_singleVoiceSourcePreview) {
        box.textContent = 'No source preview loaded for this book.';
        meta.textContent = '';
        if (confirmBox) { confirmBox.checked = false; }
        const emptyStatus = document.getElementById('source-preview-status');
        if (emptyStatus) { emptyStatus.textContent = 'No source preview loaded.'; }
        return;
    }
    const preview = _singleVoiceSourcePreview;
    box.textContent = preview.text_start || '';
    // total_chars counts Unicode code points (Python len); count code points
    // here too, not UTF-16 code units (#1724 C010).
    const excerptChars = [...(preview.text_start || '')].length;
    meta.textContent = `${preview.source_name} · encoding ${preview.encoding} · original extracted text (before annotation/preparation) · `
        + `${preview.total_chars} chars total · excerpt ${excerptChars} shown`
        + (preview.truncated ? ' (truncated)' : '') + ` · sha256 ${preview.source_sha256.slice(0, 16)}…`
        + ` · book ${(preview.book_token || '').slice(0, 12)}…`;
    const confirmed = isSingleVoiceSourceConfirmed();
    if (confirmBox) { confirmBox.checked = confirmed; }
    const status = document.getElementById('source-preview-status');
    if (status) { status.textContent = confirmed
        ? 'Loaded excerpt snapshot confirmed in this tab. Reload the preview to check the current source.'
        : 'Loaded excerpt snapshot not confirmed for the current book/source.'; }
}

async function loadSingleVoiceSourcePreview() {
    if (_singleVoiceBusy) {
        showToast('A single-voice action is still running; please wait.', 'warning');
        return;
    }
    // Capture book+epoch+selection+identity before the await: a book switch
    // while the GET is pending must not install the old book's preview (or
    // let a later confirmation bind it) into the new book. Epoch also
    // catches A->B->A; selection/identity catches another tab's book B.
    const identity = singleVoiceCaptureIdentity();
    const generation = _singleVoiceGeneration;
    _singleVoiceBusy = true;
    try {
        // Shared API.get deadline (abort included) instead of a second
        // Promise.race copy that never aborted the fetch (#1724 C048).
        const preview = await API.get('/api/single-narrator/source-preview',
            {timeoutMs: 60000, timeoutMessage: 'The source preview timed out. Check the selected book and try again.'});
        if (!singleVoiceIdentityCurrent(identity) || generation !== _singleVoiceGeneration) { return; }
        if (!preview || typeof preview.text_start !== 'string' || !/^[0-9a-f]{64}$/.test(preview.book_token || '')
            || !/^[0-9a-f]{64}$/.test(preview.source_sha256 || '')) {
            throw new Error('Invalid source preview; your confirmation was not recorded.');
        }
        // Fence BEFORE installing: the server's operation token must equal
        // the identity this tab captured. Never compare to the voice
        // snapshot book_token (a different digest). A missing captured token
        // fails closed: the book identity is still loading.
        if (!identity.identityToken) {
            throw new Error('Voices are still loading for this book. Wait for Voices to finish, then reload the source preview.');
        }
        if (preview.book_token !== identity.identityToken) {
            throw new Error('Active book changed while loading the source preview. Nothing was confirmed; reload the preview for the current book.');
        }
        if (!preview.text_start.trim()) {
            throw new Error('Source excerpt is blank; import a non-blank book before confirming.');
        }
        _singleVoiceSourcePreview = preview;
        if (_singleVoiceSourceBinding) {
            const current = getSingleVoiceSourceBinding(preview);
            if (_singleVoiceSourceBinding.sha !== current.sha || _singleVoiceSourceBinding.token !== current.token
                || _singleVoiceSourceBinding.book !== current.book || _singleVoiceSourceBinding.epoch !== current.epoch
                || _singleVoiceSourceBinding.literal !== current.literal) {
                _singleVoiceSourceBinding = null;
            }
        }
        renderSingleVoiceSourcePreview();
    } catch (error) {
        // Guard by the owning generation BEFORE clearing globals: a stale
        // rejection after a book switch must not wipe the new book's
        // preview/binding or toast over its state.
        if (generation !== _singleVoiceGeneration) { return; }
        _singleVoiceSourcePreview = null;
        _singleVoiceSourceBinding = null;
        renderSingleVoiceSourcePreview();
        showActionError('Source preview failed', error, 'Inspect the extracted text in Script instead. Check the selected book and source first.');
    } finally {
        // A book switch bumps the generation and clears the busy flag for the
        // new book; only the owning generation may clear it here (#1724 C048).
        if (generation === _singleVoiceGeneration) { _singleVoiceBusy = false; }
    }
}

function onSingleVoiceSourceConfirm() {
    const box = document.getElementById('source-preview-confirm');
    if (!box?.checked) {
        _singleVoiceSourceBinding = null;
        renderSingleVoiceSourcePreview();
        return;
    }
    if (!_singleVoiceSourcePreview) {
        box.checked = false;
        showToast('Load the source preview before confirming it.', 'warning');
        return;
    }
    _singleVoiceSourceBinding = getSingleVoiceSourceBinding(_singleVoiceSourcePreview);
    renderSingleVoiceSourcePreview();
}

function makeSingleVoicePlaceholder(select) {
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Choose a speaker whose voice you picked…';
    select.appendChild(placeholder);
}

window.resetSingleVoiceBook = function resetSingleVoiceBook() {
    _singleVoiceGeneration += 1;
    _singleVoiceBusy = false;
    _singleVoiceSourcePreview = null;
    _singleVoiceSourceBinding = null;
    _singleVoiceSpeakersKey = null;
    renderSingleVoiceSourcePreview();
    const select = document.getElementById('single-voice-source');
    if (select) {
        select.replaceChildren();
        makeSingleVoicePlaceholder(select);
    }
};

function refreshSingleVoiceSpeakers() {
    const select = document.getElementById('single-voice-source');
    if (!select) { return; }
    const voices = (typeof _voiceSaveSnapshot !== 'undefined' && _voiceSaveSnapshot?.voices) || [];
    const names = voices.map(voice => voice?.name).filter(Boolean);
    const rosterKey = JSON.stringify(names);
    // Cheap dedup: identical roster + preserved selection needs no DOM rebuild.
    if (rosterKey === _singleVoiceSpeakersKey && select.children && select.children.length === names.length + 1) {
        if (!select.value || names.includes(select.value)) { return; }
    }
    const current = select.value;
    select.replaceChildren();
    makeSingleVoicePlaceholder(select);
    let restored = false;
    for (const voice of voices) {
        if (!voice?.name) { continue; }
        const option = document.createElement('option');
        option.value = voice.name;
        option.textContent = voice.name;
        select.appendChild(option);
        if (voice.name === current) { restored = true; }
    }
    _singleVoiceSpeakersKey = rosterKey;
    if (current && restored) { select.value = current; }
    else if (current) {
        // A previously selected speaker left the roster: say so instead of
        // silently resetting the select to the placeholder (#1724 C052).
        showToast(`Speaker '${current}' is no longer in this book's roster; choose a speaker again.`, 'warning');
    }
}

async function previewSingleVoiceBook() {
    if (_singleVoiceBusy) {
        showToast('A single-voice action is still running; please wait.', 'warning');
        return;
    }
    const select = document.getElementById('single-voice-source');
    const source = select?.value || '';
    if (!source) {
        showToast('Choose a speaker whose voice you picked on its card first.', 'warning');
        return;
    }
    if (typeof _voiceSaveSnapshot === 'undefined' || !_voiceSaveSnapshot) {
        showToast('Wait for Voices to finish loading before previewing.', 'warning');
        return;
    }
    if (typeof isEditorEditPending === 'function' && isEditorEditPending()) {
        showToast('Resolve pending or failed Editor edits before previewing a voice change.', 'warning');
        return;
    }
    _singleVoiceBusy = true;
    // Capture before EVERY await, including the pre-flush: a book switch
    // during the flush must be detected by the rechecks below (#1724 C012).
    const identity = singleVoiceCaptureIdentity();
    const generation = _singleVoiceGeneration;
    const book = identity.book;
    const epoch = identity.epoch;
    let flushedPending = false;
    let preflushUnknown = false;
    try {
        // Preview may save the user's already-authorized pending card edits,
        // exactly as the existing Voices controls do, but it never
        // bulk-applies. The Voices copy states this truthfully (#1724 C126).
        // A rejected preflush may itself be past commit: preserve the detail
        // and classify it exactly like the bulk save below instead of
        // claiming no edits were applied.
        if (typeof voiceSaveQueue !== 'undefined' && voiceSaveQueue?.isDirty?.()) {
            try {
                await flushVoiceSaves();
                flushedPending = true;
            } catch (preError) {
                const detail = singleVoiceSaveErrorDetail(preError);
                if (singleVoiceIsDefiniteSaveRefusal(preError)) {
                    throw new Error(`Pending voice edits were refused by the server: ${detail} Your edits were retained; fix the cause in Voices and try again.`, {cause: preError});
                }
                const unknownPre = new Error(`Pending voice edits could not be confirmed: ${detail} They may have been saved. Check Voices state before trying again; your edits are retained.`, {cause: preError});
                unknownPre.unknownOutcome = true;
                preflushUnknown = true;
                throw unknownPre;
            }
        }
        if (!singleVoiceIdentityCurrent(identity) || generation !== _singleVoiceGeneration) {
            throw new Error('Active book changed while previewing. Nothing was applied; reload Voices and preview again.');
        }
        await refreshVoiceMetadata(singleVoiceRefreshOpts(identity));
        if (!singleVoiceIdentityCurrent(identity) || generation !== _singleVoiceGeneration) {
            throw new Error('Active book changed while previewing. Nothing was applied; reload Voices and preview again.');
        }
        const snapshot = _voiceSaveSnapshot;
        if (!snapshot) {
            throw new Error('Voices are still loading. Nothing was applied; reload Voices and preview again.');
        }
        const revisionBefore = voiceSaveQueue.getRevision();
        const preview = await API.post('/api/voice_config/single-voice-preview', {
            source_speaker: source, revision: snapshot.revision, book_token: snapshot.book_token},
            {timeoutMs: 60000, timeoutMessage: 'The single-voice preview timed out. Nothing was applied; preview again.'});
        if (!singleVoiceIdentityCurrent(identity) || generation !== _singleVoiceGeneration
            || _voiceSaveSnapshot.book_token !== preview.book_token || _voiceSaveSnapshot.revision !== preview.revision
            || (typeof voiceSaveQueue !== 'undefined' && voiceSaveQueue.getRevision() !== revisionBefore)) {
            throw new Error('Book, voices or selection changed while previewing. Nothing was applied; preview again.');
        }
        if (!Array.isArray(preview.speakers) || !preview.voices || typeof preview.voices !== 'object'
            || Array.isArray(preview.voices)
            || !/^[0-9a-f]{64}$/.test(preview.revision || '') || !/^[0-9a-f]{64}$/.test(preview.book_token || '')) {
            throw new Error('Invalid voice preview; nothing was applied.');
        }
        // Roster consistency: the voices map must cover exactly the previewed
        // speakers, otherwise the modal could call a changing speaker
        // "unchanged" (#1724 C032, C056).
        const voiceNames = Object.keys(preview.voices).sort();
        const speakerNames = preview.speakers.map(row => {
            if (!row || typeof row.name !== 'string' || typeof row.line_count !== 'number' || !Array.isArray(row.changes)) {
                throw new Error('Invalid voice preview; nothing was applied.');
            }
            return row.name;
        }).sort();
        if (JSON.stringify(voiceNames) !== JSON.stringify(speakerNames)) {
            throw new Error('Invalid voice preview; nothing was applied.');
        }
        if (preview.affected_speakers !== preview.speakers.length
            || preview.affected_lines !== preview.speakers.reduce((sum, row) => sum + row.line_count, 0)) {
            throw new Error('Invalid voice preview; nothing was applied.');
        }
        const lines = preview.speakers.map(row => `${row.name}: ${row.line_count} lines · `
            + (row.line_count === 0
                ? '0 lines — voice config will still be updated'
                : (row.changes.length ? row.changes.join(', ') : 'unchanged'))).join('\n');
        const confirmed = await showConfirm(
            `Use the ${preview.source_speaker} voice for all ${preview.affected_speakers} speakers `
            + `(${preview.affected_lines} lines)?\n${lines}\n\nReady approvals are cleared for every affected speaker on this apply. `
            + `Changed-voice lines become pending for re-render; already rendered take files are retained, not deleted. `
            + `Voice approvals are reset for speakers whose effective voice changes.`,
            {title: 'Use one voice for the book?', actionLabel: 'Use this voice for all speakers', danger: false});
        if (!confirmed) { return; }
        if (!singleVoiceIdentityCurrent(identity) || generation !== _singleVoiceGeneration) {
            throw new Error('Active book changed while confirming. Nothing was applied; preview again.');
        }
        await applySingleVoicePreview(preview, identity, source);
    } catch (error) {
        // If the pre-flush already persisted, say so instead of the false
        // "Nothing was applied" (#1724 C038, C006). A bulk save whose outcome
        // is unknown (timeout/lost response) must never claim "Nothing was
        // applied" either.
        const recovery = error?.savedApplied
            ? 'The bulk voice was saved, but Voices could not be refreshed. Reload Voices before trying again.'
            : (error?.unknownOutcome
                ? 'The voice save could not be confirmed: it may have been saved. Check Voices state before trying again; your edits are retained.'
                : (flushedPending
                    ? 'Your pending card edits were saved by this preview; the bulk voice was not applied. Check Voices state, then preview again.'
                    : 'Nothing was applied. Check the selected book, speaker and Voices state, then preview again. Unsaved voice edits are retained.'));
        showActionError('Single-voice preview failed', error, recovery);
    } finally {
        if (generation === _singleVoiceGeneration) { _singleVoiceBusy = false; }
        // refreshVoiceMetadata/loadVoices already repopulate the select; only
        // refresh here when the book is unchanged so a stale finally cannot
        // paint the new book's roster mid-switch (#1724 C015, C079). Never
        // call refreshVoiceMetadata while a retained draft is pending: it
        // flushes first and could retry an ambiguous write.
        if (singleVoiceIdentityCurrent(identity) && generation === _singleVoiceGeneration
                && (typeof voiceSaveQueue === 'undefined' || !voiceSaveQueue.isDirty())) { refreshSingleVoiceSpeakers(); }
    }
}

async function applySingleVoicePreview(preview, identity, source) {
    // Recheck everything after the modal. Newly arrived edits refuse here
    // without flushing them, so we never claim "nothing changed" falsely.
    if (!singleVoiceIdentityCurrent(identity)) {
        throw new Error('Active book changed. Nothing was applied.');
    }
    if (!_voiceSaveSnapshot || _voiceSaveSnapshot.book_token !== preview.book_token
        || _voiceSaveSnapshot.revision !== preview.revision) {
        throw new Error('Voice configuration changed after preview. Nothing was applied; preview again.');
    }
    if (typeof voiceSaveQueue !== 'undefined' && voiceSaveQueue.isDirty()) {
        throw new Error('New unsaved voice edits arrived while confirming. Nothing was applied; save or discard them in Voices first.');
    }
    const select = document.getElementById('single-voice-source');
    if ((select?.value || '') !== source || preview.source_speaker !== source) {
        throw new Error('Speaker selection changed. Nothing was applied; preview again.');
    }
    enqueueVoiceDraft(preview.voices);
    let savedApplied = false;
    try {
        await flushVoiceSaves();
        savedApplied = true;
    } catch (error) {
        // Keep the original server detail for diagnosis AND for the user
        // (#1724 C041). A rejected/lost flush is an UNKNOWN outcome unless
        // the server response confirmed persistence: never claim "nothing
        // applied", never call refreshVoiceMetadata as a "read" while the
        // retained draft is pending (it flushes first), and keep the draft
        // for canonical recovery. Preserve the actionable detail in the
        // displayed message, not only in Error.cause. Only a documented
        // 400/409/422 refusal is a definite no-write; transport/timeouts/5xx
        // stay unknown because a proxy/app error may follow a commit.
        const detail = singleVoiceSaveErrorDetail(error);
        if (singleVoiceIsDefiniteSaveRefusal(error)) {
            // The server answered with a validation/CAS refusal: definite.
            const refused = new Error(`Voice save refused by the server: ${detail} Your edits were retained; fix the cause in Voices and try again.`, {cause: error});
            refused.savedApplied = false;
            refused.unknownOutcome = false;
            throw refused;
        }
        const unknown = new Error(`Voice save could not be confirmed: ${detail} It may have been saved. Check Voices state before trying again; your edits are retained.`, {cause: error});
        unknown.savedApplied = false;
        unknown.unknownOutcome = true;
        throw unknown;
    }
    if (!singleVoiceIdentityCurrent(identity)) {
        const persistedStale = new Error('The voice was saved, but the active book changed before Voices could be refreshed. Reload Voices for the current book to see the saved state.', {cause: null});
        persistedStale.savedApplied = true;
        throw persistedStale;
    }
    try {
        // Rerender the actual cards through loadVoices (not just metadata):
        // Ready checkboxes/counters must reflect cleared approvals and a
        // later ordinary card edit must read the new DOM, not revert it.
        await loadVoices(true, singleVoiceRefreshOpts(identity));
    } catch (error) {
        // ANY post-save render/refresh error is a persisted save, with the
        // same identity or a changed one: never fall through with a bare
        // error that the outer catch would misreport as not applied.
        const persisted = new Error('The voice was saved, but Voices could not be refreshed. Reload Voices to see the applied state before trying again.', {cause: error});
        persisted.savedApplied = true;
        throw persisted;
    }
    showToast('One voice applied book-wide. Play a sample in Editor and mark Ready in Voices only after listening.', 'success');
}

// A literal-mode change invalidates any earlier source confirmation.
document.getElementById('script-literal-source-preserving')?.addEventListener('change', () => {
    if (_singleVoiceSourceBinding) {
        _singleVoiceSourceBinding = null;
        renderSingleVoiceSourcePreview();
    }
});
