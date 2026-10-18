// Workspace navigation paints existing state; it never starts or resumes work.
const WORKSPACE_TASK_TABS = {
    script: 'script', batch_script: 'script', review: 'script', batch_review: 'script',
    nicknames: 'script', cast_list: 'script', persona: 'voices', voices: 'voices',
    audio: 'audio', drift_check: 'editor', book_preflight: 'script',
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
    const outputs = data.outputs.map(row => `${row.format}: ${row.status === 'present_unverified'
        ? 'file present; book match unverified' : row.status === 'unavailable' ? 'could not inspect' : 'no file found'}`).join(' · ');
    document.getElementById('workspace-stages').replaceChildren(
        renderWorkspaceStage('Script', data.script.entries ? `${data.script.entries} entries saved` : 'Not started', 'Saved annotations for the selected book.'),
        renderWorkspaceStage('Cast', `${data.cast.total} characters · ${data.cast.pending} need preparation`, 'Finish preparing characters in Voices, then listen to their samples.'),
        renderWorkspaceStage('Audio', audio.status === 'unavailable' ? 'Progress unavailable'
            : audio.total ? `${audio.present} / ${audio.total} files present` : 'Not prepared',
            `${audio.message}${audio.failed || audio.missing ? ` ${audio.failed} failed; ${audio.missing} done rows have missing audio.` : ''}`),
        renderWorkspaceStage('Output files', outputs || 'Not inspected', 'Check that each output belongs to this book before sharing it.')
    );
}

function renderWorkspaceLibrary(books) {
    const signature = JSON.stringify(books.map(book => [book.name, book.has_voice_config]));
    if (signature === workspaceLibrarySignature) { return; }
    workspaceLibrarySignature = signature;
    const container = document.getElementById('workspace-library');
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
        row.append(title, description, load);
        container.appendChild(row);
    }
}

function isWorkspaceSnapshotValid(data) {
    const count = value => Number.isSafeInteger(value) && value >= 0;
    return !!data && typeof data.name === 'string' && /^[a-f0-9]{64}$/.test(data.book_token)
        && count(data.script?.entries) && count(data.cast?.total) && count(data.cast?.pending)
        && data.cast.pending <= data.cast.total
        && ['not_started', 'files_present', 'needs_attention', 'unavailable'].includes(data.audio?.status)
        && typeof data.audio.message === 'string'
        && ['total', 'present', 'failed', 'missing', 'generating'].every(key => count(data.audio[key]))
        && data.audio.present <= data.audio.total
        && Array.isArray(data.outputs) && data.outputs.every(row => row && typeof row.format === 'string'
            && ['present_unverified', 'not_found', 'unavailable'].includes(row.status))
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

async function openWorkspaceSavedBook(name, button) {
    button.disabled = true;
    try { await loadScript(name); }
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
