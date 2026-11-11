/* Audition is separate from explicit, dependency-checked restoration. */
let takeHistoryIdentity = null;
let takeHistoryRequest = 0;
let takeHistoryBusy = false;
let takeHistoryAll = false;
let takeHistoryOffset = 0;

function getTakeHistoryIdentity() {
    const target = getEditorReviewTarget();
    return target && JSON.stringify([chunkSnapshotBookToken, target.chunk.uid,
        getChunkAudioIdentity(target.chunk), target.chunk.take ?? null,
        target.chunk.text, target.chunk.instruct, target.chunk.speaker]);
}

function resetTakeHistoryView() {
    takeHistoryRequest++;
    takeHistoryIdentity = null;
    const list = document.getElementById('editor-take-list');
    for (const audio of list.querySelectorAll('audio')) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
    list.replaceChildren();
    document.getElementById('editor-take-more').hidden = true;
    document.getElementById('editor-take-first').hidden = true;
}

window.applyTakeHistoryInspector = () => {
    const identity = getTakeHistoryIdentity();
    if (takeHistoryIdentity && (identity !== takeHistoryIdentity || hasTakeHistoryDrafts())) {
        resetTakeHistoryView();
        document.getElementById('editor-take-status').textContent = hasTakeHistoryDrafts()
            ? t('take.status.drafts-hidden', 'Pending edits hide the comparison; saved audio is unchanged. Save or discard drafts, then refresh snapshots.')
            : t('take.status.changed', 'The line or saved take changed. Refresh snapshots.');
    }
    document.getElementById('editor-take-save').disabled = takeHistoryBusy || !getEditorReviewTarget()?.chunk.audio_path || hasTakeHistoryDrafts();
};

function hasTakeHistoryDrafts() {
    const target = getEditorReviewTarget();
    return isEditorEditPending() || !!(target && typeof getEditorReviewRowState === 'function' && getEditorReviewRowState(target).dirty);
}

async function loadEditorTakeSnapshots(all = false, offset = 0) {
    if (takeHistoryBusy) { showToast(t('take.error.busy', 'A take operation is still running. Wait for it to finish before browsing snapshots.'), 'warning'); return; }
    const target = getEditorReviewTarget();
    if (!target) { showToast(t('take.error.select-line', 'Select a saved line before browsing snapshots.'), 'warning'); return; }
    if (hasTakeHistoryDrafts()) { showToast(t('take.error.drafts', 'Save or discard pending edits before browsing snapshots.'), 'warning'); return; }
    resetTakeHistoryView();
    const request = takeHistoryRequest;
    const identity = getTakeHistoryIdentity();
    takeHistoryIdentity = identity;
    takeHistoryAll = all;
    takeHistoryOffset = offset;
    const book = chunkSnapshotBookToken;
    const params = new URLSearchParams({expected_book_token: book, offset: String(offset)});
    if (all) { params.set('all_books', 'true'); }
    else { params.set('uid', target.chunk.uid); }
    try {
        const result = await API.get(`/api/take_snapshots?${params}`);
        if (request !== takeHistoryRequest || identity !== getTakeHistoryIdentity() || hasTakeHistoryDrafts()) { return; }
        renderEditorTakeSnapshots(result, book, identity);
    } catch (error) {
        if (request === takeHistoryRequest) { showActionError(t('take.error.load', 'Cannot load snapshots'), error, t('take.error.load-recovery', 'Current line audio is unchanged.')); }
    }
}

function takeStateLabel(state) {
    return { ready: t('take.state.ready', 'ready'), deleted: t('take.state.deleted', 'deleted'), incomplete: t('take.state.incomplete', 'incomplete'),
             interrupted: t('take.state.interrupted', 'interrupted'), protected: t('take.state.protected', 'protected') }[state] || state || t('take.meta.unknown', 'unknown');
}

