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
    if (takeHistoryIdentity && (identity !== takeHistoryIdentity || isEditorEditPending())) {
        resetTakeHistoryView();
        document.getElementById('editor-take-status').textContent = 'The line or saved take changed. Refresh snapshots.';
    }
    document.getElementById('editor-take-save').disabled = takeHistoryBusy || !getEditorReviewTarget()?.chunk.audio_path || isEditorEditPending();
};

async function loadEditorTakeSnapshots(all = false, offset = 0) {
    if (takeHistoryBusy) { return; }
    try { await ensureEditorRenderSnapshot(); }
    catch (error) { showActionError('Cannot load snapshots', error, 'Save pending edits and refresh Editor.'); return; }
    const target = getEditorReviewTarget();
    if (!target || isEditorEditPending()) { return; }
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
        if (request !== takeHistoryRequest || identity !== getTakeHistoryIdentity() || isEditorEditPending()) { return; }
        renderEditorTakeSnapshots(result, book, identity);
    } catch (error) {
        if (request === takeHistoryRequest) { showActionError('Cannot load snapshots', error, 'Current line audio is unchanged.'); }
    }
}

function renderEditorTakeSnapshots(result, book, identity) {
    const list = document.getElementById('editor-take-list');
    list.replaceChildren();
    const usage = result.usage;
    document.getElementById('editor-take-status').textContent = `${result.total} saved snapshot(s) in this view. Project archive: ${usage.records}/${usage.max_records} records, ${(usage.bytes / 1048576).toFixed(1)} MiB/${usage.max_bytes / 1048576} MiB. Deleted and incomplete copies count. Audition keeps selected audio. Restoration checks eligibility when requested.${result.restore_supported ? '' : ' Verified restoration is not qualified on this host.'}`;
    for (const record of result.records) {
        const item = document.createElement('div');
        item.className = 'border rounded p-2 mb-2';
        const label = document.createElement('p');
        label.className = 'small mb-1';
        const otherBook = record.book_token !== book;
        label.textContent = `${new Date(record.created * 1000).toLocaleString()} · take ${record.take ?? 'unknown'} · ${record.state}${record.protected ? ' · protected' : ''}${takeHistoryAll ? ` · ${otherBook ? 'earlier book generation' : 'current book'} / ${record.book_label || 'unnamed book'} / line UID ${record.uid}` : ''}`;
        item.append(label);
        const excerpt = document.createElement('p');
        excerpt.className = 'small mb-1'; excerpt.textContent = `${record.speaker || 'Speaker unknown'}: ${record.text || 'Text not recorded'}`;
        item.append(excerpt);
        if (record.state === 'ready') {
            const audio = document.createElement('audio');
            audio.controls = true;
            audio.className = 'w-100';
            audio.title = 'Audition this verified snapshot without changing selected line audio.';
            audio.preload = 'none';
            const params = new URLSearchParams({expected_book_token: book, source_book_token: record.book_token, expected_uid: record.uid, sha256: record.sha256});
            audio.src = `/api/take_snapshots/${encodeURIComponent(record.version)}/audio?${params}`;
            audio.addEventListener('error', () => { label.textContent += ' · Audio unavailable; refresh snapshots and inspect this copy.'; }, {once: true});
            item.append(audio);
        }
        const buttons = document.createElement('div');
        buttons.className = 'd-flex flex-wrap gap-2 mt-1';
        if (result.restore_supported && record.state === 'ready' && record.has_restore_evidence
                && !otherBook && record.uid === getEditorReviewTarget()?.chunk.uid) {
            const restore = document.createElement('button');
            restore.type = 'button'; restore.className = 'btn btn-outline-primary btn-sm';
            restore.textContent = 'Check & restore';
            restore.title = 'Verify original dependencies and select these saved bytes. Retain the displaced take; refuse incompatible snapshots.';
            restore.onclick = () => restoreEditorTakeSnapshot(record, identity, book);
            buttons.append(restore);
        }
        const actions = record.state === 'ready' ? [[record.protected ? 'unprotect' : 'protect', record.protected ? 'Unprotect' : 'Protect'], ...(!record.protected ? [['delete', 'Delete with undo']] : [])] : record.state === 'deleted' ? [['undo', 'Undo deletion'], ['purge', 'Remove permanently…']] : [['purge', 'Remove incomplete copy…']];
        for (const [action, text] of actions) {
            const button = document.createElement('button');
            button.title = action === 'purge' ? 'Permanently remove this archive copy after confirmation; this cannot be undone.' : 'Change only this snapshot archive record; selected line audio remains unchanged.';
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
    catch (error) { showActionError('Cannot restore snapshot', error, 'Save pending edits and refresh Editor.'); return; }
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
            document.getElementById('editor-take-status').textContent = 'Restoration requested. Checking saved audio and current voice dependencies; follow the audio task below.';
        }
        _startPolling('take_restore', () => API.get('/api/status/audio'), {
            displayLabel: 'Saved take restoration',
            doneCheck: status => status.running === false,
            onTick: status => {
                if (typeof status.running !== 'boolean') { throw new Error('Audio task status is unavailable.'); }
                if (status.running && identity === getTakeHistoryIdentity()) {
                    document.getElementById('editor-take-status').textContent = 'Audio task is checking restoration. Cancel audio task to request cancellation.';
                }
            },
            onDone: status => {
                takeHistoryBusy = false; cancel.hidden = true;
                window.applyTakeHistoryInspector();
                if (identity === getTakeHistoryIdentity()) {
                    document.getElementById('editor-take-status').textContent = status.error || `Audio task ${status.status || 'finished'}. Refresh snapshots to inspect the selected performance.`;
                    void loadChunks(true);
                }
            },
        });
    } catch (error) {
        takeHistoryBusy = false; cancel.hidden = true; window.applyTakeHistoryInspector();
        showActionError('Cannot start restoration', error, 'Check audio task status and refresh snapshots before retrying.');
    }
}

