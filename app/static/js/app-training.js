        let loraDatasetsRequest = 0;
        let loraModelsRequest = 0;
        function trainingSamplesText(count) {
            return count === 1
                ? t('training.dataset.samples.one', '{count} sample', {count})
                : t('training.dataset.samples.many', '{count} samples', {count});
        }
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
                selectEl.innerHTML = `<option value="">${escapeHtml(t('training.dataset.select-empty', '-- Select dataset --'))}</option>` +
                    datasets.map(d => `<option value="${escapeHtml(d.dataset_id)}">${escapeHtml(d.dataset_id)} (${escapeHtml(trainingSamplesText(d.sample_count))})</option>`).join('');
                if (currentVal) { selectEl.value = currentVal; }

                // Update list
                if (!datasets.length) {
                    listEl.innerHTML = `<span class="text-muted">${escapeHtml(t('training.dataset.empty', 'No datasets uploaded yet.'))}</span>`;
                    return datasets;
                }
                listEl.innerHTML = datasets.map(d => `
                    <div class="d-flex justify-content-between align-items-center py-1">
                        <span><strong>${escapeHtml(d.dataset_id)}</strong> <small class="text-muted">(${escapeHtml(trainingSamplesText(d.sample_count))})</small></span>
                        <button class="btn btn-sm btn-outline-danger" data-dataset-id="${escapeHtml(d.dataset_id)}" onclick="deleteLoraDataset(this.dataset.datasetId)" aria-label="${escapeHtml(t('training.dataset.delete-aria', 'Delete dataset {name}', {name: d.dataset_id}))}" title="${escapeHtml(t('training.dataset.delete-aria', 'Delete dataset {name}', {name: d.dataset_id}))}"><i class="fas fa-trash"></i></button>
                    </div>
                `).join('');
                return datasets;
            } catch (e) {
                console.error('Failed to load LoRA datasets:', e);
                if (request === loraDatasetsRequest) {
                    status.textContent = t('training.dataset.load-failed', 'Could not load datasets. Previously loaded datasets and your selection are kept. Check that Alexandria is running, then retry the list refresh.');
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
            if (!fileInput.files.length) { showToast(t('training.dataset.upload.no-file', 'Select a ZIP file first.'), 'warning'); return; }

            const file = fileInput.files[0];
            if (!file.name.toLowerCase().endsWith('.zip')) { showToast(t('training.dataset.upload.not-zip', 'File must be a .zip archive.'), 'warning'); return; }

            const formData = new FormData();
            formData.append('file', file);
            const button = document.getElementById('btn-lora-upload');
            const label = button.innerHTML;
            const status = document.getElementById('lora-upload-status');
            const select = document.getElementById('lora-dataset-select');
            const selected = select.value;
            loraDatasetUploadPending = true;
            button.disabled = true;
            button.textContent = t('training.dataset.upload.uploading-button', 'Uploading…');
            status.textContent = t('training.dataset.upload.uploading', 'Uploading {name}…', {name: file.name});

            try {
                const res = await fetch('/api/lora/upload_dataset', { method: 'POST', body: formData });
                if (!res.ok) {
                    await API._handleError(res);
                }
                const result = await res.json();
                showToast(t('training.dataset.upload.done', 'Dataset "{name}" uploaded ({samples}).', {name: result.dataset_id, samples: trainingSamplesText(result.sample_count)}), 'success');
                if (fileInput.files[0] === file) { fileInput.value = ''; }
                status.textContent = t('training.dataset.upload.refreshing', 'Dataset "{name}" uploaded. Refreshing the dataset list…', {name: result.dataset_id});
                const datasets = await loadLoraDatasets();
                if (Array.isArray(datasets) && datasets.some(dataset => dataset.dataset_id === result.dataset_id) && select.value === selected) {
                    select.value = result.dataset_id;
                    status.textContent = t('training.dataset.upload.selected', 'Dataset "{name}" uploaded and selected for training.', {name: result.dataset_id});
                } else {
                    status.textContent = t('training.dataset.upload.unselected', 'Dataset "{name}" uploaded. Review the dataset list and select it when you are ready to train.', {name: result.dataset_id});
                }
            } catch (e) {
                const refused = Number.isInteger(e.status);
                status.textContent = refused
                    ? t('training.dataset.upload.refused', 'Upload refused (HTTP {status}). Keep the ZIP and review the archive validation details before trying again.', {status: String(e.status)})
                    : t('training.dataset.upload.unconfirmed', 'Upload was not confirmed. Keep the ZIP and check the dataset list before uploading again.');
                showActionError(refused ? 'Dataset upload refused' : 'Upload error', e,
                    'Keep the ZIP file. Check the dataset list before uploading again, and review any archive validation details.');
            } finally {
                loraDatasetUploadPending = false;
                button.disabled = false;
                button.innerHTML = label;
            }
        };

        window.deleteLoraDataset = async (datasetId) => {
            if (!await showConfirm(formatUiSafetyText('safety.training.dataset.delete', 'Delete dataset "{name}"? The uploaded ZIP and its samples are removed. This cannot be undone.', {name: datasetId}), {title: t('training.dataset.delete.title', 'Delete training dataset?'), actionLabel: t('training.dataset.delete.action', 'Delete dataset'), danger: true})) { return; }
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
            if (!name) { showToast(t('training.train.no-name', 'Enter an adapter name.'), 'warning'); return; }
            if (!datasetId) { showToast(t('training.train.no-dataset', 'Select a dataset.'), 'warning'); return; }

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
            document.getElementById('lora-train-status').innerHTML = `<i class="fas fa-spinner fa-spin me-1"></i>${escapeHtml(t('training.train.starting', 'Starting...'))}`;

            try {
                const result = await API.post('/api/lora/train', request);
                document.getElementById('btn-lora-cancel').style.display = 'inline-block';
                document.getElementById('lora-progress-section').style.display = 'block';
                document.getElementById('lora-train-status').innerHTML = `<span class="text-info">${escapeHtml(t('training.train.in-progress', 'Training in progress...'))}</span>`;
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
            progressBar.ariaValueText = Number.isFinite(maxEpoch) && maxEpoch > 0
                ? t('training.progress.epoch-of', 'Epoch {epoch} of {epochs}', {epoch, epochs: maxEpoch}) : t('training.progress.unreported', 'Epoch progress has not been reported yet.');
        }

        function applyLoraProgressState(progressBar, outcome) {
            progressBar.classList.remove('bg-info', 'bg-success', 'bg-danger', 'bg-warning');
            const color = {running: 'bg-info', finished: 'bg-success', failed: 'bg-danger', stopped: 'bg-warning'}[outcome];
            progressBar.classList.add(color);
            if (outcome === 'running') { progressBar.classList.add('progress-bar-animated'); }
            else { progressBar.classList.remove('progress-bar-animated'); }
        }

        function pollLoraTraining(totalEpochs = null) {
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
                        document.getElementById('lora-train-status').innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>${escapeHtml(t('training.train.complete', 'Training complete!'))}</span>`;
                        applyLoraProgress(progressBar, 100, maxEpochs, maxEpochs);
                        loadLoraModels();
                    } else if (outcome === 'failed') {
                        document.getElementById('lora-train-status').innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(t('training.train.failed', 'Training failed'))}</span>`;
                    } else {
                        document.getElementById('lora-train-status').innerHTML = `<span class="text-warning">${escapeHtml(t('training.train.stopped', 'Training stopped'))}</span>`;
                    }
                }
            });
        }

        window.cancelLoraTraining = async () => {
            const btn = document.getElementById('btn-lora-cancel');
            btn.disabled = true;
            try {
                await API.post('/api/lora/train/cancel', {});
                document.getElementById('lora-train-status').innerHTML = `<span class="text-warning">${escapeHtml(t('training.train.cancelling', 'Cancellation requested…'))}</span>`;
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
                    star.title = result.favorite
                        ? t('training.adapter.favorite-on', 'Favorite — voice suggestions prefer it when compatible')
                        : t('training.adapter.favorite-off', 'Mark as favorite');
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
                        return { backups: [], unavailable: true };
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
                status.textContent = backupStatus.unavailable ? t('training.adapter.backups-unavailable', 'Adapters loaded; rollback backup and disk-space status are unavailable. Retry the list refresh to check them.') : '';
                retry.hidden = !backupStatus.unavailable;
                window._loraModelsCache = models;
                const container = document.getElementById('lora-models-list');
                const testForm = document.getElementById('lora-test-form');

                if (!models.length) {
                    container.innerHTML = `<p class="text-muted mb-0">${escapeHtml(t('training.adapter.list-empty', 'No adapters available.'))}</p>`;
                    testForm.style.display = 'none';
                    return;
                }

                const backupSummary = backupStatus.backups.length || backupStatus.low_space_warning ? `
                    <div class="alert ${backupStatus.low_space_warning ? 'alert-warning' : 'alert-secondary'} py-2 mb-2 small">
                        ${backupStatus.low_space_warning ? `<strong>${escapeHtml(t('training.adapter.low-space', 'Low disk space.'))}</strong> ` : ''}
                        ${escapeHtml(t('training.adapter.backups-summary', 'Rollback backups: {count}, {size} GB; {free} GB free.', {count: backupStatus.backups.length, size: (backupStatus.total_size_bytes / 1024 ** 3).toFixed(2), free: (backupStatus.free_bytes / 1024 ** 3).toFixed(1)}))}
                    </div>` : '';
                const renderCandidateSummary = model => {
                    const summary = model.candidate_summary;
                    if (!summary || summary.state === 'no_candidates') {
                        return '';
                    }
                    const labels = {
                        awaiting_evaluation: t('training.candidate.state.awaiting', 'awaiting evaluation'),
                        candidate_recommended: t('training.candidate.state.candidate', 'candidate recommended'),
                        production_recommended: t('training.candidate.state.production', 'production recommended'),
                        promoted: t('training.candidate.state.promoted', 'candidate promoted'),
                        rolled_back: t('training.candidate.state.rolled-back', 'promotion rolled back'),
                    };
                    const counts = [];
                    if (summary.evaluated_count) {
                        counts.push(t('training.candidate.evaluated', '{count} evaluated', {count: summary.evaluated_count}));
                    }
                    if (summary.retained_count) {
                        counts.push(t('training.candidate.retained', '{count} retained', {count: summary.retained_count}));
                    }
                    if (summary.duplicate_count) {
                        counts.push(t('training.candidate.duplicates', '{count} duplicate skipped', {count: summary.duplicate_count}));
                    }
                    const unchanged = summary.production_unchanged
                        ? `<span class="me-2">${escapeHtml(t('training.candidate.production-unchanged', 'production unchanged'))}</span>` : '';
                    return `<div class="text-muted small mt-1"><span class="me-2">${escapeHtml(labels[summary.state] || summary.state)}</span>${counts.map(item => `<span class="me-2">${escapeHtml(item)}</span>`).join('')}${unchanged}</div>`;
                };
                // Human review tally, kept visually distinct from the automated
                // recommendation above (the "human" icon vs the evaluation badge).
                const renderReviewSummary = model => {
                    const r = model.review_summary;
                    if (!r || !r.count) { return ''; }
                    const parts = [];
                    if (r.preferred_candidate) { parts.push(t('training.review.prefer-candidate', '{count} prefer candidate', {count: r.preferred_candidate})); }
                    if (r.preferred_production) { parts.push(t('training.review.prefer-production', '{count} prefer production', {count: r.preferred_production})); }
                    if (r.tie) { parts.push(t('training.review.no-preference', '{count} no preference', {count: r.tie})); }
                    const total = r.count === 1
                        ? t('training.review.count.one', '{count} human review', {count: r.count})
                        : t('training.review.count.many', '{count} human reviews', {count: r.count});
                    return `<div class="text-muted small mt-1"><i class="fas fa-user me-1"></i><span class="me-2">${escapeHtml(total)}</span>${parts.map(item => `<span class="me-2">${escapeHtml(item)}</span>`).join('')}</div>`;
                };
                const renderEvaluationBadge = model => {
                    const evaluation = model.evaluation;
                    if (!evaluation) { return '--'; }
                    if (evaluation.status === 'failed') {
                        return `<span class="badge bg-danger" title="${escapeHtml([model.candidate_summary?.evidence_reason, ...(evaluation.warnings || [])].filter(Boolean).join('; '))}">${escapeHtml(evaluation.status)}</span>`;
                    }
                    const status = evaluation.status || t('training.adapter.unknown', 'unknown');
                    const badge = `<span class="badge bg-secondary" title="${escapeHtml([model.candidate_summary?.evidence_reason, ...(evaluation.warnings || [])].filter(Boolean).join('; '))}">${escapeHtml(t('training.adapter.historical', 'historical · {status}', {status}))}</span>`;
                    if (!evaluation.recommended_candidate || evaluation.recommended_candidate === 'production') {
                        return badge;
                    }
                    return `${badge} <small title="${escapeHtml(t('training.adapter.production-unchanged-title', 'Production remains unchanged'))}">${escapeHtml(t('training.adapter.recommend', 'recommend {candidate}', {candidate: evaluation.recommended_candidate}))}</small>`;
                };
                container.innerHTML = `${backupSummary}
                    <div class="table-responsive"><table class="table table-sm table-hover mb-0">
                        <thead><tr><th>${escapeHtml(t('training.table.name', 'Name'))}</th><th>${escapeHtml(t('training.table.dataset', 'Dataset'))}</th><th>${escapeHtml(t('training.table.epochs', 'Epochs'))}</th><th>${escapeHtml(t('training.table.final-loss', 'Final Loss'))}</th><th>${escapeHtml(t('training.table.evaluation', 'Evaluation'))}</th><th>${escapeHtml(t('training.table.samples', 'Samples'))}</th><th style="width:240px">${escapeHtml(t('training.table.actions', 'Actions'))}</th></tr></thead>
                        <tbody>
                            ${models.map(m => `
                                <tr${m.builtin ? ' class="table-light"' : ''}>
                                    <td><button class="btn btn-sm btn-link p-0 me-1 ${m.favorite ? 'text-warning' : 'text-muted'}" data-adapter-id="${escapeHtml(m.id)}" data-lora-favorite onclick="onToggleFavoriteAdapter(this.dataset.adapterId, this)" title="${escapeHtml(m.favorite ? t('training.adapter.favorite-on', 'Favorite — voice suggestions prefer it when compatible') : t('training.adapter.favorite-off', 'Mark as favorite'))}"><i class="${m.favorite ? 'fas' : 'far'} fa-star"></i></button><strong>${escapeHtml(m.name)}</strong>${m.builtin ? ` <span class="badge bg-secondary">${escapeHtml(t('training.adapter.builtin', 'built-in'))}</span>${m.downloaded === false ? ` <span class="badge bg-warning text-dark">${escapeHtml(t('training.adapter.not-downloaded', 'not downloaded'))}</span>` : ''}` : ''}</td>
                                    <td>${escapeHtml(m.dataset_id || (m.builtin ? '--' : '--'))}</td>
                                    <td>${m.epochs || '--'}</td>
                                    <td>${m.final_loss != null ? m.final_loss.toFixed(4) : '--'}</td>
                                    <td>${m.checkpoint_swap ? `<span class="badge bg-danger">${escapeHtml(t('training.adapter.recovery-required', 'recovery required'))}</span>` : renderEvaluationBadge(m)}${renderCandidateSummary(m)}${renderReviewSummary(m)}</td>
                                    <td title="${escapeHtml(t('training.table.samples-title', 'Dataset quantity only; not a voice-quality score'))}">${m.sample_count || '--'}</td>
                                    <td>
                                        ${m.builtin && m.downloaded === false ? `
                                            <button class="btn btn-sm btn-outline-warning" id="lora-dl-btn-${escapeHtml(m.id)}" data-adapter-id="${escapeHtml(m.id)}" onclick="downloadBuiltinAdapter(this.dataset.adapterId)" title="${escapeHtml(t('training.adapter.download-title', 'Download from HuggingFace'))}"><i class="fas fa-download me-1"></i>${escapeHtml(t('training.adapter.download', 'Download'))}</button>
                                        ` : `
                                            <button class="btn btn-sm ${m.preview_audio_url ? 'btn-outline-success' : 'btn-outline-secondary'} me-1" id="lora-preview-btn-${escapeHtml(m.id)}" data-adapter-id="${escapeHtml(m.id)}" onclick="playLoraPreview(this.dataset.adapterId)" title="${escapeHtml(m.preview_audio_url ? t('training.adapter.preview-play-title', 'Play preview') : t('training.adapter.preview-generate-title', 'Generate and play preview (first time may take a moment)'))}"><i class="fas fa-volume-up"></i></button>
                                            <button class="btn btn-sm btn-outline-primary me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="testLoraModel(this.dataset.adapterId)" title="${escapeHtml(t('training.adapter.test-title', 'Generate test line with custom text'))}"><i class="fas fa-flask me-1"></i>${escapeHtml(t('training.adapter.test', 'Test'))}</button>
                                            ${!m.builtin && m.checkpoint_swap ? `<button class="btn btn-sm btn-danger me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="recoverLoraCheckpointSwap(this.dataset.adapterId)" title="${escapeHtml(t('training.adapter.recover-title', 'Restore production from the interrupted operation journal'))}"><i class="fas fa-life-ring me-1"></i>${escapeHtml(t('training.adapter.recover', 'Recover'))}</button>` : ''}
                                            ${!m.builtin && !m.checkpoint_swap && m.evaluation?.recommended_candidate && m.evaluation.recommended_candidate !== 'production' ? `<button class="btn btn-sm btn-outline-info me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="openLoraCandidateComparison(this.dataset.adapterId)" title="${escapeHtml(t('training.adapter.compare-title', 'Listen to matched production and candidate evaluation probes'))}"><i class="fas fa-headphones me-1"></i>${escapeHtml(t('training.adapter.compare', 'Compare'))}</button><button class="btn btn-sm btn-outline-primary me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="openLoraBlindReview(this.dataset.adapterId)" title="${escapeHtml(t('training.adapter.blind-title', 'Blind A/B listening review — identities hidden until you submit; never promotes'))}"><i class="fas fa-user-secret me-1"></i>${escapeHtml(t('training.adapter.blind', 'Blind review'))}</button><button class="btn btn-sm btn-outline-secondary me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="openLoraReviewHistory(this.dataset.adapterId)" title="${escapeHtml(t('training.adapter.history-title', 'Human review history for this adapter'))}"><i class="fas fa-clock-rotate-left me-1"></i>${escapeHtml(t('training.adapter.history', 'History'))}</button><button class="btn btn-sm btn-outline-success me-1" data-adapter-id="${escapeHtml(m.id)}" data-candidate-id="${escapeHtml(m.evaluation.recommended_candidate)}" ${m.candidate_summary?.evidence_state === 'mode_matched' ? '' : 'disabled'} onclick="promoteLoraCandidate(this.dataset.adapterId, this.dataset.candidateId)" title="${escapeHtml(m.candidate_summary?.evidence_state === 'mode_matched' ? t('training.adapter.promote-title-ready', 'Preserve production, then promote this evaluated candidate') : t('training.adapter.promote-title-blocked', 'Generate a LoRA test line in this app, then Refresh. Re-evaluate only if the observed mode or settings differ'))}"><i class="fas fa-arrow-up me-1"></i>${escapeHtml(t('training.adapter.promote', 'Promote'))}</button>` : ''}
                                            ${!m.builtin && !m.checkpoint_swap && m.promotion?.status === 'promoted' ? `<button class="btn btn-sm btn-outline-warning me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="rollbackLoraPromotion(this.dataset.adapterId)" title="${escapeHtml(t('training.adapter.rollback-title', 'Restore the production checkpoint saved before promotion'))}"><i class="fas fa-undo me-1"></i>${escapeHtml(t('training.adapter.rollback', 'Rollback'))}</button>` : ''}
                                            ${!m.builtin && !m.checkpoint_swap && m.rollback_backup ? `<button class="btn btn-sm btn-outline-danger me-1" data-adapter-id="${escapeHtml(m.id)}" onclick="deleteLoraRollbackBackup(this.dataset.adapterId)" title="${escapeHtml(t('training.adapter.delete-backup-title', 'Delete {size} MB rollback backup', {size: (m.rollback_backup.size_bytes / 1024 ** 2).toFixed(1)}))}"><i class="fas fa-hard-drive me-1"></i>${escapeHtml(t('training.adapter.delete-backup', 'Delete backup'))}</button>` : ''}
                                            ${m.builtin ? '' : `<button class="btn btn-sm btn-outline-danger" data-adapter-id="${escapeHtml(m.id)}" onclick="deleteLoraModel(this.dataset.adapterId)" title="${escapeHtml(t('training.adapter.delete-title', 'Delete'))}"><i class="fas fa-trash"></i></button>`}
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
                    status.textContent = t('training.adapter.load-failed', 'Could not load adapters. Previously loaded adapters and your selection are kept. Check that Alexandria is running, then retry the list refresh.');
                    retry.hidden = false;
                }
            } finally {
                if (request === loraModelsRequest) { retry.disabled = false; }
            }
        }

        function beginLoraPanelView(panel) {
            const request = {};
            panel._comparisonRequest = request;
            return () => panel._comparisonRequest === request
                && document.getElementById('lora-comparison-panel') === panel;
        }

        window.openLoraCandidateComparison = async (adapterId) => {
            const panel = document.getElementById('lora-comparison-panel');
            const isCurrent = beginLoraPanelView(panel);
            panel.style.display = '';
            panel.innerHTML = `<div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i>${escapeHtml(t('training.compare.loading', 'Loading comparison…'))}</div>`;
            try {
                const comparison = await API.get(`/api/lora/models/${encodeURIComponent(adapterId)}/comparison`);
                if (!isCurrent()) { return; }
                const renderMetrics = probe => {
                    const metrics = probe.metrics || {};
                    const values = [
                        [t('training.compare.metric.speaker', 'speaker similarity'), metrics.speaker_similarity],
                        [t('training.compare.metric.clipping', 'clipping'), metrics.clipping_ratio],
                        [t('training.compare.metric.silence', 'silence'), metrics.silence_ratio],
                    ].filter(item => item[1] != null);
                    return values.length
                        ? values.map(item => `<span class="me-2">${escapeHtml(item[0])} ${escapeHtml(String(Number(item[1]).toFixed(3)))}</span>`).join('')
                        : escapeHtml(t('training.compare.metrics-none', 'No metrics recorded'));
                };
                panel.innerHTML = `
                    <div class="card border-info">
                        <div class="card-header d-flex justify-content-between align-items-center">
                            <div><strong>${escapeHtml(t('training.compare.title', 'Candidate comparison: {candidate}', {candidate: comparison.candidate_id}))}</strong><br><small class="text-muted">${escapeHtml(t('training.compare.advisory', 'Advisory only — listening does not change production.'))}</small><div class="small text-muted">${escapeHtml(comparison.evidence_state === 'current' ? t('training.compare.state-current', 'Current observed serving mode') : t('training.compare.state-historical', 'Historical evaluation — audition only'))}: ${escapeHtml(comparison.evidence_reason || t('training.compare.serving-unobserved', 'Serving mode has not been observed.'))}</div></div>
                            <button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('lora-comparison-panel').style.display='none'" title="${escapeHtml(t('training.panel.close', 'Close'))}"><i class="fas fa-times"></i></button>
                        </div>
                        <div class="card-body">
                            ${comparison.reason ? `<p class="small mb-3">${escapeHtml(comparison.reason)}</p>` : ''}
                            ${comparison.probe_pairs.map(pair => `
                                <div class="border rounded p-2 mb-2">
                                    <div class="small mb-2"><strong>${escapeHtml(pair.id)}</strong> ${escapeHtml(t('training.compare.seed', 'seed {seed}', {seed: String(pair.seed)}))}<br>${escapeHtml(pair.text)}</div>
                                    <div class="row g-2">
                                        <div class="col-md-6"><label class="form-label small mb-1">${escapeHtml(t('training.compare.production', 'Production'))}</label><audio controls preload="none" class="w-100" src="${escapeHtml(pair.production.audio_url)}"></audio><div class="text-muted small">${renderMetrics(pair.production)}</div></div>
                                        <div class="col-md-6"><label class="form-label small mb-1">${escapeHtml(t('training.compare.candidate', 'Candidate'))}</label><audio controls preload="none" class="w-100" src="${escapeHtml(pair.candidate.audio_url)}"></audio><div class="text-muted small">${renderMetrics(pair.candidate)}</div></div>
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
            const isCurrent = beginLoraPanelView(panel);
            panel.style.display = '';
            panel.innerHTML = `<div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i>${escapeHtml(t('training.blind.loading', 'Opening blind review…'))}</div>`;
            try {
                const session = await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/review/session`, {});
                if (!isCurrent()) { return; }
                _blindReview = { adapterId: adapterId, sessionId: session.session_id, isCurrent };
                const pairsHtml = session.pairs.map(pair => `
                    <div class="border rounded p-2 mb-2">
                        <div class="small mb-2"><strong>${escapeHtml(pair.id)}</strong><br>${escapeHtml(pair.text)}</div>
                        <div class="row g-2">
                            <div class="col-md-6"><label class="form-label small mb-1">${escapeHtml(t('training.blind.sample-a', 'Sample A'))}</label><audio controls preload="none" class="w-100" src="${escapeHtml(pair.A.audio_url)}"></audio></div>
                            <div class="col-md-6"><label class="form-label small mb-1">${escapeHtml(t('training.blind.sample-b', 'Sample B'))}</label><audio controls preload="none" class="w-100" src="${escapeHtml(pair.B.audio_url)}"></audio></div>
                        </div>
                    </div>`).join('');
                panel.innerHTML = `
                    <div class="card border-primary">
                        <div class="card-header d-flex justify-content-between align-items-center">
                            <div><strong>${escapeHtml(t('training.blind.title', 'Blind review'))}</strong><br><small class="text-muted">${escapeHtml(t('training.blind.intro', 'Identities are hidden until you submit. This never changes production.'))}</small></div>
                            <button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('lora-comparison-panel').style.display='none'" title="${escapeHtml(t('training.panel.close', 'Close'))}"><i class="fas fa-times"></i></button>
                        </div>
                        <div class="card-body">
                            ${pairsHtml}
                            <div class="mb-2">
                                <label class="form-label small mb-1">${escapeHtml(t('training.blind.question', 'Which sounds better?'))}</label>
                                <div class="d-flex gap-3">
                                    <div class="form-check"><input class="form-check-input" type="radio" name="blind-pref" id="blind-pref-a" value="A"><label class="form-check-label small" for="blind-pref-a">${escapeHtml(t('training.blind.sample-a', 'Sample A'))}</label></div>
                                    <div class="form-check"><input class="form-check-input" type="radio" name="blind-pref" id="blind-pref-b" value="B"><label class="form-check-label small" for="blind-pref-b">${escapeHtml(t('training.blind.sample-b', 'Sample B'))}</label></div>
                                    <div class="form-check"><input class="form-check-input" type="radio" name="blind-pref" id="blind-pref-tie" value="tie"><label class="form-check-label small" for="blind-pref-tie">${escapeHtml(t('training.blind.no-preference', 'No preference'))}</label></div>
                                </div>
                            </div>
                            <div class="row g-2 mb-2">
                                <div class="col-md-4"><label class="form-label small mb-1">${escapeHtml(t('training.blind.rating', 'Rating (optional)'))}</label>
                                    <select class="form-select form-select-sm" id="blind-rating"><option value="">—</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option></select></div>
                                <div class="col-md-8"><label class="form-label small mb-1">${escapeHtml(t('training.blind.notes', 'Notes (optional)'))}</label>
                                    <input type="text" class="form-control form-control-sm" id="blind-notes" maxlength="1000" placeholder="${escapeHtml(t('training.blind.notes-placeholder', 'What stood out?'))}"></div>
                            </div>
                            <button id="btn-blind-submit" class="btn btn-sm btn-primary" onclick="submitLoraBlindReview()"><i class="fas fa-check me-1"></i>${escapeHtml(t('training.blind.submit', 'Submit decision'))}</button>
                        </div>
                    </div>`;
                panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            } catch (e) {
                if (!isCurrent()) { return; }
                panel.innerHTML = `<div class="alert alert-danger py-2 mb-0">${escapeHtml(getActionErrorMessage("Blind review unavailable", e, "Check the selected adapter and review candidates, then reopen blind review."))}</div>`;
            }
        };

        window.submitLoraBlindReview = async () => {
            if (!_blindReview || !_blindReview.isCurrent()) { return; }
            const review = _blindReview;
            const choice = _blindReviewSelectedChoice();
            if (!choice) { showToast(t('training.blind.pick', 'Pick Sample A, Sample B, or No preference.'), 'warning'); return; }
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
                    `/api/lora/models/${encodeURIComponent(review.adapterId)}/review/session/${encodeURIComponent(review.sessionId)}`, body);
                if (!review.isCurrent()) { return; }
                renderBlindReviewResult(result);
            } catch (e) {
                if (!review.isCurrent()) { return; }
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
            const yourPick = role === 'tie'
                ? t('training.blind.no-preference', 'No preference')
                : t('training.blind.pick-role', 'the {role} checkpoint', {role});
            const automated = (result.automated && result.automated.recommended_candidate) || t('training.blind.none', 'none');
            panel.innerHTML = `
                <div class="card border-success">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <strong>${escapeHtml(t('training.blind.recorded', 'Review recorded'))}</strong>
                        <button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('lora-comparison-panel').style.display='none'" title="${escapeHtml(t('training.panel.close', 'Close'))}"><i class="fas fa-times"></i></button>
                    </div>
                    <div class="card-body small">
                        <div class="mb-1">${escapeHtml(t('training.blind.labels', 'Sample A was {a}, Sample B was {b}.', {a: labels.A || '?', b: labels.B || '?'}))}</div>
                        <div class="mb-1"><i class="fas fa-user me-1"></i>${escapeHtml(t('training.blind.your-pick', 'Your preference: {pick}.', {pick: yourPick}))}</div>
                        <div class="text-muted"><i class="fas fa-robot me-1"></i>${escapeHtml(t('training.blind.automated', 'Automated recommendation (separate): {candidate}. Human feedback does not change production.', {candidate: automated}))}</div>
                    </div>
                </div>`;
            _blindReview = null;
        }

        window.openLoraReviewHistory = async (adapterId) => {
            const panel = document.getElementById('lora-comparison-panel');
            const isCurrent = beginLoraPanelView(panel);
            panel.style.display = '';
            panel.innerHTML = `<div class="text-muted small"><i class="fas fa-spinner fa-spin me-1"></i>${escapeHtml(t('training.history.loading', 'Loading review history…'))}</div>`;
            try {
                const data = await API.get(`/api/lora/models/${encodeURIComponent(adapterId)}/reviews`);
                if (!isCurrent()) { return; }
                renderLoraReviewHistory(adapterId, data.reviews || []);
            } catch (e) {
                if (!isCurrent()) { return; }
                panel.innerHTML = `<div class="alert alert-danger py-2 mb-0">${escapeHtml(getActionErrorMessage("History unavailable", e, "Check the selected adapter, then reopen review history. An unavailable history is not an empty history."))}</div>`;
            }
        };

        function renderLoraReviewHistory(adapterId, reviews) {
            const panel = document.getElementById('lora-comparison-panel');
            const rows = reviews.map(r => {
                const when = r.created_at ? formatUiDate(new Date(r.created_at)) : '';
                const human = r.human || {};
                const rating = human.rating ? ` ${escapeHtml(t('training.history.rating', 'Rating {rating} of 5', {rating: human.rating}))}` : '';
                const notes = human.notes ? ` ${escapeHtml(human.notes)}` : '';
                const auto = (r.automated || {}).recommended_candidate || '—';
                return `<div class="border rounded p-2 mb-1 small">
                    <div><i class="fas fa-user me-1"></i>${escapeHtml(t('training.history.preferred', 'Preferred: {role}', {role: human.choice_role || '?'}))}${rating}${notes}</div>
                    <div class="text-muted"><i class="fas fa-robot me-1"></i>${escapeHtml(t('training.history.automated', 'Automated: {candidate}', {candidate: auto}))} ${escapeHtml(when)}${r.blind ? ` ${escapeHtml(t('training.history.blind', 'blind'))}` : ''}</div>
                </div>`;
            }).join('');
            panel.innerHTML = `
                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <strong>${escapeHtml(t('training.history.title', 'Review history ({count})', {count: reviews.length}))}</strong>
                        <span>
                            ${reviews.length ? `<button class="btn btn-sm btn-outline-danger me-1" data-adapter-id="${escapeHtml(adapterId)}" onclick="clearLoraReviewHistory(this.dataset.adapterId)"><i class="fas fa-trash me-1"></i>${escapeHtml(t('training.history.clear', 'Clear'))}</button>` : ''}
                            <button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('lora-comparison-panel').style.display='none'" title="${escapeHtml(t('training.panel.close', 'Close'))}"><i class="fas fa-times"></i></button>
                        </span>
                    </div>
                    <div class="card-body">${rows || `<div class="text-muted small">${escapeHtml(t('training.history.empty', 'No human reviews recorded yet.'))}</div>`}</div>
                </div>`;
        }

        window.clearLoraReviewHistory = async (adapterId) => {
            const panel = document.getElementById('lora-comparison-panel');
            const request = panel._comparisonRequest;
            const isCurrent = () => panel._comparisonRequest === request
                && document.getElementById('lora-comparison-panel') === panel;
            if (!await showConfirm(formatUiSafetyText('safety.training.review.clear', 'Delete all human review history for this adapter? The decisions and notes are removed and cannot be recovered.', {}), {title: t('training.history.clear-title', 'Delete review history?'), actionLabel: t('training.history.clear-action', 'Delete history'), danger: true})) { return; }
            if (!isCurrent()) { return; }
            try {
                const result = await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/reviews/cleanup`, {});
                if (!isCurrent()) { return; }
                const freedKb = (result.freed_bytes / 1024).toFixed(1);
                showToast(t('training.history.cleared', 'Cleared {count} review(s), freed {size} KB.', {count: result.removed_count, size: freedKb}), 'success');
                openLoraReviewHistory(adapterId);
            } catch (e) {
                showActionError("Could not clear history", e, "Reopen review history to check whether it was cleared before trying again.");
            }
        };

        window.promoteLoraCandidate = async (adapterId, candidateId) => {
            if (typeof candidateId !== 'string' || !candidateId.trim()) {
                showToast(t('training.promote.refresh-first', 'Refresh the candidate list before promoting.'), 'warning');
                return;
            }
            if (!await showConfirm(formatUiSafetyText('safety.training.promote', 'Promote {candidate}? The current production checkpoint will be preserved for rollback.', {candidate: candidateId}), {title: t('training.promote.title', 'Promote checkpoint?'), actionLabel: t('training.promote.action', 'Promote'), danger: false})) {
                return;
            }
            try {
                await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/promote`, {expected_candidate_id: candidateId});
                showToast(t('training.promote.done', 'Promoted {candidate}. Production backup retained.', {candidate: candidateId}), 'success');
                await loadLoraModels();
            } catch (e) {
                showActionError("Promotion failed", e, "Refresh the adapter list and checkpoint history to check production and backup state before promoting again.");
            }
        };

        window.rollbackLoraPromotion = async (adapterId) => {
            if (!await showConfirm(formatUiSafetyText('safety.training.rollback', 'Restore the production checkpoint saved before promotion? The promoted candidate stops being production.', {}), {title: t('training.rollback.title', 'Restore production checkpoint?'), actionLabel: t('training.rollback.action', 'Restore'), danger: false})) {
                return;
            }
            try {
                await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/rollback-promotion`, {});
                showToast(t('training.rollback.done', 'Previous production checkpoint restored.'), 'success');
                await loadLoraModels();
            } catch (e) {
                showActionError("Rollback failed", e, "Refresh the adapter list and checkpoint history to check production and backup state before rolling back again.");
            }
        };

        window.recoverLoraCheckpointSwap = async (adapterId) => {
            if (!await showConfirm(formatUiSafetyText('safety.training.recover', 'Recover production from the checkpoint saved before the interrupted operation? The files written by that operation are discarded.', {}), {title: t('training.recover.title', 'Recover production checkpoint?'), actionLabel: t('training.recover.action', 'Recover'), danger: false})) {
                return;
            }
            try {
                await API.post(`/api/lora/models/${encodeURIComponent(adapterId)}/recover-checkpoint-swap`, {});
                showToast(t('training.recover.done', 'Interrupted checkpoint operation recovered.'), 'success');
                await loadLoraModels();
            } catch (e) {
                showActionError("Recovery failed", e, "Refresh the adapter list and checkpoint history before recovering again. Review any refusal about changed checkpoint files.");
            }
        };

        window.deleteLoraRollbackBackup = async (adapterId) => {
            if (!await showConfirm(formatUiSafetyText('safety.training.delete-backup', 'Permanently delete this rollback backup? You will no longer be able to restore the pre-promotion checkpoint.', {}), {title: t('training.delete-backup.title', 'Delete rollback backup?'), actionLabel: t('training.delete-backup.action', 'Delete backup'), danger: true})) {
                return;
            }
            try {
                const response = await fetch(`/api/lora/models/${encodeURIComponent(adapterId)}/rollback-backup`, { method: 'DELETE' });
                await API._handleError(response);
                showToast(t('training.delete-backup.done', 'Rollback backup deleted.'), 'success');
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
                btn.title = t('training.adapter.preview-play-title', 'Play preview');
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
            if (loraTestPending) { showToast(t('training.test.wait', 'Wait for the current voice test to finish.'), 'warning'); return; }
            document.getElementById('lora-test-adapter').value = adapterId;
            document.getElementById('lora-test-form').style.display = '';
            document.getElementById('lora-test-text').focus();
        };

        window.runLoraTest = async () => {
            if (loraTestPending) { return; }
            const adapterId = document.getElementById('lora-test-adapter').value;
            const text = document.getElementById('lora-test-text').value.trim();
            const instruct = document.getElementById('lora-test-instruct').value.trim();
            if (!adapterId) { showToast(t('training.test.no-adapter', 'Select an adapter.'), 'warning'); return; }
            if (!text) { showToast(t('training.test.no-text', 'Enter text to synthesize.'), 'warning'); return; }

            const statusEl = document.getElementById('lora-test-status');
            const button = document.getElementById('btn-lora-test-generate');
            const label = button.innerHTML;
            const controls = ['lora-test-adapter', 'lora-test-text', 'lora-test-instruct', 'btn-lora-test-generate']
                .map(id => { const field = document.getElementById(id); return {field, disabled: field.disabled}; });
            loraTestPending = true;
            controls.forEach(({field}) => { field.disabled = true; });
            button.textContent = t('training.test.generating-button', 'Generating…');
            statusEl.textContent = t('training.test.generating', 'Generating test audio for {adapter}…', {adapter: adapterId});
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
            if (!await showConfirm(formatUiSafetyText('safety.training.delete-adapter', 'Delete this trained adapter? This cannot be undone.', {}), {title: t('training.delete-adapter.title', 'Delete trained adapter?'), actionLabel: t('training.delete-adapter.action', 'Delete adapter'), danger: true})) { return; }
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
            btn.innerHTML = `<i class="fas fa-spinner fa-spin me-1"></i>${escapeHtml(t('training.adapter.downloading', 'Downloading...'))}`;

            try {
                await API.post(`/api/lora/download/${encodeURIComponent(adapterId)}`, {});
                showToast(t('training.adapter.downloaded', 'Adapter downloaded successfully.'), 'success');
                loadLoraModels();
            } catch (e) {
                showActionError("Download failed", e, "Refresh the adapter list and check whether the download completed before downloading again.");
            } finally {
                btn.disabled = false;
                btn.innerHTML = origHtml;
            }
        };