function renderEditorTakeSnapshots(result, book, identity) {
    const list = document.getElementById('editor-take-list');
    list.replaceChildren();
    const usage = result.usage;
    const usageValues = {count: result.total, records: usage.records, max_records: usage.max_records, used: (usage.bytes / 1048576).toFixed(1), max_bytes: usage.max_bytes / 1048576};
    document.getElementById('editor-take-status').textContent = result.restore_supported
        ? t('take.status.summary', '{count} saved snapshots in this view. Project archive: {records}/{max_records} records, {used} MiB/{max_bytes} MiB. Deleted and incomplete copies count. Audition keeps selected audio. Restoration checks eligibility when requested.', usageValues)
        : t('take.status.summary.no-restore', '{count} saved snapshots in this view. Project archive: {records}/{max_records} records, {used} MiB/{max_bytes} MiB. Deleted and incomplete copies count. Audition keeps selected audio. Restoration checks eligibility when requested. Verified restoration is not qualified on this host.', usageValues);
    for (const record of result.records) {
        const item = document.createElement('div');
        item.className = 'border rounded p-2 mb-2';
        const label = document.createElement('p');
        label.className = 'small mb-1';
        const otherBook = record.book_token !== book;
        const meta = [new Date(record.created * 1000).toLocaleString(getIntlUiLocale()),
            t('take.meta.take', 'take {take}', {take: record.take ?? t('take.meta.unknown', 'unknown')}),
            takeStateLabel(record.state)];
        if (record.protected) { meta.push(t('take.meta.protected', 'protected')); }
        if (takeHistoryAll) {
            meta.push(t('take.meta.book-line', '{generation} / {book} / line UID {uid}', {
                generation: otherBook ? t('take.meta.other-book', 'earlier book generation') : t('take.meta.current-book', 'current book'),
                book: record.book_label || t('take.meta.unnamed-book', 'unnamed book'), uid: record.uid}));
        }
        label.textContent = meta.join(' · ');
        item.append(label);
        const excerpt = document.createElement('p');
        excerpt.className = 'small mb-1'; excerpt.textContent = t('take.excerpt', '{speaker}: {text}', {speaker: record.speaker || t('take.excerpt.speaker-unknown', 'Speaker unknown'), text: record.text || t('take.excerpt.text-missing', 'Text not recorded')});
        item.append(excerpt);
        const evidence = document.createElement('p');
        evidence.className = 'small mb-1';
        const speech = record.speech_evidence;
        const speechLabel = typeof EDITOR_SPEECH_LABELS !== 'undefined' ? EDITOR_SPEECH_LABELS[speech?.status] : speech?.status;
        const archive = t('take.evidence.archive', 'Archive {version} · audio SHA-256 {sha}', {version: record.version, sha: record.sha256});
        const speechText = speech?.state === 'historical'
            ? (speech.reason ? t('take.evidence.historical', 'Historical speech evidence: {label}. {reason} Not a current quality approval.', {label: speechLabel || t('take.evidence.unknown-status', 'Unknown status'), reason: speech.reason})
                : t('take.evidence.historical-no-reason', 'Historical speech evidence: {label}. Not a current quality approval.', {label: speechLabel || t('take.evidence.unknown-status', 'Unknown status')}))
            : speech?.state === 'unavailable' ? t('take.evidence.invalid', 'Saved speech evidence is invalid or does not bind to this archived audio; unavailable, not clean.')
                : t('take.evidence.none', 'No recorded speech evidence; not checked, not clean.');
        evidence.textContent = joinUiSentences([archive, speechText, (record.has_restore_evidence
            ? t('take.evidence.restore-present', 'Restoration metadata exists; current compatibility is checked only on explicit restore.')
            : t('take.evidence.restore-missing', 'Restoration evidence is unavailable; audition only.'))]);
        if (record.state !== 'ready') {
            evidence.textContent = t('take.evidence.unavailable', 'Archive {version} · {state}. Not available for audition or restoration. Recorded metadata does not verify that a complete audio copy is present.', {version: record.version, state: takeStateLabel(record.state)});
        }
        item.append(evidence);
        if (record.state === 'ready') {
            const audio = document.createElement('audio');
            audio.controls = true;
            audio.className = 'w-100';
            audio.title = t('take.audio.title', 'Audition this verified snapshot without changing selected line audio.');
            audio.setAttribute('aria-label', t('take.audio.aria', 'Audition archived take {take}, line {uid}, snapshot {version}; selected audio is unchanged', {take: record.take ?? t('take.meta.unknown', 'unknown'), uid: record.uid, version: record.version}));
            audio.preload = 'none';
            const params = new URLSearchParams({expected_book_token: book, source_book_token: record.book_token, expected_uid: record.uid, sha256: record.sha256});
            audio.src = `/api/take_snapshots/${encodeURIComponent(record.version)}/audio?${params}`;
            audio.addEventListener('error', () => { label.textContent += ' · ' + t('take.audio.error', 'Audio unavailable; refresh snapshots and inspect this copy.'); }, {once: true});
            item.append(audio);
        }
        const buttons = document.createElement('div');
        buttons.className = 'd-flex flex-wrap gap-2 mt-1';
        if (result.restore_supported && record.state === 'ready' && record.has_restore_evidence
                && !otherBook && record.uid === getEditorReviewTarget()?.chunk.uid) {
            const restore = document.createElement('button');
            restore.type = 'button'; restore.className = 'btn btn-outline-primary btn-sm';
            restore.textContent = t('take.action.check-restore', 'Check & restore');
            restore.title = t('take.action.check-restore-title', 'Verify original dependencies and select these saved bytes. Retain the displaced take; refuse incompatible snapshots.');
            restore.onclick = () => restoreEditorTakeSnapshot(record, identity, book);
            buttons.append(restore);
        }
        const actions = record.state === 'ready'
            ? [[record.protected ? 'unprotect' : 'protect', record.protected ? t('take.action.unprotect', 'Unprotect') : t('take.action.protect', 'Protect')],
               ...(!record.protected ? [['delete', t('take.action.delete-undo', 'Delete with undo')]] : [])]
            : record.state === 'deleted' ? [['undo', t('take.action.undo', 'Undo deletion')], ['purge', t('take.action.purge', 'Remove permanently…')]]
                : [['purge', t('take.action.purge-incomplete', 'Remove incomplete copy…')]];
        for (const [action, text] of actions) {
            const button = document.createElement('button');
            button.title = action === 'purge' ? t('take.action.purge-title', 'Permanently remove this archive copy after confirmation; this cannot be undone.') : t('take.action.change-title', 'Change only this snapshot archive record; selected line audio remains unchanged.');
            button.type = 'button'; button.className = 'btn btn-outline-secondary btn-sm'; button.textContent = text;
            button.onclick = () => changeEditorTakeSnapshot(record, action, identity, book);
            buttons.append(button);
        }
        item.append(buttons); list.append(item);
    }
    const more = document.getElementById('editor-take-more');
    more.hidden = result.next_offset === null;
    more.onclick = () => loadEditorTakeSnapshots(takeHistoryAll, result.next_offset);
    document.getElementById('editor-take-first').hidden = takeHistoryOffset === 0;
}

