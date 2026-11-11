/* Optional OPDS catalog browsing, pure metadata preview, and explicit
   capture/import. Nothing touches the network until an explicit button
   click. Settings are saved explicitly, browsing is explicit, cross-origin
   destinations need an explicit exact review, the metadata preview is a
   pure read, capture starts only from an explicit button bound to the
   current preview, and import starts only from an explicit button bound
   to a captured confirmation for that same preview. Capture never
   auto-imports, and import never selects a book. */
let calibreEpoch = 0;
let calibreBusy = false;
let calibreConfigReady = false;
let calibreReview = null;
let calibreNextToken = null;
let calibreStartToken = null;
let calibreCandidate = null;
let calibreCaptureJob = null;

function calibreEl(id) {
    return document.getElementById(id);
}

function calibreSetStatus(text) {
    calibreEl('calibre-status').textContent = text;
}

function calibreSetJobStatus(text) {
    const el = calibreEl('calibre-job-status');
    if (el) {
        el.textContent = text;
    }
}

function calibreIsCurrent(epoch) {
    return epoch === calibreEpoch;
}

function calibreIsHex32(value) {
    return typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
}

function calibreIsHex64(value) {
    return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
}

function calibreIsTerminalCaptureStatus(status) {
    return ['cancelled', 'discarded', 'failed', 'imported', 'stale'].includes(status);
}

function calibreCancelAlreadySent(job, token) {
    return Boolean(job && token && job.cancelSentTokens && job.cancelSentTokens[token]);
}

function calibreNoteCancelSent(job, token) {
    if (!job || !token) {
        return;
    }
    if (!job.cancelSentTokens) {
        job.cancelSentTokens = {};
    }
    job.cancelSentTokens[token] = true;
    job.cancelSent = true;
}

function calibreIsDefinitiveCaptureRejection(error) {
    // POST /api/calibre/capture returns 4xx only before a job is started
    // (validation, preview/review binding, live-slot/residue/cap refusals),
    // so a 4xx is a confirmed pre-side-effect rejection of this attempt.
    // Transport failures, 5xx and malformed 200 bodies are ambiguous and
    // must stay UNKNOWN rather than failed.
    return Boolean(error && Number.isInteger(error.status) && error.status >= 400 && error.status < 500);
}

function calibreMarkUnknownCaptureStart(job, errorText, label) {
    // A replay may have started a new job whose token was lost. The prior
    // job cannot reconcile or cancel that attempt; discard its authority.
    stopCalibrePoll(job);
    job.jobToken = null;
    job.reviewToken = null;
    job.reviewUrl = '';
    job.reviewOperation = '';
    calibreReview = null;
    calibreEl('calibre-review-panel').hidden = true;
    job.startPending = false;
    job.status = 'unknown';
    job.unknownStart = true;
    job.error = errorText || '';
    job.message = formatUiSafetyText('safety.calibre.capture-start-unknown', '{label} outcome is UNKNOWN: the start response was lost or invalid, so the capture may have been accepted by the backend. No retry was sent and this attempt is not claimed failed or cancelled. Capture is disabled while this outcome is unknown; use Check status if a job token is shown, or explicitly save the catalog settings to retire this local view after a successful save. Saving does not claim the capture never ran or was cancelled, and a backend slot may still block a new capture until its worker cleans up.', {label});
    calibreSetJobStatus(job.message);
    renderCalibreJobPanel();
}

function calibreMarkUnknownImport(job, errorText) {
    // Keep importInFlight true so cancel/discard/import stay disabled until
    // the server status (via the kept job token) confirms the outcome.
    job.importInFlight = true;
    job.importOutcomeUnknown = true;
    job.status = 'unknown';
    job.error = errorText || '';
    job.message = formatUiSafetyText('safety.calibre.import-unknown', 'Import outcome is UNKNOWN: the import response was lost or invalid, so the authority may already have published the book. This is not claimed failed or imported, and no retry was sent. Cancel and discard are disabled until the actual status is safe; use Check status to reconcile this job token. A server status of captured means the import never reserved, imported confirms publication, and a terminal status is shown truthfully.');
    calibreSetJobStatus(job.message);
    renderCalibreJobPanel();
}

function calibreProviderText(provider) {
    if (!provider || typeof provider !== 'object') {
        return '';
    }
    const authors = Array.isArray(provider.authors) ? provider.authors.join(', ') : '';
    const series = provider.series ? `${provider.series}${provider.series_index != null ? ` #${provider.series_index}` : ''}` : '';
    return [t('calibre.provider.title', 'Title: {value}', {value: provider.title || ''}),
        t('calibre.provider.authors', 'Authors: {value}', {value: authors}),
        t('calibre.provider.series', 'Series: {value}', {value: series}),
        t('calibre.provider.subtitle', 'Subtitle: {value}', {value: provider.subtitle || ''}),
        t('calibre.provider.format', 'Format: {value}', {value: provider.format || ''})].join('\n');
}

function calibreCopyProvider(provider) {
    if (!provider || typeof provider !== 'object') {
        return null;
    }
    return {
        title: provider.title || '',
        authors: Array.isArray(provider.authors) ? [...provider.authors] : [],
        series: provider.series || null,
        series_index: provider.series_index != null ? provider.series_index : null,
        subtitle: provider.subtitle || '',
        format: provider.format || '',
    };
}

function calibreJobIsLive(job) {
    if (!job) {
        return false;
    }
    if (job.importInFlight) {
        return true;
    }
    return !calibreIsTerminalCaptureStatus(job.status);
}

function calibreCanImport(job) {
    if (!job || job.status !== 'captured') {
        return false;
    }
    if (!calibreIsHex32(job.confirmationToken) || !calibreIsHex64(job.sha256)) {
        return false;
    }
    if (job.importInFlight || job.importConsumed || job.discardInFlight) {
        return false;
    }
    if (job.retired || job.cancelRequested || job.epoch !== calibreEpoch) {
        return false;
    }
    if (!calibreCandidate || !job.candidate) {
        return false;
    }
    if (calibreCandidate.preview_token !== job.candidate.preview_token) {
        return false;
    }
    if (calibreCandidate.config_revision !== job.configRevision) {
        return false;
    }
    if (job.validationError) {
        return false;
    }
    return true;
}

function updateCalibreControls() {
    calibreEl('calibre-load-config').disabled = calibreBusy;
    calibreEl('calibre-save-config').disabled = calibreBusy;
    calibreEl('calibre-root-url').disabled = calibreBusy;
    calibreEl('calibre-username').disabled = calibreBusy;
    calibreEl('calibre-password').disabled = calibreBusy;
    calibreEl('calibre-clear-password').disabled = calibreBusy;
    calibreEl('calibre-browse-root').disabled = calibreBusy || !calibreConfigReady;
    calibreEl('calibre-next-page').disabled = calibreBusy || !calibreNextToken;
    calibreEl('calibre-start-page').disabled = calibreBusy || !calibreStartToken;
    const reviewStale = calibreReview && calibreReview.epoch !== calibreEpoch;
    calibreEl('calibre-review-approve').disabled = calibreBusy || !calibreReview || reviewStale;
    calibreEl('calibre-review-cancel').disabled = !calibreReview && !calibreBusy;
    const job = calibreCaptureJob;
    const captureStart = calibreEl('calibre-capture-start');
    if (captureStart) {
        captureStart.disabled = calibreBusy || !calibreCandidate || calibreJobIsLive(job);
    }
    const checkStatus = calibreEl('calibre-check-status');
    if (checkStatus) {
        checkStatus.disabled = !job || !job.jobToken || job.pollInFlight;
    }
    const cancelJob = calibreEl('calibre-cancel-capture');
    if (cancelJob) {
        cancelJob.disabled = !job || job.importInFlight || job.status === 'unknown' || (!job.jobToken && !job.startPending) || calibreIsTerminalCaptureStatus(job.status);
    }
    const canDiscard = Boolean(job && job.status !== 'unknown' && calibreIsHex32(job.confirmationToken) && !job.importInFlight && !job.discardInFlight && !job.importConsumed);
    const discard = calibreEl('calibre-discard-capture');
    if (discard) {
        discard.disabled = !canDiscard;
    }
    const jobDiscard = calibreEl('calibre-job-discard');
    if (jobDiscard) {
        jobDiscard.disabled = !canDiscard;
    }
    const importButton = calibreEl('calibre-import-confirm');
    if (importButton) {
        importButton.disabled = !calibreCanImport(job);
    }
}

