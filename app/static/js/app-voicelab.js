        // ── Voice Lab: audiobook → named LoRA pipeline ───────────────────────
        const _voicelabPauseResume = _makePauseResumeHandler(
            '/api/voicelab/pause', '/api/voicelab/resume', 'btn-vl-pause');
        window.pauseResumeVoicelab = _voicelabPauseResume;

        function _vlSetCheckIcon(id, ok) {
            const el = document.getElementById(id);
            if (el) {
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
                    ? `<span class="badge bg-warning text-dark">${issues} issue${issues>1?'s':''}</span>`
                    : '<span class="badge bg-success">ready</span>';
                const profilerErrors = res.profiler_errors || [];
                if (profilerErrors.length) {
                    document.getElementById('voicelab-readiness').title = profilerErrors.join('\n');
                } else {
                    document.getElementById('voicelab-readiness').removeAttribute('title');
                }
            } catch (e) { console.error('Failed to load Voice Lab config:', e); }
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
                showToast('Voice Lab settings saved.', 'success');
                await loadVoicelabConfig();
            } catch (e) { showToast('Save failed: ' + (e.message || 'unknown'), 'error'); }
        };

        window.voicelabInspect = async () => {
            const el = document.getElementById('vl-inspect');
            const dir = document.getElementById('vl-zips_dir').value.trim();
            el.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Inspecting…';
            try {
                const r = await API.get('/api/voicelab/inspect' + (dir ? `?zips_dir=${encodeURIComponent(dir)}` : ''));
                const m = r.manifest || {};
                const q = r.quality || {};
                el.innerHTML =
                    `<span class="badge bg-secondary me-1">${r.narrator_count} narrator folder${r.narrator_count!==1?'s':''}</span>`
                    + (r.deduped_exists
                        ? `<span class="badge bg-info me-1">${r.deduped_count} deduped zip${r.deduped_count!==1?'s':''}</span>`
                        : '<span class="badge bg-light text-muted border me-1">no _deduped yet</span>')
                    + `<span class="badge bg-success me-1">${m.trained||0} trained</span>`
                    + `<span class="badge bg-secondary me-1">${q.zip_count||0} audited · ${q.warning_clip_count||0} warnings</span>`
                    + `<span class="badge bg-info me-1">${m.evaluated||0} evaluated</span>`
                    + `<span class="badge bg-primary me-1">${m.profiled||0} profiled</span>`
                    + `<span class="badge bg-warning text-dark me-1">${m.unnamed||0} unnamed</span>`;
            } catch (e) {
                el.innerHTML = `<span class="text-danger">${escapeHtml(e.message || String(e))}</span>`;
            }
        };

        function _vlSetRunning(running) {
            document.getElementById('btn-vl-start').disabled = running;
            document.getElementById('btn-vl-pause').style.display = running ? 'inline-block' : 'none';
            document.getElementById('btn-vl-cancel').style.display = running ? 'inline-block' : 'none';
            if (running) { _resetPauseBtn('btn-vl-pause'); }
        }

        function getVoicelabRequest() {
            const stages = Array.from(document.querySelectorAll('.vl-stage:checked')).map(c => c.value);
            return {
                zips_dir: document.getElementById('vl-zips_dir').value.trim() || undefined,
                stages,
                device: document.getElementById('vl-device').value || undefined,
                target_loss: parseFloat(document.getElementById('vl-target-loss').value) || 4.15,
                max_epochs: parseInt(document.getElementById('vl-max-epochs').value, 10) || 6,
                lora_r: parseInt(document.getElementById('vl-lora-r').value, 10) || 64,
                candidate_checkpoints: Math.max(0, Math.min(2, parseInt(document.getElementById('vl-candidate-checkpoints').value, 10) || 0)),
                name_apply: document.getElementById('vl-name-apply').checked,
                name_overwrite: document.getElementById('vl-name-overwrite').checked,
            };
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
            el.innerHTML = `<strong>Preflight: ${blockers.length ? 'blocked' : 'ready'}</strong>`
                + `<div>${escapeHtml((report.stages || []).join(' → '))}</div>`
                + `<div>${dataset.narrator_count || 0} narrators · ${dataset.zip_count || 0} source ZIPs · ${dataset.deduped_count || 0} deduped ZIPs</div>`
                + `<div>${escapeHtml(runtime.gpu || runtime.device || 'device unavailable')} · ${runtime.free_vram_gb ?? 'unknown'} GB VRAM free · ${runtime.free_disk_gb ?? 'unknown'} GB disk free</div>`
                + (findings ? `<ul class="mb-0 mt-1">${findings}</ul>` : '');
        }

        window.startVoicelab = async () => {
            const body = getVoicelabRequest();
            if (!body.stages.length) { showToast('Select at least one stage to run.', 'warning'); return; }
            let preflight;
            try {
                preflight = await API.post('/api/voicelab/preflight', body);
                renderVoicelabPreflight(preflight);
            } catch (e) {
                showToast('Preflight failed: ' + (e.message || 'unknown'), 'error');
                return;
            }
            if (!preflight.ready) { return; }
            const warningText = (preflight.warnings || []).map(item => item.message).join('\n');
            const prompt = `${warningText ? warningText + '\n\n' : ''}Preflight is ready. Run ${preflight.stages.join(' → ')}?`;
            if (!confirm(prompt)) { return; }
            body.preflight_id = preflight.preflight_id;
            _vlSetRunning(true);
            document.getElementById('voicelab-status').innerHTML =
                '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Starting…</span>';
            try {
                await API.post('/api/voicelab/start', body);
                pollVoicelab();
            } catch (e) {
                _vlSetRunning(false);
                document.getElementById('voicelab-status').innerHTML =
                    `<span class="text-danger">${escapeHtml(e.message || String(e))}</span>`;
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
            if (h > 0) { return `${h}h ${m}m`; }
            if (m > 0) { return `${m}m ${s}s`; }
            return `${s}s`;
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
                + `<span><strong>Pipeline health</strong> `
                + `<span class="badge bg-${tone}">${escapeHtml(health.status || 'idle')}</span></span>`
                + `<span class="text-muted">${escapeHtml(health.device || 'device: auto')}</span></div>`);

            if ((health.pending_recovery || []).length) {
                const names = health.pending_recovery.map(r => escapeHtml(r.adapter_id)).join(', ');
                parts.push(`<div class="alert alert-danger py-1 px-2 mb-1">`
                    + `<i class="fas fa-life-ring me-1"></i>Recovery required: ${names}. `
                    + `<a href="#" onclick="openLoraModelsTab(); return false;">Open LoRA models</a> to recover.</div>`);
            }

            const active = health.active_run;
            if (active) {
                const stage = active.stage ? escapeHtml(active.stage) : 'starting';
                const pos = (active.stage_index !== null && active.stage_count)
                    ? ` (${active.stage_index + 1}/${active.stage_count})` : '';
                const elapsed = _vlFormatElapsed(active.elapsed_seconds);
                const eta = _vlFormatElapsed(active.eta_seconds);
                parts.push(`<div><i class="fas fa-spinner fa-spin me-1"></i>`
                    + `Running stage <strong>${stage}</strong>${pos}`
                    + (active.paused ? ' <span class="badge bg-warning text-dark">paused</span>' : '')
                    + (elapsed ? ` · ${elapsed} elapsed` : '')
                    + (eta ? ` · ~${eta} left` : '')
                    + (active.progress ? ` · ${escapeHtml(String(active.progress))}` : '') + `</div>`);
            }

            const fmtWhen = (iso) => iso ? new Date(iso).toLocaleString() : '';
            if (health.last_success) {
                parts.push(`<div class="text-success"><i class="fas fa-check me-1"></i>`
                    + `Last success: ${escapeHtml(fmtWhen(health.last_success.finished_at)) || 'unknown'}</div>`);
            }
            if (health.last_failure) {
                const f = health.last_failure;
                const detail = f.failure && f.failure.message ? ` — ${escapeHtml(f.failure.message)}` : '';
                parts.push(`<div class="text-danger"><i class="fas fa-triangle-exclamation me-1"></i>`
                    + `Last ${escapeHtml(f.status || 'failure')}: ${escapeHtml(fmtWhen(f.finished_at)) || 'unknown'}${detail}</div>`);
            }
            if (!active && !health.last_success && !health.last_failure && !(health.pending_recovery || []).length) {
                parts.push(`<div class="text-muted">No Voice Lab runs recorded yet.</div>`);
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

        async function refreshVoicelabHealth() {
            const el = document.getElementById('vl-health-body');
            if (!el) { return; }
            try {
                const health = await API.get('/api/voicelab/health');
                renderVoicelabHealth(health);
            } catch (e) {
                el.innerHTML = `<span class="text-muted">Pipeline health unavailable.</span>`;
            }
        }

        // Sanitized, bounded diagnostics bundle (secrets/home paths redacted
        // server-side). Full logs are intentionally excluded — use the Full log
        // button or Pinokio's Get Help / session bundle for those.
        async function _fetchVoicelabDiagnostics() {
            const data = await API.get('/api/voicelab/diagnostics');
            return JSON.stringify(data, null, 2);
        }

        window.copyVoicelabDiagnostics = async () => {
            try {
                const text = await _fetchVoicelabDiagnostics();
                await navigator.clipboard.writeText(text);
                showToast('Sanitized diagnostics copied to clipboard.', 'success');
            } catch (e) {
                showToast('Could not copy diagnostics: ' + (e.message || String(e)), 'error');
            }
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
                showToast('Could not download diagnostics: ' + (e.message || String(e)), 'error');
            }
        };

        function pollVoicelab() {
            const logEl = document.getElementById('voicelab-logs');
            const progEl = document.getElementById('vl-stage-progress');
            const colours = { pending: 'secondary', running: 'primary', done: 'success', failed: 'danger', cancelled: 'warning' };
            _startPolling('voicelab', () => API.get('/api/status/voicelab'), {
                doneCheck: s => !s.running,
                onTick: s => {
                    if (logEl) { logEl.innerText = (s.logs || []).join('\n'); logEl.scrollTop = logEl.scrollHeight; }
                    if (s.tasks && s.tasks.length) {
                        progEl.style.display = 'flex';
                        progEl.innerHTML = s.tasks.map(t =>
                            `<span class="badge bg-${colours[t.status] || 'secondary'}">${escapeHtml(t.name)}: ${t.status}</span>`).join('');
                    }
                    refreshVoicelabHealth();  // reuse this poll tick; no extra timer
                },
                onDone: s => {
                    _vlSetRunning(false);
                    notifyJobDone('voicelab');
                    const cls = s.status === 'done' ? 'text-success'
                              : s.status === 'cancelled' ? 'text-warning' : 'text-danger';
                    document.getElementById('voicelab-status').innerHTML =
                        `<span class="${cls}">Pipeline ${escapeHtml(s.status || 'finished')}.</span>`;
                    voicelabInspect();  // refresh manifest counts
                    refreshVoicelabHealth();
                }
            });
        }