async function restoreEditorTakeSnapshot(record, identity, book) {
    if (takeHistoryBusy || identity !== getTakeHistoryIdentity() || book !== chunkSnapshotBookToken || isEditorEditPending()) { return; }
    try { await ensureEditorRenderSnapshot(); }
    catch (error) { showActionError(t('take.error.restore', 'Cannot restore snapshot'), error, t('take.error.restore-recovery', 'Save pending edits and refresh Editor.')); return; }
    const target = getEditorReviewTarget();
    if (takeHistoryBusy || identity !== getTakeHistoryIdentity() || book !== chunkSnapshotBookToken
            || isEditorEditPending() || !target || record.book_token !== book || record.uid !== target.chunk.uid) { return; }
    takeHistoryRequest++;
    takeHistoryBusy = true; window.applyTakeHistoryInspector();
    const cancel = document.getElementById('editor-take-cancel');
    cancel.hidden = false; cancel.disabled = false;
    try {
        await API.post(`/api/chunks/${target.chunk.id}/take_restore`, {
            expected_book_token: book, source_book_token: record.book_token,
            expected_uid: target.chunk.uid, expected_audio_revision: target.chunk.audio_revision || null,
            expected_audio_path: target.chunk.audio_path || null, expected_take: target.chunk.take ?? null,
            version: record.version, sha256: record.sha256,
        });
        if (identity === getTakeHistoryIdentity() && book === chunkSnapshotBookToken && !isEditorEditPending()) {
            document.getElementById('editor-take-status').textContent = t('take.restore.requested', 'Restoration requested. Checking saved audio and current voice dependencies; follow the audio task below.');
        }
        _startPolling('take_restore', () => API.get('/api/status/audio'), {
            displayLabel: t('take.restore.label', 'Saved take restoration'),
            doneCheck: status => status.running === false,
            onTick: status => {
                if (typeof status.running !== 'boolean') { throw new Error(t('take.restore.error-status', 'Audio task status is unavailable.')); }
                if (status.running && identity === getTakeHistoryIdentity()) {
                    document.getElementById('editor-take-status').textContent = t('take.restore.checking', 'Audio task is checking restoration. Cancel audio task to request cancellation.');
                }
            },
            onDone: status => {
                takeHistoryBusy = false; cancel.hidden = true;
                window.applyTakeHistoryInspector();
                if (identity === getTakeHistoryIdentity()) {
                    document.getElementById('editor-take-status').textContent = status.error || t('take.restore.done', 'Audio task {status}. Refresh snapshots to inspect the selected performance.', {status: status.status || t('take.restore.finished', 'finished')});
                    void loadChunks(true);
                }
            },
        });
    } catch (error) {
        takeHistoryBusy = false; cancel.hidden = true; window.applyTakeHistoryInspector();
        showActionError(t('take.error.start-restore', 'Cannot start restoration'), error, t('take.error.start-restore-recovery', 'Check audio task status and refresh snapshots before retrying.'));
    }
}