function renderCalibreJobPanel() {
    const panel = calibreEl('calibre-job-panel');
    if (!panel) {
        return;
    }
    const job = calibreCaptureJob;
    if (!job) {
        panel.hidden = true;
        return;
    }
    panel.hidden = false;
    const lines = [];
    lines.push(t('calibre.job.status', 'Status: {status}', {status: job.status || 'starting'}));
    if (job.jobToken) {
        lines.push(t('calibre.job.token', 'Job token: {token}', {token: job.jobToken}));
    }
    if (job.candidate) {
        lines.push(calibreProviderText(job.candidate.provider));
        lines.push(t('calibre.job.stable-id', 'Stable ID: {value}', {value: job.candidate.stable_id || ''}));
        lines.push(t('calibre.job.source', 'Source: {value}', {value: job.candidate.source_url || ''}));
        lines.push(t('calibre.job.config-revision', 'Config revision: {value}', {value: job.configRevision != null ? job.configRevision : ''}));
    }
    if (job.provider) {
        lines.push(t('calibre.job.captured-provider', 'Captured provider (read-only):'));
        lines.push(calibreProviderText(job.provider));
    }
    if (job.sourceIdentity) {
        lines.push(t('calibre.job.stable-id', 'Stable ID: {value}', {value: job.sourceIdentity.stable_id || ''}));
        lines.push(t('calibre.job.source', 'Source: {value}', {value: job.sourceIdentity.source_url || ''}));
        lines.push(t('calibre.job.final-url', 'Final URL: {value}', {value: job.sourceIdentity.final_url || ''}));
    }
    if (job.sha256) {
        lines.push(t('calibre.job.sha', 'SHA-256: {value}', {value: job.sha256}));
    }
    if (job.sizeBytes != null) {
        lines.push(t('calibre.job.size', 'Size: {value} bytes', {value: job.sizeBytes}));
    }
    if (job.error) {
        lines.push(t('calibre.job.error', 'Error: {value}', {value: job.error}));
    }
    if (job.validationError) {
        lines.push(t('calibre.job.import-blocked', 'Import blocked: {value}', {value: job.validationError}));
    }
    if (job.reviewToken) {
        lines.push(t('calibre.job.pending-review', 'Pending review destination: {url} ({operation})', {url: job.reviewUrl || '', operation: job.reviewOperation || ''}));
    }
    if (job.message) {
        lines.push(job.message);
    }
    calibreEl('calibre-job-out').textContent = lines.join('\n');
    const receiptEl = calibreEl('calibre-import-receipt');
    if (receiptEl) {
        if (job.receipt && job.receipt.upload) {
            const upload = job.receipt.upload;
            const receiptLines = [];
            receiptLines.push(t('calibre.receipt.imported', 'Imported: {file}', {file: upload.stored_filename || ''}));
            receiptLines.push(t('calibre.job.sha', 'SHA-256: {value}', {value: job.receipt.sha256 || ''}));
            receiptLines.push(t('calibre.job.size', 'Size: {value} bytes', {value: job.receipt.size_bytes != null ? job.receipt.size_bytes : ''}));
            if (job.receipt.provider) {
                receiptLines.push(calibreProviderText(job.receipt.provider));
            }
            if (job.receipt.source_identity) {
                receiptLines.push(t('calibre.job.stable-id', 'Stable ID: {value}', {value: job.receipt.source_identity.stable_id || ''}));
                receiptLines.push(t('calibre.job.source', 'Source: {value}', {value: job.receipt.source_identity.source_url || ''}));
                receiptLines.push(t('calibre.job.final-url', 'Final URL: {value}', {value: job.receipt.source_identity.final_url || ''}));
            }
            receiptLines.push(t('calibre.receipt.no-selection', 'Import did not select a book. Choose the file above from existing uploads to select it.'));
            receiptEl.textContent = receiptLines.join('\n');
        } else {
            receiptEl.textContent = '';
        }
    }
    const confirmationPanel = calibreEl('calibre-confirmation-panel');
    if (confirmationPanel) {
        if (calibreCanImport(job)) {
            confirmationPanel.hidden = false;
            calibreEl('calibre-confirmation-provider').textContent = calibreProviderText(job.provider);
            calibreEl('calibre-confirmation-stable-id').textContent = t('calibre.job.stable-id', 'Stable ID: {value}', {value: job.sourceIdentity.stable_id || ''});
            calibreEl('calibre-confirmation-source').textContent = t('calibre.job.source', 'Source: {value}', {value: job.sourceIdentity.source_url || ''});
            calibreEl('calibre-confirmation-final').textContent = t('calibre.job.final-url', 'Final URL: {value}', {value: job.sourceIdentity.final_url || ''});
            calibreEl('calibre-confirmation-sha256').textContent = t('calibre.job.sha', 'SHA-256: {value}', {value: job.sha256});
            calibreEl('calibre-confirmation-size').textContent = t('calibre.job.size', 'Size: {value} bytes', {value: job.sizeBytes});
        } else {
            confirmationPanel.hidden = true;
            calibreEl('calibre-confirmation-provider').textContent = '';
            calibreEl('calibre-confirmation-stable-id').textContent = '';
            calibreEl('calibre-confirmation-source').textContent = '';
            calibreEl('calibre-confirmation-final').textContent = '';
            calibreEl('calibre-confirmation-sha256').textContent = '';
            calibreEl('calibre-confirmation-size').textContent = '';
        }
    }
    updateCalibreControls();
}

function clearCalibreConfirmationPanel() {
    const panel = calibreEl('calibre-confirmation-panel');
    if (panel) {
        panel.hidden = true;
    }
    for (const id of ['calibre-confirmation-provider', 'calibre-confirmation-stable-id', 'calibre-confirmation-source', 'calibre-confirmation-final', 'calibre-confirmation-sha256', 'calibre-confirmation-size']) {
        const el = calibreEl(id);
        if (el) {
            el.textContent = '';
        }
    }
}

function retireCalibreAuthority() {
    calibreCandidate = null;
    clearCalibreConfirmationPanel();
    const job = calibreCaptureJob;
    if (!job) {
        return;
    }
    if (job.importInFlight) {
        job.retired = true;
        if (job.importOutcomeUnknown || job.status === 'unknown') {
            job.message = formatUiSafetyText('safety.calibre.reset.import-unknown', 'View reset while an import with UNKNOWN outcome is in flight. The outcome is still UNKNOWN and not claimed failed or imported; use Check status to reconcile it. It will not select a book or restore this view.');
            calibreSetJobStatus(t('safety.calibre.reset.import-unknown.status', 'View reset while import outcome is UNKNOWN. Use Check status to reconcile it.'));
        } else {
            job.message = formatUiSafetyText('safety.calibre.reset.import-inflight', 'View reset while import is in flight. The confirmed import may finish; its receipt stays in this pane. It will not select a book or restore this view.');
            calibreSetJobStatus(t('safety.calibre.reset.import-inflight.status', 'View reset while import is in flight. The confirmed import may finish; its receipt stays in this pane.'));
        }
        renderCalibreJobPanel();
        return;
    }
    if (job.status === 'unknown' && !job.importInFlight) {
        if (job.unknownRecoveredByConfigSave) {
            job.status = 'stale';
            job.message = formatUiSafetyText('safety.calibre.reset.config-saved', 'An explicit successful catalog settings save retired this local view after a capture start with UNKNOWN outcome. This does not claim the capture never ran or was cancelled; a backend slot may still block a new capture until its worker cleans up.');
            calibreSetJobStatus(t('safety.calibre.reset.config-saved.status', 'An explicit catalog settings save retired the unknown capture-start view. Browse and preview again before capturing.'));
            renderCalibreJobPanel();
            return;
        }
        job.retired = true;
        job.message = formatUiSafetyText('safety.calibre.reset.unknown-kept', 'View reset does not free a capture start with UNKNOWN outcome. No retry was sent and this attempt is not claimed failed or cancelled. Explicitly save the catalog settings to retire this local view after a successful save.');
        calibreSetJobStatus(t('safety.calibre.reset.unknown-kept.status', 'Capture start outcome is UNKNOWN. Explicitly save the catalog settings to retire this local view; a view reset alone does not free it.'));
        renderCalibreJobPanel();
        return;
    }
    job.retired = true;
    if (calibreIsTerminalCaptureStatus(job.status)) {
        stopCalibrePoll(job);
        renderCalibreJobPanel();
        return;
    }
    if (job.startPending) {
        job.cancelRequested = true;
        if (job.jobToken && !calibreCancelAlreadySent(job, job.jobToken)) {
            calibreNoteCancelSent(job, job.jobToken);
            job.message = formatUiSafetyText('safety.calibre.reset.cancel-known', 'View reset during capture start. Cancelling the known job token now; a late replay token for this start will also be cancelled when it arrives, then checked.');
            calibreSetJobStatus(t('safety.calibre.reset.cancel-known.status', 'View reset during capture start. Cancelling the known token and any late replay token.'));
            renderCalibreJobPanel();
            void cancelCalibreCaptureInternal(job);
            startCalibrePoll(job);
            return;
        }
        job.message = formatUiSafetyText('safety.calibre.reset.cancel-late', 'View reset during capture start. The late job token will be bound to this original start and cancelled, then checked.');
        calibreSetJobStatus(t('safety.calibre.reset.cancel-late.status', 'View reset during capture start. Waiting for the job token to cancel it.'));
        renderCalibreJobPanel();
        return;
    }
    if (job.confirmationToken && job.status === 'captured') {
        if (!job.discardSent) {
            job.discardSent = job.confirmationToken;
            job.message = formatUiSafetyText('safety.calibre.reset.discard-captured', 'View reset retired this captured source. Discarding its confirmation; import is not available for the retired view.');
            calibreSetJobStatus(t('safety.calibre.reset.discard-captured.status', 'View reset retired the captured source. Discarding its confirmation.'));
            renderCalibreJobPanel();
            void discardCalibreCaptureInternal(job);
            return;
        }
    }
    if (job.jobToken) {
        if (!calibreCancelAlreadySent(job, job.jobToken)) {
            calibreNoteCancelSent(job, job.jobToken);
            job.cancelRequested = true;
            job.message = formatUiSafetyText('safety.calibre.reset.cancel-retired', 'View reset retired this capture. Cancel requested; this pane stays until the job reaches a terminal status.');
            calibreSetJobStatus(t('safety.calibre.reset.cancel-retired.status', 'View reset retired this capture. Cancel requested.'));
            renderCalibreJobPanel();
            void cancelCalibreCaptureInternal(job);
            startCalibrePoll(job);
            return;
        }
        startCalibrePoll(job);
        renderCalibreJobPanel();
        return;
    }
    job.status = 'stale';
    job.message = formatUiSafetyText('safety.calibre.reset.review-retired', 'Capture review was retired by the view reset. No capture job was started, so nothing is downloading.');
    calibreSetJobStatus(t('safety.calibre.reset.review-retired.status', 'Capture review was retired by the view reset. No capture job was started.'));
    stopCalibrePoll(job);
    renderCalibreJobPanel();
}

