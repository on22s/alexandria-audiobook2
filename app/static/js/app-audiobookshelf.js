/* Manual, explicit delivery of a completed export to Audiobookshelf (#1727).
   Nothing here touches the network until the user clicks a button; the preview
   checks the destination on the server and the send button stays disabled until a fresh preview and
   the right-to-share confirmation are both present. Any book, server,
   library, folder, format, or metadata change invalidates the preview. */
let absEpoch = 0;
let absPreview = null;
let absLibraries = [];
let absSending = false;
let absSendingPreviewId = null;
let absProgressTimer = null;

function getAbsBookToken() {
    return (typeof chunkSnapshotBookToken !== 'undefined' && chunkSnapshotBookToken)
        || (typeof currentBookToken !== 'undefined' && currentBookToken) || '';
}

function absSetStatus(text) {
    document.getElementById('abs-status').textContent = text;
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
        absSetStatus('Preview cleared. Preview the destination and file again before sending.');
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
                element.textContent = `Uploading ${progress.sent} / ${progress.total} bytes (${pct}%).`;
            } else if (element && progress) {
                element.textContent = `Uploading ${progress.sent} bytes.`;
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
        absSetStatus('Enter the server URL first.');
        return;
    }
    try {
        const result = await API.post('/api/abs/config', {
            base_url: url,
            api_key: apiKey || null,
            library_id: document.getElementById('abs-library').value || null,
            folder_id: document.getElementById('abs-folder').value || null,
        });
        if (epoch !== absEpoch) { return; }
        document.getElementById('abs-api-key').value = '';
        absSetStatus(`Server saved (${result.base_url}). API key ${result.has_key ? 'is stored' : 'is not stored yet'}${result.key_store ? ` in the ${result.key_store}` : ''}.`);
    } catch (error) {
        if (epoch === absEpoch) {
            absSetStatus('Could not save the server. The API key was not stored anywhere new.');
            showActionError('Could not save the Audiobookshelf server', error, 'Check the URL and key, then retry.', 'error');
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
        absSetStatus('Enter the server URL, save it, then load libraries.');
        return;
    }
    absSetStatus('Loading libraries from the server (explicit network request)…');
    try {
        const result = await API.get(`/api/abs/libraries?base_url=${encodeURIComponent(url)}`);
        if (epoch !== absEpoch) { return; }
        absLibraries = result.libraries || [];
        const librarySelect = document.getElementById('abs-library');
        librarySelect.replaceChildren();
        for (const library of absLibraries) {
            const option = document.createElement('option');
            option.value = library.id;
            option.textContent = library.name || library.id;
            librarySelect.appendChild(option);
        }
        fillAbsFolders();
        absSetStatus(`Loaded ${absLibraries.length} librar${absLibraries.length === 1 ? 'y' : 'ies'}. Choose a library and folder, then preview.`);
    } catch (error) {
        if (epoch === absEpoch) {
            absSetStatus('Could not load libraries. Nothing was sent anywhere.');
            showActionError('Could not load libraries', error, 'Check the server URL and API key, then retry.', 'error');
        }
    }
}

