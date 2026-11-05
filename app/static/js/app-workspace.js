// Workspace navigation paints existing state; it never starts or resumes work.
const WORKSPACE_TASK_TABS = {
    script: 'script', batch_script: 'script', review: 'script', batch_review: 'script',
    nicknames: 'script', cast_list: 'script', persona: 'voices', voices: 'voices',
    audio: 'audio', drift_check: 'editor', outlier_check: 'editor', word_timing: 'editor', speech_verification: 'editor', book_preflight: 'script',
    audacity_export: 'audio', m4b_export: 'audio', chapter_export: 'audio',
    lora_training: 'training', lora_test: 'training', voicelab: 'voicelab',
    preparer: 'preparer', batch_preparer: 'preparer', dataset_builder: 'dataset-builder',
    voice_design: 'designer', speaker_review: 'script', report_explanation: 'reports', benchmark: 'reports', llm_test: 'setup', lmstudio_optimize: 'setup'
};
let workspaceBookRequest = 0;
let workspaceBookSnapshot = null;
let workspaceBookPending = null;
let workspaceMonitorEpoch = 0;
let workspaceContinuePending = false;
let workspaceMonitorPending = false;
let workspacePollTimer = null;
let workspacePreviousRunning = false;
const workspaceTaskViews = new Map();
let workspaceLibrarySignature = null;
let collectionOverviewRequest = 0;
let collectionOverviewPending = null;
let collectionOverviewLimit = 50;

function isWorkspaceVisible() {
    return currentTabName === 'home' && document.visibilityState !== 'hidden';
}

function renderWorkspaceStage(title, status, explanation) {
    const card = document.createElement('section');
    card.className = 'workspace-stage';
    const heading = document.createElement('h6');
    heading.textContent = title;
    const value = document.createElement('p');
    value.textContent = status;
    const note = document.createElement('div');
    note.className = 'small text-muted';
    note.textContent = explanation;
    card.append(heading, value, note);
    return card;
}

function renderBookWorkspace(data) {
    document.getElementById('workspace-book-name').textContent = data.name;
    document.getElementById('workspace-next-reason').textContent = data.next.reason;
    const button = document.getElementById('workspace-continue');
    button.textContent = data.next.label;
    button.disabled = workspaceContinuePending;
    const audio = data.audio;
    const review = audio.review;
    const speech = review?.status === 'observed'
        ? ` Saved speech checks: ${review.speech_current_metadata} match current row/audio metadata; ${review.speech_not_current} no longer match; ${review.speech_not_checked} unchecked. ${review.speech_attention} need speech review; ${review.drift_flagged} have drift flags; ${review.unknown_speakers} have unknown speakers.`
        : review?.status === 'unavailable' ? ' Saved review evidence could not be read.' : '';
    const exported = data.export_review;
    const exportNote = exported?.status === 'observed'
        ? `${exported.verified_at_export} passed at export; ${exported.review_at_export} needed review; ${exported.unavailable} receipts no longer match output metadata; ${exported.failed_attempts} failed attempts; ${exported.running} running. Checks are from this app session. Current script and voice inputs have not been rechecked; confirm book content before sharing.`
        : 'Saved export checks are unavailable. Check that each output belongs to this book before sharing it.';
    const outputs = data.outputs.map(row => `${row.format}: ${row.status === 'present_unverified'
        ? 'file present; book match unverified' : row.status === 'historical_source_scope'
            ? 'older scope; rebuild before download' : row.status === 'unavailable' ? 'could not inspect' : 'no file found'}`).join(' · ');
    document.getElementById('workspace-stages').replaceChildren(
        renderWorkspaceStage('Script', data.script.entries ? `${data.script.entries} entries saved` : 'Not started', data.script.recovery?.status === 'candidate'
            ? 'Interrupted script run has saved recovery evidence. Inspect it in Script; source and settings eligibility must be rechecked before resuming.'
            : 'Saved annotations for the selected book.'),
        renderWorkspaceStage('Cast', `${data.cast.total} characters · ${data.cast.pending} need preparation`, 'Finish preparing characters in Voices, then listen to their samples.'),
        renderWorkspaceStage('Audio', audio.status === 'unavailable' ? 'Progress unavailable'
            : audio.total ? `${audio.present} / ${audio.total} files present` : 'Not prepared',
            `${audio.message}${speech}${audio.failed || audio.missing ? ` ${audio.failed} failed; ${audio.missing} done rows have missing audio.` : ''}`),
        renderWorkspaceStage('Export', outputs || 'Not inspected', exportNote)
    );
}