function clearCalibreDecisionPanels() {
    calibreReview = null;
    calibreEl('calibre-review-panel').hidden = true;
    calibreEl('calibre-review-url').textContent = '';
    calibreEl('calibre-review-origin').textContent = '';
    calibreEl('calibre-review-operation').textContent = '';
    calibreEl('calibre-preview-panel').hidden = true;
    calibreEl('calibre-preview-out').textContent = '';
    retireCalibreAuthority();
}

function clearCalibreView() {
    calibreEpoch += 1;
    calibreBusy = false;
    calibreReview = null;
    calibreNextToken = null;
    calibreStartToken = null;
    calibreEl('calibre-entries').replaceChildren();
    calibreEl('calibre-page-title').textContent = '';
    calibreEl('calibre-page-info').textContent = '';
    clearCalibreDecisionPanels();
    updateCalibreControls();
}

function resetCalibreSourceView() {
    clearCalibreView();
    calibreSetStatus(t('calibre.status.source-changed', 'Source changed. Catalog browse and preview cleared.'));
}

function onCalibreConfigChange() {
    calibreConfigReady = false;
    clearCalibreView();
    calibreSetStatus(t('calibre.status.settings-dirty', 'Catalog settings changed but are not saved. Save, then browse.'));
}

function calibreErrorText(error) {
    const message = error && error.message ? error.message : t('calibre.error.request-failed', 'Request failed.');
    if ((error && error.status === 410) || /Browse the catalog again/i.test(message)) {
        return /Browse the catalog again/i.test(message) ? message : t('calibre.error.rebrowse', '{message} Browse the catalog again.', {message});
    }
    return message;
}

async function loadCalibreConfig() {
    if (calibreBusy) { return; }
    calibreConfigReady = false;
    clearCalibreView();
    const epoch = calibreEpoch;
    calibreBusy = true;
    updateCalibreControls();
    calibreSetStatus(t('calibre.status.loading-config', 'Loading saved catalog settings (local read only)…'));
    try {
        const result = await API.get('/api/calibre/config');
        if (!calibreIsCurrent(epoch)) { return; }
        calibreEl('calibre-root-url').value = result.root_url || '';
        calibreEl('calibre-username').value = result.username || '';
        calibreEl('calibre-password').value = '';
        if (result.recovery_required) {
            calibreConfigReady = false;
            calibreSetStatus(t('calibre.status.recovery-required', 'Recovery required: enter the password again and save the catalog settings to recover. Browsing is disabled until then.'));
        } else {
            calibreConfigReady = result.configured === true;
            calibreSetStatus(result.configured ? t('calibre.status.config-loaded', 'Saved catalog settings loaded. Save again before browsing if you edit them.') : t('calibre.status.config-missing', 'No catalog is saved yet. Enter a root URL, save, then browse.'));
        }
    } catch (error) {
        if (!calibreIsCurrent(epoch)) { return; }
        calibreSetStatus(calibreErrorText(error));
    } finally {
        if (calibreIsCurrent(epoch)) {
            calibreBusy = false;
            updateCalibreControls();
        }
    }
}

async function saveCalibreConfig() {
    if (calibreBusy) { return; }
    calibreConfigReady = false;
    const unknownStartJob = calibreCaptureJob && calibreCaptureJob.status === 'unknown' && !calibreCaptureJob.importInFlight ? calibreCaptureJob : null;
    const unknownStartCandidateToken = unknownStartJob && unknownStartJob.candidate ? unknownStartJob.candidate.preview_token : null;
    clearCalibreView();
    const epoch = calibreEpoch;
    const rootUrl = calibreEl('calibre-root-url').value.trim();
    const username = calibreEl('calibre-username').value.trim();
    const clearPassword = calibreEl('calibre-clear-password').checked;
    const passwordValue = calibreEl('calibre-password').value;
    calibreEl('calibre-password').value = '';
    calibreEl('calibre-clear-password').checked = false;
    calibreBusy = true;
    updateCalibreControls();
    calibreSetStatus(t('calibre.status.saving-config', 'Saving catalog settings…'));
    try {
        const result = await API.post('/api/calibre/config', {
            root_url: rootUrl,
            username,
            password: clearPassword ? '' : (passwordValue || null),
        });
        if (!calibreIsCurrent(epoch)) { return; }
        calibreConfigReady = result.configured === true && !result.recovery_required;
        if (unknownStartJob && calibreCaptureJob === unknownStartJob && unknownStartJob.candidate && unknownStartJob.candidate.preview_token === unknownStartCandidateToken && unknownStartJob.status === 'unknown' && !unknownStartJob.importInFlight) {
            // Explicit successful save is the bounded manual-recovery fence
            // for an unknown capture start: the backend on_config_changed
            // hook cancels/stales unconfirmed jobs. Bind the local
            // retirement to this exact job/candidate so an ordinary reset
            // never frees it.
            unknownStartJob.unknownRecoveredByConfigSave = true;
            unknownStartJob.unknownRecoveryRevision = result.revision != null ? result.revision : null;
        }
        clearCalibreView();
        calibreEl('calibre-root-url').value = result.root_url || '';
        calibreEl('calibre-username').value = result.username || '';
        calibreSetStatus(calibreConfigReady ? t('calibre.status.config-saved', 'Catalog settings saved. Browse the catalog root when ready.') : t('calibre.status.config-disabled', 'Catalog disabled. Enter a root URL and save before browsing.'));
    } catch (error) {
        if (!calibreIsCurrent(epoch)) { return; }
        calibreSetStatus(calibreErrorText(error));
    } finally {
        if (calibreIsCurrent(epoch)) {
            calibreBusy = false;
            updateCalibreControls();
        }
    }
}

function renderCalibreReview(review, epoch) {
    let origin = review.destination_origin || '';
    if (!origin && review.destination_url) {
        try {
            origin = new URL(review.destination_url).origin;
        } catch (error) {
            origin = '';
        }
    }
    calibreReview = {
        review_token: review.review_token,
        destination_url: review.destination_url || '',
        destination_origin: origin,
        operation: review.operation || '',
        epoch,
        capturePreviewToken: review.operation === 'capture' && calibreCandidate ? calibreCandidate.preview_token : null,
        captureJob: review.operation === 'capture' ? calibreCaptureJob : null,
    };
    calibreEl('calibre-review-url').textContent = calibreReview.destination_url;
    calibreEl('calibre-review-origin').textContent = calibreReview.destination_origin;
    calibreEl('calibre-review-operation').textContent = calibreReview.operation;
    calibreEl('calibre-review-panel').hidden = false;
    updateCalibreControls();
}

