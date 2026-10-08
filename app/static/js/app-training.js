        let loraDatasetsRequest = 0;
        let loraModelsRequest = 0;
        async function loadLoraDatasets() {
            const request = ++loraDatasetsRequest;
            const status = document.getElementById('lora-datasets-refresh-status');
            const retry = document.getElementById('lora-datasets-refresh-retry');
            retry.disabled = true;
            try {
                const datasets = await API.get('/api/lora/datasets');
                if (request !== loraDatasetsRequest) { return null; }
                if (!Array.isArray(datasets) || datasets.some(d => !d || typeof d.dataset_id !== 'string')) { throw new Error('Dataset list is malformed'); }
                status.textContent = '';
                retry.hidden = true;
                const listEl = document.getElementById('lora-datasets-list');
                const selectEl = document.getElementById('lora-dataset-select');

                // Update dropdown
                const currentVal = selectEl.value;
                selectEl.innerHTML = '<option value="">-- Select dataset --</option>' +
                    datasets.map(d => `<option value="${escapeHtml(d.dataset_id)}">${escapeHtml(d.dataset_id)} (${d.sample_count} samples)</option>`).join('');
                if (currentVal) { selectEl.value = currentVal; }

                // Update list
                if (!datasets.length) {
                    listEl.innerHTML = '<span class="text-muted">No datasets uploaded yet.</span>';
                    return datasets;
                }
                listEl.innerHTML = datasets.map(d => `
                    <div class="d-flex justify-content-between align-items-center py-1">
                        <span><strong>${escapeHtml(d.dataset_id)}</strong> <small class="text-muted">(${d.sample_count} samples)</small></span>
                        <button class="btn btn-sm btn-outline-danger" data-dataset-id="${escapeHtml(d.dataset_id)}" onclick="deleteLoraDataset(this.dataset.datasetId)" aria-label="${escapeHtml('Delete dataset ' + d.dataset_id)}" title="${escapeHtml('Delete dataset ' + d.dataset_id)}"><i class="fas fa-trash"></i></button>
                    </div>
                `).join('');
                return datasets;
            } catch (e) {
                console.error('Failed to load LoRA datasets:', e);
                if (request === loraDatasetsRequest) {
                    status.textContent = 'Could not load datasets. Previously loaded datasets and your selection are kept. Check that Alexandria is running, then retry the list refresh.';
                    retry.hidden = false;
                }
                return null;
            } finally {
                if (request === loraDatasetsRequest) { retry.disabled = false; }
            }
        }

        let loraDatasetUploadPending = false;
        window.uploadLoraDataset = async () => {
            if (loraDatasetUploadPending) { return; }
            const fileInput = document.getElementById('lora-dataset-file');
            if (!fileInput.files.length) { showToast('Select a ZIP file first.', 'warning'); return; }

            const file = fileInput.files[0];
            if (!file.name.toLowerCase().endsWith('.zip')) { showToast('File must be a .zip archive.', 'warning'); return; }

            const formData = new FormData();
            formData.append('file', file);
            const button = document.getElementById('btn-lora-upload');
            const label = button.innerHTML;
            const status = document.getElementById('lora-upload-status');
            const select = document.getElementById('lora-dataset-select');
            const selected = select.value;
            loraDatasetUploadPending = true;
            button.disabled = true;
            button.textContent = 'Uploading…';
            status.textContent = `Uploading ${file.name}…`;

            try {
                const res = await fetch('/api/lora/upload_dataset', { method: 'POST', body: formData });
                if (!res.ok) {
                    const err = await res.json();
                    status.textContent = 'Upload refused. Keep the ZIP and review the archive validation details before trying again.';
                    showActionError('Dataset upload refused', {message: err.detail || 'Upload failed.', status: res.status}, 'Keep the selected ZIP. Review the archive validation details and correct the dataset before uploading again.');
                    return;
                }
                const result = await res.json();
                showToast(`Dataset "${result.dataset_id}" uploaded (${result.sample_count} samples).`, 'success');
                if (fileInput.files[0] === file) { fileInput.value = ''; }
                status.textContent = `Dataset "${result.dataset_id}" uploaded. Refreshing the dataset list…`;
                const datasets = await loadLoraDatasets();
                if (Array.isArray(datasets) && datasets.some(dataset => dataset.dataset_id === result.dataset_id) && select.value === selected) {
                    select.value = result.dataset_id;
                    status.textContent = `Dataset "${result.dataset_id}" uploaded and selected for training.`;
                } else {
                    status.textContent = `Dataset "${result.dataset_id}" uploaded. Review the dataset list and select it when you are ready to train.`;
                }
            } catch (e) {
                status.textContent = 'Upload was not confirmed. Keep the ZIP and check the dataset list before uploading again.';
                showActionError("Upload error", e, "Keep the ZIP file. Check the dataset list before uploading again, and review any archive validation details.");
            } finally {
                loraDatasetUploadPending = false;
                button.disabled = false;
                button.innerHTML = label;
            }
        };

        window.deleteLoraDataset = async (datasetId) => {
            if (!await showConfirm(`Delete dataset "${datasetId}"?`, {title: 'Delete training dataset?', actionLabel: 'Delete dataset', danger: true})) { return; }
            try {
                const res = await fetch(`/api/lora/datasets/${encodeURIComponent(datasetId)}`, { method: 'DELETE' });
                await API._handleError(res);
                loadLoraDatasets();
            } catch (e) {
                showActionError("Error deleting dataset", e, "Refresh the dataset list and check whether the dataset was deleted before trying again.");
            }
        };


        window.startLoraTraining = async () => {
            const name = document.getElementById('lora-adapter-name').value.trim();
            const datasetId = document.getElementById('lora-dataset-select').value;
            if (!name) { showToast('Enter an adapter name.', 'warning'); return; }
            if (!datasetId) { showToast('Select a dataset.', 'warning'); return; }

            const request = {
                name: name,
                dataset_id: datasetId,
                epochs: Number(document.getElementById('lora-epochs').value),
                lr: Number(document.getElementById('lora-lr').value),
                batch_size: Number(document.getElementById('lora-batch-size').value),
                lora_r: Number(document.getElementById('lora-rank').value),
                lora_alpha: Number(document.getElementById('lora-alpha').value),
                gradient_accumulation_steps: Number(document.getElementById('lora-grad-accum').value),
                language: document.getElementById('lora-language').value || 'english'
            };

            const btn = document.getElementById('btn-lora-train');
            btn.disabled = true;
            document.getElementById('lora-train-status').innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Starting...';

            try {
                const result = await API.post('/api/lora/train', request);
                document.getElementById('btn-lora-cancel').style.display = 'inline-block';
                document.getElementById('lora-progress-section').style.display = 'block';
                document.getElementById('lora-train-status').innerHTML = '<span class="text-info">Training in progress...</span>';
                pollLoraTraining(request.epochs);
            } catch (e) {
                showActionError("Failed to start training", e, "Check training status before starting again. If refused, review the selected dataset, adapter name and training settings.");
                btn.disabled = false;
                document.getElementById('lora-train-status').innerHTML = '';
            }
        };

        function applyLoraProgress(progressBar, percentage, epoch, maxEpoch) {
            progressBar.style.width = `${percentage}%`;
            progressBar.innerText = `${percentage}%`;
            progressBar.ariaValueMin = '0';
            progressBar.ariaValueMax = '100';
            progressBar.ariaValueNow = String(percentage);
            progressBar.ariaValueText = `Epoch ${epoch} of ${maxEpoch}`;
        }

        function applyLoraProgressState(progressBar, outcome) {
            progressBar.classList.remove('bg-info', 'bg-success', 'bg-danger', 'bg-warning');
            const color = {running: 'bg-info', finished: 'bg-success', failed: 'bg-danger', stopped: 'bg-warning'}[outcome];
            progressBar.classList.add(color);
            if (outcome === 'running') { progressBar.classList.add('progress-bar-animated'); }
            else { progressBar.classList.remove('progress-bar-animated'); }
        }

        function pollLoraTraining(totalEpochs) {
            const logsEl = document.getElementById('lora-train-logs');
            const progressBar = document.getElementById('lora-progress-bar');
            const epochDisplay = document.getElementById('lora-epoch-display');
            const lossDisplay = document.getElementById('lora-loss-display');

            const renderLogs = createTaskLogRenderer(logsEl);
            let currentEpoch = 0;
            let maxEpochs = totalEpochs;
            epochDisplay.innerText = '';
            lossDisplay.innerText = '';
            applyLoraProgressState(progressBar, 'running');
            applyLoraProgress(progressBar, 0, currentEpoch, maxEpochs);

            _startPolling('lora_training', () => API.get('/api/status/lora_training'), {
                intervalMs: 2000,
                doneCheck: status => !status.running,
                onTick: status => {
                    const update = renderLogs(status);
                    if (!update.changed) { return; }
                    if (update.reset) {
                        epochDisplay.innerText = '';
                        lossDisplay.innerText = '';
                        currentEpoch = 0;
                        maxEpochs = totalEpochs;
                        applyLoraProgress(progressBar, 0, currentEpoch, maxEpochs);
                    }

                    // Parse latest metrics only from new lines, or all lines after a reset.
                    for (let i = status.logs.length - 1; i >= update.startIndex; i--) {
                        const line = status.logs[i];
                        const epochMatch = line.match(/\[EPOCH\]\s*(\d+)\/(\d+)\s+avg_loss=([\d.]+)/);
                        if (epochMatch) {
                            const epoch = parseInt(epochMatch[1]);
                            const maxEpoch = parseInt(epochMatch[2]);
                            const loss = epochMatch[3];
                            const pct = Math.round((epoch / maxEpoch) * 100);
                            epochDisplay.innerText = `${epoch}/${maxEpoch}`;
                            lossDisplay.innerText = loss;
                            currentEpoch = epoch;
                            maxEpochs = maxEpoch;
                            applyLoraProgress(progressBar, pct, currentEpoch, maxEpochs);
                            break;
                        }
                        const trainMatch = line.match(/\[TRAIN\]\s*epoch=(\d+)\/(\d+)\s+step=\d+\/\d+\s+loss=([\d.]+)/);
                        if (trainMatch) {
                            const epoch = parseInt(trainMatch[1]);
                            const maxEpoch = parseInt(trainMatch[2]);
                            const loss = trainMatch[3];
                            const pct = Math.round(((epoch - 1) / maxEpoch) * 100);
                            epochDisplay.innerText = `${epoch}/${maxEpoch}`;
                            lossDisplay.innerText = loss;
                            currentEpoch = epoch;
                            maxEpochs = maxEpoch;
                            applyLoraProgress(progressBar, pct, currentEpoch, maxEpochs);
                            break;
                        }
                    }
                },
                onDone: status => {
                    notifyJobDone('lora_training', '', 'finished', status);
                    const btn = document.getElementById('btn-lora-train');
                    btn.disabled = false;
                    const cancelBtn = document.getElementById('btn-lora-cancel');
                    cancelBtn.style.display = 'none';
                    cancelBtn.disabled = false;

                    const isDone = status.logs.some(l => l.includes('[DONE]'));
                    const outcome = getTaskCompletionOutcome(status);
                    applyLoraProgressState(progressBar, isDone && outcome === 'finished' ? 'finished' : outcome === 'failed' ? 'failed' : 'stopped');

                    if (isDone && outcome === 'finished') {
                        document.getElementById('lora-train-status').innerHTML = '<span class="text-success"><i class="fas fa-check me-1"></i>Training complete!</span>';
                        applyLoraProgress(progressBar, 100, maxEpochs, maxEpochs);
                        loadLoraModels();
                    } else if (outcome === 'failed') {
                        document.getElementById('lora-train-status').innerHTML = '<span class="text-danger"><i class="fas fa-times me-1"></i>Training failed</span>';
                    } else {
                        document.getElementById('lora-train-status').innerHTML = '<span class="text-warning">Training stopped</span>';
                    }
                }
            });
        }

        window.cancelLoraTraining = async () => {
            const btn = document.getElementById('btn-lora-cancel');
            btn.disabled = true;
            try {
                await API.post('/api/lora/train/cancel', {});
                document.getElementById('lora-train-status').innerHTML = '<span class="text-warning">Cancellation requested…</span>';
            } catch (e) {
                btn.disabled = false;
                showActionError("Failed to cancel training", e, "Check training status before cancelling again; training may still be running.");
            }
        };

        async function onToggleFavoriteAdapter(adapterId, button) {
            if (button?.disabled) { return; }
            if (button) { button.disabled = true; }
            try {
                const result = await API.post(`/api/voice_library/favorites/${encodeURIComponent(adapterId)}`, {});
                window._loraModelsCache = (window._loraModelsCache || []).map(model =>
                    model.id === adapterId ? { ...model, favorite: result.favorite } : model);
                document.querySelectorAll('[data-lora-favorite]').forEach(star => {
                    if (star.dataset.adapterId !== adapterId) { return; }
                    star.classList.toggle('text-warning', result.favorite);
                    star.classList.toggle('text-muted', !result.favorite);
                    star.title = result.favorite ? 'Favorite — voice suggestions prefer it when compatible' : 'Mark as favorite';
                    star.querySelector('i').className = `${result.favorite ? 'fas' : 'far'} fa-star`;
                });
            } catch (e) {
                showActionError("Could not update favorite", e, "Refresh the adapter list and check the favorite state before changing it again.");
            } finally {
                if (button) { button.disabled = false; }
            }
        }

        async function loadLoraModels() {
            const request = ++loraModelsRequest;
            const status = document.getElementById('lora-models-refresh-status');
            const retry = document.getElementById('lora-models-refresh-retry');
            retry.disabled = true;
            try {
                const [loadedModels, backupStatus] = await Promise.all([
                    API.get('/api/lora/models'),
                    API.get('/api/lora/backups').catch(e => {
                        console.debug('LoRA backup status unavailable', e);
                        return { backups: [], total_size_bytes: 0, free_bytes: 0, low_space_warning: false };
                    }),
                ]);
                if (request !== loraModelsRequest) { return; }
                if (!Array.isArray(loadedModels) || loadedModels.some(m => !m || typeof m.id !== 'string')) { throw new Error('Adapter list is malformed'); }
                const backupsByAdapter = new Map();
                backupStatus.backups.forEach(backup => {
                    const adapterId = backup.adapter_id;
                    if (!backupsByAdapter.has(adapterId)) {
                        backupsByAdapter.set(adapterId, backup);
                    }
                });
                const models = loadedModels.map(model => ({
                    ...model, rollback_backup: backupsByAdapter.get(model.id) || null,
                }));
                status.textContent = '';
                retry.hidden = true;
                window._loraModelsCache = models;
                const container = document.getElementById('lora-models-list');
                const testForm = document.getElementById('lora-test-form');

                if (!models.length) {
                    container.innerHTML = '<p class="text-muted mb-0">No adapters available.</p>';
                    testForm.style.display = 'none';
                    return;
                }

                const backupSummary = backupStatus.backups.length || backupStatus.low_space_warning ? `
                    <div class="alert ${backupStatus.low_space_warning ? 'alert-warning' : 'alert-secondary'} py-2 mb-2 small">
                        ${backupStatus.low_space_warning ? '<strong>Low disk space.</strong> ' : ''}
                        Rollback backups: ${backupStatus.backups.length},
                        ${(backupStatus.total_size_bytes / 1024 ** 3).toFixed(2)} GB;
                        ${(backupStatus.free_bytes / 1024 ** 3).toFixed(1)} GB free.
                    </div>` : '';
                const renderCandidateSummary = model => {
                    const summary = model.candidate_summary;
                    if (!summary || summary.state === 'no_candidates') {
                        return '';
                    }
                    const labels = {
                        awaiting_evaluation: 'awaiting evaluation',
                        candidate_recommended: 'candidate recommended',
                        production_recommended: 'production recommended',
                        promoted: 'candidate promoted',
                        rolled_back: 'promotion rolled back',
                    };
                    const counts = [];
                    if (summary.evaluated_count) {
                        counts.push(`${summary.evaluated_count} evaluated`);
                    }
                    if (summary.retained_count) {
                        counts.push(`${summary.retained_count} retained`);
                    }
                    if (summary.duplicate_count) {
                        counts.push(`${summary.duplicate_count} duplicate skipped`);
                    }
                    const unchanged = summary.production_unchanged ? ' · production unchanged' : '';
                    return `<div class="text-muted small mt-1">${escapeHtml(labels[summary.state] || summary.state)}${counts.length ? ` · ${escapeHtml(counts.join(', '))}` : ''}${unchanged}</div>`;
                };
                // Human review tally, kept visually distinct from the automated
                // recommendation above (the "human" icon vs the evaluation badge).
                const renderReviewSummary = model => {
                    const r = model.review_summary;
                    if (!r || !r.count) { return ''; }
                    const parts = [];
                    if (r.preferred_candidate) { parts.push(`${r.preferred_candidate} prefer candidate`); }
                    if (r.preferred_production) { parts.push(`${r.preferred_production} prefer production`); }
                    if (r.tie) { parts.push(`${r.tie} no preference`); }
                    return `<div class="text-muted small mt-1"><i class="fas fa-user me-1"></i>${r.count} human review${r.count === 1 ? '' : 's'}${parts.length ? ` · ${escapeHtml(parts.join(', '))}` : ''}</div>`;
                };
                container.innerHTML = `${backupSummary}
                    <div class="table-responsive"><table class="table table-sm table-hover mb-0">
                        <thead><tr><th>Name</th><th>Dataset</th><th>Epochs</th><th>Final Loss</th><th>Evaluation</th><th>Samples</th><th style="width:240px">Actions</th></tr></thead>
                        <tbody>
                            ${models.map(m => `
                                <tr${m.builtin ? ' class="table-light"' : ''}>
                                    <td><button class="btn btn-sm btn-link p-0 me-1 ${m.favorite ? 'text-warning' : 'text-muted'}" data-adapter-id="${escapeHtml(m.id)}" data-lora-favorite onclick="onToggleFavoriteAdapter(this.dataset.adapterId, this)" title="${m.favorite ? 'Favorite — voice suggestions prefer it when compatible' : 'Mark as favorite'}"><i class="${m.favorite ? 'fas' : 'far'} fa-star"></i></button><strong>${escapeHtml(m.name)}</strong>${m.builtin ? ` <span class="badge bg-secondary">built-in</span>${m.downloaded === false ? ' <span class="badge bg-warning text-dark">not downloaded</span>' : ''}` : ''}</td>
                                    <td>${escapeHtml(m.dataset_id || (m.builtin ? '--' : '--'))}</td>
                                    <td>${m.epochs || '--'}</td>
                                    <td>${m.final_loss != null ? m.final_loss.toFixed(4) : '--'}</td>
                                    <td>${m.checkpoint_swap ? `<span class="badge bg-danger">recovery required</span>` : m.evaluation ? `<span class="badge ${m.evaluation.status === 'pass' ? 'bg-success' : m.evaluation.status === 'warning' ? 'bg-warning text-dark' : 'bg-danger'}" title="${escapeHtml((m.evaluation.warnings || []).join(', '))}">${escapeHtml(m.evaluation.status || 'unknown')}</span>${m.evaluation.recommended_candidate && m.evaluation.recommended_candidate !== 'production' ? ` <small title="Production remains unchanged">recommend ${escapeHtml(m.evaluation.recommended_candidate)}</small>` : ''}` : '--'}${renderCandidateSummary(m)}${renderReviewSummary(m)}</td>
                                    <td title="Dataset quantity only; not a voice-quality score">${m.sample_count || '--'}</td>
                                    <td>
                                        ${m.builtin && m.downloaded === false ? `
                                            <button class="btn btn-sm btn-outline-warning" id="lora-dl-btn-${escapeHtml(m.id)}" data-adapter-id="${escapeHtml(m.id)}" onclick="downloadBuiltinAdapter(this.dataset.adapterId)" title="Download from HuggingFace"><i class="fas fa-download me-1"></i>Download</button>
                                        ` : `
                                            <button class="btn btn-sm ${m.preview_audio_url ? 'btn-outline-success' : 'btn-outline-secondary'} me-1" id="lora-preview-btn-${escapeHtml(m.id)}" data-adapter-id="${escapeHtml(m.id)}" onclick="playLoraPreview(this.dataset.adapterId)" title="${m.preview_audio_url ? 'Play preview' : 'Generate and play preview (first time may take a moment)'}"><i class="fas fa-volume-up"></i></button>
                                            <button class="btn btn-sm btn-outline-primary me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="testLoraModel(this.dataset.adapterId)" title="Generate test line with custom text"><i class="fas fa-flask me-1"></i>Test</button>
                                            ${!m.builtin && m.checkpoint_swap ? `<button class="btn btn-sm btn-danger me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="recoverLoraCheckpointSwap(this.dataset.adapterId)" title="Restore production from the interrupted operation journal"><i class="fas fa-life-ring me-1"></i>Recover</button>` : ''}
                                            ${!m.builtin && !m.checkpoint_swap && m.evaluation?.recommended_candidate && m.evaluation.recommended_candidate !== 'production' ? `<button class="btn btn-sm btn-outline-info me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="openLoraCandidateComparison(this.dataset.adapterId)" title="Listen to matched production and candidate evaluation probes"><i class="fas fa-headphones me-1"></i>Compare</button><button class="btn btn-sm btn-outline-primary me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="openLoraBlindReview(this.dataset.adapterId)" title="Blind A/B listening review — identities hidden until you submit; never promotes"><i class="fas fa-user-secret me-1"></i>Blind review</button><button class="btn btn-sm btn-outline-secondary me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="openLoraReviewHistory(this.dataset.adapterId)" title="Human review history for this adapter"><i class="fas fa-clock-rotate-left me-1"></i>History</button><button class="btn btn-sm btn-outline-success me-1" data-adapter-id="${escapeHtml(m.id)}" data-candidate-id="${escapeHtml(m.evaluation.recommended_candidate)}" onclick="promoteLoraCandidate(this.dataset.adapterId, this.dataset.candidateId)" title="Preserve production, then promote this evaluated candidate"><i class="fas fa-arrow-up me-1"></i>Promote</button>` : ''}
                                            ${!m.builtin && !m.checkpoint_swap && m.promotion?.status === 'promoted' ? `<button class="btn btn-sm btn-outline-warning me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="rollbackLoraPromotion(this.dataset.adapterId)" title="Restore the production checkpoint saved before promotion"><i class="fas fa-undo me-1"></i>Rollback</button>` : ''}
                                            ${!m.builtin && !m.checkpoint_swap && m.rollback_backup ? `<button class="btn btn-sm btn-outline-danger me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="deleteLoraRollbackBackup(this.dataset.adapterId)" title="Delete ${(m.rollback_backup.size_bytes / 1024 ** 2).toFixed(1)} MB rollback backup"><i class="fas fa-hard-drive me-1"></i>Delete backup</button>` : ''}
                                            ${m.builtin ? '' : `<button class="btn btn-sm btn-outline-danger" data-adapter-id="${escapeHtml(m.id)}" onclick="deleteLoraModel(this.dataset.adapterId)" title="Delete"><i class="fas fa-trash"></i></button>`}
                                        `}
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table></div>`;

                // Populate test dropdown
                const dropdown = document.getElementById('lora-test-adapter');
                const prevVal = dropdown.value;
                dropdown.innerHTML = models.filter(m => m.downloaded !== false).map(m =>
                    `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`
                ).join('');
                if (prevVal && models.some(m => m.id === prevVal)) { dropdown.value = prevVal; }
                testForm.style.display = '';
            } catch (e) {
                console.error('Failed to load LoRA models:', e);
                if (request === loraModelsRequest) {
                    status.textContent = 'Could not load adapters. Previously loaded adapters and your selection are kept. Check that Alexandria is running, then retry the list refresh.';
                    retry.hidden = false;
                }
            } finally {
                if (request === loraModelsRequest) { retry.disabled = false; }
            }
        }

        window.openLoraCandidateComparison = async (adapterId) => {
            const panel = document.getElementById('lora-comparison-panel');
            const request = {};
            panel._comparisonRequest = request;
            const isCurrent = () => panel._comparisonRequest === request
                && document.getElementById('lora-comparison-panel') === panel;
            panel.style.display = '';
            panel.innerHTML = '<div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i>Loading comparison…</div>';
            try {
                const comparison = await API.get(`/api/lora/models/${encodeURIComponent(adapterId)}/comparison`);
                if (!isCurrent()) { return; }
                const renderMetrics = probe => {
                    const metrics = probe.metrics || {};
                    const values = [
                        ['speaker similarity', metrics.speaker_similarity],
                        ['clipping', metrics.clipping_ratio],
                        ['silence', metrics.silence_ratio],
                    ].filter(item => item[1] != null);
                    return values.length ? values.map(item => `${escapeHtml(item[0])}: ${Number(item[1]).toFixed(3)}`).join(' · ') : 'No metrics recorded';
                };
                panel.innerHTML = `
                    <div class="card border-info">
                        <div class="card-header d-flex justify-content-between align-items-center">
                            <div><strong>Candidate comparison: ${escapeHtml(comparison.candidate_id)}</strong><br><small class="text-muted">Advisory only — listening does not change production.</small></div>
                            <button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('lora-comparison-panel').style.display='none'" title="Close"><i class="fas fa-times"></i></button>
                        </div>
                        <div class="card-body">
                            ${comparison.reason ? `<p class="small mb-3">${escapeHtml(comparison.reason)}</p>` : ''}
                            ${comparison.probe_pairs.map(pair => `
                                <div class="border rounded p-2 mb-2">
                                    <div class="small mb-2"><strong>${escapeHtml(pair.id)}</strong> · seed ${escapeHtml(String(pair.seed))}<br>${escapeHtml(pair.text)}</div>
                                    <div class="row g-2">
                                        <div class="col-md-6"><label class="form-label small mb-1">Production</label><audio controls preload="none" class="w-100" src="${escapeHtml(pair.production.audio_url)}"></audio><div class="text-muted small">${renderMetrics(pair.production)}</div></div>
                                        <div class="col-md-6"><label class="form-label small mb-1">Candidate</label><audio controls preload="none" class="w-100" src="${escapeHtml(pair.candidate.audio_url)}"></audio><div class="text-muted small">${renderMetrics(pair.candidate)}</div></div>
                                    </div>
                                </div>`).join('')}
                        </div>
                    </div>`;
                panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            } catch (e) {
                if (!isCurrent()) { return; }
                panel.innerHTML = `<div class="alert alert-danger py-2 mb-0">${escapeHtml(getActionErrorMessage("Comparison unavailable", e, "Check the selected adapter and candidate, then reopen Compare."))}</div>`;
            }
        };

        // Blind A/B human review. Identities are hidden by the server until a
        // decision is submitted; this never calls promote (Rule: human feedback
        // never auto-promotes). Reuses the comparison panel area.
        let _blindReview = null;

        function _blindReviewSelectedChoice() {
            const checked = document.querySelector('input[name="blind-pref"]:checked');
            return checked ? checked.value : null;
        }

        window.openLoraBlindReview = async (adapterId) => {
            const panel = document.getElementById('lora-comparison-panel');
            panel.style.display = '';
            panel.innerHTML = '<div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i>Opening blind review…</div>';
            try {
                const session = await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/review/session`, {});
                _blindReview = { adapterId: adapterId, sessionId: session.session_id };
                const pairsHtml = session.pairs.map(pair => `
                    <div class="border rounded p-2 mb-2">
                        <div class="small mb-2"><strong>${escapeHtml(pair.id)}</strong><br>${escapeHtml(pair.text)}</div>
                        <div class="row g-2">
                            <div class="col-md-6"><label class="form-label small mb-1">Sample A</label><audio controls preload="none" class="w-100" src="${escapeHtml(pair.A.audio_url)}"></audio></div>
                            <div class="col-md-6"><label class="form-label small mb-1">Sample B</label><audio controls preload="none" class="w-100" src="${escapeHtml(pair.B.audio_url)}"></audio></div>
                        </div>
                    </div>`).join('');
                panel.innerHTML = `
                    <div class="card border-primary">
                        <div class="card-header d-flex justify-content-between align-items-center">
                            <div><strong>Blind review</strong><br><small class="text-muted">Identities are hidden until you submit. This never changes production.</small></div>
                            <button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('lora-comparison-panel').style.display='none'" title="Close"><i class="fas fa-times"></i></button>
                        </div>
                        <div class="card-body">
                            ${pairsHtml}
                            <div class="mb-2">
                                <label class="form-label small mb-1">Which sounds better?</label>
                                <div class="d-flex gap-3">
                                    <div class="form-check"><input class="form-check-input" type="radio" name="blind-pref" id="blind-pref-a" value="A"><label class="form-check-label small" for="blind-pref-a">Sample A</label></div>
                                    <div class="form-check"><input class="form-check-input" type="radio" name="blind-pref" id="blind-pref-b" value="B"><label class="form-check-label small" for="blind-pref-b">Sample B</label></div>
                                    <div class="form-check"><input class="form-check-input" type="radio" name="blind-pref" id="blind-pref-tie" value="tie"><label class="form-check-label small" for="blind-pref-tie">No preference</label></div>
                                </div>
                            </div>
                            <div class="row g-2 mb-2">
                                <div class="col-md-4"><label class="form-label small mb-1">Rating (optional)</label>
                                    <select class="form-select form-select-sm" id="blind-rating"><option value="">—</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select></div>
                                <div class="col-md-8"><label class="form-label small mb-1">Notes (optional)</label>
                                    <input type="text" class="form-control form-control-sm" id="blind-notes" maxlength="1000" placeholder="What stood out?"></div>
                            </div>
                            <button id="btn-blind-submit" class="btn btn-sm btn-primary" onclick="submitLoraBlindReview()"><i class="fas fa-check me-1"></i>Submit decision</button>
                        </div>
                    </div>`;
                panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            } catch (e) {
                panel.innerHTML = `<div class="alert alert-danger py-2 mb-0">${escapeHtml(getActionErrorMessage("Blind review unavailable", e, "Check the selected adapter and review candidates, then reopen blind review."))}</div>`;
            }
        };

        window.submitLoraBlindReview = async () => {
            if (!_blindReview) { return; }
            const choice = _blindReviewSelectedChoice();
            if (!choice) { showToast('Pick Sample A, Sample B, or No preference.', 'warning'); return; }
            const ratingRaw = document.getElementById('blind-rating').value;
            const body = {
                choice: choice,
                rating: ratingRaw ? parseInt(ratingRaw, 10) : null,
                notes: document.getElementById('blind-notes').value || '',
            };
            // Disable on click so a double-click can't fire two submissions for
            // the same session; the backend also guards this, but not sending
            // the second request is cleaner. Re-enable only on error.
            const submitBtn = document.getElementById('btn-blind-submit');
            if (submitBtn) { submitBtn.disabled = true; }
            try {
                const result = await API.post(
                    `/api/lora/models/${encodeURIComponent(_blindReview.adapterId)}/review/session/${encodeURIComponent(_blindReview.sessionId)}`, body);
                renderBlindReviewResult(result);
            } catch (e) {
                if (submitBtn) { submitBtn.disabled = false; }
                showActionError("Could not record review", e, "Keep your rating and notes. Reopen review history to check whether the decision was saved before submitting again.");
            }
        };

        // Reveal identities AFTER submission. Human preference and the automated
        // recommendation are shown as separate, clearly labelled facts.
        function renderBlindReviewResult(result) {
            const panel = document.getElementById('lora-comparison-panel');
            const labels = result.revealed.labels || {};
            const role = result.revealed.choice_role;
            const yourPick = role === 'tie' ? 'No preference'
                : `the <strong>${escapeHtml(role)}</strong> checkpoint`;
            const automated = (result.automated && result.automated.recommended_candidate) || 'none';
            panel.innerHTML = `
                <div class="card border-success">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <strong>Review recorded</strong>
                        <button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('lora-comparison-panel').style.display='none'" title="Close"><i class="fas fa-times"></i></button>
                    </div>
                    <div class="card-body small">
                        <div class="mb-1">Sample A was <strong>${escapeHtml(labels.A || '?')}</strong>, Sample B was <strong>${escapeHtml(labels.B || '?')}</strong>.</div>
                        <div class="mb-1"><i class="fas fa-user me-1"></i>Your preference: ${yourPick}.</div>
                        <div class="text-muted"><i class="fas fa-robot me-1"></i>Automated recommendation (separate): ${escapeHtml(automated)}. Human feedback does not change production.</div>
                    </div>
                </div>`;
            _blindReview = null;
        }

        window.openLoraReviewHistory = async (adapterId) => {
            const panel = document.getElementById('lora-comparison-panel');
            panel.style.display = '';
            panel.innerHTML = '<div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i>Loading review history…</div>';
            try {
                const data = await API.get(`/api/lora/models/${encodeURIComponent(adapterId)}/reviews`);
                renderLoraReviewHistory(adapterId, data.reviews || []);
            } catch (e) {
                panel.innerHTML = `<div class="alert alert-danger py-2 mb-0">${escapeHtml(getActionErrorMessage("History unavailable", e, "Check the selected adapter, then reopen review history. An unavailable history is not an empty history."))}</div>`;
            }
        };

        function renderLoraReviewHistory(adapterId, reviews) {
            const panel = document.getElementById('lora-comparison-panel');
            const rows = reviews.map(r => {
                const when = r.created_at ? new Date(r.created_at).toLocaleString() : '';
                const human = r.human || {};
                const rating = human.rating ? ` · ${human.rating}/5` : '';
                const notes = human.notes ? ` · ${escapeHtml(human.notes)}` : '';
                const auto = (r.automated || {}).recommended_candidate || '—';
                return `<div class="border rounded p-2 mb-1 small">
                    <div><i class="fas fa-user me-1"></i>Preferred: <strong>${escapeHtml(human.choice_role || '?')}</strong>${rating}${notes}</div>
                    <div class="text-muted"><i class="fas fa-robot me-1"></i>Automated: ${escapeHtml(auto)} · ${escapeHtml(when)}${r.blind ? ' · blind' : ''}</div>
                </div>`;
            }).join('');
            panel.innerHTML = `
                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <strong>Review history (${reviews.length})</strong>
                        <span>
                            ${reviews.length ? `<button class="btn btn-sm btn-outline-danger me-1" data-adapter-id="${escapeHtml(adapterId)}" onclick="clearLoraReviewHistory(this.dataset.adapterId)"><i class="fas fa-trash me-1"></i>Clear</button>` : ''}
                            <button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('lora-comparison-panel').style.display='none'" title="Close"><i class="fas fa-times"></i></button>
                        </span>
                    </div>
                    <div class="card-body">${rows || '<div class="text-muted small">No human reviews recorded yet.</div>'}</div>
                </div>`;
        }

        window.clearLoraReviewHistory = async (adapterId) => {
            if (!await showConfirm('Delete all human review history for this adapter?', {title: 'Delete review history?', actionLabel: 'Delete history', danger: true})) { return; }
            try {
                const result = await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/reviews/cleanup`, {});
                const freedKb = (result.freed_bytes / 1024).toFixed(1);
                showToast(`Cleared ${result.removed_count} review(s), freed ${freedKb} KB.`, 'success');
                openLoraReviewHistory(adapterId);
            } catch (e) {
                showActionError("Could not clear history", e, "Reopen review history to check whether it was cleared before trying again.");
            }
        };

        window.promoteLoraCandidate = async (adapterId, candidateId) => {
            if (typeof candidateId !== 'string' || !candidateId.trim()) {
                showToast('Refresh the candidate list before promoting.', 'warning');
                return;
            }
            if (!await showConfirm(`Promote ${candidateId}? The current production checkpoint will be preserved for rollback.`, {title: 'Promote checkpoint?', actionLabel: 'Promote', danger: false})) {
                return;
            }
            try {
                await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/promote`, {expected_candidate_id: candidateId});
                showToast(`Promoted ${candidateId}. Production backup retained.`, 'success');
                await loadLoraModels();
            } catch (e) {
                showActionError("Promotion failed", e, "Refresh the adapter list and checkpoint history to check production and backup state before promoting again.");
            }
        };

        window.rollbackLoraPromotion = async (adapterId) => {
            if (!await showConfirm('Restore the production checkpoint saved before promotion?', {title: 'Restore production checkpoint?', actionLabel: 'Restore', danger: false})) {
                return;
            }
            try {
                await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/rollback-promotion`, {});
                showToast('Previous production checkpoint restored.', 'success');
                await loadLoraModels();
            } catch (e) {
                showActionError("Rollback failed", e, "Refresh the adapter list and checkpoint history to check production and backup state before rolling back again.");
            }
        };

        window.recoverLoraCheckpointSwap = async (adapterId) => {
            if (!await showConfirm('Recover production from the checkpoint saved before the interrupted operation?', {title: 'Recover production checkpoint?', actionLabel: 'Recover', danger: false})) {
                return;
            }
            try {
                await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/recover-checkpoint-swap`, {});
                showToast('Interrupted checkpoint operation recovered.', 'success');
                await loadLoraModels();
            } catch (e) {
                showActionError("Recovery failed", e, "Refresh the adapter list and checkpoint history before recovering again. Review any refusal about changed checkpoint files.");
            }
        };

        window.deleteLoraRollbackBackup = async (adapterId) => {
            if (!await showConfirm('Permanently delete this rollback backup? You will no longer be able to restore the pre-promotion checkpoint.', {title: 'Delete rollback backup?', actionLabel: 'Delete backup', danger: true})) {
                return;
            }
            try {
                const response = await fetch(`/api/lora/models/${encodeURIComponent(adapterId)}/rollback-backup`, { method: 'DELETE' });
                await API._handleError(response);
                showToast('Rollback backup deleted.', 'success');
                await loadLoraModels();
            } catch (e) {
                showActionError("Backup deletion failed", e, "Refresh checkpoint history and check whether the rollback backup was deleted before trying again.");
            }
        };

        window.playLoraPreview = async (adapterId) => {
            const btn = document.getElementById(`lora-preview-btn-${adapterId}`);
            const origHtml = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

            try {
                const result = await API.post(`/api/lora/preview/${encodeURIComponent(adapterId)}`, {});
                const audio = new Audio(`${result.audio_url}?t=${Date.now()}`);
                await audio.play();
                // Update button now that preview is cached
                btn.title = 'Play preview';
                btn.classList.replace('btn-outline-secondary', 'btn-outline-success');
            } catch (e) {
                showActionError("Preview failed", e, "Check that the adapter has preview audio and that the app connection is available, then play the preview again.");
            } finally {
                btn.disabled = false;
                btn.innerHTML = origHtml;
            }
        };

        let loraTestPending = false;
        window.testLoraModel = (adapterId) => {
            if (loraTestPending) { showToast('Wait for the current voice test to finish.', 'warning'); return; }
            document.getElementById('lora-test-adapter').value = adapterId;
            document.getElementById('lora-test-form').style.display = '';
            document.getElementById('lora-test-text').focus();
        };

        window.runLoraTest = async () => {
            if (loraTestPending) { return; }
            const adapterId = document.getElementById('lora-test-adapter').value;
            const text = document.getElementById('lora-test-text').value.trim();
            const instruct = document.getElementById('lora-test-instruct').value.trim();
            if (!adapterId) { showToast('Select an adapter.', 'warning'); return; }
            if (!text) { showToast('Enter text to synthesize.', 'warning'); return; }

            const statusEl = document.getElementById('lora-test-status');
            const button = document.getElementById('btn-lora-test-generate');
            const label = button.innerHTML;
            const controls = ['lora-test-adapter', 'lora-test-text', 'lora-test-instruct', 'btn-lora-test-generate']
                .map(id => { const field = document.getElementById(id); return {field, disabled: field.disabled}; });
            loraTestPending = true;
            controls.forEach(({field}) => { field.disabled = true; });
            button.textContent = 'Generating…';
            statusEl.textContent = `Generating test audio for ${adapterId}…`;
            document.getElementById('lora-test-audio').innerHTML = '';

            try {
                const result = await API.post('/api/lora/test', {
                    adapter_id: adapterId,
                    text: text,
                    instruct: instruct
                });

                statusEl.innerHTML = '';
                const audioDiv = document.getElementById('lora-test-audio');
                audioDiv.innerHTML = `<audio controls autoplay src="${escapeHtml(result.audio_url)}?t=${Date.now()}"></audio>`;
            } catch (e) {
                statusEl.innerHTML = `<span class="text-danger">${escapeHtml(getActionErrorMessage('Test audio did not finish', e, 'Check the selected adapter and test task status before generating again. Your test text is retained.'))}</span>`;
            } finally {
                loraTestPending = false;
                controls.forEach(({field, disabled}) => { field.disabled = disabled; });
                button.innerHTML = label;
            }
        };

        window.deleteLoraModel = async (adapterId) => {
            if (!await showConfirm('Delete this trained adapter? This cannot be undone.', {title: 'Delete trained adapter?', actionLabel: 'Delete adapter', danger: true})) { return; }
            try {
                const res = await fetch(`/api/lora/models/${encodeURIComponent(adapterId)}`, { method: 'DELETE' });
                await API._handleError(res);
                loadLoraModels();
            } catch (e) {
                showActionError("Error deleting adapter", e, "Refresh the adapter list and check whether the adapter was deleted before trying again.");
            }
        };

        window.downloadBuiltinAdapter = async (adapterId) => {
            const btn = document.getElementById(`lora-dl-btn-${adapterId}`);
            const origHtml = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Downloading...';

            try {
                await API.post(`/api/lora/download/${encodeURIComponent(adapterId)}`, {});
                showToast('Adapter downloaded successfully.', 'success');
                loadLoraModels();
            } catch (e) {
                showActionError("Download failed", e, "Refresh the adapter list and check whether the download completed before downloading again.");
            } finally {
                btn.disabled = false;
                btn.innerHTML = origHtml;
            }
        };
