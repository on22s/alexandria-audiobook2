/* Single-narrator checklist support (#1724): read-only source confirmation
   plus the compact Voices single-voice action. Navigation stays in
   app-guides.js; sample/render/export/readiness stay on existing controls.
   This file never starts generation, never writes the literal checkbox,
   and never plays or approves audio. */
let _singleVoiceSourcePreview = null;
let _singleVoiceSourceBinding = null;
let _singleVoiceBusy = false;

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
        + (preview.truncated ? ' (truncated)' : '') + ` · sha256 ${preview.source_sha256.slice(0, 16)}…`;
    const confirmed = isSingleVoiceSourceConfirmed();
    if (confirmBox) { confirmBox.checked = confirmed; }
    const status = document.getElementById('source-preview-status');
    if (status) { status.textContent = confirmed
        ? 'Loaded excerpt snapshot confirmed in this tab. Reload the preview to check the current source.'
        : 'Loaded excerpt snapshot not confirmed for the current book/source.'; }
}

async function loadSingleVoiceSourcePreview() {
    if (_singleVoiceBusy) {
        showToast('Source preview is still loading; please wait.', 'warning');
        return;
    }
    // Capture book+epoch before the await: a book switch while the GET is
    // pending must not install the old book's preview (or let a later
    // confirmation bind it) into the new book. Epoch also catches A->B->A.
    const book = currentBookFilename;
    const epoch = window._bookSelectionEpoch;
    _singleVoiceBusy = true;
    // API.get has no deadline of its own; race a local one so a hung server
    // cannot wedge the busy flag forever. A late response after the race
    // settles is ignored by the await itself (#1724 C048).
    let timer = null;
    const deadline = new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(new Error('The source preview timed out. Check the selected book and try again.')), 60000);
    });
    try {
        const preview = await Promise.race([API.get('/api/single-narrator/source-preview'), deadline]);
        if (!singleVoiceBookEpochCurrent(book, epoch)) { return; }
        if (!preview || typeof preview.text_start !== 'string' || !/^[0-9a-f]{64}$/.test(preview.book_token || '')
            || !/^[0-9a-f]{64}$/.test(preview.source_sha256 || '')) {
            throw new Error('Invalid source preview; your confirmation was not recorded.');
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
        _singleVoiceSourcePreview = null;
        _singleVoiceSourceBinding = null;
        renderSingleVoiceSourcePreview();
        showActionError('Source preview failed', error, 'Inspect the extracted text in Script instead. Check the selected book and source first.');
    } finally {
        if (timer) { clearTimeout(timer); }
        _singleVoiceBusy = false;
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
    _singleVoiceSourcePreview = null;
    _singleVoiceSourceBinding = null;
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
    if (current && restored) { select.value = current; }
    else if (current) {
        // A previously selected speaker left the roster: say so instead of
        // silently resetting the select to the placeholder (#1724 C052).
        showToast(`Speaker '${current}' is no longer in this book's roster; choose a speaker again.`, 'warning');
    }
}

function singleVoiceAllMatchSingleAsset(snapshot) {
    const voices = snapshot?.voices || [];
    if (!voices.length) { return null; }
    // Completeness via the server's canonical descriptor only: single_voice_asset
    // is _direct_source_asset(config) computed by get_voice_rows, so a valid
    // custom without persona text is eligible and an empty clone/LoRA is not.
    // persona_pending is persona metadata, never asset completeness (#1724 C000/C001).
    // Missing/invalid descriptors fail closed; a client-side completeness
    // policy would drift from the server helper.
    const expectedKeys = ['adapter_id', 'adapter_path', 'members', 'ref_audio', 'ref_text', 'type', 'voice'];
    const validDescriptor = asset => asset && typeof asset === 'object' && !Array.isArray(asset)
        && expectedKeys.every(key => Object.hasOwn(asset, key))
        && ['custom', 'clone', 'lora', 'builtin_lora'].includes(asset.type);
    if (voices.some(voice => !validDescriptor(voice?.single_voice_asset))) { return null; }
    const first = JSON.stringify(voices[0].single_voice_asset);
    if (voices.some(voice => JSON.stringify(voice.single_voice_asset) !== first)) { return null; }
    return voices[0].single_voice_asset;
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
    const book = currentBookFilename;
    const epoch = window._bookSelectionEpoch;
    let flushedPending = false;
    try {
        // Preview may save the user's already-authorized pending card edits,
        // exactly as the existing Voices controls do, but it never
        // bulk-applies. The Voices copy states this truthfully (#1724 C126).
        if (typeof voiceSaveQueue !== 'undefined' && voiceSaveQueue?.isDirty?.()) {
            await flushVoiceSaves();
            flushedPending = true;
        }
        await refreshVoiceMetadata();
        if (!singleVoiceBookEpochCurrent(book, epoch)) {
            throw new Error('Active book changed while previewing. Nothing was applied; reload Voices and preview again.');
        }
        const snapshot = _voiceSaveSnapshot;
        const revisionBefore = voiceSaveQueue.getRevision();
        const preview = await API.post('/api/voice_config/single-voice-preview', {
            source_speaker: source, revision: snapshot.revision, book_token: snapshot.book_token},
            {timeoutMs: 60000, timeoutMessage: 'The single-voice preview timed out. Nothing was applied; preview again.'});
        if (!singleVoiceBookEpochCurrent(book, epoch)
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
        const lines = preview.speakers.map(row => `${row.name}: ${row.line_count} lines · `
            + (row.line_count === 0
                ? '0 lines — voice config will still be updated'
                : (row.changes.length ? row.changes.join(', ') : 'unchanged'))).join('\n');
        const confirmed = await showConfirm(
            `Use the ${preview.source_speaker} voice for all ${preview.affected_speakers} speakers `
            + `(${preview.affected_lines} lines)?\n${lines}\n\nReady approvals are cleared for every affected speaker on this apply. Audio is kept.`,
            {title: 'Use one voice for the book?', actionLabel: 'Use this voice for all speakers', danger: false});
        if (!confirmed) { return; }
        await applySingleVoicePreview(preview, book, epoch, source);
    } catch (error) {
        // If the pre-flush or the bulk save already persisted, say so
        // instead of the false "Nothing was applied" (#1724 C038, C006).
        const recovery = error?.savedApplied
            ? 'The bulk voice was saved, but Voices could not be refreshed. Reload Voices before trying again.'
            : (flushedPending
                ? 'Your pending card edits were saved by this preview; the bulk voice was not applied. Check Voices state, then preview again.'
                : 'Nothing was applied. Check the selected book, speaker and Voices state, then preview again. Unsaved voice edits are retained.');
        showActionError('Single-voice preview failed', error, recovery);
    } finally {
        _singleVoiceBusy = false;
        // refreshVoiceMetadata already repopulates the select; only refresh
        // here when the book is unchanged so a stale finally cannot paint
        // the new book's roster mid-switch (#1724 C015, C079).
        if (singleVoiceBookEpochCurrent(book, epoch)) { refreshSingleVoiceSpeakers(); }
    }
}

async function applySingleVoicePreview(preview, book, epoch, source) {
    // Recheck everything after the modal. Newly arrived edits refuse here
    // without flushing them, so we never claim "nothing changed" falsely.
    if (!singleVoiceBookEpochCurrent(book, epoch)) {
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
        await refreshVoiceMetadata();
    } catch (error) {
        // Keep the original server detail for diagnosis (#1724 C041), and
        // never claim "nothing applied" once the save has persisted (#1724 C006).
        // savedApplied travels on the error so the outer catch reports the
        // persisted state truthfully instead of the generic recovery copy.
        if (savedApplied) {
            const persisted = new Error('The voice was saved, but Voices could not be refreshed. Reload Voices to see the applied state before trying again.', {cause: error});
            persisted.savedApplied = true;
            throw persisted;
        }
        throw new Error('Voice save failed. Your edits were retained; try again from Voices.', {cause: error});
    }
    showToast('One voice applied book-wide. Play a sample in Editor and mark Ready in Voices only after listening.', 'success');
}

async function markSingleVoiceReady() {
    if (_singleVoiceBusy) {
        showToast('A single-voice action is still running; please wait.', 'warning');
        return;
    }
    if (typeof isEditorEditPending === 'function' && isEditorEditPending()) {
        showToast('Resolve pending or failed Editor edits before marking voices ready.', 'warning');
        return;
    }
    // refreshVoiceMetadata flushes the queue, so a preexisting dirty queue
    // must be refused here first — never silently saved and then approved
    // as if the bulk readiness had been confirmed against it.
    if (typeof voiceSaveQueue !== 'undefined' && voiceSaveQueue.isDirty()) {
        showToast('Save or discard unsaved voice edits in Voices before marking ready.', 'warning');
        return;
    }
    _singleVoiceBusy = true;
    let savedReady = false;
    let savedApplied = false;
    try {
        const book = currentBookFilename;
        const epoch = window._bookSelectionEpoch;
        await refreshVoiceMetadata();
        if (!singleVoiceBookEpochCurrent(book, epoch)) {
            throw new Error('Active book changed while preparing readiness. Nothing was marked ready.');
        }
        const snapshot = _voiceSaveSnapshot;
        const asset = singleVoiceAllMatchSingleAsset(snapshot);
        if (!asset) {
            throw new Error('Every speaker must use the one chosen voice before bulk readiness is available. Use the existing per-card Ready controls instead.');
        }
        const confirmed = await showConfirm(
            'You listened to this voice. Mark these speakers ready?',
            {title: 'Mark ready after listening?', actionLabel: 'I listened; mark ready', danger: false});
        if (!confirmed) { return; }
        if (!singleVoiceBookEpochCurrent(book, epoch)) {
            throw new Error('Active book changed while confirming. Nothing was marked ready.');
        }
        if (!_voiceSaveSnapshot || _voiceSaveSnapshot.book_token !== snapshot.book_token
            || _voiceSaveSnapshot.revision !== snapshot.revision) {
            throw new Error('Voice configuration changed while confirming. Nothing was marked ready.');
        }
        if (typeof voiceSaveQueue !== 'undefined' && voiceSaveQueue.isDirty()) {
            throw new Error('New unsaved voice edits arrived while confirming. Nothing was marked ready; save or discard them in Voices first.');
        }
        const voicesMap = Object.create(null);
        for (const voice of snapshot.voices) {
            // Patch-only, like the preview path: only ready changes here.
            voicesMap[voice.name] = {ready: true};
        }
        enqueueVoiceDraft(voicesMap);
        try {
            await flushVoiceSaves();
            savedReady = true;
            savedApplied = true;
            await refreshVoiceMetadata();
        } catch (error) {
            if (savedReady) {
                throw new Error('Speakers were marked ready, but Voices could not be refreshed. Reload Voices to see the saved state before trying again.', {cause: error});
            }
            throw error;
        }
        showToast('Speakers marked ready. Render and export with the existing controls.', 'success');
    } catch (error) {
        // Never claim "nothing marked ready" once the save has persisted.
        const recovery = savedApplied
            ? 'The ready save already persisted. Reload Voices before trying again.'
            : 'Nothing was marked ready. Check Voices state before trying again.';
        showActionError('Marking ready failed', error, recovery);
    } finally {
        _singleVoiceBusy = false;
    }
}

// A literal-mode change invalidates any earlier source confirmation.
document.getElementById('script-literal-source-preserving')?.addEventListener('change', () => {
    if (_singleVoiceSourceBinding) {
        _singleVoiceSourceBinding = null;
        renderSingleVoiceSourcePreview();
    }
});