function renderCalibreEntries(entries) {
    const container = calibreEl('calibre-entries');
    container.replaceChildren();
    for (const entry of entries || []) {
        const row = document.createElement('div');
        const heading = document.createElement('p');
        heading.textContent = entry.title || entry.stable_id || t('calibre.entry.untitled', 'Untitled entry');
        row.appendChild(heading);
        const details = document.createElement('p');
        const authors = Array.isArray(entry.authors) ? entry.authors.join(', ') : '';
        const series = entry.series ? `${entry.series}${entry.series_index != null ? ` #${entry.series_index}` : ''}` : '';
        details.textContent = [authors, series, entry.format].filter(Boolean).join(' · ');
        row.appendChild(details);
        if (entry.rejected_reason) {
            const reason = document.createElement('p');
            reason.textContent = t('calibre.entry.not-available', 'Not available: {reason}', {reason: entry.rejected_reason});
            row.appendChild(reason);
        }
        const navigation = Array.isArray(entry.navigation) ? entry.navigation : [];
        for (const nav of navigation) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-outline-primary btn-sm';
            button.textContent = t('calibre.entry.open-category', 'Open category: {title}', {title: nav.title || t('calibre.entry.untitled-category', 'Untitled category')});
            button.setAttribute('data-help-key', 'openCalibreNav');
            if (!nav.nav_token || nav.loop_refused) {
                button.disabled = true;
            } else {
                const token = nav.nav_token;
                button.addEventListener('click', () => openCalibreNav(token));
            }
            row.appendChild(button);
        }
        const previewButton = document.createElement('button');
        previewButton.type = 'button';
        previewButton.className = 'btn btn-outline-secondary btn-sm';
        previewButton.textContent = t('calibre.entry.preview', 'Preview metadata');
        previewButton.setAttribute('data-help-key', 'previewCalibreBook');
        if (!entry.preview_token || entry.rejected_reason || !entry.has_acquisition) {
            previewButton.disabled = true;
        } else {
            const token = entry.preview_token;
            previewButton.addEventListener('click', () => previewCalibreBook(token));
        }
        row.appendChild(previewButton);
        container.appendChild(row);
    }
}

function handleCalibreBrowseResponse(result, epoch) {
    if (result && result.review_required) {
        renderCalibreReview(result, epoch);
        calibreSetStatus(t('safety.calibre.review.browse', 'Cross-origin destination requires your exact review before browsing continues.'));
        return;
    }
    calibreNextToken = result.next_token || null;
    calibreStartToken = result.start_token || null;
    calibreEl('calibre-page-title').textContent = result.title || '';
    calibreEl('calibre-page-info').textContent = t('calibre.page.info', '{feed} · Page {page} · {total} entries shown', {feed: result.feed_url || '', page: result.pages_fetched || '', total: result.entries_total || 0}).trim();
    renderCalibreEntries(result.entries);
    calibreSetStatus(t('calibre.status.page-loaded', 'Catalog page loaded. Choose a category, or preview a book. Nothing has been downloaded.'));
    updateCalibreControls();
}

async function requestCalibreBrowse(url, body, epoch) {
    try {
        const result = await API.post(url, body);
        if (!calibreIsCurrent(epoch)) { return; }
        handleCalibreBrowseResponse(result, epoch);
    } catch (error) {
        if (!calibreIsCurrent(epoch)) { return; }
        calibreSetStatus(calibreErrorText(error));
    } finally {
        if (calibreIsCurrent(epoch)) {
            calibreBusy = false;
            updateCalibreControls();
        }
    }
}

async function browseCalibreRoot() {
    if (calibreBusy) { return; }
    if (!calibreConfigReady) {
        calibreSetStatus(t('calibre.status.config-first', 'Load or save the catalog settings first, then browse.'));
        return;
    }
    clearCalibreView();
    const epoch = calibreEpoch;
    calibreBusy = true;
    updateCalibreControls();
    calibreSetStatus(t('calibre.status.browsing-root', 'Browsing the saved catalog root (explicit request)…'));
    await requestCalibreBrowse('/api/calibre/browse-root', {}, epoch);
}

async function browseCalibreNextPage() {
    if (calibreBusy || !calibreNextToken) { return; }
    const token = calibreNextToken;
    clearCalibreView();
    const epoch = calibreEpoch;
    calibreBusy = true;
    updateCalibreControls();
    calibreSetStatus(t('calibre.status.next-page', 'Loading the next catalog page (explicit request)…'));
    await requestCalibreBrowse('/api/calibre/browse-next', { token }, epoch);
}

async function browseCalibreStartPage() {
    if (calibreBusy || !calibreStartToken) { return; }
    const token = calibreStartToken;
    clearCalibreView();
    const epoch = calibreEpoch;
    calibreBusy = true;
    updateCalibreControls();
    calibreSetStatus(t('calibre.status.first-page', 'Returning to the first catalog page (explicit request)…'));
    await requestCalibreBrowse('/api/calibre/browse-next', { token }, epoch);
}

async function openCalibreNav(token) {
    if (calibreBusy || !token) { return; }
    clearCalibreView();
    const epoch = calibreEpoch;
    calibreBusy = true;
    updateCalibreControls();
    calibreSetStatus(t('calibre.status.opening-category', 'Opening the selected category (explicit request)…'));
    await requestCalibreBrowse('/api/calibre/browse-next', { token }, epoch);
}

function stopCalibrePoll(job) {
    if (!job) {
        return;
    }
    if (job.timer !== null && job.timer !== undefined) {
        clearInterval(job.timer);
        job.timer = null;
    }
}

function startCalibrePoll(job) {
    if (!job || !job.jobToken) {
        return;
    }
    if (job.timer !== null && job.timer !== undefined) {
        return;
    }
    if (calibreIsTerminalCaptureStatus(job.status) || job.status === 'captured' || job.status === 'review_required') {
        return;
    }
    job.timer = setInterval(() => {
        if (job !== calibreCaptureJob) {
            stopCalibrePoll(job);
            return;
        }
        if (job.pollInFlight) {
            return;
        }
        void pollCalibreCapture(job);
    }, 1000);
}

function calibreValidateCaptured(job, result) {
    if (!calibreIsHex32(result.confirmation_token)) {
        return formatUiSafetyText('safety.calibre.validation.confirmation-token', 'Confirmation token is missing or invalid.');
    }
    if (!calibreIsHex64(result.sha256)) {
        return formatUiSafetyText('safety.calibre.validation.sha', 'Captured SHA-256 is missing or invalid.');
    }
    if (!Number.isInteger(result.size_bytes) || result.size_bytes < 0) {
        return formatUiSafetyText('safety.calibre.validation.size', 'Captured size is missing or invalid.');
    }
    if (!result.provider || typeof result.provider !== 'object') {
        return formatUiSafetyText('safety.calibre.validation.provider', 'Captured provider is missing.');
    }
    const candidateProvider = job.candidate ? job.candidate.provider : null;
    if (candidateProvider) {
        if ((result.provider.title || '') !== (candidateProvider.title || '')) {
            return formatUiSafetyText('safety.calibre.validation.title-mismatch', 'Captured provider title does not match the previewed book.');
        }
        const capturedAuthors = Array.isArray(result.provider.authors) ? result.provider.authors.join(', ') : '';
        const candidateAuthors = Array.isArray(candidateProvider.authors) ? candidateProvider.authors.join(', ') : '';
        if (capturedAuthors !== candidateAuthors) {
            return formatUiSafetyText('safety.calibre.validation.authors-mismatch', 'Captured provider authors do not match the previewed book.');
        }
    }
    if (!result.source_identity || typeof result.source_identity !== 'object') {
        return formatUiSafetyText('safety.calibre.validation.identity', 'Captured source identity is missing.');
    }
    if (job.candidate) {
        if ((result.source_identity.stable_id || '') !== (job.candidate.stable_id || '')) {
            return formatUiSafetyText('safety.calibre.validation.stable-id-mismatch', 'Captured stable ID does not match the previewed book.');
        }
        if ((result.source_identity.source_url || '') !== (job.candidate.source_url || '')) {
            return formatUiSafetyText('safety.calibre.validation.url-mismatch', 'Captured source URL does not match the previewed book.');
        }
    }
    if (result.config_revision != null && job.configRevision != null && result.config_revision !== job.configRevision) {
        return formatUiSafetyText('safety.calibre.validation.revision-mismatch', 'Captured config revision does not match the preview.');
    }
    return '';
}