async function previewAbsSend() {
    const epoch = ++absEpoch;
    absPreview = null;
    updateAbsSendEnabled();
    const book = getAbsBookToken();
    if (!book) {
        absSetStatus('Refresh Editor for the current book, then preview.');
        return;
    }
    const destination = getAbsDestination();
    if (!destination.library_id || !destination.folder_id) {
        absSetStatus('Choose a library and folder first (Load libraries).');
        return;
    }
    if (!destination.title) {
        absSetStatus('Enter the title first.');
        return;
    }
    absSetStatus('Building the preview and checking this library/folder on the server (explicit network request)…');
    try {
        const preview = await API.post('/api/abs/preview', { expected_book_token: book, ...destination });
        if (epoch !== absEpoch || book !== getAbsBookToken()) { return; }
        absPreview = preview;
        const bytes = absPreview.output_bytes;
        document.getElementById('abs-preview-out').textContent =
            `Server: ${absPreview.base_url || destination.base_url}\n`
            + `Destination: library ${absPreview.library_id}, folder ${absPreview.folder_id}\n`
            + `File: ${absPreview.export_file} (${bytes} bytes)\n`
            + `SHA-256: ${absPreview.sha256}\n`
            + `Input revision: ${absPreview.input_revision}\n`
            + `Title: ${absPreview.title}${absPreview.author ? ` · Author: ${absPreview.author}` : ''}${absPreview.series ? ` · Series: ${absPreview.series}` : ''}\n`
            + 'Confirm the right-to-share box, then Send. Metadata is sent only when you send.';
        absSetStatus('Preview ready. Nothing has been sent yet.');
        updateAbsSendEnabled();
    } catch (error) {
        if (epoch === absEpoch) {
            absPreview = null;
            absSetStatus('Preview refused. The local export is unchanged.');
            showActionError('Preview refused', error, 'Re-export if the book changed, then preview again.', 'error');
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
        absSetStatus('Confirm you have the right to share this export first.');
        return;
    }
    const epoch = ++absEpoch;
    absSending = true;
    const destination = getAbsDestination();
    const previewId = absPreview.preview_id;
    absSendingPreviewId = previewId;
    document.getElementById('abs-cancel-btn').style.display = '';
    absSetStatus('Sending… the local export stays in place. You can cancel; a cancelled send may still have arrived, so it is checked before any retry.');
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
        const labels = { sent: 'Delivered — file bytes verified.', failed: 'Send refused before delivery; safe to fix and retry.', unknown: 'Outcome unknown — verify before retrying. Resend stays blocked until byte verification confirms delivery.' };
        const acceptedLabel = 'Upload accepted; verify delivery. The server accepted the upload (HTTP 200) but that alone does not prove the file was stored, so delivery is not yet confirmed. Resend stays blocked until Verify delivery confirms the exact file bytes.';
        absSetStatus(`${isAccepted ? acceptedLabel : (labels[entry.status] || entry.status)} Local export unchanged (${entry.output_bytes} bytes, SHA-256 ${entry.sha256}).`);
        loadAbsDeliveries();
    } catch (error) {
        if (epoch === absEpoch) {
            absPreview = null;
            updateAbsSendEnabled();
            absSetStatus('Send refused. The local export is unchanged.');
            showActionError('Send refused', error, 'Preview again and retry.', 'error');
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
        absSetStatus('Cancel requested. If the server already received the bytes, the outcome stays unknown until you verify delivery.');
    } catch (error) {
        showActionError('Could not cancel the send', error, 'Check delivery status, then reconcile.', 'error');
    }
}

async function loadAbsDeliveries() {
    const epoch = absEpoch;
    const book = getAbsBookToken();
    if (!book) {
        absSetStatus('Refresh Editor for the current book, then check delivery status.');
        return;
    }
    try {
        const result = await API.get(`/api/abs/deliveries?expected_book_token=${encodeURIComponent(book)}`);
        if (epoch !== absEpoch) { return; }
        const container = document.getElementById('abs-deliveries');
        container.replaceChildren();
        if (!result.deliveries.length) {
            absSetStatus('No deliveries recorded for this book yet.');
            return;
        }
        for (const entry of result.deliveries) {
            const row = document.createElement('div');
            row.className = 'border rounded p-2 mb-2';
            const line = document.createElement('p');
            line.className = 'mb-1';
            const isAccepted = entry.status === 'unknown' && (entry.verification_pending || entry.upload_accepted_at);
            const statusLabel = entry.status === 'sent' ? 'Delivered — file bytes verified'
                : isAccepted ? 'Upload accepted; verify delivery' : entry.status;
            line.textContent = `${entry.export_file} → ${entry.title}: ${statusLabel}. ${entry.output_bytes} bytes, SHA-256 ${entry.sha256}.`;
            row.appendChild(line);
            if (entry.error) {
                const note = document.createElement('p');
                note.className = 'mb-1';
                note.textContent = entry.error;
                row.appendChild(note);
            }
            if (entry.verification_note) {
                const note = document.createElement('p');
                note.className = 'mb-1';
                note.textContent = entry.verification_note;
                row.appendChild(note);
            }
            if (entry.reconciliation) {
                const note = document.createElement('p');
                note.className = 'mb-1';
                note.textContent = entry.reconciliation;
                row.appendChild(note);
            }
            if (entry.status === 'unknown') {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'btn btn-outline-secondary btn-sm';
                button.textContent = 'Verify delivery';
                button.onclick = () => reconcileAbsDelivery(entry.id);
                row.appendChild(button);
            }
            container.appendChild(row);
        }
        absSetStatus(`Delivery status (separate from export status): ${result.deliveries.length} record(s) for this book.`);
    } catch (error) {
        if (epoch === absEpoch) {
            showActionError('Could not load delivery status', error, 'Refresh Editor and retry.', 'error');
        }
    }
}

async function reconcileAbsDelivery(deliveryId) {
    try {
        const entry = await API.post('/api/abs/reconcile', { delivery_id: deliveryId });
        const isAccepted = entry.status === 'unknown' && (entry.verification_pending || entry.upload_accepted_at);
        const labels = { sent: 'Delivered — file bytes verified.', unknown: 'Still unknown. Byte verification did not confirm a single exact file; the ledger note explains why and resend stays blocked.' };
        const acceptedLabel = 'Still awaiting verification. The upload was accepted but byte verification did not confirm a single exact file; the ledger note explains why and resend stays blocked.';
        absSetStatus(isAccepted ? acceptedLabel : (labels[entry.status] || entry.status));
        loadAbsDeliveries();
    } catch (error) {
        showActionError('Reconciliation failed', error, 'Check the server is reachable, then retry.', 'error');
    }
}
