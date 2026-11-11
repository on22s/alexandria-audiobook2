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

// One source-binding equality rule for confirmation and reload checks.
function singleVoiceBindingsEqual(first, second) {
    return !!first && !!second
        && first.book === second.book
        && first.epoch === second.epoch
        && first.token === second.token
        && first.sha === second.sha
        && first.literal === second.literal;
}

function isSingleVoiceSourceConfirmed() {
    if (!_singleVoiceSourceBinding || !_singleVoiceSourcePreview) { return false; }
    const current = getSingleVoiceSourceBinding(_singleVoiceSourcePreview);
    return singleVoiceBindingsEqual(_singleVoiceSourceBinding, current);
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
    if (identity.identityToken) {
        // The selected upload is authoritative when present: a stale
        // rendered voice snapshot from a previous book (A) must not veto a
        // valid source preview for the newly selected book (B) — the
        // upload handler sets selection B without refreshing voice
        // metadata, and the source guide runs before Voices. Only the
        // saved-script fallback (no selection) is fenced by the snapshot.
        const currentSnapshotToken = (typeof _voiceSaveSnapshot !== 'undefined' && _voiceSaveSnapshot)
            ? (_voiceSaveSnapshot.book_identity_token || null) : null;
        if (!identity.selection && currentSnapshotToken && currentSnapshotToken !== identity.identityToken) { return false; }
        if (identity.selection && window._selectedUploadIdentity
                && window._selectedUploadIdentity.token !== identity.identityToken) { return false; }
    }
    return true;
}

// Explicit unavailable selection ({token:null, unavailable:true}): the
// book identity could not be verified at load, so Reload Voices cannot
// resolve it. Recovery is an explicit reload/reselect of the book
// itself (Scripts load or re-upload), never a snapshot fallback.
function singleVoiceIdentityUnavailable(identity) {
    return !!(identity && identity.selection && identity.selection.unavailable);
}

