        // ── Voice Lab: audiobook → named LoRA pipeline ───────────────────────
        const _voicelabPauseResume = _makePauseResumeHandler(
            '/api/voicelab/pause', '/api/voicelab/resume', 'btn-vl-pause');
        window.pauseResumeVoicelab = _voicelabPauseResume;

        function _vlSetCheckIcon(id, ok) {
            const el = document.getElementById(id);
            if (el) {
                el.title = ok ? t('voicelab.check.found', 'Found') : t('voicelab.check.missing', 'Not found — check the configured path, then save settings.');
                el.innerHTML = ok
                    ? '<i class="fas fa-check-circle text-success"></i>'
                    : '<i class="fas fa-times-circle text-danger"></i>';
            }
        }

        async function loadVoicelabConfig() {
            try {
                const res = await API.get('/api/voicelab/config');
                const c = res.config || {};
                document.getElementById('vl-rocm_python').value = c.rocm_python || '';
                document.getElementById('vl-profiler_model').value = c.profiler_model || '';
                document.getElementById('vl-zips_dir_cfg').value = c.zips_dir || '';
                document.getElementById('vl-epub_dirs').value = (c.epub_dirs || []).join('\n');
                const input = document.getElementById('vl-zips_dir');
                if (!input.value) { input.value = c.zips_dir || ''; }

                const ck = res.checks || {};
                // batch_train_lora/voice_profiler ship with the app, so like
                // voice_analysis/name_voices they have no settings input - they
                // still count toward the issue badge below via res.checks.
                ['rocm_python','profiler_model','zips_dir','epub_dirs']
                    .forEach(k => _vlSetCheckIcon('chk-' + k, !!ck[k]));
                const issues = Object.values(ck).filter(v => v === false).length;
                document.getElementById('voicelab-readiness').innerHTML = issues
                    ? `<span class="badge bg-warning text-dark">${escapeHtml(issues === 1 ? t('voicelab.readiness.issues.one', '{count} issue', {count: issues}) : t('voicelab.readiness.issues.many', '{count} issues', {count: issues}))}</span>`
                    : `<span class="badge bg-success">${escapeHtml(t('voicelab.readiness.ready', 'ready'))}</span>`;
                const labels = {
                    rocm_python: t('voicelab.settings.rocm.label', 'ROCm Python'),
                    profiler_model: t('voicelab.check.profiler-model', 'Profiler model'),
                    zips_dir: t('voicelab.check.zips-dir', 'ZIP directory'),
                    epub_dirs: t('voicelab.check.epub-dirs', 'EPUB directories'),
                    batch_train_lora: t('voicelab.check.batch-script', 'Batch training script'),
                    voice_profiler: t('voicelab.check.profiler-script', 'Voice profiler script'),
                    voice_analysis: t('voicelab.check.analysis-script', 'Voice analysis script'),
                    name_voices: t('voicelab.check.naming-script', 'Voice naming script'),
                    profiler_environment: t('voicelab.check.profiler-env', 'Profiler environment')};
                const failedLabels = Object.entries(ck).filter(([, ok]) => ok === false)
                    .map(([key]) => labels[key] || key);
                const profilerErrors = res.profiler_errors || [];
                const help = document.getElementById('voicelab-readiness-help');
                const message = [failedLabels.length
                    ? t('voicelab.readiness.not-ready', 'Not ready: {items}. Check the configured paths in Pipeline settings & environment, then save settings. If an app script is missing, update or repair the installation.', {items: failedLabels.join(', ')}) : '',
                    ...profilerErrors].filter(Boolean).join('\n');
                help.textContent = message;
                help.hidden = !message;
                if (message) { document.getElementById('voicelab-readiness').title = message; }
                else { document.getElementById('voicelab-readiness').removeAttribute('title'); }
            } catch (e) {
                console.error('Failed to load Voice Lab config:', e);
                const readiness = document.getElementById('voicelab-readiness');
                readiness.innerHTML = `<span class="badge bg-secondary">${escapeHtml(t('voicelab.readiness.config-unavailable', 'config unavailable'))}</span>`;
                readiness.title = getActionErrorMessage('Displayed settings could not be verified', e, 'Your current fields are kept. Reopen Pipeline settings & environment to retry.');
                const help = document.getElementById('voicelab-readiness-help');
                help.textContent = getActionErrorMessage('Settings could not be verified', e, 'Your current fields are kept. Reopen Pipeline settings & environment to retry.');
                help.hidden = false;
            }
        }

        window.saveVoicelabConfig = async () => {
            const body = {
                rocm_python: document.getElementById('vl-rocm_python').value.trim(),
                profiler_model: document.getElementById('vl-profiler_model').value.trim(),
                zips_dir: document.getElementById('vl-zips_dir_cfg').value.trim(),
                epub_dirs: document.getElementById('vl-epub_dirs').value.split('\n').map(v => v.trim()).filter(Boolean),
            };
            try {
                await API.post('/api/voicelab/config', body);
                showToast(t('voicelab.settings.saved', 'Voice Lab settings saved.'), 'success');
                await loadVoicelabConfig();
            } catch (e) { showActionError('Voice Lab settings save is unconfirmed', e, 'Keep your settings. Check the saved Pipeline settings & environment before saving again.'); }
        };

        let _voicelabInspectRequest = 0;
        window.voicelabInspect = async (runDir = null) => {
            const request = ++_voicelabInspectRequest;
            const el = document.getElementById('vl-inspect');
            const dir = runDir === null ? document.getElementById('vl-zips_dir').value.trim() : runDir;
            const isCurrent = () => request === _voicelabInspectRequest &&
                (runDir !== null || document.getElementById('vl-zips_dir').value.trim() === dir);
            el.innerHTML = `<i class="fas fa-spinner fa-spin me-1"></i>${escapeHtml(t('voicelab.inspect.loading', 'Inspecting…'))}`;
            try {
                const r = await API.get('/api/voicelab/inspect' + (dir ? `?zips_dir=${encodeURIComponent(dir)}` : ''));
                if (!isCurrent()) { return; }
                const m = r.manifest || {};
                const q = r.quality || {};
                el.innerHTML =
                    `<span class="badge bg-secondary me-1">${escapeHtml(r.narrator_count === 1 ? t('voicelab.inspect.narrators.one', '{count} narrator folder', {count: r.narrator_count}) : t('voicelab.inspect.narrators.many', '{count} narrator folders', {count: r.narrator_count}))}</span>`
                    + (r.deduped_exists
                        ? `<span class="badge bg-info me-1">${escapeHtml(r.deduped_count === 1 ? t('voicelab.inspect.deduped.one', '{count} deduped zip', {count: r.deduped_count}) : t('voicelab.inspect.deduped.many', '{count} deduped zips', {count: r.deduped_count}))}</span>`
                        : `<span class="badge bg-light text-muted border me-1">${escapeHtml(t('voicelab.inspect.no-deduped', 'no _deduped yet'))}</span>`)
                    + `<span class="badge bg-success me-1">${escapeHtml(t('voicelab.inspect.trained', '{count} trained', {count: m.trained || 0}))}</span>`
                    + `<span class="badge bg-secondary me-1">${escapeHtml(t('voicelab.inspect.audited', '{audited} audited · {warnings} warnings', {audited: q.zip_count || 0, warnings: q.warning_clip_count || 0}))}</span>`
                    + `<span class="badge bg-info me-1">${escapeHtml(t('voicelab.inspect.evaluated', '{count} evaluated', {count: m.evaluated || 0}))}</span>`
                    + `<span class="badge bg-primary me-1">${escapeHtml(t('voicelab.inspect.profiled', '{count} profiled', {count: m.profiled || 0}))}</span>`
                    + `<span class="badge bg-warning text-dark me-1">${escapeHtml(t('voicelab.inspect.unnamed', '{count} unnamed', {count: m.unnamed || 0}))}</span>`;
            } catch (e) {
                if (!isCurrent()) { return; }
                el.innerHTML = `<span class="text-danger">${escapeHtml(getActionErrorMessage('Voice Lab inspection failed', e, 'Check the ZIP directory and configured interpreter, then inspect the directory again.'))}</span>`;
            }
        };

        function _vlSetRunning(running, starting = false) {
            document.getElementById('btn-vl-start').disabled = running || starting;
            document.getElementById('btn-vl-pause').style.display = running ? 'inline-block' : 'none';
            document.getElementById('btn-vl-cancel').style.display = running ? 'inline-block' : 'none';
            if (running) { _resetPauseBtn('btn-vl-pause'); }
        }

        function getVoicelabNumber(id, label, integer = false) {
            const raw = document.getElementById(id).value.trim();
            const value = Number(raw);
            if (!raw || !Number.isFinite(value) || (integer && !Number.isInteger(value))) {
                throw new Error(integer
                    ? t('voicelab.validation.integer', '{field} must be a whole number.', {field: label})
                    : t('voicelab.validation.number', '{field} must be a number.', {field: label}));
            }
            return value;
        }

        function getVoicelabRequest() {
            const stages = Array.from(document.querySelectorAll('.vl-stage:checked')).map(c => c.value);
            return {
                zips_dir: document.getElementById('vl-zips_dir').value.trim() || undefined,
                stages,
                device: document.getElementById('vl-device').value || undefined,
                target_loss: getVoicelabNumber('vl-target-loss', t('voicelab.target-loss.label', 'Target loss')),
                max_epochs: getVoicelabNumber('vl-max-epochs', t('voicelab.max-epochs.label', 'Max epochs'), true),
                lora_r: getVoicelabNumber('vl-lora-r', t('voicelab.lora-rank.label', 'LoRA rank'), true),
                candidate_checkpoints: getVoicelabNumber('vl-candidate-checkpoints', t('voicelab.candidates.label', 'Eval candidates'), true),
                name_apply: document.getElementById('vl-name-apply').checked,
                name_overwrite: document.getElementById('vl-name-overwrite').checked,
            };
        }

        // The one mapping from a pipeline stage id (VOICELAB_STAGES on the
        // server) to its on-screen label, so the checkboxes, the preflight
        // list and the progress pills cannot drift apart.
        function voicelabStageLabel(stage) {
            if (stage === 'quality') { return t('voicelab.stage.quality', 'Quality'); }
            if (stage === 'dedup') { return t('voicelab.stage.dedup', 'Dedup'); }
            if (stage === 'train') { return t('voicelab.stage.train', 'Train'); }
            if (stage === 'evaluate') { return t('voicelab.stage.evaluate', 'Evaluate'); }
            if (stage === 'profile') { return t('voicelab.stage.profile', 'Profile'); }
            if (stage === 'name') { return t('voicelab.stage.name', 'Name'); }
            return stage;
        }

        function renderVoicelabPreflight(report) {
            const el = document.getElementById('vl-preflight');
            const blockers = report.blockers || [];
            const warnings = report.warnings || [];
            const runtime = report.runtime || {};
            const dataset = report.dataset || {};
            el.className = `alert ${blockers.length ? 'alert-danger' : warnings.length ? 'alert-warning' : 'alert-success'} small mb-2`;
            el.style.display = 'block';
            const findings = [...blockers, ...warnings]
                .map(item => `<li>${escapeHtml(item.message)}</li>`).join('');
            el.innerHTML = `<strong>${escapeHtml(blockers.length ? t('voicelab.preflight.blocked', 'Preflight: blocked') : t('voicelab.preflight.ready', 'Preflight: ready'))}</strong>`
                + `<div>${escapeHtml((report.stages || []).map(voicelabStageLabel).join(' → '))}</div>`
                + `<div>${escapeHtml(t('voicelab.preflight.dataset', '{narrators} narrators · {sources} source ZIPs · {deduped} deduped ZIPs', {narrators: dataset.narrator_count || 0, sources: dataset.zip_count || 0, deduped: dataset.deduped_count || 0}))}</div>`
                + `<div>${escapeHtml(t('voicelab.preflight.runtime', '{device} · {vram} GB VRAM free · {disk} GB disk free', {device: runtime.gpu || runtime.device || t('voicelab.preflight.device-unavailable', 'device unavailable'), vram: runtime.free_vram_gb ?? t('voicelab.preflight.unknown', 'unknown'), disk: runtime.free_disk_gb ?? t('voicelab.preflight.unknown', 'unknown')}))}</div>`
                + (findings ? `<ul class="mb-0 mt-1">${findings}</ul>` : '');
        }

        let _vlStarting = false;
        window.startVoicelab = async () => {
            if (_vlStarting) { return; }
            let body;
            try {
                body = getVoicelabRequest();
            } catch (e) {
                showActionError('Pipeline settings need attention', e, 'Keep the current settings and correct the field identified below before running the pipeline.', 'warning');
                return;
            }
            if (!body.stages.length) { showToast(t('voicelab.start.no-stages', 'Select at least one stage to run.'), 'warning'); return; }
            _vlStarting = true;
            _vlSetRunning(false, true);
            const startButton = document.getElementById('btn-vl-start');
            const startLabel = startButton.innerHTML;
            startButton.textContent = t('voicelab.start.preflighting', 'Preflighting…');
            document.getElementById('voicelab-status').textContent = t('voicelab.start.checking', 'Checking configuration, dataset, GPU, and disk…');
            let started = false;
            let phase = 'Preflight';
            try {
                const preflight = await API.post('/api/voicelab/preflight', body);
                renderVoicelabPreflight(preflight);
                if (!preflight.ready) { document.getElementById('voicelab-status').textContent = t('voicelab.start.blocked', 'Preflight blocked the run. Review the checks below and try again.'); return; }
                const warningText = (preflight.warnings || []).map(item => item.message).join('\n');
                const prompt = warningText
                    ? t('voicelab.start.confirm-warnings', '{warnings}\n\nPreflight is ready. Run {stages}?', {warnings: warningText, stages: preflight.stages.map(voicelabStageLabel).join(' → ')})
                    : t('voicelab.start.confirm', 'Preflight is ready. Run {stages}?', {stages: preflight.stages.map(voicelabStageLabel).join(' → ')});
                if (!await showConfirm(prompt, {title: t('voicelab.start.confirm-title', 'Start Voice Lab pipeline?'), actionLabel: t('voicelab.start.confirm-action', 'Start pipeline'), danger: false})) { document.getElementById('voicelab-status').textContent = t('voicelab.start.cancelled', 'Pipeline start cancelled.'); return; }
                body.preflight_id = preflight.preflight_id;
                phase = 'Start';
                document.getElementById('voicelab-status').innerHTML =
                    `<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>${escapeHtml(t('voicelab.start.starting', 'Starting…'))}</span>`;
                const result = await API.post('/api/voicelab/start', body);
                started = true;
                _vlSetRunning(true);
                pollVoicelab(result.zips_dir);
            } catch (e) {
                if (phase === 'Preflight') {
                    document.getElementById('voicelab-status').textContent = getActionErrorMessage('Pipeline preflight did not finish', e, 'Keep the current settings. Check the configured paths, stages and numeric fields, then run the pipeline again.');
                    showActionError('Pipeline preflight failed', e, 'Check the configured paths, stages and numeric fields before running the pipeline again.');
                } else {
                    document.getElementById('voicelab-status').innerHTML =
                        `<span class="text-danger">${escapeHtml(getActionErrorMessage('Pipeline start is unconfirmed', e, 'Check the pipeline task status before running again. Review any configuration or stage refusal below.'))}</span>`;
                }
            } finally {
                startButton.innerHTML = startLabel;
                _vlStarting = false;
                if (!started) { _vlSetRunning(false); }
            }
        };

        window.cancelVoicelab = () => cancelTask('/api/voicelab/cancel', {
            onSuccess: () => _resetPauseBtn('btn-vl-pause'),
        });

        function _vlFormatElapsed(seconds) {
            if (seconds === null || seconds === undefined) { return ''; }
            const total = Math.max(0, Math.round(seconds));
            const h = Math.floor(total / 3600);
            const m = Math.floor((total % 3600) / 60);
            const s = total % 60;
            if (h > 0) { return t('core.duration.hours', '{hours}h {minutes}m', {hours: h, minutes: m}); }
            if (m > 0) { return t('core.duration.minutes', '{minutes}m {seconds}s', {minutes: m, seconds: s}); }
            return t('core.duration.seconds', '{seconds}s', {seconds: s});
        }

        function _vlHealthTone(status) {
            if (status === 'recovery_required') { return 'danger'; }
            if (status === 'running') { return 'primary'; }
            if (status === 'failed' || status === 'interrupted') { return 'danger'; }
            if (status === 'cancelled') { return 'warning'; }
            if (status === 'ok') { return 'success'; }
            return 'secondary';
        }

        // Paints the read-only Voice Lab health card. Recovery-required always
        // wins the headline banner over ordinary run status.
        function renderVoicelabHealth(health) {
            const el = document.getElementById('vl-health-body');
            if (!el) { return; }
            const tone = _vlHealthTone(health.status);
            const parts = [];
            parts.push(`<div class="d-flex justify-content-between align-items-center mb-1">`
                + `<span><strong>${escapeHtml(t('voicelab.health.title', 'Pipeline health'))}</strong> `
                + `<span class="badge bg-${tone}">${escapeHtml(taskStatusLabel(health.status) || t('voicelab.health.idle', 'idle'))}</span></span>`
                + `<span class="text-muted">${escapeHtml(health.device || t('voicelab.health.device-auto', 'device: auto'))}</span></div>`);

            if ((health.pending_recovery || []).length) {
                const names = health.pending_recovery.map(r => escapeHtml(r.adapter_id)).join(', ');
                parts.push(`<div class="alert alert-danger py-1 px-2 mb-1">`
                    + `<i class="fas fa-life-ring me-1"></i>${escapeHtml(t('voicelab.health.recovery-required', 'Recovery required: {adapters}.', {adapters: names}))} `
                    + `<a href="#" onclick="openLoraModelsTab(); return false;">${escapeHtml(t('voicelab.health.open-models', 'Open LoRA models'))}</a> ${escapeHtml(t('voicelab.health.to-recover', 'to recover.'))}</div>`);
            }

            const active = health.active_run;
            if (active) {
                const stage = active.stage ? voicelabStageLabel(active.stage) : t('voicelab.health.starting', 'starting');
                const pos = (active.stage_index !== null && active.stage_count)
                    ? ` (${active.stage_index + 1}/${active.stage_count})` : '';
                const elapsed = _vlFormatElapsed(active.elapsed_seconds);
                const eta = _vlFormatElapsed(active.eta_seconds);
                parts.push(`<div><i class="fas fa-spinner fa-spin me-1"></i>`
                    + `${escapeHtml(t('voicelab.health.running-stage', 'Running stage {stage}', {stage}))}${pos}`
                    + (active.paused ? ` <span class="badge bg-warning text-dark">${escapeHtml(t('voicelab.health.paused', 'paused'))}</span>` : '')
                    + (elapsed ? ` <span class="me-2">${escapeHtml(t('voicelab.health.elapsed', '{elapsed} elapsed', {elapsed}))}</span>` : '')
                    + (eta ? ` <span class="me-2">${escapeHtml(t('voicelab.health.remaining', '~{eta} left', {eta}))}</span>` : '')
                    + (active.progress ? ` <span>${escapeHtml(String(active.progress))}</span>` : '') + `</div>`);
            }

            const fmtWhen = (iso) => iso ? formatUiDate(new Date(iso)) : '';
            if (health.last_success) {
                parts.push(`<div class="text-success"><i class="fas fa-check me-1"></i>`
                    + `${escapeHtml(t('voicelab.health.last-success', 'Last success: {when}', {when: fmtWhen(health.last_success.finished_at) || t('voicelab.health.unknown', 'unknown')}))}</div>`);
            }
            if (health.last_failure) {
                const f = health.last_failure;
                const detail = f.failure && f.failure.message ? ` — ${escapeHtml(f.failure.message)}` : '';
                parts.push(`<div class="text-danger"><i class="fas fa-triangle-exclamation me-1"></i>`
                    + `${escapeHtml(t('voicelab.health.last-failure', 'Last {status}: {when}', {status: taskStatusLabel(f.status) || t('voicelab.health.failure', 'failure'), when: fmtWhen(f.finished_at) || t('voicelab.health.unknown', 'unknown')}))}${detail}</div>`);
            }
            if (!active && !health.last_success && !health.last_failure && !(health.pending_recovery || []).length) {
                parts.push(`<div class="text-muted">${escapeHtml(t('voicelab.health.none', 'No Voice Lab runs recorded yet.'))}</div>`);
            }

            parts.push(`<div class="mt-1"><i class="fas fa-arrow-right me-1"></i>`
                + `${escapeHtml(health.next_action || '')}</div>`);
            el.innerHTML = parts.join('');
        }

        // The LoRA models list (with the Recover control) lives in the Training
        // tab; reuse the nav link so its normal load hook fires.
        window.openLoraModelsTab = () => {
            const link = document.querySelector('.nav-link[data-tab="training"]');
            if (link) { link.click(); }
        };

        let _vlHealthRequest = null;
        let _vlHealthGeneration = 0;
        async function refreshVoicelabHealth(force = false) {
            const el = document.getElementById('vl-health-body');
            if (!el) { return; }
            while (_vlHealthRequest) {
                await _vlHealthRequest;
                if (!force) { return; }
            }
            const generation = _vlHealthGeneration;
            _vlHealthRequest = Promise.resolve().then(async () => {
                try {
                    const health = await API.get('/api/voicelab/health');
                    if (generation === _vlHealthGeneration) { renderVoicelabHealth(health); }
                } catch (e) {
                    if (generation === _vlHealthGeneration) {
                        el.innerHTML = `<span class="text-muted">${escapeHtml(t('voicelab.health.unavailable', 'Pipeline health unavailable.'))}</span>`;
                    }
                } finally {
                    _vlHealthRequest = null;
                }
            });
            await _vlHealthRequest;
        }

        // Sanitized, bounded diagnostics bundle (secrets/home paths redacted
        // server-side). Full logs are intentionally excluded — use the Full log
        // button or Pinokio's Get Help / session bundle for those.
        async function _fetchVoicelabDiagnostics() {
            const data = await API.get('/api/voicelab/diagnostics');
            return JSON.stringify(data, null, 2);
        }

        window.copyVoicelabDiagnostics = async () => {
            let text;
            try {
                text = await _fetchVoicelabDiagnostics();
            } catch (e) {
                showActionError('Could not fetch diagnostics', e, 'Check the app connection, then request the sanitized diagnostics again.');
                return;
            }
            await copyToClipboard(text, 'Sanitized diagnostics');
        };

        window.downloadVoicelabDiagnostics = async () => {
            try {
                const text = await _fetchVoicelabDiagnostics();
                const blob = new Blob([text], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                const stamp = new Date().toISOString().replace(/[:.]/g, '-');
                a.href = url;
                a.download = `voicelab-diagnostics-${stamp}.json`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
            } catch (e) {
                showActionError('Could not download diagnostics', e, 'Check the app connection and browser download permissions, then download again or use Copy Diagnostics.');
            }
        };

        function pollVoicelab(runDir = null) {
            const logEl = document.getElementById('voicelab-logs');
            const progEl = document.getElementById('vl-stage-progress');
            const colours = { pending: 'secondary', running: 'primary', done: 'success', failed: 'danger', cancelled: 'warning' };
            const renderLogs = createTaskLogRenderer(logEl);
            _startPolling('voicelab', () => API.get('/api/status/voicelab?include_health=true'), {
                doneCheck: s => !s.running,
                onTick: s => {
                    if (s.health) {
                        _vlHealthGeneration++;
                        renderVoicelabHealth(s.health);
                    }
                    if (s.zips_dir) { runDir = s.zips_dir; }
                    renderLogs(s);
                    if (s.tasks && s.tasks.length) {
                        progEl.style.display = 'flex';
                        progEl.innerHTML = s.tasks.map(task =>
                            `<span class="badge bg-${colours[task.status] || 'secondary'}">${escapeHtml(voicelabStageLabel(task.name))}: ${escapeHtml(taskStatusLabel(task.status))}</span>`).join('');
                    }
                    else {
                        progEl.style.display = 'none';
                        progEl.innerHTML = '';
                    }
                },
                onDone: s => {
                    _vlSetRunning(false);
                    notifyJobDone('voicelab', '', 'finished', s);
                    const cls = s.status === 'done' ? 'text-success'
                              : s.status === 'cancelled' ? 'text-warning' : 'text-danger';
                    document.getElementById('voicelab-status').innerHTML =
                        `<span class="${cls}">${escapeHtml(t('voicelab.run.outcome', 'Pipeline {status}.', {status: taskStatusLabel(s.status) || taskStatusLabel('finished')}))}</span>`;
                    if (runDir !== null) { voicelabInspect(runDir); }
                }
            });
        }
