        // ── Reports ────────────────────────────────────────────────────
        async function loadReports() {
            const listEl = document.getElementById('reports-list');
            if (!listEl) { return; }
            try {
                const reports = await API.get('/api/reports');
                if (!reports.length) {
                    listEl.innerHTML = '<div class="list-group-item text-muted small">No reports yet. Reports are generated automatically each time a script review finishes.</div>';
                    return;
                }
                listEl.innerHTML = reports.map(r => {
                    const when = r.mtime ? new Date(r.mtime * 1000).toLocaleString() : '';
                    const icon = r.type === 'batch' ? 'fa-layer-group' : 'fa-file-lines';
                    const label = r.type === 'batch' ? 'Batch review' : 'Review';
                    return `<a href="#" class="list-group-item list-group-item-action report-list-item" data-filename="${escapeHtml(r.filename)}" onclick="viewReport('${escapeHtml(r.filename)}'); return false;">
                        <div><i class="fas ${icon} me-2"></i>${label}</div>
                        <div class="text-muted small">${escapeHtml(when)}</div>
                    </a>`;
                }).join('');
            } catch (e) {
                listEl.innerHTML = `<div class="list-group-item text-danger small">Failed to load reports: ${escapeHtml(e.message || String(e))}</div>`;
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
                listEl.innerHTML = `<div class="list-group-item text-danger small">Failed to load checkpoints: ${escapeHtml(e.message || String(e))}</div>`;
            }
        }

        async function viewReport(filename) {
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
                const html = marked.parse(markdown);
                contentEl.innerHTML = DOMPurify.sanitize(html);
            } catch (e) {
                contentEl.innerHTML = `<p class="text-danger">Failed to load report: ${escapeHtml(e.message || String(e))}</p>`;
            }
        }