function singleVoiceUnavailableBookError() {
    return new Error(t('single_voice.book-unavailable', 'The selected book identity is unavailable. Reload or reselect this book (load it again from Scripts, or re-upload its source) before previewing again; Reload Voices cannot resolve an unavailable book identity.'));
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
// detail is always kept in the displayed message. The one status rule
// lives in app-core (isDefiniteVoiceSaveRefusal, loaded first); this
// alias keeps the single-voice call sites readable with no second copy.
function singleVoiceIsDefiniteSaveRefusal(error) {
    return isDefiniteVoiceSaveRefusal(error);
}

// Owned pre-send refusal (canonical queue write guard): the write was
// provably NOT sent (book changed before send). It is neither a server
// refusal nor an unknown outcome — HTTP 5xx errors never carry this
// marker, so this classification cannot misclassify them, and a prior
// ambiguous outcome stays sticky in the queue (this never clears flags).
function singleVoiceIsNotSent(error) {
    return error?.voiceSaveNotSent === true;
}

function singleVoiceSaveErrorDetail(error) {
    return (error && (error.message || error.detail)) || t('single_voice.save.failed', 'Voice save failed.');
}

function renderSingleVoiceSourcePreview() {
    const box = document.getElementById('source-preview-text');
    const meta = document.getElementById('source-preview-meta');
    const confirmBox = document.getElementById('source-preview-confirm');
    if (!box || !meta) { return; }
    if (!_singleVoiceSourcePreview) {
        box.textContent = t('single_voice.source.empty', 'No source preview loaded for this book.');
        meta.textContent = '';
        if (confirmBox) { confirmBox.checked = false; }
        const emptyStatus = document.getElementById('source-preview-status');
        if (emptyStatus) { emptyStatus.textContent = t('single_voice.source.empty-status', 'No source preview loaded.'); }
        return;
    }
    const preview = _singleVoiceSourcePreview;
    box.textContent = preview.text_start || '';
    // total_chars counts Unicode code points (Python len); count code points
    // here too, not UTF-16 code units (#1724 C010).
    const excerptChars = [...(preview.text_start || '')].length;
    // DS3007: distinguish the imported document from the retained
    // extracted text actually shown, and label the digest as the
    // extracted-text digest it is. Filename labels are display-escaped
    // (control/bidi) without changing raw stored/returned strings.
    const identityLabel = preview.imported_source_name
        ? t('single_voice.source.identity.imported', 'imported document {imported} · retained extracted text {retained}', {
            imported: singleVoiceEscapeLabel(preview.imported_source_name),
            retained: singleVoiceEscapeLabel(preview.source_name)})
        : t('single_voice.source.identity.retained', 'retained extracted text {name}', {name: singleVoiceEscapeLabel(preview.source_name)});
    meta.textContent = preview.truncated
        ? t('single_voice.source.meta-truncated', '{identity} · encoding {encoding} · original extracted text (before annotation/preparation) · {total} chars total · excerpt {shown} shown (truncated) · extracted-text sha256 {sha}… · book {token}…', {
            identity: identityLabel,
            encoding: preview.encoding,
            total: preview.total_chars,
            shown: excerptChars,
            sha: preview.source_sha256.slice(0, 16),
            token: (preview.book_token || '').slice(0, 12)})
        : t('single_voice.source.meta', '{identity} · encoding {encoding} · original extracted text (before annotation/preparation) · {total} chars total · excerpt {shown} shown · extracted-text sha256 {sha}… · book {token}…', {
            identity: identityLabel,
            encoding: preview.encoding,
            total: preview.total_chars,
            shown: excerptChars,
            sha: preview.source_sha256.slice(0, 16),
            token: (preview.book_token || '').slice(0, 12)});
    const confirmed = isSingleVoiceSourceConfirmed();
    if (confirmBox) { confirmBox.checked = confirmed; }
    const status = document.getElementById('source-preview-status');
    if (status) { status.textContent = confirmed
        ? t('single_voice.source.confirmed', 'Loaded excerpt snapshot confirmed in this tab. Reload the preview to check the current source.')
        : t('single_voice.source.unconfirmed', 'Loaded excerpt snapshot not confirmed for the current book/source.'); }
}

async function loadSingleVoiceSourcePreview() {
    if (_singleVoiceBusy) {
        showToast(t('single_voice.busy', 'A single-voice action is still running; please wait.'), 'warning');
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
            {timeoutMs: 60000, timeoutMessage: t('single_voice.source.timeout', 'The source preview timed out. Check the selected book and try again.')});
        if (!singleVoiceIdentityCurrent(identity)) { return; }
        if (!preview || typeof preview.text_start !== 'string' || !/^[0-9a-f]{64}$/.test(preview.book_token || '')
            || !/^[0-9a-f]{64}$/.test(preview.source_sha256 || '')) {
            throw new Error(t('single_voice.source.invalid', 'Invalid source preview; your confirmation was not recorded.'));
        }
        // Fence BEFORE installing: the server's operation token must equal
        // the identity this tab captured. Never compare to the voice
        // snapshot book_token (a different digest). A missing captured token
        // fails closed: the book identity is still loading.
        if (!identity.identityToken) {
            if (singleVoiceIdentityUnavailable(identity)) { throw singleVoiceUnavailableBookError(); }
            throw new Error(t('single_voice.source.voices-loading', 'Voices are still loading for this book. Wait for Voices to finish, then reload the source preview.'));
        }
        if (preview.book_token !== identity.identityToken) {
            throw new Error(t('single_voice.source.book-changed', 'Active book changed while loading the source preview. Nothing was confirmed; reload the preview for the current book.'));
        }
        if (!preview.text_start.trim()) {
            throw new Error(t('single_voice.source.blank', 'Source excerpt is blank; import a non-blank book before confirming.'));
        }
        _singleVoiceSourcePreview = preview;
        if (_singleVoiceSourceBinding) {
            const current = getSingleVoiceSourceBinding(preview);
            if (!singleVoiceBindingsEqual(_singleVoiceSourceBinding, current)) {
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
        showActionError(t('single_voice.source.failed', 'Source preview failed'), error, t('single_voice.source.failed-recovery', 'Inspect the extracted text in Script instead. Check the selected book and source first.'));
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
        showToast(t('single_voice.confirm-first', 'Load the source preview before confirming it.'), 'warning');
        return;
    }
    _singleVoiceSourceBinding = getSingleVoiceSourceBinding(_singleVoiceSourcePreview);
    renderSingleVoiceSourcePreview();
}

function makeSingleVoicePlaceholder(select) {
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = t('single_voice.placeholder', 'Choose a speaker whose voice you picked…');
    select.appendChild(placeholder);
}

window.resetSingleVoiceBook = function resetSingleVoiceBook() {
    _singleVoiceGeneration += 1;
    _singleVoiceBusy = false;
    window._singleVoiceApplying = false;
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
        showToast(t('single_voice.speaker-gone', "Speaker '{name}' is no longer in this book's roster; choose a speaker again.", {name: current}), 'warning');
    }
}

// Show script-supplied speaker labels unambiguously in plain-text modals
// without changing stored names: control/newline characters become visible
// escapes so one name cannot masquerade as several lines.
function singleVoiceEscapeLabel(name) {
    // Display-only escaping of invisible control/format characters using
    // the platform Unicode data (standard Cc/Cf property escapes plus the
    // line/paragraph separators), never a new hand-enumerated table:
    // every invisible format control — BMP and astral — becomes an
    // explicit code-point escape, so one label cannot masquerade as
    // another. Stored names/asset identities are unchanged; legitimate
    // Unicode (letters, marks, emoji) is never normalized or altered.
    return String(name).replace(/[\p{Cc}\p{Cf}\u2028\u2029]/gu, char => {
        if (char === '\n') { return '\\n'; }
        if (char === '\r') { return '\\r'; }
        if (char === '\t') { return '\\t'; }
        return '\\u{' + char.codePointAt(0).toString(16) + '}';
    });
}

// Minimal escaped identity of the actual asset being copied, from the
// server-authoritative preview patch for the source speaker. No new
// availability/path qualification: this only shows what consent copies.
function singleVoiceSourceAssetLabel(preview) {
    const asset = preview && preview.voices ? preview.voices[preview.source_speaker] : null;
    if (!asset || typeof asset !== 'object') { return t('single_voice.asset.unknown', 'unknown voice asset'); }
    const type = asset.type || 'custom';
    if (type === 'clone') {
        return t('single_voice.asset.clone', 'clone reference {ref}', {ref: singleVoiceEscapeLabel(asset.ref_audio || '')});
    }
    if (type === 'lora' || type === 'builtin_lora') {
        if (asset.adapter_generation_sha256) {
            return t('single_voice.asset.lora-pinned', 'LoRA adapter {adapter} path {path} pin {sha}…', {
                adapter: singleVoiceEscapeLabel(asset.adapter_id || ''),
                path: singleVoiceEscapeLabel(asset.adapter_path || ''),
                sha: singleVoiceEscapeLabel(String(asset.adapter_generation_sha256).slice(0, 12))});
        }
        return t('single_voice.asset.lora', 'LoRA adapter {adapter} path {path}', {
            adapter: singleVoiceEscapeLabel(asset.adapter_id || ''),
            path: singleVoiceEscapeLabel(asset.adapter_path || '')});
    }
    if (type === 'custom') {
        return t('single_voice.asset.custom', 'custom voice {voice}', {voice: singleVoiceEscapeLabel(asset.voice || '')});
    }
    return t('single_voice.asset.typed', '{type} voice {voice}', {type: singleVoiceEscapeLabel(type), voice: singleVoiceEscapeLabel(asset.voice || '')});
}

async function previewSingleVoiceBook() {
    if (_singleVoiceBusy) {
        showToast(t('single_voice.busy', 'A single-voice action is still running; please wait.'), 'warning');
        return;
    }
    const select = document.getElementById('single-voice-source');
    const source = select?.value || '';
    if (!source) {
        showToast(t('single_voice.preview.choose-first', 'Choose a speaker whose voice you picked on its card first.'), 'warning');
        return;
    }
    if (typeof _voiceSaveSnapshot === 'undefined' || !_voiceSaveSnapshot) {
        showToast(t('single_voice.preview.wait-voices', 'Wait for Voices to finish loading before previewing.'), 'warning');
        return;
    }
    if (typeof isEditorEditPending === 'function' && isEditorEditPending()) {
        showToast(t('single_voice.preview.resolve-edits', 'Resolve pending or failed Editor edits before previewing a voice change.'), 'warning');
        return;
    }
    _singleVoiceBusy = true;
    // Capture before EVERY await, including the pre-flush: a book switch
    // during the flush must be detected by the rechecks below (#1724 C012).
    const identity = singleVoiceCaptureIdentity();
    const generation = _singleVoiceGeneration;
    let flushedPending = false;
    try {
        if (singleVoiceIdentityUnavailable(identity)) { throw singleVoiceUnavailableBookError(); }
        // Preview may save the user's already-authorized pending card edits,
        // exactly as the existing Voices controls do, but it never
        // bulk-applies. The Voices copy states this truthfully (#1724 C126).
        // A rejected preflush may itself be past commit: preserve the detail
        // and classify it exactly like the bulk save below instead of
        // claiming no edits were applied.
        if (typeof voiceSaveQueue !== 'undefined' && voiceSaveQueue?.isDirty?.()) {
            try {
                // The canonical flush refuses an ambiguous outcome
                // (DS3014) without sending; the catch below classifies
                // it as unknown with an explicit-recovery message.
                await flushVoiceSaves();
                flushedPending = true;
            } catch (preError) {
                const detail = singleVoiceSaveErrorDetail(preError);
                if (singleVoiceIsNotSent(preError)) {
                    const notSentPre = new Error(formatUiSafetyText('safety.single_voice.pending.not-sent', 'Pending voice edits were not sent: {detail} Nothing was sent; your edits were retained.', {detail}), {cause: preError});
                    notSentPre.notSentOutcome = true;
                    notSentPre.unknownOutcome = false;
                    throw notSentPre;
                }
                if (singleVoiceIsDefiniteSaveRefusal(preError)) {
                    throw new Error(formatUiSafetyText('safety.single_voice.pending.refused', 'Pending voice edits were refused by the server: {detail} Your edits were retained; fix the cause in Voices and try again.', {detail}), {cause: preError});
                }
                const unknownPre = new Error(formatUiSafetyText('safety.single_voice.pending.unknown', 'Pending voice edits could not be confirmed: {detail} They may have been saved. Check Voices state before trying again; your edits are retained.', {detail}), {cause: preError});
                unknownPre.unknownOutcome = true;
                throw unknownPre;
            }
        }
        if (!singleVoiceIdentityCurrent(identity)) {
            throw new Error(t('single_voice.preview.book-changed', 'Active book changed while previewing. Nothing was applied; reload Voices and preview again.'));
        }
        await refreshVoiceMetadata(singleVoiceRefreshOpts(identity));
        if (!singleVoiceIdentityCurrent(identity)) {
            throw new Error(t('single_voice.preview.book-changed', 'Active book changed while previewing. Nothing was applied; reload Voices and preview again.'));
        }
        const snapshot = _voiceSaveSnapshot;
        if (!snapshot) {
            throw new Error(t('single_voice.preview.voices-loading', 'Voices are still loading. Nothing was applied; reload Voices and preview again.'));
        }
        const revisionBefore = voiceSaveQueue.getRevision();
        const preview = await API.post('/api/voice_config/single-voice-preview', {
            source_speaker: source, revision: snapshot.revision, book_token: snapshot.book_token},
            {timeoutMs: 60000, timeoutMessage: t('single_voice.preview.timeout', 'The single-voice preview timed out. Nothing was applied; preview again.')});
        if (!singleVoiceIdentityCurrent(identity)
            || _voiceSaveSnapshot.book_token !== preview.book_token || _voiceSaveSnapshot.revision !== preview.revision
            || (typeof voiceSaveQueue !== 'undefined' && voiceSaveQueue.getRevision() !== revisionBefore)) {
            throw new Error(t('single_voice.preview.changed', 'Book, voices or selection changed while previewing. Nothing was applied; preview again.'));
        }
        if (!Array.isArray(preview.speakers) || !preview.voices || typeof preview.voices !== 'object'
            || Array.isArray(preview.voices)
            || !/^[0-9a-f]{64}$/.test(preview.revision || '') || !/^[0-9a-f]{64}$/.test(preview.book_token || '')) {
            throw new Error(t('single_voice.preview.invalid', 'Invalid voice preview; nothing was applied.'));
        }
        // Fail closed on an unbound preview source: the echoed source
        // must be the one captured before awaits and an OWN key of the
        // voices map (an inherited name is not a server proposal).
        if (preview.source_speaker !== source || !Object.hasOwn(preview.voices, source)) {
            throw new Error(t('single_voice.preview.invalid', 'Invalid voice preview; nothing was applied.'));
        }
        // Roster consistency: the voices map must cover exactly the previewed
        // speakers, otherwise the modal could call a changing speaker
        // "unchanged" (#1724 C032, C056).
        const voiceNames = Object.keys(preview.voices).sort();
        const speakerNames = preview.speakers.map(row => {
            if (!row || typeof row.name !== 'string' || typeof row.line_count !== 'number' || !Array.isArray(row.changes)) {
                throw new Error(t('single_voice.preview.invalid', 'Invalid voice preview; nothing was applied.'));
            }
            return row.name;
        }).sort();
        if (JSON.stringify(voiceNames) !== JSON.stringify(speakerNames)) {
            throw new Error(t('single_voice.preview.invalid', 'Invalid voice preview; nothing was applied.'));
        }
        if (preview.affected_speakers !== preview.speakers.length
            || preview.affected_lines !== preview.speakers.reduce((sum, row) => sum + row.line_count, 0)) {
            throw new Error(t('single_voice.preview.invalid', 'Invalid voice preview; nothing was applied.'));
        }
        const lines = preview.speakers.map(row => t('single_voice.preview.line', '{speaker}: {count} lines · {state}', {
            speaker: singleVoiceEscapeLabel(row.name),
            count: row.line_count,
            state: row.line_count === 0
                ? t('single_voice.preview.line-zero', '0 lines — voice config will still be updated')
                : (row.changes.length ? row.changes.join(', ') : t('single_voice.preview.line-unchanged', 'unchanged')),
        })).join('\n');
        const confirmBody = formatUiSafetyText('safety.single_voice.apply-confirm', 'Use the {speaker} voice ({asset}) for all {speakers} speakers ({lines} lines)?\n{detail}\n\nReady approvals are cleared for every affected speaker on this apply. Changed-voice lines become pending for re-render; already rendered take files are retained, not deleted. Voice approvals are reset for speakers whose effective voice changes.', {
            speaker: singleVoiceEscapeLabel(preview.source_speaker),
            asset: singleVoiceSourceAssetLabel(preview),
            speakers: preview.affected_speakers,
            lines: preview.affected_lines,
            detail: lines});
        const confirmed = await showConfirm(
            confirmBody,
            {title: t('safety.single_voice.apply-confirm.title', 'Use one voice for the book?'),
             actionLabel: t('safety.single_voice.apply-confirm.action', 'Use this voice for all speakers'), danger: false});
        if (!confirmed) { return; }
        if (!singleVoiceIdentityCurrent(identity)) {
            throw new Error(t('single_voice.preview.confirm-changed', 'Active book changed while confirming. Nothing was applied; preview again.'));
        }
        await applySingleVoicePreview(preview, identity, source);
    } catch (error) {
        // DS3013: a stale rejection after a book switch (reset bumps
        // the generation) must not toast over the new book's state,
        // mirroring loadSingleVoiceSourcePreview's guard.
        if (generation !== _singleVoiceGeneration) { return; }
        // If the pre-flush already persisted, say so instead of the false
        // "Nothing was applied" (#1724 C038, C006). A bulk save whose outcome
        // is unknown (timeout/lost response) must never claim "Nothing was
        // applied" either.
        const recovery = error?.savedApplied
            ? formatUiSafetyText('safety.single_voice.recovery.saved-applied', 'The bulk voice was saved, but Voices could not be refreshed. Reload Voices before trying again.')
            : (error?.unknownOutcome
                ? formatUiSafetyText('safety.single_voice.recovery.unknown', 'The voice save could not be confirmed: it may have been saved. Check Voices state before trying again; your edits are retained.')
                : (flushedPending
                    ? formatUiSafetyText('safety.single_voice.recovery.flushed', 'Your pending card edits were saved by this preview; the bulk voice was not applied. Check Voices state, then preview again.')
                    : formatUiSafetyText('safety.single_voice.recovery.nothing', 'Nothing was applied. Check the selected book, speaker and Voices state, then preview again. Unsaved voice edits are retained.')));
        showActionError(t('single_voice.preview.failed', 'Single-voice preview failed'), error, recovery);
    } finally {
        if (generation === _singleVoiceGeneration) { _singleVoiceBusy = false; }
        // refreshVoiceMetadata/loadVoices already repopulate the select; only
        // refresh here when the book is unchanged so a stale finally cannot
        // paint the new book's roster mid-switch (#1724 C015, C079). Never
        // call refreshVoiceMetadata while a retained draft is pending: it
        // flushes first and could retry an ambiguous write.
        if (singleVoiceIdentityCurrent(identity)
                && (typeof voiceSaveQueue === 'undefined' || !voiceSaveQueue.isDirty())) { refreshSingleVoiceSpeakers(); }
    }
}

async function applySingleVoicePreview(preview, identity, source) {
    // Recheck everything after the modal. Newly arrived edits refuse here
    // without flushing them, so we never claim "nothing changed" falsely.
    if (!singleVoiceIdentityCurrent(identity)) {
        throw new Error(t('single_voice.preview.apply-book-changed', 'Active book changed. Nothing was applied.'));
    }
    if (!_voiceSaveSnapshot || _voiceSaveSnapshot.book_token !== preview.book_token
        || _voiceSaveSnapshot.revision !== preview.revision) {
        throw new Error(t('single_voice.preview.config-changed', 'Voice configuration changed after preview. Nothing was applied; preview again.'));
    }
    if (typeof voiceSaveQueue !== 'undefined' && voiceSaveQueue.isDirty()) {
        throw new Error(t('single_voice.preview.unsaved-edits', 'New unsaved voice edits arrived while confirming. Nothing was applied; save or discard them in Voices first.'));
    }
    const select = document.getElementById('single-voice-source');
    if ((select?.value || '') !== source || preview.source_speaker !== source) {
        throw new Error(t('single_voice.preview.speaker-changed', 'Speaker selection changed. Nothing was applied; preview again.'));
    }
    // Mass-save transition: while the bulk flush and post-save reload are
    // pending, the old card DOM is still visible but the CAS revision is
    // already fresh. An ordinary edit collected from that stale DOM would
    // re-save pre-bulk voices over the confirmed change. Transiently
    // disable mutating controls inside #voices-list and fence
    // saveVoicesDebounced via window._singleVoiceApplying; canonical
    // recovery (outside the list) stays usable. Released only by this
    // owning apply, on success and on every failure path.
    // Preserve each owned control's original disabled state (stale/card/
    // capability policies may already disable some). Only these captured
    // elements are restored, and only by the owning same-generation apply:
    // fresh cards from a successful rerender are new elements and are
    // never blanket-enabled, and a new-book list is never touched.
    const voicesList = document.getElementById('voices-list');
    const ownedControls = [...((voicesList && typeof voicesList.querySelectorAll === 'function')
        ? voicesList.querySelectorAll('button,input,select,textarea') : [])]
        .map(control => ({control, wasDisabled: !!control.disabled}));
    const applyOwner = {generation: identity.generation, controls: ownedControls, stale: false};
    window._singleVoiceApplying = applyOwner;
    ownedControls.forEach(({control}) => { control.disabled = true; });
    let refreshedAfterSave = false;
    let persistedOrUnknown = false;
    try {
    enqueueVoiceDraft(preview.voices);
    try {
        await flushVoiceSaves();
        persistedOrUnknown = true;
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
        if (singleVoiceIsNotSent(error)) {
            // Provably unsent: never claim a server refusal or an
            // unknown/maybe-saved outcome, and never mark persisted.
            const notSent = new Error(formatUiSafetyText('safety.single_voice.save.not-sent', 'Voice save was not sent: {detail} Nothing was sent; your edits were retained.', {detail}), {cause: error});
            notSent.savedApplied = false;
            notSent.unknownOutcome = false;
            notSent.notSentOutcome = true;
            throw notSent;
        }
        if (singleVoiceIsDefiniteSaveRefusal(error)) {
            // The server answered with a validation/CAS refusal: definite.
            const refused = new Error(formatUiSafetyText('safety.single_voice.save.refused', 'Voice save refused by the server: {detail} Your edits were retained; fix the cause in Voices and try again.', {detail}), {cause: error});
            refused.savedApplied = false;
            refused.unknownOutcome = false;
            throw refused;
        }
        const unknown = new Error(formatUiSafetyText('safety.single_voice.save.unknown', 'Voice save could not be confirmed: {detail} It may have been saved. Check Voices state before trying again; your edits are retained.', {detail}), {cause: error});
        unknown.savedApplied = false;
        unknown.unknownOutcome = true;
        persistedOrUnknown = true;
        throw unknown;
    }
    if (!singleVoiceIdentityCurrent(identity)) {
        const persistedStale = new Error(formatUiSafetyText('safety.single_voice.saved-book-changed', 'The voice was saved, but the active book changed before Voices could be refreshed. Reload Voices for the current book to see the saved state.'), {cause: null});
        persistedStale.savedApplied = true;
        throw persistedStale;
    }
    let loadReport = null;
    try {
        // Rerender the actual cards through loadVoices (not just metadata):
        // Ready checkboxes/counters must reflect cleared approvals and a
        // later ordinary card edit must read the new DOM, not revert it.
        // loadVoices(false) still reloads metadata and re-renders when the
        // revision changed (it does after this save), while reusing fresh
        // catalogs instead of forcing a full resource refetch (CR2078).
        loadReport = await loadVoices(false, singleVoiceRefreshOpts(identity));
        refreshedAfterSave = true;
    } catch (error) {
        // ANY post-save render/refresh error is a persisted save, with the
        // same identity or a changed one: never fall through with a bare
        // error that the outer catch would misreport as not applied.
        const persisted = new Error(formatUiSafetyText('safety.single_voice.saved-unrefreshed', 'The voice was saved, but Voices could not be refreshed. Reload Voices to see the applied state before trying again.'), {cause: error});
        persisted.savedApplied = true;
        throw persisted;
    }
    showToast(t('single_voice.applied', 'One voice applied book-wide. Play a sample in Editor and mark Ready in Voices only after listening.'), 'success');
    const failedResources = loadReport?.failedResources || [];
    if (failedResources.length) {
        // CR2024: a failed optional catalog refresh is not an exception and
        // must not be silent. The save DID succeed (no false no-write): warn
        // visibly with plain catalog labels (no raw endpoint paths in the
        // normal guide flow); previously loaded lists remain shown.
        const labels = {
            '/api/voice_design/list': t('single_voice.catalog.designed-voices', 'designed voices'),
            '/api/clone_voices/list': t('single_voice.catalog.clone-voices', 'cloned voices'),
            '/api/lora/models': t('single_voice.catalog.lora-models', 'LoRA models'),
            '/api/voice_library': t('single_voice.catalog.cast-library', 'cast library'),
        };
        const names = failedResources.map(path => labels[path] || t('single_voice.catalog.other', 'voice catalogs'));
        console.debug('single-voice catalog refresh failed', failedResources);
        showToast(formatUiSafetyText('safety.single_voice.catalogs', 'Voice saved, but these catalogs could not refresh: {names}. Previously loaded lists are kept. Reload Voices to retry.', {names: names.join(', ')}), 'warning');
    }
    } finally {
        // Release only our own ownership: a book switch (resetSingleVoiceBook)
        // already cleared the flag, and a newer apply owns a different
        // object — neither must be disturbed by this stale finally.
        // DS3075: when a confirmed/possibly-committed save was NOT
        // followed by a successful canonical reload/rerender, leave a
        // recoverable stale-card fence in place (controls stay disabled,
        // card saves stay blocked) until loadVoices successfully
        // rerenders CURRENT cards and releases it. A definite refusal
        // never committed, so it releases normally below.
        if (window._singleVoiceApplying === applyOwner) {
            if (persistedOrUnknown && !refreshedAfterSave) {
                applyOwner.stale = true;
            } else {
                window._singleVoiceApplying = false;
                if (identity.generation === _singleVoiceGeneration) {
                    applyOwner.controls.forEach(({control, wasDisabled}) => { control.disabled = wasDisabled; });
                }
            }
        }
    }
}

// A literal-mode change invalidates any earlier source confirmation.
document.getElementById('script-literal-source-preserving')?.addEventListener('change', () => {
    if (_singleVoiceSourceBinding) {
        _singleVoiceSourceBinding = null;
        renderSingleVoiceSourcePreview();
    }
});