async function cancelEditorTakeRestore() {
    if (!takeHistoryBusy) { return; }
    const cancel = document.getElementById('editor-take-cancel');
    cancel.disabled = true;
    try { await API.post('/api/cancel_audio', {}); }
    catch (error) { cancel.disabled = false; showActionError(t('take.error.cancel', 'Cannot cancel audio task'), error, t('take.error.cancel-recovery', 'Check audio task status before retrying.')); }
}

async function saveEditorTakeSnapshot() {
    if (takeHistoryBusy) { return; }
    if (hasTakeHistoryDrafts()) { showToast(t('take.error.drafts-save', 'Save or discard pending edits before saving a take snapshot.'), 'warning'); return; }
    const clickedIdentity = getTakeHistoryIdentity();
    try { await ensureEditorRenderSnapshot(); }
    catch (error) { showActionError(t('take.error.save', 'Cannot save snapshot'), error, t('take.error.save-recovery', 'Save pending edits and refresh Editor.')); return; }
    if (takeHistoryBusy || clickedIdentity !== getTakeHistoryIdentity()) { return; }
    const target = getEditorReviewTarget();
    if (!target?.chunk.audio_path || isEditorEditPending()) { return; }
    takeHistoryRequest++;
    takeHistoryBusy = true;
    window.applyTakeHistoryInspector();
    const identity = getTakeHistoryIdentity();
    try {
        await API.post(`/api/chunks/${target.chunk.id}/take_snapshots`, {
            expected_book_token: chunkSnapshotBookToken, expected_uid: target.chunk.uid,
            expected_audio_revision: target.chunk.audio_revision || null,
        });
        if (identity === getTakeHistoryIdentity()) { showToast(t('take.toast.saved', 'Immutable take snapshot saved.'), 'success'); }
    } catch (error) { showActionError(t('take.error.save', 'Cannot save snapshot'), error, t('take.error.save-recovery-archive', 'Current audio is unchanged. Incomplete copies remain visible in the project archive.')); }
    finally { takeHistoryBusy = false; window.applyTakeHistoryInspector(); }
    if (identity === getTakeHistoryIdentity()) { await loadEditorTakeSnapshots(false); }
}

