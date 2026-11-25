/* Manual, explicit delivery of a completed export to Audiobookshelf (#1727).
   Nothing here contacts the Audiobookshelf server until the user clicks a
   button; opening the panel only reads the saved local config. The preview
   checks the destination on the server and the send button stays disabled until a fresh preview and
   the right-to-share confirmation are both present. Any book, server,
   library, folder, format, or metadata change invalidates the preview. */
let absEpoch = 0;
let absPreview = null;
let absLibraries = [];
let absSending = false;
let absSendingPreviewId = null;
let absProgressTimer = null;
let absSavedSelection = null;
let absListingBaseUrl = null;
let absConfigRequest = null;
let absServerTouched = false;

function isAbsSameServerUrl(left, right) {
    const strip = value => String(value || '').replace(/\/+$/, '');
    return Boolean(left) && Boolean(right) && strip(left) === strip(right);
}

function getAbsBookToken() {
    return (typeof chunkSnapshotBookToken !== 'undefined' && chunkSnapshotBookToken)
        || (typeof currentBookToken !== 'undefined' && currentBookToken) || '';
}

function absSetStatus(text) {
    document.getElementById('abs-status').textContent = text;
}

// The saved-config read has its own line so its failure can never write over
// the safety text in #abs-status, which the app fills at every page load.
function absSetConfigStatus(text) {
    document.getElementById('abs-config-status').textContent = text;
}

function getAbsKeyStatusText(hasKey, keyStatus) {
    if (keyStatus === 'credential-store-insecure') {
        return t('abs.server.key-insecure', 'could not be read (the OS credential store backend is not secure; configure a secure OS keyring backend, then retry)');
    }
    if (hasKey === null) {
        return t('abs.server.key-unreadable', 'could not be read (the OS credential store is unavailable)');
    }
    return hasKey ? t('abs.server.key-stored', 'is stored') : t('abs.server.key-missing', 'is not stored yet');
}

function markAbsServerTouched() {
    absServerTouched = true;
}

async function loadAbsSavedConfig() {
    if (absSavedSelection || absConfigRequest) { return absConfigRequest; }
    absConfigRequest = (async () => {
        absSetConfigStatus('');
        let config;
        try {
            config = await API.get('/api/abs/config');
        } catch (error) {
            absConfigRequest = null;
            const urlInput = document.getElementById('abs-server-url');
            // Only a pristine panel may receive this late failure: a typed URL
            // or a send wins. It goes on the config line, never on #abs-status.
            if (!absServerTouched && !urlInput.value.trim()) {
                const permanent = typeof error?.status === 'number' && error.status >= 400 && error.status < 500;
                absSetConfigStatus(permanent
                    ? t('abs.error.load-config-invalid', 'The saved server settings are invalid. Enter the server URL and save it to replace them.')
                    : t('abs.error.load-config', 'Could not read the saved server settings. Reopen the panel to retry.'));
            }
            return;
        }
        if (!config || typeof config !== 'object' || typeof config.base_url !== 'string' || !config.base_url.trim()) {
            return;
        }
        if (!absSavedSelection) {
            absSavedSelection = {
                base_url: config.base_url,
                library_id: config.library_id || '',
                folder_id: config.folder_id || '',
            };
        }
        const urlInput = document.getElementById('abs-server-url');
        if (!absServerTouched && !urlInput.value.trim()) {
            urlInput.value = config.base_url;
            absSetStatus(t('abs.config.restored', 'Restored saved server ({url}). API key {key}.', {
                url: config.base_url,
                key: getAbsKeyStatusText(config.has_key, config.key_status),
            }));
        }
    })();
    return absConfigRequest;
}

function absDeliveryStatusLabel(status) {
    return {
        sent: t('safety.abs.outcome.sent', 'Delivered — file bytes verified.'),
        failed: t('safety.abs.outcome.failed', 'Send refused before delivery; safe to fix and retry.'),
        unknown: t('safety.abs.outcome.unknown', 'Outcome unknown — verify before retrying. Resend stays blocked until byte verification confirms delivery.'),
    }[status] || status;
}