function handleCalibreCaptureStatus(job, result) {
    if (!result || result.job_token !== job.jobToken) {
        job.message = formatUiSafetyText('safety.calibre.status.mismatch', 'Status response did not match this job token. The job token is kept; use Check status or Cancel capture.');
        calibreSetJobStatus(job.message);
        renderCalibreJobPanel();
        return;
    }
    job.status = result.status || job.status;
    job.error = result.error || '';
    if (result.review_token) {
        job.reviewToken = result.review_token;
        job.reviewUrl = result.review_destination_url || '';
        job.reviewOperation = result.review_operation || 'capture';
    }
    if (job.status === 'review_required') {
        stopCalibrePoll(job);
        const isCurrentCapture = !job.retired && job.epoch === calibreEpoch && calibreCandidate && job.candidate && calibreCandidate.preview_token === job.candidate.preview_token;
        if (isCurrentCapture && job.reviewToken) {
            renderCalibreReview({
                review_token: job.reviewToken,
                destination_url: job.reviewUrl || '',
                destination_origin: '',
                operation: job.reviewOperation || 'capture',
            }, calibreEpoch);
            job.message = formatUiSafetyText('safety.calibre.review.paused', 'Capture paused for an exact review of the redirect destination. Approve that destination to replay capture once.');
            calibreSetStatus(t('safety.calibre.review.paused.status', 'Capture redirect requires your exact review before capture continues.'));
            calibreSetJobStatus(t('safety.calibre.review.paused.job', 'Capture paused for exact redirect review. Approval replays capture once for this preview.'));
        } else {
            calibreEl('calibre-review-panel').hidden = true;
            calibreReview = null;
            job.message = formatUiSafetyText('safety.calibre.review.retired', 'Capture is waiting for a redirect review that was retired by the view reset. Approve is not available; use Cancel or Check status.');
            calibreSetJobStatus(t('safety.calibre.review.retired.status', 'Capture redirect review was retired by the view reset. Use Cancel or Check status.'));
            if (job.retired && !calibreCancelAlreadySent(job, job.jobToken)) {
                calibreNoteCancelSent(job, job.jobToken);
                job.cancelRequested = true;
                void cancelCalibreCaptureInternal(job);
            }
        }
        renderCalibreJobPanel();
        return;
    }
    if (job.status === 'captured') {
        stopCalibrePoll(job);
        if (job.importOutcomeUnknown) {
            // Server still holds this job as captured, so the lost import
            // never reserved/published; a later explicit import may proceed.
            job.importOutcomeUnknown = false;
            job.importInFlight = false;
            job.importConsumed = false;
        }
        job.unknownStart = false;
        job.confirmationToken = result.confirmation_token || null;
        job.sha256 = result.sha256 || null;
        job.sizeBytes = result.size_bytes != null ? result.size_bytes : null;
        job.provider = result.provider || null;
        job.sourceIdentity = result.source_identity || null;
        job.configRevision = result.config_revision != null ? result.config_revision : job.configRevision;
        job.validationError = calibreValidateCaptured(job, result);
        const isCurrentCapture = !job.retired && !job.cancelRequested && job.epoch === calibreEpoch && calibreCandidate && job.candidate && calibreCandidate.preview_token === job.candidate.preview_token;
        if (!isCurrentCapture) {
            if (job.retired || job.epoch !== calibreEpoch) {
                job.message = formatUiSafetyText('safety.calibre.captured.reset', 'Capture finished after the view reset. Its confirmation will be discarded; import is not available for the retired view.');
                calibreSetJobStatus(t('safety.calibre.captured.reset.status', 'Capture finished after the view reset. Discarding its confirmation; import is not available.'));
            } else {
                job.message = formatUiSafetyText('safety.calibre.captured.cancelled', 'Capture completed after cancel was requested. Its confirmation will be discarded; import is not available.');
                calibreSetJobStatus(t('safety.calibre.captured.cancelled.status', 'Capture completed after cancel was requested. Discarding its confirmation.'));
            }
            renderCalibreJobPanel();
            if (calibreIsHex32(job.confirmationToken) && job.discardSent !== job.confirmationToken) {
                job.discardSent = job.confirmationToken;
                void discardCalibreCaptureInternal(job);
            }
            return;
        }
        if (job.validationError) {
            job.message = formatUiSafetyText('safety.calibre.captured.invalid', 'Captured source did not pass validation. Import is blocked; discard or check status.');
            calibreSetJobStatus(t('safety.calibre.captured.invalid.status', 'Captured source did not pass validation. Import is blocked.'));
        } else {
            job.message = formatUiSafetyText('safety.calibre.capture.review', 'Capture finished. Review the exact captured source, then import explicitly or discard it.');
            calibreSetStatus(t('safety.calibre.capture.review.status', 'Capture finished. Review the captured source before importing. Nothing is imported yet.'));
            calibreSetJobStatus(t('safety.calibre.capture.review.job', 'Capture finished. Review the captured source before importing.'));
        }
        renderCalibreJobPanel();
        return;
    }
    if (calibreIsTerminalCaptureStatus(job.status)) {
        stopCalibrePoll(job);
        job.importOutcomeUnknown = false;
        job.importInFlight = false;
        job.unknownStart = false;
        if (job.status === 'imported' && !job.receipt) {
            job.message = formatUiSafetyText('safety.calibre.imported.no-receipt', 'Job reports imported. The import receipt is shown only from the explicit import response.');
            calibreSetJobStatus(t('safety.calibre.imported.no-receipt.status', 'Job reports imported.'));
        } else if (job.status === 'failed') {
            job.message = formatUiSafetyText('safety.calibre.capture.failed', 'Capture failed: {error}. Use Check status or Cancel capture; no automatic retry will start.', {error: job.error || t('calibre.capture.failed-fallback', 'see status')});
            calibreSetJobStatus(job.message);
        } else {
            job.message = t('calibre.status.capture-ended', 'Capture ended with status {status}.', {status: job.status});
            calibreSetJobStatus(job.message);
        }
        renderCalibreJobPanel();
        return;
    }
    job.message = t('calibre.status.capture-status', 'Capture status: {status}. No byte progress is reported by the backend.', {status: job.status});
    calibreSetJobStatus(job.message);
    renderCalibreJobPanel();
}

async function pollCalibreCapture(job) {
    if (!job || !job.jobToken || job.pollInFlight) {
        return;
    }
    const token = job.jobToken;
    job.pollInFlight = true;
    renderCalibreJobPanel();
    try {
        const result = await API.get(`/api/calibre/capture/status?job_token=${encodeURIComponent(token)}`);
        if (job !== calibreCaptureJob || job.jobToken !== token) {
            return;
        }
        handleCalibreCaptureStatus(job, result);
        if (job.status === 'capturing' || job.status === 'importing') {
            startCalibrePoll(job);
        }
    } catch (error) {
        if (job !== calibreCaptureJob || job.jobToken !== token) {
            return;
        }
        stopCalibrePoll(job);
        job.message = formatUiSafetyText('safety.calibre.status.check-failed', 'Status check failed: {detail}. The job token is kept. Use Check status to retry or Cancel capture; capture will not retry automatically.', {detail: calibreErrorText(error)});
        calibreSetJobStatus(job.message);
        renderCalibreJobPanel();
    } finally {
        // This poll call owns the flag: a poll attempted for a newer token
        // while this one was in flight was refused, so clearing here is
        // what lets the newer generation poll. Only repaint when the
        // token still matches, so a stale completion cannot repaint the
        // new generation with old state.
        job.pollInFlight = false;
        if (job === calibreCaptureJob && job.jobToken === token) {
            renderCalibreJobPanel();
        }
    }
}

async function checkCalibreCaptureStatus() {
    const job = calibreCaptureJob;
    if (!job || !job.jobToken || job.pollInFlight) {
        return;
    }
    await pollCalibreCapture(job);
}

