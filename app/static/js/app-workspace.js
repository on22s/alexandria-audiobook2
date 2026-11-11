// Workspace navigation paints existing state; it never starts or resumes work.
const WORKSPACE_TASK_TABS = {
    script: 'script', batch_script: 'script', review: 'script', batch_review: 'script',
    nicknames: 'script', cast_list: 'script', persona: 'voices', voices: 'voices',
    audio: 'audio', drift_check: 'editor', outlier_check: 'editor', word_timing: 'editor', speech_verification: 'editor', book_preflight: 'script',
    audacity_export: 'audio', m4b_export: 'audio', chapter_export: 'audio',
    readalong_sidecar: 'audio', readaloud_epub: 'audio',
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
        ? ' ' + t('home.workspace.audio.speech', 'Saved speech checks: {current} match current row/audio metadata; {not_current} no longer match; {not_checked} unchecked. {attention} need speech review; {drift} have drift flags; {unknown} have unknown speakers.', {current: review.speech_current_metadata, not_current: review.speech_not_current, not_checked: review.speech_not_checked, attention: review.speech_attention, drift: review.drift_flagged, unknown: review.unknown_speakers})
        : review?.status === 'unavailable' ? ' ' + t('home.workspace.audio.review-unavailable', 'Saved review evidence could not be read.') : '';
    const exported = data.export_review;
    const exportNote = exported?.status === 'observed'
        ? t('home.workspace.export.note', '{passed} passed at export; {review} needed review; {unavailable} receipts no longer match output metadata; {failed} failed attempts; {running} running. Checks are from this app session. Current script and voice inputs have not been rechecked; confirm book content before sharing.', {passed: exported.verified_at_export, review: exported.review_at_export, unavailable: exported.unavailable, failed: exported.failed_attempts, running: exported.running})
        : t('safety.home.export.unavailable', 'Saved export checks are unavailable. Check that each output belongs to this book before sharing it.');
    const outputs = data.outputs.map(row => `${row.format}: ${row.status === 'present_unverified'
        ? t('home.workspace.output.present-unverified', 'file present; book match unverified') : row.status === 'historical_source_scope'
            ? t('home.workspace.output.older-scope', 'older scope; rebuild before download') : row.status === 'unavailable' ? t('home.workspace.output.unavailable', 'could not inspect') : t('home.workspace.output.missing', 'no file found')}`).join(' · ');
    document.getElementById('workspace-stages').replaceChildren(
        renderWorkspaceStage(t('home.workspace.stage.script', 'Script'), data.script.entries ? t('home.workspace.script.entries', 'Entries saved: {count}', {count: data.script.entries}) : t('home.workspace.not-started', 'Not started'), data.script.recovery?.status === 'candidate'
            ? t('safety.home.script.recovery', 'Interrupted script run has saved recovery evidence. Inspect it in Script; source and settings eligibility must be rechecked before resuming.')
            : t('home.workspace.script.saved', 'Saved annotations for the selected book.')),
        renderWorkspaceStage(t('home.workspace.stage.cast', 'Cast'), t('home.workspace.cast.summary', 'Characters: {total} · need preparation: {pending}', {total: data.cast.total, pending: data.cast.pending}), t('home.workspace.cast.help', 'Finish preparing characters in Voices, then listen to their samples.')),
        renderWorkspaceStage(t('home.workspace.stage.audio', 'Audio'), audio.status === 'unavailable' ? t('home.workspace.audio.progress-unavailable', 'Progress unavailable')
            : audio.total ? t('home.workspace.audio.files', 'Files present: {present} / {total}', {present: audio.present, total: audio.total}) : t('home.workspace.not-prepared', 'Not prepared'),
            `${audio.message}${speech}${audio.failed || audio.missing ? ' ' + t('home.workspace.audio.failures', '{failed} failed; {missing} done rows have missing audio.', {failed: audio.failed, missing: audio.missing}) : ''}`),
        renderWorkspaceStage(t('home.workspace.stage.export', 'Export'), outputs || t('home.workspace.not-inspected', 'Not inspected'), exportNote)
    );
}