function getAbsDestination() {
    return {
        base_url: document.getElementById('abs-server-url').value.trim(),
        library_id: document.getElementById('abs-library').value || '',
        folder_id: document.getElementById('abs-folder').value || '',
        export_file: document.getElementById('abs-export-format').value,
        title: document.getElementById('abs-title').value.trim(),
        author: document.getElementById('abs-author').value.trim(),
        series: document.getElementById('abs-series').value.trim(),
    };
}

function resetAbsPreview() {
    absEpoch++;
    absPreview = null;
    document.getElementById('abs-right-to-share').checked = false;
    document.getElementById('abs-send-btn').disabled = true;
    document.getElementById('abs-preview-out').textContent = '';
    if (!absSending) {
        renderUiSafety(document.getElementById('abs-status'), 'safety.abs.preview_cleared', 'Preview cleared. Preview the destination and file again before sending.');
    }
}

function startAbsProgressPoll(previewId, epoch) {
    stopAbsProgressPoll();
    absProgressTimer = setInterval(async () => {
        if (epoch !== absEpoch || !absSending) {
            return;
        }
        try {
            const result = await API.get(`/api/abs/progress?preview_id=${encodeURIComponent(previewId)}`);
            if (epoch !== absEpoch || !absSending) {
                return;
            }
            const progress = result.progress;
            const element = document.getElementById('abs-progress');
            if (element && progress && progress.total > 0) {
                const pct = Math.min(100, Math.round((progress.sent / progress.total) * 100));
                element.textContent = t('abs.progress.total', 'Uploading {sent} / {total} bytes ({pct}%).', {sent: progress.sent, total: progress.total, pct});
            } else if (element && progress) {
                element.textContent = t('abs.progress.bytes', 'Uploading {sent} bytes.', {sent: progress.sent});
            }
        } catch (error) {
            // Progress polling is best-effort; the send outcome decides.
        }
    }, 500);
}

function stopAbsProgressPoll() {
    if (absProgressTimer !== null) {
        clearInterval(absProgressTimer);
        absProgressTimer = null;
    }
}

async function saveAbsConfig() {
    const epoch = absEpoch;
    const url = document.getElementById('abs-server-url').value.trim();
    const apiKey = document.getElementById('abs-api-key').value;
    if (!url) {
        absSetStatus(t('abs.error.url-required', 'Enter the server URL first.'));
        return;
    }
    const savedMatch = absSavedSelection && isAbsSameServerUrl(absSavedSelection.base_url, url);
    const listingMatch = isAbsSameServerUrl(absListingBaseUrl, url);
    let libraryId = document.getElementById('abs-library').value || null;
    let folderId = document.getElementById('abs-folder').value || null;
    if (!listingMatch) {
        if (savedMatch) {
            libraryId = absSavedSelection.library_id || null;
            folderId = absSavedSelection.folder_id || null;
        } else if (absListingBaseUrl) {
            libraryId = null;
            folderId = null;
        }
    }
    try {
        const result = await API.post('/api/abs/config', {
            base_url: url,
            api_key: apiKey || null,
            library_id: libraryId,
            folder_id: folderId,
        });
        absSavedSelection = {
            base_url: result.base_url,
            library_id: result.library_id || '',
            folder_id: result.folder_id || '',
        };
        if (epoch !== absEpoch) { return; }
        absSetConfigStatus('');
        document.getElementById('abs-api-key').value = '';
        // A stored key that cannot be read back is not an unavailable store:
        // name what happened without claiming the store is gone.
        const readBackFailed = Boolean(result.key_store) && result.has_key === null;
        const keyText = readBackFailed
            ? t('abs.server.key-read-back-failed', 'was stored in the {store}, but could not be read back', {store: result.key_store})
            : getAbsKeyStatusText(result.has_key, result.key_status);
        absSetStatus(t('abs.server.saved', 'Server saved ({url}). API key {key}{store}.', {url: result.base_url, key: keyText, store: result.key_store && !readBackFailed ? ' ' + t('abs.server.key-store-name', 'in the {store}', {store: result.key_store}) : ''}));
    } catch (error) {
        if (epoch === absEpoch) {
            absSetStatus(t('abs.error.save-server', 'Could not save the server. The API key was not stored anywhere new.'));
            showActionError(t('abs.error.save-server-action', 'Could not save the Audiobookshelf server'), error, t('abs.error.save-server-recovery', 'Check the URL and key, then retry.'), 'error');
        }
    }
}

