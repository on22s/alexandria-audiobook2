        // ── Saved Scripts ──────────────────────────────────────

        async function loadSavedScripts() {
            await _loadScriptList('saved-scripts-list', (scripts) => {
                const container = document.getElementById('saved-scripts-list');

                if (!scripts.length) {
                    container.innerHTML = '<p class="text-muted mb-0">No saved scripts yet.</p>';
                    return;
                }

                container.innerHTML = scripts.map(s => {
                    const date = new Date(s.created * 1000).toLocaleDateString('en-US', {
                        month: 'short', day: 'numeric', year: 'numeric'
                    });
                    const voiceBadge = s.has_voice_config
                        ? '<span class="badge bg-info ms-2" title="Includes voice configuration">voices</span>'
                        : '';
                    return `
                        <div class="d-flex align-items-center justify-content-between py-2 border-bottom">
                            <div>
                                <strong>${escapeHtml(s.name)}</strong>${voiceBadge}
                                <small class="text-muted ms-2">${date}</small>
                            </div>
                            <div>
                                <button class="btn btn-sm btn-outline-info me-1" onclick='auditSavedScript(${getInlineStringArgument(s.name)})'><i class="fas fa-stethoscope me-1"></i>Audit</button>
                                <button class="btn btn-sm btn-outline-success me-1" onclick='loadScript(${getInlineStringArgument(s.name)})'><i class="fas fa-upload me-1"></i>Load</button>
                                <button class="btn btn-sm btn-outline-danger" onclick='deleteScript(${getInlineStringArgument(s.name)})'><i class="fas fa-trash"></i></button>
                            </div>
                        </div>`;
                }).join('');
            });
        }

        let _savedScriptAuditRequest = 0;
        async function auditSavedScript(name) {
            const requestId = ++_savedScriptAuditRequest;
            const panel = document.getElementById('saved-script-preflight');
            panel.style.display = 'block';
            panel.className = 'alert alert-secondary small mt-3 mb-0';
            panel.textContent = `Auditing ${name}…`;
            try {
                const report = await API.post(`/api/scripts/${encodeURIComponent(name)}/preflight`, {});
                if (requestId !== _savedScriptAuditRequest) { return; }
                const counts = report.counts || {};
                const findings = report.findings || [];
                panel.className = `alert ${counts.blocking ? 'alert-danger' : findings.length ? 'alert-warning' : 'alert-success'} small mt-3 mb-0`;
                const items = findings.map(item => {
                    const entries = (item.entry_numbers || []).join(', ');
                    const location = entries ? `Entry ${escapeHtml(entries)}: ` : '';
                    const message = escapeHtml(item.message || item.code || 'Review this finding.');
                    let detailsText = '';
                    if (item.code === 'nonprose_speech_risk') {
                        const details = item.details || {};
                        const categories = (details.categories || []).join(', ');
                        detailsText = ` ${escapeHtml(categories)} — preview: ${escapeHtml(details.normalized_preview || '')}. Audio validation: ${escapeHtml((details.validation || {}).status || 'unknown')}.`;
                    }
                    return `<li>${location}${message}${detailsText}</li>`;
                }).join('');
                panel.innerHTML = `<strong>${escapeHtml(name)}:</strong> ${counts.blocking || 0} blocking, ${counts.manual_review || 0} manual review.`
                    + (items ? `<ul class="mt-1 mb-0">${items}</ul>` : ' No findings detected.');
            } catch (error) {
                if (requestId !== _savedScriptAuditRequest) { return; }
                panel.className = 'alert alert-danger small mt-3 mb-0';
                panel.textContent = `Audit failed: ${error.message || error}`;
            }
        }

        async function saveScript() {
            const nameInput = document.getElementById('save-script-name');
            const name = nameInput.value.trim();
            if (!name) {
                showToast('Please enter a name for the script.', 'warning');
                return;
            }
            try {
                await flushVoiceSaves();
                await API.post('/api/scripts/save', { name });
                nameInput.value = '';
                loadSavedScripts();
            } catch (e) {
                console.error('Error saving script:', e);
                showToast('Error saving script: ' + e.message, 'error');
            }
        }

        async function loadScript(name) {
            if (!await showConfirm(`Load "${name}"? This will replace your current script and chunks.`)) { return; }
            try {
                await flushVoiceSaves();
                const loaded = await API.post('/api/scripts/load', { name });
                applyCurrentBookFilename(`${loaded.name}.json`);
                clearCharacterAliases();
                resetDesignerForm();
                showToast(`Script "${name}" loaded.`, 'success');
                clearVoiceSuggestions();
                await loadCharacterAliases(false);
                await loadChunks(true);
                await loadVoices();
                loadSavedScripts();
                loadDesignedVoices();
            } catch (e) {
                console.error('Error loading script:', e);
                showToast('Error loading script: ' + e.message, 'error');
            }
        }

        async function deleteScript(name) {
            if (!await showConfirm(`Delete saved script "${name}"? This cannot be undone.`)) { return; }
            try {
                await API.del(`/api/scripts/${encodeURIComponent(name)}`);
                loadSavedScripts();
            } catch (e) {
                console.error('Error deleting script:', e);
                showToast('Error deleting script: ' + e.message, 'error');
            }
        }

        // --- Voice Designer ---
        window._designedVoicesCache = [];
        window._cloneVoicesCache = [];
        window._currentPreviewFile = null;

        async function loadDesignedVoices() {
            try {
                const voices = await API.get('/api/voice_design/list');
                window._designedVoicesCache = voices;
                const container = document.getElementById('designed-voices-list');

                if (!voices.length) {
                    container.innerHTML = '<p class="text-muted mb-0">No designed voices yet. Generate and save a preview above.</p>';
                    return;
                }

                container.innerHTML = `
                    <table class="table table-sm table-hover mb-0">
                        <thead><tr><th>Name</th><th>Description</th><th style="width:120px">Actions</th></tr></thead>
                        <tbody>
                            ${voices.map(v => `
                                <tr>
                                    <td><strong>${escapeHtml(v.name)}</strong></td>
                                    <td class="text-muted" style="max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(v.description)}</td>
                                    <td>
                                        <button class="btn btn-sm btn-outline-primary me-1" onclick='playDesignedVoice(${getInlineStringArgument(v.filename)})' title="Play"><i class="fas fa-play"></i></button>
                                        <button class="btn btn-sm btn-outline-secondary me-1" onclick='openDesignedVoiceForEdit(${getInlineStringArgument(v.id)})' title="Edit"><i class="fas fa-edit"></i></button>
                                        <button class="btn btn-sm btn-outline-danger" onclick='deleteDesignedVoice(${getInlineStringArgument(v.id)})' title="Delete"><i class="fas fa-trash"></i></button>
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>`;
            } catch (e) {
                console.error('Failed to load designed voices:', e);
                showToast('Failed to load designed voices', 'error');
            }
        }

        function invalidateDesignerWork() {
            window._designerGeneration = (window._designerGeneration || 0) + 1;
            window._designPreviewRequest = null;
            const previewButton = document.getElementById('btn-design-preview');
            if (previewButton) {
                previewButton.disabled = false;
            }
            return window._designerGeneration;
        }

        function isDesignerGenerationCurrent(generation) {
            return generation === (window._designerGeneration || 0);
        }

        function resetDesignerForm() {
            invalidateDesignerWork();
            document.getElementById('design-voice-name').value = '';
            document.getElementById('design-source-name').value = '';
            document.getElementById('design-description').value = '';
            document.getElementById('design-sample-text').value = '';
            document.getElementById('design-alias-select').innerHTML = '<option value="">-- None --</option>';
            document.getElementById('design-preview-container').style.display = 'none';
            document.getElementById('design-status').innerHTML = '';
            window._editingDesignedVoiceId = null;
            window._currentPreviewFile = null;
            const previewButton = document.getElementById('btn-design-preview');
            if (previewButton) {
                previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Generate Preview';
            }
        }

        window.generateDesignPreview = async () => {
            const description = document.getElementById('design-description').value.trim();
            const sampleText = document.getElementById('design-sample-text').value.trim();
            const statusEl = document.getElementById('design-status');
            const previewContainer = document.getElementById('design-preview-container');

            if (!description) { showToast('Please enter a voice description.', 'warning'); return; }
            if (!sampleText) { showToast('Please enter sample text.', 'warning'); return; }

            const generation = window._designerGeneration || 0;
            const request = {};
            window._designPreviewRequest = request;
            const isCurrent = () => isDesignerGenerationCurrent(generation)
                && window._designPreviewRequest === request;
            const btn = document.getElementById('btn-design-preview');
            btn.disabled = true;
            statusEl.innerHTML = window._editingDesignedVoiceId
                ? '<i class="fas fa-spinner fa-spin me-1"></i>Re-designing preview (this may take a moment)...'
                : '<i class="fas fa-spinner fa-spin me-1"></i>Generating preview (this may take a moment on first run)...';
            previewContainer.style.display = 'none';

            try {
                const result = await API.post('/api/voice_design/preview', {
                    description: description,
                    sample_text: sampleText
                });

                if (!isCurrent()) { return; }
                const audio = document.getElementById('design-preview-audio');
                audio.src = result.audio_url + '?t=' + Date.now();
                previewContainer.style.display = 'block';
                statusEl.innerHTML = window._editingDesignedVoiceId
                    ? '<span class="text-success"><i class="fas fa-check me-1"></i>Voice re-designed</span>'
                    : '<span class="text-success"><i class="fas fa-check me-1"></i>Preview ready</span>';

                // Extract filename from URL for save
                window._currentPreviewFile = result.audio_url.split('/').pop().split('?')[0];
            } catch (e) {
                if (!isCurrent()) { return; }
                statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>Failed: ${escapeHtml(e.message)}</span>`;
            } finally {
                if (isCurrent()) {
                    btn.disabled = false;
                }
            }
        };

        window.saveDesignedVoice = async () => {
            if (window._designSavePending) { return; }
            const name = document.getElementById('design-voice-name').value.trim();
            if (!name) { showToast('Please enter a name for the voice.', 'warning'); return; }
            if (!window._currentPreviewFile) { showToast('Generate a preview first.', 'warning'); return; }

            window._designSavePending = true;
            try {
                const editingId = window._editingDesignedVoiceId || null;
                await API.post('/api/voice_design/save', {
                    name: name,
                    description: document.getElementById('design-description').value.trim(),
                    sample_text: document.getElementById('design-sample-text').value.trim(),
                    preview_file: window._currentPreviewFile,
                    voice_id: editingId
                });
                document.getElementById('design-voice-name').value = '';
                window._editingDesignedVoiceId = null;
                const previewButton = document.getElementById('btn-design-preview');
                if (previewButton) {
                    previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Generate Preview';
                }
                loadDesignedVoices();
                // If we're editing a generated persona, propagate alias choice to voice card and trigger save
                const source = document.getElementById('design-source-name').value;
                const aliasSelect = document.getElementById('design-alias-select');
                const selectedAlias = aliasSelect.value;
                if (source && (selectedAlias || aliasSelect.dataset.aliasLookupFailed !== 'true')) {
                    const card = document.querySelector(`.voice-card[data-voice="${CSS.escape(source)}"]`);
                    if (card) {
                        const aliasSel = card.querySelector('.alias-select');
                        if (aliasSel) {
                            aliasSel.value = selectedAlias || '';
                            // Trigger save via existing auto-save debounce
                            saveVoicesDebounced();
                        }
                    }
                }
            } catch (e) {
                showToast('Error saving voice: ' + e.message, 'error');
            } finally {
                window._designSavePending = false;
            }
        };

        function getLocalAudioUrl(rawPath, timestamp) {
            const path = rawPath.split('/').map(segment => encodeURIComponent(segment)).join('/');
            return `/${path}?t=${timestamp}`;
        }

        window.playDesignedVoice = (filename) => {
            const audio = new Audio(getLocalAudioUrl(`designed_voices/${filename}`, Date.now()));
            audio.play();
        };

        window.deleteDesignedVoice = async (voiceId) => {
            if (!await showConfirm('Delete this designed voice?')) return;
            try {
                const res = await fetch(`/api/voice_design/${encodeURIComponent(voiceId)}`, {method: 'DELETE'});
                if (!res.ok) { const err = await res.json(); showToast(err.detail || 'Failed to delete.', 'error'); return; }
                loadDesignedVoices();
            } catch (e) {
                showToast('Error deleting voice: ' + e.message, 'error');
            }
        };

        window.openDesignedVoiceForEdit = async (voiceId) => {
            const generation = invalidateDesignerWork();
            try {
                const voice = (window._designedVoicesCache || []).find(v => v.id === voiceId);
                if (!voice) {
                    showToast('Designed voice not found', 'error');
                    return;
                }

                // Switch to Designer tab
                document.querySelector('[data-tab="designer"]').click();

                // Populate fields
                document.getElementById('design-voice-name').value = voice.name || '';
                document.getElementById('design-source-name').value = voice.name || '';
                document.getElementById('design-description').value = voice.description || '';
                document.getElementById('design-sample-text').value = voice.sample_text || '';

                // Populate alias dropdown
                const aliasSelect = document.getElementById('design-alias-select');
                aliasSelect.innerHTML = '<option value="">-- None --</option>';
                aliasSelect.dataset.aliasLookupFailed = 'false';
                aliasSelect.onchange = () => { aliasSelect.dataset.aliasLookupFailed = 'false'; };
                const names = (window._voicesNames || []).filter(n => n !== voice.name);
                names.forEach(n => {
                    const opt = document.createElement('option');
                    opt.value = n;
                    opt.text = n;
                    aliasSelect.appendChild(opt);
                });

                // Try to read existing alias from voices config
                try {
                    const voices = await API.get('/api/voices');
                    if (!isDesignerGenerationCurrent(generation)) { return; }
                    aliasSelect.innerHTML = '<option value="">-- None --</option>';
                    voices.filter(v => v.name !== voice.name).forEach(v => {
                        const opt = document.createElement('option');
                        opt.value = v.name;
                        opt.text = v.name;
                        aliasSelect.appendChild(opt);
                    });
                    const entry = voices.find(v => v.name === voice.name);
                    if (entry && entry.config && entry.config.alias_of) {
                        if (!Array.from(aliasSelect.options).some(option => option.value === entry.config.alias_of)) {
                            const opt = document.createElement('option');
                            opt.value = entry.config.alias_of;
                            opt.text = entry.config.alias_of;
                            aliasSelect.appendChild(opt);
                        }
                        aliasSelect.value = entry.config.alias_of;
                    } else {
                        aliasSelect.value = '';
                    }
                } catch (e) {
                    if (!isDesignerGenerationCurrent(generation)) { return; }
                    aliasSelect.dataset.aliasLookupFailed = 'true';
                }

                // Update preview audio and current preview file
                const audio = document.getElementById('design-preview-audio');
                audio.src = getLocalAudioUrl(`designed_voices/${voice.filename}`, Date.now());
                window._currentPreviewFile = voice.filename;
                window._editingDesignedVoiceId = voice.id;
                document.getElementById('design-preview-container').style.display = 'block';

                const previewButton = document.getElementById('btn-design-preview');
                if (previewButton) {
                    previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Re-design Voice';
                }

                // Focus description for quick edits
                document.getElementById('design-description').focus();
                showToast('Loaded designed voice for editing', 'info');
            } catch (e) {
                if (!isDesignerGenerationCurrent(generation)) { return; }
                showToast('Failed to load voice for edit: ' + e.message, 'error');
            }
        };

        window.openVoiceDesignEditor = (button) => {
            const card = button.closest('.card-body');
            const cardRoot = button.closest('.voice-card');
            const voiceName = cardRoot ? cardRoot.dataset.voice : '';
            const description = card ? (card.querySelector('.design-description')?.value || '') : '';

            document.querySelector('[data-tab="designer"]').click();
            resetDesignerForm();
            document.getElementById('design-voice-name').value = voiceName;
            document.getElementById('design-source-name').value = voiceName;
            document.getElementById('design-description').value = description;
            document.getElementById('design-sample-text').value = card?.querySelector('.ref-text')?.value || '';
            const cardAlias = card?.querySelector('.alias-select');
            const aliasSelect = document.getElementById('design-alias-select');
            aliasSelect.innerHTML = cardAlias?.innerHTML || '<option value="">-- None --</option>';
            aliasSelect.value = cardAlias?.value || '';
            aliasSelect.dataset.aliasLookupFailed = 'false';
            window._editingDesignedVoiceId = null;
            window._currentPreviewFile = null;
            document.getElementById('design-preview-container').style.display = 'none';

            const previewButton = document.getElementById('btn-design-preview');
            if (previewButton) {
                previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Re-design Voice';
            }

            const statusEl = document.getElementById('design-status');
            if (statusEl) {
                statusEl.innerHTML = '<span class="text-muted"><i class="fas fa-info me-1"></i>Edit the description, then generate a preview.</span>';
            }
        };

        window.onDesignedVoiceSelect = (select) => {
            const card = select.closest('.card-body');
            const refText = card.querySelector('.ref-text');
            const refAudio = card.querySelector('.ref-audio');
            const playBtn = card.querySelector('.clone-play-btn');
            const deleteBtn = card.querySelector('.clone-delete-btn');
            const val = select.value;

            if (val === '' || val === '__manual__') {
                refAudio.readOnly = false;
                if (val === '__manual__') {
                    refAudio.value = '';
                    refText.value = '';
                }
                if (playBtn) playBtn.style.display = 'none';
                if (deleteBtn) deleteBtn.style.display = 'none';
                refAudio.focus();
                return;
            }

            const isClone = val.startsWith('clone:');
            // Plain IDs are legacy designed-voice values.
            const voiceId = isClone ? val.substring(6) : val.startsWith('design:') ? val.substring(7) : val;
            const voices = isClone ? window._cloneVoicesCache : window._designedVoicesCache;
            const voice = (voices || []).find(v => v.id === voiceId);
            if (!voice) {
                const previous = getLibraryVoiceReference(refAudio.value);
                const previousValue = previous ? `${previous.type}:${previous.id}` : refAudio.value ? '__manual__' : '';
                select.value = previousValue;
                if (select.value !== previousValue) { select.value = refAudio.value ? '__manual__' : ''; }
                refAudio.readOnly = !!previous && select.value === previousValue;
                if (playBtn) { playBtn.style.display = refAudio.value ? 'inline-block' : 'none'; }
                if (deleteBtn) { deleteBtn.style.display = previous?.type === 'clone' && refAudio.readOnly ? 'inline-block' : 'none'; }
                showToast('The selected voice reference is no longer available. Previous reference kept; refresh Voices and choose another.', 'warning');
                return;
            }
            refAudio.value = `${isClone ? 'clone_voices' : 'designed_voices'}/${voice.filename}`;
            refText.value = isClone ? voice.ref_text || '' : voice.sample_text;
            refAudio.readOnly = true;
            if (playBtn) { playBtn.style.display = 'inline-block'; }
            if (deleteBtn) { deleteBtn.style.display = isClone ? 'inline-block' : 'none'; }
            saveVoicesDebounced();
        };

        // --- Clone Voice Upload Handlers ---

        window.uploadCloneVoice = (btn) => {
            const card = btn.closest('.card-body');
            card.querySelector('.clone-voice-file-input').click();
        };

        // The import needs the exact transcript and a rights confirmation, so the
        // file picker hands off to a small modal; the server refuses without them.
        window.handleCloneVoiceUpload = (input) => {
            const file = input.files[0];
            if (!file) { return; }
            input.value = '';
            _openCloneImportModal(file);
        };

        function _cloneImportFields() {
            return {
                refText: document.getElementById('clone-import-ref-text'),
                sourceTitle: document.getElementById('clone-import-source-title'),
                sourceUrl: document.getElementById('clone-import-source-url'),
                rightsBasis: document.getElementById('clone-import-rights-basis'),
                rightsConfirmed: document.getElementById('clone-import-rights-confirmed'),
                submit: document.getElementById('clone-import-submit'),
            };
        }

        function _refreshCloneImportSubmit() {
            const f = _cloneImportFields();
            f.submit.disabled = !(f.refText.value.trim() && f.rightsConfirmed.checked);
        }

        let _cloneImportGeneration = 0;
        function _openCloneImportModal(file) {
            const generation = ++_cloneImportGeneration;
            const f = _cloneImportFields();
            document.getElementById('clone-import-filename').textContent = file.name;
            f.refText.value = '';
            f.sourceTitle.value = '';
            f.sourceUrl.value = '';
            f.rightsBasis.value = '';
            f.rightsConfirmed.checked = false;
            f.refText.oninput = _refreshCloneImportSubmit;
            f.rightsConfirmed.onchange = _refreshCloneImportSubmit;
            _refreshCloneImportSubmit();
            const modalEl = document.getElementById('cloneImportModal');
            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modalEl.addEventListener('hidden.bs.modal', () => {
                if (generation !== _cloneImportGeneration) { return; }
                _cloneImportGeneration++;
                f.submit.onclick = null;
            }, { once: true });
            f.submit.onclick = async () => {
                f.submit.disabled = true;
                const ok = await _submitCloneImport(file);
                if (generation !== _cloneImportGeneration) { return; }
                if (ok) {
                    f.submit.onclick = null;
                    modal.hide();
                } else {
                    _refreshCloneImportSubmit();
                }
            };
            modal.show();
        }

        async function _submitCloneImport(file) {
            const f = _cloneImportFields();
            const formData = new FormData();
            formData.append('file', file);
            formData.append('ref_text', f.refText.value.trim());
            formData.append('source_title', f.sourceTitle.value.trim());
            formData.append('source_url', f.sourceUrl.value.trim());
            formData.append('rights_basis', f.rightsBasis.value.trim());
            formData.append('rights_confirmed', f.rightsConfirmed.checked ? 'true' : 'false');
            try {
                const res = await fetch('/api/clone_voices/upload', { method: 'POST', body: formData });
                if (!res.ok) {
                    const err = await res.json();
                    showToast(err.detail || 'Import failed', 'error');
                    return false;
                }
                const result = await res.json();
                window._cloneVoicesCache = await API.get('/api/clone_voices/list');
                await loadVoices();
                const m = result.measures || {};
                showToast(`Imported "${file.name}" (${m.duration_s ?? '?'} s, 24 kHz mono)`, 'success');
                return true;
            } catch (e) {
                showToast('Import failed: ' + e.message, 'error');
                return false;
            }
        }

        window.playCloneVoice = (btn) => {
            const card = btn.closest('.card-body');
            const refAudio = card.querySelector('.ref-audio').value;
            if (refAudio) {
                const audio = new Audio(getLocalAudioUrl(refAudio, Date.now()));
                audio.play();
            }
        };

        window.deleteCloneVoice = async (btn) => {
            if (!await showConfirm('Delete this uploaded clone voice?')) { return; }
            const card = btn.closest('.card-body');
            const select = card.querySelector('.designed-voice-select');
            const val = select.value;
            if (!val.startsWith('clone:')) { return; }
            const voiceId = val.substring(6);

            try {
                const res = await fetch(`/api/clone_voices/${encodeURIComponent(voiceId)}`, { method: 'DELETE' });
                if (!res.ok) { const err = await res.json(); showToast(err.detail || 'Failed to delete', 'error'); return; }

                window._cloneVoicesCache = await API.get('/api/clone_voices/list');
                await loadVoices();
                showToast('Clone voice deleted', 'success');
            } catch (e) {
                showToast('Error: ' + e.message, 'error');
            }
        };

        // --- LoRA Training ---
        window._loraModelsCache = [];