function renderWorkspaceLibrary(books) {
    const signature = JSON.stringify(books.map(book => [book.name, book.has_voice_config]));
    if (signature === workspaceLibrarySignature) { return; }
    workspaceLibrarySignature = signature;
    const container = document.getElementById('workspace-library');
    const selected = new Set(collectionSelectedNames());
    container.replaceChildren();
    if (!books.length) { container.textContent = 'No saved books yet. Save the current script from Script.'; return; }
    for (const book of books) {
        const row = document.createElement('div');
        row.className = 'workspace-library-row';
        const title = document.createElement('strong');
        title.textContent = book.name;
        const description = document.createElement('div');
        description.className = 'small text-muted mb-2';
        description.textContent = `Script saved · ${book.has_voice_config ? 'voice settings file saved' : 'no saved voice settings file'}`;
        const load = document.createElement('button');
        load.type = 'button';
        load.className = 'btn btn-outline-secondary btn-sm';
        load.textContent = 'Load saved book…';
        load.setAttribute('data-help-key', 'openWorkspaceSavedBook');
        load.addEventListener('click', () => openWorkspaceSavedBook(book.name, load));
        const checkLabel = document.createElement('label');
        checkLabel.className = 'small d-block mt-2';
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.setAttribute('data-collection-select', book.name);
        check.checked = selected.has(book.name);
        const checkText = document.createElement('span');
        checkText.textContent = ` Select ${book.name} for collection overview`;
        checkLabel.append(check, checkText);
        row.append(title, description, load, checkLabel);
        container.appendChild(row);
    }
}

function collectionSelectedNames() {
    return [...document.getElementById('workspace-library').querySelectorAll('input[data-collection-select]')]
        .filter(box => box.checked).map(box => box.getAttribute('data-collection-select'));
}

function isCollectionDigest(value) {
    return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
}

function isCollectionRowValid(row) {
    const stages = [row?.script, row?.review, row?.voices, row?.render];
    const revisionValid = row && (row.revision === null || isCollectionDigest(row.revision));
    return !!row && typeof row.name === 'string' && typeof row.book_id === 'string'
        && typeof row.is_active === 'boolean' && typeof row.overall === 'string'
        && stages.every(stage => stage && typeof stage.status === 'string' && typeof stage.evidence === 'string')
        && typeof row.next?.tab === 'string' && typeof row.next?.label === 'string'
        && typeof row.next?.reason === 'string' && revisionValid;
}

function isCollectionRowLoadable(row) {
    return !!row && isCollectionDigest(row.revision) && isCollectionDigest(row.script?.script_sha256)
        && row.script?.status !== 'missing' && row.loadable !== false;
}