function fillAbsFolders() {
    const libraryId = document.getElementById('abs-library').value;
    const folderSelect = document.getElementById('abs-folder');
    folderSelect.replaceChildren();
    const library = absLibraries.find(item => item.id === libraryId);
    if (!library) { return; }
    for (const folder of library.folders || []) {
        const option = document.createElement('option');
        option.value = folder.id;
        option.textContent = folder.fullPath || folder.id;
        folderSelect.appendChild(option);
    }
}

async function loadAbsLibraries() {
    const epoch = absEpoch;
    const url = document.getElementById('abs-server-url').value.trim();
    if (!url) {
        absSetStatus(t('abs.error.url-load', 'Enter the server URL, save it, then load libraries.'));
        return;
    }
    absSetStatus(t('abs.libraries.loading', 'Loading libraries from the server (explicit network request)…'));
    try {
        const result = await API.get(`/api/abs/libraries?base_url=${encodeURIComponent(url)}`);
        if (epoch !== absEpoch) { return; }
        absLibraries = result.libraries || [];
        absListingBaseUrl = result.base_url;
        const librarySelect = document.getElementById('abs-library');
        const folderSelect = document.getElementById('abs-folder');
        librarySelect.replaceChildren();
        for (const library of absLibraries) {
            const option = document.createElement('option');
            option.value = library.id;
            option.textContent = library.name || library.id;
            librarySelect.appendChild(option);
        }
        const savedSelection = absSavedSelection;
        const restoreSaved = Boolean(savedSelection)
            && savedSelection.base_url === result.base_url
            && Boolean(savedSelection.library_id);
        if (restoreSaved) {
            librarySelect.value = savedSelection.library_id;
        }
        fillAbsFolders();
        if (restoreSaved) {
            folderSelect.value = savedSelection.folder_id;
        }
        absSetStatus(absLibraries.length === 1 ? t('abs.libraries.loaded.one', 'Loaded {count} library. Choose a library and folder, then preview.', {count: absLibraries.length}) : t('abs.libraries.loaded.many', 'Loaded {count} libraries. Choose a library and folder, then preview.', {count: absLibraries.length}));
    } catch (error) {
        if (epoch === absEpoch) {
            absSetStatus(t('safety.abs.libraries.unavailable', 'Could not load libraries. Nothing was sent anywhere.'));
            showActionError(t('abs.error.load-libraries', 'Could not load libraries'), error, t('abs.error.load-libraries-recovery', 'Check the server URL and API key, then retry.'), 'error');
        }
    }
}