function renderWorkspaceLibrary(books) {
    const signature = JSON.stringify(books.map(book => [book.name, book.has_voice_config]));
    if (signature === workspaceLibrarySignature) { return; }
    workspaceLibrarySignature = signature;
    const container = document.getElementById('workspace-library');
    const selected = new Set(collectionSelectedNames());
    container.replaceChildren();
    if (!books.length) { container.textContent = t('home.workspace.library.empty', 'No saved books yet. Save the current script from Script.'); return; }
    for (const book of books) {
        const row = document.createElement('div');
        row.className = 'workspace-library-row';
        const title = document.createElement('strong');
        title.textContent = book.name;
        const description = document.createElement('div');
        description.className = 'small text-muted mb-2';
        description.textContent = t('home.workspace.library.script-saved', 'Script saved · {voices}', {voices: book.has_voice_config ? t('home.workspace.library.voices-saved', 'voice settings file saved') : t('home.workspace.library.voices-missing', 'no saved voice settings file')});
        const load = document.createElement('button');
        load.type = 'button';
        load.className = 'btn btn-outline-secondary btn-sm';
        load.textContent = t('home.workspace.library.load', 'Load saved book…');
        load.setAttribute('data-help-key', 'openWorkspaceSavedBook');
        load.addEventListener('click', () => openWorkspaceSavedBook(book.name, load));
        const checkLabel = document.createElement('label');
        checkLabel.className = 'small d-block mt-2';
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.setAttribute('data-collection-select', book.name);
        check.checked = selected.has(book.name);
        const checkText = document.createElement('span');
        checkText.textContent = ' ' + t('home.workspace.library.select', 'Select {book} for collection overview', {book: book.name});
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
    for (const label of [t('home.workspace.collection.book', 'Book'), t('home.workspace.collection.stage', 'Stage'), t('home.workspace.collection.evidence', 'Evidence'), t('home.workspace.collection.next', 'Next step'), t('home.workspace.collection.open', 'Open')]) {
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
        bookId.textContent = t('home.workspace.collection.id', 'ID {id}{active} · revision {revision}', {id: row.book_id, active: row.is_active ? ' · ' + t('home.workspace.collection.active', 'active') : '', revision: isCollectionDigest(row.revision) ? row.revision.slice(0, 12) : t('home.workspace.collection.unknown', 'unknown')});
        bookCell.append(bookTitle, bookId);
        const stageCell = document.createElement('td');
        stageCell.textContent = row.overall;
        const evidenceCell = document.createElement('td');
        const evidence = document.createElement('ul');
        evidence.className = 'small mb-0';
        for (const [label, stage] of [[t('home.workspace.stage.script', 'Script'), row.script], [t('home.workspace.stage.review', 'Review'), row.review], [t('home.workspace.stage.voices', 'Voices'), row.voices], [t('home.workspace.stage.render', 'Render/export'), row.render]]) {
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
        open.textContent = t('home.workspace.collection.load', 'Load this book…');
        open.setAttribute('data-help-key', 'openWorkspaceSavedBook');
        if (!isCollectionRowLoadable(row)) {
            open.disabled = true;
            open.title = row.loadable === false ? row.script.evidence
                : t('home.workspace.collection.no-proof', 'No valid revision proof for this book; show the overview again before loading.');
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
        feedback.textContent = t('home.workspace.collection.select-first', 'Select at least one saved book above first.');
        return null;
    }
    if (names.length > collectionOverviewLimit) {
        feedback.textContent = t('home.workspace.collection.limit', 'Select at most {limit} books for the collection overview; deselect some books and try again.', {limit: collectionOverviewLimit});
        return null;
    }
    feedback.textContent = t('home.workspace.collection.reading', 'Reading the selected books…');
    const pending = { signature };
    collectionOverviewPending = pending;
    const show = document.getElementById('workspace-collection-show');
    if (show) { show.disabled = true; }
    try {
        const rows = await API.post('/api/collection/overview', { names });
        if (request !== collectionOverviewRequest) { return null; }
        if (selectionEpoch !== (window._bookSelectionEpoch || 0)
                || JSON.stringify(names) !== JSON.stringify(collectionSelectedNames())) {
            feedback.textContent = t('safety.home.collection.changed', 'The book or collection selection changed. Show the overview again.');
            return null;
        }
        if (!Array.isArray(rows) || !rows.every(isCollectionRowValid)
                || JSON.stringify(rows.map(row => row.name)) !== JSON.stringify(names)) {
            feedback.textContent = t('safety.home.collection.incomplete', 'Collection overview was incomplete. Nothing was loaded; refresh the selection and try again.');
            return null;
        }
        renderCollectionOverview(rows);
        feedback.textContent = rows.length === 1
            ? t('home.workspace.collection.updated.one', 'Collection overview updated for {count} book. Opening it started no work.', {count: rows.length})
            : t('home.workspace.collection.updated.many', 'Collection overview updated for {count} books. Opening it started no work.', {count: rows.length});
        return rows;
    } catch (error) {
        if (request !== collectionOverviewRequest) { return null; }
        feedback.textContent = t('safety.home.collection.unreadable', 'Collection overview could not be read: {detail}. No book was switched.', {detail: error.message || t('home.workspace.collection.request-failed', 'request failed')});
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
                : t('safety.home.collection.no-proof', 'This book has no valid revision proof. Nothing was loaded; show the overview again before loading.');
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
        document.getElementById('workspace-library-feedback').textContent = t('safety.home.library.unavailable', 'Saved books could not be refreshed. Any displayed list is from an earlier reading.');
    }
    if (book.status !== 'fulfilled') {
        document.getElementById('workspace-feedback').textContent = t('safety.home.status.unavailable', 'Book status could not be refreshed. Any displayed facts are from an earlier reading. Continue is unavailable until Refresh succeeds.');
        await showBookRecoveryHint(request);
        return null;
    }
    const data = book.value;
    if (!isWorkspaceSnapshotValid(data)) {
        document.getElementById('workspace-feedback').textContent = t('safety.home.status.incomplete', 'Book status was incomplete. Continue is unavailable; refresh before proceeding.');
        return null;
    }
    workspaceBookSnapshot = data;
    if (Number.isSafeInteger(data.collection_overview_limit) && data.collection_overview_limit > 0) {
        collectionOverviewLimit = data.collection_overview_limit;
    }
    renderBookWorkspace(data);
    document.getElementById('workspace-feedback').textContent = t('home.workspace.status.updated', 'Book status updated at {time}.', {time: new Date().toLocaleTimeString(getIntlUiLocale())});
    return data;
}

function getBookRecoveryText(summary) {
    if (!summary || typeof summary !== 'object' || !['pending', 'migration_required', 'unreadable'].includes(summary.status)) { return null; }
    const commands = Array.isArray(summary.commands) ? summary.commands.filter(item => item
        && typeof item.purpose === 'string' && typeof item.command === 'string') : [];
    const steps = commands.map(item => `${item.purpose}: ${item.command}`).join(' — ');
    return {status: summary.status, steps};
}

// When the book cannot be read, ask why: an interrupted save names the
// command that resolves it instead of a generic refresh failure (#1660).
// Each branch keeps its English literal at the call site so the catalog
// harvest can seed the safety key it renders.
async function showBookRecoveryHint(request) {
    let summary;
    try { summary = await API.get('/api/book/recovery'); } catch (error) { return; }
    if (request !== workspaceBookRequest) { return; }
    const recovery = getBookRecoveryText(summary);
    if (!recovery) { return; }
    const target = document.getElementById('workspace-feedback');
    if (recovery.status === 'pending' && recovery.steps) {
        renderUiSafety(target, 'safety.home.recovery.pending.steps', 'An interrupted book save has not been finished, so the book cannot open yet. Nothing was deleted. In a terminal in the Alexandria folder — {steps}', {steps: recovery.steps});
    } else if (recovery.status === 'pending') {
        renderUiSafety(target, 'safety.home.recovery.pending', 'An interrupted book save has not been finished, so the book cannot open yet. Nothing was deleted.');
    } else if (recovery.status === 'migration_required' && recovery.steps) {
        renderUiSafety(target, 'safety.home.recovery.migration.steps', 'An interrupted save from an older version of the app needs a one-time check before the book can open. Nothing was deleted. In a terminal in the Alexandria folder — {steps}', {steps: recovery.steps});
    } else if (recovery.status === 'migration_required') {
        renderUiSafety(target, 'safety.home.recovery.migration', 'An interrupted save from an older version of the app needs a one-time check before the book can open. Nothing was deleted.');
    } else if (recovery.steps) {
        renderUiSafety(target, 'safety.home.recovery.unreadable.steps', "The book's recovery state could not be read, so the book cannot open yet. Nothing was changed. In a terminal in the Alexandria folder — {steps}", {steps: recovery.steps});
    } else {
        renderUiSafety(target, 'safety.home.recovery.unreadable', "The book's recovery state could not be read, so the book cannot open yet. Nothing was changed.");
    }
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
    catch (error) { showActionError(t('home.workspace.load.failed', 'Could not load the saved book'), error, t('home.workspace.load.recovery', 'Review the currently selected book before trying Load again; its reply may have been lost.')); }
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
        controls.textContent = t('home.workspace.task.open', 'Open task screen');
        controls.hidden = !getWorkspaceTaskTab(name);
        controls.setAttribute('data-help-key', 'openWorkspaceTask');
        controls.addEventListener('click', () => openWorkspaceTask(name));
        const details = document.createElement('details');
        details.className = 'mt-3';
        const summary = document.createElement('summary');
        summary.textContent = t('home.workspace.task.log', 'Recent task log');
        const logs = document.createElement('pre');
        details.append(summary, logs); card.append(title, activity, timing, controls, details);
        document.getElementById('workspace-monitor').appendChild(card);
        view = {card, activity, timing, logs, track: {}};
        workspaceTaskViews.set(name, view);
    }
    if (stale) {
        view.timing.textContent = t('safety.home.task.stale', 'Task details could not be refreshed. Any log shown is from an earlier reading; task may still be running.');
        return;
    }
    const identity = state.claim_id || state.run_id || state.start_time;
    if (identity != null && identity !== view.identity) { view.identity = identity; view.track = {}; }
    const logs = Array.isArray(state.logs) ? state.logs.filter(line => typeof line === 'string').slice(-40).map(line => line.slice(0, 2000)) : [];
    renderActivity(view.activity, {...state, logs}, view.track);
    if (!state.running) { view.activity.hidden = false; view.activity.textContent = t('home.workspace.task.finished', 'No longer running; refreshing the task list.'); }
    if (state.cancel) { view.activity.textContent = t('home.workspace.task.cancelling', 'Cancellation requested; waiting for the worker to stop.'); }
    const eta = state.eta || {};
    const timing = [];
    if (eta.progress) { timing.push(t('home.workspace.task.progress', 'Progress: {progress}', {progress: eta.progress})); }
    if (eta.elapsed_seconds != null) { timing.push(t('home.workspace.task.elapsed', 'Elapsed {duration}', {duration: formatDuration(eta.elapsed_seconds)})); }
    timing.push(state.paused || state.manual_request || state.cancel ? t('home.workspace.task.eta-withheld', 'ETA withheld while waiting or stopping.') : eta.eta_seconds != null ? t('home.workspace.task.eta-estimated', 'ETA estimated from current progress.') : t('home.workspace.task.eta-unknown', 'Not enough progress for an ETA yet.'));
    view.timing.textContent = timing.join(' · ');
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
        applySystemStatusText('workspace-monitor-feedback', names.length ? (names.length === 1
            ? t('home.workspace.task.running.one', '{count} task reported running. Open a task screen to see its available controls.', {count: names.length})
            : t('home.workspace.task.running.many', '{count} tasks reported running. Open a task screen to see its available controls.', {count: names.length})) : t('home.workspace.task.none', 'No tasks reported running at the last check.'));
        if (workspacePreviousRunning && !names.length) { refreshBookWorkspace(); }
        workspacePreviousRunning = !!names.length;
    } catch (error) {
        if (!isWorkspaceVisible() || epoch !== workspaceMonitorEpoch) { return; }
        applySystemStatusText('workspace-monitor-feedback', t('safety.home.task.registry-unavailable', 'Task registry unavailable. Any displayed tasks are from an earlier reading; work may still be running. Refresh before starting anything else.'));
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
    if (feedback) { feedback.textContent = t('safety.home.collection.active-changed', 'The active book changed. Show the collection overview again.'); }
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