function renderCollectionOverview(rows) {
    const container = document.getElementById('workspace-collection');
    container.replaceChildren();
    const table = document.createElement('table');
    table.className = 'table table-sm';
    const head = document.createElement('thead');
    const headRow = document.createElement('tr');
    for (const label of ['Book', 'Stage', 'Evidence', 'Next step', 'Open']) {
        const cell = document.createElement('th');
        cell.scope = 'col';
        cell.textContent = label;
        headRow.appendChild(cell);
    }
    head.appendChild(headRow);
    table.append(head);
    const body = document.createElement('tbody');
    for (const row of rows) {
        const line = document.createElement('tr');
        const bookCell = document.createElement('td');
        const bookTitle = document.createElement('strong');
        bookTitle.textContent = row.name;
        const bookId = document.createElement('div');
        bookId.className = 'small text-muted';
        bookId.textContent = `ID ${row.book_id}${row.is_active ? ' · active' : ''} · revision ${isCollectionDigest(row.revision) ? row.revision.slice(0, 12) : 'unknown'}`;
        bookCell.append(bookTitle, bookId);
        const stageCell = document.createElement('td');
        stageCell.textContent = row.overall;
        const evidenceCell = document.createElement('td');
        const evidence = document.createElement('ul');
        evidence.className = 'small mb-0';
        for (const [label, stage] of [['Script', row.script], ['Review', row.review], ['Voices', row.voices], ['Render/export', row.render]]) {
            const item = document.createElement('li');
            item.textContent = `${label}: ${stage.status} — ${stage.evidence}`;
            evidence.appendChild(item);
        }
        evidenceCell.appendChild(evidence);
        const nextCell = document.createElement('td');
        nextCell.className = 'small';
        nextCell.textContent = `${row.next.label} — ${row.next.reason}`;
        const openCell = document.createElement('td');
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'btn btn-outline-secondary btn-sm';
        open.textContent = 'Load this book…';
        open.setAttribute('data-help-key', 'openWorkspaceSavedBook');
        if (!isCollectionRowLoadable(row)) {
            open.disabled = true;
            open.title = row.loadable === false ? row.script.evidence
                : 'No valid revision proof for this book; show the overview again before loading.';
        }
        open.addEventListener('click', () => openCollectionBook(row, open));
        openCell.appendChild(open);
        line.append(bookCell, stageCell, evidenceCell, nextCell, openCell);
        body.appendChild(line);
    }
    table.appendChild(body);
    container.appendChild(table);
}

async function showCollectionOverview() {
    const selectionEpoch = window._bookSelectionEpoch || 0;
    const names = collectionSelectedNames();
    const signature = JSON.stringify([selectionEpoch, names]);
    if (collectionOverviewPending?.signature === signature) { return null; }
    const request = ++collectionOverviewRequest;
    const feedback = document.getElementById('workspace-collection-feedback');
    const container = document.getElementById('workspace-collection');
    container.replaceChildren();
    if (!names.length) {
        feedback.textContent = 'Select at least one saved book above first.';
        return null;
    }
    if (names.length > collectionOverviewLimit) {
        feedback.textContent = `Select at most ${collectionOverviewLimit} books for the collection overview; deselect some books and try again.`;
        return null;
    }
    feedback.textContent = 'Reading the selected books…';
    const pending = { signature };
    collectionOverviewPending = pending;
    const show = document.getElementById('workspace-collection-show');
    if (show) { show.disabled = true; }
    try {
        const rows = await API.post('/api/collection/overview', { names });
        if (request !== collectionOverviewRequest) { return null; }
        if (selectionEpoch !== (window._bookSelectionEpoch || 0)
                || JSON.stringify(names) !== JSON.stringify(collectionSelectedNames())) {
            feedback.textContent = 'The book or collection selection changed. Show the overview again.';
            return null;
        }
        if (!Array.isArray(rows) || !rows.every(isCollectionRowValid)
                || JSON.stringify(rows.map(row => row.name)) !== JSON.stringify(names)) {
            feedback.textContent = 'Collection overview was incomplete. Nothing was loaded; refresh the selection and try again.';
            return null;
        }
        renderCollectionOverview(rows);
        feedback.textContent = `Collection overview updated for ${rows.length} book${rows.length === 1 ? '' : 's'}. Opening it started no work.`;
        return rows;
    } catch (error) {
        if (request !== collectionOverviewRequest) { return null; }
        feedback.textContent = `Collection overview could not be read: ${error.message || 'request failed'}. No book was switched.`;
        return null;
    } finally {
        if (collectionOverviewPending === pending) {
            collectionOverviewPending = null;
            if (show) { show.disabled = false; }
        }
    }
}