async function cancelCalibreCaptureInternal(job) {
    if (!job || !job.jobToken || job.importInFlight) {
        return;
    }
    const token = job.jobToken;
    calibreNoteCancelSent(job, token);
    job.cancelRequested = true;
    try {
        const result = await API.post('/api/calibre/capture/cancel', { token });
        if (job !== calibreCaptureJob || job.jobToken !== token) {
            return;
        }
        if (!result || result.job_token !== token) {
            job.message = formatUiSafetyText('safety.calibre.cancel.mismatch', 'Cancel response did not match this job token. The job token is kept; use Check status.');
            calibreSetJobStatus(job.message);
            renderCalibreJobPanel();
            return;
        }
        if (result.status) {
            job.status = result.status;
        }
        if (calibreIsTerminalCaptureStatus(job.status)) {
            stopCalibrePoll(job);
            if (job.status === 'cancelled') {
                job.error = t('calibre.cancel.cancelled', 'Capture cancelled.');
                job.message = t('calibre.cancel.terminal', 'Capture cancelled. This terminal status came from the cancel response; local ownership is freed.');
            } else {
                job.message = t('calibre.cancel.terminal-status', 'Cancel returned terminal status {status}; local ownership is freed.', {status: job.status});
            }
            calibreSetJobStatus(job.message);
            renderCalibreJobPanel();
            return;
        }
        if (result.cancelling) {
            job.message = formatUiSafetyText('safety.calibre.cancel.pending', 'Cancel accepted (cancelling). This is not yet cancelled; status will confirm.');
            calibreSetJobStatus(t('safety.calibre.cancel.pending.status', 'Cancel accepted (cancelling). Waiting for status to confirm cancellation.'));
        } else {
            job.message = formatUiSafetyText('safety.calibre.cancel.unconfirmed', 'Cancel returned status {status}; cancellation is not claimed until status says cancelled.', {status: result.status || 'unknown'});
            calibreSetJobStatus(job.message);
        }
        renderCalibreJobPanel();
        if (!calibreIsTerminalCaptureStatus(job.status)) {
            startCalibrePoll(job);
        }
    } catch (error) {
        if (job !== calibreCaptureJob || job.jobToken !== token) {
            return;
        }
        if (error && (error.status === 409 || /discard/i.test(error.message || ''))) {
            job.message = formatUiSafetyText('safety.calibre.cancel.discard-instead', 'Backend says to discard this capture instead of cancelling. Checking status to confirm.');
            calibreSetJobStatus(t('safety.calibre.cancel.discard-instead.status', 'Backend says discard instead. Checking status.'));
            renderCalibreJobPanel();
            await pollCalibreCapture(job);
            return;
        }
        job.message = formatUiSafetyText('safety.calibre.cancel.failed', 'Cancel failed: {detail}. The job token is kept; use Check status or try Cancel again.', {detail: calibreErrorText(error)});
        calibreSetJobStatus(job.message);
        renderCalibreJobPanel();
    }
}

async function cancelCalibreCapture() {
    const job = calibreCaptureJob;
    if (!job) {
        return;
    }
    if (job.status === 'unknown') {
        calibreSetJobStatus(t('safety.calibre.cancel.unknown', 'Capture outcome is UNKNOWN. Cancel is not available until Check status (or an explicit catalog settings save for a tokenless start) confirms the actual status.'));
        return;
    }
    if (job.importInFlight) {
        calibreSetJobStatus(t('safety.calibre.cancel.import-inflight', 'Import is in flight. Cancel is not available and the confirmed import will not be deleted.'));
        return;
    }
    if (job.startPending) {
        job.cancelRequested = true;
        if (job.jobToken && !calibreCancelAlreadySent(job, job.jobToken)) {
            calibreNoteCancelSent(job, job.jobToken);
            job.message = formatUiSafetyText('safety.calibre.cancel.replay-known', 'Cancel requested for the known job token during a pending capture replay. A late replay token will also be cancelled when it arrives.');
            calibreSetJobStatus(t('safety.calibre.cancel.replay-known.status', 'Cancel requested during pending capture replay.'));
            renderCalibreJobPanel();
            void cancelCalibreCaptureInternal(job);
            startCalibrePoll(job);
            return;
        }
        job.message = formatUiSafetyText('safety.calibre.cancel.remembered', 'Cancel remembered for the pending capture start. The late job token will be cancelled when it arrives.');
        calibreSetJobStatus(t('safety.calibre.cancel.remembered.status', 'Cancel remembered for the pending capture start.'));
        renderCalibreJobPanel();
        return;
    }
    if (!job.jobToken) {
        calibreSetJobStatus(t('calibre.cancel.none', 'No capture job token yet. Nothing to cancel.'));
        return;
    }
    await cancelCalibreCaptureInternal(job);
}

async function discardCalibreCaptureInternal(job) {
    if (!job || !calibreIsHex32(job.confirmationToken) || job.importInFlight || job.discardInFlight) {
        return;
    }
    const token = job.confirmationToken;
    job.discardInFlight = true;
    renderCalibreJobPanel();
    try {
        const result = await API.post('/api/calibre/capture/discard', { token });
        if (job !== calibreCaptureJob || job.confirmationToken !== token) {
            return;
        }
        if (!result || result.discarded !== true) {
            throw new Error(t('safety.calibre.discard.unconfirmed', 'Discard was not confirmed.'));
        }
        stopCalibrePoll(job);
        job.status = 'discarded';
        job.confirmationToken = null;
        job.validationError = '';
        job.message = t('calibre.discard.done', 'Discarded the captured staged file. No published book was deleted.');
        calibreSetJobStatus(t('calibre.discard.done.status', 'Discarded the captured staged file.'));
        clearCalibreConfirmationPanel();
        renderCalibreJobPanel();
    } catch (error) {
        if (job !== calibreCaptureJob || job.confirmationToken !== token) {
            return;
        }
        job.message = formatUiSafetyText('safety.calibre.discard.failed', 'Discard failed: {detail}. The confirmation is kept; use Discard, Check status, or Cancel capture.', {detail: calibreErrorText(error)});
        calibreSetJobStatus(job.message);
        renderCalibreJobPanel();
    } finally {
        // This discard owns the flag (a newer discard cannot start while
        // it is set), so always clear it; only repaint for the same
        // confirmation token.
        job.discardInFlight = false;
        if (job === calibreCaptureJob && job.confirmationToken === token) {
            renderCalibreJobPanel();
        }
    }
}

async function discardCalibreCapture() {
    const job = calibreCaptureJob;
    if (!job) {
        return;
    }
    if (job.status === 'unknown') {
        calibreSetJobStatus(t('safety.calibre.discard.unknown', 'Capture outcome is UNKNOWN. Discard is disabled until Check status confirms the actual status is safe to discard.'));
        return;
    }
    if (job.importInFlight) {
        calibreSetJobStatus(t('safety.calibre.discard.import-inflight', 'Import is in flight. Discard is disabled while importing.'));
        return;
    }
    if (!calibreIsHex32(job.confirmationToken)) {
        calibreSetJobStatus(t('calibre.discard.none', 'No captured confirmation to discard.'));
        return;
    }
    job.discardSent = job.confirmationToken;
    await discardCalibreCaptureInternal(job);
}

async function handleCalibreCaptureStartResult(job, result) {
    if (result && result.review_required) {
        job.startPending = false;
        job.status = 'review_required';
        job.reviewToken = result.review_token || null;
        job.reviewUrl = result.destination_url || '';
        job.reviewOperation = result.operation || 'capture';
        stopCalibrePoll(job);
        if (job.retired || job.epoch !== calibreEpoch || !calibreCandidate) {
            job.status = 'stale';
            job.message = formatUiSafetyText('safety.calibre.capture-review.retired', 'Capture review arrived after the view reset and was retired. No capture started.');
            calibreEl('calibre-review-panel').hidden = true;
            calibreReview = null;
            calibreSetJobStatus(t('safety.calibre.capture-review.retired.status', 'Capture review arrived after the view reset and was retired. No capture started.'));
            renderCalibreJobPanel();
            return;
        }
        renderCalibreReview(result, job.epoch);
        job.message = formatUiSafetyText('safety.calibre.review.capture', 'Cross-origin capture destination requires your exact review before capture starts.');
        calibreSetStatus(t('safety.calibre.review.capture.status', 'Cross-origin capture destination requires your exact review before capture starts.'));
        calibreSetJobStatus(t('safety.calibre.review.capture.job', 'Capture waiting for exact cross-origin review.'));
        renderCalibreJobPanel();
        return;
    }
    const token = result && result.job_token;
    if (!calibreIsHex32(token)) {
        calibreMarkUnknownCaptureStart(job, t('safety.calibre.capture.invalid-token', 'Capture start did not return a valid job token.'), t('calibre.capture.start-label', 'Capture start'));
        return;
    }
    stopCalibrePoll(job);
    job.jobToken = token;
    job.startPending = false;
    job.status = result.status || 'capturing';
    job.reviewToken = null;
    job.reviewUrl = '';
    job.reviewOperation = '';
    job.confirmationToken = null;
    job.sha256 = null;
    job.sizeBytes = null;
    job.provider = null;
    job.sourceIdentity = null;
    job.validationError = '';
    job.receipt = null;
    if (job.retired || job.cancelRequested || job.epoch !== calibreEpoch) {
        job.message = formatUiSafetyText('safety.calibre.capture.late-token', 'Late capture job token arrived after the view reset. Cancelling this original capture and checking its status; it will not attach to the new view.');
        calibreEl('calibre-review-panel').hidden = true;
        calibreReview = null;
        calibreSetJobStatus(t('safety.calibre.capture.late-token.status', 'Late capture job token arrived after the view reset. Cancelling and checking it.'));
        job.cancelRequested = true;
        if (!calibreCancelAlreadySent(job, token)) {
            calibreNoteCancelSent(job, token);
            await cancelCalibreCaptureInternal(job);
        }
        await pollCalibreCapture(job);
        if (job.status === 'capturing') {
            startCalibrePoll(job);
        }
        renderCalibreJobPanel();
        return;
    }
    job.message = t('calibre.status.capture-started', 'Capture started. Checking status; nothing is imported yet.');
    calibreSetStatus(t('calibre.status.capture-started', 'Capture started. Checking status; nothing is imported yet.'));
    calibreSetJobStatus(t('calibre.status.capture-started.job', 'Capture started. Checking status.'));
    renderCalibreJobPanel();
    startCalibrePoll(job);
}