async function previewAbsSend() {
    const epoch = ++absEpoch;
    absPreview = null;
    updateAbsSendEnabled();
    const book = getAbsBookToken();
    if (!book) {
        absSetStatus(t('abs.error.refresh-editor', 'Refresh Editor for the current book, then preview.'));
        return;
    }
    const destination = getAbsDestination();
    if (!destination.library_id || !destination.folder_id) {
        absSetStatus(t('abs.error.choose-library', 'Choose a library and folder first (Load libraries).'));
        return;
    }
    if (!destination.title) {
        absSetStatus(t('abs.error.title-required', 'Enter the title first.'));
        return;
    }
    absSetStatus(t('abs.preview.building', 'Building the preview and checking this library/folder on the server (explicit network request)…'));
    try {
        const preview = await API.post('/api/abs/preview', { expected_book_token: book, ...destination });
        if (epoch !== absEpoch || book !== getAbsBookToken()) { return; }
        absPreview = preview;
        const bytes = absPreview.output_bytes;
        document.getElementById('abs-preview-out').textContent =
            t('abs.preview.server', 'Server: {server}', {server: absPreview.base_url || destination.base_url}) + '\n'
            + t('abs.preview.destination', 'Destination: library {library}, folder {folder}', {library: absPreview.library_id, folder: absPreview.folder_id}) + '\n'
            + t('abs.preview.file', 'File: {file} ({bytes} bytes)', {file: absPreview.export_file, bytes}) + '\n'
            + t('abs.preview.sha', 'SHA-256: {sha}', {sha: absPreview.sha256}) + '\n'
            + t('abs.preview.revision', 'Input revision: {revision}', {revision: absPreview.input_revision}) + '\n'
            + t('abs.preview.title', 'Title: {title}{author}{series}', {title: absPreview.title,
                author: absPreview.author ? ' · ' + t('abs.preview.author', 'Author: {author}', {author: absPreview.author}) : '',
                series: absPreview.series ? ' · ' + t('abs.preview.series', 'Series: {series}', {series: absPreview.series}) : ''}) + '\n'
            + t('abs.preview.confirm', 'Confirm the right-to-share box, then Send. Metadata is sent only when you send.');
        absSetStatus(t('abs.preview.ready', 'Preview ready. Nothing has been sent yet.'));
        updateAbsSendEnabled();
    } catch (error) {
        if (epoch === absEpoch) {
            absPreview = null;
            renderUiSafety(document.getElementById('abs-status'), 'safety.abs.preview_refused', 'Preview refused. The local export is unchanged.');
            showActionError(t('abs.error.preview-refused', 'Preview refused'), error, t('abs.error.preview-refused-recovery', 'Re-export if the book changed, then preview again.'), 'error');
        }
    }
}

function updateAbsSendEnabled() {
    const ready = Boolean(absPreview && document.getElementById('abs-right-to-share').checked && !absSending);
    document.getElementById('abs-send-btn').disabled = !ready;
}