async function openCollectionBook(row, button) {
    if (!isCollectionRowLoadable(row)) {
        document.getElementById('workspace-collection-feedback').textContent =
            row.loadable === false ? row.script.evidence
                : 'This book has no valid revision proof. Nothing was loaded; show the overview again before loading.';
        return;
    }
    // The confirmed load rechecks this proof under admission before publishing.
    await openWorkspaceSavedBook(row.name, button, row.revision);
}

function isWorkspaceSnapshotValid(data) {
    const count = value => Number.isSafeInteger(value) && value >= 0;
    const review = data?.audio?.review;
    const exported = data?.export_review;
    const recovery = data?.script?.recovery;
    const recoveryValid = !recovery || (['candidate', 'not_reported'].includes(recovery.status)
        && recovery.eligibility === 'not_rechecked');
    const reviewValid = !review || ['not_prepared', 'unavailable'].includes(review.status)
        || (review.status === 'observed' && ['speech_current_metadata', 'speech_not_current',
            'speech_not_checked', 'speech_attention', 'drift_flagged', 'unknown_speakers'].every(key => count(review[key])));
    const exportValid = !exported || exported.status === 'unavailable'
        || (exported.status === 'observed' && exported.current_inputs === 'not_rechecked'
            && ['verified_at_export', 'review_at_export', 'unavailable', 'failed_attempts', 'running'].every(key => count(exported[key])));
    return recoveryValid && reviewValid && exportValid && !!data && typeof data.name === 'string' && /^[a-f0-9]{64}$/.test(data.book_token)
        && count(data.script?.entries) && count(data.cast?.total) && count(data.cast?.pending)
        && data.cast.pending <= data.cast.total
        && ['not_started', 'files_present', 'needs_attention', 'unavailable'].includes(data.audio?.status)
        && typeof data.audio.message === 'string'
        && ['total', 'present', 'failed', 'missing', 'generating'].every(key => count(data.audio[key]))
        && data.audio.present <= data.audio.total
        && Array.isArray(data.outputs) && data.outputs.every(row => row && typeof row.format === 'string'
            && ['present_unverified', 'historical_source_scope', 'not_found', 'unavailable'].includes(row.status))
        && ['script', 'voices', 'editor', 'audio'].includes(data.next?.tab)
        && typeof data.next.label === 'string' && typeof data.next.reason === 'string';
}

async function refreshBookWorkspace() {
    const request = ++workspaceBookRequest;
    workspaceBookSnapshot = null;
    const button = document.getElementById('workspace-continue');
    button.disabled = true;
    if (workspaceBookPending) { await workspaceBookPending; }
    if (request !== workspaceBookRequest) { return null; }
    const pending = Promise.allSettled([API.get('/api/workspace'), API.get('/api/scripts')]);
    workspaceBookPending = pending;
    const [book, library] = await pending;
    if (workspaceBookPending === pending) { workspaceBookPending = null; }
    if (request !== workspaceBookRequest) { return null; }
    if (library.status === 'fulfilled' && Array.isArray(library.value)
            && library.value.every(book => book && typeof book.name === 'string' && typeof book.has_voice_config === 'boolean')) {
        renderWorkspaceLibrary(library.value);
        document.getElementById('workspace-library-feedback').textContent = '';
    } else {
        document.getElementById('workspace-library-feedback').textContent = 'Saved books could not be refreshed. Any displayed list is from an earlier reading.';
    }
    if (book.status !== 'fulfilled') {
        document.getElementById('workspace-feedback').textContent = 'Book status could not be refreshed. Any displayed facts are from an earlier reading. Continue is unavailable until Refresh succeeds.';
        return null;
    }
    const data = book.value;
    if (!isWorkspaceSnapshotValid(data)) {
        document.getElementById('workspace-feedback').textContent = 'Book status was incomplete. Continue is unavailable; refresh before proceeding.';
        return null;
    }
    workspaceBookSnapshot = data;
    if (Number.isSafeInteger(data.collection_overview_limit) && data.collection_overview_limit > 0) {
        collectionOverviewLimit = data.collection_overview_limit;
    }
    renderBookWorkspace(data);
    document.getElementById('workspace-feedback').textContent = `Book status updated at ${new Date().toLocaleTimeString()}.`;
    return data;
}

