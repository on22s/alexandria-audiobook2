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
        let dsbProjectLoadSequence = 0;
        let dsbProjectListSequence = 0;

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
                const select = document.getElementById('dsb-project-select');
                select.innerHTML = '<option value="">-- Select project --</option>' +
                    projects.map(p => getEscapedHtml`<option value="${p.name}">${p.name} (${p.done_count}/${p.sample_count})</option>`).join('');
                if (selectName) {
                    select.value = selectName;
                    await dsbOnProjectChange();
                }
            } catch (e) { console.error('Failed to load projects:', e); }
        }

        window.dsbOnProjectChange = async () => {
            try {
                await Promise.all([dsbSaveMetaQueue?.flush(), dsbSaveRowsQueue?.flush()]);
            } catch (error) {
                document.getElementById('dsb-project-select').value = dsbCurrentProject;
                return;
            }
            dsbStopBatch();
            const name = document.getElementById('dsb-project-select').value;
            const formArea = document.getElementById('dsb-form-area');
            const deleteBtn = document.getElementById('dsb-btn-delete-project');
            if (!name) {
                dsbCurrentProject = '';
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

        async function dsbLoadProject(name) {
            const loadSequence = ++dsbProjectLoadSequence;
            try {
                if (dsbSaveRowsQueue?.isDirty()) { await dsbSaveRowsQueue.flush(); }
            } catch (error) {
                showToast('Save the pending row edits before reloading this project: ' + error.message, 'error');
                return;
            }
            try {
                const result = await API.get(`/api/dataset_builder/status/${encodeURIComponent(name)}`);
                if (loadSequence !== dsbProjectLoadSequence || !isDatasetProjectSelected(name)) { return; }
                applyDatasetBatchState(!!result.running);
                applyDatasetRowRevisions(name, result.row_revisions, (result.samples || []).length);
                document.getElementById('dsb-description').value = result.description || '';
                document.getElementById('dsb-global-seed').value = result.global_seed ?? '';
                dsbRows = (result.samples || []).map(s => ({
                    emotion: s.emotion || s.description || '',
                    text: s.text || '',
                    seed: s.seed ?? '',
                    status: s.status || 'pending',
                    audio_url: s.audio_url || null,
                }));
                if (dsbRows.length === 0 && !dsbBatchRunning) { dsbAddRow(); }
                dsbRenderTable();
                // Resume polling if batch is running
                if (result.running) {
                    dsbStartPolling(name);

                }
            } catch (e) {
                if (loadSequence !== dsbProjectLoadSequence || !isDatasetProjectSelected(name)) { return; }
                // A transient status-GET failure must NOT destroy the saved
                // project. Disarm dsbSaveRows (guarded on dsbCurrentProject)
                // BEFORE clearing rows so its debounced POST can't overwrite the
                // real samples on disk with an empty row, and do NOT dsbAddRow.
                console.error('Failed to load project:', e);
                dsbCurrentProject = '';
                dsbRows = [];
                dsbRenderTable();
                document.getElementById('dsb-form-area').style.display = 'none';
                document.getElementById('dsb-btn-delete-project').style.display = 'none';
                showToast('Failed to load dataset "' + name + '": ' + (e.message || e));
            }
        }

        window.dsbCreateProject = async () => {
            const name = prompt('Dataset name:');
            if (!name || !name.trim()) { return; }
            try {
                const result = await API.post('/api/dataset_builder/create', { name: name.trim() });
                await dsbLoadProjects(result.name);
            } catch (e) {
                showToast('Failed to create project: ' + e.message, 'error');
            }
        };

        window.dsbDeleteProject = async () => {
            if (!dsbCurrentProject || !ensureDatasetRowsEditable()) { return; }
            if (!await showConfirm(`Delete project "${dsbCurrentProject}" and all its samples?`)) { return; }
            if (!ensureDatasetRowsEditable()) { return; }
            try {
                const res = await fetch(`/api/dataset_builder/${encodeURIComponent(dsbCurrentProject)}`, { method: 'DELETE' });
                await API._handleError(res);
                dsbCurrentProject = '';
                document.getElementById('dsb-form-area').style.display = 'none';
                document.getElementById('dsb-btn-delete-project').style.display = 'none';
                dsbRows = [];
                dsbRenderTable();
                await dsbLoadProjects();
            } catch (e) {
                showToast('Delete failed: ' + e.message, 'error');
            }
        };

        const dsbRowSaveStates = new Map();
        function getDatasetRowDefinition(row) {
            return { emotion: row.emotion || row.description || '', text: (row.text || '').trim(), seed: row.seed ?? '' };
        }

        function ensureDatasetRowSaveState(name) {
            if (!dsbRowSaveStates.has(name)) {
                dsbRowSaveStates.set(name, { tokens: null, edits: new Map(), sequence: 0, fullRevision: null });
            }
            return dsbRowSaveStates.get(name);
        }

        function applyDatasetRowRevisions(name, revisions, count) {
            const state = ensureDatasetRowSaveState(name);
            state.tokens = Array.isArray(revisions) && revisions.length === count
                && revisions.every(token => typeof token === 'string' && /^[0-9a-f]{64}$/.test(token)) ? revisions.slice() : null;
        }

        async function saveDatasetRows(value) {
            const state = ensureDatasetRowSaveState(value.name);
            if (value.rows) {
                const result = await API.post('/api/dataset_builder/update_rows', { name: value.name, rows: value.rows });
                applyDatasetRowRevisions(value.name, result.row_revisions, value.rows.length);
                if (state.fullRevision === value.revision) { state.fullRevision = null; }
                return;
            }
            const queuedEdits = value.edits.filter(edit => state.edits.has(edit.index));
            if (!queuedEdits.length) { return; }
            const edits = queuedEdits.map(edit => ({ index: edit.index, row: edit.row, expected_revision: state.tokens?.[edit.index] }));
            let result;
            try {
                result = await API.post('/api/dataset_builder/edit_rows', { name: value.name, expected_count: value.count, edits });
                if (!result.row_revisions || edits.some(edit => !/^[0-9a-f]{64}$/.test(result.row_revisions[edit.index] || ''))) {
                    throw new Error('Row save returned no revision acknowledgement');
                }
            } catch (error) {
                if (error.status && error.status < 500) { throw error; }
                // A response can be lost after commit. A read may acknowledge
                // the desired definitions; it never replays or overwrites them.
                const saved = await API.get(`/api/dataset_builder/status/${encodeURIComponent(value.name)}`);
                if (!Array.isArray(saved.samples) || saved.samples.length !== value.count
                    || !Array.isArray(saved.row_revisions) || edits.some(edit =>
                        JSON.stringify(getDatasetRowDefinition(saved.samples[edit.index] || {})) !== JSON.stringify(edit.row)
                        || !/^[0-9a-f]{64}$/.test(saved.row_revisions[edit.index] || ''))) { throw error; }
                result = { row_revisions: Object.fromEntries(edits.map(edit => [edit.index, saved.row_revisions[edit.index]])) };
            }
            queuedEdits.forEach(edit => {
                state.tokens[edit.index] = result.row_revisions[edit.index];
                if (state.edits.get(edit.index)?.revision === edit.revision) { state.edits.delete(edit.index); }
            });
        }

        function dsbSaveForm() {
            const name = dsbCurrentProject;
            if (!name) { return; }
            const description = document.getElementById('dsb-description').value;
            const globalSeed = document.getElementById('dsb-global-seed').value;
            if (!dsbSaveMetaQueue) {
                dsbSaveMetaQueue = createSerializedSaveQueue({
                    write: value => API.post('/api/dataset_builder/update_meta', value),
                    onError: error => _toastSaveError('meta', error),
                });
            }
            dsbSaveMetaQueue.enqueue({ name, description, global_seed: globalSeed });
        }

        function dsbSaveRows(index = null) {
            const name = dsbCurrentProject;
            if (!name) { return; }
            const state = ensureDatasetRowSaveState(name);
            const revision = ++state.sequence;
            let value;
            if (index !== null && state.tokens?.length === dsbRows.length && state.fullRevision === null) {
                state.edits.set(index, { row: getDatasetRowDefinition(dsbRows[index]), revision });
                value = { name, count: dsbRows.length, edits: Array.from(state.edits, ([index, edit]) => ({ index, ...edit })) };
            } else {
                state.fullRevision = revision;
                state.edits.clear();
                value = { name, revision, rows: dsbRows.map(getDatasetRowDefinition) };
            }
            if (!dsbSaveRowsQueue) {
                dsbSaveRowsQueue = createSerializedSaveQueue({
                    write: saveDatasetRows,
                    onError: error => _toastSaveError('rows', error),
                });
            }
            dsbSaveRowsQueue.enqueue(value);
        }

        function ensureDatasetRowsEditable() {
            if (dsbBatchRunning) {
                showToast('Wait for batch generation to finish before editing samples.', 'warning');
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

        function dsbRemoveRow(index) {
            if (!ensureDatasetRowsEditable()) { return; }
            dsbRows.splice(index, 1);
            dsbRenderTable();
            dsbSaveRows();
            dsbUpdateRefDropdown();
        }

        function dsbBuildRowHtml(row, i) {
            const disabled = dsbBatchRunning ? 'disabled' : '';
            const statusColor = row.status === 'done' ? 'success' :
                                row.status === 'generating' ? 'warning' :
                                row.status === 'error' ? 'danger' : 'secondary';
            const statusLabel = row.status || 'pending';

            let actionHtml = '';
            if (row.status === 'generating') {
                actionHtml = '<div class="progress" style="width:80px;height:20px;"><div class="progress-bar progress-bar-striped progress-bar-animated bg-warning" style="width:100%"></div></div>';
            } else {
                const genLabel = row.status === 'done' ? '<i class="fas fa-redo"></i>' : '<i class="fas fa-play"></i>';
                actionHtml = getEscapedHtml`<button class="btn btn-sm btn-primary" ${disabled} onclick="dsbGenSample(${i})" title="${row.status === 'done' ? 'Regenerate' : 'Generate'}">` + genLabel + '</button>';
            }

            let audioHtml = '';
            if (row.status === 'done' && row.audio_url) {
                audioHtml = getEscapedHtml`<audio controls src="${row.audio_url}" style="width:180px;height:28px;" onplay="dsbStopOthers(${i})"></audio>`;
            }

            return getEscapedHtml`<tr data-dsb-idx="${i}" data-dsb-status="${row.status || 'pending'}" data-dsb-audio="${row.audio_url || ''}" class="${row.status === 'generating' ? 'table-info' : ''}">
                <td class="text-center align-middle">${i + 1}</td>
                <td><input type="text" class="form-control form-control-sm" ${disabled} value="${row.emotion || ''}" onchange="dsbUpdateRow(${i}, 'emotion', this.value)" placeholder="e.g. Savagely sarcastic"></td>
                <td><textarea class="form-control form-control-sm" rows="2" ${disabled} onchange="dsbUpdateRow(${i}, 'text', this.value)" placeholder="Sample text...">${row.text || ''}</textarea></td>
                <td><input type="number" class="form-control form-control-sm" ${disabled} value="${row.seed ?? ''}" onchange="dsbUpdateRow(${i}, 'seed', this.value)" placeholder="-" style="width:65px;" min="-1"></td>
                <td class="text-center align-middle"><span class="badge bg-${statusColor}">${statusLabel}</span></td>
                <td class="align-middle">
                    <div class="d-flex align-items-center gap-1">
                    ` + actionHtml + audioHtml + getEscapedHtml`
                        <button class="btn btn-sm btn-outline-danger ms-auto" ${disabled} onclick="dsbRemoveRow(${i})" title="Delete row"><i class="fas fa-trash"></i></button>
                    </div>
                </td>
            </tr>`;
        }

        function dsbRenderTable(changedIndices) {
            const tbody = document.getElementById('dsb-table-body');

            // Full rebuild if no specific indices or row count changed
            if (!changedIndices || tbody.children.length !== dsbRows.length) {
                tbody.innerHTML = dsbRows.map((row, i) => dsbBuildRowHtml(row, i)).join('');
                dsbUpdateProgress();
                return;
            }

            // Targeted update: only re-render changed rows
            for (const i of changedIndices) {
                const existing = tbody.children[i];
                if (!existing) { continue; }
                const row = dsbRows[i];
                const oldStatus = existing.getAttribute('data-dsb-status');
                const oldAudio = existing.getAttribute('data-dsb-audio');
                if (oldStatus === (row.status || 'pending') && oldAudio === (row.audio_url || '')) { continue; }
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
                bar.style.width = pct + '%';
                bar.innerText = `${pct}% (${done}/${total})`;
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
                ? '<option value="0">No completed samples yet</option>'
                : doneSamples.map(x => `<option value="${x.index}">${x.index + 1}. ${escapeHtml((x.row.emotion || 'neutral').substring(0, 30))} - "${escapeHtml((x.row.text || '').substring(0, 40))}..."</option>`).join('');
        }

        // Single sample generation
        window.dsbGenSample = async (index) => {
            const name = dsbCurrentProject;
            const rootDesc = document.getElementById('dsb-description').value.trim();
            if (!name) { showToast('Select or create a project first.', 'warning'); return; }
            if (!rootDesc) { showToast('Enter a root voice description first.', 'warning'); return; }

            const row = dsbRows[index];
            if (!row || !row.text.trim()) { showToast('This row has no text.', 'warning'); return; }

            const emotion = row.emotion.trim();
            const description = emotion ? `${rootDesc}, ${emotion}` : rootDesc;

            // Resolve seed: per-line > global > random
            const globalSeed = parseInt(document.getElementById('dsb-global-seed').value);
            const lineSeed = row.seed !== '' ? parseInt(row.seed) : NaN;
            const seed = !isNaN(lineSeed) && lineSeed >= 0 ? lineSeed : (!isNaN(globalSeed) && globalSeed >= 0 ? globalSeed : -1);

            // Optimistic UI
            dsbRows[index].status = 'generating';
            dsbRenderTable([index]);

            try {
                const result = await API.post('/api/dataset_builder/generate_sample', {
                    description,
                    text: row.text.trim(),
                    dataset_name: name,
                    sample_index: index,
                    seed,
                });
                if (!isDatasetProjectSelected(name) || dsbRows[index] !== row) { return; }
                dsbRows[index].status = 'done';
                dsbRows[index].audio_url = result.audio_url;
            } catch (e) {
                if (!isDatasetProjectSelected(name) || dsbRows[index] !== row) { return; }
                dsbRows[index].status = 'error';
                console.error('Sample generation failed:', e);
            }
            dsbRenderTable([index]);
        };

        // Batch generation
        window.dsbGenerateAll = async (regenAll = false) => {
            if (dsbBatchRunning) { return; }
            const rows = dsbRows;
            const name = dsbCurrentProject;
            const rootDesc = document.getElementById('dsb-description').value.trim();
            if (!name) { showToast('Select or create a project first.', 'warning'); return; }
            if (!rootDesc) { showToast('Enter a root voice description first.', 'warning'); return; }

            const samples = dsbRows.filter(r => r.text.trim());
            if (samples.length === 0) { showToast('Add at least one sample with text.', 'warning'); return; }

            const indices = regenAll
                ? dsbRows.map((_, i) => i).filter(i => dsbRows[i].text.trim())
                : dsbRows.map((r, i) => i).filter(i => dsbRows[i].text.trim() && dsbRows[i].status !== 'done');

            if (indices.length === 0) { showToast('All samples are already generated.', 'warning'); return; }
            if (regenAll && !await showConfirm(`Regenerate all ${indices.length} samples?`)) { return; }
            if (dsbBatchRunning || !isDatasetProjectSelected(name) || dsbRows !== rows) { return; }
            const sequence = ++dsbBatchStartSequence;
            dsbPendingStart = sequence;
            const previous = indices.map(index => ({ index, row: dsbRows[index], status: dsbRows[index].status }));

            // Mark as generating
            indices.forEach(i => { dsbRows[i].status = 'generating'; });
            applyDatasetBatchState(false, true);
            dsbRenderTable();
            document.getElementById('dsb-logs').style.display = '';

            const globalSeed = parseInt(document.getElementById('dsb-global-seed').value);
            const perSeeds = dsbRows.map(r => r.seed !== '' && r.seed !== undefined ? parseInt(r.seed) : -1);

            try {
                await Promise.all([dsbSaveRowsQueue?.flush(), dsbSaveMetaQueue?.flush()]);
                if (sequence !== dsbBatchStartSequence || !isDatasetProjectSelected(name)) { return; }
                await API.post('/api/dataset_builder/generate_batch', {
                    name,
                    description: rootDesc,
                    samples: dsbRows.map(r => ({ emotion: r.emotion || '', text: r.text || '' })),
                    indices,
                    global_seed: !isNaN(globalSeed) && globalSeed >= 0 ? globalSeed : -1,
                    seeds: perSeeds,
                });

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
                showToast('Batch generation failed: ' + e.message, 'error');
                applyDatasetBatchState(false, true);
                dsbRenderTable();
                // A lost response can follow accepted work. Keep edits blocked
                // until the existing status poller reconciles the server state.
                dsbStartPolling(name, false);
            }
        };

        function dsbStartPolling(name, admitted = true) {
            if (dsbPolling) { dsbPolling(); }
            dsbPolling = _startPolling(`dataset_builder:${name}`, () => API.get(`/api/dataset_builder/status/${encodeURIComponent(name)}`), {
                intervalMs: 2000,
                doneCheck: result => !result.running,
                onTick: result => {
                    if (!isDatasetProjectSelected(name)) { return; }
                    const wasBusy = dsbBatchRunning;
                    applyDatasetBatchState(!!result.running);
                    if (result.running) { admitted = true; }
                    const serverSamples = result.samples || [];
                    applyDatasetRowRevisions(name, result.row_revisions, serverSamples.length);

                    // Merge server state into local rows, creating missing rows
                    const changed = [];
                    let added = false;
                    serverSamples.forEach((s, i) => {
                        if (i < dsbRows.length) {
                            const oldStatus = dsbRows[i].status;
                            const oldAudio = dsbRows[i].audio_url;
                            if (s.status) { dsbRows[i].status = s.status; }
                            if (s.audio_url) { dsbRows[i].audio_url = s.audio_url; }
                            if (dsbRows[i].status !== oldStatus || dsbRows[i].audio_url !== oldAudio) { changed.push(i); }
                        } else {
                            dsbRows.push({
                                emotion: s.description || '',
                                text: s.text || '',
                                seed: s.seed ?? '',
                                status: s.status || 'pending',
                                audio_url: s.audio_url || null
                            });
                            added = true;
                        }
                    });

                    if (added || wasBusy !== dsbBatchRunning) {
                        dsbRenderTable();
                    } else if (changed.length > 0) {
                        dsbRenderTable(changed);
                    }

                    // Update logs
                    if (result.logs && result.logs.length > 0) {
                        const logsEl = document.getElementById('dsb-logs');
                        logsEl.style.display = '';
                        logsEl.innerText = result.logs.join('\n');
                        logsEl.scrollTop = logsEl.scrollHeight;
                    }


                },
                onDone: () => {
                    if (!isDatasetProjectSelected(name)) { return; }
                    if (admitted) { notifyJobDone('dataset_builder'); }
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
            const reader = new FileReader();
            reader.onload = (e) => {
                if (!ensureDatasetRowsEditable()) { return; }
                try {
                    const data = JSON.parse(e.target.result);
                    if (!Array.isArray(data)) { throw new Error('Expected JSON array'); }
                    dsbRows = data.map((item, index) => {
                        if (!item || typeof item !== 'object' || Array.isArray(item)) {
                            throw new Error(`Sample ${index + 1} must be an object`);
                        }
                        for (const field of ['text', 'emotion', 'instruct']) {
                            if (item[field] != null && typeof item[field] !== 'string') {
                                throw new Error(`Sample ${index + 1} ${field} must be a string`);
                            }
                        }
                        if (item.seed != null && typeof item.seed !== 'number' && typeof item.seed !== 'string') {
                            throw new Error(`Sample ${index + 1} seed must be a number or string`);
                        }
                        return {
                            emotion: item.emotion || item.instruct || '',
                            text: item.text || '',
                            seed: item.seed ?? '',
                            status: 'pending',
                            audio_url: null,
                        };
                    });
                    dsbRenderTable();
                    dsbSaveRows();
                } catch (err) {
                    showToast('Import failed: ' + err.message, 'error');
                }
            };
            reader.readAsText(file);
            event.target.value = '';  // reset file input
        };

        window.dsbExport = () => {
            const data = dsbRows.map(r => {
                const entry = { emotion: r.emotion, text: r.text };
                if (r.seed !== '' && r.seed !== undefined) { entry.seed = parseInt(r.seed); }
                return entry;
            });
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const name = dsbCurrentProject || 'dataset';
            a.download = `${name}_script.json`;
            a.click();
            URL.revokeObjectURL(url);
        };

        // Save as training dataset
        window.dsbSave = async () => {
            const name = dsbCurrentProject;
            if (!name) { showToast('Select or create a project first.', 'warning'); return; }

            const doneSamples = dsbRows.filter(r => r.status === 'done');
            if (doneSamples.length === 0) { showToast('No completed samples to save. Generate some first.', 'warning'); return; }

            const refIdx = parseInt(document.getElementById('dsb-ref-select').value) || 0;

            if (!await showConfirm(`Save "${name}" as training dataset with ${doneSamples.length} samples?`)) return;

            const statusEl = document.getElementById('dsb-save-status');
            statusEl.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Saving...';

            try {
                const result = await API.post('/api/dataset_builder/save', {
                    name,
                    ref_index: refIdx,
                });
                statusEl.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Saved! ${result.sample_count} samples.</span>`;
            } catch (e) {
                statusEl.innerHTML = `<span class="text-danger">Save failed: ${escapeHtml(e.message)}</span>`;
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

        let _systemStatsPending = false;
        async function updateSystemStats() {
            if (_systemStatsPending) { return; }
            _systemStatsPending = true;
            try {
                const stats = await API.get('/api/system/stats');
                const gpuEl = document.getElementById('sys-gpu-val');
                const buildEl = document.getElementById('sys-build-val');
                const buildWrap = document.getElementById('sys-build');
                const gpuWrap = document.getElementById('sys-gpu');
                const diskEl = document.getElementById('sys-disk-val');
                const diskWrap = document.getElementById('sys-disk');

                const runtime = stats.runtime || {};
                buildEl.textContent = runtime.short_revision ? `build ${runtime.short_revision}` : 'build unknown';
                checkStaleBuild(runtime.short_revision);
                const packageVersions = Object.entries(runtime.packages || {})
                    .filter(item => item[1])
                    .map(item => `${item[0]} ${item[1]}`)
                    .join(', ');
                buildWrap.title = [
                    runtime.revision ? `Revision: ${runtime.revision}` : 'Revision unavailable',
                    runtime.branch ? `Branch: ${runtime.branch}` : '',
                    runtime.python ? `Python ${runtime.python}` : '',
                    packageVersions,
                ].filter(Boolean).join('\n');

                if (stats.gpu_mismatch) {
                    // A GPU is physically present but torch can't use it - everything
                    // is silently running on CPU. Worth a much louder signal than the
                    // normal VRAM-pressure red, since this is a broken install, not
                    // just "busy right now".
                    gpuEl.textContent = 'CPU fallback!';
                    gpuWrap.title = `${stats.gpu_mismatch_vendor || 'A'} GPU was detected on this system, ` +
                        `but the installed torch build can't use it - generation/training will run on CPU ` +
                        `and be dramatically slower. This usually means torch/torchaudio is the wrong build ` +
                        `for this GPU; re-run install.js to fix it.`;
                    gpuWrap.classList.add('text-danger');
                    gpuWrap.classList.remove('text-light');
                } else if (stats.gpu) {
                    const used = stats.gpu.reserved_gb.toFixed(1);
                    const total = stats.gpu.total_gb.toFixed(1);
                    gpuEl.textContent = `${used}/${total} GB`;
                    gpuWrap.title = '';
                    if (stats.gpu.allocated_percent > 90) {
                        gpuWrap.classList.add('text-danger');
                        gpuWrap.classList.remove('text-light');
                    } else {
                        gpuWrap.classList.remove('text-danger');
                        gpuWrap.classList.add('text-light');
                    }
                } else {
                    gpuEl.textContent = 'N/A';
                    gpuWrap.title = '';
                }

                diskEl.textContent = `${stats.disk.free_gb} GB`;
                if (stats.disk.low_space) {
                    diskWrap.classList.add('text-danger');
                    diskWrap.classList.remove('text-light');
                } else {
                    diskWrap.classList.remove('text-danger');
                    diskWrap.classList.add('text-light');
                }
            } catch (e) { console.error('Failed to update system stats', e); }
            finally { _systemStatsPending = false; }
        }

        // Format a duration in seconds as a short "1h 5m" / "5m 30s" / "30s" string.
        function formatDuration(seconds) {
            if (seconds == null || !isFinite(seconds) || seconds < 0) { return '--'; }
            seconds = Math.round(seconds);
            if (seconds < 60) { return `${seconds}s`; }
            const m = Math.floor(seconds / 60);
            if (seconds < 3600) { return `${m}m ${seconds % 60}s`; }
            const h = Math.floor(seconds / 3600);
            return `${h}h ${Math.floor((seconds % 3600) / 60)}m`;
        }

        // Always-visible "what's running and how long until it's done" indicator,
        // shown next to the GPU/disk stats so it's visible from any tab.
        let _etaStatusPending = false;
        async function updateEtaStatus() {
            if (_etaStatusPending) { return; }
            const wrap = document.getElementById('sys-eta');
            const valEl = document.getElementById('sys-eta-val');
            _etaStatusPending = true;
            try {
                const eta = await API.get('/api/status/eta');
                if (!eta.running) {
                    wrap.style.display = 'none';
                    return;
                }
                let text = eta.label;
                if (eta.progress) { text += ` — ${eta.progress}`; }
                if (eta.eta_seconds != null) {
                    text += ` (ETA ${formatDuration(eta.eta_seconds)})`;
                } else if (eta.elapsed_seconds != null) {
                    text += ` (running ${formatDuration(eta.elapsed_seconds)})`;
                }
                valEl.textContent = text;
                wrap.style.display = 'flex';
            } catch (e) {
                console.error('Failed to update ETA status', e);
                wrap.style.display = 'none';
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
            try {
                const status = await API.get('/api/lmstudio/status');
                if (status.remote) {
                    badge.textContent = 'Remote (optimize via SSH)';
                    toggle.checked = !!status.optimized;
                    badge.className = 'badge bg-info text-dark';
                    toggle.disabled = false;
                } else if (!status.available) {
                    badge.textContent = 'lms CLI not found';
                    badge.className = 'badge bg-secondary';
                    toggle.disabled = true;
                } else if (!status.loaded) {
                    badge.textContent = 'Model not loaded';
                    badge.className = 'badge bg-secondary';
                    toggle.disabled = false;
                    toggle.checked = false;
                } else if (status.optimized) {
                    badge.textContent = `On (ctx ${status.context_length}, parallel ${status.parallel})`;
                    badge.className = 'badge bg-success';
                    toggle.disabled = false;
                    toggle.checked = true;
                } else {
                    badge.textContent = `Off (ctx ${status.context_length}, parallel ${status.parallel})`;
                    badge.className = 'badge bg-warning text-dark';
                    toggle.disabled = false;
                    toggle.checked = false;
                }
            } catch (e) {
                badge.textContent = 'Status unavailable';
                badge.className = 'badge bg-secondary';
            }
        }

        async function toggleLmStudioOptimize() {
            const toggle = document.getElementById('lmstudio-optimize-toggle');
            const badge = document.getElementById('lmstudio-status-badge');
            const enable = toggle.checked;
            toggle.disabled = true;
            badge.textContent = 'Applying...';
            badge.className = 'badge bg-secondary';
            try {
                await API.post('/api/lmstudio/optimize', { enable });
                showToast(enable ? 'LM Studio set to VRAM-safe settings' : 'LM Studio reset to default settings', 'success');
            } catch (e) {
                showToast('Failed to update LM Studio settings: ' + (e.message || 'unknown error'), 'error');
                toggle.checked = !enable;
            } finally {
                toggle.disabled = false;
                await refreshLmStudioStatus();
            }
        }

        function reattachTaskActivity(taskName, buttonIds = [], statusId = null, afterDone = null) {
            claimTaskStart(taskName);
            const buttons = buttonIds.map(id => document.getElementById(id)).filter(Boolean);
            const statusEl = statusId ? document.getElementById(statusId) : null;
            const label = taskName.replaceAll('_', ' ');
            buttons.forEach(button => { button.disabled = true; });
            if (statusEl) { statusEl.textContent = `${label} running...`; }
            else { showToast(`${label} is still running.`, 'info'); }
            _startPolling(`reattach:${taskName}`, () => API.get(`/api/status/${encodeURIComponent(taskName)}`), {
                doneCheck: state => !state.running,
                onTick: state => {
                    const last = (state.logs || []).slice(-1)[0];
                    if (statusEl && last) { statusEl.textContent = last; }
                },
                onDone: async state => {
                    releaseTaskStart(taskName);
                    buttons.forEach(button => { button.disabled = false; });
                    const message = (state.logs || []).slice(-1)[0] || `${label} finished; the original request result is unavailable after reload.`;
                    if (statusEl) { statusEl.textContent = message; }
                    else { showToast(message, 'info'); }
                    notifyJobDone(taskName);
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
                if (!status || status.running || !Array.isArray(status.logs) || !status.logs.length) { continue; }
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
                showToast('Could not restore running task controls: ' + e.message, 'warning');
                return;
            }
            let running = Object.fromEntries(Object.entries(statuses).map(([name, state]) => [name, state.running]));

            const logGroups = [
                { elementId: 'script-logs', tasks: ['script', 'batch_script', 'review', 'batch_review', 'nicknames'] },
                { elementId: 'voices-logs', tasks: ['persona'] },
                { elementId: 'audio-logs', tasks: ['audio'] },
                { elementId: 'voicelab-logs', tasks: ['voicelab'] },
            ];
            const pollGenerations = logGroups.map(group => getLogPollGeneration(group.tasks));
            const fetched = await Promise.all(logGroups.flatMap(group => group.tasks).map(async taskName => {
                if (running[taskName]) { return null; }
                try {
                    return [taskName, await API.get(`/api/status/${taskName}`)];
                } catch (e) {
                    console.debug(`${taskName} log hydration failed`, e);
                    return null;
                }
            }));
            if (generation !== _reattachGeneration) { return; }
            const hydrated = Object.fromEntries(fetched.filter(Boolean));
            running = Object.fromEntries(Object.entries({ ...statuses, ...hydrated }).map(([name, state]) => [name, state.running]));
            logGroups.forEach((group, index) => {
                if (group.tasks.some(name => running[name]) || pollGenerations[index] !== getLogPollGeneration(group.tasks)) { return; }
                const latest = getLatestCompletedLog(group.tasks, hydrated);
                const el = document.getElementById(group.elementId);
                if (el && latest) {
                    el.innerText = latest.logs.join('\n');
                    el.title = `Restored ${latest.name.replaceAll('_', ' ')} logs`;
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
                    pollLogs('audio', 'audio-logs', () => { show('btn-cancel-merge', 'none'); });
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
                drift_check: () => { reattachTaskActivity('drift_check', [], null, () => loadChunks(false)); },
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
                    const attach = Object.prototype.hasOwnProperty.call(reattachers, name)
                        ? reattachers[name] : () => reattachTaskActivity(name);
                    try {
                        await attach();
                    } catch (e) {
                        showToast(`Could not restore ${name.replaceAll('_', ' ')} controls: ${e.message}`, 'warning');
                    }
                }
            }));
        }

        // Init
        loadConfig();
        loadVoices();
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
            const files = document.getElementById('prep-batch-files').files;
            const tbody = document.getElementById('prep-batch-queue-body');
            tbody.innerHTML = '';
            prepBatchQueue = [];

            if (!files.length) {
                document.getElementById('prep-batch-queue-container').style.display = 'none';
                return;
            }
            document.getElementById('prep-batch-queue-container').style.display = 'block';

            [...files].forEach((file, i) => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td class="text-truncate" style="max-width:350px;">${escapeHtml(file.name)}</td>
                    <td id="prep-batch-status-${i}"><span class="badge bg-secondary">Pending</span></td>
                `;
                tbody.appendChild(row);
                prepBatchQueue.push({ audio: file });
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
        }

        async function _submitPreparer(taskName, formData) {
            if (prepSubmitting || prepActiveTask) { return; }
            const task = PREPARER_TASKS[taskName];
            _applyPreparerControls(null, true);
            document.getElementById('preparer-progress-section').style.display = 'block';
            document.getElementById('prep-status-msg').innerHTML = `<span class="text-info">${task.starting}</span>`;
            try {
                const res = await fetch(task.start, {method: 'POST', body: formData});
                await API._handleError(res);
                _applyPreparerControls(taskName);
                _pollPreparerLogs(taskName);
            } catch (e) {
                _applyPreparerControls(null);
                showToast(task.failure + e.message, 'error');
            }
        }

        window.startPreparer = async () => {
            const isBatch = document.getElementById('prep-batch-mode').checked;
            if (isBatch) { return _startBatchPreparer(); }

            const audioFile = document.getElementById('prep-audio-file').files[0];
            if (!audioFile) { showToast('Audio file required', 'error'); return; }

            const sourceFile = document.getElementById('prep-source-file').files[0];
            const diarizationMode = document.getElementById('prep-diarization-mode').value;

            const config = {
                audio_filename: audioFile.name,
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
                llm_model_path:  document.getElementById('prep-llm-model-path').value || null,
                enrich_speaker_attribution: document.getElementById('prep-enrich-speaker').checked,
                enrich_narration_style:     document.getElementById('prep-enrich-narration').checked,
                enrich_emotional_tone:      document.getElementById('prep-enrich-emotion').checked,
                diarize:                    diarizationMode === 'full',
                auto_detect_speakers:       diarizationMode === 'auto',
                hf_token:                   document.getElementById('prep-hf-token').value || null,
            };

            const fd = new FormData();
            fd.append('config_json', JSON.stringify(config));
            fd.append('audio_file', audioFile);
            if (sourceFile) { fd.append('source_file', sourceFile); }

            return _submitPreparer('preparer', fd);
        };

        window.cancelPreparer = () => {
            if (!prepActiveTask) { return; }
            return cancelTask(PREPARER_TASKS[prepActiveTask].cancel);
        };

        async function _startBatchPreparer() {
            if (!prepBatchQueue.length) { showToast('No files selected', 'warning'); return; }

            const files = prepBatchQueue.map(t => t.audio);
            const tasks = files.map(file => ({
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
            files.forEach(file => { fd.append('audio_files', file); });
            return _submitPreparer('batch_preparer', fd);
        }

        function _pollPreparerLogs(taskName) {
            _applyPreparerControls(taskName);
            const logEl = document.getElementById('preparer-logs');
            let offset = 0;

            _startPolling(taskName, () => API.get(`/api/status/${taskName}`), {
                doneCheck: state => !state.running,
                onTick: state => {
                    if (prepActiveTask !== taskName) { return; }
                    const newLines = state.logs.slice(offset);
                    offset = state.logs.length;
                    newLines.forEach(line => {
                        const div = document.createElement('div');
                        div.textContent = line;
                        logEl.appendChild(div);
                    });
                    logEl.scrollTop = logEl.scrollHeight;

                    // Update batch queue status badges
                    if (taskName === 'batch_preparer' && state.tasks) {
                        state.tasks.forEach((t, i) => {
                            const el = document.getElementById(`prep-batch-status-${i}`);
                            if (!el) { return; }
                            const colours = { pending: 'secondary', running: 'primary', done: 'success', failed: 'danger', cancelled: 'warning' };
                            const colour = Object.prototype.hasOwnProperty.call(colours, t.status) ? colours[t.status] : 'secondary';
                            el.innerHTML = `<span class="badge bg-${colour}">${escapeHtml(t.status)}</span>`;
                        });
                    }
                },
                onDone: state => {
                    if (prepActiveTask !== taskName) { return; }
                    notifyJobDone(taskName);
                    _applyPreparerControls(null);
                    const msg = taskName === 'preparer' ? state.status : 'Batch finished';
                    document.getElementById('prep-status-msg').innerHTML = `<span class="text-muted">${escapeHtml(msg)}</span>`;
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
                    el.innerHTML = '<div class="text-muted small">No datasets yet. Completed preparer runs will appear here.</div>';
                    return;
                }
                el.innerHTML = files.map(f => {
                    const when = f.modified ? new Date(f.modified * 1000).toLocaleString() : '';
                    return `<div class="list-group-item d-flex justify-content-between align-items-center">
                        <span class="text-truncate me-2">
                            <i class="fas fa-file-zipper me-2"></i>${escapeHtml(f.filename)}
                            <span class="text-muted ms-1">${f.size_mb} MB${when ? ' · ' + escapeHtml(when) : ''}</span>
                        </span>
                        <a class="btn btn-sm btn-outline-success flex-shrink-0" href="/api/preparer/download/${encodeURIComponent(f.filename)}" download>
                            <i class="fas fa-download me-1"></i>Download
                        </a>
                    </div>`;
                }).join('');
            } catch (e) {
                el.innerHTML = `<div class="text-danger small">${escapeHtml(e.message || String(e))}</div>`;
            }
        }
