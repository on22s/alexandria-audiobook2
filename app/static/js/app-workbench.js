        // ── Dataset Builder ──────────────────────────────────────

        // In-memory row data: [{emotion, text, status, audio_url}, ...]
        let dsbRows = [];
        let dsbPolling = null;
        let dsbBatchRunning = false;
        let dsbBatchStartSequence = 0;
        let dsbPendingStart = null;
        let dsbSaveMetaQueue = null;
        let dsbSaveRowsQueue = null;
        let dsbCurrentProject = '';
        let dsbLoadedProject = '';
        let dsbProjectLoadSequence = 0;
        let dsbPendingProjectLoad = null;
        let dsbProjectListSequence = 0;
        let dsbTrainingSaved = null;
        let dsbTrainingSavePending = false;
        let dsbConflictRecoveryShown = false;
        let dsbSnapshotGeneration = 0;

        // Clean up legacy localStorage
        try { localStorage.removeItem('alexandria-dsb-form'); } catch (e) { /* storage blocked */ }

        // Pending starts must block edits before the server reports running.
        function applyDatasetBatchState(running, starting = false) {
            dsbBatchRunning = running || starting || dsbPendingStart !== null;
            document.getElementById('dsb-btn-gen-all').style.display = dsbBatchRunning ? 'none' : '';
            document.getElementById('dsb-btn-regen-all').style.display = dsbBatchRunning ? 'none' : '';
            document.getElementById('dsb-btn-cancel').style.display = running ? '' : 'none';
        }

        async function dsbLoadProjects(selectName) {
            const sequence = ++dsbProjectListSequence;
            try {
                const projects = await API.get('/api/dataset_builder/list');
                if (sequence !== dsbProjectListSequence) { return; }
                if (!Array.isArray(projects)) { throw new Error('Invalid dataset project list'); }
                const select = document.getElementById('dsb-project-select');
                const selected = selectName || select.value;
                const missing = selected && !projects.some(project => project.name === selected);
                select.innerHTML = '<option value="">' + t('workbench.projects.select-empty', '-- Select project --') + '</option>' +
                    projects.map(p => getEscapedHtml`<option value="${p.name}">${p.name} (${p.done_count}/${p.sample_count})</option>`).join('') +
                    (missing ? getEscapedHtml`<option value="${selected}">${selected} (${t('workbench.projects.not-in-list', 'not in current list')})</option>` : '');
                select.value = selected;
                document.getElementById('dsb-project-list-status').textContent = missing
                    ? t('workbench.projects.not-in-latest', 'The selected dataset is not in the latest project list. Its loaded rows have been kept; check the project before saving.') : '';
                if (selectName) {
                    select.value = selectName;
                    await dsbOnProjectChange();
                }
            } catch (e) {
                if (sequence !== dsbProjectListSequence) { return; }
                document.getElementById('dsb-project-list-status').innerHTML = '<span>' + t('workbench.projects.refresh-failed', 'Dataset project list could not be refreshed. Showing the last available list. Check that Alexandria is running, then ') + '</span><button type="button" class="btn btn-sm btn-outline-secondary" onclick="dsbLoadProjects()">' + t('workbench.projects.retry', 'Retry project list') + '</button>';
                console.error('Failed to load projects:', e);
            }
        }

        function ensureDatasetProjectSwitchAllowed() {
            const select = document.getElementById('dsb-project-select');
            if (dsbBatchRunning && select.value !== dsbCurrentProject) {
                select.value = dsbCurrentProject;
                showToast(t('workbench.projects.batch-running', 'A batch is running in project "{project}". Wait for it to finish or cancel it before changing projects.', {project: dsbCurrentProject}), 'warning');
                return false;
            }
            return true;
        }

        window.dsbOnProjectChange = async () => {
            if (!ensureDatasetProjectSwitchAllowed()) { return; }
            if (!datasetImplicitFlushAllowed(dsbCurrentProject)) {
                document.getElementById('dsb-project-select').value = dsbCurrentProject;
                return;
            }
            try {
                await Promise.all([dsbSaveMetaQueue?.flush(), dsbSaveRowsQueue?.flush()]);
            } catch (error) {
                document.getElementById('dsb-project-select').value = dsbCurrentProject;
                // A refused draft is NOT replayed on every switch/reload. The
                // banner keeps the draft available and offers copy/reload paths.
                showDatasetConflict(dsbCurrentProject);
                return;
            }
            if (!ensureDatasetProjectSwitchAllowed()) { return; }
            dsbStopBatch();
            const name = document.getElementById('dsb-project-select').value;
            const formArea = document.getElementById('dsb-form-area');
            const deleteBtn = document.getElementById('dsb-btn-delete-project');
            if (!name) {
                dsbCurrentProject = '';
                dsbLoadedProject = '';
                formArea.style.display = 'none';
                deleteBtn.style.display = 'none';
                dsbRows = [];
                dsbRenderTable();
                return;
            }
            dsbCurrentProject = name;
            formArea.style.display = '';
            deleteBtn.style.display = '';
            await dsbLoadProject(name);
        };

        function isDatasetProjectSelected(name) {
            return dsbCurrentProject === name;
        }

        async function dsbLoadProject(name, { skipFlush = false, prefetched = null } = {}) {
            const loadSequence = ++dsbProjectLoadSequence;
            dsbPendingProjectLoad = loadSequence;
            document.getElementById('dsb-form-area').style.display = 'none';
            if (!skipFlush && !datasetImplicitFlushAllowed(name)) {
                if (dsbPendingProjectLoad === loadSequence) { dsbPendingProjectLoad = null; }
                document.getElementById('dsb-form-area').style.display = '';
                return;
            }
            try {
                if (!skipFlush && dsbSaveRowsQueue?.isDirty()) { await dsbSaveRowsQueue.flush(); }
            } catch (error) {
                if (loadSequence !== dsbProjectLoadSequence) { return; }
                if (dsbPendingProjectLoad === loadSequence) { dsbPendingProjectLoad = null; }
                document.getElementById('dsb-form-area').style.display = '';
                showActionError(t('workbench.project.save-pending', 'Pending dataset edits could not be saved'), error, t('workbench.project.save-pending.guidance', 'Keep the current project open. Resolve the row-save error before reloading or switching projects.'));
                showDatasetConflict(name);
                return;
            }
            try {
                const result = prefetched || await datasetFetchStatus(name, { loadSequence });
                if (!result || loadSequence !== dsbProjectLoadSequence || !isDatasetProjectSelected(name)) {
                    document.getElementById('dsb-form-area').style.display = '';
                    return;
                }
                finishDatasetLoad(name, result);
            } catch (e) {
                if (loadSequence !== dsbProjectLoadSequence || !isDatasetProjectSelected(name)) { return; }
                // A transient status-GET failure must NOT destroy the saved
                // project. Disarm dsbSaveRows (guarded on dsbCurrentProject)
                // BEFORE clearing rows so its debounced POST can't overwrite the
                // real samples on disk with an empty row, and do NOT dsbAddRow.
                console.error('Failed to load project:', e);
                const retained = dsbLoadedProject === name;
                document.getElementById('dsb-project-load-status').innerHTML = getEscapedHtml`<div class="alert alert-warning small py-2">${retained ? t('workbench.project.refresh-failed', 'Could not refresh dataset "{project}". Showing the last loaded version.', {project: name}) : t('workbench.project.load-failed', 'Could not load dataset "{project}". Check that Alexandria is still running, then try again.', {project: name})} <button type="button" class="btn btn-sm btn-outline-secondary" onclick="dsbOnProjectChange()">${t('workbench.project.retry', 'Retry')}</button></div>`;
                if (retained) { document.getElementById('dsb-form-area').style.display = ''; return; }
                dsbCurrentProject = '';
                dsbLoadedProject = '';
                dsbRows = [];
                dsbRenderTable();
                document.getElementById('dsb-form-area').style.display = 'none';
                document.getElementById('dsb-btn-delete-project').style.display = 'none';
                showActionError(t('workbench.project.load-failed.title', 'Could not load dataset "{project}"', {project: name}), e, t('workbench.project.load-failed.guidance', 'Check the selected dataset and app connection, then use Retry to load it again.'));
            } finally {
                if (dsbPendingProjectLoad === loadSequence) { dsbPendingProjectLoad = null; }
            }
        }

        // Explicit conflict recovery. A single project-scoped attempt identity
        // decides whether a held reload response may still retire the draft.
        // Returning to the draft invalidates that attempt; the conflict latch
        // stays until the user explicitly chooses Keep editing.
        function clearDatasetRecoveryBanner() {
            const el = document.getElementById('dsb-project-load-status');
            if (el) { if (el.dataset) { delete el.dataset.dsbReloadPending; } el.innerHTML = ''; }
        }

        window.dsbKeepDatasetDraft = () => {
            const state = ensureDatasetRowSaveState(dsbCurrentProject);
            state.conflictLatch = false;
            state.awaitingReplacement = false;
            state.attemptId += 1;
            dsbConflictRecoveryShown = false;
            clearDatasetRecoveryBanner();
        };

        window.dsbRetryDatasetReload = () => dsbDiscardDatasetDraftAndReload();

        window.dsbReturnToDatasetDraft = () => {
            const state = ensureDatasetRowSaveState(dsbCurrentProject);
            state.attemptId += 1;
            state.awaitingReplacement = false;
            dsbConflictRecoveryShown = false;
            clearDatasetRecoveryBanner();
            showDatasetConflict(dsbCurrentProject);
        };

        window.dsbCopyDatasetDraft = () => {
            let rows;
            try { rows = datasetExportRows(); } catch (e) { showToast(e.message, 'error'); return; }
            const state = ensureDatasetRowSaveState(dsbCurrentProject);
            const draft = state.metadataDraft;
            const data = {
                description: draft ? draft.description : document.getElementById('dsb-description').value,
                global_seed: draft ? draft.global_seed : document.getElementById('dsb-global-seed').value,
                rows,
            };
            dsbDownloadJson(data, 'draft');
            showToast(t('workbench.rows.conflict.copied', 'Draft copied for review. The saved dataset has not changed.'), 'success');
        };

        window.dsbDiscardDatasetDraftAndReload = async () => {
            const name = dsbCurrentProject;
            if (!name) { return; }
            if (typeof showConfirm === 'function'
                && !await showConfirm(formatUiSafetyText('safety.workbench.rows.discard', 'Discard the unsaved dataset draft and reload the saved version? Export it first if you need a copy.'),
                    {title: t('workbench.rows.conflict.discard.title', 'Discard draft and reload?'), actionLabel: t('workbench.rows.conflict.discard', 'Discard and reload'), danger: true})) { return; }
            // Writes stay suspended until a fresh replacement snapshot is
            // installed. A failed read keeps the refused draft recoverable and
            // blocks every implicit and explicit write path.
            const state = ensureDatasetRowSaveState(name);
            const attempt = ++state.attemptId;
            state.awaitingReplacement = true;
            let result;
            try {
                result = await datasetFetchStatus(name, {
                    loadSequence: dsbProjectLoadSequence, attemptId: attempt });
            } catch (error) {
                if (state.attemptId !== attempt || !isDatasetProjectSelected(name)) { return; }
                showActionError(t('workbench.project.load-failed.title', 'Could not load dataset "{project}"', {project: name}),
                    error, t('workbench.project.load-failed.guidance', 'Check the selected dataset and app connection, then use Retry to load it again.'));
                showReloadPendingBanner(name);
                return;
            }
            if (!result || state.attemptId !== attempt || !isDatasetProjectSelected(name)) { return; }
            await dsbSaveRowsQueue?.discard();
            await dsbSaveMetaQueue?.discard();
            if (state.attemptId !== attempt || !isDatasetProjectSelected(name)) { return; }
            finishDatasetLoad(name, result);
        };

        window.dsbCreateProject = async () => {
            const project = dsbCurrentProject;
            const values = await showPresetEditor({title: t('workbench.create.title', 'Create dataset'), nameLabel: t('workbench.create.name.label', 'Dataset name'), includeDescription: false,
                actionLabel: t('workbench.create.submit', 'Create'), helperText: t('workbench.create.helper', 'Use a short name you will recognize later, such as “The Hobbit — narration”.'),
                submitValues: async ({name}) => {
                    if (project !== dsbCurrentProject) { throw new Error(t('workbench.create.project-changed', 'The selected project changed. Close this dialog and review it before creating a dataset.')); }
                    try {
                        return await API.post('/api/dataset_builder/create', {name});
                    } catch (error) {
                        console.error('Dataset creation failed', error);
                        if (error.status === 400 || error.status === 409) { throw error; }
                        throw new Error(t('workbench.create.unconfirmed', 'Creation was not confirmed. Check that Alexandria is running, then refresh the project list before trying again.'));
                    }
                }});
            if (!values) { return; }
            if (project !== dsbCurrentProject) {
                showToast(t('workbench.create.created', 'Dataset “{project}” was created. Your later project selection was kept. Refresh the list to see it.', {project: values.receipt.name}), 'success'); return;
            }
            await dsbLoadProjects(values.receipt.name);
        };

        window.dsbDeleteProject = async () => {
            if (!dsbCurrentProject || !ensureDatasetRowsEditable()) { return; }
            const project = dsbCurrentProject;
            const loadSequence = dsbProjectLoadSequence;
            if (!await showConfirm(formatUiSafetyText('safety.workbench.project.delete', 'Delete project "{project}" and all its samples?', {project}), {title: t('workbench.project.delete.title', 'Delete project?'), actionLabel: t('workbench.project.delete.action', 'Delete project'), danger: true})) { return; }
            if (!ensureDatasetRowsEditable() || project !== dsbCurrentProject || loadSequence !== dsbProjectLoadSequence) { return; }
            try {
                const res = await fetch(`/api/dataset_builder/${encodeURIComponent(project)}`, { method: 'DELETE' });
                await API._handleError(res);
                if (project !== dsbCurrentProject || loadSequence !== dsbProjectLoadSequence) {
                    await dsbLoadProjects();
                    return;
                }
                dsbCurrentProject = '';
                dsbLoadedProject = '';
                document.getElementById('dsb-form-area').style.display = 'none';
                document.getElementById('dsb-btn-delete-project').style.display = 'none';
                dsbRows = [];
                dsbRenderTable();
                await dsbLoadProjects();
            } catch (e) {
                showActionError(t('workbench.project.delete-failed', 'Dataset project deletion failed'), e, t('workbench.project.delete-failed.guidance', 'Refresh the project list to check whether the project was deleted before trying again.'));
            }
        };

        const dsbRowSaveStates = new Map();
        function getDatasetRowDefinition(row) {
            const definition = datasetRowDefinition(row);
            definition.text = definition.text.trim();
            return definition;
        }

        function getDatasetSeed(value) {
            if (value == null || String(value).trim() === '') { return -1; }
            const number = Number(value);
            if (!['string', 'number'].includes(typeof value) || !Number.isSafeInteger(number) || number < -1) {
                throw new Error(t('workbench.rows.seed-invalid', 'Seeds must be safe whole numbers: -1 for fallback/random, or 0 or higher.'));
            }
            return number;
        }

        function ensureDatasetRowSaveState(name) {
            if (!dsbRowSaveStates.has(name)) {
                dsbRowSaveStates.set(name, { tokens: null, baseDefinitions: null, metadata: null,
                    metadataDraft: null, edits: new Map(), sequence: 0,
                    fullRevision: null, fullConflict: false, conflictLatch: false,
                    awaitingReplacement: false, attemptId: 0, refreshPromise: null,
                    lastAckGeneration: 0,
                    conflictShown: false, completedSequence: 0, conflicted: new Set() });
            }
            return dsbRowSaveStates.get(name);
        }

        // The acknowledged project metadata (root description + global seed)
        // travels with every metadata write and generation admission. It is the
        // root-level counterpart of the row base: a peer's voice edit keeps the
        // row hash unchanged, so row CAS alone cannot detect it.
        function applyDatasetMetadataAck(name, metadata, { own = false, syncControls = true } = {}) {
            if (!metadata || typeof metadata !== 'object') { return; }
            const state = ensureDatasetRowSaveState(name);
            const next = {};
            if (typeof metadata.description === 'string') { next.description = metadata.description; }
            if (metadata.global_seed !== undefined && metadata.global_seed !== null) {
                next.global_seed = String(metadata.global_seed);
            }
            if (!Object.keys(next).length) { return; }
            // A dirty metadata control keeps the expectation it was captured
            // against; adopting an external observation would authorise a write
            // over the peer's value while still displaying the old draft.
            if (!own && state.metadataDraft) { return; }
            state.metadata = { ...(state.metadata || {}), ...next };
            if (!syncControls) { return; }
            const description = document.getElementById('dsb-description');
            const seed = document.getElementById('dsb-global-seed');
            if (typeof next.description === 'string' && description) { description.value = next.description; }
            if (typeof next.global_seed === 'string' && seed) { seed.value = next.global_seed; }
        }

        function datasetExpectedMetadata(name) {
            const state = ensureDatasetRowSaveState(name);
            const expected = state.metadataDraft?.expected || state.metadata;
            const body = {};
            if (!expected) { return body; }
            if (typeof expected.description === 'string') { body.expected_description = expected.description; }
            if (typeof expected.global_seed === 'string') { body.expected_global_seed = expected.global_seed; }
            return body;
        }

        function markDatasetAck(name) {
            const state = ensureDatasetRowSaveState(name);
            state.lastAckGeneration = ++dsbSnapshotGeneration;
        }

        // Every status read carries the project load, recovery attempt and
        // acknowledgement generation it was dispatched against. A superseded
        // read resolves to null so no obsolete text, audio or token can be
        // installed and no terminal decision can be made from it.
        function datasetFetchStatus(name, { loadSequence = null, attemptId = null } = {}) {
            const state = ensureDatasetRowSaveState(name);
            const generation = ++dsbSnapshotGeneration;
            const load = loadSequence ?? dsbProjectLoadSequence;
            const attempt = attemptId ?? state.attemptId;
            return API.get(`/api/dataset_builder/status/${encodeURIComponent(name)}`).then(result => {
                const current = ensureDatasetRowSaveState(name);
                if (!isDatasetProjectSelected(name)) { return null; }
                if (load !== dsbProjectLoadSequence || attempt !== current.attemptId) { return null; }
                if (generation < (current.lastAckGeneration || 0)) { return null; }
                return { ...result, __dsbDispatchGeneration: generation };
            });
        }

        // A refused write latches its queue against implicit replays: switching
        // projects, generating or re-opening must not send it again just because
        // a peer restored the baseline and the CAS would now pass.
        function datasetImplicitFlushAllowed(name) {
            const state = ensureDatasetRowSaveState(name);
            if (state.awaitingReplacement) { showReloadPendingBanner(name); return false; }
            if (!state.conflictLatch) { return true; }
            showDatasetConflict(name);
            return false;
        }

        function showReloadPendingBanner(name) {
            const el = document.getElementById('dsb-project-load-status');
            if (!el || el.dataset?.dsbReloadPending === 'true') { return; }
            if (el.dataset) { el.dataset.dsbReloadPending = 'true'; }
            el.innerHTML = `<div class="alert alert-warning small py-2">${escapeHtml(t('workbench.rows.conflict.reload-pending', 'The dataset reload failed. The draft is still held and no writes are allowed until the reload succeeds or you return to the draft.'))} <button type="button" class="btn btn-sm btn-outline-secondary" onclick="dsbRetryDatasetReload()">${escapeHtml(t('workbench.project.retry', 'Retry'))}</button> <button type="button" class="btn btn-sm btn-outline-danger" onclick="dsbReturnToDatasetDraft()">${escapeHtml(t('workbench.rows.conflict.return-to-draft', 'Return to draft'))}</button></div>`;
        }

        // One authoritative snapshot installs controls, metadata expectations,
        // rows and tokens together. Every draft it supersedes is retired first,
        // so a discarded form can never leave its old expectation behind.
        function applyDatasetLoadedSnapshot(name, result) {
            const state = ensureDatasetRowSaveState(name);
            const samples = Array.isArray(result.samples) ? result.samples : [];
            document.getElementById('dsb-description').value = result.description || '';
            document.getElementById('dsb-global-seed').value = result.global_seed ?? '';
            dsbRows = samples.map(sample => ({ ...datasetRowDefinition(sample),
                status: sample.status || 'pending', audio_url: sample.audio_url || null }));
            state.metadataDraft = null;
            state.edits.clear();
            state.conflicted.clear();
            state.fullRevision = null;
            state.fullConflict = false;
            state.conflictLatch = false;
            state.awaitingReplacement = false;
            state.completedSequence = 0;
            state.conflictShown = false;
            state.attemptId += 1;
            applyDatasetRowRevisions(name, result.row_revisions, dsbRows.length);
            applyDatasetMetadataAck(name, result, { own: true });
            dsbConflictRecoveryShown = false;
            const el = document.getElementById('dsb-project-load-status');
            if (el) { if (el.dataset) { delete el.dataset.dsbReloadPending; } el.innerHTML = ''; }
        }

        // Every authoritative project load - ordinary switch or explicit
        // discard recovery - finishes through this one flow: batch ownership,
        // snapshot, forced physical table rebuild and worker polling.
        function finishDatasetLoad(name, result) {
            applyDatasetBatchState(!!(result.running || result.ownership_unconfirmed));
            applyDatasetLoadedSnapshot(name, result);
            dsbLoadedProject = name;
            dsbPendingProjectLoad = null;
            document.getElementById('dsb-form-area').style.display = '';
            dsbRenderTable(null, true);
            if (dsbRows.length === 0 && !dsbBatchRunning) { dsbAddRow(); }
            if (result.running || result.ownership_unconfirmed) { dsbStartPolling(name); }
        }

        function applyDatasetRowRevisions(name, revisions, count, definitions = null) {
            const state = ensureDatasetRowSaveState(name);
            state.tokens = Array.isArray(revisions) && revisions.length === count
                && revisions.every(token => typeof token === 'string' && /^[0-9a-f]{64}$/.test(token)) ? revisions.slice() : null;
            if (state.tokens) { state.baseDefinitions = (definitions || dsbRows.map(datasetRowDefinition)).map(definition => ({ ...definition })); }
        }

        // The immutable base every write binds to. It is advanced ONLY by an
        // own, CAS-verified acknowledgement (or a cold load). A poll, a 409 or
        // a metadata reconciliation of an external edit never touches it, so
        // a stale payload can never be re-authorised by a later token adopted
        // from somewhere else. An empty array is a verified empty base, never
        // "missing": count 0 and an empty revision map must still be sent.
        function datasetOwnBase(state) {
            if (!Array.isArray(state.tokens)) { return null; }
            const revisions = state.tokens;
            if (!revisions.every(token => typeof token === 'string' && /^[0-9a-f]{64}$/.test(token))) { return null; }
            return { count: revisions.length, revisions: revisions.slice() };
        }

        function datasetRowDefinition(row) {
            const emotion = row.emotion;
            const text = row.text;
            const seed = row.seed;
            return {
                emotion: typeof emotion === 'string' ? emotion
                    : (typeof row.description === 'string' ? row.description : ''),
                text: typeof text === 'string' ? text : '',
                seed: seed ?? '',
            };
        }

        function applyDatasetRowAck(name, index, row, revision) {
            if (!row || typeof row !== 'object' || Array.isArray(row)
                || !/^[0-9a-f]{64}$/.test(revision || '') || index >= dsbRows.length) { return false; }
            const state = ensureDatasetRowSaveState(name);
            const definition = datasetRowDefinition(row);
            // A live draft owns its visible row. Adopting an ACK over it would
            // persist a value the user never authorised.
            if (state.edits.has(index)) { state.conflicted.add(index); return false; }
            Object.assign(dsbRows[index], definition);
            if (row.status) { dsbRows[index].status = row.status; }
            dsbRows[index].audio_url = row.audio_url || null;
            if (row.status !== 'error') { delete dsbRows[index].error; }
            if (!Array.isArray(state.tokens)) { state.tokens = new Array(dsbRows.length).fill(null); }
            state.tokens[index] = revision;
            if (Array.isArray(state.baseDefinitions)) { state.baseDefinitions[index] = definition; }
            dsbRenderTable();
            return true;
        }

        // A server response is ONE snapshot: definitions, status, audio, row
        // revisions and project metadata all come from the same read. A clean
        // row takes the whole snapshot atomically, so a displayed definition is
        // never paired with an old token (the ABA overwrite, stale generation
        // and stale-audio class of bugs). A dirty row keeps its original
        // immutable base token and visible draft; only this tab's own
        // CAS-verified receipt may clear it and advance that token.
        function applyDatasetServerStatus(name, samples, { preserveDrafts = true } = {}) {
            if (!Array.isArray(samples)) { return; }
            const state = ensureDatasetRowSaveState(name);
            const changed = [];
            samples.forEach((serverRow, index) => {
                if (index >= dsbRows.length || !dsbRows[index]) { return; }
                if (preserveDrafts && state.edits.has(index)) { return; }
                const before = { status: dsbRows[index].status, audio: dsbRows[index].audio_url };
                if (serverRow.status) { dsbRows[index].status = serverRow.status; }
                dsbRows[index].audio_url = serverRow.audio_url || null;
                if (serverRow.status !== 'error') { delete dsbRows[index].error; }
                if (before.status !== dsbRows[index].status || before.audio !== dsbRows[index].audio_url) {
                    changed.push(index);
                }
            });
            if (changed.length) { dsbRenderTable(changed); }
        }

        function showDatasetConflict(name) {
            showToast(t('workbench.rows.external-edit', 'Dataset rows changed elsewhere. Reload before saving the affected rows.'), 'warning');
            if (ensureDatasetRowSaveState(name).awaitingReplacement) {
                showReloadPendingBanner(name);
                return;
            }
            const el = document.getElementById('dsb-project-load-status');
            if (!el || dsbConflictRecoveryShown) { return; }
            dsbConflictRecoveryShown = true;
            el.innerHTML = `<div class="alert alert-warning small py-2">${escapeHtml(t('workbench.rows.conflict.recovery', 'Dataset rows changed elsewhere. Your draft is kept; nothing is sent until you choose. Copy the draft for review, discard it and reload, or keep editing.'))} <button type="button" class="btn btn-sm btn-outline-secondary" onclick="dsbCopyDatasetDraft()">${escapeHtml(t('workbench.rows.conflict.copy', 'Copy draft for review'))}</button> <button type="button" class="btn btn-sm btn-outline-danger" onclick="dsbDiscardDatasetDraftAndReload()">${escapeHtml(t('workbench.rows.conflict.discard', 'Discard and reload'))}</button> <button type="button" class="btn btn-sm btn-link" onclick="dsbKeepDatasetDraft()">${escapeHtml(t('workbench.rows.conflict.keep', 'Keep editing'))}</button></div>`;
        }

        function reconcileDatasetRows(name, samples, revisions, { preserveDrafts = true, sequence = 0, own = false, metadata = null } = {}) {
            const state = ensureDatasetRowSaveState(name);
            const validTokens = Array.isArray(revisions) && Array.isArray(samples)
                && revisions.length === samples.length
                && revisions.every(token => typeof token === 'string' && /^[0-9a-f]{64}$/.test(token));
            if (!Array.isArray(samples)) { return false; }
            applyDatasetMetadataAck(name, metadata, { own });
            if (!validTokens) {
                if (own) { applyDatasetRowRevisions(name, null, 0); }
                // A token-less status still refreshes status/audio, never a base.
                applyDatasetServerStatus(name, samples, { preserveDrafts });
                return false;
            }
            if (own && preserveDrafts && state.fullRevision !== null) {
                // A queued structural draft owns the visible rows. Our own
                // CAS-verified receipt may advance the acknowledged rows and
                // their status/audio, but never install a definition over the
                // queued payload (e.g. a root change invalidating a done row
                // before its append commits).
                if (Array.isArray(state.tokens) && samples.length === state.tokens.length
                        && samples.every((serverRow, index) => {
                            const expected = state.baseDefinitions?.[index];
                            return !expected || JSON.stringify(expected) === JSON.stringify(datasetRowDefinition(serverRow));
                        })) {
                    state.tokens = revisions.slice();
                    state.baseDefinitions = samples.map(datasetRowDefinition);
                    applyDatasetServerStatus(name, samples, { preserveDrafts });
                }
                return false;
            }
            if (own && sequence && sequence < (state.completedSequence || 0)) { return false; }
            if (samples.length !== dsbRows.length) {
                if (preserveDrafts && (state.edits.size || state.fullRevision !== null)) {
                    applyDatasetServerStatus(name, samples, { preserveDrafts });
                    return false;
                }
                dsbRows = samples.map((sample, index) => ({ ...datasetRowDefinition(sample),
                    status: sample.status || 'pending', audio_url: sample.audio_url || null }));
                state.tokens = revisions.slice();
                state.baseDefinitions = samples.map(datasetRowDefinition);
                state.edits.clear();
                state.conflicted.clear();
                state.conflictShown = false;
                dsbRenderTable();
                return true;
            }
            if (!Array.isArray(state.baseDefinitions)) { state.baseDefinitions = dsbRows.map(datasetRowDefinition); }
            let changedDefinition = false, conflict = false;
            const changedStatus = [];
            const tbody = document.getElementById('dsb-table-body');
            samples.forEach((serverRow, index) => {
                const definition = datasetRowDefinition(serverRow);
                const localDefinition = getDatasetRowDefinition(dsbRows[index]);
                const hasLocalDraft = JSON.stringify(localDefinition)
                    !== JSON.stringify(state.baseDefinitions[index]);
                if (state.conflicted.has(index)
                    || (preserveDrafts && hasLocalDraft && !state.edits.has(index))) {
                    conflict = true;
                    return;
                }
                const draft = preserveDrafts ? state.edits.get(index) : null;
                if (draft) {
                    const base = draft.base || state.baseDefinitions[index];
                    if (JSON.stringify(base) !== JSON.stringify(definition)) {
                        // The draft keeps its original base and token, so its
                        // CAS still rejects; a later unrelated revision never
                        // re-authorises it.
                        conflict = true;
                        return;
                    }
                    if (own) { draft.base = definition; }
                }
                if (JSON.stringify(state.baseDefinitions[index]) !== JSON.stringify(definition)) {
                    changedDefinition = true;
                }
                const before = { status: dsbRows[index].status, audio: dsbRows[index].audio_url };
                const focused = dsbRowOwnsLiveControl(index, tbody);
                // A focused control is only protected when its visible text
                // actually diverges from the acknowledged definition. If it
                // still matches, this tab's own ACK may advance its token; a
                // truly dirty draft stays bound to the original base.
                const focusedMatches = focused && JSON.stringify(localDefinition) === JSON.stringify(definition);
                if (!draft && (!focused || focusedMatches)) {
                    if (!focused) { Object.assign(dsbRows[index], definition); }
                    state.baseDefinitions[index] = definition;
                    if (state.tokens) { state.tokens[index] = revisions[index]; }
                } else if (own && draft && state.tokens) {
                    state.tokens[index] = revisions[index];
                }
                if (serverRow.status) { dsbRows[index].status = serverRow.status; }
                dsbRows[index].audio_url = serverRow.audio_url || null;
                if (serverRow.status !== 'error') { delete dsbRows[index].error; }
                if (before.status !== dsbRows[index].status || before.audio !== dsbRows[index].audio_url) {
                    changedStatus.push(index);
                }
            });
            if (changedDefinition) { dsbRenderTable(); }
            else if (changedStatus.length) { dsbRenderTable(changedStatus); }
            if (conflict && !state.conflictShown) {
                state.conflictShown = true;
                showDatasetConflict(name);
            }
            if (!conflict) { state.conflictShown = false; }
            return validTokens && !conflict;
        }

        async function saveDatasetRows(value, order = 0) {
            const state = ensureDatasetRowSaveState(value.name);
            // Bind to the acknowledged base at write time. It advances only on
            // this tab's own CAS-verified receipts, so a preceding own append
            // contributes its full new count + revisions while a 409 or an
            // external edit never re-authorises this payload.
            const base = datasetOwnBase(state);
            if (value.rows) {
                const body = { name: value.name, rows: value.rows };
                if (base) {
                    // A verified empty base is still a base: count 0 with an
                    // empty revision map must guard the first external append.
                    body.expected_count = base.count;
                    body.expected_revisions = Object.fromEntries(
                        base.revisions.map((token, index) => [index, token]));
                }
                let result;
                try {
                    result = await API.post('/api/dataset_builder/update_rows', body);
                } catch (error) {
                    if (error.status === 409) {
                        // A refused structural draft stays dirty and bound to
                        // its original base. Clearing it here let an unrelated
                        // later delta masquerade as a clean queue and silently
                        // drop the rest of the refused payload on switch.
                        state.fullConflict = true;
                        state.conflictLatch = true;
                        state.conflictShown = false;
                        showDatasetConflict(value.name);
                    }
                    throw error;
                }
                const superseded = state.fullRevision !== null && state.fullRevision > value.revision;
                state.fullConflict = false;
                state.conflictLatch = false;
                state.conflictShown = false;
                state.completedSequence = Math.max(state.completedSequence || 0, order || value.revision || 0);
                markDatasetAck(value.name);
                // A row-only receipt never owns the metadata controls: a newer
                // voice draft must keep its text and captured expectations.
                if (Array.isArray(result.samples) && Array.isArray(result.row_revisions)) {
                    // A positional merge can invalidate a surviving row. Use
                    // the server's own projection, not the payload we posted;
                    // reseat the array so a deleted trailing row leaves no
                    // stale token behind.
                    state.tokens = result.row_revisions.slice();
                    state.baseDefinitions = result.samples.map(datasetRowDefinition);
                    if (superseded) {
                        // Settle only the acknowledged structural revision. A
                        // newer queued append stays visible and downloadable.
                        return;
                    }
                    state.fullRevision = null;
                    state.edits.clear();
                    state.conflicted.clear();
                    reconcileDatasetRows(value.name, result.samples, result.row_revisions,
                                         { own: true, preserveDrafts: false, sequence: order });
                } else {
                    if (!superseded) {
                        state.fullRevision = null;
                        state.edits.clear();
                        state.conflicted.clear();
                    }
                    applyDatasetRowRevisions(value.name, result.row_revisions, value.rows.length, value.rows);
                    state.baseDefinitions = value.rows.map(row => ({ ...row }));
                }
                return;
            }
            const queuedEdits = value.edits.filter(edit => state.edits.has(edit.index));
            if (!queuedEdits.length) { return; }
            // A coalesced draft binds to the same acknowledged base as the
            // committed chain: only our own receipts advance it, so an
            // external edit still makes the CAS fail instead of being adopted.
            const edits = queuedEdits.map(edit => ({ index: edit.index, row: edit.row,
                expected_revision: base ? base.revisions[edit.index] : state.tokens?.[edit.index] }));
            let result;
            try {
                result = await API.post('/api/dataset_builder/edit_rows', { name: value.name,
                    expected_count: base ? base.count : value.count, edits });
                if (!result.row_revisions || edits.some(edit => !/^[0-9a-f]{64}$/.test(result.row_revisions[edit.index] || ''))) {
                    throw new Error('Row save returned no revision acknowledgement');
                }
            } catch (error) {
                if (error.status && error.status < 500) {
                    queuedEdits.forEach(edit => state.conflicted.add(edit.index));
                    if (error.status === 409) {
                        state.conflictLatch = true;
                        showDatasetConflict(value.name);
                    }
                    throw error;
                }
                // A response can be lost after commit. A read may acknowledge
                // the desired definitions; it never replays or overwrites them.
                const saved = await datasetFetchStatus(value.name);
                if (!saved || !Array.isArray(saved.samples) || saved.samples.length !== value.count
                    || !Array.isArray(saved.row_revisions) || edits.some(edit =>
                        JSON.stringify(getDatasetRowDefinition(saved.samples[edit.index] || {})) !== JSON.stringify(edit.row)
                        || !/^[0-9a-f]{64}$/.test(saved.row_revisions[edit.index] || ''))) { throw error; }
                result = { row_revisions: Object.fromEntries(edits.map(edit => [edit.index, saved.row_revisions[edit.index]])) };
            }
            queuedEdits.forEach(edit => {
                state.tokens[edit.index] = result.row_revisions[edit.index];
                if (Array.isArray(state.baseDefinitions)) { state.baseDefinitions[edit.index] = { ...edit.row }; }
                state.conflicted.delete(edit.index);
                if (state.edits.get(edit.index)?.revision === edit.revision) { state.edits.delete(edit.index); }
            });
            state.completedSequence = Math.max(state.completedSequence || 0, order || value.revision || 0);
            state.conflictLatch = false;
            markDatasetAck(value.name);
        }

        function isDatasetProjectLoading() {
            return dsbPendingProjectLoad !== null;
        }

        // Metadata and rows keep separate debounce slots (so a form edit does
        // not replace a queued row draft), but their COMMITS share one chain:
        // a metadata ACK can never be applied after a later row ACK carrying
        // an older snapshot of the same rows.
        let dsbCommitTail = null;
        let dsbCommitOrder = 0;
        function serializeDatasetCommit(write) {
            // Order is assigned when the write enters the committed chain, NOT
            // when its debounce slot was enqueued. A metadata change enqueued
            // first but committed last carries the authoritative after-state;
            // an enqueue-order guard would discard it as stale.
            const order = ++dsbCommitOrder;
            // When idle, run synchronously so the first commit is observable
            // immediately; later writes queue behind the previous one.
            const chained = dsbCommitTail !== null;
            let result;
            if (chained) {
                result = dsbCommitTail.then(() => write(order), () => write(order));
            } else {
                try { result = write(order); } catch (error) { result = Promise.reject(error); }
            }
            const settled = Promise.resolve(result).then(() => {}, () => {});
            dsbCommitTail = settled;
            settled.then(() => { if (dsbCommitTail === settled) { dsbCommitTail = null; } });
            return result;
        }

        function ensureDatasetMetaQueue() {
            if (!dsbSaveMetaQueue) {
                dsbSaveMetaQueue = createSerializedSaveQueue({
                    write: value => serializeDatasetCommit(async order => {
                        if (!datasetImplicitFlushAllowed(value.name)) {
                            throw new Error(t('workbench.rows.conflict.blocked', 'Dataset writes are paused until conflict recovery completes'));
                        }
                        const { revision, ...body } = value;
                        // Bind the metadata mutation to the same acknowledged
                        // rows as every other write. A matched CAS proves an
                        // invalidation is our own; a stale one is refused by
                        // the server instead of silently adopting an external
                        // edit into the visible model and its token.
                        const state = ensureDatasetRowSaveState(value.name);
                        const base = datasetOwnBase(state);
                        if (base) {
                            body.expected_count = base.count;
                            body.expected_revisions = Object.fromEntries(
                                base.revisions.map((token, index) => [index, token]));
                        }
                        Object.assign(body, datasetExpectedMetadata(value.name));
                        let result;
                        try {
                            result = await API.post('/api/dataset_builder/update_meta', body);
                        } catch (error) {
                            if (error.status === 409) {
                                state.conflictLatch = true;
                                state.conflictShown = false;
                                showDatasetConflict(value.name);
                            }
                            throw error;
                        }
                        state.conflictLatch = false;
                        markDatasetAck(value.name);
                        // Only the acknowledged metadata revision may retire a
                        // draft. A newer typed voice keeps its controls and base.
                        const draft = state.metadataDraft;
                        const settlesDraft = !draft || value.revision >= draft.revision;
                        if (settlesDraft) {
                            state.metadataDraft = null;
                        } else if (draft) {
                            // This receipt is OUR verified predecessor. Advance
                            // the confirmed metadata and rebase the successor's
                            // expectation onto it, without repainting its newer
                            // typed values. External observations never do this.
                            applyDatasetMetadataAck(value.name, result, { own: true, syncControls: false });
                            draft.expected = { description: result.description,
                                global_seed: result.global_seed == null ? '' : String(result.global_seed) };
                        }
                        if (Array.isArray(result.samples) && Array.isArray(result.row_revisions)) {
                            reconcileDatasetRows(value.name, result.samples, result.row_revisions,
                                                 { own: true, sequence: order,
                                                   metadata: settlesDraft ? result : null });
                        } else if (settlesDraft) {
                            applyDatasetMetadataAck(value.name, result);
                        }
                        return result;
                    }),
                    onError: error => _toastSaveError('meta', error),
                });
            }
            return dsbSaveMetaQueue;
        }

        function ensureDatasetRowsQueue() {
            if (!dsbSaveRowsQueue) {
                dsbSaveRowsQueue = createSerializedSaveQueue({
                    write: value => serializeDatasetCommit(order => {
                        if (!datasetImplicitFlushAllowed(value.name)) {
                            throw new Error(t('workbench.rows.conflict.blocked', 'Dataset writes are paused until conflict recovery completes'));
                        }
                        return saveDatasetRows(value, order);
                    }),
                    onError: error => _toastSaveError('rows', error),
                });
            }
            return dsbSaveRowsQueue;
        }

        function dsbSaveForm() {
            const name = dsbCurrentProject;
            if (!name || isDatasetProjectLoading()) { return; }
            const state = ensureDatasetRowSaveState(name);
            if (state.awaitingReplacement) { showReloadPendingBanner(name); return; }
            const description = document.getElementById('dsb-description').value;
            const globalSeed = document.getElementById('dsb-global-seed').value;
            const revision = ++state.sequence;
            // Capture the values and the expectation the draft was authored
            // against. A later poll or older ACK may advance the acknowledged
            // metadata; this form stays bound to what it actually saw.
            const ack = state.metadata || {};
            state.metadataDraft = { description, global_seed: globalSeed, revision,
                expected: { description: ack.description, global_seed: ack.global_seed } };
            ensureDatasetMetaQueue().enqueue({ name, description, global_seed: globalSeed, revision });
        }

        function dsbSaveRows(index = null) {
            const name = dsbCurrentProject;
            if (!name || isDatasetProjectLoading()) { return; }
            const state = ensureDatasetRowSaveState(name);
            if (state.awaitingReplacement) { showReloadPendingBanner(name); return; }
            const revision = ++state.sequence;
            let value;
            if (index !== null && state.tokens?.length === dsbRows.length
                && state.fullRevision === null && !state.fullConflict) {
                state.edits.set(index, { row: getDatasetRowDefinition(dsbRows[index]),
                    base: state.baseDefinitions?.[index] || null, revision });
                value = { name, count: dsbRows.length, revision,
                    edits: Array.from(state.edits, ([index, edit]) => ({ index, ...edit })) };
            } else {
                state.fullRevision = revision;
                state.edits.clear();
                value = { name, revision, rows: dsbRows.map(getDatasetRowDefinition),
                    count: dsbRows.length, revisions: state.tokens?.slice() || [] };
            }
            ensureDatasetRowsQueue().enqueue(value);
        }

        function isDatasetRowGenerationRunning() {
            return dsbBatchRunning || dsbRows.some(row => row.status === 'generating');
        }

        function ensureDatasetRowsEditable() {
            if (dsbCurrentProject && ensureDatasetRowSaveState(dsbCurrentProject).awaitingReplacement) {
                showReloadPendingBanner(dsbCurrentProject);
                return false;
            }
            if (isDatasetProjectLoading()) {
                showToast(t('workbench.rows.wait-load', 'Wait for the selected dataset to load before editing samples.'), 'warning');
                return false;
            }
            if (isDatasetRowGenerationRunning()) {
                showToast(t('workbench.rows.wait-generation', 'Wait for sample generation to finish before editing samples.'), 'warning');
                return false;
            }
            return true;
        }

        function dsbAddRow(emotion = '', text = '', seed = '') {
            if (!ensureDatasetRowsEditable()) { return; }
            dsbRows.push({ emotion, text, seed, status: 'pending', audio_url: null });
            dsbRenderTable();
            dsbSaveRows();
            // Focus the new emotion field
            setTimeout(() => {
                const rows = document.querySelectorAll('#dsb-table-body tr');
                const last = rows[rows.length - 1];
                if (last) { last.querySelector('input')?.focus(); }
            }, 50);
        }

        async function dsbRemoveRow(index) {
            if (!ensureDatasetRowsEditable()) { return; }
            const project = dsbCurrentProject;
            const row = dsbRows[index];
            if (!row) { return; }
            if (!await showConfirm(formatUiSafetyText('safety.workbench.sample.delete', 'Delete sample {index}? This removes its text and generated audio link and cannot be undone.', {index: index + 1}), {title: t('workbench.sample.delete.title', 'Delete sample?'), actionLabel: t('workbench.sample.delete.action', 'Delete sample'), danger: true})) { return; }
            if (!isDatasetProjectSelected(project) || dsbRows[index] !== row || !ensureDatasetRowsEditable()) { return; }
            dsbRows.splice(index, 1);
            dsbRenderTable();
            dsbSaveRows();
            dsbUpdateRefDropdown();
        }

        function datasetRowStatusLabel(status) {
            if (status === 'done') { return t('workbench.row.status.done', 'done'); }
            if (status === 'generating') { return t('workbench.row.status.generating', 'generating'); }
            if (status === 'error') { return t('workbench.row.status.error', 'error'); }
            return t('workbench.row.status.pending', 'pending');
        }

        function dsbBuildRowHtml(row, i) {
            const disabled = isDatasetRowGenerationRunning() ? 'disabled' : '';
            const statusColor = row.status === 'done' ? 'success' :
                                row.status === 'generating' ? 'warning' :
                                row.status === 'error' ? 'danger' : 'secondary';
            const statusLabel = datasetRowStatusLabel(row.status);

            let actionHtml = '';
            if (row.status === 'generating') {
                // One sample has no fraction to report: indeterminate dither.
                actionHtml = '<div class="tp tp-dither tp-sm tp-indeterminate" style="width:80px;" role="status" aria-label="' + escapeHtml(t('workbench.row.status.generating', 'generating')) + '"></div>';
            } else {
                const genLabel = row.status === 'done' ? '<i class="fas fa-redo"></i>' : '<i class="fas fa-play"></i>';
                actionHtml = getEscapedHtml`<button class="btn btn-sm btn-primary" ${disabled} onclick="dsbGenSample(${i})" title="${row.status === 'done' ? t('workbench.row.regenerate', 'Regenerate') : t('workbench.row.generate', 'Generate')}">` + genLabel + '</button>';
            }

            let audioHtml = '';
            if (row.status === 'done' && row.audio_url) {
                audioHtml = getEscapedHtml`<audio controls src="${row.audio_url}" style="width:180px;height:28px;" onplay="dsbStopOthers(${i})"></audio>`;
            }

            return getEscapedHtml`<tr data-dsb-idx="${i}" data-dsb-status="${row.status || 'pending'}" data-dsb-audio="${row.audio_url || ''}" class="${row.status === 'generating' ? 'table-info' : ''}">
                <td class="text-center align-middle">${i + 1}</td>
                <td><input type="text" class="form-control form-control-sm" aria-label="${t('workbench.row.emotion.aria-label', 'Emotion or delivery style for sample {index}', {index: i + 1})}" ${disabled} value="${row.emotion || ''}" oninput="dsbUpdateRow(${i}, 'emotion', this.value)" placeholder="${t('workbench.row.emotion.placeholder', 'e.g. Savagely sarcastic')}"></td>
                <td><textarea class="form-control form-control-sm" aria-label="${t('workbench.row.text.aria-label', 'Sample text for sample {index}', {index: i + 1})}" rows="2" ${disabled} oninput="dsbUpdateRow(${i}, 'text', this.value)" placeholder="${t('workbench.row.text.placeholder', 'Sample text...')}">${row.text || ''}</textarea></td>
                <td><input type="number" class="form-control form-control-sm" aria-label="${t('workbench.row.seed.aria-label', 'Seed for sample {index}; blank or -1 uses the global seed; 0 or higher overrides it', {index: i + 1})}" ${disabled} value="${row.seed ?? ''}" oninput="dsbUpdateRow(${i}, 'seed', this.value)" placeholder="-" style="width:65px;" min="-1"></td>
                <td class="text-center align-middle"><span class="badge bg-${statusColor}">${statusLabel}</span>${row.status === 'error' ? row.error || '' : ''}</td>
                <td class="align-middle">
                    <div class="d-flex align-items-center gap-1">
                    ` + actionHtml + audioHtml + getEscapedHtml`
                        <button class="btn btn-sm btn-outline-danger ms-auto" aria-label="${t('workbench.row.delete.aria-label', 'Delete sample {index}', {index: i + 1})}" ${disabled} onclick="dsbRemoveRow(${i})" title="${t('workbench.row.delete.title', 'Delete row')}"><i class="fas fa-trash"></i></button>
                    </div>
                </td>
            </tr>`;
        }

        function applyDatasetTrainingSaveFeedback() {
            const status = document.getElementById('dsb-save-status');
            if (!dsbTrainingSaved || dsbTrainingSaved.name !== dsbCurrentProject) {
                status.textContent = '';
                return;
            }
            if (dsbTrainingSaved.snapshot !== JSON.stringify(dsbRows)) { dsbTrainingSaved.stale = true; }
            if (dsbTrainingSaved.stale) {
                status.textContent = t('workbench.training.stale', 'Training dataset saved before these changes. Save again to include them.');
            } else {
                status.textContent = dsbTrainingSaved.count === 1
                    ? t('workbench.training.saved.one', 'Saved! {count} sample.', {count: dsbTrainingSaved.count})
                    : t('workbench.training.saved.many', 'Saved! {count} samples.', {count: dsbTrainingSaved.count});
            }
        }

        function dsbRowOwnsLiveControl(index, tbody) {
            const state = ensureDatasetRowSaveState(dsbCurrentProject);
            if (state.edits.has(index)) { return true; }
            const row = tbody?.children?.[index];
            const active = document.activeElement;
            return !!(row && active && typeof row.contains === 'function' && row.contains(active)
                && 'value' in active);
        }

        // A protected input must never be detached, but its badge and audio
        // cell are independent of the draft: repaint just those two cells so
        // a local edit that saves does not leave an obsolete player behind.
        function dsbRepaintRowStatus(index, row) {
            const tbody = document.getElementById('dsb-table-body');
            const existing = tbody?.children?.[index];
            if (!existing || !existing.children || existing.children.length < 6) { return false; }
            const temp = document.createElement('tbody');
            temp.innerHTML = dsbBuildRowHtml(row, index);
            const newCells = temp.firstElementChild.children;
            const statusCell = newCells[4];
            const actionsCell = newCells[5];
            existing.children[4].replaceWith(statusCell);
            existing.children[5].replaceWith(actionsCell);
            existing.setAttribute('data-dsb-status', row.status || 'pending');
            existing.setAttribute('data-dsb-audio', row.audio_url || '');
            return true;
        }

        function dsbRenderTable(changedIndices, force = false) {
            applyDatasetTrainingSaveFeedback();
            const tbody = document.getElementById('dsb-table-body');

            // Full rebuild, unless a live draft/focused control must survive it.
            // An authoritative replacement explicitly discards those controls:
            // keeping a detached draft in the DOM would write old text under
            // the newly accepted revision.
            if (force || !changedIndices || tbody.children.length !== dsbRows.length) {
                if (!force && tbody.children.length === dsbRows.length
                    && dsbRows.some((row, i) => dsbRowOwnsLiveControl(i, tbody))) {
                    changedIndices = dsbRows.map((row, i) => i);
                } else {
                    tbody.innerHTML = dsbRows.map((row, i) => dsbBuildRowHtml(row, i)).join('');
                    dsbUpdateProgress();
                    return;
                }
            }

            // Targeted update: only re-render changed rows
            for (const i of changedIndices) {
                const existing = tbody.children[i];
                if (!existing) { continue; }
                const row = dsbRows[i];
                const protectedRow = dsbRowOwnsLiveControl(i, tbody);
                const oldStatus = existing.getAttribute('data-dsb-status');
                const oldAudio = existing.getAttribute('data-dsb-audio');
                if (oldStatus === (row.status || 'pending') && oldAudio === (row.audio_url || '')) { continue; }
                if (protectedRow) {
                    // Never detach a focused/dirty control: a native change on
                    // detach would otherwise commit a stale visible value. Its
                    // status and audio cells can still follow the acknowledged
                    // state without touching the protected input.
                    dsbRepaintRowStatus(i, row);
                    continue;
                }
                const temp = document.createElement('tbody');
                temp.innerHTML = dsbBuildRowHtml(row, i);
                existing.replaceWith(temp.firstElementChild);
            }
            dsbUpdateProgress();
        }

        window.dsbUpdateRow = (index, field, value) => {
            if (!ensureDatasetRowsEditable()) { return; }
            if (dsbRows[index]) {
                const changed = dsbRows[index][field] !== value;
                dsbRows[index][field] = value;
                if (changed) {
                    dsbRows[index].status = 'pending';
                    dsbRows[index].audio_url = null;
                    dsbRenderTable([index]);
                    dsbSaveRows(index);
                }
            }
        };

        window.dsbStopOthers = (index) => {
            document.querySelectorAll('#dsb-table-body audio').forEach(audio => {
                const row = audio.closest('tr');
                if (row && parseInt(row.getAttribute('data-dsb-idx')) !== index) { audio.pause(); }
            });
        };

        let dsbLastDoneIndicesKey = null;

        function dsbUpdateProgress() {
            const done = dsbRows.filter(r => r.status === 'done').length;
            const total = dsbRows.length;
            const pct = total > 0 ? Math.round((done / total) * 100) : 0;
            const wrap = document.getElementById('dsb-progress-wrap');
            const bar = document.getElementById('dsb-progress-bar');
            if (done > 0 || dsbBatchRunning) {
                wrap.style.display = '';
                // Hatch: the dataset-build texture (#2042).
                renderTexturedProgress(bar, {
                    fraction: total > 0 ? done / total : 0,
                    label: `${pct}% (${done}/${total})`,
                });
            } else {
                wrap.style.display = 'none';
            }
            // Rebuild when the project or completed-row indices change.
            const doneIndicesKey = JSON.stringify([dsbCurrentProject,
                dsbRows.map((r, i) => r.status === 'done' ? i : null).filter(i => i !== null)]);
            if (doneIndicesKey !== dsbLastDoneIndicesKey) {
                dsbLastDoneIndicesKey = doneIndicesKey;
                dsbUpdateRefDropdown();
            }
        }

        function dsbUpdateRefDropdown() {
            const select = document.getElementById('dsb-ref-select');
            const doneSamples = dsbRows.map((r, i) => ({ index: i, row: r })).filter(x => x.row.status === 'done');
            select.innerHTML = doneSamples.length === 0
                ? '<option value="0">' + t('workbench.refs.none', 'No completed samples yet') + '</option>'
                : doneSamples.map(x => {
                    const emotion = x.row.emotion || 'neutral';
                    const text = x.row.text || '';
                    return `<option value="${x.index}">${x.index + 1}. ${escapeHtml(emotion.substring(0, 30))}${emotion.length > 30 ? '…' : ''} - "${escapeHtml(text.substring(0, 40))}"${text.length > 40 ? '…' : ''}</option>`;
                }).join('');
        }

        // Single sample generation
        window.dsbGenSample = async (index) => {
            if (!ensureDatasetRowsEditable()) { return; }
            const name = dsbCurrentProject;
            if (!name) { showToast(t('workbench.error.no-project', 'Select or create a project first.'), 'warning'); return; }
            const row = dsbRows[index];
            if (!row || !row.text.trim()) { showToast(t('workbench.error.row-no-text', 'This row has no text.'), 'warning'); return; }
            const sequence = dsbBatchStartSequence;
            if (!datasetImplicitFlushAllowed(name)) { return; }
            try {
                await dsbSaveRowsQueue?.flush();
                await dsbSaveMetaQueue?.flush();
            } catch (error) {
                _toastSaveError('rows', error);
                return;
            }
            if (sequence !== dsbBatchStartSequence || !isDatasetProjectSelected(name)
                || dsbRows[index] !== row || !ensureDatasetRowsEditable()) { return; }
            const rootDesc = document.getElementById('dsb-description').value.trim();
            if (!rootDesc) { showToast(t('workbench.error.no-root-description', 'Enter a root voice description first.'), 'warning'); return; }

            const definition = JSON.stringify(getDatasetRowDefinition(row));
            const isCurrent = () => isDatasetProjectSelected(name) && dsbRows[index] === row
                && JSON.stringify(getDatasetRowDefinition(row)) === definition;

            const emotion = row.emotion.trim();
            const description = emotion ? `${rootDesc}, ${emotion}` : rootDesc;

            // Resolve seed: per-line > global > random
            let seed, inheritSeed;
            try {
                const globalSeed = getDatasetSeed(document.getElementById('dsb-global-seed').value);
                const lineSeed = getDatasetSeed(row.seed);
                inheritSeed = lineSeed < 0;
                seed = inheritSeed ? globalSeed : lineSeed;
            } catch (e) { showToast(e.message, 'error'); return; }

            const tokens = ensureDatasetRowSaveState(name).tokens;
            const expectedRevision = tokens?.[index];
            const request = {
                description,
                text: getDatasetRowDefinition(row).text,
                dataset_name: name,
                sample_index: index,
                seed,
                inherit_seed: inheritSeed,
                expected_count: dsbRows.length,
                ...datasetExpectedMetadata(name),
            };
            if (/^[0-9a-f]{64}$/.test(expectedRevision || '')) { request.expected_revision = expectedRevision; }

            // Optimistic UI
            dsbRows[index].status = 'generating';
            dsbRows[index].error = '';
            dsbRenderTable();

            try {
                const result = await API.post('/api/dataset_builder/generate_sample', request);
                if (!isCurrent()) { return; }
                // A token only advances with its authoritative visible row;
                // never adopt it from a bare revision on an unrecognised ACK.
                applyDatasetRowAck(name, index, result.row, result.row_revision);
                dsbRows[index].status = 'done';
                dsbRows[index].audio_url = result.audio_url;
            } catch (e) {
                if (!isCurrent()) { return; }
                const committed = e.detail && typeof e.detail === 'object' ? e.detail : null;
                if (committed) { applyDatasetRowAck(name, index, committed.row, committed.row_revision); }
                dsbRows[index].status = 'error';
                dsbRows[index].error = getActionErrorMessage(t('workbench.sample.generate-failed', 'Sample generation failed'), e, t('workbench.sample.generate-failed.guidance', 'Check the sample audio and dataset task status before generating again. Review the voice description and TTS configuration if the request was refused.'));
                showToast(dsbRows[index].error, 'error', 10000);
                console.error('Sample generation failed:', e);
            }
            dsbRenderTable();
        };

        // Batch generation
        window.dsbGenerateAll = async (regenAll = false) => {
            if (!ensureDatasetRowsEditable()) { return; }
            const rows = dsbRows;
            const name = dsbCurrentProject;
            const state = ensureDatasetRowSaveState(name);
            const rootDesc = document.getElementById('dsb-description').value.trim();
            if (!name) { showToast(t('workbench.error.no-project', 'Select or create a project first.'), 'warning'); return; }
            if (!rootDesc) { showToast(t('workbench.error.no-root-description', 'Enter a root voice description first.'), 'warning'); return; }
            if (!dsbRows.some(r => r.text.trim())) { showToast(t('workbench.batch.no-samples', 'Add at least one sample with text.'), 'warning'); return; }

            const sequence = ++dsbBatchStartSequence;
            dsbPendingStart = sequence;
            applyDatasetBatchState(false, true);

            // A pre-send flush refusal sent NO generation request. Keep the
            // visible project intact and do not poll a forgiving status
            // endpoint that would reconcile an empty list over these rows.
            if (!datasetImplicitFlushAllowed(name)) {
                if (dsbPendingStart === sequence) { dsbPendingStart = null; applyDatasetBatchState(false); }
                return;
            }
            try {
                await dsbSaveRowsQueue?.flush();
                await dsbSaveMetaQueue?.flush();
                // A superseded terminal GET may still be refreshing identity.
                // Derive the eligible rows only after that read settles.
                if (state.refreshPromise) { await state.refreshPromise; }
            } catch (error) {
                if (dsbPendingStart === sequence) { dsbPendingStart = null; applyDatasetBatchState(false); }
                _toastSaveError('rows', error);
                return;
            }
            const release = () => {
                if (dsbPendingStart === sequence) { dsbPendingStart = null; applyDatasetBatchState(false); }
            };
            if (sequence !== dsbBatchStartSequence || !isDatasetProjectSelected(name)
                || dsbRows !== rows) { release(); return; }

            // Select only AFTER the flush: metadata invalidation can make an
            // all-done list eligible again, and skipping it used to submit
            // nothing after the UI had already announced completion.
            const indices = regenAll
                ? dsbRows.map((_, i) => i).filter(i => dsbRows[i].text.trim())
                : dsbRows.map((r, i) => i).filter(i => dsbRows[i].text.trim() && dsbRows[i].status !== 'done');
            if (indices.length === 0) { release(); showToast(t('workbench.batch.all-generated', 'All samples are already generated.'), 'warning'); return; }
            if (regenAll && !await showConfirm(formatUiSafetyText('safety.workbench.batch.regenerate', 'Regenerate all {count} samples?', {count: indices.length}), {title: t('workbench.batch.regenerate.title', 'Replace all sample audio?'), actionLabel: t('workbench.batch.regenerate.action', 'Regenerate all'), danger: true})) { release(); return; }
            if (sequence !== dsbBatchStartSequence || !isDatasetProjectSelected(name) || dsbRows !== rows) { release(); return; }

            const seedInput = document.getElementById('dsb-global-seed');
            let globalSeed, perSeeds;
            try {
                globalSeed = getDatasetSeed(seedInput.value);
                perSeeds = dsbRows.map(row => getDatasetSeed(row.seed));
            } catch (e) { release(); showToast(e.message, 'error'); return; }
            const previous = indices.map(index => ({ index, row: dsbRows[index], status: dsbRows[index].status }));

            indices.forEach(i => { dsbRows[i].status = 'generating'; });
            dsbRenderTable();
            document.getElementById('dsb-logs').style.display = '';

            const abort = () => {
                if (dsbPendingStart !== sequence) { return; }
                dsbPendingStart = null;
                previous.forEach(({ index, row, status }) => {
                    if (dsbRows[index] === row && row.status === 'generating') { row.status = status; }
                });
                applyDatasetBatchState(false);
                dsbRenderTable();
            };
            const tokens = ensureDatasetRowSaveState(name).tokens;
            const revisions = {};
            let complete = true;
            for (const index of indices) {
                const revision = tokens?.[index];
                if (/^[0-9a-f]{64}$/.test(revision || '')) { revisions[index] = revision; }
                else { complete = false; }
            }
            const request = {
                name,
                description: rootDesc,
                samples: dsbRows.map(r => {
                    const definition = getDatasetRowDefinition(r);
                    return { emotion: definition.emotion, text: definition.text };
                }),
                indices,
                global_seed: globalSeed,
                seeds: perSeeds,
                expected_count: dsbRows.length,
                ...datasetExpectedMetadata(name),
            };
            if (complete) { request.expected_revisions = revisions; }
            try {
                await API.post('/api/dataset_builder/generate_batch', request);
                if (sequence !== dsbBatchStartSequence || !isDatasetProjectSelected(name)) { return; }
                dsbPendingStart = null;
                applyDatasetBatchState(true);
                dsbStartPolling(name);
            } catch (e) {
                if (sequence !== dsbBatchStartSequence || !isDatasetProjectSelected(name)) { return; }
                dsbPendingStart = null;
                previous.forEach(({ index, row, status }) => {
                    if (dsbRows[index] === row && row.status === 'generating') { row.status = status; }
                });
                showActionError(t('workbench.batch.unconfirmed', 'Dataset batch generation is unconfirmed'), e, t('workbench.batch.unconfirmed.guidance', 'Check dataset task status and the saved samples before starting again; some samples may already have completed.'));
                applyDatasetBatchState(false, true);
                dsbRenderTable();
                // A lost response can follow accepted work. Keep edits blocked
                // until the existing status poller reconciles the server state.
                dsbStartPolling(name, false);
            }
        };

        function dsbStartPolling(name, admitted = true) {
            if (dsbPolling) { dsbPolling(); }
            const logsEl = document.getElementById('dsb-logs');
            const renderLogs = createTaskLogRenderer(logsEl);
            dsbPolling = _startPolling(`dataset_builder:${name}`, () => datasetFetchStatus(name), {
                intervalMs: 2000,
                doneCheck: result => !!result && !result.running && !result.ownership_unconfirmed,
                onTick: result => {
                    if (!result || !isDatasetProjectSelected(name)) { return; }
                    const state = ensureDatasetRowSaveState(name);
                    const stale = typeof result.__dsbDispatchGeneration === 'number'
                        && result.__dsbDispatchGeneration < (state.lastAckGeneration || 0);
                    if (stale) {
                        // A GET sent before a newer own ACK cannot revive its
                        // rows, audio, revisions or metadata. Refresh identity
                        // from a current read instead of installing it.
                        const refresh = datasetFetchStatus(name).then(fresh => {
                            if (!fresh) { return; }
                            reconcileDatasetRows(name, fresh.samples || [], fresh.row_revisions,
                                                 { own: false, metadata: fresh });
                        }).catch(() => {});
                        state.refreshPromise = refresh;
                        refresh.then(() => { if (state.refreshPromise === refresh) { state.refreshPromise = null; } });
                        return;
                    }
                    const wasBusy = dsbBatchRunning;
                    applyDatasetBatchState(!!(result.running || result.ownership_unconfirmed));
                    if (result.running || result.ownership_unconfirmed) { admitted = true; }
                    const serverSamples = result.samples || [];
                    const before = dsbRows.map(row => ({ status: row.status, audio_url: row.audio_url }));
                    const beforeCount = dsbRows.length;
                    reconcileDatasetRows(name, serverSamples, result.row_revisions,
                                         { own: false, metadata: result });
                    const added = dsbRows.length !== beforeCount;
                    const changed = [];
                    dsbRows.forEach((row, index) => {
                        if (index < before.length
                            && (row.status !== before[index].status || row.audio_url !== before[index].audio_url)) {
                            changed.push(index);
                        }
                    });

                    if (added || wasBusy !== dsbBatchRunning) {
                        dsbRenderTable();
                    } else if (changed.length > 0) {
                        dsbRenderTable(changed);
                    }

                    // Update logs
                    if (result.logs && result.logs.length > 0) {
                        logsEl.style.display = '';
                    }
                    renderLogs(result);


                },
                onDone: status => {
                    if (!isDatasetProjectSelected(name)) { return; }
                    if (admitted) { notifyJobDone('dataset_builder', '', 'finished', status); }
                    dsbStopBatch();
                }
            });
        }

        function dsbStopBatch() {
            dsbBatchStartSequence++;
            dsbPendingStart = null;
            applyDatasetBatchState(false);
            if (dsbPolling) { dsbPolling(); dsbPolling = null; }
            dsbRenderTable();
        }

        window.dsbCancel = () => {
            if (!dsbCurrentProject) { return; }
            return cancelTask(`/api/dataset_builder/cancel?name=${encodeURIComponent(dsbCurrentProject)}`);
        };

        // Import / Export
        window.dsbImport = (event) => {
            if (!ensureDatasetRowsEditable()) { return; }
            const file = event.target.files[0];
            if (!file) { return; }
            const project = dsbCurrentProject;
            const rows = dsbRows;
            const snapshot = JSON.stringify(rows);
            const reader = new FileReader();
            reader.onload = async (e) => {
                if (!isDatasetProjectSelected(project) || dsbRows !== rows) { return; }
                if (!ensureDatasetRowsEditable()) { return; }
                try {
                    const data = JSON.parse(e.target.result);
                    if (!Array.isArray(data)) { throw new Error(t('workbench.import.not-array', 'Expected JSON array')); }
                    const importedRows = data.map((item, index) => {
                        if (!item || typeof item !== 'object' || Array.isArray(item)) {
                            throw new Error(t('workbench.import.not-object', 'Sample {index} must be an object', {index: index + 1}));
                        }
                        for (const field of ['text', 'emotion', 'instruct']) {
                            if (item[field] != null && typeof item[field] !== 'string') {
                                throw new Error(t('workbench.import.field-string', 'Sample {index} {field} must be a string', {index: index + 1, field}));
                            }
                        }
                        if (item.seed != null && typeof item.seed !== 'number' && typeof item.seed !== 'string') {
                            throw new Error(t('workbench.import.seed-type', 'Sample {index} seed must be a number or string', {index: index + 1}));
                        }
                        return {
                            emotion: item.emotion || item.instruct || '',
                            text: item.text || '',
                            seed: item.seed ?? '',
                            status: 'pending',
                            audio_url: null,
                        };
                    });
                    if (dsbRows.length && !await showConfirm(formatUiSafetyText('safety.workbench.import.replace', 'Import {count} samples into "{project}"? This will replace the {rows} rows and their generated audio links in this project. Continue?', {count: importedRows.length, project, rows: dsbRows.length}), {title: t('workbench.import.replace.title', 'Replace project samples?'), actionLabel: t('workbench.import.replace.action', 'Import samples'), danger: true})) { return; }
                    if (!isDatasetProjectSelected(project) || dsbRows !== rows || !ensureDatasetRowsEditable()) { return; }
                    if (JSON.stringify(dsbRows) !== snapshot) {
                        showToast(t('workbench.import.changed', 'The samples changed while importing. Select the file again to review the replacement.'), 'warning');
                        return;
                    }
                    dsbRows = importedRows;
                    dsbRenderTable();
                    dsbSaveRows();
                } catch (err) {
                    showToast(t('workbench.import.failed', 'Import failed: {detail}', {detail: err.message}), 'error');
                }
            };
            reader.readAsText(file);
            event.target.value = '';  // reset file input
        };

        function datasetExportRows() {
            return dsbRows.map(r => {
                const entry = { emotion: r.emotion, text: r.text };
                if (r.seed !== '' && r.seed != null) { entry.seed = getDatasetSeed(r.seed); }
                return entry;
            });
        }

        function dsbDownloadJson(data, suffix) {
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${dsbCurrentProject || 'dataset'}_${suffix}.json`;
            a.click();
            URL.revokeObjectURL(url);
        }

        window.dsbExport = () => {
            let data;
            try { data = datasetExportRows(); } catch (e) { showToast(e.message, 'error'); return; }
            dsbDownloadJson(data, 'script');
        };

        // Save as training dataset
        window.dsbSave = async () => {
            if (dsbTrainingSavePending) { return; }
            const name = dsbCurrentProject;
            if (!name) { showToast(t('workbench.error.no-project', 'Select or create a project first.'), 'warning'); return; }
            if (!ensureDatasetRowsEditable()) { return; }
            const doneSamples = dsbRows.filter(row => row.status === 'done');
            if (!doneSamples.length) { showToast(t('workbench.training.no-completed', 'No completed samples to save. Generate some first.'), 'warning'); return; }
            const snapshot = JSON.stringify(dsbRows);
            const refIdx = parseInt(document.getElementById('dsb-ref-select').value) || 0;
            const status = document.getElementById('dsb-save-status');
            dsbTrainingSavePending = true;
            try {
                if (!await showConfirm(doneSamples.length === 1
                    ? t('workbench.training.save.one', 'Save "{project}" as training dataset with {count} sample?', {project: name, count: doneSamples.length})
                    : t('workbench.training.save.many', 'Save "{project}" as training dataset with {count} samples?', {project: name, count: doneSamples.length}), {title: t('workbench.training.save.title', 'Save training dataset?'), actionLabel: t('workbench.training.save.action', 'Save dataset'), danger: false})) { return; }
                const isCurrent = () => isDatasetProjectSelected(name) && snapshot === JSON.stringify(dsbRows)
                    && refIdx === (parseInt(document.getElementById('dsb-ref-select').value) || 0) && !dsbBatchRunning;
                if (!isCurrent()) { showToast(t('workbench.training.changed', 'The dataset changed. Review the current samples before saving.'), 'warning'); return; }
                await Promise.all([dsbSaveMetaQueue?.flush(), dsbSaveRowsQueue?.flush()]);
                if (!isCurrent()) { showToast(t('workbench.training.changed-while-saving', 'The dataset changed while saving edits. Review it before saving the training dataset.'), 'warning'); return; }
                status.textContent = t('workbench.training.saving', 'Saving training dataset…');
                const result = await API.post('/api/dataset_builder/save', { name, ref_index: refIdx });
                dsbTrainingSaved = { name, snapshot, count: result.sample_count, stale: !isCurrent() };
                if (isDatasetProjectSelected(name)) { applyDatasetTrainingSaveFeedback(); }
            } catch (e) {
                if (isDatasetProjectSelected(name)) {
                    status.textContent = t('workbench.training.save-unconfirmed', 'Training dataset save could not be confirmed. Check the Training dataset list before retrying. Your project rows are still here.');
                }
                console.error('Training dataset save failed:', e);
            } finally {
                dsbTrainingSavePending = false;
            }
        };

        // Persist on input changes
        document.getElementById('dsb-description')?.addEventListener('input', dsbSaveForm);
        document.getElementById('dsb-global-seed')?.addEventListener('input', dsbSaveForm);

        // The build this tab was served with (stamped server-side). Stays the
        // literal placeholder if the page was opened as a raw file → treat as
        // unknown and never warn.
        const PAGE_BUILD = (document.querySelector('meta[name="app-build"]')?.content || '').trim();
        const PAGE_BUILD_KNOWN = PAGE_BUILD && PAGE_BUILD !== '__APP_BUILD__';

        // Reuses the /api/system/stats poll below — adds no timer. Shows the
        // non-destructive banner only on a real mismatch; unknown either side is
        // informational. Once dismissed/shown it never forces a reload.
        function checkStaleBuild(currentBuild) {
            const banner = document.getElementById('stale-build-banner');
            if (!banner) { return; }
            const current = (currentBuild || '').trim();
            if (!PAGE_BUILD_KNOWN || !current) { return; }
            if (current !== PAGE_BUILD) {
                banner.style.display = 'block';
            }
        }

        function applySystemStatusText(id, text) {
            const element = document.getElementById(id);
            if (element && element.textContent !== text) { element.textContent = text; }
        }

        let _systemStatsPending = false;
        async function updateSystemStats() {
            if (_systemStatsPending) { return; }
            _systemStatsPending = true;
            try {
                const stats = await API.get('/api/system/stats');
                const buildWrap = document.getElementById('sys-build');
                const gpuWrap = document.getElementById('sys-gpu');
                const diskWrap = document.getElementById('sys-disk');

                const runtime = stats.runtime || {};
                applySystemStatusText('sys-build-val', runtime.short_revision
                    ? t('core.system.build', 'build {revision}', {revision: runtime.short_revision})
                    : t('core.system.build-unknown', 'build unknown'));
                checkStaleBuild(runtime.short_revision);
                const packageVersions = Object.entries(runtime.packages || {})
                    .filter(item => item[1])
                    .map(item => `${item[0]} ${item[1]}`)
                    .join(', ');
                buildWrap.title = [
                    runtime.revision ? t('core.system.revision', 'Revision: {revision}', {revision: runtime.revision}) : t('core.system.revision-unavailable', 'Revision unavailable'),
                    runtime.branch ? t('core.system.branch', 'Branch: {branch}', {branch: runtime.branch}) : '',
                    runtime.python ? `Python ${runtime.python}` : '',
                    packageVersions,
                ].filter(Boolean).join('\n');

                if (stats.gpu_mismatch) {
                    // A GPU is physically present but torch can't use it - everything
                    // is silently running on CPU. Worth a much louder signal than the
                    // normal VRAM-pressure red, since this is a broken install, not
                    // just "busy right now".
                    applySystemStatusText('sys-gpu-val', t('core.system.cpu-fallback', 'CPU fallback!'));
                    gpuWrap.title = t('core.system.gpu-mismatch', '{vendor} GPU was detected on this system, but the installed torch build cannot use it - generation and training will run on CPU and be dramatically slower. This usually means torch/torchaudio is the wrong build for this GPU; re-run install.js to fix it.', {vendor: stats.gpu_mismatch_vendor || 'A'});
                    gpuWrap.classList.add('text-danger');
                    gpuWrap.classList.remove('text-light');
                } else if (stats.gpu) {
                    const used = stats.gpu.reserved_gb.toFixed(1);
                    const total = stats.gpu.total_gb.toFixed(1);
                    applySystemStatusText('sys-gpu-val', t('core.system.gpu-used', '{used}/{total} GB', {used, total}));
                    gpuWrap.title = '';
                    if (stats.gpu.allocated_percent > 90) {
                        gpuWrap.classList.add('text-danger');
                        gpuWrap.classList.remove('text-light');
                    } else {
                        gpuWrap.classList.remove('text-danger');
                        gpuWrap.classList.add('text-light');
                    }
                } else {
                    applySystemStatusText('sys-gpu-val', t('core.system.gpu-unavailable', 'Unavailable'));
                    gpuWrap.classList.remove('text-danger');
                    gpuWrap.classList.add('text-light');
                    gpuWrap.title = '';
                }

                applySystemStatusText('sys-disk-val', t('core.system.disk-free', '{free} GB', {free: stats.disk.free_gb}));
                applySystemStatusText('sys-gpu-warning', stats.gpu_mismatch
                    ? t('core.system.gpu-warning-mismatch', 'GPU unavailable; using CPU.')
                    : stats.gpu?.allocated_percent > 90 ? t('core.system.gpu-warning-pressure', 'High GPU memory usage.') : '');
                applySystemStatusText('sys-disk-warning', stats.disk.low_space ? t('core.system.disk-warning', 'Low disk space.') : '');
                applySystemStatusText('sys-status-error', '');
                if (stats.disk.low_space) {
                    diskWrap.classList.add('text-danger');
                    diskWrap.classList.remove('text-light');
                } else {
                    diskWrap.classList.remove('text-danger');
                    diskWrap.classList.add('text-light');
                }
            } catch (e) {
                applySystemStatusText('sys-status-error', t('core.system.readings-stale', 'System readings could not be refreshed. Showing last available readings; check the app connection.'));
                console.error('Failed to update system stats', e);
            }
            finally { _systemStatsPending = false; }
        }

        // Format a duration in seconds as a short "1h 5m" / "5m 30s" / "30s" string.
        function formatDuration(seconds) {
            if (seconds == null || !isFinite(seconds) || seconds < 0) { return '--'; }
            seconds = Math.round(seconds);
            if (seconds < 60) { return t('core.duration.seconds', '{seconds}s', {seconds}); }
            const m = Math.floor(seconds / 60);
            if (seconds < 3600) { return t('core.duration.minutes', '{minutes}m {seconds}s', {minutes: m, seconds: seconds % 60}); }
            const h = Math.floor(seconds / 3600);
            return t('core.duration.hours', '{hours}h {minutes}m', {hours: h, minutes: Math.floor((seconds % 3600) / 60)});
        }

        // Always-visible "what's running and how long until it's done" indicator,
        // shown next to the GPU/disk stats so it's visible from any tab.
        let _etaStatusPending = false;
        let _etaStatusSnapshot = null;
        function renderEtaLocaleChrome() {
            const eta = _etaStatusSnapshot;
            if (eta?.ownership_unconfirmed) {
                applySystemStatusText('sys-eta-val', getTaskOwnershipMessage(eta));
                return;
            }
            if (!eta?.running) { return; }
            let text = eta.task ? getTaskLabel(eta.task) : eta.label;
            if (eta.progress) { text = t('core.eta.with-progress', '{label} — {progress}', {label: text, progress: eta.progress}); }
            if (eta.eta_seconds != null) {
                text = t('core.eta.with-remaining', '{label} (ETA {remaining})', {label: text, remaining: formatDuration(eta.eta_seconds)});
            } else if (eta.elapsed_seconds != null) {
                text = t('core.eta.with-elapsed', '{label} (running {elapsed})', {label: text, elapsed: formatDuration(eta.elapsed_seconds)});
            }
            applySystemStatusText('sys-eta-val', text);
        }
        async function updateEtaStatus() {
            if (_etaStatusPending) { return; }
            const wrap = document.getElementById('sys-eta');
            const valEl = document.getElementById('sys-eta-val');
            _etaStatusPending = true;
            try {
                const eta = await API.get('/api/status/eta');
                _etaStatusSnapshot = eta;
                if (!eta.running && !eta.ownership_unconfirmed) {
                    if (wrap.style.display !== 'none') { wrap.style.display = 'none'; }
                    return;
                }
                renderEtaLocaleChrome();
                if (valEl.title) { valEl.title = ''; }
                if (wrap.style.display !== 'flex') { wrap.style.display = 'flex'; }
            } catch (e) {
                _etaStatusSnapshot = null;
                console.error('Failed to update ETA status', e);
                if (wrap.style.display !== 'none' && valEl.textContent) {
                    if (!valEl.title) { valEl.title = valEl.textContent; }
                    applySystemStatusText('sys-eta-val', t('core.eta.unavailable', 'Status unavailable — a task may still be running'));
                }
            } finally {
                _etaStatusPending = false;
            }
        }

        function pollLmStudioStatus() {
            const setup = document.getElementById('setup-tab');
            if (document.hidden || !setup || setup.style.display === 'none') { return; }
            return refreshLmStudioStatus();
        }

        async function refreshLmStudioStatus() {
            const badge = document.getElementById('lmstudio-status-badge');
            const toggle = document.getElementById('lmstudio-optimize-toggle');
            if (!badge || !toggle) { return; }
            if (toggle._optimizePending) { return; }
            const request = {};
            badge._statusRequest = request;
            const isCurrent = () => badge._statusRequest === request && !toggle._optimizePending;
            try {
                const status = await API.get('/api/lmstudio/status');
                if (!isCurrent()) { return; }
                if (status.remote) {
                    badge.textContent = t('core.lmstudio.remote', 'Remote (optimize via SSH)');
                    toggle.checked = !!status.optimized;
                    badge.className = 'badge bg-info text-dark';
                    toggle.disabled = false;
                } else if (!status.available) {
                    badge.textContent = t('core.lmstudio.cli-missing', 'lms CLI not found');
                    badge.className = 'badge bg-secondary';
                    toggle.disabled = true;
                } else if (!status.loaded) {
                    badge.textContent = t('core.lmstudio.not-loaded', 'Model not loaded');
                    badge.className = 'badge bg-secondary';
                    toggle.disabled = false;
                    toggle.checked = false;
                } else if (status.optimized) {
                    badge.textContent = t('core.lmstudio.on', 'On (ctx {context}, parallel {parallel})', {context: status.context_length, parallel: status.parallel});
                    badge.className = 'badge bg-success';
                    toggle.disabled = false;
                    toggle.checked = true;
                } else {
                    badge.textContent = t('core.lmstudio.off', 'Off (ctx {context}, parallel {parallel})', {context: status.context_length, parallel: status.parallel});
                    badge.className = 'badge bg-warning text-dark';
                    toggle.disabled = false;
                    toggle.checked = false;
                }
            } catch (e) {
                if (!isCurrent()) { return; }
                badge.textContent = t('core.lmstudio.unavailable', 'Status unavailable');
                badge.className = 'badge bg-secondary';
            }
        }

        async function toggleLmStudioOptimize() {
            const toggle = document.getElementById('lmstudio-optimize-toggle');
            const badge = document.getElementById('lmstudio-status-badge');
            if (toggle._optimizePending) { return; }
            toggle._optimizePending = true;
            badge._statusRequest = {};
            const enable = toggle.checked;
            toggle.disabled = true;
            badge.textContent = t('core.lmstudio.applying', 'Applying...');
            badge.className = 'badge bg-secondary';
            try {
                await API.post('/api/lmstudio/optimize', { enable });
                showToast(enable
                    ? t('core.lmstudio.enabled', 'LM Studio set to VRAM-safe settings')
                    : t('core.lmstudio.reset', 'LM Studio reset to default settings'), 'success');
            } catch (e) {
                showActionError('LM Studio settings update is unconfirmed', e, 'Check the refreshed LM Studio status before changing settings again.');
                toggle.checked = !enable;
            } finally {
                toggle._optimizePending = false;
                toggle.disabled = false;
                await refreshLmStudioStatus();
            }
        }

        function reattachTaskActivity(taskName, buttonIds = [], statusId = null, afterDone = null, initialState = null) {
            claimTaskStart(taskName);
            const buttons = buttonIds.map(id => document.getElementById(id)).filter(Boolean);
            const statusEl = statusId ? document.getElementById(statusId) : null;
            const label = taskName.replaceAll('_', ' ');
            buttons.forEach(button => { button.disabled = true; });
            if (initialState?.ownership_unconfirmed) {
                const message = getTaskOwnershipMessage(initialState);
                if (statusEl) { statusEl.textContent = message; }
                else { showToast(message, 'warning'); }
            }
            else if (statusEl) { statusEl.textContent = t('core.task.restored-running', '{task} running...', {task: label}); }
            else { showToast(t('core.task.still-running', '{task} is still running.', {task: label}), 'info'); }
            _startPolling(`reattach:${taskName}`, () => API.get(`/api/status/${encodeURIComponent(taskName)}`), {
                doneCheck: state => !state.running && !state.ownership_unconfirmed,
                onTick: state => {
                    const last = (state.logs || []).slice(-1)[0];
                    if (statusEl && state.ownership_unconfirmed) { statusEl.textContent = getTaskOwnershipMessage(state); }
                    else if (statusEl && last) { statusEl.textContent = last; }
                },
                onDone: async state => {
                    releaseTaskStart(taskName);
                    buttons.forEach(button => { button.disabled = false; });
                    const message = (state.logs || []).slice(-1)[0] || t('core.task.finished-result-unavailable', '{task} finished; the original request result is unavailable after reload.', {task: label});
                    if (statusEl) { statusEl.textContent = message; }
                    else { showToast(message, 'info'); }
                    notifyJobDone(taskName, '', 'finished', state);
                    if (afterDone) { await afterDone(); }
                },
            });
        }

        let _reattachGeneration = 0;
        function getLogPollGeneration(taskNames) {
            const polls = typeof _pollGen === 'undefined' ? {} : _pollGen;
            return JSON.stringify(taskNames.map(name => [polls[name] || 0, polls[`logs:${name}`] || 0]));
        }

        function getLatestCompletedLog(taskNames, statuses) {
            let latest = null;
            for (const name of taskNames) {
                const status = statuses[name];
                if (!status || status.running || status.ownership_unconfirmed || !Array.isArray(status.logs) || !status.logs.length) { continue; }
                const start = Number.isFinite(status.start_time) ? status.start_time : 0;
                if (!latest || start > latest.start) { latest = { name, start, logs: status.logs }; }
            }
            return latest;
        }

        // Restore each running task independently: remote LLM work can coexist
        // with local training/Voice Lab and CPU exports.
        async function reattachRunningPollers() {
            const generation = ++_reattachGeneration;
            let statuses;
            try {
                statuses = await API.get('/api/status');
                if (generation !== _reattachGeneration) { return; }
            } catch (e) {
                if (generation !== _reattachGeneration) { return; }
                showActionError('Could not restore running task controls', e, 'Check the app connection and reload task status before starting new work; a task may still be running.', 'warning');
                return;
            }
            let running = Object.fromEntries(Object.entries(statuses).map(([name, state]) => [name, state.running || state.ownership_unconfirmed]));

            const logGroups = [
                { elementId: 'script-logs', tasks: ['script', 'batch_script', 'review', 'batch_review', 'nicknames', 'cast_list'] },
                { elementId: 'voices-logs', tasks: ['persona'] },
                { elementId: 'audio-logs', tasks: ['audio'] },
                { elementId: 'voicelab-logs', tasks: ['voicelab'] },
            ];
            const pollGenerations = logGroups.map(group => getLogPollGeneration(group.tasks));
            const hydrateNames = new Set([...logGroups.flatMap(group => group.tasks),
                ...Object.keys(statuses).filter(name => statuses[name]?.ownership_unconfirmed)]);
            const fetched = await Promise.all([...hydrateNames].map(async taskName => {
                if (statuses[taskName]?.running && !statuses[taskName]?.ownership_unconfirmed) { return null; }
                try {
                    return [taskName, await API.get(`/api/status/${taskName}`)];
                } catch (e) {
                    console.debug(`${taskName} log hydration failed`, e);
                    return null;
                }
            }));
            if (generation !== _reattachGeneration) { return; }
            const hydrated = Object.fromEntries(fetched.filter(Boolean));
            statuses = { ...statuses, ...hydrated };
            running = Object.fromEntries(Object.entries(statuses).map(([name, state]) => [name, state.running || state.ownership_unconfirmed]));
            logGroups.forEach((group, index) => {
                if (group.tasks.some(name => running[name]) || pollGenerations[index] !== getLogPollGeneration(group.tasks)) { return; }
                const latest = getLatestCompletedLog(group.tasks, hydrated);
                const el = document.getElementById(group.elementId);
                if (el && latest) {
                    el.innerText = latest.logs.join('\n');
                    el.title = t('core.task.logs-restored', 'Restored {task} logs', {task: latest.name.replaceAll('_', ' ')});
                    el.scrollTop = el.scrollHeight;
                }
            });

            const show = (id, disp = 'inline-block') => {
                const el = document.getElementById(id); if (el) { el.style.display = disp; }
            };
            const disable = (id) => {
                const el = document.getElementById(id); if (el) { el.disabled = true; }
            };

            const reattachers = {
                batch_script: () => {
                    disable('btn-gen-script');
                    show('btn-pause-batch-script'); show('btn-cancel-batch-script');
                    _pollScriptBatchLogs();
                },
                script: () => {
                    disable('btn-gen-script');
                    show('btn-cancel-script'); show('btn-pause-script');
                    pollScriptLogs('script', () => {
                        if (!scriptBatchPoller) {
                            const b = document.getElementById('btn-gen-script'); if (b) { b.disabled = false; }
                        }
                        show('btn-cancel-script', 'none'); show('btn-pause-script', 'none');
                    });
                },
                batch_review: async () => {
                    disable('btn-review-batch-start');
                    show('btn-pause-batch-review'); show('btn-cancel-batch-review');
                    await loadReviewBatchScripts();
                    pollReviewBatch();
                },
                review: () => {
                    _disableReviewButtons(true);
                    _showReviewControls(true);
                    pollScriptLogs('review', _onReviewDone);
                },
                nicknames: () => {
                    disable('btn-find-nicknames');
                    show('btn-pause-nick'); show('btn-cancel-nick');
                    pollScriptLogs('nicknames', async () => {
                        const btn = document.getElementById('btn-find-nicknames');
                        if (btn) { btn.disabled = false; }
                        show('btn-pause-nick', 'none'); show('btn-cancel-nick', 'none');
                        await loadCharacterAliases(true);
                    });
                },
                cast_list: () => {
                    disable('btn-build-cast-list');
                    show('btn-cancel-cast-list');
                    pollScriptLogs('cast_list', async () => {
                        const btn = document.getElementById('btn-build-cast-list');
                        if (btn) { btn.disabled = false; }
                        show('btn-cancel-cast-list', 'none');
                        await loadCastList(true);
                    });
                },
                persona: () => {
                    pollPersonaStatus();
                },
                voicelab: () => {
                    _vlSetRunning(true);
                    refreshVoicelabHealth();
                    pollVoicelab();
                },
                lora_training: () => {
                    disable('btn-lora-train');
                    show('btn-lora-cancel');
                    show('lora-progress-section', 'block');
                    pollLoraTraining();
                },
                preparer: () => { _pollPreparerLogs('preparer'); },
                batch_preparer: () => { _pollPreparerLogs('batch_preparer'); },
                audio: () => {
                    show('btn-cancel-merge');
                    pollLogs('audio', 'audio-logs', () => {
                        show('btn-cancel-merge', 'none');
                        window.resetExportQualityView?.();
                    });
                },
                benchmark: () => { refreshBenchmarkStatus(); },
                audacity_export: () => { pollExport('audacity_export'); },
                m4b_export: () => { pollExport('m4b_export'); },
                chapter_export: () => { pollExport('chapter_export'); },
                dataset_builder: async () => {
                    const state = await API.get('/api/status/dataset_builder');
                    if (!state.running) { return; }
                    if (!state.dataset_name) { throw new Error('Active Dataset Builder project is unavailable'); }
                    await dsbLoadProjects(state.dataset_name);
                },
                voices: () => { reattachTaskActivity('voices', ['btn-suggest-voices'], 'suggest-status', loadVoices); },
                llm_test: () => { reattachTaskActivity('llm_test', ['llm-test-btn'], 'llm-test-result'); },
                lmstudio_optimize: () => { reattachTaskActivity('lmstudio_optimize', ['lmstudio-optimize-toggle'], 'lmstudio-status-badge', refreshLmStudioStatus); },
                voice_design: () => { reattachTaskActivity('voice_design', ['btn-design-preview'], 'design-status', loadDesignedVoices); },
                lora_test: () => { reattachTaskActivity('lora_test', [], 'lora-test-status'); },
                word_timing: () => {
                    if (typeof applyWordTimingTaskActivity === 'function') { applyWordTimingTaskActivity(true); }
                    reattachTaskActivity('word_timing', ['editor-word-timing'], 'editor-word-status', () => {
                        if (typeof applyWordTimingTaskActivity === 'function') { applyWordTimingTaskActivity(false); }
                    });
                },
                speech_verification: () => {
                    if (typeof applySpeechVerificationTaskActivity === 'function') { applySpeechVerificationTaskActivity(true); }
                    reattachTaskActivity('speech_verification', ['editor-line-speech-recheck'], 'editor-line-speech-status', () => {
                        if (typeof applySpeechVerificationTaskActivity === 'function') { applySpeechVerificationTaskActivity(false); }
                    });
                },
                drift_check: () => { reattachTaskActivity('drift_check', [], null, () => loadChunks(false)); },
                outlier_check: async () => { attachEditorOutlierCheck(await API.get('/api/status/outlier_check')); },
            };
            const controlGroups = new Map(logGroups.flatMap(group =>
                group.tasks.map(name => [name, group.elementId])));
            controlGroups.set('preparer', 'preparer-controls');
            controlGroups.set('batch_preparer', 'preparer-controls');
            const attachments = new Map();
            for (const [name, isRunning] of Object.entries(running)) {
                if (!isRunning) { continue; }
                const group = controlGroups.get(name) || name;
                if (!attachments.has(group)) { attachments.set(group, []); }
                attachments.get(group).push(name);
            }
            await Promise.allSettled([...attachments.values()].map(async names => {
                for (const name of names) {
                    if (statuses[name]?.ownership_unconfirmed) {
                        reattachTaskActivity(name, [], controlGroups.get(name) || null, null, statuses[name]);
                        continue;
                    }
                    const attach = Object.prototype.hasOwnProperty.call(reattachers, name)
                        ? reattachers[name] : () => reattachTaskActivity(name);
                    try {
                        await attach();
                    } catch (e) {
                        showActionError(`Could not restore ${name.replaceAll('_', ' ')} controls`, e, 'Check the app connection and task status before starting again; the task may still be running.', 'warning');
                    }
                }
            }));
        }

        // Init
        window._voiceRosterStartupPending = loadConfig().then(() => loadVoices()).catch(e => {
            showActionError('Could not load voices', e, 'Retry loading Voices after configuration finishes loading.');
        }).finally(() => { window._voiceRosterStartupPending = null; });
        loadCastList(false);
        loadSavedScripts();
        loadDesignedVoices();
        dsbLoadProjects();
        updateSystemStats();
        updateEtaStatus();
        pollLmStudioStatus();
        reattachRunningPollers();
        setInterval(updateSystemStats, 10000); // Update every 10s
        setInterval(updateEtaStatus, 10000); // Update every 10s
        setInterval(pollLmStudioStatus, 30000); // Update visible Setup every 30s
        document.addEventListener('visibilitychange', pollLmStudioStatus);

        // ── Preparer ──────────────────────────────────────────────
        let prepBatchQueue = [];

        window.togglePrepBatchMode = () => {
            const isBatch = document.getElementById('prep-batch-mode').checked;
            document.getElementById('prep-single-area').style.display = isBatch ? 'none' : 'block';
            document.getElementById('prep-batch-area').style.display  = isBatch ? 'block' : 'none';
        };

        window.onPrepBatchFilesChange = () => {
            if (prepSubmitting || prepActiveTask === 'batch_preparer') {
                document.getElementById('prep-batch-files').value = '';
                showToast(t('preparer.batch.wait', 'Wait for the current preparation to finish or cancel it before changing batch files.'), 'warning');
                return;
            }
            const files = document.getElementById('prep-batch-files').files;
            const tbody = document.getElementById('prep-batch-queue-body');
            tbody.innerHTML = '';
            prepBatchQueue.forEach(task => { task.picker?.dispose(); });
            prepBatchQueue = [];

            if (!files.length) {
                document.getElementById('prep-batch-queue-container').style.display = 'none';
                return;
            }
            document.getElementById('prep-batch-queue-container').style.display = 'block';

            [...files].forEach((file, i) => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td title="${escapeHtml(file.name)}" style="max-width:350px;white-space:normal;overflow-wrap:anywhere;">${escapeHtml(file.name)}</td>
                    <td id="prep-batch-status-${i}"><span class="badge bg-secondary">${escapeHtml(taskStatusLabel('pending'))}</span></td>
                `;
                tbody.appendChild(row);
                const pickerArea = document.createElement('div');
                row.firstElementChild.appendChild(pickerArea);
                const picker = window.createAudioChannelPicker(pickerArea, file, 'preparer');
                prepBatchQueue.push({ audio: file, picker });
            });
        };

        const PREPARER_TASKS = {
            preparer: {start: '/api/preparer/start', cancel: '/api/preparer/cancel',
                       starting: 'Starting…', failure: 'Failed to start: '},
            batch_preparer: {start: '/api/preparer/batch/upload_start', cancel: '/api/preparer/batch/cancel',
                             starting: 'Starting batch…', failure: 'Failed to start batch: '},
        };
        let prepActiveTask = null;
        let prepSubmitting = false;

        function _applyPreparerControls(taskName, submitting = false) {
            prepActiveTask = taskName;
            prepSubmitting = submitting;
            document.getElementById('btn-prep-start').disabled = submitting || taskName !== null;
            document.getElementById('btn-prep-cancel').style.display = taskName ? 'inline-block' : 'none';
            document.getElementById('prep-batch-files').disabled = submitting || taskName === 'batch_preparer';
            document.getElementById('prep-audio-file').disabled = submitting || taskName === 'preparer';
            document.getElementById('prep-source-file').disabled = submitting || taskName === 'preparer';
            window.preparerChannelPicker?.setDisabled?.(submitting || taskName === 'preparer');
            prepBatchQueue.forEach(task => { task.picker?.setDisabled?.(submitting || taskName === 'batch_preparer'); });
        }

        async function _submitPreparer(taskName, formData, onAccepted) {
            if (prepSubmitting || prepActiveTask) { return; }
            const task = PREPARER_TASKS[taskName];
            _applyPreparerControls(null, true);
            document.getElementById('preparer-progress-section').style.display = 'block';
            document.getElementById('prep-status-msg').innerHTML = `<span class="text-info">${escapeHtml(taskName === 'batch_preparer' ? t('preparer.start.starting-batch', 'Starting batch…') : t('preparer.start.starting', 'Starting…'))}</span>`;
            try {
                const res = await fetch(task.start, {method: 'POST', body: formData});
                await API._handleError(res);
                _applyPreparerControls(taskName);
                _pollPreparerLogs(taskName);
            } catch (e) {
                _applyPreparerControls(null);
                showActionError(taskName === 'batch_preparer' ? 'Batch preparer start is unconfirmed' : 'Preparer start is unconfirmed', e, 'Check preparer task status and output files before starting again. Review any input or configuration refusal below.');
                return false;
            }
            onAccepted?.();
            return true;
        }

        async function _submitPreparerAfterSelection(taskName, buildForm, onAccepted) {
            if (prepSubmitting || prepActiveTask) { return; }
            _applyPreparerControls(null, true);
            try {
                const form = await buildForm();
                // Transfer the UI reservation synchronously to the existing request owner.
                prepSubmitting = false;
                return _submitPreparer(taskName, form, onAccepted);
            } catch (error) {
                _applyPreparerControls(null);
                showActionError('Could not prepare selected audio', error, 'Your files are retained. Select an expired or changed preview again before starting.');
            }
        }

        window.startPreparer = async () => {
            const isBatch = document.getElementById('prep-batch-mode').checked;
            if (isBatch) { return _startBatchPreparer(); }

            const audioFile = document.getElementById('prep-audio-file').files[0];
            setFieldFeedback('prep-audio-file');
            if (!audioFile) { setFieldFeedback('prep-audio-file', createUiMessageNode('span', 'preparer.start.no-audio', 'Audio file required')); return; }

            const picker = window.preparerChannelPicker;
            const channelSelection = picker ? picker.getSelection() : {channel: 'mix', token: null};
            const sourceFile = document.getElementById('prep-source-file').files[0];
            const diarizationMode = document.getElementById('prep-diarization-mode').value;

            const config = {
                audio_filename: audioFile.name,
                audio_channel: channelSelection.channel,
                output_filename: document.getElementById('prep-output').value,
                lang:            document.getElementById('prep-lang').value,
                min_confidence:  getNumFieldValue('prep-confidence', 0.85),
                min_snr:         getNumFieldValue('prep-snr', 25, true),
                model:           document.getElementById('prep-model').value || null,
                fallback_model:  document.getElementById('prep-fallback-model').value || null,
                source_filename: sourceFile ? sourceFile.name : null,
                source_threshold: getNumFieldValue('prep-source-threshold', 0.65),
                keep_unaligned:  document.getElementById('prep-keep-unaligned').checked,
                chunk_size:      getNumFieldValue('prep-chunk-size', 10),
                min_chunk_duration: getNumFieldValue('prep-min-chunk-duration', 2),
                resume:          document.getElementById('prep-resume').checked,
                skip_annotation: false,
                source_start:    document.getElementById('prep-source-start').value ? getNumFieldValue('prep-source-start', 0, true) : null,
                source_start_text: document.getElementById('prep-source-start-text').value || null,
                no_auto_anchor:  document.getElementById('prep-no-auto-anchor').checked,
                batch_size:      getNumFieldValue('prep-batch-size', 1, true),
                enrich_with_llm: document.getElementById('prep-enrich-with-llm').checked,
                allow_cpu_fallback: document.getElementById('prep-allow-cpu-fallback').checked,
                llm_model_path:  document.getElementById('prep-llm-model-path').value || null,
                enrich_speaker_attribution: document.getElementById('prep-enrich-speaker').checked,
                enrich_narration_style:     document.getElementById('prep-enrich-narration').checked,
                enrich_emotional_tone:      document.getElementById('prep-enrich-emotion').checked,
                diarize:                    diarizationMode === 'full',
                auto_detect_speakers:       diarizationMode === 'auto',
                hf_token:                   document.getElementById('prep-hf-token').value || null,
            };

            return _submitPreparerAfterSelection('preparer', async () => {
                if (channelSelection.pending) { channelSelection.token = await channelSelection.pending; }
                if (document.getElementById('prep-audio-file').files[0] !== audioFile) { throw new Error('Audio selection changed. Start again with the current file.'); }
                const fd = new FormData();
                fd.append('config_json', JSON.stringify(config));
                if (channelSelection.token) { fd.append('preview_token', channelSelection.token); }
                else { fd.append('audio_file', audioFile); }
                if (sourceFile) { fd.append('source_file', sourceFile); }

                return fd;
            }, () => picker?.consumeStage?.(channelSelection.token));
        };

        window.cancelPreparer = () => {
            if (!prepActiveTask) { return; }
            return cancelTask(PREPARER_TASKS[prepActiveTask].cancel);
        };

        async function _startBatchPreparer() {
            if (!prepBatchQueue.length) { showToast(t('preparer.start.no-files', 'No files selected'), 'warning'); return; }

            const files = prepBatchQueue.map(t => t.audio);
            const pickers = prepBatchQueue.map(t => t.picker);
            let selections;
            return _submitPreparerAfterSelection('batch_preparer', async () => {
                selections = await Promise.all(pickers.map(async picker => {
                    const selected = picker.getSelection();
                    return {...selected, token: selected.pending ? await selected.pending : selected.token};
                }));
                const tasks = files.map((file, index) => ({
                    audio_channel: selections[index].channel,
                    preview_token: selections[index].token || "",
                    audio_filename:  file.name,
                    output_filename: `voice_dataset_${file.name.replace(/\.[^.]+$/, '')}.zip`,
                }));
                const diarizationMode = document.getElementById('prep-diarization-mode').value;
                const body = {
                    tasks,
                    lang:           document.getElementById('prep-lang').value,
                    min_confidence: getNumFieldValue('prep-confidence', 0.85),
                    min_snr:        getNumFieldValue('prep-snr', 25, true),
                    diarize:        diarizationMode === 'full',
                    auto_detect_speakers: diarizationMode === 'auto',
                    hf_token:       document.getElementById('prep-hf-token').value || null,
                };

                const fd = new FormData();
                fd.append('config_json', JSON.stringify(body));
                files.forEach((file, index) => { if (!selections[index].token) { fd.append('audio_files', file); } });
                return fd;
            }, () => pickers.forEach((picker, index) => picker.consumeStage?.(selections[index].token)));
        }

        function _pollPreparerLogs(taskName) {
            _applyPreparerControls(taskName);
            const logEl = document.getElementById('preparer-logs');
            const renderLogs = createTaskLogRenderer(logEl);

            _startPolling(taskName, () => API.get(`/api/status/${taskName}`), {
                doneCheck: state => !state.running,
                onTick: state => {
                    if (prepActiveTask !== taskName) { return; }
                    renderLogs(state);
                    // Hatch: the dataset-build texture, same as the builder's
                    // (#2042). The server already embeds the estimate.
                    const progress = state.eta || null;
                    renderTexturedProgress(document.getElementById('preparer-progress-bar'), {
                        fraction: progress && Number.isFinite(progress.fraction) ? progress.fraction : null,
                        label: getTaskLabel(taskName),
                        task: getTaskLabel(taskName),
                    });

                    // Update batch queue status badges
                    if (taskName === 'batch_preparer' && state.tasks) {
                        state.tasks.forEach((task, i) => {
                            const el = document.getElementById(`prep-batch-status-${i}`);
                            if (!el) { return; }
                            const colours = { pending: 'secondary', running: 'primary', done: 'success', failed: 'danger', cancelled: 'warning' };
                            const colour = Object.prototype.hasOwnProperty.call(colours, task.status) ? colours[task.status] : 'secondary';
                            el.innerHTML = `<span class="badge bg-${colour}">${escapeHtml(taskStatusLabel(task.status))}</span>`;
                        });
                    }
                },
                onDone: state => {
                    if (prepActiveTask !== taskName) { return; }
                    notifyJobDone(taskName, '', 'finished', state);
                    _applyPreparerControls(null);
                    const outcome = getTaskCompletionOutcome(state);
                    const label = taskName === 'preparer'
                        ? t('preparer.outcome.label', 'Preparation')
                        : t('preparer.outcome.label-batch', 'Batch preparation');
                    const tone = outcome === 'failed' ? 'text-danger' : outcome === 'cancelled' ? 'text-warning' : 'text-success';
                    const msg = outcome === 'failed'
                        ? t('preparer.outcome.failed', '{label} failed. Review the activity log before retrying; completed datasets remain available below.', {label})
                        : outcome === 'cancelled'
                            ? t('preparer.outcome.cancelled', '{label} cancelled. Completed datasets remain available below.', {label})
                            : t('preparer.outcome.finished', '{label} finished. Download completed datasets below.', {label});
                    document.getElementById('prep-status-msg').innerHTML = `<span class="${tone}">${escapeHtml(msg)}</span>`;
                    loadPreparerOutputs();  // refresh the download list with any new ZIPs
                }
            });
        }

        // List/download the dataset ZIPs produced by completed preparer runs.
        async function loadPreparerOutputs() {
            const el = document.getElementById('preparer-outputs');
            if (!el) { return; }
            try {
                const res = await API.get('/api/preparer/list');
                const files = res.files || [];
                if (!files.length) {
                    el.innerHTML = `<div class="text-muted small">${escapeHtml(t('preparer.outputs.empty-long', 'No datasets yet. Completed preparer runs will appear here.'))}</div>`;
                    return;
                }
                el.innerHTML = files.map(f => {
                    const when = f.modified ? formatUiDate(new Date(f.modified * 1000)) : '';
                    return `<div class="list-group-item d-flex justify-content-between align-items-center">
                        <span class="text-truncate me-2">
                            <i class="fas fa-file-zipper me-2"></i>${escapeHtml(f.filename)}
                            <span class="text-muted ms-1">${f.size_mb} MB${when ? ` · ${escapeHtml(when)}` : ''}</span>
                        </span>
                        <a class="btn btn-sm btn-outline-success flex-shrink-0" href="/api/preparer/download/${encodeURIComponent(f.filename)}" download>
                            <i class="fas fa-download me-1"></i>${escapeHtml(t('preparer.outputs.download', 'Download'))}
                        </a>
                    </div>`;
                }).join('');
            } catch (e) {
                el.innerHTML = `<div class="text-danger small">${escapeHtml(getActionErrorMessage('Preparer outputs unavailable', e, 'Check the app connection, then refresh the output list. A failed list request does not mean the output files are missing.'))}</div>`;
            }
        }