async function sendAbsExport() {
    if (!absPreview) { return; }
    if (!document.getElementById('abs-right-to-share').checked) {
        renderUiSafety(document.getElementById('abs-status'), 'safety.abs.rights-required', 'Confirm you have the right to share this export first.');
        return;
    }
    const epoch = ++absEpoch;
    absSending = true;
    const destination = getAbsDestination();
    const previewId = absPreview.preview_id;
    absSendingPreviewId = previewId;
    document.getElementById('abs-cancel-btn').style.display = '';
    renderUiSafety(document.getElementById('abs-status'), 'safety.abs.sending', 'Sending… the local export stays in place. You can cancel; a cancelled send may still have arrived, so it is checked before any retry.');
    startAbsProgressPoll(previewId, epoch);
    try {
        const entry = await API.post('/api/abs/send', {
            expected_book_token: getAbsBookToken(),
            preview_id: previewId,
            ...destination,
            right_to_share: true,
            acknowledge_unknown: false,
        });
        if (epoch !== absEpoch) { return; }
        absPreview = null;
        updateAbsSendEnabled();
        const isAccepted = entry.status === 'unknown' && (entry.verification_pending || entry.upload_accepted_at);
        const outcomeValues = {bytes: entry.output_bytes, sha: entry.sha256};
        const statusTarget = document.getElementById('abs-status');
        if (isAccepted) {
            renderUiSafety(statusTarget, 'safety.abs.send-outcome.accepted', 'Upload accepted; verify delivery. The server accepted the upload (HTTP 200) but that alone does not prove the file was stored, so delivery is not yet confirmed. Resend stays blocked until Verify delivery confirms the exact file bytes. Local export unchanged ({bytes} bytes, SHA-256 {sha}).', outcomeValues);
        } else if (entry.status === 'sent') {
            renderUiSafety(statusTarget, 'safety.abs.send-outcome.sent', 'Delivered — file bytes verified. Local export unchanged ({bytes} bytes, SHA-256 {sha}).', outcomeValues);
        } else if (entry.status === 'failed') {
            renderUiSafety(statusTarget, 'safety.abs.send-outcome.failed', 'Send refused before delivery; safe to fix and retry. Local export unchanged ({bytes} bytes, SHA-256 {sha}).', outcomeValues);
        } else {
            renderUiSafety(statusTarget, 'safety.abs.send-outcome.unknown', 'Outcome unknown — verify before retrying. Resend stays blocked until byte verification confirms delivery. Local export unchanged ({bytes} bytes, SHA-256 {sha}).', outcomeValues);
        }
        loadAbsDeliveries();
    } catch (error) {
        if (epoch === absEpoch) {
            absPreview = null;
            updateAbsSendEnabled();
            absSetSafety('safety.abs.send-refused', 'Send refused. The local export is unchanged.');
            showActionError(t('abs.error.send-refused', 'Send refused'), error, t('abs.error.send-refused-recovery', 'Preview again and retry.'), 'error');
        }
    } finally {
        stopAbsProgressPoll();
        absSending = false;
        absSendingPreviewId = null;
        document.getElementById('abs-cancel-btn').style.display = 'none';
        const progressElement = document.getElementById('abs-progress');
        if (progressElement) {
            progressElement.textContent = '';
        }
    }
}

async function cancelAbsSend() {
    const previewId = absSendingPreviewId || (absPreview && absPreview.preview_id);
    if (!previewId) { return; }
    try {
        await API.post('/api/abs/cancel', { preview_id: previewId });
        renderUiSafety(document.getElementById('abs-status'), 'safety.abs.cancel-requested', 'Cancel requested. If the server already received the bytes, the outcome stays unknown until you verify delivery.');
    } catch (error) {
        showActionError(t('abs.error.cancel', 'Could not cancel the send'), error, t('abs.error.cancel-recovery', 'Check delivery status, then reconcile.'), 'error');
    }
}

