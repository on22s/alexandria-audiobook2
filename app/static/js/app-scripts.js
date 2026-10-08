        // ── Saved Scripts ──────────────────────────────────────

        async function loadSavedScripts() {
            await _loadScriptList('saved-scripts-list', (scripts) => {
                const container = document.getElementById('saved-scripts-list');

                if (!scripts.length) {
                    container.innerHTML = '<p class="text-muted mb-0">No saved scripts yet. Generate a script above, or select Save Current to keep the current script here.</p>';
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
                        <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 py-2 border-bottom">
                            <div style="min-width:0;overflow-wrap:anywhere;flex:1 1 200px;">
                                <strong>${escapeHtml(s.name)}</strong>${voiceBadge}
                                <small class="text-muted ms-2">${date}</small>
                            </div>
                            <div class="flex-shrink-0">
                                <button class="btn btn-sm btn-outline-info me-1" onclick='auditSavedScript(${getInlineStringArgument(s.name)})'><i class="fas fa-stethoscope me-1"></i>Audit</button>
                                <button class="btn btn-sm btn-outline-success me-1" onclick='loadScript(${getInlineStringArgument(s.name)})'><i class="fas fa-upload me-1"></i>Load</button>
                                <button class="btn btn-sm btn-outline-danger" aria-label="${escapeHtml('Delete saved script ' + s.name)}" onclick='deleteScript(${getInlineStringArgument(s.name)})'><i class="fas fa-trash"></i></button>
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
                panel.textContent = getActionErrorMessage('Saved-script audit could not finish', error, 'Check that Alexandria is running, then use Audit again. The failed audit does not establish whether this script is ready.');
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
                showActionError("Error saving script", e, "Check the saved-script list for this name before saving again. Keep your current book open while checking.");
            }
        }

        async function loadScript(name) {
            if (!await showConfirm(`Load "${name}"? This will replace your current script and chunks.`, {title: 'Replace active book?', actionLabel: 'Load book', danger: true})) { return; }
            let request;
            let loading;
            const isCurrent = () => !request || loadScript.request === request;
            try {
                await flushVoiceSaves();
                if (!await ensureCastListEditsDiscardable()) { return; }
                request = {};
                loadScript.request = request;
                const previous = loadScript.pending || Promise.resolve();
                loading = (async () => {
                    await previous.catch(() => {});
                    if (!isCurrent()) { return; }
                    const loaded = await API.post('/api/scripts/load', { name });
                    if (!isCurrent()) { return; }
                    applyCurrentBookFilename(`${loaded.name}.json`);
                    document.getElementById('cast-list-panel').style.display = 'none';
                    clearCastListEditor();
                    clearCharacterAliases();
                    resetDesignerForm();
                    showToast(`Script "${name}" loaded.`, 'success');
                    clearVoiceSuggestions();
                    await Promise.all([loadCharacterAliases(false), loadCastList(false), loadChunks(true)]);
                    if (!isCurrent()) { return; }
                    await loadVoices();
                    if (!isCurrent()) { return; }
                    loadSavedScripts();
                    loadDesignedVoices();
                })();
                loadScript.pending = loading;
                await loading;
            } catch (e) {
                if (!isCurrent()) { return; }
                console.error('Error loading script:', e);
                showActionError("Error loading script", e, "Check the currently loaded book before trying Load again; the load may have completed before its reply was lost.");
            } finally {
                if (loading && loadScript.pending === loading) { loadScript.pending = null; }
            }
        }

        async function deleteScript(name) {
            if (!await showConfirm(`Delete saved script "${name}"? This cannot be undone.`, {title: 'Delete saved script?', actionLabel: 'Delete script', danger: true})) { return; }
            try {
                await API.del(`/api/scripts/${encodeURIComponent(name)}`);
                loadSavedScripts();
            } catch (e) {
                console.error('Error deleting script:', e);
                showActionError("Error deleting script", e, "Refresh the saved-script list to check whether this script was deleted before trying Delete again.");
            }
        }

        // --- Voice Designer ---
        window._designedVoicesCache = [];
        window._cloneVoicesCache = [];
        window._currentPreviewFile = null;
        window._designerPreviewInputs = null;

        async function loadDesignedVoices() {
            const request = (window._designedVoicesLoadRequest || 0) + 1;
            window._designedVoicesLoadRequest = request;
            try {
                const voices = await API.get('/api/voice_design/list');
                if (request !== window._designedVoicesLoadRequest) { return; }
                document.getElementById('designed-voices-load-status').textContent = '';
                window._designedVoicesCache = voices;
                const container = document.getElementById('designed-voices-list');

                if (!voices.length) {
                    container.innerHTML = '<p class="text-muted mb-0">No designed voices yet. Generate and save a preview above.</p>';
                    return;
                }

                container.innerHTML = `
                    <div class="table-responsive"><table class="table table-sm table-hover mb-0">
                        <thead><tr><th>Name</th><th>Description</th><th style="width:120px">Actions</th></tr></thead>
                        <tbody>
                            ${voices.map(v => `
                                <tr>
                                    <td style="overflow-wrap:anywhere;white-space:normal;"><strong>${escapeHtml(v.name)}</strong></td>
                                    <td class="text-muted" style="max-width:400px;overflow-wrap:anywhere;white-space:normal;">${escapeHtml(v.description)}</td>
                                    <td>
                                        <button class="btn btn-sm btn-outline-primary me-1" onclick='playDesignedVoice(${getInlineStringArgument(v.filename)})' title="Play" aria-label="${escapeHtml('Play designed voice ' + v.name)}"><i class="fas fa-play me-1"></i>Play</button>
                                        <button class="btn btn-sm btn-outline-secondary me-1" onclick='openDesignedVoiceForEdit(${getInlineStringArgument(v.id)})' title="Edit" aria-label="${escapeHtml('Edit designed voice ' + v.name)}"><i class="fas fa-edit me-1"></i>Edit</button>
                                        <button class="btn btn-sm btn-outline-danger" onclick='deleteDesignedVoice(${getInlineStringArgument(v.id)}, this)' title="Delete" aria-label="${escapeHtml('Delete designed voice ' + v.name)}"><i class="fas fa-trash me-1"></i>Delete</button>
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table></div>`;
            } catch (e) {
                console.error('Failed to load designed voices:', e);
                if (request !== window._designedVoicesLoadRequest) { return; }
                const message = 'Designed voices could not be loaded. The displayed list may be out of date. Check that Alexandria is still running, then use Refresh in Saved Voices.';
                document.getElementById('designed-voices-load-status').textContent = message;
                showToast(message, 'warning');
            }
        }

        function getDesignerFormSnapshot() {
            return JSON.stringify(['design-voice-name', 'design-source-name', 'design-description',
                'design-sample-text', 'design-alias-select'].map(id => document.getElementById(id).value));
        }

        function markDesignerFormClean() {
            window._designerCleanForm = getDesignerFormSnapshot();
            window._designerCleanPreview = window._currentPreviewFile || null;
        }

        function ensureDesignerEditsDiscardable() {
            const snapshot = getDesignerFormSnapshot();
            const baseline = window._designerCleanForm ?? JSON.stringify(
                ['design-voice-name', 'design-source-name', 'design-description', 'design-sample-text',
                    'design-alias-select'].map(id => document.getElementById(id).defaultValue || ''));
            if (snapshot === baseline && (window._currentPreviewFile || null) === (window._designerCleanPreview || null)) { return true; }
            if (window._designerDiscardPending) { return false; }
            const generation = window._designerGeneration || 0;
            const preview = window._currentPreviewFile;
            window._designerDiscardPending = true;
            return (async () => {
                try {
                    if (!await showConfirm('Discard unsaved Designer edits and open another voice? Save Voice first to keep them, or Cancel to continue editing.', {title: 'Discard Designer edits?', actionLabel: 'Discard edits', danger: true})) { return false; }
                    if (generation !== (window._designerGeneration || 0) || snapshot !== getDesignerFormSnapshot() || preview !== window._currentPreviewFile) {
                        showToast('The Designer changed while confirmation was open. Review your edits before switching voices.', 'warning');
                        return false;
                    }
                    return true;
                } finally { window._designerDiscardPending = false; }
            })();
        }

        function stopDesignedVoicePlayback(except = null) {
            for (const audio of [window._designedPlaybackAudio, document.getElementById('design-preview-audio')]) {
                if (audio && audio !== except) { audio.pause?.(); }
            }
        }

        function invalidateDesignerWork() {
            const previewStatus = document.getElementById('design-status');
            if (previewStatus) { previewStatus.innerHTML = ''; }
            const saveStatus = document.getElementById('design-save-status');
            if (saveStatus) { saveStatus.textContent = ''; }
            stopDesignedVoicePlayback();
            const audio = document.getElementById('design-preview-audio');
            audio?.removeAttribute?.('src');
            audio?.load?.();
            document.getElementById('design-preview-container').style.display = 'none';
            window._currentPreviewFile = null;
            window._designerPreviewInputs = null;
            window._editingDesignedVoiceId = null;
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
            window._designerPreviewInputs = null;
            const previewButton = document.getElementById('btn-design-preview');
            if (previewButton) {
                previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Generate Preview';
            }
            markDesignerFormClean();
        }

        function getDesignerSynthesisInputs() {
            return {
                description: document.getElementById('design-description').value.trim(),
                sample_text: document.getElementById('design-sample-text').value.trim()
            };
        }

        window.generateDesignPreview = async () => {
            const inputs = getDesignerSynthesisInputs();
            const description = inputs.description;
            const sampleText = inputs.sample_text;
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
            stopDesignedVoicePlayback();
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
                    ? '<span class="text-success"><i class="fas fa-check me-1"></i>New preview ready — click Save Voice to keep it</span>'
                    : '<span class="text-success"><i class="fas fa-check me-1"></i>Preview ready — click Save Voice to save this voice</span>';

                // Extract filename from URL for save
                window._currentPreviewFile = result.audio_url.split('/').pop().split('?')[0];
                window._designerPreviewInputs = {file: window._currentPreviewFile, ...inputs};
            } catch (e) {
                if (!isCurrent()) { return; }
                statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(getActionErrorMessage('Voice preview failed', e, 'Your description and sample text are retained. Check that the TTS service and selected model are available, then generate the preview again.'))}</span>`;
            } finally {
                if (isCurrent()) {
                    btn.disabled = false;
                }
            }
        };

        function getDesignerSaveSnapshot() {
            return JSON.stringify(['design-voice-name', 'design-source-name', 'design-description',
                'design-sample-text', 'design-alias-select'].map(id => document.getElementById(id).value)
                .concat([window._currentPreviewFile, window._editingDesignedVoiceId]));
        }

        window.saveDesignedVoice = async () => {
            if (window._designSavePending) { return; }
            const name = document.getElementById('design-voice-name').value.trim();
            if (!name) { showToast('Please enter a name for the voice.', 'warning'); return; }
            if (!window._currentPreviewFile) { showToast('Generate a preview first.', 'warning'); return; }

            const inputs = getDesignerSynthesisInputs();
            const previewInputs = window._designerPreviewInputs;
            if (!previewInputs || previewInputs.file !== window._currentPreviewFile
                || previewInputs.description !== inputs.description || previewInputs.sample_text !== inputs.sample_text) {
                showToast('The description or sample text changed. Generate a matching preview before saving.', 'warning');
                return;
            }

            const generation = window._designerGeneration || 0;
            const submitted = getDesignerSaveSnapshot();
            const previewFile = window._currentPreviewFile;
            const editingId = window._editingDesignedVoiceId || null;
            const source = document.getElementById('design-source-name').value;
            const aliasSelect = document.getElementById('design-alias-select');
            const selectedAlias = aliasSelect.value;
            const canApplyAlias = selectedAlias || aliasSelect.dataset.aliasLookupFailed !== 'true';
            const sourceCard = source ? document.querySelector(`.voice-card[data-voice="${CSS.escape(source)}"]`) : null;
            const sourceAlias = sourceCard?.querySelector('.alias-select');
            const sourceAliasValue = sourceAlias?.value;
            const button = document.getElementById('btn-design-save');
            const buttonLabel = button?.innerHTML;
            const wasDisabled = button?.disabled;
            const status = document.getElementById('design-save-status');
            window._designSavePending = true;
            if (button) { button.disabled = true; button.innerHTML = 'Saving…'; }
            if (status) { status.textContent = 'Saving voice…'; }
            try {
                const result = await API.post('/api/voice_design/save', {
                    name,
                    description: document.getElementById('design-description').value.trim(),
                    sample_text: document.getElementById('design-sample-text').value.trim(),
                    preview_file: previewFile,
                    voice_id: editingId
                });
                loadDesignedVoices();
                if (isDesignerGenerationCurrent(generation)) {
                    const unchanged = submitted === getDesignerSaveSnapshot();
                    if (unchanged && source && canApplyAlias) {
                        const card = document.querySelector(`.voice-card[data-voice="${CSS.escape(source)}"]`);
                        const aliasSel = card?.querySelector('.alias-select');
                        if (aliasSel && card === sourceCard && aliasSel === sourceAlias && aliasSel.value === sourceAliasValue) {
                            aliasSel.value = selectedAlias || '';
                            saveVoicesDebounced();
                        }
                    }
                    if (unchanged) {
                        resetDesignerForm();
                        if (status) { status.textContent = `Saved "${name}" to the voice library.`; }
                    } else {
                        if (window._currentPreviewFile === previewFile) {
                            window._currentPreviewFile = null;
                            window._designerPreviewInputs = null;
                            window._editingDesignedVoiceId = result.voice_id || editingId;
                        }
                        if (status) { status.textContent = `Saved "${name}". Later form edits were kept.${window._currentPreviewFile ? '' : ' Generate a preview before saving them.'}`; }
                    }
                }
                showToast(`Saved "${name}" to the voice library.`, 'success');
            } catch (e) {
                if (isDesignerGenerationCurrent(generation) && status) {
                    status.textContent = getActionErrorMessage('Saving designed voice failed', e, 'Your form is retained. Check the voice library for the saved voice before trying again.');
                }
                showActionError("Saving designed voice failed", e, "Your form is retained. Refresh the voice library and check whether the voice was saved before saving again.");
            } finally {
                window._designSavePending = false;
                if (button) { button.disabled = wasDisabled; button.innerHTML = buttonLabel; }
            }
        };

        function getLocalAudioUrl(rawPath, timestamp) {
            const path = rawPath.split('/').map(segment => encodeURIComponent(segment)).join('/');
            return `/${path}?t=${timestamp}`;
        }

        window.playDesignedVoice = async (filename) => {
            stopDesignedVoicePlayback();
            const audio = new Audio(getLocalAudioUrl(`designed_voices/${filename}`, Date.now()));
            window._designedPlaybackAudio = audio;
            try {
                await audio.play();
            } catch (error) {
                if (window._designedPlaybackAudio === audio) {
                    showToast('Could not play the saved voice. Check that Alexandria is running, then try Play again.', 'warning');
                }
            }
        };

        const pendingLibraryVoiceRemovals = new Set();

        async function applyLibraryVoiceRemoval(key, button, message, isCurrent, remove) {
            if (pendingLibraryVoiceRemovals.has(key) || button?.disabled) { return; }
            pendingLibraryVoiceRemovals.add(key);
            const wasDisabled = button?.disabled;
            if (button) { button.disabled = true; }
            try {
                if (!await showConfirm(message, {title: 'Remove library voice?', actionLabel: 'Remove voice', danger: true})) { return; }
                if (!isCurrent()) {
                    showToast('The selected voice changed. Review the current selection before deleting.', 'warning');
                    return;
                }
                await remove();
            } finally {
                pendingLibraryVoiceRemovals.delete(key);
                if (button) { button.disabled = wasDisabled; }
            }
        }

        window.deleteDesignedVoice = async (voiceId, button) => {
            const voice = (window._designedVoicesCache || []).find(v => v.id === voiceId);
            const name = voice?.name || voiceId;
            try {
                await applyLibraryVoiceRemoval(`design:${voiceId}`, button,
                    `Delete designed voice "${name}"? This removes its saved preview from the voice library.`,
                    () => true, async () => {
                        const res = await fetch(`/api/voice_design/${encodeURIComponent(voiceId)}`, {method: 'DELETE'});
                        if (!res.ok) { const err = await res.json(); showActionError('Designed voice deletion refused', {message: err.detail || 'Failed to delete.', status: res.status}, 'Refresh the voice library and check the selected voice before deleting again.'); return; }
                        showToast('Designed voice deleted. Characters that used it need a new reference.', 'warning');
                        try {
                            await loadDesignedVoices();
                            await loadVoices();
                        } catch (e) {
                            showActionError('Voice deleted; list refresh failed', e, 'Reload the voice library to refresh the list. The deletion was confirmed; do not delete again.');
                        }
                    });
            } catch (e) {
                showActionError("Deleting designed voice failed", e, "Refresh the voice library to check whether the voice was deleted before trying again.");
            }
        };

        window.openDesignedVoiceForEdit = async (voiceId) => {
            const admission = ensureDesignerEditsDiscardable();
            if (admission !== true && !await admission) { return; }
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

                markDesignerFormClean();
                const lookupSnapshot = getDesignerFormSnapshot();
                let formUnchanged = false;
                // Try to read existing alias from voices config
                try {
                    const voices = await API.get('/api/voices');
                    if (!isDesignerGenerationCurrent(generation)) { return; }
                    formUnchanged = lookupSnapshot === getDesignerFormSnapshot();
                    if (formUnchanged) {
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
                    }
                } catch (e) {
                    if (!isDesignerGenerationCurrent(generation)) { return; }
                    formUnchanged = lookupSnapshot === getDesignerFormSnapshot();
                    aliasSelect.dataset.aliasLookupFailed = 'true';
                }

                // Update preview audio and current preview file
                const audio = document.getElementById('design-preview-audio');
                audio.src = getLocalAudioUrl(`designed_voices/${voice.filename}`, Date.now());
                window._currentPreviewFile = voice.filename;
                window._designerPreviewInputs = {file: voice.filename,
                    description: (voice.description || '').trim(), sample_text: (voice.sample_text || '').trim()};
                window._editingDesignedVoiceId = voice.id;
                document.getElementById('design-preview-container').style.display = 'block';

                const previewButton = document.getElementById('btn-design-preview');
                if (previewButton) {
                    previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Re-design Voice';
                }

                if (formUnchanged) { markDesignerFormClean(); }
                // Focus description for quick edits
                document.getElementById('design-description').focus();
                showToast('Loaded designed voice for editing', 'info');
            } catch (e) {
                if (!isDesignerGenerationCurrent(generation)) { return; }
                showActionError("Opening designed voice failed", e, "Refresh the voice library and check that the saved voice is available, then open it again.");
            }
        };

        window.openVoiceDesignEditor = async (button) => {
            const admission = ensureDesignerEditsDiscardable();
            if (admission !== true && !await admission) { return; }
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
            window._designerPreviewInputs = null;
            document.getElementById('design-preview-container').style.display = 'none';

            const previewButton = document.getElementById('btn-design-preview');
            if (previewButton) {
                previewButton.innerHTML = '<i class="fas fa-wand-magic-sparkles me-1"></i>Re-design Voice';
            }

            const statusEl = document.getElementById('design-status');
            if (statusEl) {
                statusEl.innerHTML = '<span class="text-muted"><i class="fas fa-info me-1"></i>Edit the description, then generate a preview.</span>';
            }
            markDesignerFormClean();
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
                fields: document.getElementById('clone-import-fields'),
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
            f.submit.disabled = _cloneImportPendingGeneration === _cloneImportGeneration || !(f.refText.value.trim() && f.rightsConfirmed.checked);
        }

        let _cloneImportGeneration = 0;
        let _cloneImportPendingGeneration = null;
        function _openCloneImportModal(file) {
            const generation = ++_cloneImportGeneration;
            const f = _cloneImportFields();
            document.getElementById('clone-import-filename').textContent = file.name;
            f.fields.disabled = false;
            f.submit.textContent = 'Import';
            document.getElementById('clone-import-status').textContent = '';
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
                f.fields.disabled = false;
                _cloneImportGeneration++;
                f.submit.onclick = null;
            }, { once: true });
            f.submit.onclick = async () => {
                if (generation !== _cloneImportGeneration || _cloneImportPendingGeneration === generation) { return; }
                if (!f.refText.value.trim() || !f.rightsConfirmed.checked) {
                    _refreshCloneImportSubmit();
                    return;
                }
                _cloneImportPendingGeneration = generation;
                f.fields.disabled = true;
                f.submit.disabled = true;
                f.submit.textContent = 'Importing…';
                document.getElementById('clone-import-status').textContent = `Uploading and checking "${file.name}"…`;
                try {
                    const ok = await _submitCloneImport(file);
                    if (generation !== _cloneImportGeneration) { return; }
                    document.getElementById('clone-import-status').textContent = ok ? `Imported "${file.name}".` : 'Import did not complete. Review the error, then retry.';
                    if (ok) {
                        f.fields.disabled = false;
                        f.submit.textContent = 'Import';
                        f.submit.onclick = null;
                        modal.hide();
                    }
                } finally {
                    if (_cloneImportPendingGeneration === generation) { _cloneImportPendingGeneration = null; }
                    if (generation === _cloneImportGeneration) {
                        f.fields.disabled = false;
                        f.submit.textContent = 'Import';
                        _refreshCloneImportSubmit();
                    }
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
            let imported = false;
            try {
                const res = await fetch('/api/clone_voices/upload', { method: 'POST', body: formData });
                if (!res.ok) {
                    const err = await res.json();
                    showActionError('Clone import refused', {message: err.detail || 'Import failed', status: res.status}, 'Keep the selected file and import fields. Correct the transcript, source or rights details identified by the validation error before importing again.');
                    return false;
                }
                imported = true;
                const result = await res.json();
                window._cloneVoicesCache = await API.get('/api/clone_voices/list');
                await loadVoices();
                const m = result.measures || {};
                showToast(`Imported "${file.name}" (${m.duration_s ?? '?'} s, 24 kHz mono)`, 'success');
                return true;
            } catch (e) {
                if (imported) {
                    console.error('Imported clone voice refresh failed:', e);
                    showToast(`Imported "${file.name}", but the voice list could not be refreshed. Do not import again. Check that Alexandria is running, then reopen Voices.`, 'warning');
                    return true;
                }
                showActionError("Importing clone voice failed", e, "Your selected file and import fields are retained. Check the voice library before retrying; correct any validation details below.");
                return false;
            }
        }

        window.playCloneVoice = async (btn) => {
            const card = btn.closest('.card-body');
            const refAudio = card.querySelector('.ref-audio').value;
            if (refAudio) {
                try {
                    const audio = new Audio(getLocalAudioUrl(refAudio, Date.now()));
                    await audio.play();
                } catch (error) {
                    showToast('Could not play the clone reference. Check that Alexandria is running, then try Play again.', 'warning');
                }
            }
        };

        window.deleteCloneVoice = async (btn) => {
            const card = btn.closest('.card-body');
            const select = card.querySelector('.designed-voice-select');
            const val = select.value;
            if (!val.startsWith('clone:')) { return; }
            const voiceId = val.substring(6);
            const voice = (window._cloneVoicesCache || []).find(v => v.id === voiceId);
            const name = voice?.name || voiceId;
            try {
                await applyLibraryVoiceRemoval(val, btn,
                    `Delete clone voice "${name}"? This removes its saved reference from the voice library.`,
                    () => select.value === val && card.isConnected !== false, async () => {
                        const res = await fetch(`/api/clone_voices/${encodeURIComponent(voiceId)}`, { method: 'DELETE' });
                        if (!res.ok) { const err = await res.json(); showActionError('Clone voice deletion refused', {message: err.detail || 'Failed to delete', status: res.status}, 'Refresh the voice library and check the selected clone voice before deleting again.'); return; }
                        window._cloneVoicesCache = await API.get('/api/clone_voices/list');
                        await loadVoices();
                        showToast('Clone voice deleted', 'success');
                    });
            } catch (e) {
                showActionError("Deleting clone voice failed", e, "Refresh the voice library to check whether the clone voice was deleted before trying again.");
            }
        };

        // --- LoRA Training ---
        window._loraModelsCache = [];
