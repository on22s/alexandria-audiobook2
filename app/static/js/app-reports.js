        let currentReportFilename = null;
        let reportViewRequest = 0;
        let reportExplanationPending = false;
        let reportExplanationRunId = null;
        const reportExplanationEligibility = new Map();

        // ── Reports ────────────────────────────────────────────────────
        async function loadReports() {
            const listEl = document.getElementById('reports-list');
            if (!listEl) { return; }
            const request = {};
            listEl._reportListRequest = request;
            const isCurrent = () => listEl._reportListRequest === request
                && document.getElementById('reports-list') === listEl;
            try {
                const reports = await API.get('/api/reports');
                if (!isCurrent()) { return; }
                reportExplanationEligibility.clear();
                if (!reports.length) {
                    listEl.innerHTML = '<div class="list-group-item text-muted small">No reports yet. Reports are generated automatically each time a script review finishes.</div>';
                    return;
                }
                reports.forEach(r => { reportExplanationEligibility.set(r.filename, r.can_explain === true); });
                listEl.innerHTML = reports.map(r => {
                    const when = r.mtime ? new Date(r.mtime * 1000).toLocaleString() : '';
                    const icon = r.type === 'batch' ? 'fa-layer-group' : 'fa-file-lines';
                    const label = r.type === 'batch' ? 'Batch review' : 'Review';
                    return `<a href="#" class="list-group-item list-group-item-action report-list-item" data-filename="${escapeHtml(r.filename)}" onclick="viewReport(this.dataset.filename); return false;">
                        <div><i class="fas ${icon} me-2"></i>${label}</div>
                        <div class="text-muted small">${escapeHtml(when)}</div>
                    </a>`;
                }).join('');
            } catch (e) {
                if (!isCurrent()) { return; }
                listEl.innerHTML = `<div class="list-group-item text-danger small">${escapeHtml(getActionErrorMessage("Failed to load reports", e, "Reopen Reports or refresh the report list after checking the app connection."))}</div>`;
            }
        }

        async function loadCheckpoints() {
            const listEl = document.getElementById('checkpoints-list');
            if (!listEl) { return; }
            try {
                const data = await API.get('/api/review/checkpoints');
                const cps = data.checkpoints || [];
                let html = '';
                if (data.live) {
                    const L = data.live;
                    const passLabel = L.bidirectional
                        ? (L.current_pass === 'bwd' ? 'backward pass (2/2)' : 'forward pass (1/2)')
                        : 'single pass';
                    const items = (L.tasks || []).map((t, i) =>
                        `${i + 1}. ${escapeHtml(t.name || '')} — ${escapeHtml(t.status || 'pending')}`).join('<br>');
                    html += `<div class="list-group-item">
                        <div><span class="badge bg-primary">running</span> <strong>${escapeHtml(passLabel)}</strong></div>
                        <div class="text-muted small mt-1">${items}</div>
                    </div>`;
                }
                if (!cps.length) {
                    html += '<div class="list-group-item text-muted small">No saved checkpoints. One is written while a review runs and cleared when it finishes cleanly — a lingering one here means that book\'s review was interrupted.</div>';
                } else {
                    html += cps.map(c => {
                        const pct = c.total_batches ? Math.round(100 * c.completed_batches / c.total_batches) : 0;
                        const when = c.mtime ? new Date(c.mtime * 1000).toLocaleString() : '';
                        const failed = (c.failed_batches && c.failed_batches.length) ? ` · ${c.failed_batches.length} failed` : '';
                        const vram = c.batches_skipped_vram ? ` · ${c.batches_skipped_vram} VRAM-skipped` : '';
                        return `<div class="list-group-item">
                            <div><i class="fas fa-bookmark me-2"></i><strong>${escapeHtml(c.book)}</strong></div>
                            <div class="small">${c.completed_batches}/${c.total_batches} batches (${pct}%) · ${c.entries_done} entries done</div>
                            <div class="text-muted small">resumes at batch ${c.resume_from_batch}${failed}${vram}</div>
                            <div class="text-muted small">${escapeHtml(when)}</div>
                        </div>`;
                    }).join('');
                }
                listEl.innerHTML = html;
            } catch (e) {
                listEl.innerHTML = `<div class="list-group-item text-danger small">${escapeHtml(getActionErrorMessage("Failed to load checkpoints", e, "Reopen Reports to refresh checkpoint status. Do not treat an unavailable checkpoint list as an empty one."))}</div>`;
            }
        }

        async function viewReport(filename) {
            currentReportFilename = filename;
            const viewRequest = ++reportViewRequest;
            const explainButton = document.getElementById('btn-report-explain');
            explainButton.style.display = reportExplanationEligibility.get(filename) ? '' : 'none';
            explainButton.disabled = reportExplanationPending;
            const titleEl = document.getElementById('report-view-title');
            const contentEl = document.getElementById('report-view-content');
            document.querySelectorAll('#reports-list .report-list-item').forEach(el => {
                el.classList.toggle('active', el.dataset.filename === filename);
            });
            titleEl.textContent = filename;
            contentEl.innerHTML = '<p class="text-muted">Loading…</p>';
            try {
                const res = await fetch(`/api/reports/${encodeURIComponent(filename)}`);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const markdown = await res.text();
                if (viewRequest !== reportViewRequest || currentReportFilename !== filename) { return; }
                const html = marked.parse(markdown);
                contentEl.innerHTML = DOMPurify.sanitize(html);
            } catch (e) {
                if (viewRequest !== reportViewRequest || currentReportFilename !== filename) { return; }
                contentEl.innerHTML = `<p class="text-danger">${escapeHtml(getActionErrorMessage("Failed to load report", e, "Refresh the report list and reopen the selected report; check that the file is still available."))}</p>`;
            }
        }

        async function onExplainReport() {
            const filename = currentReportFilename;
            if (!filename || reportExplanationPending || !reportExplanationEligibility.get(filename)) { return; }
            reportExplanationPending = true;
            document.getElementById('btn-report-explain').disabled = true;
            let started = false;
            try {
                if (!(await confirmIfRemote('this report explanation'))) { return; }
                if (currentReportFilename !== filename) { return; }
                const response = await API.post(`/api/reports/${encodeURIComponent(filename)}/explain`, {});
                if (!response || typeof response.run_id !== 'string' || !response.run_id) { throw new Error('Explanation start did not return a run identifier'); }
                const runId = response.run_id;
                reportExplanationRunId = runId;
                started = true;
                document.getElementById('btn-report-explanation-cancel').style.display = '';
                _startPolling('report_explanation', () => API.get('/api/status/report_explanation'), {
                    interval: 1000,
                    doneCheck: data => data.run_id !== runId || !data.running,
                    onDone: async data => {
                        reportExplanationPending = false;
                        reportExplanationRunId = null;
                        document.getElementById('btn-report-explanation-cancel').style.display = 'none';
                        document.getElementById('btn-report-explain').disabled = false;
                        if (data.run_id !== runId) {
                            showToast('Explanation status changed; reload the report to check its result', 'warning');
                        } else if (data.status === 'done') {
                            if (currentReportFilename === filename) { await viewReport(filename); }
                            showToast('Report explanation saved', 'success');
                        } else {
                            showToast(data.error || 'Explanation cancelled; report retained', 'warning');
                        }
                    },
                });
            } catch (e) {
                showActionError('Report explanation start is unconfirmed', e, 'Check explanation status and reopen the report before starting again. For an LLM refusal, use Setup → Test Connection.');
            } finally {
                if (!started) {
                    reportExplanationPending = false;
                    document.getElementById('btn-report-explain').disabled = false;
                }
            }
        }

        async function onCancelReportExplanation() {
            try { await API.post('/api/reports/explanation/cancel', {run_id: reportExplanationRunId}); }
            catch (e) { showActionError('Explanation cancellation is unconfirmed', e, 'Check explanation status before cancelling again; the provider call may still be running.'); }
        }

        // Last script: tab restoration runs after the state below is initialized.

        // ── Build badge, run history, benchmark ─────────────────────────
        async function loadBuildBadge() {
            const el = document.getElementById('app-build-badge');
            if (!el) { return; }
            try {
                const v = await API.get('/api/status/version');
                const rev = v.short_revision || (v.revision || '').slice(0, 8);
                el.textContent = rev ? `${rev}${v.branch ? ' · ' + v.branch : ''}` : 'build unknown';
                el.title = `Running build ${v.revision || 'unknown'} (${v.revision_source || 'unknown'})`;
            } catch (e) {
                el.textContent = '';
            }
        }
        loadBuildBadge();

        function runStatusBadge(status) {
            const cls = { running: 'bg-primary', completed: 'bg-success', failed: 'bg-danger',
                          interrupted: 'bg-warning text-dark', cancelled: 'bg-secondary' }[status] || 'bg-secondary';
            return `<span class="badge ${cls}">${escapeHtml(status || 'unknown')}</span>`;
        }

        async function loadRunHistory() {
            const listEl = document.getElementById('runs-list');
            if (!listEl) { return; }
            try {
                const data = await API.get('/api/runs?limit=50');
                const runs = data.runs || [];
                if (!runs.length) {
                    listEl.innerHTML = '<div class="list-group-item text-muted small">No runs recorded yet. Every long task (script, review, personas, audio, training…) leaves a record here.</div>';
                    return;
                }
                listEl.innerHTML = runs.map(r => {
                    const started = r.started_at ? new Date(r.started_at).toLocaleString() : '';
                    const took = (r.started_at && r.finished_at)
                        ? `${Math.round((new Date(r.finished_at) - new Date(r.started_at)) / 1000)} s` : '';
                    return `<div class="list-group-item small py-2">
                        <div class="d-flex justify-content-between align-items-center">
                            <div><strong>${escapeHtml(r.task || '')}</strong> ${runStatusBadge(r.status)}</div>
                            <div class="text-muted">${escapeHtml(started)}${took ? ' · ' + escapeHtml(took) : ''}</div>
                        </div>
                        ${r.error ? `<div class="text-danger text-truncate" title="${escapeHtml(r.error)}">${escapeHtml(r.error)}</div>` : ''}
                    </div>`;
                }).join('');
            } catch (e) {
                listEl.innerHTML = `<div class="list-group-item text-danger small">${escapeHtml(getActionErrorMessage("Failed to load runs", e, "Reopen Reports to refresh the run list after checking the app connection."))}</div>`;
            }
        }

        let _benchmarkPreflightId = null;
        let _benchmarkPreflightManifest = null;
        let _benchmarkPreflightGeneration = 0;
        let _benchmarkStarting = false;
        let _benchmarkRunning = false;
        let _benchmarkRunRevision = 0;
        let _benchmarkPoll = null;
        let _benchmarkStatusPending = false;

        function _applyBenchmarkStartButton() {
            const ready = !!_benchmarkPreflightId
                && _benchmarkPreflightManifest === document.getElementById('benchmark-manifest')?.value;
            const button = document.getElementById('btn-benchmark-start');
            if (button) { button.disabled = _benchmarkStarting || _benchmarkRunning || !ready; }
            const help = document.getElementById('benchmark-start-help');
            const message = _benchmarkStarting ? 'Starting benchmark…'
                : _benchmarkRunning ? 'A benchmark is running. Start is available after it ends and the current manifest passes preflight.'
                    : ready ? 'Preflight passed — ready to start.'
                        : 'Start becomes available after a successful preflight of the current manifest. Run Preflight again after editing the manifest.';
            if (help && help.textContent !== message) { help.textContent = message; }
        }

        function onBenchmarkManifestChange() {
            _benchmarkPreflightGeneration++;
            _benchmarkPreflightId = null;
            _benchmarkPreflightManifest = null;
            _applyBenchmarkStartButton();
        }

        function readBenchmarkManifest() {
            const raw = document.getElementById('benchmark-manifest')?.value || '';
            try {
                const manifest = JSON.parse(raw);
                if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) { throw new Error('manifest must be a JSON object'); }
                return manifest;
            } catch (e) {
                showActionError('Manifest is not valid JSON', e, 'Keep the manifest text and correct the JSON syntax. It must be an object, then pass Preflight before Start.');
                return null;
            }
        }

        async function onBenchmarkPreflight() {
            if (_benchmarkStarting || _benchmarkRunning) { return; }
            onBenchmarkManifestChange();
            const generation = _benchmarkPreflightGeneration;
            const raw = document.getElementById('benchmark-manifest').value;
            const manifest = readBenchmarkManifest();
            const out = document.getElementById('benchmark-output');
            if (!manifest) { return; }
            try {
                const res = await API.post('/api/benchmark/preflight', { manifest });
                if (generation !== _benchmarkPreflightGeneration || raw !== document.getElementById('benchmark-manifest').value) { return; }
                _benchmarkPreflightId = res.preflight_id || null;
                _benchmarkPreflightManifest = raw;
                out.textContent = JSON.stringify(res, null, 2);
                _applyBenchmarkStartButton();
            } catch (e) {
                if (generation !== _benchmarkPreflightGeneration || raw !== document.getElementById('benchmark-manifest').value) { return; }
                out.textContent = getActionErrorMessage('Benchmark preflight failed', e, 'Keep the manifest text. Review the validation details, correct the manifest, then run Preflight again.');
            }
        }

        async function onBenchmarkStart() {
            if (_benchmarkStarting || _benchmarkRunning) { return; }
            if (_benchmarkPreflightManifest !== document.getElementById('benchmark-manifest').value) {
                onBenchmarkManifestChange();
                return;
            }
            const manifest = readBenchmarkManifest();
            if (!manifest || !_benchmarkPreflightId) { return; }
            const preflightId = _benchmarkPreflightId;
            _benchmarkStarting = true;
            _benchmarkRunRevision++;
            onBenchmarkManifestChange();
            try {
                await API.post('/api/benchmark/start', { manifest, preflight_id: preflightId });
                _benchmarkRunning = true;
                _benchmarkRunRevision++;
                _startBenchmarkStatusPolling();
                refreshBenchmarkStatus();
            } catch (e) {
                showActionError('Benchmark start is unconfirmed', e, 'Check benchmark status before starting again. Run Preflight again for the current manifest after any refusal.');
            } finally {
                _benchmarkStarting = false;
                _applyBenchmarkStartButton();
            }
        }

        async function onBenchmarkCancel() {
            try {
                await API.post('/api/benchmark/cancel', {});
            } catch (e) {
                showActionError('Benchmark cancellation is unconfirmed', e, 'Check benchmark status before cancelling again; work may still be running.');
            }
        }

        function _startBenchmarkStatusPolling() {
            if (_benchmarkPoll) { return; }
            _benchmarkPoll = _startPolling('benchmark', () => refreshBenchmarkStatus(true), {
                intervalMs: 3000,
                immediate: false,
                pauseWhenHidden: true,
                doneCheck: data => !!data && !data.running,
                onDone: () => { _benchmarkPoll = null; }
            });
        }

        async function refreshBenchmarkStatus(fromPoll = false) {
            const status = document.getElementById('benchmark-status');
            const out = document.getElementById('benchmark-output');
            const cancelBtn = document.getElementById('btn-benchmark-cancel');
            if (!status || _benchmarkStatusPending) { return; }
            if (document.hidden) {
                _startBenchmarkStatusPolling();
                return;
            }
            _benchmarkStatusPending = true;
            const revision = _benchmarkRunRevision;
            try {
                const s = await API.get('/api/benchmark/status');
                if (revision !== _benchmarkRunRevision) { return; }
                _benchmarkRunning = !!s.running;
                _applyBenchmarkStartButton();
                const done = (s.tasks || []).filter(t => t.status === 'done').length;
                status.textContent = s.running
                    ? `running · ${done}/${(s.tasks || []).length} fixtures`
                    : (s.status && s.status !== 'idle' ? s.status : 'idle');
                cancelBtn.style.display = s.running ? '' : 'none';
                if (s.running || (s.logs || []).length) {
                    out.textContent = (s.logs || []).slice(-40).join('\n');
                }
                if (s.running && !_benchmarkPoll) {
                    _startBenchmarkStatusPolling();
                } else if (!s.running && _benchmarkPoll) {
                    _benchmarkPoll();
                    _benchmarkPoll = null;
                }
                return s;
            } catch (e) {
                if (revision !== _benchmarkRunRevision) { return; }
                status.textContent = getActionErrorMessage('Benchmark status unavailable', e, 'Status polling will retry. Check the app connection and wait for a current status before starting another benchmark.');
                _startBenchmarkStatusPolling();
                if (fromPoll) { throw e; }
            } finally {
                _benchmarkStatusPending = false;
            }
        }

        restoreTab();