async function changeEditorTakeSnapshot(record, action, identity, book) {
    if (takeHistoryBusy || identity !== getTakeHistoryIdentity() || isEditorEditPending()) { return; }
    if (action === 'purge' && !await showConfirm(formatUiSafetyText('safety.take.purge', 'Permanently remove this snapshot? This frees its archive storage and cannot be undone. The selected line audio is kept.'), {title: t('safety.take.purge.title', 'Remove snapshot permanently?'), actionLabel: t('safety.take.purge.action', 'Remove permanently'), danger: true})) { return; }
    if (takeHistoryBusy || identity !== getTakeHistoryIdentity() || book !== chunkSnapshotBookToken || isEditorEditPending()) { return; }
    takeHistoryRequest++;
    takeHistoryBusy = true; window.applyTakeHistoryInspector();
    try {
        await API.post('/api/take_snapshots/action', {expected_book_token: book,
            source_book_token: record.book_token, expected_uid: record.uid,
            version: record.version, sha256: record.sha256, action});
    } catch (error) { showActionError(t('take.error.change', 'Cannot change snapshot'), error, t('take.error.change-recovery', 'Refresh history before retrying. Protected or referenced audio is retained.')); }
    finally { takeHistoryBusy = false; window.applyTakeHistoryInspector(); }
    if (identity === getTakeHistoryIdentity()) { await loadEditorTakeSnapshots(takeHistoryAll, takeHistoryOffset); }
}


let automaticTakePolicy = null;
let automaticTakePolicyBusy = false;
let automaticTakePolicyRequest = 0;

function renderAutomaticTakePolicy() {
    document.getElementById('editor-auto-take-refresh').disabled = automaticTakePolicyBusy;
    document.getElementById('editor-auto-take-on').disabled = automaticTakePolicyBusy || !automaticTakePolicy || automaticTakePolicy.enabled === true;
    document.getElementById('editor-auto-take-off').disabled = automaticTakePolicyBusy || !automaticTakePolicy || automaticTakePolicy.enabled === false;
    const status = document.getElementById('editor-auto-take-status');
    if (!automaticTakePolicy) {
        status.textContent = t('take.auto.refresh-default', 'Refresh to read this project setting. Automatic history is off by default.');
    } else if (automaticTakePolicy.error) {
        status.textContent = automaticTakePolicy.error;
    } else {
        status.textContent = automaticTakePolicy.enabled ? t('take.auto.on', 'On. Prior takes are saved before regeneration. History limits include deleted and interrupted copies.') : t('take.auto.off', 'Off. Saved snapshots remain available. A pending prior take stays protected until its replacement succeeds.');
    }
}

async function loadAutomaticTakePolicy() {
    if (automaticTakePolicyBusy) { return; }
    const request = ++automaticTakePolicyRequest;
    automaticTakePolicyBusy = true;
    renderAutomaticTakePolicy();
    try {
        const result = await API.get('/api/take_history_policy');
        if (request === automaticTakePolicyRequest) { automaticTakePolicy = result; }
    } catch (error) {
        automaticTakePolicy = null;
        showActionError(t('take.auto.error.read', 'Cannot read automatic history'), error, t('take.auto.error.read-recovery', 'Inspect the saved policy and refresh this setting.'));
    } finally {
        automaticTakePolicyBusy = false;
        renderAutomaticTakePolicy();
    }
}

async function changeAutomaticTakePolicy(enabled) {
    if (automaticTakePolicyBusy || !automaticTakePolicy) { return; }
    const policy = automaticTakePolicy;
    automaticTakePolicyBusy = true;
    renderAutomaticTakePolicy();
    try {
        automaticTakePolicy = await API.post('/api/take_history_policy', {
            enabled, expected_book_token: policy.book_token, expected_revision: policy.revision,
        });
        showToast(enabled ? t('take.auto.enabled', 'Automatic take history enabled.') : t('take.auto.disabled', 'Automatic take history disabled.'), 'success');
    } catch (error) {
        automaticTakePolicy = null;
        showActionError(t('take.auto.error.change', 'Cannot change automatic history'), error, t('take.auto.error.change-recovery', 'Refresh the setting before retrying. Existing audio is retained.'));
    } finally {
        automaticTakePolicyBusy = false;
        renderAutomaticTakePolicy();
    }
}