async function startCalibreCapture() {
    if (calibreBusy) { return; }
    if (!calibreCandidate) {
        calibreSetStatus(t('calibre.capture.preview-first', 'Preview a book first, then capture that exact source.'));
        return;
    }
    if (calibreCaptureJob && calibreJobIsLive(calibreCaptureJob)) {
        if (calibreCaptureJob.status === 'unknown') {
            calibreSetJobStatus(t('safety.calibre.capture.unknown-start', 'Capture outcome is UNKNOWN. Do not start another capture; use Check status if a job token is shown, or explicitly save the catalog settings to retire this local view after a successful save.'));
        } else {
            calibreSetJobStatus(t('safety.calibre.capture.in-progress', 'A capture is already in progress. Wait for it, cancel it, or discard it before starting another.'));
        }
        return;
    }
    if (calibreCaptureJob) {
        stopCalibrePoll(calibreCaptureJob);
    }
    const candidate = {
        preview_token: calibreCandidate.preview_token,
        config_revision: calibreCandidate.config_revision,
        provider: calibreCopyProvider(calibreCandidate.provider),
        stable_id: calibreCandidate.stable_id,
        feed_url: calibreCandidate.feed_url,
        source_url: calibreCandidate.source_url,
        epoch: calibreEpoch,
    };
    if (!calibreIsHex32(candidate.preview_token) || !candidate.provider) {
        calibreSetStatus(t('safety.calibre.capture.invalid-preview', 'Preview identity is invalid. Preview the book again before capturing.'));
        return;
    }
    const job = {
        jobToken: null, startPending: true, startGeneration: 1, epoch: calibreEpoch, candidate,
        configRevision: candidate.config_revision, status: 'starting',
        confirmationToken: null, sha256: null, sizeBytes: null, provider: null,
        sourceIdentity: null, reviewToken: null, reviewUrl: '', reviewOperation: '',
        pollInFlight: false, timer: null, importInFlight: false, importConsumed: false,
        importEpoch: null, cancelRequested: false, cancelSent: false, cancelSentTokens: {},
        discardSent: null, discardInFlight: false, retired: false,
        receipt: null, error: '', validationError: '', message: t('calibre.status.starting-capture', 'Starting explicit capture for the previewed source…'),
    };
    calibreCaptureJob = job;
    calibreBusy = true;
    updateCalibreControls();
    calibreSetStatus(t('calibre.status.starting-capture', 'Starting explicit capture for the previewed source…'));
    calibreSetJobStatus(t('calibre.status.starting-capture', 'Starting explicit capture for the previewed source…'));
    renderCalibreJobPanel();
    try {
        const result = await API.post('/api/calibre/capture', { token: candidate.preview_token });
        if (job !== calibreCaptureJob) {
            return;
        }
        await handleCalibreCaptureStartResult(job, result);
    } catch (error) {
        if (job !== calibreCaptureJob) {
            return;
        }
        if (calibreIsDefinitiveCaptureRejection(error)) {
            job.startPending = false;
            job.status = 'failed';
            job.error = calibreErrorText(error);
            job.message = formatUiSafetyText('safety.calibre.capture.start-failed', 'Capture start failed: {detail}. This rejection was confirmed before a job started. No automatic retry will start.', {detail: job.error});
            calibreSetJobStatus(job.message);
            renderCalibreJobPanel();
        } else {
            calibreMarkUnknownCaptureStart(job, calibreErrorText(error), t('calibre.capture.start-label', 'Capture start'));
        }
    } finally {
        if (calibreIsCurrent(job.epoch)) {
            calibreBusy = false;
        }
        renderCalibreJobPanel();
    }
}

async function importCalibreCapture() {
    const job = calibreCaptureJob;
    if (!job || job.importInFlight || job.importConsumed) {
        return;
    }
    if (!calibreCanImport(job)) {
        calibreSetJobStatus(t('safety.calibre.import.unavailable', 'Import is not available until this capture is bound to the current preview and has a valid hash and confirmation.'));
        return;
    }
    job.importInFlight = true;
    job.importConsumed = true;
    job.importEpoch = calibreEpoch;
    const confirmation = job.confirmationToken;
    const sha = job.sha256;
    const isImportCurrent = () => job === calibreCaptureJob && !job.retired && job.importEpoch === calibreEpoch && Boolean(calibreCandidate) && Boolean(job.candidate) && calibreCandidate.preview_token === job.candidate.preview_token;
    updateCalibreControls();
    calibreSetStatus(t('calibre.status.importing', 'Importing the captured source exactly once…'));
    calibreSetJobStatus(t('calibre.status.importing', 'Importing the captured source exactly once…'));
    renderCalibreJobPanel();
    try {
        const result = await API.post('/api/calibre/import', { confirmation_token: confirmation, sha256: sha });
        if (job !== calibreCaptureJob) {
            return;
        }
        if (!result || !result.upload || typeof result.upload.stored_filename !== 'string' || !result.upload.stored_filename) {
            throw new Error(formatUiSafetyText('safety.calibre.import.missing-filename', 'Import receipt is missing the stored filename.'));
        }
        if (result.sha256 !== sha) {
            throw new Error(formatUiSafetyText('safety.calibre.import.sha-mismatch', 'Import receipt SHA-256 did not match the captured source.'));
        }
        if (result.size_bytes != null && job.sizeBytes != null && result.size_bytes !== job.sizeBytes) {
            throw new Error(formatUiSafetyText('safety.calibre.import.size-mismatch', 'Import receipt size did not match the captured source.'));
        }
        if (!result.provider || !result.source_identity) {
            throw new Error(formatUiSafetyText('safety.calibre.import.missing-identity', 'Import receipt is missing provider or source identity.'));
        }
        job.status = 'imported';
        job.receipt = result;
        job.error = '';
        job.message = t('calibre.import.done', 'Imported. The receipt below is the finished outcome; importing did not select a book.');
        stopCalibrePoll(job);
        // Keep importInFlight true through the inventory refresh so a new
        // capture cannot replace this job while completion is still
        // pending; ownership is released only after the refresh settles.
        renderCalibreJobPanel();
        let refreshError = null;
        try {
            if (typeof loadExistingScriptUploads === 'function') {
                await loadExistingScriptUploads();
            }
        } catch (error) {
            refreshError = error;
        }
        job.importInFlight = false;
        if (isImportCurrent()) {
            if (refreshError) {
                job.message = formatUiSafetyText('safety.calibre.import.refresh-failed', 'Imported as {file}, but existing uploads could not be refreshed: {detail}. Choose the file from existing uploads after refreshing.', {file: result.upload.stored_filename, detail: calibreErrorText(refreshError)});
                calibreSetJobStatus(job.message);
                renderCalibreJobPanel();
            }
            if (typeof renderEpubExtractionReport === 'function' && result.upload.extraction_report) {
                renderEpubExtractionReport(result.upload.extraction_report);
            }
            calibreSetStatus(t('calibre.status.imported', 'Imported {file}. Choose it from existing uploads to select it; importing did not select a book.', {file: result.upload.stored_filename}));
            clearCalibreConfirmationPanel();
            renderCalibreJobPanel();
        } else {
            job.message = joinUiSentences([formatUiSafetyText('safety.calibre.import.after-reset', 'Import finished after a view reset. The receipt stays in this pane; the new view was not changed and no book was selected.'),
                refreshError ? formatUiSafetyText('safety.calibre.import.after-reset-refresh', 'Existing uploads could not be refreshed: {detail}.', {detail: calibreErrorText(refreshError)}) : '']);
            if (job === calibreCaptureJob) {
                calibreSetJobStatus(job.message);
                renderCalibreJobPanel();
            }
        }
    } catch (error) {
        if (job !== calibreCaptureJob) {
            return;
        }
        calibreMarkUnknownImport(job, calibreErrorText(error));
        clearCalibreConfirmationPanel();
        renderCalibreJobPanel();
    } finally {
        if (job === calibreCaptureJob) {
            renderCalibreJobPanel();
        }
    }
}