async function cancelEditorTakeRestore() {
    if (!takeHistoryBusy) { return; }
    const cancel = document.getElementById('editor-take-cancel');
    cancel.disabled = true;
    try { await API.post('/api/cancel_audio', {}); }
    catch (error) { cancel.disabled = false; showActionError('Cannot cancel audio task', error, 'Check audio task status before retrying.'); }
}

async function saveEditorTakeSnapshot() {
    if (takeHistoryBusy) { return; }
    const clickedIdentity = getTakeHistoryIdentity();
    try { await ensureEditorRenderSnapshot(); }
    catch (error) { showActionError('Cannot save snapshot', error, 'Save pending edits and refresh Editor.'); return; }
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
        if (identity === getTakeHistoryIdentity()) { showToast('Immutable take snapshot saved.', 'success'); }
    } catch (error) { showActionError('Cannot save snapshot', error, 'Current audio is unchanged. Incomplete copies remain visible in the project archive.'); }
    finally { takeHistoryBusy = false; window.applyTakeHistoryInspector(); }
    if (identity === getTakeHistoryIdentity()) { await loadEditorTakeSnapshots(false); }
}

async function changeEditorTakeSnapshot(record, action, identity, book) {
    if (takeHistoryBusy || identity !== getTakeHistoryIdentity() || isEditorEditPending()) { return; }
    if (action === 'purge' && !await showConfirm('Permanently remove this snapshot? This frees its archive storage and cannot be undone. The selected line audio is kept.', {title: 'Remove snapshot permanently?', actionLabel: 'Remove permanently', danger: true})) { return; }
    if (takeHistoryBusy || identity !== getTakeHistoryIdentity() || book !== chunkSnapshotBookToken || isEditorEditPending()) { return; }
    takeHistoryRequest++;
    takeHistoryBusy = true; window.applyTakeHistoryInspector();
    try {
        await API.post('/api/take_snapshots/action', {expected_book_token: book,
            source_book_token: record.book_token, expected_uid: record.uid,
            version: record.version, sha256: record.sha256, action});
    } catch (error) { showActionError('Cannot change snapshot', error, 'Refresh history before retrying. Protected or referenced audio is retained.'); }
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
        status.textContent = 'Refresh to read this project setting. Automatic history is off by default.';
    } else if (automaticTakePolicy.error) {
        status.textContent = automaticTakePolicy.error;
    } else {
        status.textContent = automaticTakePolicy.enabled ? 'On. Prior takes are saved before regeneration. History limits include deleted and interrupted copies.' : 'Off. Saved snapshots remain available. A pending prior take stays protected until its replacement succeeds.';
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
        showActionError('Cannot read automatic history', error, 'Inspect the saved policy and refresh this setting.');
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
        showToast(enabled ? 'Automatic take history enabled.' : 'Automatic take history disabled.', 'success');
    } catch (error) {
        automaticTakePolicy = null;
        showActionError('Cannot change automatic history', error, 'Refresh the setting before retrying. Existing audio is retained.');
    } finally {
        automaticTakePolicyBusy = false;
        renderAutomaticTakePolicy();
    }
}