async function loadAbsDeliveries() {
    const epoch = absEpoch;
    const book = getAbsBookToken();
    if (!book) {
        absSetStatus(t('abs.error.refresh-delivery', 'Refresh Editor for the current book, then check delivery status.'));
        return;
    }
    try {
        const result = await API.get(`/api/abs/deliveries?expected_book_token=${encodeURIComponent(book)}`);
        if (epoch !== absEpoch) { return; }
        const container = document.getElementById('abs-deliveries');
        container.replaceChildren();
        if (!result.deliveries.length) {
            absSetStatus(t('abs.deliveries.none', 'No deliveries recorded for this book yet.'));
            return;
        }
        for (const entry of result.deliveries) {
            const row = document.createElement('div');
            row.className = 'border rounded p-2 mb-2';
            const line = document.createElement('p');
            line.className = 'mb-1';
            const isAccepted = entry.status === 'unknown' && (entry.verification_pending || entry.upload_accepted_at);
            const statusLabel = entry.status === 'sent' ? t('safety.abs.outcome.sent', 'Delivered — file bytes verified.')
                : isAccepted ? t('abs.deliveries.accepted', 'Upload accepted; verify delivery') : entry.status;
            line.textContent = t('abs.deliveries.row', '{file} → {title}: {status}. {bytes} bytes, SHA-256 {sha}.', {file: entry.export_file, title: entry.title, status: statusLabel, bytes: entry.output_bytes, sha: entry.sha256});
            row.appendChild(line);
            if (entry.error) {
                const note = document.createElement('p');
                note.className = 'mb-1';
                note.textContent = entry.error;
                row.appendChild(note);
            }
            const noteText = entry.discarded_at ? entry.discarded_note : entry.verification_note;
            if (noteText) {
                const note = document.createElement('p');
                note.className = 'mb-1';
                note.textContent = noteText;
                row.appendChild(note);
            }
            if (entry.reconciliation && !entry.discarded_at) {
                const note = document.createElement('p');
                note.className = 'mb-1';
                note.textContent = entry.reconciliation;
                row.appendChild(note);
            }
            if (entry.status === 'unknown') {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'btn btn-outline-secondary btn-sm';
                button.textContent = t('abs.deliveries.verify', 'Verify delivery');
                button.onclick = () => reconcileAbsDelivery(entry.id);
                row.appendChild(button);
                const discard = document.createElement('button');
                discard.type = 'button';
                discard.className = 'btn btn-outline-danger btn-sm ms-2';
                discard.textContent = t('abs.deliveries.discard', 'Abandon this send');
                discard.onclick = () => discardAbsDelivery(entry.id);
                row.appendChild(discard);
            }
            container.appendChild(row);
        }
        absSetStatus(t('abs.deliveries.summary', 'Delivery status (separate from export status): {count} records for this book.', {count: result.deliveries.length}));
    } catch (error) {
        if (epoch === absEpoch) {
            showActionError(t('abs.error.load-deliveries', 'Could not load delivery status'), error, t('abs.error.load-deliveries-recovery', 'Refresh Editor and retry.'), 'error');
        }
    }
}

async function reconcileAbsDelivery(deliveryId) {
    try {
        const entry = await API.post('/api/abs/reconcile', { delivery_id: deliveryId });
        const isAccepted = entry.status === 'unknown' && (entry.verification_pending || entry.upload_accepted_at);
        const target = document.getElementById('abs-status');
        if (isAccepted) {
            renderUiSafety(target, 'safety.abs.verify.pending', 'Still awaiting verification. The upload was accepted but byte verification did not confirm a single exact file; the ledger note explains why and resend stays blocked.');
        } else if (entry.status === 'sent') {
            renderUiSafety(target, 'safety.abs.verify.sent', 'Delivered — file bytes verified.');
        } else if (entry.status === 'unknown') {
            renderUiSafety(target, 'safety.abs.verify.unknown', 'Still unknown. Byte verification did not confirm a single exact file; the ledger note explains why and resend stays blocked.');
        } else {
            renderUiSafety(target, 'safety.abs.verify.other', '{status}', {status: entry.status});
        }
        loadAbsDeliveries();
    } catch (error) {
        showActionError(t('abs.error.reconcile', 'Reconciliation failed'), error, t('abs.error.reconcile-recovery', 'Check the server is reachable, then retry.'), 'error');
    }
}

async function discardAbsDelivery(deliveryId) {
    const confirmed = await showConfirm(
        formatUiSafetyText('safety.abs.discard', 'Abandon this send? Check the server first. If the file did arrive, or arrives late, sending again can create a duplicate library item. The local export is unchanged; only this delivery record is marked failed so a new send is allowed.'),
        {title: t('abs.discard.title', 'Abandon this send?'), actionLabel: t('abs.discard.action', 'Abandon this send'), danger: true});
    if (!confirmed) { return; }
    try {
        await API.post('/api/abs/discard', {delivery_id: deliveryId, confirm: true});
        absSetStatus(t('safety.abs.discard.done', 'Send abandoned. Preview and send again if you need to retry; a send that is unknown or already delivered is still blocked by the normal duplicate check.'));
        loadAbsDeliveries();
    } catch (error) {
        showActionError(t('abs.error.discard', 'Could not abandon the send'), error, t('abs.error.discard-recovery', 'Refresh the delivery status, then check the current state.'), 'error');
    }
}