async function approveCalibreReview() {
    const review = calibreReview;
    if (calibreBusy || !review) { return; }
    const epoch = review.epoch;
    if (!calibreIsCurrent(epoch)) { return; }
    calibreReview = null;
    calibreBusy = true;
    calibreEl('calibre-review-panel').hidden = true;
    updateCalibreControls();
    if (review.operation === 'capture') {
        calibreSetStatus(t('calibre.review.approved-capture', 'Approved exactly the reviewed destination. Replaying capture for the same preview…'));
    } else {
        calibreSetStatus(t('calibre.review.approved-browse', 'Approved exactly the reviewed destination. Replaying the pending browse step…'));
    }
    try {
        const result = await API.post('/api/calibre/review/approve', { token: review.review_token });
        if (!calibreIsCurrent(epoch)) {
            if (review.operation === 'capture') {
                const job = calibreCaptureJob;
                if (job) {
                    job.message = formatUiSafetyText('safety.calibre.review.approval-retired', 'Capture approval arrived after the view reset and was not replayed. No capture was started from that approval.');
                    calibreSetJobStatus(job.message);
                    renderCalibreJobPanel();
                }
            }
            return;
        }
        if (review.operation === 'capture') {
            const previewToken = result && result.preview_token;
            const job = calibreCaptureJob;
            const expected = (job && job.candidate && job.candidate.preview_token) || (calibreCandidate && calibreCandidate.preview_token);
            if (!calibreIsHex32(previewToken) || previewToken !== expected || !calibreCandidate || calibreCandidate.preview_token !== previewToken || !job || job.retired || job.epoch !== epoch) {
                calibreSetStatus(t('safety.calibre.review.approval-mismatch', 'Capture approval did not match the current preview. Capture again after previewing the book.'));
                if (job) {
                    job.message = formatUiSafetyText('safety.calibre.review.approval-unbound', 'Capture approval was not bound to the current candidate; no capture was replayed.');
                    calibreSetJobStatus(job.message);
                    renderCalibreJobPanel();
                }
                return;
            }
            // Register pending replay ownership BEFORE the await so a reset
            // during the replay POST latches cancellation for the late
            // token (per-token, even if the old token was already cancelled).
            job.startPending = true;
            job.startGeneration = (job.startGeneration || 0) + 1;
            const replayGeneration = job.startGeneration;
            renderCalibreJobPanel();
            let replay;
            try {
                replay = await API.post('/api/calibre/capture', { token: previewToken });
            } catch (replayError) {
                if (job !== calibreCaptureJob) {
                    return;
                }
                if (job.startGeneration !== replayGeneration) {
                    return;
                }
                if (calibreIsDefinitiveCaptureRejection(replayError)) {
                    job.startPending = false;
                    job.status = 'failed';
                    job.error = calibreErrorText(replayError);
                    job.message = formatUiSafetyText('safety.calibre.capture.replay-failed', 'Capture replay failed: {detail}. This rejection was confirmed before a new job started. No automatic retry will start.', {detail: job.error});
                    calibreSetJobStatus(job.message);
                    renderCalibreJobPanel();
                } else {
                    calibreMarkUnknownCaptureStart(job, calibreErrorText(replayError), t('calibre.capture.replay-label', 'Capture replay'));
                }
                return;
            }
            if (job !== calibreCaptureJob) {
                return;
            }
            if (job.startGeneration !== replayGeneration) {
                // Superseded generation: cancel its token without
                // attaching it to the current job view.
                const orphanToken = replay && replay.job_token;
                if (calibreIsHex32(orphanToken)) {
                    try {
                        await API.post('/api/calibre/capture/cancel', { token: orphanToken });
                    } catch (orphanError) {
                        // The orphan token is already detached; nothing
                        // truthful can be shown for it in this view.
                    }
                }
                return;
            }
            await handleCalibreCaptureStartResult(job, replay);
            return;
        }
        const navToken = result.nav_token;
        if (!navToken) {
            calibreSetStatus(t('calibre.review.no-token', 'Approval did not return a browse token. Browse the catalog again.'));
            return;
        }
        await requestCalibreBrowse('/api/calibre/browse-next', { token: navToken }, epoch);
    } catch (error) {
        if (!calibreIsCurrent(epoch)) { return; }
        calibreSetStatus(calibreErrorText(error));
        if (review.operation === 'capture' && calibreCaptureJob) {
            calibreCaptureJob.message = formatUiSafetyText('safety.calibre.review.approval-failed', 'Capture approval failed: {detail}. No capture was replayed from that approval.', {detail: calibreErrorText(error)});
            calibreSetJobStatus(calibreCaptureJob.message);
            renderCalibreJobPanel();
        }
    } finally {
        if (calibreIsCurrent(epoch)) {
            calibreBusy = false;
            updateCalibreControls();
        }
    }
}

function cancelCalibreReview() {
    clearCalibreView();
    calibreSetStatus(t('calibre.review.cancelled', 'Cross-origin review cancelled. Browse the catalog again to continue.'));
}

async function previewCalibreBook(previewToken) {
    if (calibreBusy || !previewToken || !/^[0-9a-f]{32}$/.test(previewToken)) { return; }
    calibreEpoch += 1;
    clearCalibreDecisionPanels();
    const epoch = calibreEpoch;
    calibreBusy = true;
    updateCalibreControls();
    calibreSetStatus(t('calibre.status.loading-preview', 'Loading the pure metadata preview (explicit request)…'));
    try {
        const result = await API.get(`/api/calibre/preview?preview_token=${encodeURIComponent(previewToken)}`);
        if (!calibreIsCurrent(epoch)) { return; }
        const provider = result.provider || {};
        const authors = Array.isArray(provider.authors) ? provider.authors.join(', ') : '';
        const series = provider.series ? `${provider.series}${provider.series_index != null ? ` #${provider.series_index}` : ''}` : '';
        calibreEl('calibre-preview-out').textContent = [
            t('calibre.provider.title', 'Title: {value}', {value: provider.title || ''}),
            t('calibre.provider.authors', 'Authors: {value}', {value: authors}),
            t('calibre.provider.series', 'Series: {value}', {value: series}),
            t('calibre.provider.subtitle', 'Subtitle: {value}', {value: provider.subtitle || ''}),
            t('calibre.provider.format', 'Format: {value}', {value: provider.format || ''}),
            t('calibre.preview.stable-id', 'Stable ID: {value}', {value: result.stable_id || ''}),
            t('calibre.preview.feed', 'Feed: {value}', {value: result.feed_url || ''}),
            t('calibre.preview.source', 'Source: {value}', {value: result.source_url || ''}),
            formatUiSafetyText('safety.calibre.preview.readonly', 'Read-only preview. This is not permission to download, capture, import, or synthesize.'),
        ].join('\n');
        calibreEl('calibre-preview-panel').hidden = false;
        if (calibreIsHex32(result.preview_token) && result.provider && typeof result.provider === 'object') {
            calibreCandidate = {
                preview_token: result.preview_token,
                config_revision: result.config_revision,
                provider: calibreCopyProvider(result.provider),
                stable_id: result.stable_id || '',
                feed_url: result.feed_url || '',
                source_url: result.source_url || '',
                epoch,
            };
        } else {
            calibreCandidate = null;
        }
        calibreSetStatus(t('calibre.status.preview-loaded', 'Metadata preview loaded. Nothing has been downloaded. Capture is available only as an explicit next step.'));
    } catch (error) {
        if (!calibreIsCurrent(epoch)) { return; }
        calibreSetStatus(calibreErrorText(error));
    } finally {
        if (calibreIsCurrent(epoch)) {
            calibreBusy = false;
            updateCalibreControls();
        }
    }
}