async function continueBookWorkspace() {
    if (workspaceContinuePending) { return; }
    workspaceContinuePending = true;
    const epoch = workspaceMonitorEpoch;
    document.getElementById('workspace-continue').disabled = true;
    try {
        const data = await refreshBookWorkspace();
        if (data && isWorkspaceVisible() && epoch === workspaceMonitorEpoch) { activateTab(data.next.tab); }
    } finally {
        workspaceContinuePending = false;
        document.getElementById('workspace-continue').disabled = !workspaceBookSnapshot;
    }
}

async function openWorkspaceSavedBook(name, button, expectedRevision = null) {
    button.disabled = true;
    try { await loadScript(name, expectedRevision); }
    catch (error) { showActionError('Could not load the saved book', error, 'Review the currently selected book before trying Load again; its reply may have been lost.'); }
    finally { button.disabled = false; await refreshBookWorkspace(); }
}

function getWorkspaceTaskTab(name) {
    return Object.hasOwn(WORKSPACE_TASK_TABS, name) ? WORKSPACE_TASK_TABS[name] : null;
}

async function openWorkspaceTask(name) {
    const tab = getWorkspaceTaskTab(name);
    if (!tab) { return; }
    const epoch = workspaceMonitorEpoch;
    await reattachRunningPollers();
    if (isWorkspaceVisible() && epoch === workspaceMonitorEpoch) { activateTab(tab); }
}

function renderWorkspaceTask(name, state, stale = false) {
    let view = workspaceTaskViews.get(name);
    if (!view) {
        const card = document.createElement('article');
        card.className = 'workspace-task';
        const title = document.createElement('h6');
        title.textContent = Object.hasOwn(TASK_LABELS, name) ? TASK_LABELS[name] : name.replaceAll('_', ' ');
        const activity = document.createElement('p');
        activity.className = 'mb-2';
        const timing = document.createElement('p');
        timing.className = 'small text-muted';
        const controls = document.createElement('button');
        controls.type = 'button'; controls.className = 'btn btn-outline-secondary btn-sm';
        controls.textContent = 'Open task screen';
        controls.hidden = !getWorkspaceTaskTab(name);
        controls.setAttribute('data-help-key', 'openWorkspaceTask');
        controls.addEventListener('click', () => openWorkspaceTask(name));
        const details = document.createElement('details');
        details.className = 'mt-3';
        const summary = document.createElement('summary');
        summary.textContent = 'Recent task log';
        const logs = document.createElement('pre');
        details.append(summary, logs); card.append(title, activity, timing, controls, details);
        document.getElementById('workspace-monitor').appendChild(card);
        view = {card, activity, timing, logs, track: {}};
        workspaceTaskViews.set(name, view);
    }
    if (stale) {
        view.timing.textContent = 'Task details could not be refreshed. Any log shown is from an earlier reading; task may still be running.';
        return;
    }
    const identity = state.claim_id || state.run_id || state.start_time;
    if (identity != null && identity !== view.identity) { view.identity = identity; view.track = {}; }
    const logs = Array.isArray(state.logs) ? state.logs.filter(line => typeof line === 'string').slice(-40).map(line => line.slice(0, 2000)) : [];
    renderActivity(view.activity, {...state, logs}, view.track);
    if (!state.running) { view.activity.hidden = false; view.activity.textContent = 'No longer running; refreshing the task list.'; }
    if (state.cancel) { view.activity.textContent = 'Cancellation requested; waiting for the worker to stop.'; }
    const eta = state.eta || {};
    view.timing.textContent = `${eta.progress ? `Progress: ${eta.progress} · ` : ''}${eta.elapsed_seconds != null ? `Elapsed ${formatDuration(eta.elapsed_seconds)} · ` : ''}${state.paused || state.manual_request || state.cancel ? 'ETA withheld while waiting or stopping.' : eta.eta_seconds != null ? 'ETA estimated from current progress.' : 'Not enough progress for an ETA yet.'}`;
    const text = logs.join('\n');
    if (view.logs.textContent !== text) { view.logs.textContent = text; }
}

