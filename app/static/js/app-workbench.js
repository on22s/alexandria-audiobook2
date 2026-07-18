        // ── Dataset Builder ──────────────────────────────────────

        // In-memory row data: [{emotion, text, status, audio_url}, ...]
        let dsbRows = [];
        let dsbPolling = null;
        let dsbBatchRunning = false;
        let dsbSaveMetaTimer = null;
        let dsbSaveRowsTimer = null;
        let dsbCurrentProject = '';

        // Clean up legacy localStorage
        localStorage.removeItem('alexandria-dsb-form');

        async function dsbLoadProjects(selectName) {
            try {
                const projects = await API.get('/api/dataset_builder/list');
                const select = document.getElementById('dsb-project-select');
                select.innerHTML = '<option value="">-- Select project --</option>' +
                    projects.map(p => `<option value="${p.name}">${p.name} (${p.done_count}/${p.sample_count})</option>`).join('');
                if (selectName) {
                    select.value = selectName;
                    dsbOnProjectChange();
                }
            } catch (e) { console.error('Failed to load projects:', e); }
        }

        window.dsbOnProjectChange = async () => {
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

        async function dsbLoadProject(name) {
            try {
                const result = await API.get(`/api/dataset_builder/status/${encodeURIComponent(name)}`);
                document.getElementById('dsb-description').value = result.description || '';
                document.getElementById('dsb-global-seed').value = result.global_seed || '';
                dsbRows = (result.samples || []).map(s => ({
                    emotion: s.emotion || s.description || '',
                    text: s.text || '',
                    seed: s.seed ?? '',
                    status: s.status || 'pending',
                    audio_url: s.audio_url || null,
                }));
                if (dsbRows.length === 0) { dsbAddRow(); }
                dsbRenderTable();
                // Resume polling if batch is running
                if (result.running) {
                    dsbBatchRunning = true;
                    dsbStartPolling(name);
                    document.getElementById('dsb-btn-gen-all').style.display = 'none';
                    document.getElementById('dsb-btn-regen-all').style.display = 'none';
                    document.getElementById('dsb-btn-cancel').style.display = '';
                }
            } catch (e) {
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
            if (!dsbCurrentProject) { return; }
            if (!await showConfirm(`Delete project "${dsbCurrentProject}" and all its samples?`)) { return; }
            try {
                await fetch(`/api/dataset_builder/${encodeURIComponent(dsbCurrentProject)}`, { method: 'DELETE' });
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

        function dsbSaveForm() {
            if (!dsbCurrentProject) { return; }
            clearTimeout(dsbSaveMetaTimer);
            dsbSaveMetaTimer = setTimeout(async () => {
                try {
                    await API.post('/api/dataset_builder/update_meta', {
                        name: dsbCurrentProject,
                        description: document.getElementById('dsb-description').value,
                        global_seed: document.getElementById('dsb-global-seed').value,
                    });
                } catch (e) {
                    _toastSaveError('meta', e);
                }
            }, 500);
        }

        function dsbSaveRows() {
            if (!dsbCurrentProject) { return; }
            clearTimeout(dsbSaveRowsTimer);
            dsbSaveRowsTimer = setTimeout(async () => {
                try {
                    await API.post('/api/dataset_builder/update_rows', {
                        name: dsbCurrentProject,
                        rows: dsbRows.map(r => ({ emotion: r.emotion || '', text: (r.text || '').trim(), seed: r.seed ?? '' })),
                    });
                } catch (e) {
                    _toastSaveError('rows', e);
                }
            }, 500);
        }

        function dsbAddRow(emotion = '', text = '', seed = '') {
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
            dsbRows.splice(index, 1);
            dsbRenderTable();
            dsbSaveRows();
            dsbUpdateRefDropdown();
        }

        function dsbBuildRowHtml(row, i) {
            const statusColor = row.status === 'done' ? 'success' :
                                row.status === 'generating' ? 'warning' :
                                row.status === 'error' ? 'danger' : 'secondary';
            const statusLabel = row.status || 'pending';

            let actionHtml = '';
            if (row.status === 'generating') {
                actionHtml = '<div class="progress" style="width:80px;height:20px;"><div class="progress-bar progress-bar-striped progress-bar-animated bg-warning" style="width:100%"></div></div>';
            } else {
                const genLabel = row.status === 'done' ? '<i class="fas fa-redo"></i>' : '<i class="fas fa-play"></i>';
                actionHtml = `<button class="btn btn-sm btn-primary" onclick="dsbGenSample(${i})" title="${row.status === 'done' ? 'Regenerate' : 'Generate'}">${genLabel}</button>`;
            }

            let audioHtml = '';
            if (row.status === 'done' && row.audio_url) {
                audioHtml = `<audio controls src="${row.audio_url}" style="width:180px;height:28px;" onplay="dsbStopOthers(${i})"></audio>`;
            }

            return `<tr data-dsb-idx="${i}" data-dsb-status="${row.status || 'pending'}" data-dsb-audio="${row.audio_url || ''}" class="${row.status === 'generating' ? 'table-info' : ''}">
                <td class="text-center align-middle">${i + 1}</td>
                <td><input type="text" class="form-control form-control-sm" value="${escapeHtml(row.emotion || '')}" onchange="dsbUpdateRow(${i}, 'emotion', this.value)" placeholder="e.g. Savagely sarcastic"></td>
                <td><textarea class="form-control form-control-sm" rows="2" onchange="dsbUpdateRow(${i}, 'text', this.value)" placeholder="Sample text...">${escapeHtml(row.text || '')}</textarea></td>
                <td><input type="number" class="form-control form-control-sm" value="${escapeHtml(row.seed ?? '')}" onchange="dsbUpdateRow(${i}, 'seed', this.value)" placeholder="-" style="width:65px;" min="-1"></td>
                <td class="text-center align-middle"><span class="badge bg-${statusColor}">${statusLabel}</span></td>
                <td class="align-middle">
                    <div class="d-flex align-items-center gap-1">
                        ${actionHtml}
                        ${audioHtml}
                        <button class="btn btn-sm btn-outline-danger ms-auto" onclick="dsbRemoveRow(${i})" title="Delete row"><i class="fas fa-trash"></i></button>
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
            if (dsbRows[index]) {
                dsbRows[index][field] = value;
                dsbSaveRows();
            }
        };

        window.dsbStopOthers = (index) => {
            document.querySelectorAll('#dsb-table-body audio').forEach(audio => {
                const row = audio.closest('tr');
                if (row && parseInt(row.getAttribute('data-dsb-idx')) !== index) { audio.pause(); }
            });
        };

        let dsbLastDoneCount = -1;

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
            // Only rebuild dropdown when done count actually changes
            if (done !== dsbLastDoneCount) {
                dsbLastDoneCount = done;
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
                dsbRows[index].status = 'done';
                dsbRows[index].audio_url = result.audio_url;
            } catch (e) {
                dsbRows[index].status = 'error';
                console.error('Sample generation failed:', e);
            }
            dsbRenderTable([index]);
        };

        // Batch generation
        window.dsbGenerateAll = async (regenAll = false) => {
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
            if (regenAll && !await showConfirm(`Regenerate all ${indices.length} samples?`)) return;

            // Mark as generating
            indices.forEach(i => { dsbRows[i].status = 'generating'; });
            dsbRenderTable();
            dsbBatchRunning = true;
            document.getElementById('dsb-btn-gen-all').style.display = 'none';
            document.getElementById('dsb-btn-regen-all').style.display = 'none';
            document.getElementById('dsb-btn-cancel').style.display = '';
            document.getElementById('dsb-logs').style.display = '';

            const globalSeed = parseInt(document.getElementById('dsb-global-seed').value);
            const perSeeds = dsbRows.map(r => r.seed !== '' && r.seed !== undefined ? parseInt(r.seed) : -1);

            try {
                await API.post('/api/dataset_builder/generate_batch', {
                    name,
                    description: rootDesc,
                    samples: dsbRows.map(r => ({ emotion: r.emotion || '', text: r.text || '' })),
                    indices,
                    global_seed: !isNaN(globalSeed) && globalSeed >= 0 ? globalSeed : -1,
                    seeds: perSeeds,
                });

                // Start polling
                dsbStartPolling(name);
            } catch (e) {
                showToast('Batch generation failed: ' + e.message, 'error');
                dsbStopBatch();
            }
        };

        function dsbStartPolling(name) {
            if (dsbPolling) { dsbPolling(); }
            dsbPolling = _startPolling(`dataset_builder:${name}`, () => API.get(`/api/dataset_builder/status/${encodeURIComponent(name)}`), {
                intervalMs: 2000,
                doneCheck: result => !result.running,
                onTick: result => {
                    const serverSamples = result.samples || [];

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

                    if (added) {
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

                    // Resume polling if server is still running (e.g. after page reload)
                    if (result.running && !dsbBatchRunning) {
                        dsbBatchRunning = true;
                        document.getElementById('dsb-btn-gen-all').style.display = 'none';
                        document.getElementById('dsb-btn-regen-all').style.display = 'none';
                        document.getElementById('dsb-btn-cancel').style.display = '';
                    }
                },
                onDone: () => {
                    // Check if batch is done
                    if (dsbBatchRunning) {
                        notifyJobDone('dataset_builder');
                        dsbStopBatch();
                    }
                }
            });
        }

        function dsbStopBatch() {
            dsbBatchRunning = false;
            if (dsbPolling) { dsbPolling(); dsbPolling = null; }
            document.getElementById('dsb-btn-gen-all').style.display = '';
            document.getElementById('dsb-btn-regen-all').style.display = '';
            document.getElementById('dsb-btn-cancel').style.display = 'none';
            dsbRenderTable();
        }

        window.dsbCancel = () => cancelTask('/api/dataset_builder/cancel');

        // Import / Export
        window.dsbImport = (event) => {
            const file = event.target.files[0];
            if (!file) { return; }
            const reader = new FileReader();
            reader.onload = (e) => {
                try {
                    const data = JSON.parse(e.target.result);
                    if (!Array.isArray(data)) { throw new Error('Expected JSON array'); }
                    dsbRows = data.map(item => ({
                        emotion: item.emotion || item.instruct || '',
                        text: item.text || '',
                        seed: item.seed ?? '',
                        status: 'pending',
                        audio_url: null,
                    }));
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

        async function updateSystemStats() {
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
        async function updateEtaStatus() {
            const wrap = document.getElementById('sys-eta');
            const valEl = document.getElementById('sys-eta-val');
            try {
                const eta = await API.get('/api/status/eta');
                if (!eta.running) {
                    wrap.style.display = 'none';
                    return;
                }
                let text = eta.label;
                if (eta.progress) text += ` — ${eta.progress}`;
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
            }
        }

        async function refreshLmStudioStatus() {
            const badge = document.getElementById('lmstudio-status-badge');
            const toggle = document.getElementById('lmstudio-optimize-toggle');
            if (!badge || !toggle) { return; }
            try {
                const status = await API.get('/api/lmstudio/status');
                if (status.remote) {
                    badge.textContent = 'Remote (optimize via SSH)';
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
                await refreshLmStudioStatus();
            }
        }

        // Re-attach the live #script-logs poller if a long task is still running
        // after a page reload. The window is only fed while its poller is active,
        // so without this a refresh mid-run leaves the window blank even though the
        // job is healthy server-side. Only one task feeds this window at a time, so
        // attach the first running one in priority order.
        async function reattachRunningPollers() {
            // Fetch all task statuses in parallel; each falls back to false on error.
            const names = ['batch_script', 'script', 'batch_review', 'review', 'nicknames', 'voicelab'];
            const flags = await Promise.all(names.map(t =>
                API.get(`/api/status/${t}`).then(r => r.running).catch(() => false)
            ));
            const running = Object.fromEntries(names.map((t, i) => [t, flags[i]]));

            const show = (id, disp = 'inline-block') => {
                const el = document.getElementById(id); if (el) { el.style.display = disp; }
            };
            const disable = (id) => {
                const el = document.getElementById(id); if (el) { el.disabled = true; }
            };

            if (running.batch_script) {
                disable('btn-gen-script');
                show('btn-pause-batch-script'); show('btn-cancel-batch-script');
                _pollScriptBatchLogs();
            } else if (running.script) {
                disable('btn-gen-script');
                show('btn-cancel-script'); show('btn-pause-script');
                pollLogs('script', 'script-logs', () => {
                    if (!scriptBatchPoller) {
                        const b = document.getElementById('btn-gen-script'); if (b) { b.disabled = false; }
                    }
                    show('btn-cancel-script', 'none'); show('btn-pause-script', 'none');
                });
            } else if (running.batch_review) {
                disable('btn-review-batch-start');
                show('btn-pause-batch-review'); show('btn-cancel-batch-review');
                await loadReviewBatchScripts();
                pollReviewBatch();
            } else if (running.review) {
                _disableReviewButtons(true);
                _showReviewControls(true);
                pollLogs('review', 'script-logs', _onReviewDone);
            } else if (running.nicknames) {
                disable('btn-find-nicknames');
                show('btn-pause-nick'); show('btn-cancel-nick');
                pollLogs('nicknames', 'script-logs', async () => {
                    const btn = document.getElementById('btn-find-nicknames');
                    if (btn) { btn.disabled = false; }
                    show('btn-pause-nick', 'none'); show('btn-cancel-nick', 'none');
                    await loadCharacterAliases(true);
                });
            } else if (running.voicelab) {
                _vlSetRunning(true);
                refreshVoicelabHealth();
                pollVoicelab();
            }
        }

        // Init
        loadConfig();
        loadVoices();
        loadSavedScripts();
        loadDesignedVoices();
        dsbLoadProjects();
        updateSystemStats();
        updateEtaStatus();
        refreshLmStudioStatus();
        reattachRunningPollers();
        setInterval(updateSystemStats, 10000); // Update every 10s
        setInterval(updateEtaStatus, 10000); // Update every 10s
        setInterval(refreshLmStudioStatus, 30000); // Update every 30s

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
                prepBatchQueue.push({ audio: file.name });
            });
        };

        window.startPreparer = async () => {
            const isBatch = document.getElementById('prep-batch-mode').checked;
            if (isBatch) { return _startBatchPreparer(); }

            const audioFile = document.getElementById('prep-audio-file').files[0];
            if (!audioFile) { showToast('Audio file required', 'error'); return; }

            const btn = document.getElementById('btn-prep-start');
            btn.disabled = true;
            document.getElementById('btn-prep-cancel').style.display = 'inline-block';
            document.getElementById('preparer-progress-section').style.display = 'block';
            document.getElementById('prep-status-msg').innerHTML = '<span class="text-info">Starting…</span>';

            const sourceFile = document.getElementById('prep-source-file').files[0];

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
            };

            const fd = new FormData();
            fd.append('config_json', JSON.stringify(config));
            fd.append('audio_file', audioFile);
            if (sourceFile) { fd.append('source_file', sourceFile); }

            try {
                const res = await fetch('/api/preparer/start', { method: 'POST', body: fd });
                if (!res.ok) { throw new Error((await res.json()).detail || res.statusText); }
                _pollPreparerLogs('preparer');
            } catch (e) {
                showToast('Failed to start: ' + e.message, 'error');
                btn.disabled = false;
                document.getElementById('btn-prep-cancel').style.display = 'none';
            }
        };

        window.cancelPreparer = () => {
            const isBatch = document.getElementById('prep-batch-mode').checked;
            const url = isBatch ? '/api/preparer/batch/cancel' : '/api/preparer/cancel';
            return cancelTask(url);
        };

        async function _startBatchPreparer() {
            if (!prepBatchQueue.length) { showToast('No files selected', 'warning'); return; }

            const btn = document.getElementById('btn-prep-start');
            btn.disabled = true;
            document.getElementById('btn-prep-cancel').style.display = 'inline-block';
            document.getElementById('preparer-progress-section').style.display = 'block';
            document.getElementById('prep-status-msg').innerHTML = '<span class="text-info">Starting batch…</span>';

            const tasks = prepBatchQueue.map(t => ({
                audio_filename:  t.audio,
                output_filename: `voice_dataset_${t.audio.replace(/\.[^.]+$/, '')}.zip`,
            }));
            const body = {
                tasks,
                lang:           document.getElementById('prep-lang').value,
                min_confidence: getNumFieldValue('prep-confidence', 0.85),
                min_snr:        getNumFieldValue('prep-snr', 25, true),
            };

            try {
                await API.post('/api/preparer/batch/start', body);
                _pollPreparerLogs('batch_preparer');
            } catch (e) {
                showToast('Failed to start batch: ' + e.message, 'error');
                btn.disabled = false;
                document.getElementById('btn-prep-cancel').style.display = 'none';
            }
        }

        function _pollPreparerLogs(taskName) {
            const logEl = document.getElementById('preparer-logs');
            let offset = 0;

            _startPolling(taskName, () => API.get(`/api/status/${taskName}`), {
                doneCheck: state => !state.running,
                onTick: state => {
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
                            el.innerHTML = `<span class="badge bg-${colours[t.status] || 'secondary'}">${t.status}</span>`;
                        });
                    }
                },
                onDone: state => {
                    notifyJobDone(taskName);
                    document.getElementById('btn-prep-start').disabled = false;
                    document.getElementById('btn-prep-cancel').style.display = 'none';
                    const msg = taskName === 'preparer' ? state.status : 'Batch finished';
                    document.getElementById('prep-status-msg').innerHTML = `<span class="text-muted">${msg}</span>`;
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