async function refreshWorkspaceMonitor() {
    if (workspaceMonitorPending || !isWorkspaceVisible()) { return; }
    workspaceMonitorPending = true;
    const epoch = workspaceMonitorEpoch;
    try {
        const registry = await API.get('/api/status');
        if (!registry || typeof registry !== 'object' || Array.isArray(registry)
                || !Object.values(registry).every(state => state && typeof state.running === 'boolean')) { throw new Error('Invalid task registry'); }
        const names = Object.entries(registry).filter(([, state]) => state?.running).map(([name]) => name);
        const results = await Promise.allSettled(names.map(name => API.get(`/api/status/${encodeURIComponent(name)}`)));
        if (!isWorkspaceVisible() || epoch !== workspaceMonitorEpoch) { return; }
        for (const [name, view] of workspaceTaskViews) {
            if (!names.includes(name)) { view.card.remove(); workspaceTaskViews.delete(name); }
        }
        results.forEach((result, index) => {
            const state = result.status === 'fulfilled' ? result.value : null;
            renderWorkspaceTask(names[index], state, !state || typeof state.running !== 'boolean');
        });
        applySystemStatusText('workspace-monitor-feedback', names.length ? `${names.length} task${names.length === 1 ? '' : 's'} reported running. Open a task screen to see its available controls.` : 'No tasks reported running at the last check.');
        if (workspacePreviousRunning && !names.length) { refreshBookWorkspace(); }
        workspacePreviousRunning = !!names.length;
    } catch (error) {
        if (!isWorkspaceVisible() || epoch !== workspaceMonitorEpoch) { return; }
        applySystemStatusText('workspace-monitor-feedback', 'Task registry unavailable. Any displayed tasks are from an earlier reading; work may still be running. Refresh before starting anything else.');
    } finally {
        workspaceMonitorPending = false;
        clearTimeout(workspacePollTimer);
        if (isWorkspaceVisible()) { workspacePollTimer = setTimeout(refreshWorkspaceMonitor, 5000); }
    }
}

window.refreshWorkspaceView = function () {
    workspaceMonitorEpoch++;
    clearTimeout(workspacePollTimer);
    if (!isWorkspaceVisible()) { return; }
    refreshBookWorkspace();
    refreshWorkspaceMonitor();
};
window.invalidateBookWorkspace = function () {
    ++collectionOverviewRequest;
    document.getElementById('workspace-collection')?.replaceChildren();
    const feedback = document.getElementById('workspace-collection-feedback');
    if (feedback) { feedback.textContent = 'The active book changed. Show the collection overview again.'; }
    workspaceBookRequest++;
    workspaceBookSnapshot = null;
    document.getElementById('workspace-continue').disabled = true;
    if (isWorkspaceVisible()) { refreshBookWorkspace(); }
};
document.addEventListener('visibilitychange', () => {
    workspaceMonitorEpoch++;
    clearTimeout(workspacePollTimer);
    if (isWorkspaceVisible()) { refreshWorkspaceView(); }
});
