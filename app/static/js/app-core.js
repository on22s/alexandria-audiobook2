        // --- Toast & Confirm utilities ---
        let toastSequence = 0;
        let confirmQueue = Promise.resolve();
        function showToast(message, type = 'info', duration = type === 'error' ? 10000 : 4000) {
            const container = document.getElementById('toast-container');
            const bgClass = type === 'success' ? 'bg-success' :
                           type === 'error' ? 'bg-danger' :
                           type === 'warning' ? 'bg-warning text-dark' : 'bg-info';
            const liveRole = type === 'error' ? 'alert' : 'status';
            const livePriority = type === 'error' ? 'assertive' : 'polite';
            const id = 'toast-' + Date.now() + '-' + (++toastSequence);
            const html = `
                <div id="${id}" class="toast align-items-center text-white ${bgClass} border-0" role="${liveRole}" aria-live="${livePriority}" aria-atomic="true">
                    <div class="d-flex">
                        <div class="toast-body">${escapeHtml(message)}</div>
                        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Dismiss notification"></button>
                    </div>
                </div>`;
            container.insertAdjacentHTML('beforeend', html);
            const el = document.getElementById(id);
            const toast = new bootstrap.Toast(el, { delay: duration });
            toast.show();
            el.addEventListener('hidden.bs.toast', () => el.remove());
        }

        function showActionError(action, error, recovery, type = 'error') {
            showToast(getActionErrorMessage(action, error, recovery), type, 10000);
        }

        function getActionErrorMessage(action, error, recovery) {
            const detail = error?.message || String(error);
            const unreachable = !error?.status && /failed to fetch|networkerror|network request failed|load failed/i.test(detail);
            const guidance = unreachable
                ? `Could not reach Alexandria. Check that the app is running and the connection is available. Review the current status before retrying; a request may have completed even if its reply was lost. ${recovery}`
                : recovery;
            return `${action}. ${guidance} Details: ${detail}`;
        }

        function showConfirm(message, {title = 'Confirm action', actionLabel = 'Continue', danger = false} = {}) {
            const confirmation = confirmQueue.then(() => new Promise((resolve, reject) => {
                const body = document.getElementById('confirmModalBody');
                body.textContent = message;
                const modalElement = document.getElementById('confirmModal');
                const modal = new bootstrap.Modal(modalElement);
                const okBtn = document.getElementById('confirmModalOk');
                const cancelBtn = document.getElementById('confirmModalCancel');
                document.getElementById('confirmModalTitle').textContent = title;
                okBtn.textContent = actionLabel;
                okBtn.classList.toggle('btn-danger', danger);
                okBtn.classList.toggle('btn-primary', !danger);
                let shown = false;
                let answered = false;
                let decision = false;

                function cleanup() {
                    okBtn.removeEventListener('click', onOk);
                    cancelBtn.removeEventListener('click', onCancel);
                    modalElement.removeEventListener('hidden.bs.modal', onHidden);
                    modalElement.removeEventListener('shown.bs.modal', onShown);
                }
                function finish(value) {
                    if (answered) { return; }
                    answered = true;
                    decision = value;
                    okBtn.removeEventListener('click', onOk);
                    cancelBtn.removeEventListener('click', onCancel);
                    if (shown) { modal.hide(); }
                }
                function onShown() {
                    shown = true;
                    if (answered) { modal.hide(); }
                }
                function onOk() { finish(true); }
                function onCancel() { finish(false); }
                function onHidden() {
                    cleanup();
                    modal.dispose();
                    resolve(decision);
                }

                okBtn.addEventListener('click', onOk);
                cancelBtn.addEventListener('click', onCancel);
                modalElement.addEventListener('hidden.bs.modal', onHidden);
                modalElement.addEventListener('shown.bs.modal', onShown);
                try {
                    modal.show();
                } catch (error) {
                    cleanup();
                    modal.dispose();
                    reject(error);
                }
            }));
            confirmQueue = confirmation.catch(() => {});
            return confirmation;
        }

        let presetEditorPending = false;
        async function showPresetEditor({title, name = '', description = '', includeDescription = true, validateName = () => '', nameLabel = 'Preset name', descriptionLabel = 'Description (optional)', descriptionPlaceholder = 'When should it be used?', actionLabel = 'Save preset', helperText = 'Enter a name for this preset. Using an existing name replaces that saved preset.', submitValues = null, allowEmptyName = false}) {
            if (presetEditorPending) { return null; }
            presetEditorPending = true;
            const editing = confirmQueue.then(() => new Promise((resolve, reject) => {
                const element = document.getElementById('presetEditorModal');
                const form = document.getElementById('preset-editor-form');
                const nameField = document.getElementById('preset-editor-name');
                const descriptionField = document.getElementById('preset-editor-description');
                const error = document.getElementById('preset-editor-error');
                const cancel = document.getElementById('preset-editor-cancel');
                document.getElementById('presetEditorTitle').textContent = title;
                document.getElementById('preset-editor-name-label').textContent = nameLabel;
                document.getElementById('preset-editor-description-label').textContent = descriptionLabel;
                document.getElementById('preset-editor-submit').textContent = actionLabel;
                document.getElementById('preset-editor-help').textContent = helperText;
                descriptionField.placeholder = descriptionPlaceholder;
                document.getElementById('preset-editor-description-group').hidden = !includeDescription;
                nameField.value = name;
                descriptionField.value = description;
                nameField.removeAttribute('aria-invalid');
                error.textContent = '';
                const modal = new bootstrap.Modal(element);
                let result = null;
                let answered = false;
                let shown = false;
                let submitting = false;
                const submitButton = document.getElementById('preset-editor-submit');
                function setSubmitting(pending) {
                    submitting = pending;
                    nameField.disabled = descriptionField.disabled = cancel.disabled = submitButton.disabled = pending;
                }
                setSubmitting(false);
                function onHide(event) { if (submitting) { event.preventDefault(); } }
                async function submit(values) {
                    setSubmitting(true);
                    error.textContent = '';
                    nameField.removeAttribute('aria-invalid');
                    try {
                        const receipt = await submitValues(values);
                        setSubmitting(false);
                        finish({...values, receipt});
                    } catch (failure) {
                        console.error('Text dialog submission failed', failure);
                        error.textContent = failure.message || 'The change was not confirmed. Check the app connection before trying again.';
                    } finally { setSubmitting(false); }
                }
                function cleanup() {
                    element.removeEventListener('hide.bs.modal', onHide);
                    form.removeEventListener('submit', onSubmit);
                    cancel.removeEventListener('click', onCancel);
                    element.removeEventListener('shown.bs.modal', onShown);
                    element.removeEventListener('hidden.bs.modal', onHidden);
                }
                function finish(value) {
                    if (answered || submitting) { return; }
                    answered = true;
                    result = value;
                    if (shown) { modal.hide(); }
                }
                function onSubmit(event) {
                    event.preventDefault();
                    if (answered || submitting) { return; }
                    const value = nameField.value.trim();
                    const message = value || allowEmptyName ? validateName(value) : `Enter ${/^[aeiou]/i.test(nameLabel) ? 'an' : 'a'} ${nameLabel.toLowerCase()}.`;
                    if (message) {
                        error.textContent = message;
                        nameField.setAttribute('aria-invalid', 'true');
                        nameField.focus();
                        return;
                    }
                    const values = {name: value, description: includeDescription ? descriptionField.value.trim() : ''};
                    if (submitValues) { submit(values); } else { finish(values); }
                }
                function onCancel() { finish(null); }
                function onShown() {
                    shown = true;
                    if (answered) { modal.hide(); }
                    else { nameField.focus(); }
                }
                function onHidden() {
                    cleanup();
                    modal.dispose();
                    resolve(result);
                }
                element.addEventListener('hide.bs.modal', onHide);
                form.addEventListener('submit', onSubmit);
                cancel.addEventListener('click', onCancel);
                element.addEventListener('shown.bs.modal', onShown);
                element.addEventListener('hidden.bs.modal', onHidden);
                try { modal.show(); }
                catch (error) { cleanup(); modal.dispose(); reject(error); }
            }));
            confirmQueue = editing.catch(() => {});
            try { return await editing; }
            finally { presetEditorPending = false; }
        }

        async function reloadPageAfterConfirmation() {
            if (!await showConfirm('Reload the page? Unsaved changes may be lost.', {title: 'Reload page?', actionLabel: 'Reload', danger: true})) { return; }
            location.reload();
        }

        // Confirm remote usage before starting long-running jobs. The app does
        // not know the provider's pricing or account balance.
        async function confirmIfRemote(taskLabel, failoverOnly = false) {
            if (failoverOnly && !failoverIsRemote) { return true; }
            if (currentIsRemote) {
                return await showConfirm(
                    `This will run ${taskLabel} on your REMOTE LLM profile and may incur usage ` +
                    `charges from your provider. Continue on remote, or Cancel and switch ` +
                    `to Local in Setup first?`,
                    {title: 'Remote usage charges', actionLabel: 'Continue on remote', danger: false});
            }
            if (failoverIsRemote) {
                // Server-computed: failover is on and the other profile is remote.
                return await showConfirm(
                    `${taskLabel} runs on Local, but failover is on: if Local gives up (retries run out ` +
                    `or a content-policy refusal) the rest of the run switches to your REMOTE LLM and ` +
                    `may incur usage charges from your provider. Continue, or Cancel and turn failover off in Setup?`,
                    {title: 'Remote failover charges', actionLabel: 'Continue with failover', danger: false});
            }
            return true;
        }

        async function ensureScriptStartConfirmed(taskLabel, failoverOnly = false) {
            const button = document.getElementById('btn-gen-script');
            if (button.disabled) { return false; }
            button.disabled = true;
            let approved = false;
            try {
                approved = await confirmIfRemote(taskLabel, failoverOnly);
                return approved;
            } catch (error) {
                console.error('Script start confirmation failed:', error);
                showToast('Could not confirm this script run. Check the connection and try again.', 'error');
                return false;
            } finally {
                if (!approved) { button.disabled = false; }
            }
        }

        // navigator.clipboard exists only in a secure context (https, or
        // http on localhost). Pinokio reaches the app through other hosts
        // too, and there it is undefined - "Cannot read properties of
        // undefined (reading 'writeText')" on every Copy button (#594). So:
        // the modern API where it exists, the old execCommand path where it
        // doesn't, and if both fail the text is put where the user can copy
        // it by hand rather than a toast that says it cannot be done.
        function showManualCopy(text, what) {
            const copying = confirmQueue.then(() => new Promise((resolve, reject) => {
                const element = document.getElementById('manualCopyModal');
                const field = document.getElementById('manual-copy-text');
                const close = document.getElementById('manual-copy-close');
                document.getElementById('manualCopyTitle').textContent = `Copy ${what} manually`;
                field.value = text;
                const modal = new bootstrap.Modal(element);
                let shown = false;
                let closing = false;
                function onShown() {
                    shown = true;
                    if (closing) { modal.hide(); }
                    else { field.focus(); field.select(); }
                }
                function onClose() {
                    closing = true;
                    if (shown) { modal.hide(); }
                }
                function cleanup() {
                    element.removeEventListener('shown.bs.modal', onShown);
                    element.removeEventListener('hidden.bs.modal', onHidden);
                    close.removeEventListener('click', onClose);
                    field.value = '';
                }
                function onHidden() {
                    cleanup();
                    modal.dispose();
                    resolve();
                }
                element.addEventListener('shown.bs.modal', onShown);
                element.addEventListener('hidden.bs.modal', onHidden);
                close.addEventListener('click', onClose);
                try { modal.show(); }
                catch (error) { cleanup(); modal.dispose(); reject(error); }
            }));
            confirmQueue = copying.catch(() => {});
            return copying;
        }

        async function copyToClipboard(text, what = 'Text') {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                try {
                    await navigator.clipboard.writeText(text);
                    showToast(`${what} copied to the clipboard.`, 'success');
                    return true;
                } catch (e) { /* fall through to the legacy path */ }
            }
            const area = document.createElement('textarea');
            area.value = text;
            area.setAttribute('readonly', '');
            area.style.position = 'fixed';
            area.style.left = '-9999px';
            document.body.appendChild(area);
            area.select();
            let copied = false;
            try { copied = document.execCommand('copy'); } catch (e) { copied = false; }
            document.body.removeChild(area);
            if (copied) {
                showToast(`${what} copied to the clipboard.`, 'success');
                return true;
            }
            await showManualCopy(text, what);
            return false;
        }

        function escapeHtml(str) {
            if (str == null) { return ''; }
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function getInlineStringArgument(value) {
            return escapeHtml(JSON.stringify(String(value)));
        }

        // Use for text and quoted HTML attributes, never script/style contexts.
        function getEscapedHtml(parts, ...values) {
            return parts.reduce((html, part, index) => html + part
                + (index < values.length ? escapeHtml(values[index]) : ''), '');
        }

        // Parse a numeric input's value, falling back to `def` when the field is
        // empty/non-numeric. Uses Number.isFinite (not `|| def`) so a deliberate 0
        // is preserved rather than treated as falsy.
        // generation.chunk_size drives only the legacy generate_script.py CLI;
        // the UI no longer shows it, but the saved value round-trips so the
        // config stays valid for that path.
        let legacyChunkSize = 3000;

        function getNumFieldValue(id, def, isInt = false) {
            const raw = document.getElementById(id).value;
            const v = isInt ? (raw.trim() ? Number(raw) : NaN) : parseFloat(raw);
            return Number.isFinite(v) ? v : def;
        }

        // "2000, 4000, 6000" -> [2000, 4000, 6000]; an empty box means the default,
        // anything that is not a positive integer is an error, not a silent drop.
        function getConfigValidationError(id, message) {
            const error = new Error(message);
            error.fieldId = id;
            return error;
        }

        function showConfigValidationError(error) {
            const field = error.fieldId ? document.getElementById(error.fieldId) : null;
            if (field) {
                const focusField = () => { field.focus(); field.scrollIntoView({block: 'center'}); };
                let waiting = false;
                for (let parent = field.parentElement; parent; parent = parent.parentElement) {
                    if (parent.tagName === 'DETAILS') { parent.open = true; }
                    if (parent.classList.contains('collapse') && !parent.classList.contains('show')) {
                        waiting = true;
                        parent.addEventListener('shown.bs.collapse', focusField, {once: true});
                        bootstrap.Collapse.getOrCreateInstance(parent, {toggle: false}).show();
                    }
                }
                if (!waiting) { focusField(); }
            }
            showToast(error.message, 'error');
        }

        function getIntListInput(id, label, def) {
            const raw = document.getElementById(id).value.trim();
            if (!raw) { return def; }
            const tokens = raw.split(',').map(t => t.trim());
            if (tokens.some(t => !/^[1-9]\d*$/.test(t))) {
                throw getConfigValidationError(id, `${label}: use positive whole numbers separated by commas.`);
            }
            const values = tokens.map(Number);
            if (values.some(v => !Number.isSafeInteger(v))) {
                throw getConfigValidationError(id, `${label}: use positive whole numbers separated by commas.`);
            }
            return values;
        }

        function isTaskFailed(status) {
            if (!status) { return false; }
            if ((status.tasks || []).some(task => ['failed', 'incomplete'].includes(task.status))) { return true; }
            if (['failed', 'incomplete'].includes(status.status)) { return true; }
            if (status.status === 'cancelled') { return false; }
            const logs = status.logs || [];
            for (let i = logs.length - 1; i >= 0; i--) {
                const line = String(logs[i]).trim();
                if (/^Task \S+ completed successfully\.$/i.test(line)) { return false; }
                if (/^Task \S+ (?:cancelled\.|was cancelled\s*\()/i.test(line)) { return false; }
                if (/^Task \S+ (?:failed with return code|completed, but [1-9]\d* section\(s\))/i.test(line)) { return true; }
                if (/^(?:\[ERROR\]|Error:)/i.test(line)) { return true; }
            }
            return logs.some(log => {
                const line = String(log)
                    .replace(/\b(?:0|zero|no)\s+(?:(?:batch\(es\)|section\(s\)|batches|sections)\s+)?(?:errors?|failures?|failed)\b(?:\s+or\s+(?:errors?|failures?))?/gi, '')
                    .replace(/\b(?:errors?|failed|failures?)\s*(?:count\s*)?[:=]\s*0(?!\d|\.\d)/gi, '')
                    .replace(/\bwithout (?:any |recorded )?(?:errors?|failures?|failed)(?: or skipped sections)?\b/gi, '');
                return /\b(error|errors|failed|failure|failures)\b/i.test(line);
            });
        }

        // --- Desktop notifications ---
        const TASK_LABELS = {
            script: 'Script generation',
            review: 'Script review',
            nicknames: 'Nickname discovery',
            cast_list: 'Cast list',
            persona: 'Persona generation',
            audio: 'Audio generation',
            batch_review: 'Batch review',
            batch_script: 'Batch script generation',
            lora_training: 'LoRA training',
            voicelab: 'Voice Lab pipeline',
            preparer: 'Dataset preparer',
            batch_preparer: 'Dataset preparer batch',
            dataset_builder: 'Dataset builder batch',
            book_preflight: 'Book test',
            benchmark: 'Benchmark',
            audacity_export: 'Audacity export',
            m4b_export: 'M4B export',
        };

        // Notify the user that a long-running job finished, but only if they've
        // navigated away from the tab (no point popping up a notification for
        // something they're already watching).
        function getTaskCompletionOutcome(status) {
            if (isTaskFailed(status)) { return 'failed'; }
            if (status.status === 'cancelled' || (status.tasks || []).some(task => task.status === 'cancelled')) { return 'cancelled'; }
            for (const line of (status.logs || []).slice().reverse()) {
                if (/^Task \S+ completed successfully\.$/i.test(String(line).trim())) { return 'finished'; }
                if (/^Task \S+ (?:cancelled\.|was cancelled\s*\()/i.test(String(line).trim())) { return 'cancelled'; }
            }
            return 'finished';
        }

        function notifyJobDone(taskName, detail = '', outcome = 'finished', status = null) {
            if (!('Notification' in window) || Notification.permission !== 'granted') { return; }
            if (document.visibilityState === 'visible' && document.hasFocus()) { return; }
            const completion = status ? getTaskCompletionOutcome(status) : outcome;
            const title = `${TASK_LABELS[taskName] || taskName} ${completion}`;
            const message = detail || (completion === 'failed' || completion === 'cancelled'
                ? 'Open Alexandria to review the activity log and your recovery options.'
                : 'Switch back to Alexandria to see the results.');
            try {
                new Notification(title, { body: message, icon: '/favicon.ico' });
            } catch (e) { /* notifications are a nice-to-have */ }
        }

        // Ask for permission only from the dedicated notification control.
        let notificationPermissionPending = false;

        function renderNotificationPermission() {
            const button = document.getElementById('notification-enable');
            const status = document.getElementById('notification-status');
            if (!button || !status) { return; }
            const supported = 'Notification' in window;
            const permission = supported ? Notification.permission : 'unsupported';
            button.disabled = notificationPermissionPending || permission !== 'default';
            button.textContent = notificationPermissionPending ? 'Requesting notifications…'
                : permission === 'granted' ? 'Notifications enabled' : 'Enable task notifications';
            status.textContent = permission === 'granted'
                ? 'Task updates appear when you are away from Alexandria.'
                : permission === 'denied' ? 'Notifications are blocked. Change this site’s browser permissions to enable them.'
                    : permission === 'unsupported' ? 'This browser does not support desktop notifications.'
                        : 'Optional: receive desktop updates when tasks finish.';
        }

        async function requestTaskNotifications() {
            if (notificationPermissionPending || !('Notification' in window) || Notification.permission !== 'default') { return; }
            notificationPermissionPending = true;
            renderNotificationPermission();
            try {
                await Notification.requestPermission();
            } catch (error) {
                showToast('Could not request notifications. Check this site’s browser permissions and try again.', 'warning');
            } finally {
                notificationPermissionPending = false;
                renderNotificationPermission();
            }
        }

        renderNotificationPermission();

        // --- Navigation ---
        // Remember the open tab across reloads. restoreTab() runs at the end of
        // app-reports.js (the last script), synchronously during page load, so
        // the Setup tab never gets a frame to flash.
        const TAB_STORAGE_KEY = 'alexandria.activeTab';
        let currentTabName = 'setup';
        function rememberTab(name) {
            try { localStorage.setItem(TAB_STORAGE_KEY, name); } catch (e) { /* private mode */ }
        }
        function getTabLink(name) {
            return Array.from(document.querySelectorAll('.nav-link')).find(link => link.dataset.tab === name);
        }
        function getUrlTabName() {
            const name = window.location.hash.slice(1);
            return getTabLink(name) ? name : null;
        }
        function activateTab(name, updateHistory = true) {
            const selectedLink = getTabLink(name);
            if (!selectedLink) { return; }
            const target = document.getElementById(name + '-tab');
            if (!target) { return; }
            if (updateHistory && window.location.hash !== '#' + name) {
                window.history.pushState(null, '', '#' + name);
            }
            currentTabName = name;
            rememberTab(name);
            // Remove active class from all links
            document.querySelectorAll('.nav-link').forEach(l => {
                l.classList.remove('active');
                l.removeAttribute('aria-current');
            });
            // Add active to clicked
            selectedLink.classList.add('active');
            selectedLink.setAttribute('aria-current', 'page');

            // Hide all tabs
            document.querySelectorAll('.tab-content').forEach(t => t.style.display = 'none');
            // Show target tab
            target.style.display = 'block';

            const nav = document.getElementById('navbarNav');
            if (nav.classList.contains('show') && window.innerWidth < 992) {
                bootstrap.Collapse.getOrCreateInstance(nav).hide();
            }

            // Trigger tab specific loads
            if (selectedLink.dataset.tab === 'setup') {
                pollLmStudioStatus();
            } else if (selectedLink.dataset.tab === 'editor') {
                loadChunks();
            } else if (selectedLink.dataset.tab === 'audio') {
                loadFinalAudio();
            } else if (selectedLink.dataset.tab === 'voices') {
                loadVoices(false);
            } else if (selectedLink.dataset.tab === 'designer') {
                loadDesignedVoices();
            } else if (selectedLink.dataset.tab === 'training') {
                loadLoraDatasets();
                loadLoraModels();
            } else if (selectedLink.dataset.tab === 'dataset-builder') {
                dsbLoadProjects(dsbCurrentProject);
            } else if (selectedLink.dataset.tab === 'preparer') {
                loadPreparerOutputs();
            } else if (selectedLink.dataset.tab === 'voicelab') {
                loadVoicelabConfig();
                voicelabInspect();
                refreshVoicelabHealth();
            } else if (selectedLink.dataset.tab === 'reports') {
                loadReports();
                loadCheckpoints();
                loadRunHistory();
                refreshBenchmarkStatus();
            }
        }
        function restoreTab() {
            let saved = null;
            try { saved = localStorage.getItem(TAB_STORAGE_KEY); } catch (e) { /* private mode */ }
            const name = getUrlTabName() || (getTabLink(saved) ? saved : 'setup');
            window.history.replaceState(null, '', '#' + name);
            if (name !== currentTabName) { activateTab(name, false); }
        }
        function onTabHistoryChange() {
            const name = getUrlTabName() || 'setup';
            if (name !== currentTabName) { activateTab(name, false); }
        }
        document.querySelectorAll('.nav-link').forEach(link => {
            link.addEventListener('click', (e) => {
                e.preventDefault();
                activateTab(e.currentTarget.dataset.tab);
            });
        });
        window.addEventListener('popstate', onTabHistoryChange);
        window.addEventListener('hashchange', onTabHistoryChange);

        // --- LLM model picker: ask the Base URL what it serves ---
        let llmModelRequestSequence = 0;
        async function refreshLlmModels() {
            const hint = document.getElementById('llm-model-hint');
            const list = document.getElementById('llm-model-options');
            const baseUrl = document.getElementById('llm-url').value.trim();
            const sequence = ++llmModelRequestSequence;
            const mode = currentLlmMode;
            let profile;
            const isCurrent = () => {
                if (sequence !== llmModelRequestSequence || mode !== currentLlmMode) { return false; }
                try { return !profile || JSON.stringify(profile) === JSON.stringify(getEditedLlmProfile()); }
                catch (e) { return false; }
            };
            if (!baseUrl) { hint.textContent = 'Set the Base URL first.'; return; }
            hint.textContent = 'Fetching model list...';
            try {
                profile = getEditedLlmProfile();
                const r = await API.post('/api/llm/models', profile);
                if (!isCurrent()) { return; }
                list.innerHTML = '';
                (r.models || []).forEach(id => {
                    const opt = document.createElement('option');
                    opt.value = id;
                    list.appendChild(opt);
                });
                if (r.error) {
                    hint.textContent = 'Could not list models: ' + r.error;
                } else {
                    hint.textContent = r.models.length ? `${r.models.length} model(s) available - start typing to pick one.` : 'Server reports no models loaded.';
                }
            } catch (e) {
                if (isCurrent()) { hint.textContent = 'Could not list models: ' + e.message; }
            }
        }
        document.getElementById('llm-model-refresh').addEventListener('click', refreshLlmModels);
        document.getElementById('llm-model').addEventListener('focus', () => {
            if (!document.getElementById('llm-model-options').children.length) { refreshLlmModels(); }
        });
        function toggleLlmKeyVisibility() {
            const input = document.getElementById('llm-key');
            const button = document.getElementById('llm-key-toggle');
            const visible = input.type === 'password';
            input.type = visible ? 'text' : 'password';
            button.textContent = visible ? 'Hide key' : 'Show key';
            button.setAttribute('aria-pressed', String(visible));
        }

        // --- Theme ---
        const THEMES = [
            { key: 'light',       icon: 'fa-sun',      label: 'Light'      },
            { key: 'night',       icon: 'fa-moon',     label: 'Night'      },
            { key: 'super-night', icon: 'fa-circle',   label: 'Super Night'},
            { key: 'cyberpunk',   icon: 'fa-bolt',     label: 'Cyberpunk'  },
        ];

        function applyTheme(key) {
            const html = document.documentElement;
            if (key === 'light') {
                html.removeAttribute('data-theme');
            } else {
                html.setAttribute('data-theme', key);
            }
            const t = THEMES.find(t => t.key === key) || THEMES[0];
            document.getElementById('theme-icon').className = `fas ${t.icon}`;
            document.getElementById('theme-label').textContent = `Theme: ${t.label}`;
            const next = THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];
            const action = `Current theme: ${t.label}. Switch to ${next.label}.`;
            const button = document.getElementById('theme-toggle');
            button.title = action;
            button.setAttribute('aria-label', action);
            try { localStorage.setItem('alex-theme', key); } catch (e) { /* storage blocked; theme still applies for this page */ }
        }

        function cycleTheme() {
            const current = document.documentElement.getAttribute('data-theme') || 'light';
            const idx = THEMES.findIndex(t => t.key === current);
            const next = THEMES[(idx + 1) % THEMES.length];
            applyTheme(next.key);
        }

        // Sync button label/icon with whatever the anti-flash snippet already applied
        (function() {
            let saved = 'light';
            try { saved = localStorage.getItem('alex-theme') || 'light'; } catch (e) { /* storage blocked */ }
            applyTheme(saved);
        })();

        function createSerializedSaveQueue({ write, delay = 500, onDirty = () => {}, onSaved = () => {}, onError = () => {} }) {
            let pending = null;
            let inFlight = null;
            let timer = null;
            let revision = 0;
            let savedRevision = 0;

            async function flush() {
                clearTimeout(timer);
                timer = null;
                if (inFlight) {
                    await inFlight;
                    if (pending) { return flush(); }
                    return;
                }
                if (!pending) { return; }
                const run = async () => {
                    while (pending) {
                        const batch = pending;
                        pending = null;
                        try {
                            await write(batch.value, batch.revision);
                        } catch (error) {
                            if (!pending) { pending = batch; }
                            onError(error);
                            throw error;
                        }
                        savedRevision = batch.revision;
                        if (savedRevision === revision) { onSaved(); }
                    }
                };
                inFlight = run();
                try {
                    await inFlight;
                } finally {
                    inFlight = null;
                }
            }

            function enqueue(value) {
                pending = { value: JSON.parse(JSON.stringify(value)), revision: ++revision };
                onDirty();
                clearTimeout(timer);
                timer = setTimeout(() => { flush().catch(() => {}); }, delay);
            }

            function isDirty() {
                return savedRevision !== revision;
            }

            async function discard() {
                clearTimeout(timer);
                timer = null;
                const discardedRevision = revision;
                pending = null;
                if (inFlight) {
                    try { await inFlight; } catch (error) { /* This failed draft was explicitly discarded. */ }
                }
                if (revision !== discardedRevision) {
                    throw new Error('New edits arrived while discarding; they were retained.');
                }
                pending = null;
                savedRevision = discardedRevision;
            }

            function getRevision() { return revision; }
            return { enqueue, flush, isDirty, discard, getRevision };
        }

        const taskStartButtons = {
            persona: ['btn-gen-personas'], voices: ['btn-suggest-voices'],
            audacity_export: ['btn-export-audacity'], m4b_export: ['btn-export-m4b'],
            chapter_export: ['chapter-export-btn'],
        };
        const taskStartClaims = new Map();
        function claimTaskStart(taskName, initiatingButton = null) {
            if (taskStartClaims.has(taskName)) { return false; }
            const ids = Object.hasOwn(taskStartButtons, taskName) ? taskStartButtons[taskName] : [];
            const buttons = ids.map(id => document.getElementById(id)).filter(Boolean);
            if (initiatingButton && !buttons.includes(initiatingButton)) { buttons.push(initiatingButton); }
            taskStartClaims.set(taskName, buttons);
            buttons.forEach(button => { button.disabled = true; });
            return true;
        }
        function releaseTaskStart(taskName) {
            const buttons = taskStartClaims.get(taskName) || [];
            taskStartClaims.delete(taskName);
            buttons.forEach(button => { button.disabled = false; });
        }

        // --- API Helpers ---
        const API = {
            _handleError: async (res) => {
                if (res.ok) { return; }
                let detail = res.statusText || `HTTP ${res.status}`;
                try {
                    const body = await res.json();
                    if (body && body.detail) { detail = body.detail; }
                } catch (e) { /* non-JSON error body; fall back to statusText */ }
                const err = new Error(typeof detail === 'string' ? detail : (detail.message || JSON.stringify(detail)));
                err.status = res.status;
                err.detail = detail; // structured 422 bodies (e.g. gate findings) stay readable
                throw err;
            },
            get: async (url) => {
                const res = await fetch(url);
                await API._handleError(res);
                return res.json();
            },
            post: async (url, data) => {
                const res = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                await API._handleError(res);
                return res.json();
            },
            del: async (url) => {
                const res = await fetch(url, { method: 'DELETE' });
                await API._handleError(res);
                return res.json();
            },
            upload: async (file, {selectActive = true} = {}) => {
                const formData = new FormData();
                formData.append('file', file);
                const res = await fetch(`/api/upload?select_active=${selectActive}`, {
                    method: 'POST',
                    body: formData
                });
                await API._handleError(res);
                return res.json();
            }
        };

        function getTaskLogUpdate(previousLogs, previousRunId, logs, runId) {
            const samePrefix = previousLogs !== null && runId === previousRunId
                && logs.length >= previousLogs.length
                && previousLogs.every((line, index) => line === logs[index]);
            const startIndex = samePrefix ? previousLogs.length : 0;
            const changed = !samePrefix || logs.length !== previousLogs.length;
            return {
                reset: !samePrefix, changed, startIndex, runId,
                logs: changed ? logs.slice() : previousLogs,
                text: !changed ? '' : !samePrefix ? logs.join('\n')
                    : (previousLogs.length ? '\n' : '') + logs.slice(startIndex).join('\n'),
            };
        }

        function createTaskLogRenderer(element) {
            let previousLogs = null;
            let previousRunId = null;
            return status => {
                if (!element) { return; }
                const update = getTaskLogUpdate(previousLogs, previousRunId,
                    status.logs || [], status.run_id || status.start_time || null);
                if (!update.changed) { return update; }
                const followTail = previousLogs === null
                    || element.scrollHeight - element.clientHeight - element.scrollTop <= 24;
                const scrollPosition = element.scrollTop;
                element.style.whiteSpace = 'pre-wrap';
                if (update.reset) {
                    element.innerText = update.text;
                } else {
                    element.appendChild(document.createTextNode(update.text));
                }
                previousLogs = update.logs;
                previousRunId = update.runId;
                element.scrollTop = followTail ? element.scrollHeight : scrollPosition;
                return update;
            };
        }

        // --- Setup Tab ---

        function toggleTTSMode() {
            const mode = document.getElementById('tts-mode').value;
            document.getElementById('tts-url-group').style.display = mode === 'external' ? '' : 'none';
            document.getElementById('tts-device-group').style.display = mode === 'local' ? '' : 'none';
            document.getElementById('tts-local-options').style.display = mode === 'local' ? '' : 'none';
        }

        function toggleSubBatchFields() {
            const enabled = document.getElementById('sub-batch-enabled').checked;
            ['sub-batch-min-group', 'sub-batch-ratio-group', 'sub-batch-max-items-group'].forEach(id => {
                document.getElementById(id).style.display = enabled ? '' : 'none';
            });
        }

        async function autoConfigureSettings() {
            const btn = document.getElementById('btn-auto-configure');
            const request = {};
            btn._autoConfigureRequest = request;
            const snapshot = getAutoSettingsSnapshot();
            const isCurrent = () => btn._autoConfigureRequest === request;
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Detecting…';

            try {
                const stats = await API.get('/api/system/stats');
                if (!isCurrent()) { return; }
                if (getAutoSettingsSnapshot() !== snapshot) {
                    showToast('The TTS settings changed during detection. Your edits were kept; review them before running Auto-Configure again.', 'warning');
                    return;
                }
                const { settings, summary } = _computeAutoSettings(stats);
                _applyAutoSettings(settings);

                const banner = document.getElementById('auto-config-banner');
                document.getElementById('auto-config-msg').innerHTML =
                    `<i class="fas fa-check-circle me-1 text-success"></i><strong>Suggested settings:</strong> ${escapeHtml(summary)}.${settings.ttsMode === 'external' ? ' External TTS selected: start a compatible TTS server and enter its URL below before generating audio.' : ''} Review the TTS settings below, then click Save to apply.`;
                banner.style.display = '';
                banner.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            } catch (e) {
                if (isCurrent()) { showActionError('Hardware detection failed', e, 'Review the hardware details and TTS settings before trying Auto-Configure again.'); }
            } finally {
                if (isCurrent()) {
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fas fa-magic me-1"></i>Auto-Configure';
                }
            }
        }

        function getAutoSettingsFields() {
            return {
                ttsMode: ['tts-mode', 'value'], parallelWorkers: ['parallel-workers', 'value'],
                compileCodec: ['compile-codec', 'checked'], batchGroupByType: ['batch-group-by-type', 'checked'],
                subBatchEnabled: ['sub-batch-enabled', 'checked'], subBatchMinSize: ['sub-batch-min-size', 'value'],
                subBatchRatio: ['sub-batch-ratio', 'value'], subBatchMaxItems: ['sub-batch-max-items', 'value'],
            };
        }

        function getAutoSettingsSnapshot() {
            return JSON.stringify(Object.entries(getAutoSettingsFields()).map(([key, [id, property]]) =>
                [key, document.getElementById(id)[property]]));
        }

        function _computeAutoSettings(stats) {
            // get_gpu_stats() falls back to rocm-smi for VRAM totals even when torch
            // can't use the GPU (gpu_mismatch), so stats.gpu can be populated with a
            // real, large total_gb in exactly the case where generation will actually
            // run on CPU. Treat that the same as "no usable GPU" here - otherwise this
            // confidently configures high parallelism/local mode for a GPU that's
            // sitting there unused, which is worse than the safe CPU-tier defaults.
            const gpuUsable = !!stats.gpu && !stats.gpu_mismatch;
            const vram = gpuUsable ? stats.gpu.total_gb : 0;
            const gpuName = stats.gpu_name || null;
            const ramGb = stats.ram_gb || null;

            let tier, settings;

            // Tier boundaries and maxItems calibrated from tts_vram_benchmark.py on RX 9070 XT (17.1 GB):
            // Model footprint: 4.18 GB. Peak VRAM per batch: 4-item→7.3 GB, 8-item→9.6 GB, 16-item→11.8 GB.
            // RTF: 4→2.3x, 8→3.5x (sweet spot), 12→2.8x, 16→2.9x, 24→2.7x (length-ratio caps at ~13 anyway).
            if (!gpuUsable || vram < 5) {
                // Model alone needs ~4.2 GB; under 5 GB it may not load reliably.
                // Same safe settings whether there's truly no GPU or one that's
                // physically present but unusable by torch (gpu_mismatch) - only
                // the label differs, so the two cases share one branch instead of
                // two copies of an identical settings object.
                tier = stats.gpu_mismatch
                    ? 'GPU detected but unusable by torch (wrong build) - selecting External TTS'
                    : 'No GPU / insufficient VRAM';
                settings = { ttsMode: 'external', parallelWorkers: 1, compileCodec: false,
                    batchGroupByType: false, subBatchEnabled: true, subBatchMinSize: 4,
                    subBatchRatio: 5, subBatchMaxItems: 4 };
            } else if (vram < 8) {
                // 5–8 GB: model fits (~4.2 GB) but headroom is tight; long chunks peak ~7.3 GB total
                tier = `Low VRAM (${vram.toFixed(1)} GB)`;
                settings = { ttsMode: 'local', parallelWorkers: 1, compileCodec: false,
                    batchGroupByType: true, subBatchEnabled: true, subBatchMinSize: 4,
                    subBatchRatio: 5, subBatchMaxItems: 4 };
            } else if (vram < 12) {
                // 8–12 GB: ~4–8 GB headroom; 6-item batches peak ~8 GB total (safe)
                tier = `Mid VRAM (${vram.toFixed(1)} GB)`;
                settings = { ttsMode: 'local', parallelWorkers: 1, compileCodec: false,
                    batchGroupByType: true, subBatchEnabled: true, subBatchMinSize: 4,
                    subBatchRatio: 5, subBatchMaxItems: 6 };
            } else if (vram < 20) {
                // 12–20 GB: 8-item batches peak ~9.6 GB total — best measured RTF (3.5x)
                tier = `High VRAM (${vram.toFixed(1)} GB)`;
                settings = { ttsMode: 'local', parallelWorkers: 2, compileCodec: false,
                    batchGroupByType: true, subBatchEnabled: true, subBatchMinSize: 4,
                    subBatchRatio: 5, subBatchMaxItems: 8 };
            } else if (vram < 30) {
                // 20–30 GB: 16-item batches peak ~11.8 GB total — comfortable headroom
                tier = `Large VRAM (${vram.toFixed(1)} GB)`;
                settings = { ttsMode: 'local', parallelWorkers: 2, compileCodec: false,
                    batchGroupByType: true, subBatchEnabled: true, subBatchMinSize: 4,
                    subBatchRatio: 5, subBatchMaxItems: 16 };
            } else {
                // 30+ GB: length-ratio splitter will cap practical batch size anyway
                tier = `Enthusiast VRAM (${vram.toFixed(1)} GB)`;
                settings = { ttsMode: 'local', parallelWorkers: 4, compileCodec: false,
                    batchGroupByType: true, subBatchEnabled: true, subBatchMinSize: 4,
                    subBatchRatio: 5, subBatchMaxItems: 24 };
            }

            const parts = [tier];
            if (gpuName) { parts.push(gpuName); }
            if (ramGb) { parts.push(`${Math.round(ramGb)} GB RAM`); }
            if (stats.cpu_count) { parts.push(`${stats.cpu_count} CPU threads`); }
            const summary = parts.join(' · ');

            return { settings, summary };
        }

        function _applyAutoSettings(s) {
            for (const [key, [id, property]] of Object.entries(getAutoSettingsFields())) {
                document.getElementById(id)[property] = s[key];
            }
            toggleTTSMode();
            toggleSubBatchFields();
        }

        // Local/Remote LLM profile state. Each mode keeps its own base_url/key/model
        // so switching the toggle never clobbers the other location's settings.
        let llmProfiles = { local: {base_url:'', api_key:'local', model_name:''},
                            remote: {base_url:'', api_key:'local', model_name:''} };
        let currentLlmMode = 'local';
        let savedLlmMode = 'local';   // last-saved llm_mode; drives the "Active:" badge
        // Server-computed lmstudio_settings.is_remote_llm(llm_mode, base_url)
        // for the ACTIVE (saved) config - reflects llm_mode/base_url drift
        // that a bare `currentLlmMode === 'remote'` check would miss. Updated
        // from /api/config's response on load and after a successful save.
        let currentIsRemote = false;
        let failoverIsRemote = false;   // server-computed: llm_failover on AND the other profile is remote
        let promptPresets = [];
        let activePromptPreset = 'michel2_full';
        let configSavePending = false;
        const passPromptPresets = {pass1: [], pass3: []};
        const activePassPromptPreset = {pass1: 'default', pass3: 'default'};
        const passPromptDefaults = {
            pass1: {system_prompt: '', user_prompt: ''},
            pass3: {system_prompt: '', user_prompt: ''}
        };

        function promptBoxes() {
            return {
                system_prompt: document.getElementById('system-prompt').value,
                user_prompt: document.getElementById('user-prompt').value,
                example: document.getElementById('prompt-example').value,
            };
        }

        function passPromptFields(pass) {
            return pass === 'pass1'
                ? {system: 'pass1-system-prompt', user: 'pass1-user-prompt'}
                : {system: 'pass3-system-prompt', user: 'pass3-user-prompt'};
        }

        function getPassPromptPresetPayload(pass) {
            const own = passPromptPresets[pass].map(preset => ({...preset}));
            const active = activePassPromptPreset[pass];
            const preset = own.find(item => item.name === active);
            const defaults = passPromptDefaults[pass];
            const fields = passPromptFields(pass);
            const boxes = {system_prompt: document.getElementById(fields.system).value,
                user_prompt: document.getElementById(fields.user).value};
            if (boxes.system_prompt === (preset?.system_prompt || defaults.system_prompt)
                && boxes.user_prompt === (preset?.user_prompt || defaults.user_prompt)) { return {active, own}; }
            const name = preset?.name || 'default (edited)';
            const edited = {...preset, name, ...boxes};
            const index = own.findIndex(item => item.name === name);
            if (index >= 0) { own[index] = edited; } else { own.push(edited); }
            return {active: name, own};
        }

        const pendingPromptPresetSwitches = new Set();

        async function onPromptPresetChange(pass, index) {
            const attribution = pass === 'attribution';
            const presets = attribution ? promptPresets : passPromptPresets[pass];
            const active = attribution ? activePromptPreset : activePassPromptPreset[pass];
            const select = document.getElementById(attribution ? 'prompt-preset-select' : `${pass}-prompt-preset-select`);
            const previousIndex = presets.findIndex(preset => preset.name === active);
            select.value = String(previousIndex);
            if (pendingPromptPresetSwitches.has(pass)) { return; }
            if (configSavePending) {
                showToast('Wait for the configuration save before switching prompt presets.', 'warning');
                return;
            }
            const target = presets[index];
            if (attribution && !target) { return; }
            const current = presets[previousIndex];
            const defaults = attribution ? {} : passPromptDefaults[pass];
            const boxes = getPromptEditorBoxes(pass);
            const edited = Object.entries(boxes).some(([key, value]) => value !== (current?.[key] || defaults[key] || ''));
            pendingPromptPresetSwitches.add(pass);
            try {
                if (edited) {
                    const snapshot = getPromptPresetEditorSnapshot(pass);
                    const defaultSnapshot = JSON.stringify(defaults);
                    if (!await showConfirm(`Switch to prompt preset "${target?.name || 'Default'}"? This replaces the current prompt text. Discard your unsaved prompt edits?`, {title: 'Replace edited prompt?', actionLabel: 'Switch preset', danger: true})) { return; }
                    if (snapshot !== getPromptPresetEditorSnapshot(pass)
                        || defaultSnapshot !== JSON.stringify(attribution ? {} : passPromptDefaults[pass])) {
                        showToast('The prompt edits or presets changed. Review them before switching.', 'warning');
                        return;
                    }
                }
                select.value = String(index);
                if (attribution) { applyPromptPreset(index); }
                else { applyPassPromptPreset(pass, index); }
            } catch (error) {
                console.error('Prompt preset switch failed:', error);
                showToast('Could not confirm the preset switch. Your prompt edits were kept; try again.', 'error');
            } finally {
                pendingPromptPresetSwitches.delete(pass);
            }
        }

        function renderPassPromptPresets(pass, presets, activeName) {
            const own = Array.isArray(presets) ? presets.map(preset => ({...preset})) : [];
            const names = new Set(own.map(preset => preset.name));
            for (const preset of own) {
                if (preset.name !== 'default') { continue; }
                let name = 'default (user)';
                let suffix = 2;
                while (names.has(name)) { name = `default (user ${suffix++})`; }
                names.add(name);
                preset.name = name;
                if (activeName === 'default') { activeName = name; }
                showToast(`The ${pass} user preset "default" is now "${name}" to keep it separate from the checked-in prompt. Save configuration to retain this name.`, 'warning');
            }
            passPromptPresets[pass] = own;
            activePassPromptPreset[pass] = activeName || 'default';
            const select = document.getElementById(`${pass}-prompt-preset-select`);
            if (!select) { return; }
            select.replaceChildren();
            const defaultOption = document.createElement('option');
            defaultOption.value = '-1';
            defaultOption.textContent = 'Default (checked-in prompt)';
            select.appendChild(defaultOption);
            passPromptPresets[pass].forEach((preset, index) => {
                const option = document.createElement('option');
                option.value = String(index);
                option.textContent = preset.name;
                select.appendChild(option);
            });
            select.onchange = () => onPromptPresetChange(pass, Number(select.value));
            const index = passPromptPresets[pass].findIndex(p => p.name === activePassPromptPreset[pass]);
            select.value = index >= 0 ? String(index) : '-1';
            applyPassPromptPreset(pass, index);
        }

        function applyPassPromptPreset(pass, index) {
            const preset = passPromptPresets[pass][index];
            const defaults = passPromptDefaults[pass];
            const fields = passPromptFields(pass);
            document.getElementById(fields.system).value = preset?.system_prompt || defaults.system_prompt;
            document.getElementById(fields.user).value = preset?.user_prompt || defaults.user_prompt;
            activePassPromptPreset[pass] = preset?.name || 'default';
        }

        function getPassPromptPresetNameError(name) {
            return name.trim() === 'default' ? 'The name "default" is reserved for the checked-in prompt.' : '';
        }

        window.savePassPromptPreset = async (pass) => {
            const fields = passPromptFields(pass);
            const snapshot = getPromptPresetEditorSnapshot(pass);
            const values = await showPresetEditor({title: `Save ${pass} prompt preset`,
                name: activePassPromptPreset[pass] === 'default' ? '' : activePassPromptPreset[pass],
                validateName: getPassPromptPresetNameError});
            if (!values) { return; }
            if (getPromptPresetEditorSnapshot(pass) !== snapshot) {
                showToast('The prompt or preset selection changed. Review it before saving a preset.', 'warning');
                return;
            }
            const {name, description} = values;
            const nameError = getPassPromptPresetNameError(name);
            if (nameError) { showToast(nameError, 'warning'); return; }
            const preset = {name: name.trim(), description: description.trim(),
                system_prompt: document.getElementById(fields.system).value,
                user_prompt: document.getElementById(fields.user).value};
            const presets = passPromptPresets[pass].map(item => ({...item}));
            const index = presets.findIndex(p => p.name === preset.name);
            if (index >= 0) { presets[index] = preset; } else { presets.push(preset); }
            const submitted = getPromptEditorBoxes(pass);
            try {
                await persistPromptPresets({prompts: {[`${pass}_preset`]: preset.name, [`${pass}_prompt_presets`]: presets}},
                    () => applyPromptPresetSave(pass, presets, preset.name, submitted));
                showToast(`${pass === 'pass1' ? 'Step 1' : 'Step 3'} prompt preset saved.`, 'success');
            } catch (e) { showActionError('Could not save prompt preset', e, 'Review the preset name and saved preset list before trying Save preset again. Your prompt fields remain available.'); }
        };

        const pendingPromptPresetDeletions = new Set();

        function getPromptPresetEditorSnapshot(pass) {
            const attribution = pass === 'attribution';
            return JSON.stringify({
                selected: attribution ? selectedPromptPreset()?.name : activePassPromptPreset[pass],
                presets: attribution ? promptPresets : passPromptPresets[pass],
                editor: getPromptEditorBoxes(pass),
            });
        }

        async function applyConfirmedPromptPresetDeletion(pass, message, remove) {
            if (pendingPromptPresetDeletions.has(pass)) { return; }
            pendingPromptPresetDeletions.add(pass);
            const snapshot = getPromptPresetEditorSnapshot(pass);
            try {
                if (!await showConfirm(message, {title: 'Delete prompt preset?', actionLabel: 'Delete preset and save Setup', danger: true})) { return; }
                if (getPromptPresetEditorSnapshot(pass) !== snapshot) {
                    showToast('The preset selection or prompt edits changed. Review them before deleting.', 'warning');
                    return;
                }
                await remove();
            } finally {
                pendingPromptPresetDeletions.delete(pass);
            }
        }

        window.deletePassPromptPreset = async (pass) => {
            const presets = passPromptPresets[pass].map(item => ({...item}));
            const index = presets.findIndex(p => p.name === activePassPromptPreset[pass]);
            if (index < 0) { showToast('Select a saved preset first.', 'warning'); return; }
            try {
                await applyConfirmedPromptPresetDeletion(pass,
                    `Delete preset "${presets[index].name}" and switch the active ${pass} prompt to the checked-in default? This also saves your current Setup settings.`, async () => {
                        presets.splice(index, 1);
                        const submitted = getPromptEditorBoxes(pass);
                        await persistPromptPresets({prompts: {[`${pass}_preset`]: 'default', [`${pass}_prompt_presets`]: presets}},
                            () => applyPromptPresetSave(pass, presets, 'default', submitted));
                        showToast('Prompt preset deleted.', 'success');
                    });
            } catch (e) {
                showActionError('Could not delete prompt preset', e, 'Review the saved preset list to check whether it was deleted before trying Delete again.');
            }
        };

        function selectedPromptPreset() {
            const select = document.getElementById('prompt-preset-select');
            return promptPresets[Number(select?.value)] || null;
        }

        // Does the text on screen differ from the selected preset's own text?
        function promptBoxesEdited(preset) {
            if (!preset) { return false; }
            const boxes = promptBoxes();
            return boxes.system_prompt !== (preset.system_prompt || '')
                || boxes.user_prompt !== (preset.user_prompt || '')
                || boxes.example !== (preset.example || '');
        }

        function renderPromptPresets(presets, activeName) {
            promptPresets = Array.isArray(presets) ? presets : [];
            const select = document.getElementById('prompt-preset-select');
            if (!select) { return; }
            select.replaceChildren();
            const builtins = document.createElement('optgroup');
            builtins.label = 'Built-in (measured; names as in RECIPES.md)';
            const mine = document.createElement('optgroup');
            mine.label = 'Your presets';
            promptPresets.forEach((preset, index) => {
                const option = document.createElement('option');
                option.value = String(index);
                option.textContent = preset.builtin
                    ? `${preset.name} - ${preset.description || ''}`
                    : `${preset.name} (${preset.variant || 'default'})`;
                (preset.builtin ? builtins : mine).appendChild(option);
            });
            select.appendChild(builtins);
            if (mine.childElementCount) { select.appendChild(mine); }
            select.onchange = () => onPromptPresetChange('attribution', Number(select.value));
            let index = promptPresets.findIndex(p => p.name === (activeName || activePromptPreset));
            if (index < 0) { index = promptPresets.findIndex(p => p.name === 'michel2_full'); }
            if (index < 0 && promptPresets.length) { index = 0; }
            if (index >= 0) {
                select.value = String(index);
                applyPromptPreset(index);
            }
        }

        function applyPromptPreset(index) {
            const preset = promptPresets[index];
            if (!preset) { return; }
            activePromptPreset = preset.name;
            const variant = preset.variant || 'default';
            document.getElementById('system-prompt').value = preset.system_prompt || '';
            document.getElementById('user-prompt').value = preset.user_prompt || '';
            document.getElementById('prompt-example').value = preset.example || '';
            document.getElementById('prompt-example-block').hidden = variant !== 'michel2_shot';
            document.getElementById('prompt-preset-description').textContent = preset.description || '';
            document.getElementById('prompt-preset-variant-badge').textContent = 'shape: ' + variant;
            document.getElementById('user-prompt-hint').textContent = (
                ['default', 'aliases', 'incremental', 'continuity'].includes(variant)
                    ? '(a template: the pipeline fills {roster} and {batch})'
                    : '(the pipeline adds the ROSTER line and the marked PASSAGE around it)');
            document.getElementById('prompt-preview-panel').hidden = true;
        }

        // What the save sends: the active preset by name plus the user's own
        // presets. Editing a built-in's text on screen becomes a new preset
        // "<name> (edited)" so the built-in stays what RECIPES measured.
        function promptPresetPayload() {
            const preset = selectedPromptPreset();
            let active = preset ? preset.name : 'default';
            const own = promptPresets.filter(p => !p.builtin).map(p => ({...p}));
            if (preset && promptBoxesEdited(preset)) {
                const boxes = promptBoxes();
                if (preset.builtin) {
                    const edited = {name: `${preset.name} (edited)`, description: `Edited copy of ${preset.name}`,
                        variant: preset.variant || 'default', ...boxes, builtin: false};
                    const at = own.findIndex(p => p.name === edited.name);
                    if (at >= 0) { own[at] = edited; } else { own.push(edited); }
                    active = edited.name;
                } else {
                    const at = own.findIndex(p => p.name === preset.name);
                    if (at >= 0) { own[at] = {...own[at], ...boxes}; }
                }
            }
            return {active, own};
        }

        async function saveConfigPayload(payload, onSaved = null) {
            if (configSavePending) { throw new Error('Wait for the current configuration save to finish.'); }
            configSavePending = true;
            const controls = Array.from(document.getElementById('config-form')?.querySelectorAll?.('[data-config-save-action]') || [])
                .map(button => ({button, disabled: button.disabled}));
            controls.forEach(({button}) => { button.disabled = true; });
            const saveButtons = ['config-save-button', 'config-save-button-top']
                .map(id => document.getElementById(id)).filter(Boolean)
                .map(button => ({button, label: button.innerHTML}));
            saveButtons.forEach(({button}) => { button.innerHTML = 'Saving…'; });
            const status = document.getElementById('config-save-status');
            if (status) { status.textContent = 'Saving configuration…'; }
            try {
                await API.post('/api/config', payload);
                if (status) { status.textContent = 'Configuration saved.'; }
                try {
                    if (onSaved) { await onSaved(); }
                } catch (error) {
                    console.debug('Configuration saved, but saved UI feedback failed', error);
                    if (status) { status.textContent = 'Configuration saved, but the saved status could not be displayed completely. Your form fields are kept. Reload to view the saved settings before making further changes.'; }
                }
            } catch (error) {
                if (status) { status.textContent = 'Save was not confirmed. Your form fields are kept. Review the error and current saved settings before trying again.'; }
                throw error;
            } finally {
                configSavePending = false;
                controls.forEach(({button, disabled}) => { button.disabled = disabled; });
                saveButtons.forEach(({button, label}) => { button.innerHTML = label; });
            }
        }

        function getPromptEditorBoxes(pass) {
            if (pass === 'attribution') { return promptBoxes(); }
            const fields = passPromptFields(pass);
            return {system_prompt: document.getElementById(fields.system).value,
                user_prompt: document.getElementById(fields.user).value};
        }

        function applyPromptPresetSave(pass, presets, active, submitted) {
            const current = getPromptEditorBoxes(pass);
            if (pass === 'attribution') { renderPromptPresets(presets, active); }
            else { renderPassPromptPresets(pass, presets, active); }
            if (JSON.stringify(current) !== JSON.stringify(submitted)) {
                const fields = pass === 'attribution' ? {system: 'system-prompt', user: 'user-prompt'} : passPromptFields(pass);
                document.getElementById(fields.system).value = current.system_prompt;
                document.getElementById(fields.user).value = current.user_prompt;
                if (pass === 'attribution') { document.getElementById('prompt-example').value = current.example; }
            }
        }

        async function persistPromptPresets(overrides, onSaved) {
            syncCurrentLlmProfile();
            const workers = Math.max(1, parseInt(document.getElementById('parallel-workers').value) || 2);
            const payload = buildConfigPayload(workers);
            payload.prompts = {...payload.prompts, ...overrides.prompts};
            if (overrides.prompt_presets) { payload.prompt_presets = overrides.prompt_presets; }
            await saveConfigPayload(payload, onSaved);
        }

        async function reloadPromptPresets(activeName, savedConfig = null, editorSnapshot = getPromptPresetEditorSnapshot('attribution')) {
            const config = savedConfig || await API.get('/api/config');
            if (!Array.isArray(config.prompt_presets)) { throw new Error('Saved prompt preset list is unavailable'); }
            if (editorSnapshot !== getPromptPresetEditorSnapshot('attribution')) { return false; }
            activePromptPreset = activeName || (config.prompts && config.prompts.attribution_preset) || 'default';
            renderPromptPresets(config.prompt_presets, activePromptPreset);
            return true;
        }

        let savedConfigRefreshPending = false;
        let savedConfigRefreshRequest = 0;
        let savedConfigPromptSnapshot = null;
        async function refreshSavedConfigFeedback(afterSave = false) {
            if (savedConfigRefreshPending && !afterSave) { return; }
            const request = ++savedConfigRefreshRequest;
            const promptSnapshot = savedConfigPromptSnapshot;
            const status = document.getElementById('config-refresh-status');
            const retry = document.getElementById('config-refresh-retry');
            savedConfigRefreshPending = true;
            retry.disabled = true;
            status.textContent = 'Configuration saved. Refreshing saved status…';
            try {
                const config = await API.get('/api/config');
                if (request !== savedConfigRefreshRequest) { return; }
                if (!config || !['local', 'remote'].includes(config.llm_mode)) { throw new Error('Saved profile status is unavailable'); }
                currentIsRemote = !!config.is_remote;
                failoverIsRemote = !!config.failover_is_remote;
                savedLlmMode = config.llm_mode;
                renderActiveLlmModeBadge();
                renderConfigWarnings(config);
                const refreshed = await reloadPromptPresets(undefined, config, promptSnapshot);
                if (request !== savedConfigRefreshRequest) { return; }
                status.textContent = refreshed ? ''
                    : 'Configuration saved. Your later prompt edits were kept, so the saved preset list was not reloaded. Review your edits before saving again.';
                retry.hidden = true;
            } catch (error) {
                if (request !== savedConfigRefreshRequest) { return; }
                console.debug('Saved configuration refresh failed', error);
                status.textContent = 'Configuration saved, but saved profile or preset status could not be refreshed. Your current fields are kept. Check that Alexandria is running, then retry the status refresh; this does not save again.';
                retry.hidden = false;
            } finally {
                if (request === savedConfigRefreshRequest) {
                    savedConfigRefreshPending = false;
                    retry.disabled = false;
                }
            }
        }

        window.savePromptPreset = async () => {
            const current = selectedPromptPreset();
            const snapshot = getPromptPresetEditorSnapshot('attribution');
            const values = await showPresetEditor({title: 'Save attribution prompt preset',
                name: current && !current.builtin ? current.name : '',
                description: current?.description || '',
                validateName: name => promptPresets.some(p => p.builtin && p.name === name)
                    ? 'That name belongs to a built-in prompt; choose another.' : ''});
            if (!values) { return; }
            if (getPromptPresetEditorSnapshot('attribution') !== snapshot) {
                showToast('The prompt or preset selection changed. Review it before saving a preset.', 'warning');
                return;
            }
            const {name, description} = values;
            const preset = {name: name.trim(), description: description.trim(),
                variant: (current && current.variant) || 'default', ...promptBoxes(), builtin: false};
            const presets = promptPresets.map(item => ({...item}));
            const existing = presets.findIndex(p => !p.builtin && p.name === preset.name);
            if (existing >= 0) { presets[existing] = preset; }
            else { presets.push(preset); }
            const submitted = promptBoxes();
            try {
                await persistPromptPresets({prompts: {attribution_preset: preset.name}, prompt_presets: presets.filter(item => !item.builtin)},
                    () => applyPromptPresetSave('attribution', presets, preset.name, submitted));
                showToast('Prompt preset saved and selected.', 'success');
            } catch (e) { showActionError('Could not save prompt preset', e, 'Review the preset name and saved preset list before trying Save preset again. Your prompt fields remain available.'); }
        };

        window.deletePromptPreset = async () => {
            const preset = selectedPromptPreset();
            if (!preset || preset.builtin) { showToast('Built-in prompts cannot be deleted.', 'warning'); return; }
            try {
                await applyConfirmedPromptPresetDeletion('attribution',
                    `Delete preset "${preset.name}" and switch the active attribution prompt to "michel2_full"? This also saves your current Setup settings.`, async () => {
                        const presets = promptPresets.filter(p => p !== preset).map(item => ({...item}));
                        const submitted = promptBoxes();
                        await persistPromptPresets({prompts: {attribution_preset: 'michel2_full'}, prompt_presets: presets.filter(item => !item.builtin)},
                            () => applyPromptPresetSave('attribution', presets, 'michel2_full', submitted));
                        showToast('Prompt preset deleted.', 'success');
                    });
            }
            catch (e) { showActionError('Could not delete prompt preset', e, 'Review the saved preset list to check whether it was deleted before trying Delete again.'); }
        };

        window.previewAttributionPrompt = async () => {
            const request = {};
            window._attributionPreviewRequest = request;
            const snapshot = getPromptPresetEditorSnapshot('attribution');
            const contextChars = document.getElementById('tp-attribute-context-chars').value;
            const isCurrent = () => window._attributionPreviewRequest === request
                && getPromptPresetEditorSnapshot('attribution') === snapshot
                && document.getElementById('tp-attribute-context-chars').value === contextChars;
            const preset = selectedPromptPreset();
            const body = {variant: (preset && preset.variant) || 'default', ...promptBoxes(),
                context_chars: getNumFieldValue('tp-attribute-context-chars', 0, true)};
            try {
                const preview = await API.post('/api/prompts/attribution_preview', body);
                if (!isCurrent()) { return; }
                document.getElementById('prompt-preview-note').textContent = preview.note || '';
                document.getElementById('prompt-preview-system').textContent = preview.system_prompt || '';
                document.getElementById('prompt-preview-user').textContent = preview.user_message || '';
                document.getElementById('prompt-preview-panel').hidden = false;
            } catch (e) {
                if (isCurrent()) { showActionError('Could not render the prompt', e, 'Check the prompt templates and provider settings, then try What the model will see again.'); }
            }
        };

        function renderConfigWarnings(config) {
            const banner = document.getElementById('config-warning-banner');
            const message = document.getElementById('config-warning-msg');
            document.getElementById('config-load-retry').style.display = 'none';
            const warnings = Array.isArray(config.config_warnings) ? config.config_warnings : [];
            if (!warnings.length) {
                message.textContent = '';
                banner.style.display = 'none';
                return;
            }
            const details = warnings.map((warning) => {
                const field = warning && warning.field && warning.field !== '$'
                    ? warning.field + ': ' : '';
                return field + ((warning && warning.message) || 'Invalid saved setting ignored');
            }).join('; ');
            const recovery = config.config_needs_backup
                ? ' Saving will preserve the damaged file as a backup before replacing it.'
                : '';
            message.textContent = 'Some saved configuration could not be used; safe defaults are shown.'
                + recovery + ' ' + details;
            banner.style.display = '';
        }

        function populateLlmInputs(mode) {
            const p = llmProfiles[mode] || {};
            document.getElementById('llm-url').value = p.base_url || '';
            document.getElementById('llm-key').value = p.api_key || 'local';
            document.getElementById('llm-model').value = p.model_name || '';
            document.getElementById('llm-request-timeout').value =
                p.request_timeout_seconds ?? '';
            document.getElementById('llm-connect-timeout').value =
                p.connect_timeout_seconds ?? '';
            document.getElementById('llm-request-interval').value =
                p.request_interval_seconds ?? 0;
            document.getElementById('llm-api-retry-limit').value = p.api_retry_limit ?? '';
            document.getElementById('llm-on-api-exhaustion').value = p.on_api_exhaustion || 'fail';
            document.getElementById('llm-retry-initial-delay').value =
                p.retry_initial_delay_seconds ?? 1;
            document.getElementById('llm-retry-multiplier').value = p.retry_multiplier ?? 2;
            document.getElementById('llm-retry-max-delay').value =
                p.retry_max_delay_seconds ?? 30;
            document.getElementById('llm-retry-jitter').value = p.retry_jitter ?? 0.2;
            document.getElementById('llm-provider-headers').value =
                JSON.stringify(p.provider_headers || {}, null, 2);
            document.getElementById('llm-provider-extra-body').value =
                JSON.stringify(p.provider_extra_body || {}, null, 2);
            document.getElementById('llm-reasoning-effort').value = p.reasoning_effort || '';
            document.getElementById('llm-on-this-gpu').value = (p.on_this_gpu === true || p.on_this_gpu === false) ? String(p.on_this_gpu) : '';
            document.getElementById('llm-transport').value = p.transport || 'http';
        }

        function getOnThisGpuInput() {
            const raw = document.getElementById('llm-on-this-gpu').value;
            return raw === '' ? null : raw === 'true';
        }

        function getJsonObjectInput(id, label) {
            const raw = document.getElementById(id).value.trim();
            if (!raw) { return {}; }
            let parsed;
            try {
                parsed = JSON.parse(raw);
            } catch (e) {
                throw getConfigValidationError(id, label + ' must be valid JSON.');
            }
            if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
                throw getConfigValidationError(id, label + ' must be a JSON object.');
            }
            return parsed;
        }

        function getOptionalNumberInput(id, label) {
            const raw = document.getElementById(id).value.trim();
            if (!raw) { return null; }
            const value = Number(raw);
            if (!Number.isFinite(value)) {
                throw getConfigValidationError(id, label + ' must be a number.');
            }
            return value;
        }

        function getEditedLlmProfile() {
            return {
                base_url: document.getElementById('llm-url').value,
                api_key: document.getElementById('llm-key').value,
                model_name: document.getElementById('llm-model').value,
                request_timeout_seconds: getOptionalNumberInput('llm-request-timeout', 'Request timeout'),
                connect_timeout_seconds: getOptionalNumberInput('llm-connect-timeout', 'Connect timeout'),
                request_interval_seconds: getOptionalNumberInput('llm-request-interval', 'Minimum interval') ?? 0,
                api_retry_limit: getOptionalNumberInput('llm-api-retry-limit', 'API retry limit'),
                on_api_exhaustion: document.getElementById('llm-on-api-exhaustion').value || 'fail',
                retry_initial_delay_seconds: getOptionalNumberInput('llm-retry-initial-delay', 'Initial backoff') ?? 1,
                retry_multiplier: getOptionalNumberInput('llm-retry-multiplier', 'Backoff multiplier') ?? 2,
                retry_max_delay_seconds: getOptionalNumberInput('llm-retry-max-delay', 'Maximum backoff') ?? 30,
                retry_jitter: getOptionalNumberInput('llm-retry-jitter', 'Backoff jitter') ?? 0.2,
                provider_headers: getJsonObjectInput('llm-provider-headers', 'Custom headers'),
                provider_extra_body: getJsonObjectInput('llm-provider-extra-body', 'Custom request body'),
                reasoning_effort: document.getElementById('llm-reasoning-effort').value || null,
                on_this_gpu: getOnThisGpuInput(),
                transport: document.getElementById('llm-transport').value || 'http'
            };
        }

        function syncCurrentLlmProfile() {
            llmProfiles[currentLlmMode] = getEditedLlmProfile();
        }

        // Reflects the last-SAVED llm_mode, not the dropdown's live selection
        // (currentLlmMode) - the gap between the two is what tells the user
        // their Location change hasn't taken effect yet.
        function renderActiveLlmModeBadge() {
            const badge = document.getElementById('llm-active-mode-badge');
            if (!badge) { return; }
            const labels = { local: 'Local', remote: 'Remote (Thunder / network)' };
            badge.textContent = 'Active: ' + labels[savedLlmMode] + (currentLlmMode !== savedLlmMode
                ? ' · Selected: ' + labels[currentLlmMode] + ' — Save to apply' : '');
        }

        function onLlmModeChange(isInit) {
            const newMode = document.getElementById('llm-mode').value;
            if (!isInit && newMode !== currentLlmMode) {
                try {
                    syncCurrentLlmProfile();       // stash the mode we're leaving
                } catch (e) {
                    document.getElementById('llm-mode').value = currentLlmMode;
                    showConfigValidationError(e);
                    return;
                }
                currentLlmMode = newMode;
                populateLlmInputs(currentLlmMode); // show the mode we're entering
            }
            document.getElementById('llm-ssh-group').style.display =
                (newMode === 'remote') ? '' : 'none';
            document.getElementById('llm-test-result').innerHTML = '';
            renderActiveLlmModeBadge();
        }

        async function testLlmConnection() {
            const btn = document.getElementById('llm-test-btn');
            const out = document.getElementById('llm-test-result');
            const baseUrl = document.getElementById('llm-url').value.trim();
            if (!baseUrl) {
                out.className = 'ms-2 small text-danger';
                out.innerHTML = '<i class="fas fa-times me-1"></i>Enter a Base URL before testing.';
                return;
            }
            const request = {};
            testLlmConnection.request = request;
            const mode = currentLlmMode;
            let profile;
            const isCurrent = () => {
                if (testLlmConnection.request !== request || currentLlmMode !== mode) { return false; }
                try { return !profile || JSON.stringify(getEditedLlmProfile()) === JSON.stringify(profile); }
                catch (_) { return false; }
            };
            btn.disabled = true;
            out.className = 'ms-2 small text-muted';
            out.textContent = 'Testing…';
            try {
                profile = getEditedLlmProfile();
                const res = await API.post('/api/llm/test', profile);
                if (!isCurrent()) { return; }
                if (res.ok) {
                    out.className = 'ms-2 small text-success';
                    const note = res.model_present === false
                        ? ` — warning: model "${escapeHtml(res.model)}" not in server list` : '';
                    out.innerHTML = `<i class="fas fa-check me-1"></i>Connected. Reply: "${escapeHtml(res.reply || '')}"${note}`;

                    const modeLabel = res.is_remote ? 'Remote' : 'Local';
                    const banner = document.getElementById('auto-config-banner');
                    document.getElementById('auto-config-msg').innerHTML =
                        `<i class="fas fa-check-circle me-1 text-success"></i><strong>Connection check:</strong> ` +
                        `${modeLabel} LLM connected (${escapeHtml(res.base_url)}, model "${escapeHtml(res.model)}"). This check does not save or apply settings. Click Save Configuration to apply edits.`;
                    banner.style.display = '';
                    banner.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                } else {
                    out.className = 'ms-2 small text-danger';
                    const log = res.log_file ? ` (log: ${escapeHtml(res.log_file)})` : '';
                    out.innerHTML = `<i class="fas fa-times me-1"></i>Failed at ${escapeHtml(res.step)}: ${escapeHtml(res.error || '')}${log}`;
                }
            } catch (e) {
                if (!isCurrent()) { return; }
                out.className = 'ms-2 small text-danger';
                out.textContent = 'Test request failed: ' + (e.message || 'unknown error');
            } finally {
                if (testLlmConnection.request === request) { btn.disabled = false; }
            }
        }

        const generationControlFields = {
            'max-tokens': 'max_tokens', 'temperature': 'temperature', 'top-p': 'top_p',
            'top-k': 'top_k', 'min-p': 'min_p', 'presence-penalty': 'presence_penalty',
            'tp-chunk-size': 'three_pass_chunk_size', 'tp-attribute-batch-size': 'three_pass_attribute_batch_size',
            'tp-attribute-context-chars': 'three_pass_attribute_context_chars',
            'tp-segment-output-ratio': 'three_pass_segment_output_ratio',
            'tp-segment-temperature': 'three_pass_segment_temperature',
            'tp-attribute-temperature': 'three_pass_attribute_temperature',
            'tp-instruct-temperature': 'three_pass_instruct_temperature',
            'tp-segmentation': 'three_pass_segmentation', 'context-rescue-retries': 'context_rescue_retries',
        };
        function applyGenerationSettings(g) {
            if (g.chunk_size != null) { legacyChunkSize = g.chunk_size; }
            for (const [id, key] of Object.entries(generationControlFields)) {
                if (g[key] != null) { document.getElementById(id).value = g[key]; }
            }
            if (Array.isArray(g.banned_tokens)) { document.getElementById('banned-tokens').value = g.banned_tokens.join(', '); }
            if (Array.isArray(g.context_rescue_windows)) { document.getElementById('context-rescue-windows').value = g.context_rescue_windows.join(', '); }
            document.getElementById('merge-narrators').checked = !!g.merge_narrators;
            document.getElementById('tp-quoted-must-be-spoken').checked = g.three_pass_quoted_must_be_spoken !== false;
            document.getElementById('tp-unquoted-must-be-narrator').checked = g.three_pass_unquoted_must_be_narrator !== false;
            document.getElementById('tp-keep-whole-batch').checked = g.three_pass_keep_whole_batch === true;
            document.getElementById('tp-group-rule').checked = g.three_pass_group_rule === true;
            document.getElementById('tp-speaker-traits').checked = g.three_pass_speaker_traits === true;
        }

        let currentBookFilename = '';
        function applyCurrentBookFilename(filename) {
            currentBookFilename = typeof filename === 'string' ? filename : '';
        }
        function enqueueBookSelection(request, action) {
            const previous = window._existingUploadSelectionPending || Promise.resolve();
            const pending = (async () => {
                await previous.catch(() => {});
                if (window._existingUploadSelectionRequest !== request) { return; }
                return action();
            })();
            window._existingUploadSelectionPending = pending;
            return pending;
        }
        function getCurrentBookName(fallback = 'book') {
            return currentBookFilename.trim().replace(/\.[^.]+$/, '') || fallback;
        }

        let promptDefaultsLoadPending = false;
        async function loadMissingPromptDefaults() {
            if (promptDefaultsLoadPending) { return; }
            const status = document.getElementById('prompt-defaults-status');
            const retry = document.getElementById('prompt-defaults-retry');
            const fields = {
                'review-system-prompt': 'review_system_prompt',
                'review-user-prompt': 'review_user_prompt',
                'persona-system-prompt': 'persona_system_prompt',
                'persona-user-prompt': 'persona_user_prompt',
                'persona-advanced-prompt': 'persona_advanced_prompt',
                'pass1-system-prompt': 'pass1_system_prompt',
                'pass1-user-prompt': 'pass1_user_prompt',
                'pass3-system-prompt': 'pass3_system_prompt',
                'pass3-user-prompt': 'pass3_user_prompt'
            };
            const before = Object.fromEntries(Object.keys(fields).map(id => [id, document.getElementById(id).value]));
            const selected = {...activePassPromptPreset};
            promptDefaultsLoadPending = true;
            retry.disabled = true;
            status.textContent = 'Loading default prompts… Your current edits will be kept.';
            try {
                const defaults = await API.get('/api/default_prompts');
                const required = Object.entries(fields).filter(([id]) => id !== 'persona-advanced-prompt' && (!before[id] || id.startsWith('pass')));
                if (!defaults || required.some(([, key]) => typeof defaults[key] !== 'string' || !defaults[key].trim())) {
                    throw new Error('Default prompt response is incomplete');
                }
                for (const pass of ['pass1', 'pass3']) {
                    passPromptDefaults[pass] = {system_prompt: defaults[`${pass}_system_prompt`], user_prompt: defaults[`${pass}_user_prompt`]};
                }
                for (const [id, key] of Object.entries(fields)) {
                    const field = document.getElementById(id);
                    const pass = id.startsWith('pass1-') ? 'pass1' : id.startsWith('pass3-') ? 'pass3' : null;
                    if (!before[id] && field.value === before[id] && typeof defaults[key] === 'string'
                        && (!pass || (selected[pass] === 'default' && activePassPromptPreset[pass] === selected[pass]))) {
                        field.value = defaults[key];
                    }
                }
                status.textContent = 'Default prompts loaded. Existing edits and preset choices were kept.';
                retry.hidden = true;
            } catch (error) {
                console.warn('Could not fetch default prompts', error);
                status.textContent = 'Could not load default prompts. Your current fields are unchanged. Check that Alexandria is running, then retry loading prompts; review the prompts before saving or starting a task.';
                retry.hidden = false;
            } finally {
                promptDefaultsLoadPending = false;
                retry.disabled = false;
            }
        }

        async function loadConfig() {
            const request = {};
            loadConfig.request = request;
            const isCurrent = () => loadConfig.request === request;
            const getDraftSnapshot = () => JSON.stringify(Array.from(document.getElementById('config-form').elements || [])
                .filter(field => !['button', 'submit', 'reset'].includes(field.type))
                .map(field => [field.id, field.value, field.checked]));
            const snapshot = getDraftSnapshot();
            const showKeptDraft = () => {
                document.getElementById('config-warning-msg').textContent = 'Your settings changed while configuration was loading. Your edits were kept. Retry loading to review replacing them with saved settings.';
                document.getElementById('config-warning-banner').style.display = '';
                document.getElementById('config-load-retry').style.display = '';
            };
            try {
                if (loadConfig.draftSnapshot !== undefined && snapshot !== loadConfig.draftSnapshot) {
                    if (!await showConfirm('Reload saved configuration? This replaces the edits currently shown in Setup. Discard these unsaved settings?', {title: 'Replace edited settings?', actionLabel: 'Reload settings', danger: true})) { return; }
                    if (!isCurrent()) { return; }
                    if (snapshot !== getDraftSnapshot()) { showKeptDraft(); return; }
                }
                const config = await API.get('/api/config');
                if (!isCurrent()) { return; }
                if (snapshot !== getDraftSnapshot()) { showKeptDraft(); return; }
                legacyChunkSize = 3000;
                document.getElementById('max-tokens').value = 4096;
                renderConfigWarnings(config);
                applyPauseSupport(config.capabilities);
                // Local/Remote LLM profiles: keep both in memory, show the active one.
                llmProfiles.local = config.llm_local || config.llm || {base_url:'', api_key:'local', model_name:''};
                llmProfiles.remote = config.llm_remote || {base_url:'', api_key:'local', model_name:''};
                currentLlmMode = config.llm_mode || 'local';
                savedLlmMode = currentLlmMode;
                currentIsRemote = !!config.is_remote;
                failoverIsRemote = !!config.failover_is_remote;
                document.getElementById('llm-failover').checked = !!config.llm_failover;
                renderActiveLlmModeBadge();
                document.getElementById('llm-mode').value = currentLlmMode;
                document.getElementById('llm-ssh').value = config.llm_remote_ssh || '';
                populateLlmInputs(currentLlmMode);
                onLlmModeChange(true);
                document.getElementById('tts-mode').value = config.tts.mode || 'local';
                document.getElementById('tts-url').value = config.tts.url || 'http://127.0.0.1:7860';
                document.getElementById('tts-external-urls').value = (config.tts.external_urls || []).join('\n');
                if (config.tts.external_timeout_seconds != null) { document.getElementById('tts-external-timeout').value = config.tts.external_timeout_seconds; }
                document.getElementById('tts-device').value = config.tts.device || 'auto';
                document.getElementById('tts-language').value = config.tts.language || 'English';
                document.getElementById('parallel-workers').value = config.tts.parallel_workers || 2;
                if (config.tts.batch_seed != null) {
                    document.getElementById('batch-seed').value = config.tts.batch_seed;
                }
                document.getElementById('compile-codec').checked = !!config.tts.compile_codec;
                if (config.tts.max_new_tokens != null) {
                    document.getElementById('tts-max-new-tokens').value = config.tts.max_new_tokens;
                }
                document.getElementById('batch-group-by-type').checked = !!config.tts.batch_group_by_type;
                document.getElementById('sub-batch-enabled').checked = config.tts.sub_batch_enabled !== false;
                toggleSubBatchFields();
                if (config.tts.sub_batch_min_size != null) {
                    document.getElementById('sub-batch-min-size').value = config.tts.sub_batch_min_size;
                }
                if (config.tts.sub_batch_ratio != null) {
                    document.getElementById('sub-batch-ratio').value = config.tts.sub_batch_ratio;
                }
                if (config.tts.sub_batch_max_items != null) {
                    document.getElementById('sub-batch-max-items').value = config.tts.sub_batch_max_items;
                }
                if (config.tts.pause_between_speakers_ms != null) {
                    document.getElementById('pause-between-speakers').value = config.tts.pause_between_speakers_ms;
                }
                if (config.tts.pause_same_speaker_ms != null) {
                    document.getElementById('pause-same-speaker').value = config.tts.pause_same_speaker_ms;
                }
                toggleTTSMode();

                // Load custom prompts if they exist and are non-empty
                if (config.prompts) {
                    if (config.prompts.system_prompt) {
                        document.getElementById('system-prompt').value = config.prompts.system_prompt;
                    }
                    if (config.prompts.user_prompt) {
                        document.getElementById('user-prompt').value = config.prompts.user_prompt;
                    }
                    if (config.prompts.review_system_prompt) {
                        document.getElementById('review-system-prompt').value = config.prompts.review_system_prompt;
                    }
                    if (config.prompts.review_user_prompt) {
                        document.getElementById('review-user-prompt').value = config.prompts.review_user_prompt;
                    }
                    if (config.prompts.persona_system_prompt) {
                        document.getElementById('persona-system-prompt').value = config.prompts.persona_system_prompt;
                    }
                    if (config.prompts.persona_user_prompt) {
                        document.getElementById('persona-user-prompt').value = config.prompts.persona_user_prompt;
                    }
                    if (config.prompts.persona_advanced_prompt) {
                        document.getElementById('persona-advanced-prompt').value = config.prompts.persona_advanced_prompt;
                    }
                }
                activePromptPreset = (config.prompts && config.prompts.attribution_preset) || 'michel2_full';
                renderPromptPresets(config.prompt_presets || [], activePromptPreset);
                renderPassPromptPresets('pass1', config.prompts?.pass1_prompt_presets || [],
                    config.prompts?.pass1_preset || 'default');
                renderPassPromptPresets('pass3', config.prompts?.pass3_prompt_presets || [],
                    config.prompts?.pass3_preset || 'default');

                // Apply saved generation values before yielding to the defaults request.
                if (config.generation) { applyGenerationSettings(config.generation); }

                // If review/persona/pass prompts are still empty, fetch defaults.
                if (!document.getElementById('review-system-prompt').value || !document.getElementById('review-user-prompt').value
                    || !document.getElementById('persona-system-prompt').value || !document.getElementById('persona-user-prompt').value
                    || !passPromptDefaults.pass1.system_prompt || !passPromptDefaults.pass3.system_prompt) {
                    await loadMissingPromptDefaults();
                }

                if (!isCurrent()) { return; }

                applyCurrentBookFilename(config.current_file);
                // Show previously loaded file
                if (config.current_file) {
                    document.getElementById('upload-status').innerHTML =
                        `<span class="text-success"><i class="fas fa-check me-1"></i>Loaded: ${escapeHtml(config.current_file)}</span>`;
                }
                loadConfig.draftSnapshot = getDraftSnapshot();
            } catch (e) {
                if (!isCurrent()) { return; }
                if (loadConfig.draftSnapshot === undefined) { loadConfig.draftSnapshot = snapshot; }
                console.error("Failed to load config", e);
                document.getElementById('config-warning-msg').textContent =
                    'Could not load configuration: ' + (e.message || String(e)) + '. Retry before saving settings.';
                document.getElementById('config-warning-banner').style.display = '';
                document.getElementById('config-load-retry').style.display = '';
            }
        }

        // Reset prompts and generation settings to factory defaults
        window.resetPrompts = async () => {
            try {
                const defaults = await API.get('/api/default_prompts');
                const g = defaults.generation;
                if (!g || Object.values(generationControlFields).some(key => g[key] == null)
                    || g.chunk_size == null || !Array.isArray(g.banned_tokens) || !Array.isArray(g.context_rescue_windows)
                    || ['merge_narrators', 'three_pass_quoted_must_be_spoken', 'three_pass_unquoted_must_be_narrator', 'three_pass_keep_whole_batch', 'three_pass_group_rule', 'three_pass_speaker_traits'].some(key => typeof g[key] !== 'boolean')) {
                    throw new Error('Generation defaults are unavailable');
                }

                if (!await showConfirm('Reset prompt and generation settings? Your current form edits will be replaced. Click Save Configuration afterward to keep the defaults.', {title: 'Replace prompt settings?', actionLabel: 'Reset form', danger: true})) { return; }

                renderPromptPresets(promptPresets, 'default');
                passPromptDefaults.pass1.system_prompt = defaults.pass1_system_prompt || '';
                passPromptDefaults.pass1.user_prompt = defaults.pass1_user_prompt || '';
                passPromptDefaults.pass3.system_prompt = defaults.pass3_system_prompt || '';
                passPromptDefaults.pass3.user_prompt = defaults.pass3_user_prompt || '';
                renderPassPromptPresets('pass1', passPromptPresets.pass1, 'default');
                renderPassPromptPresets('pass3', passPromptPresets.pass3, 'default');
                if (defaults.review_system_prompt) {
                    document.getElementById('review-system-prompt').value = defaults.review_system_prompt;
                }
                if (defaults.review_user_prompt) {
                    document.getElementById('review-user-prompt').value = defaults.review_user_prompt;
                }
                if (defaults.persona_system_prompt) {
                    document.getElementById('persona-system-prompt').value = defaults.persona_system_prompt;
                }
                if (defaults.persona_user_prompt) {
                    document.getElementById('persona-user-prompt').value = defaults.persona_user_prompt;
                }
                if (defaults.persona_advanced_prompt) {
                    document.getElementById('persona-advanced-prompt').value = defaults.persona_advanced_prompt;
                }
                applyGenerationSettings(g);
                showToast('Default prompts and generation settings are on this form. Click Save Configuration to keep them.', 'success');
            } catch (e) {
                console.error("Failed to fetch default prompts", e);
                showToast("Could not load defaults. Check the Alexandria server, then try Reset to Defaults again.", 'error');
            }
        };

        // Toggle chevron on collapse
        document.getElementById('promptSettings')?.addEventListener('show.bs.collapse', () => {
            document.getElementById('prompt-chevron').classList.replace('fa-chevron-right', 'fa-chevron-down');
        });
        document.getElementById('promptSettings')?.addEventListener('hide.bs.collapse', () => {
            document.getElementById('prompt-chevron').classList.replace('fa-chevron-down', 'fa-chevron-right');
        });

        // The whole payload in one place, so a bad field (a non-numeric rescue
        // window, malformed JSON) throws here and is shown, not swallowed.
        function buildConfigPayload(parallelWorkers) {
            const presetPayload = promptPresetPayload();
            const pass1Payload = getPassPromptPresetPayload('pass1');
            const pass3Payload = getPassPromptPresetPayload('pass3');
            return {
                llm: llmProfiles[currentLlmMode],
                llm_mode: currentLlmMode,
                llm_local: llmProfiles.local,
                llm_remote: llmProfiles.remote,
                llm_remote_ssh: document.getElementById('llm-ssh').value.trim() || null,
                llm_failover: document.getElementById('llm-failover').checked,
                tts: {
                    mode: document.getElementById('tts-mode').value,
                    url: document.getElementById('tts-url').value,
                    external_urls: document.getElementById('tts-external-urls').value.split('\n').map(u => u.trim()).filter(Boolean),
                    external_timeout_seconds: getNumFieldValue('tts-external-timeout', 300, true),
                    device: document.getElementById('tts-device').value,
                    language: document.getElementById('tts-language').value,
                    parallel_workers: parallelWorkers,
                    batch_seed: document.getElementById('batch-seed').value ? parseInt(document.getElementById('batch-seed').value) : null,
                    compile_codec: document.getElementById('compile-codec').checked,
                    max_new_tokens: getNumFieldValue('tts-max-new-tokens', 2048, true),
                    batch_group_by_type: document.getElementById('batch-group-by-type').checked,
                    sub_batch_enabled: document.getElementById('sub-batch-enabled').checked,
                    sub_batch_min_size: getNumFieldValue('sub-batch-min-size', 4, true),
                    sub_batch_ratio: getNumFieldValue('sub-batch-ratio', 5),
                    sub_batch_max_items: getNumFieldValue('sub-batch-max-items', 0, true),
                    pause_between_speakers_ms: getNumFieldValue('pause-between-speakers', 500, true),
                    pause_same_speaker_ms: getNumFieldValue('pause-same-speaker', 250, true)
                },
                prompts: {
                    system_prompt: document.getElementById('system-prompt').value,
                    user_prompt: document.getElementById('user-prompt').value,
                    attribution_preset: presetPayload.active,
                    review_system_prompt: document.getElementById('review-system-prompt').value,
                    review_user_prompt: document.getElementById('review-user-prompt').value,
                    persona_system_prompt: document.getElementById('persona-system-prompt').value,
                    persona_user_prompt: document.getElementById('persona-user-prompt').value,
                    persona_advanced_prompt: document.getElementById('persona-advanced-prompt').value,
                    pass1_preset: pass1Payload.active,
                    pass1_prompt_presets: pass1Payload.own,
                    pass3_preset: pass3Payload.active,
                    pass3_prompt_presets: pass3Payload.own
                },
                prompt_presets: presetPayload.own,
                generation: {
                    chunk_size: legacyChunkSize,
                    max_tokens: parseInt(document.getElementById('max-tokens').value) || 4096,
                    temperature: getNumFieldValue('temperature', 0.6),
                    top_p: getNumFieldValue('top-p', 0.8),
                    top_k: getNumFieldValue('top-k', 0, true),
                    min_p: getNumFieldValue('min-p', 0),
                    presence_penalty: getNumFieldValue('presence-penalty', 0.0),
                    banned_tokens: document.getElementById('banned-tokens').value
                        ? document.getElementById('banned-tokens').value.split(',').map(t => t.trim()).filter(t => t)
                        : [],
                    merge_narrators: document.getElementById('merge-narrators').checked,
                    three_pass_chunk_size: getNumFieldValue('tp-chunk-size', 3000, true),
                    three_pass_attribute_batch_size: getNumFieldValue('tp-attribute-batch-size', 25, true),
                    three_pass_attribute_context_chars: getNumFieldValue('tp-attribute-context-chars', 0, true),
                    three_pass_attribute_prompt_variant: (selectedPromptPreset() || {}).variant || 'michel2_full',
                    three_pass_segment_output_ratio: getNumFieldValue('tp-segment-output-ratio', 3.0),
                    three_pass_segment_temperature: getNumFieldValue('tp-segment-temperature', 0.1),
                    three_pass_attribute_temperature: getNumFieldValue('tp-attribute-temperature', 0.1),
                    three_pass_instruct_temperature: getNumFieldValue('tp-instruct-temperature', 0.1),
                    three_pass_segmentation: document.getElementById('tp-segmentation').value || 'auto',
                    three_pass_quoted_must_be_spoken: document.getElementById('tp-quoted-must-be-spoken').checked,
                    three_pass_unquoted_must_be_narrator: document.getElementById('tp-unquoted-must-be-narrator').checked,
                    three_pass_keep_whole_batch: document.getElementById('tp-keep-whole-batch').checked,
                    three_pass_group_rule: document.getElementById('tp-group-rule').checked,
                    three_pass_speaker_traits: document.getElementById('tp-speaker-traits').checked,
                    context_rescue_windows: getIntListInput('context-rescue-windows', 'Context rescue windows', [2000, 4000, 6000]),
                    context_rescue_retries: getNumFieldValue('context-rescue-retries', 2, true)
                }
            };
        }


        document.getElementById('config-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            if (configSavePending) { return; }

            // Validate parallel workers
            let parallelWorkers = parseInt(document.getElementById('parallel-workers').value) || 2;
            parallelWorkers = Math.max(1, parallelWorkers);
        document.getElementById('parallel-workers').value = parallelWorkers;

            // Persist whatever's currently shown into the active profile first,
            // then send both profiles + the active one (mirrored server-side into `llm`).
            try {
                syncCurrentLlmProfile();
            } catch (e) {
                showConfigValidationError(e);
                return;
            }
            let config;
            try {
                config = buildConfigPayload(parallelWorkers);
            } catch (e) {
                showConfigValidationError(e);
                return;
            }
            try {
                const promptSnapshot = getPromptPresetEditorSnapshot('attribution');
                await saveConfigPayload(config, async () => {
                    savedLlmMode = config.llm_mode;
                    savedConfigPromptSnapshot = promptSnapshot;
                    renderActiveLlmModeBadge();
                    showToast('Configuration Saved!', 'success');
                    await refreshSavedConfigFeedback(true);
                });
            } catch (e) {
                showActionError('Configuration save was not confirmed', e, 'Review the current saved settings and the error details before saving again. Your form fields remain available.');
            }
        });

        // --- Script Tab ---
        let existingUploadsLoadRequest = 0;

        async function loadExistingScriptUploads() {
            const request = ++existingUploadsLoadRequest;
            const status = document.getElementById('existing-uploads-status');
            try {
                const uploads = await API.get('/api/uploads');
                if (request !== existingUploadsLoadRequest) { return; }
                const options = uploads.map(item =>
                    `<option value="${escapeHtml(item.filename)}">${escapeHtml(item.filename)} (${Math.ceil(item.size / 1024)} KB)</option>`
                ).join('');
                document.getElementById('existing-upload-select').innerHTML =
                    '<option value="">Choose an existing TXT/MD file…</option>' + options;
                scriptBatchUploads = uploads;
                renderScriptBatchUploads();
                if (status) { status.innerHTML = ''; }
            } catch (e) {
                if (request !== existingUploadsLoadRequest) { return; }
                console.debug('Existing upload list unavailable', e);
                if (status) {
                    status.innerHTML = '<span class="text-warning">Could not refresh existing uploads. The displayed list may be out of date. Check that Alexandria is running, then retry.</span> <button type="button" class="btn btn-sm btn-outline-secondary" onclick="loadExistingScriptUploads()">Retry uploads</button>';
                }
            }
        }

        window.selectExistingScriptUpload = async () => {
            const select = document.getElementById('existing-upload-select');
            if (!select.value) { return; }
            const filename = select.value;
            let book = currentBookFilename;
            const request = {};
            window._existingUploadSelectionRequest = request;
            const isCurrent = () => window._existingUploadSelectionRequest === request
                && select.value === filename && currentBookFilename === book;
            if (!await ensureCastListEditsDiscardable()) {
                if (isCurrent()) { select.value = currentBookFilename; }
                return;
            }
            if (!isCurrent()) { return; }
            const statusEl = document.getElementById('upload-status');
            const selecting = enqueueBookSelection(request, async () => {
                if (!isCurrent()) { return; }
                const result = await API.post('/api/uploads/select', { filename });
                if (!isCurrent()) { return; }
                applyCurrentBookFilename(result.stored_filename);
                book = currentBookFilename;
                document.getElementById('file-upload').value = '';
                statusEl.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Reusing: ${escapeHtml(result.stored_filename)}</span>`;
                document.getElementById('cast-list-panel').style.display = 'none';
                clearCastListEditor();
                await loadCastList(false);
            });
            try { await selecting; }
            catch (e) {
                if (isCurrent()) { statusEl.innerHTML = `<span class="text-danger">${escapeHtml(getActionErrorMessage('Book selection was not confirmed', e, 'Check the current loaded book before selecting another upload.'))}</span>`; }
            } finally {
                if (window._existingUploadSelectionPending === selecting) { window._existingUploadSelectionPending = null; }
            }
        };

        document.getElementById('file-upload').addEventListener('change', async () => {
            const fileInput = document.getElementById('file-upload');
            const statusEl = document.getElementById('upload-status');
            if (fileInput.files.length === 0) { return; }
            const file = fileInput.files[0];
            if (!await ensureCastListEditsDiscardable()) { fileInput.value = ''; return; }
            if (fileInput.files[0] !== file) { return; }

            const request = {};
            window._existingUploadSelectionRequest = request;
            const isCurrent = () => window._existingUploadSelectionRequest === request
                && fileInput.files[0] === file;
            statusEl.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Loading file...</span>';
            try {
                await enqueueBookSelection(request, async () => {
                    if (!isCurrent()) { return; }
                    const res = await API.upload(file);
                    if (!isCurrent()) { return; }
                    applyCurrentBookFilename(res.stored_filename);
                    document.getElementById('existing-upload-select').value = '';
                    const verb = res.reused ? 'Reused existing copy' : 'Loaded';
                    statusEl.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>${verb}: ${escapeHtml(res.stored_filename)}</span>`;
                    document.getElementById('cast-list-panel').style.display = 'none';
                    clearCastListEditor();
                    await loadCastList(false);
                    if (!isCurrent()) { return; }
                    await loadExistingScriptUploads();
                });
            } catch (e) {
                if (!isCurrent()) { return; }
                statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(getActionErrorMessage('Book upload was not confirmed', e, 'Check the current loaded book and existing uploads before uploading again.'))}</span>`;
            }
        });

        // Generate resumes saved progress for the same text and settings;
        // Start over asks for a fresh run (#597). Same handler, one flag.
        let _scriptStartOver = false;
        // Start over while a run is active (#613): the server answers 409
        // ("Script generation is running") and the page showed nothing. Now the
        // click means "cancel this run, then start again": confirm, cancel,
        // wait for the task to report not running (bounded), then generate.
        async function waitForScriptToStop(maxMs = 30000) {
            const started = Date.now();
            while (Date.now() - started < maxMs) {
                let status;
                try { status = await API.get('/api/status/script'); } catch (e) { return false; }
                if (!status.running) { return true; }
                await new Promise(r => setTimeout(r, 500));
            }
            return false;
        }
        document.getElementById('btn-gen-script-fresh').addEventListener('click', async () => {
            if (document.getElementById('script-batch-mode')?.checked) {
                showToast('Start over applies to a single book. Turn off Batch Mode to use it.', 'warning');
                return;
            }
            const book = currentBookFilename;
            const batch = !!document.getElementById('script-batch-mode')?.checked;
            const isCurrent = () => book === currentBookFilename && batch === !!document.getElementById('script-batch-mode')?.checked;
            let running = false;
            try { running = !!(await API.get('/api/status/script')).running; }
            catch (e) {
                console.error('Could not check Script status before starting over:', e);
                showToast('Script status could not be checked. Check that Alexandria is running, then try Start over again.', 'warning');
                return;
            }
            const question = running
                ? 'Cancel the current run, discard its saved progress, and start again from chunk 1?'
                : 'Discard the saved progress for this text and start again from chunk 1?';
            if (!await showConfirm(question, {title: 'Discard script progress?', actionLabel: 'Start over', danger: true})) { return; }
            if (!isCurrent()) { showToast('The book or Batch Mode changed. Review it before starting over.', 'warning'); return; }
            if (running) {
                await cancelTask('/api/generate_script/cancel', { onSuccess: () => _resetPauseBtn('btn-pause-script') });
                if (!(await waitForScriptToStop())) {
                    showToast('The run has not stopped yet - try Start over again in a moment.', 'warning');
                    return;
                }
            }
            if (!isCurrent()) { showToast('The book or Batch Mode changed. Review it before starting over.', 'warning'); return; }
            _scriptStartOver = true;
            // the page's own poller re-enables Generate a tick after the run
            // stops; a click on a still-disabled button is silently dropped
            document.getElementById('btn-gen-script').disabled = false;
            document.getElementById('btn-gen-script').click();
        });
        let bookPreflightJob = null;
        let bookPreflightPending = false;

        function renderBookPreflightVisibility() {
            const batch = !!document.getElementById('script-batch-mode')?.checked;
            document.getElementById('btn-gen-script-fresh').style.display = batch ? 'none' : '';
            const controls = document.getElementById('book-preflight-controls');
            if (controls) { controls.hidden = batch && !bookPreflightPending && !bookPreflightJob; }
            document.getElementById('btn-book-preflight').style.display = batch ? 'none' : '';
        }

        function renderBookPreflight(result) {
            const panel = document.getElementById('book-preflight-result');
            panel.style.display = '';
            const stale = result.source_is_current === false
                ? '<div class="text-warning">The selected source has changed. This result belongs to the earlier source.</div>' : '';
            const summary = result.summary;
            let details = '';
            if (summary) {
                const plan = summary.planned_calls || {};
                details = `<div>Predicted full-book calls: Step 1 ${escapeHtml(plan[1] ?? 0)}, Step 2 ${escapeHtml(plan[2] ?? 0)}, Step 3 ${escapeHtml(plan[3] ?? 0)}. Retries can add calls.</div>`;
                details += (summary.samples || []).map(sample => {
                    const codes = Object.entries(sample.failure_codes || {}).map(([code, count]) => `${code}: ${count}`).join(', ');
                    return `<div>${escapeHtml(sample.label)} sample, source chunk ${escapeHtml(sample.chunk_index + 1)}: ${escapeHtml(sample.status)}${codes ? ` — ${escapeHtml(codes)}` : ''}${sample.error ? ` — ${escapeHtml(sample.error)}` : ''}</div>`;
                }).join('');
            }
            panel.innerHTML = `<strong>Book test: ${escapeHtml(result.status)}</strong> · ${escapeHtml(result.source_filename || '')}${stale}${details}${result.error ? `<div class="text-danger">${escapeHtml(result.error)}</div>` : ''}<div class="text-muted">Samples use the configured model and prompts. Passing samples does not establish full-book accuracy. Adjust settings or start generation when ready.</div>`;
            document.getElementById('btn-cancel-book-preflight').style.display = result.status === 'running' ? '' : 'none';
            renderBookPreflightVisibility();
        }

        function getLoadedScriptSourceFilename() {
            return currentBookFilename || '';
        }

        window.startBookPreflight = async () => {
            if (bookPreflightPending || bookPreflightJob) { return; }
            if (document.getElementById('script-batch-mode')?.checked) {
                showToast('Switch off Batch Mode to test one book with the LLM.', 'warning');
                return;
            }
            const source = getLoadedScriptSourceFilename();
            if (!source) { showToast('Select or upload a book before running preflight.', 'warning'); return; }
            const button = document.getElementById('btn-book-preflight');
            bookPreflightPending = true;
            button.disabled = true;
            try {
                if (!(await confirmIfRemote('this book sample test', true))) { return; }
                if (document.getElementById('script-batch-mode')?.checked) { return; }
                if (source !== getLoadedScriptSourceFilename()) {
                    showToast('The loaded book changed. Review it before running preflight.', 'warning'); return;
                }
                const result = await API.post('/api/generate_script/preflight', {
                    strip_front_matter: _isStripFrontMatterChecked(),
                    first_person_narrator: document.getElementById('script-first-person-narrator').value.trim() || null
                });
                const job = result.job_id;
                bookPreflightJob = job;
                renderBookPreflight(result);
                _startPolling('book_preflight', () => API.get(`/api/generate_script/preflight/${job}`), {
                    doneCheck: data => data.job_id === job && data.status !== 'running',
                    onTick: data => {
                        if (bookPreflightJob === job && data.job_id === job) { renderBookPreflight(data); }
                    },
                    onDone: data => {
                        if (bookPreflightJob !== job || data.job_id !== job) { return; }
                        bookPreflightJob = null;
                        button.disabled = false;
                        renderBookPreflightVisibility();
                    }
                });
            } catch (error) {
                showActionError('Book test was not confirmed', error, 'Review the current book-test status and selected source before testing or cancelling again.');
            } finally {
                bookPreflightPending = false;
                button.disabled = bookPreflightJob !== null;
                renderBookPreflightVisibility();
            }
        };

        window.cancelBookPreflight = async () => {
            const job = bookPreflightJob;
            if (!job) { return; }
            try {
                await API.post(`/api/generate_script/preflight/${job}/cancel`, {});
                showToast('Book-test cancellation queued; waiting for the worker to exit.', 'info');
            } catch (error) {
                showActionError('Book test was not confirmed', error, 'Review the current book-test status and selected source before testing or cancelling again.');
            }
        };
        // End book-preflight controls.

        document.getElementById('btn-gen-script').addEventListener('click', async () => {
            if (document.getElementById('btn-gen-script').disabled) { return; }
            const startOver = _scriptStartOver;
            _scriptStartOver = false;
            if (document.getElementById('script-batch-mode').checked) {
                return _startBatchScript();
            }

            const fileInput = document.getElementById('file-upload');
            const statusEl = document.getElementById('upload-status');

            const hasLoadedFile = !!getLoadedScriptSourceFilename();
            if (!hasLoadedFile && fileInput.files.length === 0) {
                statusEl.innerHTML = '<span class="text-danger"><i class="fas fa-exclamation-triangle me-1"></i>Please select a text file first using the file picker above.</span>';
                return;
            }

            // Single-book runs were never gated: a remote ACTIVE profile is a
            // visible choice in Setup. A remote FAILOVER target is not, so the
            // same prompt covers it here.
            if (!(await ensureScriptStartConfirmed('this script generation', true))) { return; }

            const genBtn = document.getElementById('btn-gen-script');
            const cancelBtn = document.getElementById('btn-cancel-script');
            const pauseBtn = document.getElementById('btn-pause-script');
            const retryBtn = document.getElementById('btn-retry-script');
            genBtn.disabled = true;
            retryBtn.style.display = 'none';
            cancelBtn.style.display = 'inline-block';
            pauseBtn.style.display = 'inline-block';
            pauseBtn.innerHTML = '<i class="fas fa-pause me-1"></i>Pause';
            pauseBtn.classList.remove('btn-outline-success');
            pauseBtn.classList.add('btn-outline-warning');
            _resetPauseBtn('btn-pause-script');

            try {
                await API.post('/api/generate_script', {
                    strip_front_matter: _isStripFrontMatterChecked(),
                    first_person_narrator:
                        document.getElementById('script-first-person-narrator').value.trim() || null,
                    start_over: startOver,
                });
                pollScriptLogs('script', () => {
                    if (!scriptBatchPoller) { genBtn.disabled = false; }
                    cancelBtn.style.display = 'none';
                    pauseBtn.style.display = 'none';
                    refreshScriptRecovery();
                });
            } catch (e) {
                genBtn.disabled = false;
                cancelBtn.style.display = 'none';
                pauseBtn.style.display = 'none';
                const detail = e.message || 'Unknown error';
                if (detail.includes('No input file')) {
                    statusEl.innerHTML = '<span class="text-danger"><i class="fas fa-exclamation-triangle me-1"></i>No file loaded. Please select a text file first.</span>';
                } else {
                    statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(getActionErrorMessage('Script generation start was not confirmed', e, 'Check the Script task state, selected book and Test Connection in Setup before starting again.'))}</span>`;
                }
            }
        });

        // Pause is SIGSTOP on the worker, which Windows does not have; the
        // server reports that in GET /api/config capabilities. Every Pause
        // button shares _makePauseResumeHandler, which returns before posting
        // when the button is disabled, and the start paths only touch
        // display/innerHTML - so disabling here is enough to make all six
        // inert without changing any of them (issue #588: "Pause failed" on
        // every click on Windows).
        function applyPauseSupport(capabilities) {
            if (!capabilities || capabilities.pause_resume !== false) { return; }
            const scriptHint = document.getElementById('script-pause-unavailable-help');
            if (scriptHint) { scriptHint.hidden = false; }
            ['btn-pause-script', 'btn-pause-batch-script'].forEach((id) => {
                const btn = document.getElementById(id);
                if (btn) { btn.setAttribute('aria-describedby', 'script-pause-unavailable-help'); }
            });
            document.querySelectorAll('button[onclick^="pauseResume"]').forEach((btn) => {
                btn.disabled = true;
                btn.title = 'Pause is not available on Windows. Use Cancel; a cancelled run resumes from its checkpoint.';
                btn.classList.remove('btn-outline-warning');
                btn.classList.add('btn-outline-secondary');
            });
        }

        // Restores the Pause appearance after a fresh start or cancellation;
        // click handlers read the current server state before choosing an action.
        function _resetPauseBtn(btnId) {
            const btn = document.getElementById(btnId);
            if (!btn) { return; }
            btn.innerHTML = '<i class="fas fa-pause me-1"></i>Pause';
            btn.classList.remove('btn-outline-success');
            btn.classList.add('btn-outline-warning');
        }

        // Shared by every cancel-button handler in this file - posts to the
        // cancel endpoint, runs an optional onSuccess callback, and toasts on
        // failure. Added so a new cancel button doesn't have to remember to
        // copy the try/catch+toast pattern by hand.
        async function cancelTask(url, {
            onSuccess,
            errorMessage = (e) => getActionErrorMessage('Cancellation was not confirmed', e, 'Check the current task state before cancelling again; work may still be running.'),
            toastType = 'warning',
        } = {}) {
            try {
                await API.post(url, {});
                if (onSuccess) { onSuccess(); }
                return true;
            } catch (e) {
                showToast(errorMessage(e), toastType);
                return false;
            }
        }

        const scriptCancellationRequests = new Map();

        function clearScriptCancellation(taskName) {
            const operation = scriptCancellationRequests.get(taskName);
            if (!operation) { return; }
            scriptCancellationRequests.delete(taskName);
            operation.button.disabled = false;
            operation.hint.hidden = true;
        }

        async function requestScriptCancellation(taskName, url, buttonId, pauseId, label) {
            if (scriptCancellationRequests.get(taskName)?.pending) { return false; }
            const button = document.getElementById(buttonId);
            const hint = document.getElementById('script-cancellation-status');
            const operation = { button, hint, pending: true };
            scriptCancellationRequests.set(taskName, operation);
            button.disabled = true;
            hint.hidden = false;
            hint.textContent = `Requesting cancellation of ${label}…`;
            const accepted = await cancelTask(url, {
                onSuccess: () => {
                    if (scriptCancellationRequests.get(taskName) !== operation) { return; }
                    _resetPauseBtn(pauseId);
                    hint.textContent = `Cancelling ${label}… Waiting for the worker to stop.`;
                },
            });
            if (!accepted && scriptCancellationRequests.get(taskName) === operation) {
                operation.pending = false;
                button.disabled = false;
                hint.textContent = `Cancellation of ${label} was not confirmed. Check the task activity before trying again; work may still be running.`;
            }
            return accepted;
        }

        // Same "unknown error" fallback cancelTask's default errorMessage
        // uses, for callers (debounced autosaves) that POST a real body and
        // so can't go through cancelTask itself, which always posts {}.
        function _toastSaveError(action, e) {
            showActionError(`Failed to save ${action}`, e, 'Review the current saved settings and task state before retrying this action.', 'warning');
        }

        function _makePauseResumeHandler(pauseUrl, resumeUrl, btnId) {
            // Retry once or twice on 503 ("starting up, retry in a moment") instead of
            // making the user notice the toast and click again themselves.
            const postWithRetry = async (url) => {
                for (let attempt = 0; ; attempt++) {
                    try {
                        return await API.post(url, {});
                    } catch (e) {
                        if (e.status === 503 && attempt < 2) {
                            await new Promise(r => setTimeout(r, 700));
                            continue;
                        }
                        throw e;
                    }
                }
            };
            // The button is presentation; the server supplies action state.
            return async () => {
                const btn = document.getElementById(btnId);
                if (btn.disabled) { return; }
                btn.disabled = true;
                let paused;
                try {
                    try {
                        const taskName = Object.keys(PAUSE_BUTTON_FOR_TASK)
                            .find(task => PAUSE_BUTTON_FOR_TASK[task] === btnId);
                        if (!taskName) { throw new Error('Unknown pause task'); }
                        const status = await API.get(`/api/status/${taskName}`);
                        paused = !!status.paused;
                    } catch (e) {
                        showActionError('Task status check failed', e, 'Wait for a current status update before trying Pause or Resume again.', 'warning');
                        return;
                    }
                    if (!paused) {
                        await postWithRetry(pauseUrl);
                        btn.innerHTML = '<i class="fas fa-play me-1"></i>Resume';
                        btn.classList.remove('btn-outline-warning');
                        btn.classList.add('btn-outline-success');
                    } else {
                        await postWithRetry(resumeUrl);
                        btn.innerHTML = '<i class="fas fa-pause me-1"></i>Pause';
                        btn.classList.remove('btn-outline-success');
                        btn.classList.add('btn-outline-warning');
                    }
                } catch (e) {
                    showActionError((paused ? 'Resume' : 'Pause') + ' was not confirmed', e, 'Check the current task state before trying Pause or Resume again.', 'warning');
                } finally {
                    btn.disabled = false;
                }
            };
        }

        const _scriptPauseResume = _makePauseResumeHandler(
            '/api/generate_script/pause', '/api/generate_script/resume', 'btn-pause-script');
        const _batchPauseResume  = _makePauseResumeHandler(
            '/api/generate_script/batch/pause', '/api/generate_script/batch/resume', 'btn-pause-batch-script');

        // #600: the finished part of a running generation into the library,
        // run untouched. Loading it while the run continues is refused by the
        // library (the run would overwrite the active book when it finishes),
        // so the toast says how to use it.
        window.snapshotScript = async () => {
            const suggested = `${getCurrentBookName()} snapshot ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`;
            const book = currentBookFilename;
            const values = await showPresetEditor({title: 'Save snapshot', name: suggested, includeDescription: false,
                nameLabel: 'Snapshot name', actionLabel: 'Save snapshot',
                helperText: 'Save the finished part of this run to the library as:'});
            if (!values) { return; }
            if (book !== currentBookFilename) { showToast('The book changed. Review it before saving a snapshot.', 'warning'); return; }
            const name = values.name;
            try {
                const res = await API.post('/api/generate_script/snapshot', { name });
                showToast(`Snapshot "${res.name}" saved: ${res.entries} finished lines (${res.chunks_done} chunks split). To work on it now, cancel this run (Generate resumes it later) and load the snapshot from the library.`, 'success', 12000);
            } catch (e) {
                showActionError('Snapshot save was not confirmed', e, 'Check the saved snapshots before saving another copy.', 'warning');
            }
        };

        window.cancelScript = () => requestScriptCancellation('script', '/api/generate_script/cancel',
            'btn-cancel-script', 'btn-pause-script', 'script generation');
        window.pauseResumeScript      = _scriptPauseResume;

        async function refreshScriptRecovery() {
            const retryBtn = document.getElementById('btn-retry-script');
            const panel = document.getElementById('script-recovery-panel');
            try {
                const recovery = await API.get('/api/generate_script/recovery?include_detail=true');
                retryBtn.style.display = recovery.recoverable ? 'inline-block' : 'none';
                if (recovery.recoverable) {
                    const location = recovery.failed_pass
                        ? ` at ${recovery.failed_pass}`
                        : '';
                    showToast(`Generation can resume from its checkpoint${location}.`, 'warning');
                    renderScriptRecovery(recovery.detail || null);
                } else {
                    renderScriptRecovery(null);
                }
            } catch (e) {
                retryBtn.style.display = 'none';
                if (panel) {
                    panel.style.display = 'none';
                }
                console.debug('Script recovery status unavailable', e);
            }
        }

        // Failed-request recovery panel (issue #522 s23): where it failed, every
        // attempt with its HTTP status and category, the source, the exact
        // prompt, and the three ways out — resume, paste a segmentation, or
        // narrate the chunk as-is (split only at its quote marks).
        function renderScriptRecovery(detail) {
            const panel = document.getElementById('script-recovery-panel');
            if (!panel) {
                return;
            }
            if (!detail) {
                panel.style.display = 'none';
                panel.innerHTML = '';
                return;
            }
            window._scriptRecoveryDetail = detail;
            const last = detail.last_error || {};
            const attemptRows = (detail.attempts || []).map(a => `
                <tr>
                    <td>${escapeHtml(String(a.attempt ?? ''))}</td>
                    <td>${escapeHtml(a.outcome || '')}</td>
                    <td>${a.http_status != null ? escapeHtml(String(a.http_status)) : '—'}</td>
                    <td>${escapeHtml(a.error_category || (a.failure_codes || []).join(', ') || '')}</td>
                    <td>${a.finish_reason ? escapeHtml(a.finish_reason) : ''}${a.completion_tokens != null ? ` · ${a.completion_tokens} tok` : ''}</td>
                    <td>${a.next_retry_seconds != null ? escapeHtml(String(a.next_retry_seconds)) + ' s' : '—'}</td>
                    <td class="text-truncate" style="max-width: 24em;" title="${escapeHtml(a.error || '')}">${escapeHtml(a.error || '')}</td>
                </tr>`).join('');
            const profile = detail.retry_profile || {};
            const isAttribute = detail.failed_pass === 'attribute';
            const where = isAttribute
                ? `batch of ${(detail.batch_entries || []).length} entries (script entries ${escapeHtml(String((detail.batch_indices || [])[0] ?? '?'))}–${escapeHtml(String((detail.batch_indices || []).slice(-1)[0] ?? '?'))})`
                : `chunk ${escapeHtml(String(detail.failed_chunk))} of ${escapeHtml(String(detail.chunks_total))}`;
            const pasteHint = isAttribute
                ? 'Speakers JSON — <code>[{"n":0,"speaker":"NAME"}, …]</code>, one per batch entry (n as shown), NARRATOR lines stay NARRATOR'
                : 'Segmentation JSON — <code>[{"type":"NARRATOR"|"SPOKEN","text":"..."}]</code>, every word of the source, in order';
            const skipLabel = isAttribute ? 'Mark speakers UNKNOWN' : 'Narrate as-is';
            panel.style.display = '';
            panel.innerHTML = `
                <div class="card-header"><strong>Script generation needs your help</strong></div>
                <div class="card-body">
                    <p>Generation stopped because the model did not return a usable result. Review this section, then retry, use Copy prompt with another model, paste a result for validation, or use the fallback action below.</p>
                    <details class="mb-3">
                        <summary>Technical details</summary>
                    <strong><i class="fas fa-triangle-exclamation me-1"></i>Generation stopped at ${escapeHtml(detail.failed_pass || 'segment')} · ${where}</strong>
                    <span class="small text-muted">${escapeHtml(String(detail.chunks_done))} chunks accepted · retries ${escapeHtml(String(profile.api_retry_limit ?? 'default'))}, backoff ${escapeHtml(String(profile.retry_initial_delay_seconds))}s ×${escapeHtml(String(profile.retry_multiplier))} ±${Math.round((profile.retry_jitter || 0) * 100)}%, on exhaustion: ${escapeHtml(profile.on_api_exhaustion || 'fail')}</span>
                    <div class="small mb-2">Last error: <strong>${escapeHtml(last.category || (detail.failure_codes || []).join(', ') || detail.reason || 'unknown')}</strong>${last.http_status != null ? ` (HTTP ${escapeHtml(String(last.http_status))})` : ''}${last.error ? ` — ${escapeHtml(last.error)}` : ''}${isAttribute && detail.roster ? `<br>Roster: ${escapeHtml(detail.roster.join(', '))}` : ''}</div>
                    <div class="table-responsive mb-2">
                        <table class="table table-sm small mb-0">
                            <thead><tr><th>#</th><th>Outcome</th><th>HTTP</th><th>Category / codes</th><th>Finish</th><th>Next retry</th><th>Error</th></tr></thead>
                            <tbody>${attemptRows || '<tr><td colspan="7" class="text-muted">No attempt records for this chunk.</td></tr>'}</tbody>
                        </table>
                    </div>
                    </details>
                    <div class="row g-2">
                        <div class="col-md-6">
                            <label class="form-label small mb-1">${isAttribute ? 'Batch entries' : 'Source chunk'}</label>
                            <textarea class="form-control font-monospace" rows="8" readonly style="font-size: 0.8em;">${escapeHtml(detail.source || '')}</textarea>
                        </div>
                        <div class="col-md-6">
                            <label class="form-label small mb-1" for="script-recovery-inject">${pasteHint}</label>
                            <textarea class="form-control font-monospace" id="script-recovery-inject" rows="8" style="font-size: 0.8em;" placeholder='${isAttribute ? '[{"n": 0, "speaker": "NAME"}]' : '[{"type": "SPOKEN", "text": "..."}, {"type": "NARRATOR", "text": "..."}]'}'></textarea>
                        </div>
                    </div>
                    <div id="script-recovery-findings" class="small text-danger mt-2"></div>
                    <div class="d-flex flex-wrap gap-2 mt-2">
                        <button class="btn btn-sm btn-primary" onclick="retryScriptGeneration()"><i class="fas fa-rotate-right me-1"></i>Retry this chunk</button>
                        <button class="btn btn-sm btn-outline-secondary" onclick="onCopyRecoveryPrompt()"><i class="fas fa-copy me-1"></i>Copy prompt</button>
                        <button class="btn btn-sm btn-outline-success" onclick="onInjectRecoverySegmentation()"><i class="fas fa-file-import me-1"></i>Validate &amp; apply segmentation</button>
                        <button class="btn btn-sm btn-outline-warning" onclick="onSkipRecoveryChunk()"><i class="fas fa-forward me-1"></i>${skipLabel}</button>
                    </div>
                </div>`;
        }

        async function onCopyRecoveryPrompt() {
            const detail = window._scriptRecoveryDetail;
            if (!detail || !detail.prompt) {
                return;
            }
            const text = `SYSTEM:\n${detail.prompt.system}\n\nUSER:\n${detail.prompt.user}`;
            await copyToClipboard(text, 'Prompt');
        }

        function renderRecoveryFindings(detail) {
            const el = document.getElementById('script-recovery-findings');
            if (!el) {
                return;
            }
            if (!detail) {
                el.innerHTML = '';
                return;
            }
            const findings = (detail.findings || []).map(f => `<li>${escapeHtml(f.message || f.code || JSON.stringify(f))}${f.entry_number ? ` (entry ${escapeHtml(String(f.entry_number))})` : ''}</li>`).join('');
            el.innerHTML = `<div>${escapeHtml(detail.message || 'Rejected.')}</div>${findings ? `<ul class="mb-0">${findings}</ul>` : ''}`;
        }

        async function onInjectRecoverySegmentation() {
            const detail = window._scriptRecoveryDetail;
            const box = document.getElementById('script-recovery-inject');
            if (!detail || !box) {
                return;
            }
            let entries;
            try {
                entries = JSON.parse(box.value);
            } catch (e) {
                renderRecoveryFindings({ message: 'Not valid JSON: ' + e.message });
                return;
            }
            if (!Array.isArray(entries)) {
                renderRecoveryFindings({ message: 'Expected a JSON array of {type, text} entries.' });
                return;
            }
            try {
                const result = await API.post('/api/generate_script/inject', { chunk: detail.failed_chunk, entries });
                renderRecoveryFindings(null);
                showToast(`Accepted (${result.resolution}). Resume to continue.`, 'success');
                await refreshScriptRecovery();
            } catch (e) {
                renderRecoveryFindings(e.detail && typeof e.detail === 'object' ? e.detail : { message: e.message });
            }
        }

        async function onSkipRecoveryChunk() {
            const detail = window._scriptRecoveryDetail;
            if (!detail) {
                return;
            }
            const question = detail.failed_pass === 'attribute'
                ? 'Label every spoken line in this batch UNKNOWN and continue? You can fix speakers in the Editor afterwards.'
                : `Put chunk ${detail.failed_chunk} into the script split only at its quote marks (no speaker attribution beyond NARRATOR/SPOKEN)? Nothing is dropped.`;
            const book = currentBookFilename;
            const chunk = detail.failed_chunk;
            const pass = detail.failed_pass;
            if (!await showConfirm(question, {title: 'Continue with fallback script?', actionLabel: 'Use fallback', danger: false})) { return; }
            if (book !== currentBookFilename || window._scriptRecoveryDetail !== detail
                || detail.failed_chunk !== chunk || detail.failed_pass !== pass) {
                showToast('The book or recovery section changed. Review the latest recovery details before continuing.', 'warning'); return;
            }
            try {
                const result = await API.post('/api/generate_script/skip', { chunk });
                showToast(`Accepted (${result.resolution}). Resume to continue.`, 'success');
                await refreshScriptRecovery();
            } catch (e) {
                renderRecoveryFindings(e.detail && typeof e.detail === 'object' ? e.detail : { message: e.message });
            }
        }

        window.retryScriptGeneration = async () => {
            const genBtn = document.getElementById('btn-gen-script');
            const retryBtn = document.getElementById('btn-retry-script');
            const cancelBtn = document.getElementById('btn-cancel-script');
            const pauseBtn = document.getElementById('btn-pause-script');
            retryBtn.disabled = true;
            try {
                await API.post('/api/generate_script/retry', {
                });
                retryBtn.style.display = 'none';
                genBtn.disabled = true;
                cancelBtn.style.display = 'inline-block';
                pauseBtn.style.display = 'inline-block';
                _resetPauseBtn('btn-pause-script');
                pollScriptLogs('script', () => {
                    if (!scriptBatchPoller) { genBtn.disabled = false; }
                    cancelBtn.style.display = 'none';
                    pauseBtn.style.display = 'none';
                    refreshScriptRecovery();
                });
            } catch (e) {
                showActionError('Script resume was not confirmed', e, 'Check the Script task state, recovery panel and Test Connection in Setup before resuming again.', 'warning');
                refreshScriptRecovery();
            } finally {
                retryBtn.disabled = false;
            }
        };

        // --- Script Batch Mode ---
        let scriptBatchQueue = [];
        let scriptBatchUploads = [];
        let scriptBatchPoller = null;

        // Checkboxes instead of a native <select multiple> - selecting a
        // subset of a multi-select normally requires ctrl/shift-click, which
        // doesn't work over a remote-desktop session with no modifier keys.
        // Reuses _renderScriptCheckboxList (also used by the review-batch and
        // cast-bulk pickers) via its keyField/renderLabel/onchange/emptyMessage
        // options instead of a parallel duplicate implementation.
        function renderScriptBatchUploads() {
            _renderScriptCheckboxList(scriptBatchUploads, {
                containerId: 'script-existing-uploads',
                checkClass: 'script-batch-upload-check',
                idPrefix: 'script-batch-upload-',
                keyField: 'filename',
                onchange: 'onScriptBatchFilesChange()',
                emptyMessage: 'No existing uploads found.',
                renderLabel: (item) => `${escapeHtml(item.filename)} (${Math.ceil(item.size / 1024)} KB)`,
            });
        }

        window.toggleScriptBatchMode = () => {
            const isBatch = document.getElementById('script-batch-mode').checked;
            document.getElementById('script-single-area').style.display = isBatch ? 'none' : 'block';
            document.getElementById('script-batch-area').style.display  = isBatch ? 'block' : 'none';
            document.getElementById('script-batch-status-msg').style.display = isBatch ? '' : 'none';
            renderBookPreflightVisibility();
        };

        window.onScriptBatchFilesChange = () => {
            const files = document.getElementById('script-batch-files').files;
            const existing = [...document.querySelectorAll('.script-batch-upload-check:checked')];
            const tbody = document.getElementById('script-batch-queue-body');
            const narratorDrafts = new Map(scriptBatchQueue.map((item, index) => [
                item.file || item.storedFilename,
                document.getElementById(`script-batch-narrator-${index}`)?.value || ''
            ]));
            tbody.innerHTML = '';
            scriptBatchQueue = [];
            document.getElementById('script-batch-selected-count').textContent =
                `${files.length + existing.length} selected`;

            if (!files.length && !existing.length) {
                document.getElementById('script-batch-queue-container').style.display = 'none';
                return;
            }
            document.getElementById('script-batch-queue-container').style.display = 'block';

            [...files].forEach((file, i) => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td title="${escapeHtml(file.name)}" style="max-width:350px;overflow-wrap:anywhere;white-space:normal;">${escapeHtml(file.name)}</td>
                    <td><input class="form-control form-control-sm" id="script-batch-narrator-${i}" maxlength="100" placeholder="Exact character name"></td>
                    <td id="script-batch-status-${i}"><span class="badge bg-secondary">Pending</span></td>
                `;
                tbody.appendChild(row);
                document.getElementById(`script-batch-narrator-${i}`).value = narratorDrafts.get(file) || '';
                scriptBatchQueue.push({ file });
            });
            existing.forEach((checkbox) => {
                const i = scriptBatchQueue.length;
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td title="${escapeHtml(checkbox.dataset.name)}" style="max-width:350px;overflow-wrap:anywhere;white-space:normal;">${escapeHtml(checkbox.dataset.name)} <span class="text-muted">(existing)</span></td>
                    <td><input class="form-control form-control-sm" id="script-batch-narrator-${i}" maxlength="100" placeholder="Exact character name"></td>
                    <td id="script-batch-status-${i}"><span class="badge bg-secondary">Pending</span></td>
                `;
                tbody.appendChild(row);
                document.getElementById(`script-batch-narrator-${i}`).value = narratorDrafts.get(checkbox.dataset.name) || '';
                scriptBatchQueue.push({ storedFilename: checkbox.dataset.name });
            });
        };

        window.scriptBatchSelectAll = (on) => _selectAllCheckboxes(
            'script-batch-upload-check', on, onScriptBatchFilesChange);

        window.scriptBatchSort = (mode) => {
            // `name` is only needed transiently for _sortScriptList's
            // comparator (it ignores the upload extension and duplicate
            // suffix when finding a book's volume number - "Volume
            // 10_3.txt" is volume 10) - dropped again before persisting
            // back into scriptBatchUploads since nothing else reads it.
            const sortable = scriptBatchUploads.map(item => ({
                name: item.filename.replace(/\.[^.]+$/, '').replace(/_\d+$/, ''),
                filename: item.filename,
                size: item.size,
            }));
            _sortScriptList(sortable, mode);
            scriptBatchUploads = sortable.map(({ filename, size }) => ({ filename, size }));
            renderScriptBatchUploads();
            onScriptBatchFilesChange();
        };

        let scriptBatchStartOperation = null;
        window.cancelBatchScript = () => {
            if (scriptBatchStartOperation && !['started', 'unconfirmed'].includes(scriptBatchStartOperation.phase)) {
                scriptBatchStartOperation.cancelled = true;
                document.getElementById('script-batch-status-msg').innerHTML =
                    '<span class="text-muted">Cancelling batch preparation…</span>';
                return;
            }
            return requestScriptCancellation('batch_script', '/api/generate_script/batch/cancel',
                'btn-cancel-batch-script', 'btn-pause-batch-script', 'batch script generation');
        };

        window.pauseResumeBatchScript = _batchPauseResume;


        async function _startBatchScript() {
            if (scriptBatchStartOperation) { return; }
            if (!scriptBatchQueue.length) { showToast('No files selected', 'warning'); return; }
            if (!(await ensureScriptStartConfirmed('this batch script generation'))) { return; }
            if (scriptBatchStartOperation) { return; }
            const queue = scriptBatchQueue;
            const narratorValues = queue.map((_, index) => document.getElementById(`script-batch-narrator-${index}`).value);
            const collisionPolicy = document.getElementById('script-collision-policy').value;
            const operation = { cancelled: false, phase: 'preparing' };
            const isCurrentQueue = () => !operation.cancelled && scriptBatchQueue === queue
                && collisionPolicy === document.getElementById('script-collision-policy').value
                && queue.every((_, index) => narratorValues[index] === document.getElementById(`script-batch-narrator-${index}`)?.value);
            const ensureCurrentQueue = () => {
                if (!isCurrentQueue() && !operation.cancelled) {
                    operation.cancelled = true;
                    showToast('The batch selection changed during preparation. Check the queue and start again.', 'warning');
                }
                return !operation.cancelled;
            };
            scriptBatchStartOperation = operation;
            let started = false;
            let startUnconfirmed = false;

            const btn = document.getElementById('btn-gen-script');
            const pauseBtn = document.getElementById('btn-pause-batch-script');
            const statusMsg = document.getElementById('script-batch-status-msg');
            btn.disabled = true;
            pauseBtn.style.display = 'none';
            _resetPauseBtn('btn-pause-batch-script');
            document.getElementById('btn-cancel-batch-script').style.display = 'inline-block';
            statusMsg.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Uploading files…</span>';
            statusMsg.style.display = '';

            const logEl = document.getElementById('script-logs');
            logEl.innerHTML = '';

            try {
                // Upload files in parallel; map preserves queue order for `tasks`.
                const tasks = await Promise.all(queue.map(async (item, index) => {
                    const firstPersonNarrator =
                        document.getElementById(`script-batch-narrator-${index}`).value.trim() || null;
                    if (item.storedFilename) {
                        return {
                            filename: item.storedFilename,
                            first_person_narrator: firstPersonNarrator,
                        };
                    }
                    const res = await API.upload(item.file, {selectActive: false});
                    // Use stored_filename if provided (handles epub→txt), otherwise fall back
                    return {
                        filename: res.stored_filename || res.filename,
                        first_person_narrator: firstPersonNarrator,
                    };
                }));
                if (!ensureCurrentQueue()) { return; }

                const preflight = await API.post('/api/generate_script/batch/preflight', {
                    tasks, collision_policy: collisionPolicy
                });
                if (!ensureCurrentQueue()) { return; }
                const scripts = [...new Set(preflight.books.flatMap(book => book.scripts))];
                const fallback = preflight.fallback_reason
                    ? `\nSafety adjustment: ${preflight.fallback_reason}` : '';
                const overCeiling = preflight.books.filter(book => book.exceeds_output_ceiling);
                if (overCeiling.length) {
                    const suggested = Math.min(...overCeiling.map(book => book.suggested_chunk_size));
                    showToast(
                        `"Step 1: text per request" (${preflight.chunk_size} characters) is more than this model can write back in one reply ` +
                        `(${overCeiling.map(book => book.filename).join(', ')}). ` +
                        `Set it to ${suggested} or below in Setup, or raise Baseline Response Tokens.`, 'error');
                    statusMsg.innerHTML = '<span class="text-danger">Not started: the text-per-request setting is more than this model can write back in one reply.</span>';
                    btn.disabled = false;
                    pauseBtn.style.display = 'none';
                    document.getElementById('btn-cancel-batch-script').style.display = 'none';
                    return;
                }
                const approved = await showConfirm(
                    `Batch preflight (${preflight.book_count} book${preflight.book_count === 1 ? '' : 's'}):\n` +
                    `Concurrency: ${preflight.workers} (LM Studio loaded for ${preflight.loaded_parallel})\n` +
                    `Context per worker: ${preflight.per_slot_context.toLocaleString()} tokens\n` +
                    `Largest predicted request: ${preflight.worst_request_tokens.toLocaleString()} tokens\n` +
                    `Writing systems detected: ${scripts.join(', ') || 'none'}${fallback}\n\nStart generation?`,
                    {title: 'Start batch generation?', actionLabel: 'Start generation', danger: false});
                if (!ensureCurrentQueue()) { return; }
                if (!approved) {
                    statusMsg.innerHTML = '<span class="text-muted">Batch cancelled after preflight.</span>';
                    btn.disabled = false;
                    pauseBtn.style.display = 'none';
                    document.getElementById('btn-cancel-batch-script').style.display = 'none';
                    return;
                }

                statusMsg.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Processing…</span>';
                operation.phase = 'starting';
                await API.post('/api/generate_script/batch/start', {
                    tasks, collision_policy: collisionPolicy,
                    strip_front_matter: _isStripFrontMatterChecked(),
                    build_cast_lists: document.getElementById('script-batch-build-cast-lists').checked
                });
                started = true;
                operation.phase = 'started';
                pauseBtn.style.display = 'inline-block';
                _pollScriptBatchLogs();
                if (operation.cancelled) { await cancelBatchScript(); }
            } catch (e) {
                if (operation.phase === 'starting') {
                    const refused = Number.isInteger(e.status) && e.status >= 400 && e.status < 500 && e.status !== 408;
                    operation.phase = refused ? 'refused' : 'unconfirmed';
                    statusMsg.innerHTML = refused
                        ? '<span class="text-danger">Batch start was refused. Review the error before trying again.</span>'
                        : '<span class="text-warning">Batch start is unconfirmed. Checking task activity; generation may have started.</span>';
                    showActionError('Batch start was not confirmed', e, 'Check the batch task activity before starting another batch; work may still be running.');
                    if (!refused) {
                        startUnconfirmed = true;
                        _pollScriptBatchLogs();
                        if (operation.cancelled) { await cancelBatchScript(); }
                    }
                } else if (!operation.cancelled) { showActionError("Failed to start batch", e, "Check the batch task state, selected books and Test Connection in Setup before starting another batch."); }
            } finally {
                if (scriptBatchStartOperation === operation) { scriptBatchStartOperation = null; }
                if (!started && !startUnconfirmed) {
                    btn.disabled = false;
                    pauseBtn.style.display = 'none';
                    document.getElementById('btn-cancel-batch-script').style.display = 'none';
                    if (operation.cancelled && operation.phase === 'preparing') {
                        statusMsg.innerHTML = '<span class="text-muted">Batch cancelled before generation started.</span>';
                    }
                }
            }
        }

        loadExistingScriptUploads();

        function _pollScriptBatchLogs() {
            const logEl = document.getElementById('script-logs');
            const renderLogs = createTaskLogRenderer(logEl);
            // scriptBatchPoller is read elsewhere (pollLogs('script', ...)'s
            // onDone callbacks) to decide whether the single-script Generate
            // button should re-enable - it's a plain "is batch_script
            // currently polling" flag, not the timer id; the actual poll
            // loop is owned by _startPolling.
            scriptBatchPoller = true;

            _startPolling('batch_script', () => API.get('/api/status/batch_script'), {
                doneCheck: state => !state.running,
                onTick: state => {
                    renderLogs(state);

                    syncPauseButton('batch_script', state);
                    // manual transport: a batch (its cast lists included) waits on
                    // the user too; pollLogs does this for the single-book tasks
                    renderManualRequest(state, 'batch_script');
                    if (state.tasks) {
                        state.tasks.forEach((t, i) => {
                            const el = document.getElementById(`script-batch-status-${i}`);
                            if (!el) { return; }
                            const colours = { pending: 'secondary', running: 'primary', done: 'success', failed: 'danger', cancelled: 'warning' };
                            el.innerHTML = `<span class="badge bg-${colours[t.status] || 'secondary'}">${t.status}</span>`;
                        });
                    }
                },
                onDone: (state) => {
                    scriptBatchPoller = null;
                    clearScriptCancellation('batch_script');
                    renderManualRequest({ running: false }, 'batch_script');
                    _showTaskRecoveryPanel('script-batch-recovery-panel', 'batch_script', state,
                        'Inspect the log and resume failed books from their validated checkpoints.');
                    notifyJobDone('batch_script', '', 'finished', state);
                    document.getElementById('btn-gen-script').disabled = false;
                    document.getElementById('btn-pause-batch-script').style.display = 'none';
                    document.getElementById('btn-cancel-batch-script').style.display = 'none';
                    const tasks = state.tasks || [];
                    const done = tasks.filter(task => task.status === 'done').length;
                    const failed = tasks.filter(task => task.status === 'failed').length;
                    const cancelled = tasks.filter(task => task.status === 'cancelled').length;
                    const tone = failed ? 'text-warning' : 'text-muted';
                    document.getElementById('script-batch-status-msg').innerHTML =
                        `<span class="${tone}">Batch finished — ${done} completed, ${failed} failed, ${cancelled} cancelled. Failed books keep validated checkpoints.</span>`;
                    loadSavedScripts();
                }
            });
        }

        // --- Single review (with pause/cancel + character-name merging) ---
        function _isReviewDedupeChecked() {
            const cb = document.getElementById('review-dedupe-speakers');
            return cb ? cb.checked : true;
        }
        function _isReviewForceChecked() {
            const cb = document.getElementById('review-force-rerun');
            return cb ? cb.checked : false;
        }
        function _isStripFrontMatterChecked() {
            const cb = document.getElementById('script-strip-front-matter');
            return cb ? cb.checked : true;
        }
        function _showReviewControls(show) {
            document.getElementById('btn-pause-review').style.display = show ? 'inline-block' : 'none';
            document.getElementById('btn-cancel-review').style.display = show ? 'inline-block' : 'none';
            if (show) { _resetPauseBtn('btn-pause-review'); }
        }
        function _disableReviewButtons(disabled) {
            document.getElementById('btn-review-script').disabled = disabled;
            document.getElementById('btn-review-script-contextual').disabled = disabled;
        }
        function _onReviewDone(status) {
            _showReviewControls(false);
            _disableReviewButtons(false);
            const panel = document.getElementById('review-recovery-panel');
            const logs = status?.logs || [];
            const failed = isTaskFailed(status);
            if (panel) {
                panel.style.display = failed ? '' : 'none';
                if (failed) {
                    const last = logs.filter(Boolean).slice(-1)[0] || 'Unknown review error';
                    panel.innerHTML = `Review stopped with an error: ${escapeHtml(last)} ` +
                        `<a href="/api/logs/review?download=true" target="_blank" rel="noopener">Download full log</a>. ` +
                        'Inspect the log, correct the source or profile, and retry.';
                }
            }
        }

        document.getElementById('btn-review-script').addEventListener('click', async () => {
            if (!(await confirmIfRemote('this review', true))) { return; }
            try {
                _disableReviewButtons(true);
                _showReviewControls(true);
                await API.post('/api/review_script', { dedupe_speakers: _isReviewDedupeChecked(), force_review: _isReviewForceChecked() });
                pollScriptLogs('review', _onReviewDone);
            } catch (e) {
                _onReviewDone();
                showActionError("Failed to start review", e, "Check the current review task state and Test Connection in Setup before starting another review.");
            }
        });

        function applyReviewContextWindow(field, minimum, defaultValue) {
            const rawWindow = parseInt(field.value, 10);
            const windowSize = Number.isFinite(rawWindow) ? Math.max(minimum, Math.min(rawWindow, 12)) : defaultValue;
            const corrected = !Number.isFinite(rawWindow) || Number(field.value) !== windowSize;
            field.value = String(windowSize);
            if (corrected) { showToast(`Context Window must be ${minimum}–12; using ${windowSize}`, 'warning'); }
            return windowSize;
        }

        document.getElementById('btn-review-script-contextual').addEventListener('click', async () => {
            try {
                if (!(await confirmIfRemote('this contextual review', true))) { return; }
                const windowSize = applyReviewContextWindow(document.getElementById('review-context-window'), 1, 4);
                _disableReviewButtons(true);
                _showReviewControls(true);
                const result = await API.post('/api/review_script_contextual', { window_size: windowSize, dedupe_speakers: _isReviewDedupeChecked(), force_review: _isReviewForceChecked() });
                const estimateEl = document.getElementById('review-context-estimate');
                if (estimateEl) {
                    estimateEl.innerText = result.estimated_calls
                        ? `Estimated LLM calls: ~${result.estimated_calls} for ${result.total_entries} entries with batches of ${result.batch_size}.`
                        : 'Contextual review started.';
                }
                pollScriptLogs('review', _onReviewDone);
            } catch (e) {
                _onReviewDone();
                showActionError("Failed to start contextual review", e, "Check the review task state, context window and Test Connection in Setup before starting another review.");
            }
        });

        const _reviewPauseResume = _makePauseResumeHandler(
            '/api/review_script/pause', '/api/review_script/resume', 'btn-pause-review');
        const _batchReviewPauseResume = _makePauseResumeHandler(
            '/api/review_script/batch/pause', '/api/review_script/batch/resume', 'btn-pause-batch-review');
        window.pauseResumeReview = _reviewPauseResume;
        window.pauseResumeBatchReview = _batchReviewPauseResume;
        window.cancelReview = () => cancelTask('/api/review_script/cancel', {
            onSuccess: () => _resetPauseBtn('btn-pause-review'),
        });
        window.cancelBatchReview = () => cancelTask('/api/review_script/batch/cancel', {
            onSuccess: () => _resetPauseBtn('btn-pause-batch-review'),
        });

        // --- Batch review ---
        let reviewBatchSelected = [];
        let reviewBatchScripts = [];   // current saved-script list, kept so we can re-sort without refetching

        window.toggleReviewBatchMode = () => {
            const isBatch = document.getElementById('review-batch-mode').checked;
            document.getElementById('review-single-area').style.display = isBatch ? 'none' : 'block';
            document.getElementById('review-batch-area').style.display = isBatch ? 'block' : 'none';
            document.getElementById('btn-review-batch-start').style.display = isBatch ? 'inline-block' : 'none';
            if (isBatch) { loadReviewBatchScripts(); }
        };

        // --- Shared helpers for saved-script checkbox pickers (Batch Review,
        // Cast bulk-apply) so the fetch/render/sort logic isn't duplicated. ---

        // Fetch /api/scripts into `containerId`'s picker, handing the result to
        // `onLoaded` (which should store it and call the matching render function).
        async function _loadScriptList(containerId, onLoaded) {
            const container = document.getElementById(containerId);
            const request = {};
            container._scriptListRequest = request;
            const isCurrent = () => container._scriptListRequest === request
                && container === document.getElementById(containerId);
            try {
                const scripts = await API.get('/api/scripts');
                if (!isCurrent()) { return; }
                onLoaded(scripts);
            } catch (e) {
                if (!isCurrent()) { return; }
                container.innerHTML = `<span class="text-danger small">${escapeHtml(getActionErrorMessage('Could not load saved scripts', e, 'Check that Alexandria is running, then refresh the saved-script list before choosing a book.'))}</span>`;
            }
        }

        // Render `scripts` into a saved-script checkbox picker, preserving any
        // checkboxes the user already ticked (re-rendering rebuilds the inputs,
        // so capture selection first). `extra(i)` optionally renders trailing
        // per-row markup (e.g. a status indicator).
        function _renderScriptCheckboxList(items, { containerId, checkClass, idPrefix, extra,
                                                     keyField = 'name', renderLabel, onchange,
                                                     emptyMessage }) {
            const container = document.getElementById(containerId);
            if (!items.length) {
                container.innerHTML = `<span class="text-muted small">${emptyMessage ||
                    'No saved scripts found. Generate or save scripts first.'}</span>`;
                return;
            }
            const checked = new Set(
                Array.from(document.querySelectorAll(`.${checkClass}:checked`)).map(cb => cb.dataset.name)
            );
            container.innerHTML = items.map((item, i) => {
                const key = item[keyField];
                const label = renderLabel ? renderLabel(item)
                    : `${escapeHtml(item.name)} ${item.has_voice_config ? '<span class="badge bg-info ms-1">voices</span>' : ''}`;
                return `
                <div class="form-check${extra ? ' d-flex align-items-center justify-content-between' : ''}">
                    <div>
                        <input class="form-check-input ${checkClass}" type="checkbox" id="${idPrefix}${i}" data-name="${escapeHtml(key)}"${onchange ? ` onchange="${onchange}"` : ''} ${checked.has(key) ? 'checked' : ''}>
                        <label class="form-check-label small" for="${idPrefix}${i}">${label}</label>
                    </div>
                    ${extra ? extra(i) : ''}
                </div>`;
            }).join('');
        }

        // Shared by every checkbox-list picker's "Select all"/"Clear" pair
        // (script-batch-uploads, review-batch, cast-bulk). `onchange` is
        // optional - programmatically setting `.checked` doesn't fire a
        // checkbox's own inline onchange handler, so callers that need a
        // refresh after a bulk toggle (script-batch-uploads) pass one; the
        // others don't need it, same as before this helper existed.
        function _selectAllCheckboxes(checkClass, on, onchange) {
            document.querySelectorAll(`.${checkClass}`).forEach(cb => { cb.checked = on; });
            if (onchange) { onchange(); }
        }

        // Trailing number in a script name for numeric ("1→10") sorting,
        // e.g. "arc_8_-_volume_37" → 37. Names without a number sort last.
        function _getScriptVolumeNum(name) {
            const m = String(name).match(/(\d+)(?!.*\d)/);
            return m ? parseInt(m[1], 10) : Number.POSITIVE_INFINITY;
        }

        // Sort `list` (array of {name, ...}) in place per the A→Z / Z→A / 1→10 /
        // 10→1 / Reverse sort buttons shared by the saved-script pickers.
        function _sortScriptList(list, mode) {
            if (!list.length) { return; }
            const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
            const byNum  = (a, b) => _getScriptVolumeNum(a.name) - _getScriptVolumeNum(b.name) || byName(a, b);
            switch (mode) {
                case 'az':       list.sort(byName); break;
                case 'za':       list.sort(byName).reverse(); break;
                case 'num-asc':  list.sort(byNum); break;
                case 'num-desc': list.sort(byNum).reverse(); break;
                case 'reverse':  list.reverse(); break;
            }
        }

        async function loadReviewBatchScripts() {
            await _loadScriptList('review-batch-list', (scripts) => {
                reviewBatchScripts = scripts;
                renderReviewBatchList();
            });
        }

        function renderReviewBatchList() {
            _renderScriptCheckboxList(reviewBatchScripts, {
                containerId: 'review-batch-list',
                checkClass: 'review-batch-check',
                idPrefix: 'rb-check-',
                extra: (i) => `<span class="small" id="rb-status-${i}"></span>`,
            });
        }

        window.reviewBatchSort = (mode) => {
            _sortScriptList(reviewBatchScripts, mode);
            renderReviewBatchList();
        };

        window.reviewBatchSelectAll = (on) => _selectAllCheckboxes('review-batch-check', on);

        async function startBatchReview() {
            const names = Array.from(document.querySelectorAll('.review-batch-check:checked')).map(cb => cb.dataset.name);
            if (!names.length) { showToast('Select at least one script to review.', 'warning'); return; }
            if (!(await confirmIfRemote('this batch review'))) { return; }
            reviewBatchSelected = names;
            const contextWindow = applyReviewContextWindow(document.getElementById('review-batch-context-window'), 0, 0);

            const startBtn = document.getElementById('btn-review-batch-start');
            const pauseBtn = document.getElementById('btn-pause-batch-review');
            const cancelBtn = document.getElementById('btn-cancel-batch-review');
            const statusMsg = document.getElementById('review-batch-status-msg');
            startBtn.disabled = true;
            pauseBtn.style.display = 'inline-block';
            cancelBtn.style.display = 'inline-block';
            _resetPauseBtn('btn-pause-batch-review');
            statusMsg.style.display = '';
            statusMsg.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Starting batch review…</span>';

            try {
                await API.post('/api/review_script/batch/start', {
                    script_names: names,
                    context_window: contextWindow,
                    dedupe_speakers: _isReviewDedupeChecked(),
                    force_review: _isReviewForceChecked(),
                    find_nicknames: document.getElementById('review-batch-find-nicknames').checked,
                    bidirectional: document.getElementById('review-batch-bidirectional').checked,
                });
                pollReviewBatch();
            } catch (e) {
                startBtn.disabled = false;
                pauseBtn.style.display = 'none';
                cancelBtn.style.display = 'none';
                statusMsg.innerHTML = `<span class="text-danger">${escapeHtml(getActionErrorMessage('Batch review start was not confirmed', e, 'Check the batch review task state, selected books and Test Connection in Setup before starting again.'))}</span>`;
            }
        }

        // --- Nickname discovery + alias editor ---
        const _nickPauseResume = _makePauseResumeHandler(
            '/api/find_nicknames/pause', '/api/find_nicknames/resume', 'btn-pause-nick');
        window.pauseResumeNicknames = _nickPauseResume;
        window.cancelNicknames = () => cancelTask('/api/find_nicknames/cancel', {
            onSuccess: () => _resetPauseBtn('btn-pause-nick'),
        });

        async function findNicknames() {
            if (!(await confirmIfRemote('this nickname discovery', true))) { return; }
            const btn = document.getElementById('btn-find-nicknames');
            btn.disabled = true;
            document.getElementById('btn-pause-nick').style.display = 'inline-block';
            document.getElementById('btn-cancel-nick').style.display = 'inline-block';
            _resetPauseBtn('btn-pause-nick');
            try {
                await API.post('/api/find_nicknames', {});
                pollScriptLogs('nicknames', async (status) => {
                    _showTaskRecoveryPanel('nickname-recovery-panel', 'nicknames', status,
                        'Inspect the log, correct aliases if needed, and retry.');
                    btn.disabled = false;
                    document.getElementById('btn-pause-nick').style.display = 'none';
                    document.getElementById('btn-cancel-nick').style.display = 'none';
                    await loadCharacterAliases(true);  // show what was found for review/edit
                });
            } catch (e) {
                btn.disabled = false;
                document.getElementById('btn-pause-nick').style.display = 'none';
                document.getElementById('btn-cancel-nick').style.display = 'none';
                showActionError("Failed to start nickname discovery", e, "Check the nickname task state and Test Connection in Setup before starting nickname discovery again.");
            }
        }

        // Cast list (#653): one request over the whole book lists everyone who
        // speaks, people the book never names included. Generation uses it when
        // the selected book has one; the table below edits it.
        let castListLoaded = false;
        let castListRequest = 0;
        let castListEditorSnapshot = null;
        let castListMutationPending = false;

        function applyCastListMutationState(pending, message = '') {
            castListMutationPending = pending;
            const fields = document.getElementById('cast-list-fields');
            if (fields) { fields.disabled = pending; fields.ariaBusy = String(pending); }
            const status = document.getElementById('cast-list-editor-status');
            if (status) { status.textContent = message; }
        }

        function getCastListEditorSnapshot() {
            return JSON.stringify(Array.from(document.querySelectorAll('#cast-list-rows .cast-list-row'), row => [
                row.querySelector('.cast-list-name').value,
                row.querySelector('.cast-list-aliases').value,
            ]));
        }

        function clearCastListEditor() {
            castListRequest++;
            castListLoaded = false;
            castListEditorSnapshot = null;
            document.getElementById('cast-list-panel').innerHTML = '';
        }

        async function ensureCastListEditsDiscardable() {
            if (castListMutationPending) {
                showToast('Wait for the cast-list request to finish before switching books.', 'warning');
                return false;
            }
            if (castListEditorSnapshot === null || document.getElementById('cast-list-panel').style.display === 'none') { return true; }
            const snapshot = getCastListEditorSnapshot();
            if (snapshot === castListEditorSnapshot) { return true; }
            const book = currentBookFilename;
            if (!await showConfirm('Discard unsaved cast-list changes?', {title: 'Discard cast edits?', actionLabel: 'Discard edits', danger: true})) { return false; }
            if (book !== currentBookFilename || snapshot !== getCastListEditorSnapshot()) {
                showToast('The book or cast edits changed. Review them before switching books.', 'warning');
                return false;
            }
            return true;
        }

        async function buildCastList() {
            const source = getLoadedScriptSourceFilename();
            if (!source) { showToast('Select or upload a book before building its cast list.', 'warning'); return; }
            if (!(await confirmIfRemote('this cast list (the whole book in one request)'))) { return; }
            if (source !== getLoadedScriptSourceFilename()) {
                showToast('The loaded book changed. Review it before building its cast list.', 'warning'); return;
            }
            const btn = document.getElementById('btn-build-cast-list');
            const cancelBtn = document.getElementById('btn-cancel-cast-list');
            btn.disabled = true;
            cancelBtn.style.display = 'inline-block';
            try {
                await API.post('/api/cast_list/build', { strip_front_matter: _isStripFrontMatterChecked() });
                pollScriptLogs('cast_list', async () => {
                    btn.disabled = false;
                    cancelBtn.style.display = 'none';
                    await loadCastList(true);
                });
            } catch (e) {
                btn.disabled = false;
                cancelBtn.style.display = 'none';
                showActionError("Failed to start the cast list", e, "Check the cast-list task state, selected book and Test Connection in Setup before starting again.");
            }
        }

        window.cancelCastList = () => cancelTask('/api/cast_list/cancel', {
            onSuccess: () => {
                document.getElementById('btn-build-cast-list').disabled = false;
                document.getElementById('btn-cancel-cast-list').style.display = 'none';
            },
        });

        function renderCastListStatus(result) {
            const status = document.getElementById('cast-list-status');
            if (result && result.cast) {
                const edited = result.edited ? ', edited' : '';
                status.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>Generate will use this book's cast list (${result.count} people${edited}).</span>`;
            } else {
                status.textContent = 'No cast list for this book. Generation finds speakers on its own; people the book never names come out as UNKNOWN.';
            }
        }

        function getCastListRowHtml(entry) {
            const aliases = (entry.aliases || []).join(', ');
            return `
                <div class="input-group input-group-sm mb-1 cast-list-row">
                    <input type="text" class="form-control cast-list-name" aria-label="Cast name" placeholder="NAME or THE DESCRIPTION" value="${escapeHtml(entry.name || '')}">
                    <input type="text" class="form-control cast-list-aliases" aria-label="${escapeHtml('Aliases for ' + (entry.name || 'new cast member'))}" placeholder="other names, comma-separated" value="${escapeHtml(aliases)}">
                    <button class="btn btn-outline-danger" type="button" aria-label="${escapeHtml('Remove ' + (entry.name || 'new member') + ' from cast')}" onclick="this.closest('.cast-list-row').remove()"><i class="fas fa-times"></i></button>
                </div>`;
        }

        async function loadCastList(show) {
            const panel = document.getElementById('cast-list-panel');
            const editorSnapshot = getCastListEditorSnapshot();
            if (show && (castListMutationPending || (castListEditorSnapshot !== null && panel.style.display !== 'none' && editorSnapshot !== castListEditorSnapshot))) {
                showToast('Your cast-list edits are still here. Save them before refreshing the editor.', 'warning');
                return;
            }
            const book = currentBookFilename;
            const request = ++castListRequest;
            castListLoaded = false;
            let result;
            try {
                result = await API.get('/api/cast_list');
            } catch (e) {
                if (request !== castListRequest || book !== currentBookFilename) { return; }
                document.getElementById('cast-list-status').textContent = getActionErrorMessage(
                    'Cast-list refresh was not confirmed', e,
                    'Existing editor rows were kept. Retry loading this book’s cast list before relying on it for generation.');
                if (show) { showActionError('Cast-list refresh was not confirmed', e, 'Existing editor rows were kept. Retry loading the current book’s cast list.'); }
                return;
            }
            if (request !== castListRequest || book !== currentBookFilename) { return; }
            castListLoaded = true;
            renderCastListStatus(result);
            if (!show) { return; }
            if (castListMutationPending || editorSnapshot !== getCastListEditorSnapshot()) {
                showToast('Your cast-list edits changed while loading. They have been kept.', 'warning');
                return;
            }
            const cast = (result && result.cast) || [];
            panel.style.display = 'block';
            panel.innerHTML = `
                <fieldset id="cast-list-fields" class="border rounded p-2">
                    <div class="small fw-bold mb-1">Cast list <span class="text-muted">(each person gets their own voice; aliases are the same person)</span></div>
                    <div id="cast-list-rows">
                        ${cast.length ? cast.map(getCastListRowHtml).join('') : '<div class="text-muted small mb-1">No cast list yet. Build one, or add people by hand.</div>'}
                    </div>
                    <div class="d-flex gap-2 mt-1">
                        <button class="btn btn-sm btn-outline-secondary" type="button" onclick="addCastListRow()"><i class="fas fa-plus me-1"></i>Add</button>
                        <button class="btn btn-sm btn-success" type="button" onclick="saveCastList()"><i class="fas fa-save me-1"></i>Save cast list</button>
                        <button class="btn btn-sm btn-outline-danger" type="button" onclick="deleteCastList()"><i class="fas fa-trash me-1"></i>Delete</button>
                    </div>
                    <div id="cast-list-editor-status" class="small mt-1" role="status"></div>
                </fieldset>`;
            castListEditorSnapshot = getCastListEditorSnapshot();
        }

        window.addCastListRow = () => {
            const rows = document.getElementById('cast-list-rows');
            const placeholder = rows.querySelector('.text-muted');
            if (placeholder) { placeholder.remove(); }
            rows.insertAdjacentHTML('beforeend', getCastListRowHtml({ name: '', aliases: [] }));
        };

        async function saveCastList() {
            if (castListMutationPending) { return; }
            if (!castListLoaded) {
                showToast('Reload the cast list before saving.', 'error');
                return;
            }
            const cast = [];
            for (const row of document.querySelectorAll('#cast-list-rows .cast-list-row')) {
                const name = row.querySelector('.cast-list-name').value.trim();
                const aliases = row.querySelector('.cast-list-aliases').value
                    .split(',').map(a => a.trim()).filter(a => a);
                if (!name && aliases.length) {
                    showToast('Every row with aliases needs a name.', 'error');
                    return;
                }
                if (name) { cast.push({ name, aliases }); }
            }
            if (!cast.length) {
                showToast('The cast list is empty; use Delete to remove it.', 'error');
                return;
            }
            const book = currentBookFilename;
            const savedSnapshot = getCastListEditorSnapshot();
            try {
                applyCastListMutationState(true, 'Saving cast list…');
                const res = await API.post('/api/cast_list', { cast });
                if (book !== currentBookFilename) { return; }
                castListEditorSnapshot = savedSnapshot;
                renderCastListStatus({ cast, count: res.count, edited: true });
                showToast(`Saved ${res.count} people. Generate will use them.`, 'success');
            } catch (e) {
                showActionError('Cast-list save was not confirmed', e, 'Keep your edited rows and check the saved cast list before saving again.');
            } finally {
                applyCastListMutationState(false);
            }
        }

        async function deleteCastList() {
            if (castListMutationPending) { return; }
            const book = currentBookFilename;
            try {
                applyCastListMutationState(true, 'Confirming cast deletion…');
                if (!await showConfirm('Delete this book’s cast list? Generation will find speakers on its own, and unnamed speakers may be labelled UNKNOWN.', {title: 'Delete book cast list?', actionLabel: 'Delete cast list', danger: true})) { return; }
                if (book !== currentBookFilename) {
                    showToast('The current book changed. Review its cast list before deleting.', 'warning');
                    return;
                }
                applyCastListMutationState(true, 'Deleting cast list…');
                await API.del('/api/cast_list');
                document.getElementById('cast-list-panel').style.display = 'none';
                await loadCastList(false);
                showToast('Cast list deleted.', 'success');
            } catch (e) {
                showActionError('Cast-list deletion was not confirmed', e, 'Reload the cast list to check whether it was deleted before trying Delete again.');
            } finally {
                applyCastListMutationState(false);
            }
        }

        let characterAliasesLoaded = false;
        let characterAliasesRequest = 0;

        function clearCharacterAliases() {
            characterAliasesRequest++;
            characterAliasesLoaded = false;
            document.getElementById('nickname-aliases-panel').innerHTML = '';
        }

        async function loadCharacterAliases(show) {
            const panel = document.getElementById('nickname-aliases-panel');
            const request = ++characterAliasesRequest;
            characterAliasesLoaded = false;
            let aliases = {};
            try {
                aliases = await API.get('/api/character_aliases');
            } catch (e) {
                if (request !== characterAliasesRequest) { return; }
                console.error('Failed to load character aliases:', e);
                showActionError('Could not load character aliases', e, 'Retry loading aliases before editing or saving them. The displayed list may be out of date.');
                return;
            }
            if (request !== characterAliasesRequest) { return; }
            characterAliasesLoaded = true;
            const entries = Object.entries(aliases || {});
            if (show) { panel.style.display = 'block'; }
            const rowHtml = (a, c) => `
                <div class="input-group input-group-sm mb-1 nick-alias-row">
                    <input type="text" class="form-control nick-alias" aria-label="Alias or nickname" placeholder="alias / nickname" value="${escapeHtml(a)}">
                    <span class="input-group-text">&rarr;</span>
                    <input type="text" class="form-control nick-canonical" aria-label="Canonical character name" placeholder="canonical name" value="${escapeHtml(c)}">
                    <button class="btn btn-outline-danger" type="button" aria-label="${escapeHtml('Remove alias ' + a)}" onclick="this.closest('.nick-alias-row').remove()"><i class="fas fa-times"></i></button>
                </div>`;
            panel.innerHTML = `
                <div class="border rounded p-2">
                    <div class="small fw-bold mb-1">Character aliases <span class="text-muted">(applied automatically when you run Review)</span></div>
                    <div id="nick-alias-rows">
                        ${entries.length ? entries.map(([a, c]) => rowHtml(a, c)).join('') : '<div class="text-muted small mb-1">No aliases yet. Run "Find Nicknames" or add rows manually.</div>'}
                    </div>
                    <div class="d-flex gap-2 mt-1">
                        <button class="btn btn-sm btn-outline-secondary" type="button" onclick="addAliasRow()"><i class="fas fa-plus me-1"></i>Add</button>
                        <button class="btn btn-sm btn-success" type="button" onclick="saveCharacterAliases()"><i class="fas fa-save me-1"></i>Save aliases</button>
                    </div>
                </div>`;
        }

        window.addAliasRow = () => {
            const rows = document.getElementById('nick-alias-rows');
            const placeholder = rows.querySelector('.text-muted');
            if (placeholder) { placeholder.remove(); }
            const div = document.createElement('div');
            div.className = 'input-group input-group-sm mb-1 nick-alias-row';
            div.innerHTML = `
                <input type="text" class="form-control nick-alias" aria-label="Alias or nickname" placeholder="alias / nickname">
                <span class="input-group-text">&rarr;</span>
                <input type="text" class="form-control nick-canonical" aria-label="Canonical character name" placeholder="canonical name">
                <button class="btn btn-outline-danger" type="button" aria-label="Remove new alias" onclick="this.closest('.nick-alias-row').remove()"><i class="fas fa-times"></i></button>`;
            rows.appendChild(div);
        };

        async function saveCharacterAliases() {
            if (!characterAliasesLoaded) {
                showToast('Reload character aliases before saving.', 'error');
                return;
            }
            const map = Object.create(null);
            for (const row of document.querySelectorAll('#nick-alias-rows .nick-alias-row')) {
                const a = row.querySelector('.nick-alias').value.trim();
                const c = row.querySelector('.nick-canonical').value.trim();
                if ((a && !c) || (!a && c)) {
                    showToast('Fill in both alias and canonical name before saving.', 'error');
                    return;
                }
                if (a && c) { map[a] = c; }
            }
            try {
                const res = await API.post('/api/character_aliases', map);
                showToast(`Saved ${res.count} alias${res.count !== 1 ? 'es' : ''}. Run Review to apply.`, 'success');
            } catch (e) {
                showActionError('Alias save was not confirmed', e, 'Keep your edited rows and check the saved aliases before saving again.');
            }
        }

        // One-line "N changes: X text, Y speaker, ..." breakdown for a finished book's badge tooltip.
        function _formatBookStats(s) {
            let txt = s.partial ? '(partial — not every pass completed) ' : '';
            txt += `${s.total_changes} changes: ${s.text_changed} text, ${s.speaker_changed} speaker, ` +
                      `${s.instruct_changed} instruct, +${s.entries_added}/-${s.entries_removed} entries`;
            if (s.narrators_merged) { txt += `, ${s.narrators_merged} narrators merged`; }
            if (s.speakers_merged) { txt += `, ${s.speakers_merged} speakers merged`; }
            if (s.batches_failed) { txt += `, ${s.batches_failed} batch(es) failed`; }
            if (s.batches_skipped_vram) { txt += `, ${s.batches_skipped_vram} batch(es) skipped (VRAM)`; }
            return txt;
        }

        function _formatTotalsLine(label, t) {
            if (!t || !t.books_done) { return `${label}: no books finished yet`; }
            let txt = `${label}: ${t.books_done} book(s), ${t.total_changes} total change(s) ` +
                      `(${t.text_changed} text, ${t.speaker_changed} speaker, ${t.instruct_changed} instruct, ` +
                      `+${t.entries_added}/-${t.entries_removed} entries)`;
            if (t.batches_failed) { txt += ` — ${t.batches_failed} batch(es) failed`; }
            return txt;
        }

        function _updateReviewBatchTotals(state) {
            const el = document.getElementById('review-batch-totals');
            if (!el) { return; }
            const fwd = state.totals_fwd, bwd = state.totals_bwd;
            const aliasesBwd = state.aliases_bwd || [];
            if (!(fwd && fwd.books_done) && !(bwd && bwd.books_done) && !aliasesBwd.length) {
                el.style.display = 'none';
                return;
            }
            let html = '';
            if (state.bidirectional) {
                html += `<div>${escapeHtml(_formatTotalsLine('Forward pass', fwd))}</div>`;
                html += `<div>${escapeHtml(_formatTotalsLine('Backward pass (hindsight)', bwd))}</div>`;
            } else {
                html += `<div>${escapeHtml(_formatTotalsLine('Totals', fwd))}</div>`;
            }
            if (aliasesBwd.length) {
                html += `<div class="mt-1"><strong>New characters found on the backward pass (${aliasesBwd.length}):</strong></div>`;
                html += '<ul class="mb-0 ps-3">' + aliasesBwd.map(a =>
                    `<li>'${escapeHtml(a.variant)}' &rarr; '${escapeHtml(a.canonical)}' <span class="text-muted">(${escapeHtml(a.book)})</span></li>`
                ).join('') + '</ul>';
            }
            el.innerHTML = html;
            el.style.display = '';
        }

        function _showTaskRecoveryPanel(panelId, taskName, status, action) {
            const panel = document.getElementById(panelId);
            const logs = status?.logs || [];
            const failed = isTaskFailed(status);
            if (!panel) { return; }
            panel.style.display = failed ? '' : 'none';
            if (failed) {
                const last = logs.filter(Boolean).slice(-1)[0] || `Unknown ${taskName} error`;
                panel.innerHTML = `${escapeHtml(taskName)} stopped with an error: ${escapeHtml(last)} ` +
                    `<a href="/api/logs/${encodeURIComponent(taskName)}?download=true" target="_blank" rel="noopener">Download full log</a>. ${escapeHtml(action)}`;
            }
        }

        function getBatchOutcome(items, expectedCount = items.length) {
            const completed = items.filter(item => item.status === 'done').length;
            const failed = items.filter(item => ['failed', 'error'].includes(item.status)).length;
            const cancelled = items.filter(item => item.status === 'cancelled').length;
            const unfinished = Math.max(0, expectedCount - completed - failed - cancelled);
            return { completed, failed, cancelled, unfinished,
                complete: expectedCount > 0 && completed === expectedCount };
        }

        function pollReviewBatch() {
            const logEl = document.getElementById('script-logs');
            const renderLogs = createTaskLogRenderer(logEl);
            _startPolling('batch_review', () => API.get('/api/status/batch_review'), {
                doneCheck: state => !state.running,
                onTick: state => {
                    renderLogs(state);
                    syncPauseButton('batch_review', state);
                    const colours = { pending: 'secondary', running: 'primary', done: 'success', incomplete: 'warning', failed: 'danger', cancelled: 'warning' };
                    (state.tasks || []).forEach(t => {
                        // Map by name (the list shows all scripts; only selected ones are tasks)
                        const cb = document.querySelector(`.review-batch-check[data-name="${CSS.escape(t.name)}"]`);
                        if (!cb) { return; }
                        const el = document.getElementById(cb.id.replace('rb-check-', 'rb-status-'));
                        if (el) {
                            el.innerHTML = `<span class="badge bg-${colours[t.status] || 'secondary'}">${t.status}</span>`;
                            el.title = t.stats ? _formatBookStats(t.stats) : '';
                        }
                    });
                    _updateReviewBatchTotals(state);
                },
                onDone: (state) => {
                    _showTaskRecoveryPanel('review-batch-recovery-panel', 'batch_review', state,
                        'Inspect the failed books and retry the batch.');
                    notifyJobDone('batch_review', '', 'finished', state);
                    document.getElementById('btn-review-batch-start').disabled = false;
                    document.getElementById('btn-pause-batch-review').style.display = 'none';
                    document.getElementById('btn-cancel-batch-review').style.display = 'none';
                    const outcome = getBatchOutcome(state.tasks || []);
                    const label = outcome.complete ? 'complete' : state.cancel || outcome.cancelled ? 'stopped' : 'incomplete';
                    const tone = outcome.complete ? 'text-success' : 'text-warning';
                    document.getElementById('review-batch-status-msg').innerHTML =
                        `<span class="${tone}">Batch review ${label}: ${outcome.completed} completed, ${outcome.failed} failed, ${outcome.cancelled} cancelled, ${outcome.unfinished} unfinished.</span>`;
                }
            });
        }

        // --- Persona Generation ---
        function toggleAdvancedPersonaOptions() {
            const advanced = document.getElementById('advanced-persona-toggle');
            const options = document.getElementById('advanced-persona-options');
            if (advanced && options) {
                options.style.display = advanced.checked ? 'flex' : 'none';
            }
            if (window._voicesByName) { refreshVoicesScope(); }
        }

        function getPersonaContextLines() {
            const select = document.getElementById('persona-context-lines');
            const custom = document.getElementById('persona-context-custom');
            const raw = select && select.value === 'custom' ? custom?.value : select?.value;
            return Math.max(1, Math.min(parseInt(raw || '10', 10) || 10, 200));
        }

        function onPersonaContextChange() {
            const select = document.getElementById('persona-context-lines');
            const custom = document.getElementById('persona-context-custom');
            if (custom) {
                custom.style.display = select && select.value === 'custom' ? '' : 'none';
            }
        }

        // Which characters Generate Personas / Suggest LoRA Voices act on (#602).
        // "new" = the ones /api/voices reports as persona_pending (no entry in
        // voice_config.json); "all" re-rolls every persona and preview, so it
        // offers to save the current voices to the library first.
        function _voicesScopeState() {
            const rows = Object.values(window._voicesByName || {});
            const advanced = !!document.getElementById('advanced-persona-toggle')?.checked;
            const isPending = voice => voice.persona_pending || (advanced && voice.persona_states_pending);
            const pending = rows.filter(isPending).map(v => v.name);
            const have = rows.filter(v => !v.persona_pending ||
                Object.keys(v.config?.versions || {}).length > 0).map(v => v.name);
            return { pending, have, total: rows.length };
        }

        function refreshVoicesScope() {
            const select = document.getElementById('voices-scope');
            if (!select) { return; }
            const { pending, have, total } = _voicesScopeState();
            select.options[0].textContent = `Only characters without a voice yet (${pending.length})`;
            select.options[1].textContent = `All characters (regenerate ${total})`;
            if (!select.dataset.userSet) {
                select.value = (pending.length >= 1 && have.length >= 1) ? 'new' : 'all';
            }
            onVoicesScopeChange(true);
        }

        function _keepCastName() {
            if (window._selectedCast) { return window._selectedCast; }
            return getCurrentBookName('current book');
        }

        function onVoicesScopeChange(fromRefresh = false) {
            const select = document.getElementById('voices-scope');
            if (!fromRefresh) { select.dataset.userSet = '1'; }
            const { have } = _voicesScopeState();
            const wrap = document.getElementById('voices-keep-wrap');
            const show = select.value === 'all' && have.length > 0;
            wrap.style.display = show ? '' : 'none';
            if (show) { document.getElementById('voices-keep-cast').textContent = `cast: ${_keepCastName()}`; }
        }

        function voicesScopeIsNew() {
            return (document.getElementById('voices-scope')?.value || 'all') === 'new';
        }

        // Before an "all characters" run: the current voices into a cast, so a
        // regenerate never silently destroys them. -> false when the save
        // failed and the run must not start.
        async function keepCurrentVoicesIfAsked() {
            const wrap = document.getElementById('voices-keep-wrap');
            const box = document.getElementById('voices-keep-in-library');
            if (!wrap || wrap.style.display === 'none' || !box.checked) { return true; }
            const { have } = _voicesScopeState();
            if (!have.length) { return true; }
            const cast = _keepCastName();
            try {
                try { await API.post('/api/voice_library/casts', { name: cast }); } catch (e) { if (e.status !== 409) { throw e; } }
                const res = await API.post('/api/voice_library/save', { cast, characters: have, cast_specific: [] });
                showToast(`Saved ${have.length} current voices to the library as "${cast}".`, 'success');
                if (typeof loadCastLibrary === 'function') { try { await loadCastLibrary(); } catch (e) { /* display only */ } }
                return !!res;
            } catch (e) {
                showActionError('Not started: saving current voices to the library failed', e, 'Check the cast library for the saved voices before starting again. Keep the save-to-library option enabled to protect the current assignments.');
                return false;
            }
        }

        async function generatePersonas() {
            if (!claimTaskStart('persona')) { return; }
            let started = false;
            const statusSpan = document.getElementById('persona-status');
            const cancelButton = document.getElementById('btn-cancel-personas');
            const advancedToggle = document.getElementById('advanced-persona-toggle');
            const batchInput = document.getElementById('persona-batch-size');
            try {
                if (!(await confirmIfRemote('this persona generation', true))) { return; }
                const advanced = !!(advancedToggle && advancedToggle.checked);
                const batchSize = Math.max(1, Math.min(parseInt(batchInput?.value || '40', 10) || 40, 200));
                const contextLines = getPersonaContextLines();
                const newOnly = voicesScopeIsNew();
                if (!newOnly && !(await keepCurrentVoicesIfAsked())) { return; }
                statusSpan.innerHTML = `<i class="fas fa-spinner fa-spin me-1"></i>${advanced ? 'Starting advanced...' : 'Starting...'}`;
                if (cancelButton) {
                    cancelButton.style.display = '';
                }
                await API.post('/api/generate_personas', { advanced, batch_size: batchSize, context_lines: contextLines, new_only: newOnly });
                started = true;
                pollPersonaStatus();
            } catch (e) {
                showActionError("Failed to start persona generation", e, "Check the persona task status before starting again. For an LLM refusal, use Setup → Test Connection and review the selected model.");
                statusSpan.innerText = '';
                if (cancelButton) {
                    cancelButton.style.display = 'none';
                }
            } finally {
                if (!started) { releaseTaskStart('persona'); }
            }
        }

        async function cancelPersonas() {
            await cancelTask('/api/cancel_persona', {
                onSuccess: () => {
                    const statusSpan = document.getElementById('persona-status');
                    if (statusSpan) { statusSpan.innerText = 'Cancelling...'; }
                },
                errorMessage: (e) => getActionErrorMessage('Persona cancellation is unconfirmed', e, 'Check the persona task status before cancelling again; generation may still be running.'),
                toastType: 'error',
            });
        }

        function renderPersonaRecoveryTargets() {
            const field = document.getElementById('persona-recovery-state');
            if (!field) { return; }
            const speaker = document.getElementById('persona-recovery-speaker')?.value.trim();
            const states = window._voicesByName?.[speaker]?.persona_states || [];
            const token = typeof _voiceCardsBookToken === 'undefined' ? '' : _voiceCardsBookToken;
            const previous = field.dataset.bookToken === token && field.dataset.speaker === speaker ? field.value : '';
            field.innerHTML = (states.length ? '<option value="" disabled>Choose base or state persona</option>' : '')
                + '<option value="__base__">Base character persona</option>'
                + states.map(state => `<option value="${escapeHtml(state.version_id)}">${escapeHtml(`${state.gender} · ${state.age_group.replaceAll('_', ' ')} · state ${state.state_number}`)}</option>`).join('');
            field.value = states.length ? (previous === '__base__' || states.some(state => state.version_id === previous) ? previous : '') : '__base__';
            field.dataset.bookToken = token;
            field.dataset.speaker = speaker;
        }

        window.openStatePersonaRecovery = function openStatePersonaRecovery(button) {
            const card = button.closest('.voice-card');
            if (!card?.dataset.version) { return; }
            document.getElementById('persona-recovery-speaker').value = card.dataset.voice;
            renderPersonaRecoveryTargets();
            document.getElementById('persona-recovery-state').value = card.dataset.version;
            document.getElementById('persona-recovery-panel').open = true;
            document.getElementById('persona-recovery-json').focus();
        };

        window.recoverPersona = async function recoverPersona(resume = false) {
        const speaker = document.getElementById('persona-recovery-speaker')?.value.trim();
        const personaJson = document.getElementById('persona-recovery-json')?.value.trim();
        const status = document.getElementById('persona-recovery-status');
        if (!speaker || !personaJson) {
            if (status) { status.textContent = 'Speaker and persona JSON are required.'; }
            return;
        }
        try {
            const field = document.getElementById('persona-recovery-state');
            const selected = field?.value;
            const states = window._voicesByName?.[speaker]?.persona_states || [];
            if (field && states.length && !selected) { throw new Error('Choose the base persona or the exact character state to recover.'); }
            const state = selected && selected !== '__base__' ? states.find(state => state.version_id === selected) : null;
            if (selected && selected !== '__base__' && !state) { throw new Error('The selected character state changed. Reload Voices.'); }
            const payload = {speaker, persona_json: personaJson, resume};
            if (state) {
                const token = field.dataset.bookToken;
                await flushVoiceSaves();
                if (!token || token !== _voiceSaveSnapshot?.book_token || field.value !== selected
                        || document.getElementById('persona-recovery-speaker').value.trim() !== speaker) {
                    throw new Error('The book or recovery target changed. Reload Voices and select the state again.');
                }
                Object.assign(payload, {state_version: state.version_id, book_token: token});
            }
            await API.post('/api/persona/recover', payload);
            if (status) { status.textContent = resume ? 'Validated and resumed.' : 'Validated and saved.'; }
            await loadVoices();
            showToast(`Persona recovered for ${speaker}.`, 'success');
        } catch (e) {
            if (status) { status.textContent = getActionErrorMessage('Persona recovery failed', e, 'Keep the pasted JSON. Check the speaker and validation details before correcting and submitting it again.'); }
            showActionError("Persona recovery failed", e, "Keep the pasted persona JSON. Check the speaker and validation details, then correct the JSON before submitting again.");
        }
    };

    window.copyPersonaPrompt = async function copyPersonaPrompt() {
        const speaker = document.getElementById('persona-recovery-speaker')?.value.trim() || 'the character';
        const system = document.getElementById('persona-system-prompt')?.value.trim()
            || 'Return JSON only with description and ref_text.';
        const userTemplate = document.getElementById('persona-user-prompt')?.value.trim()
            || 'Create a persona for {speaker}. Return exactly {"description":"...","ref_text":"..."}.';
        const samples = document.getElementById('persona-recovery-samples')?.value.trim() || '(none provided)';
        const narration = document.getElementById('persona-recovery-narration')?.value.trim() || '(none provided)';
        const selected = document.getElementById('persona-recovery-state')?.value;
        const state = window._voicesByName?.[speaker]?.persona_states?.find(state => state.version_id === selected);
        const scope = state ? `\nUse only this state's supplied evidence: ${state.gender}, ${state.age_group}, segment ${state.state_number}.` : '';
        const prompt = `${system}\n\n${userTemplate.replaceAll('{speaker}', speaker)
            .replaceAll('{sample_lines}', samples).replaceAll('{narrator_context}', narration)}${scope}`;
        await copyToClipboard(prompt, 'Persona prompt');
    };

        let personaVoiceRefreshRequest = 0;
        let personaVoiceRefreshPending = false;
        async function refreshPersonaVoiceResources(afterTask = false) {
            if (personaVoiceRefreshPending && !afterTask) { return false; }
            const request = ++personaVoiceRefreshRequest;
            const book = currentBookFilename;
            const isCurrent = () => request === personaVoiceRefreshRequest && book === currentBookFilename;
            const status = document.getElementById('persona-refresh-status');
            const retry = document.getElementById('persona-refresh-retry');
            personaVoiceRefreshPending = true;
            retry.disabled = true;
            status.textContent = 'Refreshing Voices after the persona task…';
            let refreshedResources = [];
            const failedResources = new Set();
            try {
                try {
                    const result = await loadVoices();
                    if (!result) { throw new Error('Voice refresh did not return a result'); }
                    refreshedResources = result.refreshedResources;
                    (result.failedResources || []).forEach(path => failedResources.add(path));
                } catch (error) {
                    failedResources.add('/api/voice_config/snapshot');
                    console.debug('voices refresh failed', error);
                }
                for (const [key, path] of [['_designedVoicesCache', '/api/voice_design/list'], ['_cloneVoicesCache', '/api/clone_voices/list']]) {
                    if (!isCurrent()) { return false; }
                    if (refreshedResources.includes(path)) { continue; }
                    try {
                        const list = await API.get(path);
                        if (!isCurrent()) { return false; }
                        if (!Array.isArray(list)) { throw new Error('Voice resource list is malformed'); }
                        window[key] = list;
                        failedResources.delete(path);
                    } catch (error) {
                        failedResources.add(path);
                        console.debug('persona voice resource refresh failed', error);
                    }
                }
                if (!isCurrent()) { return false; }
                const complete = failedResources.size === 0;
                status.textContent = complete ? ''
                    : 'The persona task has ended, but Voices or its reference lists could not be fully refreshed. Previously loaded lists are kept. Check that Alexandria is running, then retry the Voices refresh; this does not regenerate personas.';
                retry.hidden = complete;
                return complete;
            } finally {
                if (request === personaVoiceRefreshRequest) {
                    personaVoiceRefreshPending = false;
                    retry.disabled = false;
                    if (book !== currentBookFilename) {
                        status.textContent = 'The book changed during the Voices refresh. Review the current book, then retry the Voices refresh.';
                        retry.hidden = false;
                    }
                }
            }
        }

        async function pollPersonaStatus() {
            claimTaskStart('persona');
            const logEl = document.getElementById('voices-logs');
            const renderLogs = createTaskLogRenderer(logEl);
            const statusSpan = document.getElementById('persona-status');
            const cancelButton = document.getElementById('btn-cancel-personas');
            _startPolling('persona', () => API.get('/api/status/persona'), {
                intervalMs: 1500,
                doneCheck: status => !status.running,
                onTick: status => {
                    const advanced = !!(document.getElementById('advanced-persona-toggle')?.checked);
                    renderManualRequest(status, 'persona');
                    statusSpan.innerText = status.running ? (advanced ? 'Advanced running...' : 'Running...') : 'Finished';
                    if (cancelButton) {
                        cancelButton.style.display = status.running ? '' : 'none';
                    }
                    renderLogs(status);
                },
                onDone: async (status) => {
                    releaseTaskStart('persona');
                    notifyJobDone('persona', '', 'finished', status);
                    renderManualRequest({ running: false }, 'persona');
                    const failed = isTaskFailed(status);
                    const recoveryPanel = document.getElementById('persona-recovery-panel');
                    const recoveryStatus = document.getElementById('persona-recovery-status');
                    if (failed) {
                        if (recoveryPanel) { recoveryPanel.open = true; }
                        const recoveryContext = document.getElementById('persona-recovery-context');
                        const lastLog = (status.logs || []).filter(Boolean).slice(-1)[0] || 'Unknown persona-generation error';
                        if (recoveryContext) {
                            recoveryContext.textContent = `Stage: persona generation · Error: ${lastLog} · Next action: paste validated persona JSON below, then resume.`;
                            recoveryContext.style.display = '';
                        }
                        if (recoveryStatus) {
                            recoveryStatus.textContent = 'Persona generation stopped with an error. Copy the prompt, paste validated JSON, and resume manually.';
                        }
                    }
                    const refreshed = await refreshPersonaVoiceResources(true);
                    showToast(failed ? 'Persona generation stopped; manual recovery is available.'
                        : refreshed ? 'Persona task ended. Voices refreshed.' : 'Persona task ended; the Voices refresh needs attention.', failed || !refreshed ? 'warning' : 'success');
                    statusSpan.innerText = '';
                    if (cancelButton) {
                        cancelButton.style.display = 'none';
                    }
                }
            });
        }

        // --- Voices Tab ---
        const AVAILABLE_VOICES = ["Aiden", "Dylan", "Eric", "Ono_anna", "Ryan", "Serena", "Sohee", "Uncle_fu", "Vivian"];

        function getVoiceCandidateMarkup(candidates) {
            return Array.isArray(candidates) && candidates.length ? `<div class="small mt-2"><strong>Saved candidates</strong>${candidates.map(candidate => `<div class="d-flex align-items-center gap-1 mt-1"><span class="text-truncate" title="${escapeHtml(candidate.candidate_id || '')}">${escapeHtml(candidate.candidate_id || '')}${candidate.rank ? ` · #${candidate.rank}` : ''}</span><button class="btn btn-sm ${candidate.favorite ? 'btn-warning' : 'btn-outline-warning'} py-0" type="button" data-voice-focus-key="${escapeHtml('candidate-favorite:' + (candidate.candidate_id || ''))}" aria-label="${candidate.favorite ? 'Unfavourite' : 'Favourite'} candidate ${escapeHtml(candidate.candidate_id || '')}" onclick="favoriteVoiceCandidate(this, ${getInlineStringArgument(candidate.candidate_id || '')}, ${candidate.favorite ? 'false' : 'true'})">★</button><button class="btn btn-sm btn-outline-success py-0" type="button" aria-label="Use candidate ${escapeHtml(candidate.candidate_id || '')}" onclick="selectVoiceCandidate(this, ${getInlineStringArgument(candidate.candidate_id || '')})">Use</button><button class="btn btn-sm btn-outline-danger py-0" type="button" aria-label="Delete candidate ${escapeHtml(candidate.candidate_id || '')}" onclick="deleteVoiceCandidate(this, ${getInlineStringArgument(candidate.candidate_id || '')})">×</button></div>`).join('')}</div>` : '';
        }

        function getLibraryVoiceReference(refAudio) {
            const path = String(refAudio || '').replace(/\\/g, '/').replace(/^(?:\.\/)+/, '');
            for (const [type, directory, voices] of [
                ['clone', 'clone_voices', window._cloneVoicesCache || []],
                ['design', 'designed_voices', window._designedVoicesCache || []],
            ]) {
                const voice = voices.find(v => path === `${directory}/${v.filename}`);
                if (voice) { return {type, id: voice.id}; }
            }
            return null;
        }

        // Gender/age per line (#653): one tag per character - the most common
        // values, "ageless" when marked, and any big change in order (a time
        // skip, a gender change). Model text, so every value is escaped.
        function getTraitBadgeHtml(traits) {
            if (!traits) { return ''; }
            const label = (state) => [state.gender, state.age_group]
                .filter(value => value && value !== 'unknown').map(value => value.replace('_', ' ')).join(' · ');
            const states = (traits.states || []).map(label).filter(text => text);
            const text = states.length ? states.join(' → ') : label(traits);
            if (!text && !traits.ageless) { return ''; }
            const shown = [text, traits.ageless ? 'ageless' : ''].filter(part => part).join(' · ');
            return `<span class="badge bg-light text-dark border ms-2" title="Model-inferred from ${Number(traits.lines) || 0} script lines; review against the source">Script estimate: ${escapeHtml(shown)}</span>`;
        }

        function getVoiceCardMetadata(card) {
            const base = window._voicesByName?.[card.dataset.voice]?.config || {};
            return card.dataset.version ? (base.versions?.[card.dataset.version] || {}) : base;
        }

        function getVoiceCardKey(card) {
            return card.dataset.voice + (card.dataset.version ? '::' + card.dataset.version : '');
        }

        function getVoiceCardDescription(card) {
            const type = card.querySelector('.voice-type:checked')?.value || 'design';
            const field = type === 'design' ? '.design-description' : type === 'clone' ? '.persona-description' : null;
            return (field ? card.querySelector(field)?.value : null) ?? getVoiceCardMetadata(card).description ?? '';
        }

        async function postVoiceTarget(control, path, payload = {}) {
            const card = control.closest('.voice-card');
            if (!card?.dataset.version) { return API.post(path, payload); }
            const token = _voiceCardsBookToken;
            await flushVoiceSaves();
            if (!token || token !== _voiceSaveSnapshot?.book_token || card.isConnected === false) {
                throw new Error('The book or state card changed. Reload Voices before trying again.');
            }
            return API.post(path, {...payload, version_id: card.dataset.version, book_token: token});
        }

        async function deleteVoiceTarget(control, path) {
            const card = control.closest('.voice-card');
            if (!card?.dataset.version) { return API.del(path); }
            const token = _voiceCardsBookToken;
            await flushVoiceSaves();
            if (!token || token !== _voiceSaveSnapshot?.book_token || card.isConnected === false) {
                throw new Error('The book or state card changed. Reload Voices before trying again.');
            }
            return API.del(`${path}?version_id=${encodeURIComponent(card.dataset.version)}&book_token=${encodeURIComponent(token)}`);
        }

        function isPersonaStateCurrent(saved, target) {
            return !!saved && ['version_id', 'speaker', 'from_entry', 'segment_start', 'segment_end',
                'gender', 'age_group', 'state_number', 'source_sha256'].every(key => saved[key] === target[key]);
        }

        function getStateVoiceCardsMarkup(voices) {
            let index = 0;
            return voices.map(voice => {
                const base = createVoiceCard(voice, index++);
                if (!voice.persona_states?.length) { return base; }
                const states = voice.persona_states.map(state => {
                    const config = voice.config?.versions?.[state.version_id] || {};
                    const label = `${voice.name} (${state.gender} · ${state.age_group.replaceAll('_', ' ')}) — state ${state.state_number}`;
                    const stale = !isPersonaStateCurrent(config.persona_state, state);
                    const markup = createVoiceCard({...voice, version_id: state.version_id, display_name: label,
                        config, state_stale: stale, traits: null, persona_states: voice.persona_states}, index++);
                    if (!stale) { return markup; }
                    // Generate the persona first; pending/stale forms cannot publish invented versions.
                    return markup.replace(/<(input|select|textarea|button)\b([^>]*)>/g, (tag, kind, attributes) => {
                        if (kind === 'button' && (attributes.includes('regeneratePersona(this)') || attributes.includes('openStatePersonaRecovery(this)'))) { return tag; }
                        return `<${kind}${attributes} disabled>`;
                    });
                }).join('');
                return `<div class="voice-character-group" data-voice="${escapeHtml(voice.name)}"><details class="mb-3 voice-base-details"><summary>${escapeHtml(voice.name)}: base voice and manual versions</summary>${base}</details>${states}</div>`;
            }).join('');
        }

        function createVoiceCard(voice, index) {
            const config = voice.config || {};
            const label = voice.display_name || voice.name;
            const voiceType = config.type || 'custom';
            const reference = getLibraryVoiceReference(config.ref_audio);
            const customVoices = config.voice && !AVAILABLE_VOICES.includes(config.voice)
                ? [config.voice, ...AVAILABLE_VOICES] : AVAILABLE_VOICES;

            const ready = !!config.ready;
            return `
                <div class="card voice-card mb-3${ready ? ' border-success' : ''}" data-voice="${escapeHtml(voice.name)}" data-version="${escapeHtml(voice.version_id || '')}" data-has-states="${voice.persona_states?.length ? '1' : '0'}" data-ready="${ready ? '1' : '0'}">
                    <div class="card-body">
                        <div class="row">
                            <div class="col-md-3">
                                <h5 class="card-title">${escapeHtml(label)} ${config.alias_of ? `<span class="badge bg-info ms-2" title="Alias of ${escapeHtml(config.alias_of)}">${escapeHtml(config.alias_of)}</span>` : ''}${(!voice.version_id && window._lineCounts && window._lineCounts[voice.name] != null) ? `<span class="badge bg-secondary ms-2" title="${window._lineCounts[voice.name]} lines in this book">${window._lineCounts[voice.name]} lines</span>` : ''}${getTraitBadgeHtml(voice.traits)}</h5>
                                <button class="btn btn-sm btn-outline-primary mt-1" type="button" aria-label="${escapeHtml('Regenerate persona for ' + label)}" onclick="regeneratePersona(this)"><i class="fas fa-rotate me-1"></i>Regenerate persona</button>
                                <button class="btn btn-sm btn-outline-primary mt-1" type="button" aria-label="${escapeHtml('Generate age version for ' + label)}" onclick="generateAgeVersion(this)" ${voice.version_id ? 'hidden' : ''}><i class="fas fa-person-circle-plus me-1"></i>Generate age version</button>
                                ${voice.version_id ? `<button class="btn btn-sm btn-outline-secondary mt-1" type="button" aria-label="${escapeHtml('Recover persona for ' + label)}" onclick="openStatePersonaRecovery(this)">Recover persona</button>` : ''}
                                ${voice.version_id ? `<button class="btn btn-sm btn-outline-danger mt-1" type="button" aria-label="${escapeHtml('Remove saved state persona for ' + label)}" onclick="removeStatePersona(this)">Remove saved state persona</button>` : ''}
                                <div class="small text-muted">${voice.version_id && voice.state_stale ? 'State persona needs generation for the current script. ' : ''}Persona review: ${escapeHtml(config.persona_status || 'unreviewed')} · Voice review: ${escapeHtml(config.voice_status || 'unassigned')}</div>
                                ${config.persona_voice_audit ? `<div class="small text-muted" title="${escapeHtml(config.persona_voice_audit.suggestion_reason || '')}">Persona-to-voice audit: ${escapeHtml(config.persona_voice_audit.voice_adapter_id || 'manual')} · ${escapeHtml(config.persona_voice_audit.persona_ref || 'inline persona')} <button class="btn btn-sm btn-link p-0" type="button" aria-label="${escapeHtml('Edit persona-to-voice audit for ' + label)}" onclick="editPersonaVoiceAudit(this)">Edit</button></div>` : ''}
                                <div class="d-flex flex-wrap gap-1 mt-1" role="group" aria-label="Approval status" aria-describedby="voice-approval-help">
                                    <button style="flex:1 1 45%;min-width:0;" class="btn btn-sm btn-outline-success" type="button" onclick="setVoiceApproval(this, 'persona_status', 'approved')">Approve persona</button>
                                    <button style="flex:1 1 45%;min-width:0;" class="btn btn-sm btn-outline-secondary" type="button" onclick="setVoiceApproval(this, 'persona_status', 'reviewed')">Mark persona reviewed</button>
                                    <button style="flex:1 1 45%;min-width:0;" class="btn btn-sm btn-outline-danger" type="button" onclick="setVoiceApproval(this, 'persona_status', 'rejected')">Reject persona</button>
                                    <button style="flex:1 1 45%;min-width:0;" class="btn btn-sm btn-outline-success" type="button" onclick="setVoiceApproval(this, 'voice_status', 'approved')">Approve voice</button>
                                    <button style="flex:1 1 45%;min-width:0;" class="btn btn-sm btn-outline-secondary" type="button" onclick="setVoiceApproval(this, 'voice_status', 'reviewed')">Mark voice reviewed</button>
                                    <button style="flex:1 1 45%;min-width:0;" class="btn btn-sm btn-outline-danger" type="button" onclick="setVoiceApproval(this, 'voice_status', 'rejected')">Reject voice</button>
                                </div>
                                <div class="input-group input-group-sm mt-2" ${voice.version_id ? 'hidden' : ''}>
                                    <select class="form-select voice-version-select" aria-label="${escapeHtml('Voice version for ' + label)}" onchange="selectVoiceVersion(this)">
                                        <option value="" disabled>Choose a saved version</option>
                                        ${Object.entries(config.versions || {}).map(([id, version]) => `<option value="${escapeHtml(id)}" ${config.active_version === id ? 'selected' : ''}>${escapeHtml(id)}${version.age_group ? ` · ${escapeHtml(version.age_group)}` : ''}</option>`).join('')}
                                    </select>
                                    <button class="btn btn-outline-secondary" type="button" aria-label="${escapeHtml('Add voice version for ' + label)}" onclick="addVoiceVersion(this)">Version</button>
                                </div>
                                <div class="form-text" ${voice.version_id ? 'hidden' : ''}>Selecting a saved version replaces the current voice settings. Save the current settings as a version first if you want to return to them later.</div>
                                ${((voice.traits && voice.traits.states) || []).length > 1 ? `<div class="voice-states mt-1"><button class="btn btn-sm btn-outline-primary" type="button" aria-label="${escapeHtml('Load voice changes for ' + label)}" onclick="openVoiceStates(this)"><i class="fas fa-user-clock me-1"></i>Voice changes (${voice.traits.states.length - 1} change${voice.traits.states.length === 2 ? '' : 's'})</button><div class="form-text">Review which voice is used before and after each change.</div><div class="voice-state-rows"></div></div>` : ''}
                                <button class="btn btn-sm btn-outline-secondary mt-1" type="button" aria-label="${escapeHtml('Generate more voice candidates for ' + label)}" onclick="suggestMoreVoices(this)"><i class="fas fa-wand-magic-sparkles me-1"></i>Generate more candidates</button>
                                <div class="saved-voice-candidates">${getVoiceCandidateMarkup(config.candidates)}</div>
                                <div class="form-check form-switch small">
                                    <input class="form-check-input voice-ready" aria-label="${escapeHtml('Ready for audio generation for ' + label)}" type="checkbox" id="voice-ready-${index}" ${ready ? 'checked' : ''} onchange="onVoiceReadyChange(this)">
                                    <label class="form-check-label" for="voice-ready-${index}">Ready</label>
                                </div>
                                ${voice.version_id ? `<label class="form-label small mt-1">State seed<input class="form-control form-control-sm voice-seed" type="number" min="-1" max="4294967295" aria-label="${escapeHtml('Seed for ' + label)}" value="${escapeHtml(String(config.seed ?? '-1'))}"></label>` : ''}
                                <div class="form-text small text-muted mt-1">Alias of:</div>
                                <select class="form-select form-select-sm alias-select mt-1" ${voice.version_id ? 'disabled' : ''} aria-label="${escapeHtml('Alias target for ' + label)}">
                                    <option value="">-- None --</option>
                                    ${(() => {
                                        const names = (window._voicesNames || []).filter(n => n !== voice.name);
                                        return names.map(n => `<option value="${escapeHtml(n)}" ${config.alias_of === n ? 'selected' : ''}>${escapeHtml(n)}</option>`).join('');
                                    })()}
                                </select>
                            </div>
                            <div class="col-md-9">
                                <div class="mb-2">
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input voice-type" type="radio" name="type_${index}" value="custom" id="voice-type-${index}-custom" aria-label="${escapeHtml('Custom voice for ' + label)}" ${voiceType === 'custom' ? 'checked' : ''} onchange="toggleVoiceType(this)">
                                        <label class="form-check-label" for="voice-type-${index}-custom">Custom voice</label>
                                    </div>
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input voice-type" type="radio" name="type_${index}" value="builtin_lora" id="voice-type-${index}-builtin_lora" aria-label="${escapeHtml('Built-in LoRA voice for ' + label)}" ${voiceType === 'builtin_lora' ? 'checked' : ''} onchange="toggleVoiceType(this)">
                                        <label class="form-check-label" for="voice-type-${index}-builtin_lora">Built-in LoRA voice</label>
                                    </div>
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input voice-type" type="radio" name="type_${index}" value="clone" id="voice-type-${index}-clone" aria-label="${escapeHtml('Clone voice for ' + label)}" ${voiceType === 'clone' ? 'checked' : ''} onchange="toggleVoiceType(this)">
                                        <label class="form-check-label" for="voice-type-${index}-clone">Clone voice</label>
                                    </div>
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input voice-type" type="radio" name="type_${index}" value="lora" id="voice-type-${index}-lora" aria-label="${escapeHtml('LoRA voice for ' + label)}" ${voiceType === 'lora' ? 'checked' : ''} onchange="toggleVoiceType(this)">
                                        <label class="form-check-label" for="voice-type-${index}-lora">LoRA voice</label>
                                    </div>
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input voice-type" type="radio" name="type_${index}" value="design" id="voice-type-${index}-design" aria-label="${escapeHtml('Voice Design for ' + label)}" ${voiceType === 'design' ? 'checked' : ''} onchange="toggleVoiceType(this)">
                                        <label class="form-check-label" for="voice-type-${index}-design">Voice Design</label>
                                    </div>
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input voice-type" type="radio" name="type_${index}" value="ensemble" id="voice-type-${index}-ensemble" aria-label="${escapeHtml('Character ensemble for ' + label)}" ${voiceType === 'ensemble' ? 'checked' : ''} onchange="toggleVoiceType(this)">
                                        <label class="form-check-label" for="voice-type-${index}-ensemble">Character ensemble</label>
                                    </div>
                                </div>

                                <!-- Custom Options -->
                                <div class="custom-opts" style="display: ${voiceType === 'custom' ? 'block' : 'none'}">
                                    <div class="row g-2">
                                        <div class="col-md-6">
                                            <select class="form-select voice-select" aria-label="${escapeHtml('Custom voice for ' + label)}">
                                                ${customVoices.map(v => `<option value="${escapeHtml(v)}" ${config.voice === v ? 'selected' : ''}>${escapeHtml(v)}</option>`).join('')}
                                            </select>
                                        </div>
                                        <div class="col-md-6">
                                            <input type="text" class="form-control character-style" aria-label="${escapeHtml('Custom voice style for ' + label)}" placeholder="Character style (e.g. refined aristocratic tone, heavy Scottish accent)" value="${escapeHtml(config.character_style || config.default_style || '')}">
                                            ${renderStyleTimeline(voice.name, config)}
                                        </div>
                                    </div>
                                </div>

                                <!-- Built-in LoRA Options -->
                                <div class="builtin-lora-opts" style="display: ${voiceType === 'builtin_lora' ? 'block' : 'none'}">
                                    <div class="row g-2">
                                        <div class="col-md-6">
                                            <select class="form-select builtin-lora-select" aria-label="${escapeHtml('Built-in LoRA voice for ' + label)}">
                                                <option value="">-- Select built-in voice --</option>
                                                ${(() => {
                                                    const models = (window._loraModelsCache || []).filter(m => m.builtin);
                                                    const males = models.filter(m => m.gender === 'male');
                                                    const females = models.filter(m => m.gender === 'female');
                                                    let html = '';
                                                    if (males.length) {
                                                        html += '<optgroup label="Male">';
                                                        html += males.map(m => `<option value="${escapeHtml(m.id)}" ${config.adapter_id === m.id ? 'selected' : ''} ${m.downloaded === false ? 'disabled' : ''}>${m.favorite ? '★ ' : ''}${escapeHtml(m.name)}${m.downloaded === false ? ' (not downloaded)' : ''} — ${escapeHtml(m.description || '')}</option>`).join('');
                                                        html += '</optgroup>';
                                                    }
                                                    if (females.length) {
                                                        html += '<optgroup label="Female">';
                                                        html += females.map(m => `<option value="${escapeHtml(m.id)}" ${config.adapter_id === m.id ? 'selected' : ''} ${m.downloaded === false ? 'disabled' : ''}>${m.favorite ? '★ ' : ''}${escapeHtml(m.name)}${m.downloaded === false ? ' (not downloaded)' : ''} — ${escapeHtml(m.description || '')}</option>`).join('');
                                                        html += '</optgroup>';
                                                    }
                                                    return html;
                                                })()}
                                            </select>
                                        </div>
                                        <div class="col-md-6">
                                            <input type="text" class="form-control builtin-lora-style" aria-label="${escapeHtml('Built-in LoRA voice style for ' + label)}" placeholder="Character style (e.g. refined aristocratic tone, heavy Scottish accent)" value="${escapeHtml(voiceType === 'builtin_lora' ? (config.character_style || '') : '')}">
                                        </div>
                                    </div>
                                    <small class="text-muted mt-1 d-block">Grayed-out voices need to be downloaded first. Go to the <strong>Training</strong> tab to download them.</small>
                                </div>

                                <!-- Clone Options -->
                                <div class="clone-opts" style="display: ${voiceType === 'clone' ? 'block' : 'none'}">
                                    <div class="row g-2 mb-2 align-items-center">
                                        <div class="col">
                                            <select class="form-select designed-voice-select" aria-label="${escapeHtml('Reference voice for ' + label)}" onchange="onDesignedVoiceSelect(this)">
                                                <option value="">-- Select voice or enter path manually --</option>
                                                ${(window._cloneVoicesCache || []).length ? `<optgroup label="Uploaded Voices">
                                                    ${(window._cloneVoicesCache || []).map(v => `<option value="clone:${escapeHtml(v.id)}" ${reference?.type === 'clone' && reference.id === v.id ? 'selected' : ''}>${escapeHtml(v.name)}</option>`).join('')}
                                                </optgroup>` : ''}
                                                ${(window._designedVoicesCache || []).length ? `<optgroup label="Designed Voices">
                                                    ${(window._designedVoicesCache || []).map(v => `<option value="design:${escapeHtml(v.id)}" ${reference?.type === 'design' && reference.id === v.id ? 'selected' : ''}>${escapeHtml(v.name)}</option>`).join('')}
                                                </optgroup>` : ''}
                                                <option value="__manual__" ${config.ref_audio && !reference ? 'selected' : ''}>Custom path...</option>
                                            </select>
                                        </div>
                                        <div class="col-auto">
                                            <button class="btn btn-sm btn-outline-primary" aria-label="${escapeHtml('Upload reference audio for ' + label)}" onclick="uploadCloneVoice(this)" title="Upload audio file"><i class="fas fa-upload"></i> Upload</button>
                                            <input type="file" class="clone-voice-file-input" aria-label="${escapeHtml('Upload reference audio for ' + label)}" accept=".wav,.mp3,.flac,.ogg" style="display:none" onchange="handleCloneVoiceUpload(this)">
                                        </div>
                                    </div>
                                    <input type="text" class="form-control ref-text mb-2" aria-label="${escapeHtml('Reference transcript for ' + label)}" placeholder="Reference Text" value="${escapeHtml(config.ref_text || '')}">
                                    <label class="form-label small d-block w-100">Persona description<textarea class="form-control persona-description" aria-label="${escapeHtml('Persona description for ' + label)}">${escapeHtml(config.description || '')}</textarea></label>
                                    <div class="input-group">
                                        <input type="text" class="form-control ref-audio" aria-label="${escapeHtml('Reference audio path for ' + label)}" placeholder="Path to audio file" value="${escapeHtml(config.ref_audio || '')}" ${reference ? 'readonly' : ''}>
                                        <button class="btn btn-sm btn-outline-secondary clone-play-btn" aria-label="${escapeHtml('Play reference audio for ' + label)}" onclick="playCloneVoice(this)" title="Play reference audio" style="display:${config.ref_audio ? 'inline-block' : 'none'}"><i class="fas fa-play"></i></button>
                                        <button class="btn btn-sm btn-outline-danger clone-delete-btn" aria-label="${escapeHtml('Delete uploaded reference voice for ' + label)}" onclick="deleteCloneVoice(this)" title="Delete uploaded voice" style="display:${reference?.type === 'clone' ? 'inline-block' : 'none'}"><i class="fas fa-trash"></i></button>
                                    </div>
                                </div>

                                <!-- LoRA Options -->
                                <div class="lora-opts" style="display: ${voiceType === 'lora' ? 'block' : 'none'}">
                                    <div class="row g-2">
                                        <div class="col-md-6">
                                            <select class="form-select lora-adapter-select" aria-label="${escapeHtml('Trained LoRA voice for ' + label)}">
                                                <option value="">-- Select trained adapter --</option>
                                                ${(window._loraModelsCache || []).map(m => `<option value="${escapeHtml(m.id)}" ${config.adapter_id === m.id ? 'selected' : ''}>${m.favorite ? '★ ' : ''}${escapeHtml(m.name)}</option>`).join('')}
                                            </select>
                                        </div>
                                        <div class="col-md-6">
                                            <input type="text" class="form-control lora-character-style" aria-label="${escapeHtml('LoRA voice style for ' + label)}" placeholder="Character style (e.g. refined aristocratic tone, heavy Scottish accent)" value="${escapeHtml(voiceType === 'lora' ? (config.character_style || '') : '')}">
                                        </div>
                                    </div>
                                </div>

                                <!-- Voice Design Options -->
                                <div class="design-opts" style="display: ${voiceType === 'design' ? 'block' : 'none'}">
                                    <input type="text" class="form-control design-description mb-1" aria-label="${escapeHtml('Base voice description for ' + label)}" placeholder="Base voice description (e.g. Young strong soldier)" value="${escapeHtml(config.description || '')}">
                                    <span class="text-muted small">Per-line instruct is appended to this description as delivery/emotion direction</span>
                                    <div class="mt-2">
                                        <button type="button" class="btn btn-sm btn-outline-primary" aria-label="${escapeHtml('Re-design voice for ' + label)}" onclick="openVoiceDesignEditor(this)">
                                            <i class="fas fa-wand-magic-sparkles me-1"></i>Re-design Voice
                                        </button>
                                    </div>
                                </div>

                                <!-- Ensemble Options -->
                                <div class="ensemble-opts" style="display: ${voiceType === 'ensemble' ? 'block' : 'none'}">
                                    <div class="ensemble-members small">${ensembleMembersMarkup(voice.name, config.members)}</div>
                                    <span class="text-muted small">Each character is voiced with whatever voice it already has, then mixed together. Clips are aligned to the longest, so this sounds like a chorus rather than exact unison.</span>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        }

        // Suggest members for a compound name like "Petra and Subaru" or
        // "GOD/BUDDHA/OD LAGNA": keep the parts that are real characters.
        function suggestEnsembleMembers(name) {
            const names = window._voicesNames || [];
            const lower = new Map(names.map(n => [n.toLowerCase(), n]));
            return name.split(/\s+&\s+|\s+and\s+|\/|\s*\+\s*/i)
                .map(part => lower.get(part.trim().toLowerCase()))
                .filter(n => n && n !== name);
        }

        function ensembleMembersMarkup(name, members) {
            const names = (window._voicesNames || []).filter(n => n !== name);
            if (names.length === 0) {
                return '<div class="text-muted">No other characters to combine.</div>';
            }
            // Only prefill when nothing is saved yet — never override a choice.
            const selected = new Set(Array.isArray(members) ? members : suggestEnsembleMembers(name));
            return names.map(n => `
                <div class="form-check form-check-inline">
                    <input class="form-check-input ensemble-member" aria-label="${escapeHtml(`Include ${n} in voices together for ${name}`)}" type="checkbox" value="${escapeHtml(n)}" ${selected.has(n) ? 'checked' : ''} onchange="saveVoicesDebounced()">
                    <label class="form-check-label">${escapeHtml(n)}</label>
                </div>
            `).join('');
        }

        window.toggleVoiceType = (radio) => {
            const card = radio.closest('.card-body');
            card.querySelector('.custom-opts').style.display = radio.value === 'custom' ? 'block' : 'none';
            card.querySelector('.builtin-lora-opts').style.display = radio.value === 'builtin_lora' ? 'block' : 'none';
            card.querySelector('.clone-opts').style.display = radio.value === 'clone' ? 'block' : 'none';
            card.querySelector('.lora-opts').style.display = radio.value === 'lora' ? 'block' : 'none';
            card.querySelector('.design-opts').style.display = radio.value === 'design' ? 'block' : 'none';
            card.querySelector('.ensemble-opts').style.display = radio.value === 'ensemble' ? 'block' : 'none';
            saveVoicesDebounced();
        };

        async function refreshVoiceMetadata() {
            await flushVoiceSaves();
            const localRevision = voiceSaveQueue.getRevision();
            const snapshot = await API.get('/api/voice_config/snapshot');
            if (voiceSaveQueue.isDirty() || localRevision !== voiceSaveQueue.getRevision()) {
                throw new Error('Voice edits changed while refreshing. Your edits are still pending; try again.');
            }
            if (!Array.isArray(snapshot.voices) || !/^[0-9a-f]{64}$/.test(snapshot.revision) || !/^[0-9a-f]{64}$/.test(snapshot.book_token)) {
                throw new Error('Invalid voice snapshot; your voice settings were not replaced.');
            }
            _voiceSaveSnapshot = snapshot;
            renderVoiceDrafts();
            const voices = snapshot.voices;
            // Cache simple names for alias dropdowns
            window._voicesNames = voices.map(v => v.name);
            window._voicesByName = Object.fromEntries(voices.map(v => [v.name, v]));
            return voices;
        }

        let _voiceResourcesRefreshedAt = -Infinity;
        let _voiceCardsRevision = null;
        let _voiceCardsBookToken = null;

        let _voiceSeedRepairPending = false;

        function getVoiceSeedRepairMarkup(snapshot) {
            const changes = snapshot?.seed_changes || [];
            if (!changes.length) { return ''; }
            const rows = changes.map(change => `<li>${escapeHtml(change.name)}: proposed seed ${escapeHtml(change.seed)}</li>`).join('');
            return `<div class="alert alert-warning"><strong>${changes.length} unseeded voice settings</strong>
                <ul>${rows}</ul><p>An original-file backup will be saved. Existing rendered audio is kept. Regenerated audio may sound different.
                Individual rendering uses character seeds; fast-batch rendering uses its separate batch seed.</p>
                <button type="button" class="btn btn-sm btn-outline-warning" onclick="applyStableVoiceSeeds()" ${_voiceSeedRepairPending ? 'disabled' : ''}>Apply stable seeds to unseeded entries only</button></div>`;
        }

        window.applyStableVoiceSeeds = async function applyStableVoiceSeeds() {
            if (_voiceSeedRepairPending) { return; }
            _voiceSeedRepairPending = true;
            try {
                await flushVoiceSaves();
                const snapshot = _voiceSaveSnapshot;
                const changes = snapshot.seed_changes || [];
                if (!changes.length) { return; }
                if (voiceSaveQueue.isDirty() || snapshot.revision !== _voiceCardsRevision
                        || snapshot.book_token !== _voiceCardsBookToken) {
                    throw new Error('Voice settings changed; reload voices to review the new seed suggestions.');
                }
                const result = await API.post('/api/voice_config/seed_unseeded', {
                    revision: snapshot.revision, book_token: snapshot.book_token,
                });
                await loadVoices(false);
                showToast(`Applied ${result.changes.length} stable seeds.${result.backup ? ' Backup: ' + result.backup : ''} Existing audio was kept.`, 'success', 8000);
            } catch (e) {
                showActionError("Stable seed repair failed", e, "Reload Voices and review the current seed suggestions before applying again; the book or voice settings may have changed.");
            } finally {
                _voiceSeedRepairPending = false;
            }
        };

        function getVoicePanelSnapshot(container, bookToken) {
            const panels = [...(container.querySelectorAll?.('.voice-card') || [])].map(card => {
                const panel = card.querySelector('.voice-state-rows');
                if (!panel || !panel.innerHTML.trim()
                    || (typeof pendingVoiceStateLoads !== 'undefined' && pendingVoiceStateLoads.has(panel))
                    || (typeof pendingVoiceStateSaves !== 'undefined'
                        && pendingVoiceStateSaves.has(`${currentBookFilename}\0${card.dataset.voice}`))) { return null; }
                return {speaker: card.dataset.voice, panel};
            }).filter(Boolean);
            return {bookToken, panels};
        }

        function restoreVoicePanels(container, snapshot, bookToken) {
            if (snapshot.bookToken !== bookToken) { return; }
            for (const {speaker, panel} of snapshot.panels) {
                const card = [...(container.querySelectorAll?.('.voice-card') || [])]
                    .find(row => row.dataset.voice === speaker);
                const replacement = card?.querySelector('.voice-state-rows');
                if (replacement) { replacement.replaceWith(panel); }
            }
        }

        function getVoiceListFocusSnapshot(container, bookToken) {
            const control = document.activeElement;
            if (!control || !container.contains(control)) { return null; }
            const card = control.closest('.voice-card');
            if (!card) { return null; }
            return {control, bookToken, speaker: card.dataset.voice, version: card.dataset.version || '', tag: control.tagName,
                key: control.getAttribute('data-voice-focus-key'), label: control.getAttribute('aria-label'),
                action: control.getAttribute('onclick') || control.getAttribute('onchange'),
                selectionStart: control.selectionStart, selectionEnd: control.selectionEnd,
                selectionDirection: control.selectionDirection};
        }

        function restoreVoiceListFocus(container, snapshot, bookToken) {
            if (!snapshot || snapshot.bookToken !== bookToken
                || (document.activeElement !== document.body && document.activeElement !== snapshot.control)) { return; }
            const card = Array.from(container.querySelectorAll('.voice-card')).find(row => row.dataset.voice === snapshot.speaker && (row.dataset.version || '') === (snapshot.version || ''));
            if (!card) { return; }
            const controls = Array.from(card.querySelectorAll('button,input,select,textarea'));
            const control = controls.includes(snapshot.control) ? snapshot.control : controls.find(field => field.tagName === snapshot.tag
                && (snapshot.key ? field.getAttribute('data-voice-focus-key') === snapshot.key
                    : snapshot.label ? field.getAttribute('aria-label') === snapshot.label
                        : snapshot.action && (field.getAttribute('onclick') || field.getAttribute('onchange')) === snapshot.action));
            const target = control && !control.disabled && control.getClientRects().length ? control : card.querySelector('h5');
            if (!target || !target.getClientRects().length) { return; }
            if (target !== control) { target.tabIndex = -1; }
            target.focus({preventScroll: true});
            if (target === control && typeof snapshot.selectionStart === 'number' && target.setSelectionRange) {
                target.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd, snapshot.selectionDirection);
            }
        }

        async function loadVoices(refreshResources = true) {
            if (!refreshResources && loadVoices.pending) { return loadVoices.pending; }
            const loading = (async () => {
                await flushVoiceSaves();
                const reuseResources = !refreshResources && performance.now() - _voiceResourcesRefreshedAt < 10000;
                let resourcesComplete = true;
                const refreshedResources = [];
                const failedResources = [];
                if (!reuseResources) { _voiceResourcesRefreshedAt = -Infinity; }
                // Fetch independent lists together; render only after dropdowns and
                // per-character cast counts are ready. Keep old optional lists on error.
                const resources = reuseResources ? [] : [
                    ['_designedVoicesCache', '/api/voice_design/list', 'designed-voices'],
                    ['_cloneVoicesCache', '/api/clone_voices/list', 'clone-voices'],
                    ['_loraModelsCache', '/api/lora/models', 'lora-models'],
                ].map(async ([key, path, label]) => {
                    try {
                        const list = await API.get(path);
                        if (!Array.isArray(list)) { throw new Error('Voice resource list is malformed'); }
                        window[key] = list;
                        refreshedResources.push(path);
                    }
                    catch (e) { resourcesComplete = false; failedResources.push(path); console.debug(`${label} cache refresh failed`, e); }
                });
                const [voices] = await Promise.all([
                    refreshVoiceMetadata(), ...resources,
                    ...(reuseResources ? [] : [loadCastLibrary().catch(e => { resourcesComplete = false; failedResources.push('/api/voice_library'); console.debug('cast library refresh failed', e); })]),
                ]);
                if (!reuseResources) {
                    _voiceResourcesRefreshedAt = resourcesComplete ? performance.now() : -Infinity;
                }
                if (reuseResources && _voiceCardsRevision === _voiceSaveSnapshot.revision
                        && _voiceCardsBookToken === _voiceSaveSnapshot.book_token) {
                    return {refreshedResources, failedResources};
                }
                refreshVoicesScope();
                const narrator = window._voicesByName.NARRATOR || window._voicesByName.Narrator;
                const narratorSelect = document.getElementById('narrator-strategy');
                if (narratorSelect && narrator?.config?.narrator_strategy) {
                    narratorSelect.value = narrator.config.narrator_strategy;
                }
                updateNarratorPreviewFields();
                const container = document.getElementById('voices-list');
                const focus = getVoiceListFocusSnapshot(container, _voiceCardsBookToken);
                const panels = getVoicePanelSnapshot(container, _voiceCardsBookToken);
                if (voices.length === 0) {
                    container.innerHTML = '<div class="alert alert-info">No voices found. Generate a script first.</div>';
                    _voiceCardsRevision = _voiceSaveSnapshot.revision;
                    _voiceCardsBookToken = _voiceSaveSnapshot.book_token;
                    return {refreshedResources, failedResources};
                }
                container.innerHTML = getVoiceSeedRepairMarkup(_voiceSaveSnapshot) + getStateVoiceCardsMarkup(voices);
                _voiceCardsRevision = _voiceSaveSnapshot.revision;
                _voiceCardsBookToken = _voiceSaveSnapshot.book_token;
                if (typeof renderPersonaRecoveryTargets === 'function') { renderPersonaRecoveryTargets(); }
                renderReadyCount();
                onToggleHideReady();

                // If any voice has no saved config, save defaults immediately
                if (!_voiceRecoveryDrafts.some(record => record.book_token === _voiceSaveSnapshot.book_token) && voices.some(v => !v.config || Object.keys(v.config).length === 0)) {
                    saveVoicesDebounced();
                }

                // Restore any pending voice suggestions onto the freshly rendered cards
                if (window._voiceSuggestions && Object.keys(window._voiceSuggestions).length) {
                    renderVoiceSuggestions();
                }
                restoreVoicePanels(container, panels, _voiceCardsBookToken);
                restoreVoiceListFocus(container, focus, _voiceCardsBookToken);
                return {refreshedResources, failedResources};
            })();
            loadVoices.pending = loading;
            try { return await loading; }
            finally { if (loadVoices.pending === loading) { loadVoices.pending = null; } }
        }

        window.selectVoiceVersion = async function selectVoiceVersion(select) {
            const versionId = select.value;
            if (!versionId) { return; }
            const speaker = select.closest('.voice-card')?.dataset.voice;
            try {
                await API.post(`/api/voices/${encodeURIComponent(speaker)}/versions/${encodeURIComponent(versionId)}/select`, {});
                await loadVoices();
                showToast(`Selected ${versionId} for ${speaker}.`, 'success');
            } catch (e) { showActionError("Version selection failed", e, "Reload Voices to check the active saved version before selecting again."); }
        };

        window.addVoiceVersion = async function addVoiceVersion(button) {
            if (button.disabled) { return; }
            const card = button.closest('.voice-card');
            const speaker = card?.dataset.voice;
            const book = currentBookFilename;
            button.disabled = true;
            try {
                const values = await showPresetEditor({title: `Add voice version for ${speaker}`, nameLabel: 'Version ID',
                    descriptionLabel: 'Age group', description: 'adult', descriptionPlaceholder: '', actionLabel: 'Add version',
                    helperText: 'Use a version ID such as teen or elderly. The new version starts from this character’s current voice settings. Leave Age group empty to use adult.'});
                if (!values) { return; }
                if (book !== currentBookFilename || card?.isConnected === false) {
                    showToast('The book changed. Review it before adding a voice version.', 'warning'); return;
                }
                await flushVoiceSaves();
                if (book !== currentBookFilename || card?.isConnected === false) {
                    showToast('The book changed. Review it before adding a voice version.', 'warning'); return;
                }
                await API.post(`/api/voices/${encodeURIComponent(speaker)}/versions`, {
                    version_id: values.name, age_group: values.description || 'adult'
                });
                showToast(`Version saved for ${speaker}.`, 'success');
                if (book === currentBookFilename) { await loadVoices(); }
            } catch (e) { showActionError("Version save failed", e, "Reload Voices to check whether the version was saved before adding it again."); }
            finally { button.disabled = false; }
        };

        window.saveNarratorStrategy = async function saveNarratorStrategy(strategy) {
            updateNarratorPreviewFields();
            const book = currentBookFilename;
            const token = _voiceSaveSnapshot?.book_token;
            const request = {};
            window._narratorStrategyRequest = request;
            const isCurrentBook = () => book === currentBookFilename && token === _voiceSaveSnapshot?.book_token;
            const isLatest = () => isCurrentBook() && window._narratorStrategyRequest === request;
            const previous = window._narratorStrategySave || Promise.resolve();
            const saving = (async () => {
                await previous.catch(() => {});
                if (!isCurrentBook()) { return; }
                await flushVoiceSaves();
                if (!isCurrentBook()) { return; }
                if (!token) { throw new Error('Reload Voices before saving the narrator strategy.'); }
                const result = await API.post('/api/narrator/strategy', {strategy, book_token: token});
                if (!isCurrentBook()) { return; }
                if (/^[0-9a-f]{64}$/.test(result.revision)) { _voiceSaveSnapshot.revision = result.revision; }
                if (isLatest()) { showToast('Narrator strategy saved.', 'success'); }
            })();
            window._narratorStrategySave = saving;
            try { await saving; }
            catch (e) {
                if (isLatest()) { showActionError("Narrator strategy failed", e, "Reload Voices to check the saved narrator strategy before changing it again."); }
            } finally {
                if (window._narratorStrategySave === saving) { window._narratorStrategySave = null; }
            }
        };

        window.updateNarratorPreviewFields = function updateNarratorPreviewFields() {
            const strategy = document.getElementById('narrator-strategy')?.value || 'global';
            const focusGroup = document.getElementById('narrator-focus-group');
            const versionGroup = document.getElementById('narrator-version-group');
            const focus = document.getElementById('narrator-focus');
            const version = document.getElementById('narrator-version');
            const needsFocus = strategy.includes('character') || strategy === 'focus';
            const needsVersion = strategy === 'chapter';
            if (focusGroup) { focusGroup.style.display = needsFocus ? '' : 'none'; }
            if (versionGroup) { versionGroup.style.display = needsVersion ? '' : 'none'; }
            if (focus) {
                const selected = focus.value;
                const names = (window._voicesNames || []).filter(name => name !== 'NARRATOR' && name !== 'Narrator');
                focus.innerHTML = '<option value="">No focus override</option>' +
                    names.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
                focus.value = names.includes(selected) ? selected : '';
            }
            if (version) {
                const selected = version.value;
                const narrator = window._voicesByName?.NARRATOR || window._voicesByName?.Narrator;
                const versions = Object.keys(narrator?.config?.versions || {}).sort();
                version.innerHTML = '<option value="">Default narrator</option>' +
                    versions.map(id => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join('');
                version.value = versions.includes(selected) ? selected : '';
            }
        };

        window.previewNarratorSelection = async function previewNarratorSelection() {
            const strategy = document.getElementById('narrator-strategy')?.value || 'global';
            updateNarratorPreviewFields();
            const focus = document.getElementById('narrator-focus')?.value || null;
            const version = document.getElementById('narrator-version')?.value || null;
            const status = document.getElementById('narrator-preview-status');
            const book = currentBookFilename;
            const request = {};
            window._narratorPreviewRequest = request;
            const isCurrent = () => window._narratorPreviewRequest === request && book === currentBookFilename
                && strategy === (document.getElementById('narrator-strategy')?.value || 'global')
                && focus === (document.getElementById('narrator-focus')?.value || null)
                && version === (document.getElementById('narrator-version')?.value || null);
            try {
                const result = await API.post('/api/narrator/preview', {
                    strategy, focus_speaker: focus || null, narrator_version: version || null,
                });
                if (!isCurrent()) { return; }
                const selected = result.selected || {};
                status.textContent = `Selected ${selected.adapter_id || selected.voice || selected.type || 'default'}.`;
            } catch (e) {
                if (isCurrent()) { showActionError("Narrator preview failed", e, "Check the narrator strategy, selected speaker and saved version, then preview again."); }
            }
        };

        window.setVoiceApproval = async function setVoiceApproval(button, field, status) {
            const speaker = button.closest('.voice-card')?.dataset.voice;
            try {
                await postVoiceTarget(button, `/api/voices/${encodeURIComponent(speaker)}/approval`, {[field]: status});
                await loadVoices();
                showToast(`${field === 'persona_status' ? 'Persona' : 'Voice'} marked ${status} for ${speaker}.`, 'success');
            } catch (e) { showActionError('Approval update failed', e, 'Reload Voices to check the current approval before trying again. Your voice assignments remain available.'); }
        };

        window.editPersonaVoiceAudit = async function editPersonaVoiceAudit(button) {
            if (button.disabled) { return; }
            const card = button.closest('.voice-card');
            const speaker = card?.dataset.voice;
            if (!speaker) { return; }
            const book = currentBookFilename;
            const current = getVoiceCardMetadata(card).persona_voice_audit || {};
            button.disabled = true;
            try {
                const values = await showPresetEditor({title: `Edit voice audit for ${speaker}`,
                    nameLabel: 'Suggestion reason (optional)', name: current.suggestion_reason || '', allowEmptyName: true,
                    descriptionLabel: 'Voice adapter ID (optional)', description: current.voice_adapter_id || '',
                    descriptionPlaceholder: '', actionLabel: 'Save audit',
                    helperText: 'Correct the assignment notes. Empty fields leave existing saved values unchanged.'});
                if (!values) { return; }
                if (book !== currentBookFilename || card?.isConnected === false || card?.dataset.voice !== speaker) {
                    showToast('The book or voice changed. Review it before saving the audit.', 'warning'); return;
                }
                await postVoiceTarget(button, `/api/voices/${encodeURIComponent(speaker)}/persona-voice-audit`, {
                    persona_ref: current.persona_ref || null,
                    persona_description: current.persona_description || null,
                    voice_adapter_id: values.description,
                    suggestion_reason: values.name,
                });
                if (book === currentBookFilename) { await loadVoices(); }
                showToast(`Persona-to-voice audit updated for ${speaker}.`, 'success');
            } catch (e) { showActionError("Audit update failed", e, "Reload Voices and check the current persona-to-voice audit before saving it again."); }
            finally { button.disabled = false; }
        };

        window.removeStatePersona = async function removeStatePersona(button) {
            const card = button.closest('.voice-card');
            const speaker = card?.dataset.voice, version = card?.dataset.version;
            if (!version) { return; }
            const token = _voiceCardsBookToken;
            try {
                if (!await showConfirm('Remove this saved state persona? Its audio files, base voice and other states are kept. Applied versions must be removed from the timeline first.',
                    {title: 'Remove state persona?', actionLabel: 'Remove state', danger: true})) { return; }
                await flushVoiceSaves();
                if (!token || token !== _voiceSaveSnapshot?.book_token || card.isConnected === false) {
                    throw new Error('The book or state card changed. Reload Voices before trying again.');
                }
                await API.del(`/api/voices/${encodeURIComponent(speaker)}/versions/${encodeURIComponent(version)}?book_token=${encodeURIComponent(token)}`);
                await loadVoices();
                showToast('Saved state persona removed. Its audio files were kept.', 'success');
            } catch (e) { showActionError('State removal failed', e, 'Reload Voices and review its applied timeline before trying again.'); }
        };

        window.regeneratePersona = async function regeneratePersona(button) {
            const card = button.closest('.voice-card');
            const speaker = card?.dataset.voice;
            const version = card?.dataset.version || null;
            const token = version ? _voiceCardsBookToken : null;
            try {
                if (version) { await flushVoiceSaves(); }
                if (!(await confirmIfRemote('this persona regeneration', true))) { return; }
                if (version && (token !== _voiceSaveSnapshot?.book_token || card.isConnected === false)) { return; }
                const payload = {speaker, advanced: false, context_lines: getPersonaContextLines()};
                if (version) { Object.assign(payload, {advanced: true, state_version: version, book_token: token}); }
                await API.post('/api/generate_personas', payload);
                pollPersonaStatus();
                showToast(`Persona regeneration started for ${speaker}.`, 'success');
            } catch (e) { showActionError("Persona regeneration failed", e, "Check the persona task status before regenerating again. For an LLM refusal, use Setup → Test Connection."); }
        };

        window.generateAgeVersion = async function generateAgeVersion(button, presetAge) {
            if (button.disabled) { return; }
            const card = button.closest('.voice-card');
            const speaker = card?.dataset.voice;
            if (!speaker) { return; }
            const book = currentBookFilename;
            const isCurrent = () => book === currentBookFilename && card?.isConnected !== false && card?.dataset.voice === speaker;
            button.disabled = true;
            try {
                const values = await showPresetEditor({title: `Generate age version for ${speaker}`, nameLabel: 'Age profile',
                    name: presetAge || '', includeDescription: false, actionLabel: 'Generate age version',
                    helperText: 'Use infant, toddler, young_child, child, teen, young_adult, adult, middle_aged or elderly.'});
                if (!values) { return; }
                if (!isCurrent()) {
                    showToast('The book or voice changed. Review it before generating an age version.', 'warning'); return;
                }
                if (!(await confirmIfRemote('this age-version generation', true))) { return; }
                if (!isCurrent()) {
                    showToast('The book or voice changed. Review it before generating an age version.', 'warning'); return;
                }
                await API.post('/api/generate_personas', {
                    speaker, age_group: values.name, advanced: false,
                    context_lines: getPersonaContextLines(),
                });
                pollPersonaStatus();
                showToast(`Generating ${values.name} version for ${speaker}.`, 'success');
            } catch (e) { showActionError("Age version generation failed", e, "Check the persona task status and saved age versions before generating again. Use an age profile listed in the dialog."); }
            finally { button.disabled = false; }
        };

        window.selectVoiceCandidate = async function selectVoiceCandidate(button, candidateId) {
            const speaker = button.closest('.voice-card')?.dataset.voice;
            try {
                await postVoiceTarget(button, `/api/voices/${encodeURIComponent(speaker)}/candidates/${encodeURIComponent(candidateId)}/select`, {});
                await loadVoices();
                showToast(`Selected ${candidateId} for ${speaker}.`, 'success');
            } catch (e) { showActionError("Candidate selection failed", e, "Reload Voices to check the selected candidate before selecting again."); }
        };

        async function applyConfirmedVoiceRemoval(button, message, remove) {
            if (button?.disabled) { return; }
            const book = currentBookFilename;
            const wasDisabled = button?.disabled;
            if (button) { button.disabled = true; }
            try {
                if (!await showConfirm(message, {title: 'Remove voice setting?', actionLabel: 'Remove', danger: true})) { return; }
                if (book !== currentBookFilename) {
                    showToast('The book changed. Review the current voice before removing anything.', 'warning');
                    return;
                }
                await remove();
            } finally {
                if (button) { button.disabled = wasDisabled; }
            }
        }

        window.deleteVoiceCandidate = async function deleteVoiceCandidate(button, candidateId) {
            const speaker = button.closest('.voice-card')?.dataset.voice;
            if (!speaker) { return; }
            try {
                await applyConfirmedVoiceRemoval(button, `Delete saved candidate "${candidateId}" for ${speaker}? This cannot be undone.`, async () => {
                    await deleteVoiceTarget(button, `/api/voices/${encodeURIComponent(speaker)}/candidates/${encodeURIComponent(candidateId)}`);
                    await loadVoices();
                    showToast(`Removed candidate ${candidateId}.`, 'success');
                });
            } catch (e) { showActionError("Candidate removal failed", e, "Refresh the saved candidate pool to check whether removal completed before deleting again."); }
        };

        window.favoriteVoiceCandidate = async function favoriteVoiceCandidate(button, candidateId, favorite) {
            const speaker = button.closest('.voice-card')?.dataset.voice;
            try {
                await postVoiceTarget(button, `/api/voices/${encodeURIComponent(speaker)}/candidates/${encodeURIComponent(candidateId)}/favorite`, {favorite});
                await loadVoices();
            } catch (e) { showActionError("Candidate favorite update failed", e, "Refresh the saved candidate pool to check its favorite status before changing it again."); }
        };

        // --- Auto-suggest best LoRA voice per character ---
        window._voiceSuggestions = {};

        function getLoraModelsById() {
            const models = new Map();
            for (const model of window._loraModelsCache || []) {
                if (!models.has(model.id)) { models.set(model.id, model); }
            }
            return models;
        }

        async function suggestVoices(characterNames = null, initiatingButton = null) {
            if (!claimTaskStart('voices', initiatingButton)) { return; }
            const status = document.getElementById('suggest-status');
            const book = currentBookFilename;
            const cast = window._selectedCast || null;
            const generation = window._voiceSuggestionGeneration || 0;
            const context = {isCurrent: () => book === currentBookFilename
                && cast === (window._selectedCast || null)
                && generation === (window._voiceSuggestionGeneration || 0)};
            try {
                if (!(await confirmIfRemote('this voice suggestion', true))) { return; }
                if (!context.isCurrent()) { return; }
                const onlyUnset = characterNames ? false : voicesScopeIsNew();
                if (!characterNames && !onlyUnset && !(await keepCurrentVoicesIfAsked())) { return; }
                if (!context.isCurrent()) { return; }
                status.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Analyzing characters and matching voices...';
                // Make sure lora caches are fresh so we can resolve suggested adapters in the dropdowns
                const catalogStatus = document.getElementById('suggest-catalog-status');
                try {
                    const models = await API.get('/api/lora/models');
                    if (!Array.isArray(models)) { throw new Error('Invalid voice catalog response'); }
                    window._loraModelsCache = models;
                    catalogStatus.textContent = '';
                } catch (e) {
                    catalogStatus.textContent = 'The downloaded voice list could not be refreshed. Suggestions may have incomplete voice type and download details. Check the app connection and run Suggest LoRA Voices again.';
                    console.debug('lora-models cache refresh failed', e);
                }
                if (!context.isCurrent()) { return; }
                const res = await API.post('/api/suggest_voices', {
                    only_unset: onlyUnset,
                    cast,
                    characters: characterNames,
                });
                if (!context.isCurrent()) { return; }
                window._voiceSuggestionContext = context;
                window._voiceSuggestions = res.suggestions || {};
                const n = Object.keys(window._voiceSuggestions).length;
                // Preserve the full ranked pool produced by auto-suggest so users
                // can compare alternatives later without a second suggestion UI.
                const modelsById = getLoraModelsById();
                const candidates = [];
                for (const [name, suggestion] of Object.entries(window._voiceSuggestions)) {
                    const ranked = suggestion.ranked_adapter_ids || [suggestion.adapter_id];
                    ranked.filter(Boolean).forEach((adapterId, rank) => {
                        candidates.push({ name, suggestion, adapterId, rank });
                    });
                }
                let nextCandidate = 0;
                const failedCandidateSaves = [];
                const saveCandidates = async () => {
                    while (context.isCurrent() && nextCandidate < candidates.length) {
                        const { name, suggestion, adapterId, rank } = candidates[nextCandidate++];
                        const model = modelsById.get(adapterId) || {};
                        try {
                            await API.post(`/api/voices/${encodeURIComponent(name)}/candidates`, {
                                candidate_id: adapterId,
                                config: {
                                    type: model.builtin ? 'builtin_lora' : (suggestion.type || 'lora'),
                                    adapter_id: adapterId,
                                    adapter_path: model.path || model.adapter_path || null,
                                    description: model.description || '',
                                    age_group: model.age_group || suggestion.voice_age_group || 'unknown',
                                    gender: model.gender || suggestion.voice_gender || 'unknown',
                                    rank: rank + 1,
                                    source: 'auto_suggest',
                                },
                            });
                        } catch (e) {
                            failedCandidateSaves.push({ name, adapterId });
                            console.debug(`candidate save failed for ${name}/${adapterId}`, e);
                        }
                    }
                };
                await Promise.all(Array.from({ length: Math.min(4, candidates.length) }, saveCandidates));
                if (!context.isCurrent()) { return; }
                // Read the final pool after all writes; update only candidate UI
                // so edits made while suggestions ran remain in the existing cards.
                await refreshVoiceMetadata();
                if (!context.isCurrent()) { return; }
                document.querySelectorAll('.voice-card').forEach(card => {
                    const list = card.querySelector('.saved-voice-candidates');
                    if (list) {
                        list.innerHTML = getVoiceCandidateMarkup(getVoiceCardMetadata(card).candidates);
                    }
                });
                if (n === 0) {
                    status.textContent = res.message || 'No suggestions available.';
                    document.getElementById('btn-apply-all-suggestions').style.display = 'none';
                    document.getElementById('btn-clear-suggestions').style.display = 'none';
                } else {
                    const methodLabel = res.method === 'llm' ? 'LLM' : res.method === 'mixed' ? 'LLM + heuristic' : 'heuristic';
                    status.innerHTML = `<i class="fas fa-check text-success me-1"></i>Suggested ${n} voice${n > 1 ? 's' : ''} (${methodLabel}). Review and apply below.`;
                    document.getElementById('btn-apply-all-suggestions').style.display = 'inline-block';
                    document.getElementById('btn-clear-suggestions').style.display = 'inline-block';
                }
                if (res.llm_warning) {
                    status.innerHTML += `<div class="text-warning small mt-1"><i class="fas fa-exclamation-triangle me-1"></i>${escapeHtml(res.llm_warning)}</div>`;
                }
                if (failedCandidateSaves.length) {
                    const message = `${failedCandidateSaves.length} candidate(s) could not be saved. Check the saved pool before applying a voice.`;
                    status.innerHTML += `<div class="text-warning small mt-1">${escapeHtml(message)}</div>`;
                    showToast(message, 'warning');
                }
                renderVoiceSuggestions();
            } catch (e) {
                if (!context.isCurrent()) { return; }
                status.innerHTML = `<i class="fas fa-times text-danger me-1"></i>${escapeHtml(getActionErrorMessage('Voice suggestions failed', e, 'Use Setup → Test Connection and check the selected model. Review the saved candidate pool before requesting more suggestions.'))}`;
            } finally {
                releaseTaskStart('voices');
            }
        }

        window.suggestMoreVoices = async function suggestMoreVoices(button) {
            const speaker = button.closest('.voice-card')?.dataset.voice;
            const card = button.closest('.voice-card');
            if (!card?.dataset.version) { await suggestVoices(speaker ? [speaker] : null, button); return; }
            if (button.disabled) { return; }
            button.disabled = true;
            const token = _voiceCardsBookToken;
            try {
                await flushVoiceSaves();
                if (!(await confirmIfRemote('these state voice suggestions', true))) { return; }
                if (token !== _voiceSaveSnapshot?.book_token || card.isConnected === false) { return; }
                const result = await API.post('/api/suggest_voices', {characters: [speaker],
                    state_version: card.dataset.version, book_token: token});
                if (token !== _voiceSaveSnapshot?.book_token || card.isConnected === false) { return; }
                const suggestion = result.suggestions?.[speaker];
                const ranked = (suggestion?.ranked_adapter_ids || [suggestion?.adapter_id]).filter(Boolean);
                if (!ranked.length) {
                    showToast(result.message || 'No state voice candidates found.', 'warning');
                    return;
                }
                for (const adapterId of ranked) {
                    const adapter = getLoraModelsById().get(adapterId);
                    const type = adapter?.builtin ? 'builtin_lora' : (adapter?.type || suggestion.type || 'lora');
                    const config = {type, adapter_id: adapterId,
                        adapter_path: adapter?.adapter_path || adapter?.path || `${type === 'builtin_lora' ? 'builtin_lora' : 'lora_models'}/${adapterId}`,
                        character_style: suggestion.character_style || ''};
                    await postVoiceTarget(button, `/api/voices/${encodeURIComponent(speaker)}/candidates`,
                        {candidate_id: adapterId, config});
                }
                await loadVoices(false);
                showToast('State voice candidates saved. Review and select one on that state card.', 'success');
            } catch (error) { showActionError('State voice suggestions failed', error, 'Reload Voices and check this state’s candidate pool.'); }
            finally { button.disabled = false; }
        };

        function renderVoiceSuggestions() {
            const container = document.getElementById('voices-list');
            const focusSnapshot = getVoiceListFocusSnapshot(container, currentBookFilename);
            document.querySelectorAll('.voice-card').forEach(card => {
                if (card.dataset.version) { return; }
                const name = card.dataset.voice;
                const body = card.querySelector('.card-body');
                let banner = card.querySelector('.voice-suggestion');
                const sugg = window._voiceSuggestions[name];
                if (!sugg) {
                    if (banner) { banner.remove(); }
                    return;
                }
                if (!banner) {
                    banner = document.createElement('div');
                    banner.className = 'voice-suggestion alert alert-success d-flex align-items-center justify-content-between py-2 px-3 mt-2 mb-0';
                    body.appendChild(banner);
                }
                const typeLabel = sugg.type === 'builtin_lora' ? 'Built-in' : 'LoRA';
                const reuseText = sugg.reused
                    ? `Reused by ${sugg.reuse_count_before} existing cast character${sugg.reuse_count_before !== 1 ? 's' : ''}`
                    : 'Unused in this series cast';
                const traitText = `${(sugg.character_gender || 'unknown').replace('_', ' ')} · ${(sugg.character_age_group || 'unknown').replace('_', ' ')} → ${(sugg.voice_gender || 'unknown').replace('_', ' ')} · ${(sugg.voice_age_group || 'unknown').replace('_', ' ')}`;
                const traitWarning = sugg.gender_fallback
                    ? 'No downloaded voice matched the known gender; fallback used.'
                    : (sugg.existing_trait_mismatch ? 'Existing recurring voice retained despite a trait mismatch.' : '');
                banner.innerHTML = `
                    <div class="me-2 small">
                        <i class="fas fa-wand-magic-sparkles me-1"></i>
                        <span class="badge ${sugg.priority === 'major' ? 'bg-primary' : 'bg-secondary'} me-1">${escapeHtml((sugg.priority || 'minor').toUpperCase())}</span>
                        <strong>${sugg.line_count || 0} lines · Suggested ${typeLabel} voice:</strong> ${escapeHtml(sugg.adapter_name)}
                        <span class="d-block"><strong>Style:</strong> ${escapeHtml(sugg.character_style || '')}</span>
                        <span class="d-block"><strong>Trait match:</strong> ${escapeHtml(traitText)} <span class="text-muted">(gender ${escapeHtml(sugg.gender_confidence || 'unknown')}, age ${escapeHtml(sugg.age_confidence || 'unknown')})</span></span>
                        ${sugg.trait_evidence ? `<span class="text-muted d-block">${escapeHtml(sugg.trait_evidence)}</span>` : ''}
                        ${traitWarning ? `<span class="text-warning d-block">${escapeHtml(traitWarning)}</span>` : ''}
                        <span class="d-block ${sugg.reused ? 'text-warning' : 'text-success'}">${escapeHtml(reuseText)}${sugg.forced_reuse ? ' (candidate pool exhausted)' : ''}</span>
                        ${sugg.reason ? `<span class="text-muted d-block">${escapeHtml(sugg.reason)}</span>` : ''}
                    </div>
                    <button class="btn btn-sm btn-success flex-shrink-0" data-voice="${escapeHtml(name)}" onclick="applyVoiceSuggestion(this.dataset.voice)"><i class="fas fa-check me-1"></i>Apply</button>
                `;
            });
            Array.from(new Set(Array.from(container.querySelectorAll('.voice-card'))
                .map(card => card.closest?.('.voice-character-group') || card)))
                .sort((a, b) => (window._voiceSuggestions[b.dataset.voice]?.line_count || window._lineCounts[b.dataset.voice] || 0)
                               - (window._voiceSuggestions[a.dataset.voice]?.line_count || window._lineCounts[a.dataset.voice] || 0))
                .forEach(card => container.appendChild(card));
            restoreVoiceListFocus(container, focusSnapshot, currentBookFilename);
        }

        function applySuggestionToCard(name, sugg = window._voiceSuggestions[name]) {
            if (!sugg) { return false; }
            const card = document.querySelector(`.voice-card[data-voice="${CSS.escape(name)}"]`);
            if (!card) { return false; }
            const body = card.querySelector('.card-body');

            // Select the correct voice type radio and reveal its options
            const radio = body.querySelector(`.voice-type[value="${sugg.type}"]`);
            if (radio) {
                radio.checked = true;
                toggleVoiceType(radio);
            }

            // Set the suggested adapter in the matching dropdown
            const selectClass = sugg.type === 'builtin_lora' ? '.builtin-lora-select' : '.lora-adapter-select';
            const select = body.querySelector(selectClass);
            if (select) {
                select.value = sugg.adapter_id;
                // If the option isn't present (cache mismatch), add it so the value sticks
                if (select.value !== sugg.adapter_id) {
                    const opt = new Option(sugg.adapter_name, sugg.adapter_id, true, true);
                    select.add(opt);
                    select.value = sugg.adapter_id;
                }
            }

            const styleInput = body.querySelector(sugg.type === 'builtin_lora'
                ? '.builtin-lora-style' : '.lora-character-style');
            if (styleInput) { styleInput.value = sugg.character_style || ''; }

            // Remove the banner and clear from pending suggestions
            const banner = card.querySelector('.voice-suggestion');
            if (banner) { banner.remove(); }
            delete window._voiceSuggestions[name];
            updateSuggestionToolbar();
            return true;
        }

        function getSuggestionApplySnapshot(name, suggestion) {
            const card = document.querySelector(`.voice-card[data-voice="${CSS.escape(name)}"]`);
            const getForm = () => card ? JSON.stringify(Array.from(card.querySelectorAll('input, select, textarea'),
                field => [field.value, field.checked])) : null;
            const form = getForm();
            const book = currentBookFilename;
            const cast = window._selectedCast || null;
            const original = suggestion;
            const submitted = JSON.parse(JSON.stringify(suggestion));
            return {submitted, reconcile: () => {
                if (book !== currentBookFilename || cast !== (window._selectedCast || null)
                    || document.querySelector(`.voice-card[data-voice="${CSS.escape(name)}"]`) !== card) { return; }
                const pending = window._voiceSuggestions[name];
                if (getForm() !== form || (pending && (pending !== original || JSON.stringify(pending) !== JSON.stringify(submitted)))) {
                    showToast('Earlier voice suggestion applied on the server. Your newer edits and suggestions are kept; review before saving.', 'warning');
                    return;
                }
                applySuggestionToCard(name, submitted);
            }};
        }

        async function applyVoiceSuggestion(name) {
            if (window._voiceSuggestionContext && !window._voiceSuggestionContext.isCurrent()) {
                showToast('The cast or book changed. Request current voice suggestions before applying.', 'warning');
                return;
            }
            const sugg = window._voiceSuggestions[name];
            if (!sugg) { return; }
            const snapshot = getSuggestionApplySnapshot(name, sugg);
            try {
                await API.post('/api/suggest_voices/apply', {
                    character: name, cast: window._selectedCast || null, suggestion: snapshot.submitted,
                });
                snapshot.reconcile();
                await loadCastLibrary();
            } catch (e) {
                showActionError("Failed to apply suggestion", e, "Check the current character voice and cast library before applying the suggestion again.");
            }
        }

        async function applyAllVoiceSuggestions() {
            if (window._voiceSuggestionContext && !window._voiceSuggestionContext.isCurrent()) {
                showToast('The cast or book changed. Request current voice suggestions before applying.', 'warning');
                return;
            }
            const snapshots = Object.fromEntries(Object.entries(window._voiceSuggestions)
                .map(([name, suggestion]) => [name, getSuggestionApplySnapshot(name, suggestion)]));
            const pending = Object.fromEntries(Object.entries(snapshots).map(([name, snapshot]) => [name, snapshot.submitted]));
            if (!Object.keys(pending).length) { return; }
            try {
                await API.post('/api/suggest_voices/apply_bulk', {
                    cast: window._selectedCast || null, suggestions: pending,
                });
                Object.values(snapshots).forEach(snapshot => snapshot.reconcile());
                await loadCastLibrary();
            } catch (e) {
                showActionError("Failed to apply suggestions", e, "Check the current character voices and cast library before applying the suggestions again.");
            }
        }

        function clearVoiceSuggestions() {
            window._voiceSuggestionGeneration = (window._voiceSuggestionGeneration || 0) + 1;
            window._voiceSuggestionContext = null;
            window._voiceSuggestions = {};
            document.querySelectorAll('.voice-suggestion').forEach(b => b.remove());
            updateSuggestionToolbar();
            document.getElementById('suggest-status').textContent = '';
        }

        function updateSuggestionToolbar() {
            const remaining = Object.keys(window._voiceSuggestions).length;
            const show = remaining > 0 ? 'inline-block' : 'none';
            document.getElementById('btn-apply-all-suggestions').style.display = show;
            document.getElementById('btn-clear-suggestions').style.display = show;
            if (remaining === 0) {
                document.getElementById('suggest-status').innerHTML = '<i class="fas fa-check text-success me-1"></i>All suggestions applied.';
            }
        }

        // --- Series Cast: reuse character voices across books ---
        window._voiceLibrary = { casts: [], shared: [], current_characters: [] };
        window._lineCounts = {};
        window._selectedCast = window._selectedCast || '';
        let castBulkScripts = [];   // saved-script list for the "apply to multiple books" picker

        function setCastStatus(html, isError) {
            const el = document.getElementById('cast-status');
            if (el) { el.innerHTML = isError ? `<i class="fas fa-times text-danger me-1"></i>${html}` : html; }
        }

        async function loadCastLibrary() {
            const request = (window._castLibraryRequest || 0) + 1;
            window._castLibraryRequest = request;
            const book = currentBookFilename;
            const isCurrent = () => request === window._castLibraryRequest && book === currentBookFilename;
            let lib;
            try { lib = await API.get('/api/voice_library'); }
            catch (error) { if (!isCurrent()) { return; } throw error; }
            if (!isCurrent()) { return; }
            window._voiceLibrary = lib;
            window._lineCounts = {};
            (lib.current_characters || []).forEach(c => { window._lineCounts[c.name] = c.line_count; });

            // Populate the cast selector, preserving the current selection if still valid
            const sel = document.getElementById('cast-select');
            const castNames = (lib.casts || []).map(c => c.name);
            if (!castNames.includes(window._selectedCast)) {
                const nextCast = castNames[0] || '';
                if (nextCast !== window._selectedCast) { clearVoiceSuggestions(); }
                window._selectedCast = nextCast;
            }
            sel.innerHTML = castNames.length
                ? castNames.map(n => `<option value="${escapeHtml(n)}" ${n === window._selectedCast ? 'selected' : ''}>${escapeHtml(n)}</option>`).join('')
                : '<option value="">(no casts yet)</option>';

            const hasCast = !!window._selectedCast;
            document.getElementById('btn-cast-save').disabled = !hasCast;
            document.getElementById('btn-cast-apply').disabled = !hasCast;
            document.getElementById('btn-cast-apply-bulk').disabled = !hasCast;
            document.getElementById('btn-cast-delete').disabled = !hasCast;
            renderCastMembers();
        }

        function getSelectedCastObj() {
            return (window._voiceLibrary.casts || []).find(c => c.name === window._selectedCast) || null;
        }

        function onCastChange() {
            window._castApplyContext = null;
            window._selectedCast = document.getElementById('cast-select').value;
            clearVoiceSuggestions();
            renderCastMembers();
        }

        function renderCastMembers() {
            const panel = document.getElementById('cast-panel');
            const cast = getSelectedCastObj();
            const shared = window._voiceLibrary.shared || [];
            if (!window._selectedCast) {
                panel.innerHTML = '<div class="alert alert-light border small mb-0">No casts yet. Click <strong>New</strong> to create one for this series, then save your configured character voices to it.</div>';
                return;
            }
            const memberRow = (m, castName) => `
                <li class="list-group-item d-flex justify-content-between align-items-center py-1 px-2">
                    <span class="small">${escapeHtml(m.name)}${m.generic && m.book_id ? ` — ${escapeHtml(m.book_id)}` : ''}
                        <span class="text-muted">(${escapeHtml(m.type || 'custom')}${m.line_count ? ', ' + m.line_count + ' lines' : ''})</span>
                        ${m.character_style ? `<span class="d-block text-muted">${escapeHtml(m.character_style)}</span>` : ''}
                        ${_castKnownAsLine(m)}</span>
                    <button class="btn btn-sm btn-link text-danger p-0" title="Remove from library" data-cast="${escapeHtml(castName)}" data-key="${escapeHtml(m.key)}" onclick="deleteCastMember(this.dataset.cast, this.dataset.key, this)"><i class="fas fa-times"></i></button>
                </li>`;
            const members = (cast && cast.members) || [];
            const usageRows = Object.entries((cast && cast.adapter_usage) || {}).sort((a, b) => b[1].character_count - a[1].character_count);
            panel.innerHTML = `
                <div class="row g-3">
                    <div class="col-md-6">
                        <div class="small fw-bold mb-1">Cast members <span class="text-muted">(${members.length})</span></div>
                        <ul class="list-group list-group-flush border rounded">${members.length ? members.map(m => memberRow(m, window._selectedCast)).join('') : '<li class="list-group-item small text-muted py-2 px-2">None yet — use "Save to cast".</li>'}</ul>
                    </div>
                    <div class="col-md-6">
                        <div class="small fw-bold mb-1">Shared across series <span class="text-muted">(${shared.length})</span></div>
                        <ul class="list-group list-group-flush border rounded">${shared.length ? shared.map(m => memberRow(m, '__shared__')).join('') : '<li class="list-group-item small text-muted py-2 px-2">None yet — the narrator lands here automatically.</li>'}</ul>
                    </div>
                </div>`;
            if (usageRows.length) {
                panel.innerHTML += `<div class="small fw-bold mt-2">LoRA reuse in this series</div>
                    <ul class="list-group list-group-flush border rounded">${usageRows.map(([id, u]) =>
                        `<li class="list-group-item py-1 px-2 small"><strong>${escapeHtml(id)}</strong> — ${u.character_count} character${u.character_count !== 1 ? 's' : ''}, ${u.total_lines} lines<br><span class="text-muted">${escapeHtml((u.characters || []).join(', '))}</span></li>`
                    ).join('')}</ul>`;
            }
        }

        async function createCast() {
            const selected = window._selectedCast;
            const book = currentBookFilename;
            const values = await showPresetEditor({title: 'Create cast', nameLabel: 'Cast name', includeDescription: false,
                actionLabel: 'Create cast', helperText: 'Name this cast, for example with the series title.'});
            if (!values) { return; }
            if (selected !== window._selectedCast || book !== currentBookFilename) {
                setCastStatus('The book or selected cast changed. Review it before creating a cast.', true); return;
            }
            const name = values.name;
            try {
                await API.post('/api/voice_library/casts', { name });
                if (selected !== window._selectedCast || book !== currentBookFilename) {
                    showToast(`Cast "${name}" was created. Your later selection was kept. Refresh the cast library to see it.`, 'success'); return;
                }
                window._selectedCast = name;
                await loadCastLibrary();
                setCastStatus(`<i class="fas fa-check text-success me-1"></i>Created cast "${escapeHtml(name)}"`);
            } catch (e) { setCastStatus(escapeHtml(getActionErrorMessage("Cast creation failed", e, "Refresh the cast library and check whether the named cast was created before creating it again.")), true); }
        }

        async function deleteCast() {
            const cast = window._selectedCast;
            if (!cast) { return; }
            const button = document.getElementById('btn-cast-delete');
            try {
                await applyConfirmedVoiceRemoval(button, `Delete cast "${cast}"? Cast-only members will be removed; shared characters will be kept.`, async () => {
                    if (window._selectedCast !== cast) {
                        setCastStatus('The selected cast changed. Review it before deleting.', true);
                        return;
                    }
                    await API.del(`/api/voice_library/casts/${encodeURIComponent(cast)}`);
                    if (window._selectedCast === cast) { window._selectedCast = ''; }
                    await loadCastLibrary();
                    setCastStatus('<i class="fas fa-check text-success me-1"></i>Cast deleted');
                });
            } catch (e) { setCastStatus(escapeHtml(getActionErrorMessage("Cast deletion failed", e, "Refresh the cast library and check whether the cast was deleted before deleting again.")), true); }
        }

        async function deleteCastMember(cast, key, button = null) {
            try {
                await applyConfirmedVoiceRemoval(button, `Remove "${key}" from cast "${cast}"?`, async () => {
                    await API.del(`/api/voice_library/casts/${encodeURIComponent(cast)}/members/${encodeURIComponent(key)}`);
                    await loadCastLibrary();
                });
            } catch (e) { setCastStatus(escapeHtml(getActionErrorMessage("Cast member removal failed", e, "Refresh the selected cast and check whether the member was removed before trying again.")), true); }
        }

        // Save current-book characters into the selected cast
        function openCastSave() {
            if (!window._selectedCast) { return; }
            const panel = document.getElementById('cast-panel');
            const chars = window._voiceLibrary.current_characters || [];
            if (!chars.length) {
                panel.innerHTML = '<div class="alert alert-warning small mb-0">No characters in the current book. Generate a script first.</div>';
                return;
            }
            const defaultThreshold = 25;
            const row = c => {
                const isNarrator = c.name.trim().toLowerCase() === 'narrator';
                const checked = isNarrator || c.line_count >= defaultThreshold ? 'checked' : '';
                const scopeCell = isNarrator
                    ? `<select class="form-select form-select-sm cast-save-scope" aria-label="${escapeHtml('Narrator scope for ' + c.name)}" data-name="${escapeHtml(c.name)}" style="width:auto;">
                           <option value="shared" selected>shared (whole series)</option>
                           <option value="cast">this cast only (different narrator)</option>
                       </select>`
                    : '';
                return `<tr>
                    <td><input type="checkbox" class="cast-save-check" aria-label="${escapeHtml('Save voice for ' + c.name + ' to cast')}" data-name="${escapeHtml(c.name)}" ${checked}></td>
                    <td class="small">${escapeHtml(c.name)}</td>
                    <td class="small text-muted">${c.line_count}</td>
                    <td>${scopeCell}</td>
                </tr>`;
            };
            panel.innerHTML = `
                <div class="border rounded p-2">
                    <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
                        <span class="small fw-bold">Save to "${escapeHtml(window._selectedCast)}"</span>
                        <span class="small text-muted">Pre-select characters with ≥</span>
                        <input type="number" id="cast-save-threshold" aria-label="Minimum lines for pre-selecting cast voices" class="form-control form-control-sm" style="width:70px;" value="${defaultThreshold}" min="0" onchange="reapplyCastSaveThreshold()">
                        <span class="small text-muted">lines</span>
                    </div>
                    <div class="alert alert-light border py-1 px-2 small mb-2"><i class="fas fa-info-circle me-1"></i>Only characters you've already configured will be saved. The narrator is stored as a shared voice for the whole series.</div>
                    <div class="table-responsive" style="max-height:260px;overflow-y:auto;">
                        <table class="table table-sm table-hover mb-0">
                            <thead class="table-light"><tr><th style="width:30px;"></th><th>Character</th><th>Lines</th><th>Narrator scope</th></tr></thead>
                            <tbody>${chars.map(row).join('')}</tbody>
                        </table>
                    </div>
                    <div class="d-flex gap-2 mt-2">
                        <button class="btn btn-sm btn-success" onclick="submitCastSave()"><i class="fas fa-floppy-disk me-1"></i>Save selected</button>
                        <button class="btn btn-sm btn-outline-secondary" onclick="renderCastMembers()">Cancel</button>
                    </div>
                </div>`;
        }

        function reapplyCastSaveThreshold() {
            const t = parseInt(document.getElementById('cast-save-threshold').value, 10) || 0;
            document.querySelectorAll('.cast-save-check').forEach(cb => {
                const name = cb.dataset.name;
                const isNarrator = name.trim().toLowerCase() === 'narrator';
                const count = window._lineCounts[name] || 0;
                cb.checked = isNarrator || count >= t;
            });
        }

        async function submitCastSave() {
            const checks = Array.from(document.querySelectorAll('.cast-save-check:checked'));
            const characters = checks.map(cb => cb.dataset.name);
            if (!characters.length) { setCastStatus('Select at least one character.', true); return; }
            // Narrators marked "this cast only" are saved as a series-specific (different) narrator
            const cast_specific = Array.from(document.querySelectorAll('.cast-save-scope'))
                .filter(s => s.value === 'cast' && characters.includes(s.dataset.name))
                .map(s => s.dataset.name);
            try {
                const res = await API.post('/api/voice_library/save', { cast: window._selectedCast, characters, cast_specific });
                await loadCastLibrary();
                const nc = res.saved.cast.length, ns = res.saved.shared.length;
                const skipped = characters.length - nc - ns;
                let msg = `Saved ${nc} to cast`;
                if (ns) { msg += `, ${ns} shared`; }
                if (skipped > 0) { msg += ` (${skipped} skipped — not configured)`; }
                setCastStatus(`<i class="fas fa-check text-success me-1"></i>${msg}`);
            } catch (e) { setCastStatus(escapeHtml(getActionErrorMessage("Saving voices to the cast failed", e, "Refresh the cast library and check the saved voices before saving them again.")), true); }
        }

        // Build the candidate-member pool for the current cast (shared + cast-specific)
        function _getCastMatchPool() {
            const cast = getSelectedCastObj();
            return [...(window._voiceLibrary.shared || []).map(m => ({ ...m, source: 'shared' })),
                    ...((cast && cast.members) || []).map(m => ({ ...m, source: 'cast' }))];
        }

        // Render the <tr> rows for a cast-match proposals table (shared by the
        // single-book and bulk apply flows).
        // Labels a member has answered to in other books, besides its own name.
        function _castKnownAsLine(m) {
            const others = (m.known_as || []).filter(label => label.toLowerCase() !== (m.name || '').toLowerCase());
            if (!others.length) { return ''; }
            return `<span class="d-block text-muted fst-italic">known as ${escapeHtml(others.join(', '))}</span>`;
        }

        function _castMatchBadge(m) {
            if (!m) { return '<span class="badge bg-light text-muted border">no match</span>'; }
            const why = m.via === 'known_as' ? ' title="Matched a label this member was known as in another book"'
                : (m.via === 'alias' ? ' title="Matched through the character alias registry"' : '');
            const viaText = m.via === 'known_as' ? ' known as' : (m.via === 'alias' ? ' alias' : '');
            if (m.exact) { return `<span class="badge bg-success"${why}>exact${viaText}</span>`; }
            return `<span class="badge bg-warning text-dark" title="Fuzzy match — please confirm">~${m.score}${viaText}</span>`;
        }

        function _renderCastMatchRows(proposals, pool) {
            const optionsFor = (selKey) => '<option value="">— skip —</option>' + pool.map(m =>
                `<option value="${escapeHtml(m.key)}" ${m.key === selKey ? 'selected' : ''}>${escapeHtml(m.name)} (${m.source})</option>`).join('');
            return proposals.map(p => {
                const m = p.match;
                const fuzzy = m && !m.exact;
                const badge = _castMatchBadge(m);
                return `<tr class="${fuzzy ? 'table-warning' : ''}">
                    <td><input type="checkbox" class="cast-apply-check" aria-label="${escapeHtml('Apply cast voice to ' + p.character)}" data-char="${escapeHtml(p.character)}" ${m ? 'checked' : ''}></td>
                    <td class="small">${escapeHtml(p.character)} <span class="text-muted">(${p.line_count})</span></td>
                    <td>${badge}</td>
                    <td><select class="form-select form-select-sm cast-apply-target" aria-label="${escapeHtml('Cast voice for ' + p.character)}" data-char="${escapeHtml(p.character)}">${optionsFor(m ? m.key : '')}</select></td>
                </tr>`;
            }).join('');
        }

        // Apply a cast to the current book (fuzzy match + confirm)
        async function openCastApply() {
            if (!window._selectedCast) { return; }
            const context = {cast: window._selectedCast, book: currentBookFilename,
                bookToken: _voiceSaveSnapshot?.book_token};
            window._castApplyContext = context;
            const panel = document.getElementById('cast-panel');
            panel.innerHTML = '<div class="small text-muted"><i class="fas fa-spinner fa-spin me-1"></i>Matching characters...</div>';
            let res;
            try {
                res = await API.post('/api/voice_library/match', { name: context.cast });
            } catch (e) { if (!isCastApplyContextCurrent(context)) { return; } setCastStatus(escapeHtml(getActionErrorMessage("Matching cast voices failed", e, "Check the selected cast and current book, then reopen Apply cast.")), true); renderCastMembers(); return; }

            if (!isCastApplyContextCurrent(context)) { return; }
            const pool = _getCastMatchPool();
            const anyMatch = res.proposals.some(p => p.match);
            const rows = _renderCastMatchRows(res.proposals, pool);

            panel.innerHTML = `
                <div class="border rounded p-2">
                    <div class="small fw-bold mb-1">Apply "${escapeHtml(context.cast)}" to this book</div>
                    <div class="alert alert-light border py-1 px-2 small mb-2"><i class="fas fa-info-circle me-1"></i>Review matches before applying. <span class="badge bg-warning text-dark">~score</span> rows are fuzzy guesses — confirm or change the target. Applying overwrites those characters' current voices.</div>
                    ${anyMatch ? '' : '<div class="alert alert-warning small py-1 px-2">No matches found for this cast.</div>'}
                    <div class="table-responsive" style="max-height:300px;overflow-y:auto;">
                        <table class="table table-sm table-hover mb-0">
                            <thead class="table-light"><tr><th style="width:30px;"></th><th>Character (lines)</th><th>Match</th><th>Use voice</th></tr></thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>
                    <div class="d-flex gap-2 mt-2">
                        <button class="btn btn-sm btn-success" onclick="submitCastApply()"><i class="fas fa-check me-1"></i>Apply selected</button>
                        <button class="btn btn-sm btn-outline-secondary" onclick="renderCastMembers()">Cancel</button>
                    </div>
                </div>`;
        }

        function isCastApplyContextCurrent(context) {
            return !!context && window._castApplyContext === context
                && context.cast === window._selectedCast && context.book === currentBookFilename
                && context.bookToken === _voiceSaveSnapshot?.book_token;
        }

        // Shared by submitCastApply/submitCastApplyBulk - both build a
        // mapping from the same checked-rows/target-select DOM shape, then
        // diverge only in which endpoint they POST to.
        function _collectCastApplyMapping() {
            const mapping = {};
            document.querySelectorAll('.cast-apply-check:checked').forEach(cb => {
                const char = cb.dataset.char;
                const sel = document.querySelector(`.cast-apply-target[data-char="${CSS.escape(char)}"]`);
                if (sel && sel.value) { mapping[char] = sel.value; }
            });
            return mapping;
        }

        function getCastApplyWarningsHtml(warnings) {
            return (warnings || []).map(warning =>
                `<div class="alert alert-warning py-1 px-2 small mt-1 mb-0">${escapeHtml(warning)}</div>`).join('');
        }

        async function submitCastApply() {
            const context = window._castApplyContext;
            if (!isCastApplyContextCurrent(context)) {
                setCastStatus('The cast or book changed. Reopen Apply cast before submitting these matches.', true);
                return;
            }
            const mapping = _collectCastApplyMapping();
            if (!Object.keys(mapping).length) { setCastStatus('Nothing selected to apply.', true); return; }
            try {
                const res = await API.post('/api/voice_library/apply', { cast: context.cast, mapping });
                if (!isCastApplyContextCurrent(context)) { return; }
                setCastStatus(`<i class="fas fa-check text-success me-1"></i>Applied ${res.count} voice${res.count !== 1 ? 's' : ''}${getCastApplyWarningsHtml(res.warnings)}`);
                await loadVoices();  // re-render cards with the applied configs
            } catch (e) { if (!isCastApplyContextCurrent(context)) { return; } setCastStatus(escapeHtml(getActionErrorMessage("Applying cast voices failed", e, "Reload Voices and review the current book assignments before applying again.")), true); }
        }

        // --- Apply a cast to multiple saved books at once ---

        // Render a saved-scripts checkbox picker into #cast-panel
        async function openCastApplyBulk() {
            if (!window._selectedCast) { return; }
            const panel = document.getElementById('cast-panel');
            panel.innerHTML = `
                <div class="border rounded p-2">
                    <div class="small fw-bold mb-2">Apply "${escapeHtml(window._selectedCast)}" to multiple books</div>
                    <div class="d-flex align-items-center gap-2 mb-2 flex-wrap">
                        <button class="btn btn-sm btn-outline-secondary" type="button" onclick="castBulkSelectAll(true)">Select all</button>
                        <button class="btn btn-sm btn-outline-secondary" type="button" onclick="castBulkSelectAll(false)">Clear</button>
                        <button class="btn btn-sm btn-outline-primary" type="button" onclick="loadCastBulkScripts()"><i class="fas fa-sync me-1"></i>Refresh</button>
                        <div class="btn-group btn-group-sm" role="group" aria-label="Sort scripts">
                            <button class="btn btn-outline-secondary" type="button" onclick="castBulkSort('az')" title="Sort by name A→Z">A→Z</button>
                            <button class="btn btn-outline-secondary" type="button" onclick="castBulkSort('za')" title="Sort by name Z→A">Z→A</button>
                            <button class="btn btn-outline-secondary" type="button" onclick="castBulkSort('num-asc')" title="Sort by volume number 1→10">1→10</button>
                            <button class="btn btn-outline-secondary" type="button" onclick="castBulkSort('num-desc')" title="Sort by volume number 10→1">10→1</button>
                        </div>
                        <button class="btn btn-sm btn-outline-secondary" type="button" onclick="castBulkSort('reverse')" title="Reverse current order"><i class="fas fa-exchange-alt me-1"></i>Reverse</button>
                    </div>
                    <div id="cast-bulk-list" class="border rounded p-2 mb-2" style="max-height:220px;overflow-y:auto;">
                        <span class="text-muted small">Loading…</span>
                    </div>
                    <div class="d-flex gap-2 mt-2">
                        <button class="btn btn-sm btn-success" onclick="openCastApplyBulkMatch()"><i class="fas fa-arrow-right me-1"></i>Continue</button>
                        <button class="btn btn-sm btn-outline-secondary" onclick="renderCastMembers()">Cancel</button>
                    </div>
                </div>`;
            await loadCastBulkScripts();
        }

        async function loadCastBulkScripts() {
            await _loadScriptList('cast-bulk-list', (scripts) => {
                castBulkScripts = scripts;
                renderCastBulkList();
            });
        }

        function renderCastBulkList() {
            _renderScriptCheckboxList(castBulkScripts, {
                containerId: 'cast-bulk-list',
                checkClass: 'cast-bulk-check',
                idPrefix: 'cb-check-',
            });
        }

        window.castBulkSort = (mode) => {
            _sortScriptList(castBulkScripts, mode);
            renderCastBulkList();
        };

        window.castBulkSelectAll = (on) => _selectAllCheckboxes('cast-bulk-check', on);

        // Fuzzy-match the union of characters across the selected books, then
        // render the same review table as openCastApply but for all of them at once.
        async function openCastApplyBulkMatch() {
            const script_names = Array.from(document.querySelectorAll('.cast-bulk-check:checked')).map(cb => cb.dataset.name);
            if (!script_names.length) { setCastStatus('Select at least one book.', true); return; }
            const panel = document.getElementById('cast-panel');
            panel.innerHTML = '<div class="small text-muted"><i class="fas fa-spinner fa-spin me-1"></i>Matching characters...</div>';
            let res;
            try {
                res = await API.post('/api/voice_library/match_bulk', { name: window._selectedCast, script_names });
            } catch (e) { setCastStatus(escapeHtml(getActionErrorMessage("Matching cast voices to saved books failed", e, "Check the selected cast and saved books, then reopen the matching step.")), true); renderCastMembers(); return; }

            const pool = _getCastMatchPool();
            const anyMatch = res.proposals.some(p => p.match);
            const rows = _renderCastMatchRows(res.proposals, pool);

            panel.innerHTML = `
                <div class="border rounded p-2">
                    <div class="small fw-bold mb-1">Apply "${escapeHtml(window._selectedCast)}" to ${res.book_count} selected book${res.book_count !== 1 ? 's' : ''}</div>
                    <div class="alert alert-light border py-1 px-2 small mb-2"><i class="fas fa-info-circle me-1"></i>Review matches before applying. <span class="badge bg-warning text-dark">~score</span> rows are fuzzy guesses — confirm or change the target. Each book only receives entries for characters that appear in it; applying overwrites those characters' current voices in that book's saved config.</div>
                    ${anyMatch ? '' : '<div class="alert alert-warning small py-1 px-2">No matches found for this cast.</div>'}
                    <div class="table-responsive" style="max-height:300px;overflow-y:auto;">
                        <table class="table table-sm table-hover mb-0">
                            <thead class="table-light"><tr><th style="width:30px;"></th><th>Character (total lines)</th><th>Match</th><th>Use voice</th></tr></thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>
                    <div class="d-flex gap-2 mt-2">
                        <button class="btn btn-sm btn-success" id="btn-cast-apply-bulk-submit"><i class="fas fa-check me-1"></i>Apply selected</button>
                        <button class="btn btn-sm btn-outline-secondary" onclick="renderCastMembers()">Cancel</button>
                    </div>
                </div>`;
            document.getElementById('btn-cast-apply-bulk-submit').onclick = () => submitCastApplyBulk(script_names);
        }

        let castApplyBulkPending = false;
        async function submitCastApplyBulk(script_names) {
            if (castApplyBulkPending) { return; }
            const mapping = _collectCastApplyMapping();
            if (!Object.keys(mapping).length) { setCastStatus('Nothing selected to apply.', true); return; }
            const panel = document.getElementById('cast-panel');
            const cast = window._selectedCast;
            const button = document.getElementById('btn-cast-apply-bulk-submit');
            const wasDisabled = button?.disabled;
            castApplyBulkPending = true;
            if (button) { button.disabled = true; }
            try {
                const res = await API.post('/api/voice_library/apply_bulk', { cast, mapping, script_names: [...script_names] });
                const successful = res.results.filter(row => !row.error);
                const failed = res.results.length - successful.length;
                const summary = successful.length
                    ? `Applied to ${successful.length} book${successful.length !== 1 ? 's' : ''}${failed ? `; ${failed} failed` : ''}`
                    : `No books updated${failed ? `; ${failed} failed` : ''}`;
                const total = successful.reduce((sum, r) => sum + r.count, 0);
                const rows = res.results.map(r => `
                    <li class="list-group-item d-flex justify-content-between align-items-center py-1 px-2 small">
                        <div>${escapeHtml(r.name)}${getCastApplyWarningsHtml(r.warnings)}</div>
                        <span class="${r.error ? 'text-danger' : 'text-muted'}">${r.error ? escapeHtml(r.error) : `${r.count} applied`}</span>
                    </li>`).join('');
                panel.innerHTML = `
                    <div class="border rounded p-2">
                        <div class="small fw-bold mb-1">${escapeHtml(summary)} — cast "${escapeHtml(cast)}"</div>
                        <div class="alert alert-light border py-1 px-2 small mb-2"><i class="fas fa-info-circle me-1"></i>${total} voice${total !== 1 ? 's' : ''} applied in total, across the selected saved books. Your currently loaded book's voice settings and audio are unchanged. To use a saved book's updated voices, load it from Saved Scripts; loading replaces the current script and chunks.</div>
                        <ul class="list-group list-group-flush border rounded mb-2">${rows || '<li class="list-group-item small text-muted py-2 px-2">No books updated.</li>'}</ul>
                        <button class="btn btn-sm btn-outline-secondary" onclick="renderCastMembers()">Done</button>
                    </div>`;
                setCastStatus(`${failed || !successful.length ? '' : '<i class="fas fa-check text-success me-1"></i>'}${escapeHtml(summary)}`,
                    failed > 0 || successful.length === 0);
            } catch (e) { setCastStatus(escapeHtml(getActionErrorMessage("Applying cast to saved books failed", e, "Review the selected saved books and their voice assignments before applying again; some books may already have been updated.")), true); } finally {
                castApplyBulkPending = false;
                if (button) { button.disabled = wasDisabled; }
            }
        }

        // Identity anchors that take over from a line onward (#603): shown under
        // the character's style, added from the Editor ("Voice changes here").
        function renderStyleTimeline(name, config) {
            const points = (config && config.style_timeline) || [];
            if (!points.length) { return ''; }
            return `<div class="small text-muted mt-1">Changes:` + points.map(p =>
                ` <span class="badge bg-light text-dark border">from line ${p.from_index + 1}: ${escapeHtml(p.character_style)}` +
                ` <a href="#" title="Remove" onclick="removeStylePoint(${getInlineStringArgument(name)}, ${p.from_index}, this); return false;">&times;</a></span>`).join('') + `</div>`;
        }

        async function removeStylePoint(name, fromIndex, button = null) {
            try {
                await applyConfirmedVoiceRemoval(button, `Remove the voice-style change for ${name} from line ${fromIndex + 1}? Existing rendered audio is not changed.`, async () => {
                    await API.del(`/api/voices/${encodeURIComponent(name)}/style_timeline/${fromIndex}`);
                    await loadVoices();
                });
            } catch (e) {
                showActionError("Could not remove", e, "Reload Voices to check whether the voice-style change was removed before trying again.");
            }
        }

        // Voices tab: a character whose settled age/gender changes (#653) gets
        // one voice per state. Library voices nobody uses come first, then
        // ones other characters use, then generating an age version. Nothing
        // changes audio until Apply.
        function getVoiceStateDefault(state, applied, index) {
            const point = (applied || []).find(p => p.from_index === state.from_index);
            if (point) { return point.version_id ? `version:${point.version_id}` : 'main'; }
            const sources = state.sources || {};
            if (state.state_version && (sources.versions || []).some(version => version.version_id === state.state_version)) {
                return `version:${state.state_version}`;
            }
            if (index === 0) { return 'main'; }
            if ((sources.versions || []).length) { return `version:${sources.versions[0].version_id}`; }
            if ((sources.library_unused || []).length) { return `library:${sources.library_unused[0].adapter_id}`; }
            if ((sources.library_used || []).length) { return `library:${sources.library_used[0].adapter_id}`; }
            return 'main';
        }

        function renderVoiceStateRows(data, speaker = 'this character') {
            const states = (data && data.states) || [];
            if (!states.length) { return '<div class="small text-muted">No detected change in age or gender for this character.</div>'; }
            const option = (value, label, selected) => `<option value="${escapeHtml(value)}" ${value === selected ? 'selected' : ''}>${escapeHtml(label)}</option>`;
            const rows = states.map((state, index) => {
                const sources = state.sources || {};
                const selected = getVoiceStateDefault(state, data.applied, index);
                const where = state.from_index === null || state.from_index === undefined
                    ? 'line not found in the Editor yet' : `from line ${state.from_index + 1}`;
                const library = (c, suffix) => option(`library:${c.adapter_id}`, `${c.name} · ${c.gender} · ${c.age_group}${suffix}`, selected);
                const options = [option('main', 'Main voice', selected),
                    ...(sources.versions || []).map(v => option(`version:${v.version_id}`, `Version: ${v.version_id}`, selected)),
                    // the applied version, even when it no longer ranks as a match
                    ...(selected.startsWith('version:') && !(sources.versions || []).some(v => `version:${v.version_id}` === selected)
                        ? [option(selected, `Version: ${selected.slice(8)}`, selected)] : []),
                    ...(sources.library_unused || []).map(c => library(c, ' · unused')),
                    ...(sources.library_used || []).map(c => library(c, ` · used by ${(c.used_by || []).join(', ')}`))]
                    .join('');
                const generate = sources.offer_generate
                    ? `<button class="btn btn-sm btn-link p-0" type="button" data-age="${escapeHtml(state.age_group)}" onclick="generateAgeVersion(this, this.dataset.age)">Generate ${escapeHtml(state.age_group.replace(/_/g, ' '))} version</button>` : '';
                return `<div class="voice-state-row small mt-1" data-from-index="${state.from_index === null || state.from_index === undefined ? '' : Number(state.from_index)}" data-age="${escapeHtml(state.age_group)}">`
                    + `<div>${escapeHtml(state.gender)} · ${escapeHtml(state.age_group.replace(/_/g, ' '))}${state.chapter ? `, ${escapeHtml(state.chapter)}` : ''} <span class="text-muted">(${where})</span></div>`
                    + `<select class="form-select form-select-sm voice-state-source" aria-label="${escapeHtml(`Voice for ${speaker}, ${state.gender}, ${state.age_group.replace(/_/g, ' ')}, ${where}`)}"${where.startsWith('from') ? '' : ' disabled'}>${options}</select>${generate}</div>`;
            }).join('');
            return rows + `<div class="mt-1"><button class="btn btn-sm btn-primary" type="button" aria-label="${escapeHtml(`Apply voice changes for ${speaker}`)}" onclick="applyVoiceStates(this)">Apply</button> <button class="btn btn-sm btn-outline-secondary" type="button" aria-label="${escapeHtml(`Clear voice changes for ${speaker}`)}" onclick="clearVoiceStates(this)">Clear</button></div>`;
        }

        const pendingVoiceStateLoads = new WeakMap();
        function getVoiceStateEditorSnapshot(target) {
            return JSON.stringify({html: target.innerHTML,
                choices: [...(target.querySelectorAll?.('.voice-state-source') || [])].map(select => select.value)});
        }

        async function openVoiceStates(button) {
            const card = button.closest('.voice-card');
            const speaker = card?.dataset.voice;
            const target = card?.querySelector('.voice-state-rows');
            if (!speaker || !target || pendingVoiceStateLoads.has(target)) { return; }
            const book = currentBookFilename;
            if (pendingVoiceStateSaves.has(`${book}\0${speaker}`)) {
                showToast('Wait for these voice changes to finish saving before refreshing the panel.', 'warning');
                return;
            }
            const wasDisabled = button.disabled;
            const empty = !target.innerHTML.trim();
            pendingVoiceStateLoads.set(target, true);
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
            if (empty) { target.innerHTML = '<div class="small text-muted" role="status">Loading voice changes…</div>'; }
            const snapshot = getVoiceStateEditorSnapshot(target);
            const isCurrent = () => book === currentBookFilename && card.isConnected !== false && target.isConnected !== false;
            try {
                const data = await API.get(`/api/voices/${encodeURIComponent(speaker)}/state_timeline`);
                if (!isCurrent()) { return; }
                if (getVoiceStateEditorSnapshot(target) !== snapshot) {
                    showToast('Voice changes were edited while loading. Your current choices were kept; reopen the panel when ready to refresh.', 'warning');
                    return;
                }
                window._voiceStateSuggestions = window._voiceStateSuggestions || {};
                window._voiceStateSuggestions[speaker] = data;
                target.innerHTML = renderVoiceStateRows(data, speaker);
            } catch (e) {
                console.debug('Could not load voice changes:', e);
                if (!isCurrent()) { return; }
                const message = 'Voice changes could not be loaded. Check that Alexandria is running, then retry.';
                if (empty && getVoiceStateEditorSnapshot(target) === snapshot) {
                    target.innerHTML = `<div class="small text-warning" role="status">${message} <button type="button" class="btn btn-sm btn-outline-secondary" onclick="openVoiceStates(this)">Retry voice changes</button></div>`;
                }
                showToast(message, 'warning');
            } finally {
                pendingVoiceStateLoads.delete(target);
                button.disabled = wasDisabled;
                button.removeAttribute('aria-busy');
            }
        }

        const pendingVoiceStateSaves = new Set();
        async function applyVoiceStateSave(button, speaker, label, save, confirmation = null) {
            const card = button.closest('.voice-card');
            const book = currentBookFilename;
            const bookToken = _voiceSaveSnapshot?.book_token;
            const key = `${book}\0${speaker}`;
            if (pendingVoiceStateSaves.has(key) || button.disabled) { return; }
            pendingVoiceStateSaves.add(key);
            const controls = [...(card.querySelectorAll?.('.voice-state-rows button, .voice-state-source') || []), button]
                .filter((field, index, all) => all.indexOf(field) === index)
                .map(field => ({field, disabled: field.disabled}));
            const original = button.innerHTML;
            controls.forEach(({field}) => { field.disabled = true; });
            button.textContent = label;
            button.setAttribute?.('aria-busy', 'true');
            const isCurrent = () => currentBookFilename === book && bookToken === _voiceSaveSnapshot?.book_token && card.isConnected !== false;
            try {
                if (!bookToken) { throw new Error('Reload Voices before saving changes for this book.'); }
                if (confirmation && !await showConfirm(confirmation, {title: 'Replace voice changes?', actionLabel: 'Clear voice changes', danger: true})) { return; }
                if (!isCurrent()) { showToast('The book changed. Review the current voice changes before saving.', 'warning'); return; }
                await save(isCurrent, bookToken);
            } finally {
                pendingVoiceStateSaves.delete(key);
                controls.forEach(({field, disabled}) => { field.disabled = disabled; });
                button.innerHTML = original;
                button.removeAttribute?.('aria-busy');
            }
        }

        async function applyVoiceStates(button) {
            const card = button.closest('.voice-card');
            const speaker = card?.dataset.voice;
            const data = (window._voiceStateSuggestions || {})[speaker];
            if (!speaker || !data) { return; }
            const candidates = new Map();
            for (const state of data.states || []) {
                for (const c of [...(state.sources?.library_unused || []), ...(state.sources?.library_used || [])]) {
                    candidates.set(c.adapter_id, JSON.parse(JSON.stringify(c)));
                }
            }
            const rows = [...card.querySelectorAll('.voice-state-row')].map(row => ({
                fromIndex: row.dataset.fromIndex, age: row.dataset.age,
                value: row.querySelector('.voice-state-source')?.value || 'main'}));
            const points = [];
            const versions = JSON.parse(JSON.stringify(window._voicesByName?.[speaker]?.config?.versions || {}));
            try {
                await applyVoiceStateSave(button, speaker, 'Applying…', async (isCurrent, bookToken) => {
                for (const row of rows) {
                    if (!isCurrent()) { throw new Error('The book changed; remaining voice changes were not saved.'); }
                    const value = row.value;
                    if (row.fromIndex === '') {
                        if (value !== 'main') { throw new Error('A state change has no safe Editor boundary. Rebuild chunks and review the timeline.'); }
                        continue;
                    }
                    const fromIndex = Number(row.fromIndex);
                    if (value === 'main') {
                        if (points.length) { points.push({from_index: fromIndex, version_id: null}); }
                    } else if (value.startsWith('version:')) {
                        points.push({from_index: fromIndex, version_id: value.slice(8)});
                    } else if (value.startsWith('library:')) {
                        const chosen = candidates.get(value.slice(8));
                        if (!chosen) { continue; }
                        let versionId = Object.keys(versions).find(id => versions[id]?.age_group === row.age
                            && Object.keys(chosen.config).every(key => versions[id][key] === chosen.config[key]));
                        if (!versionId) {
                            const base = `${row.age}-${chosen.adapter_id}`.slice(0, 80);
                            versionId = base;
                            let suffix = 1;
                            while (Object.prototype.hasOwnProperty.call(versions, versionId)) {
                                const tail = `-${suffix++}`;
                                versionId = base.slice(0, 80 - tail.length) + tail;
                            }
                            await API.post(`/api/voices/${encodeURIComponent(speaker)}/versions`, {
                                version_id: versionId, age_group: row.age, config: chosen.config, book_token: bookToken});
                            versions[versionId] = {...chosen.config, age_group: row.age};
                        }
                        points.push({from_index: fromIndex, version_id: versionId});
                    }
                }
                if (!isCurrent()) { throw new Error('The book changed; the voice timeline was not saved.'); }
                if (points.length) {
                    await API.post(`/api/voices/${encodeURIComponent(speaker)}/version_timeline`, {points, book_token: bookToken});
                } else {
                    await API.del(`/api/voices/${encodeURIComponent(speaker)}/version_timeline?book_token=${encodeURIComponent(bookToken)}`);
                }
                if (!isCurrent()) { return; }
                await loadVoices();
                const first = points.length ? Math.min(...points.map(p => p.from_index)) + 1 : null;
                showToast(first ? `Voice changes saved for ${speaker}. Lines already generated from line ${first} on keep the old voice: regenerate them in the Editor.`
                    : `${speaker} uses the main voice throughout.`, 'success');
                });
            } catch (e) { showActionError("Could not apply voice changes", e, "Review the current book and saved voice changes before applying again; some version writes may have completed."); }
        }

        async function clearVoiceStates(button) {
            const speaker = button.closest('.voice-card')?.dataset.voice;
            if (!speaker) { return; }
            try {
                await applyVoiceStateSave(button, speaker, 'Clearing…', async (isCurrent, bookToken) => {
                    await API.del(`/api/voices/${encodeURIComponent(speaker)}/version_timeline?book_token=${encodeURIComponent(bookToken)}`);
                    if (!isCurrent()) { return; }
                    await loadVoices();
                    showToast(`${speaker} uses the main voice throughout. Regenerate lines already made with a state voice.`, 'success');
                }, `Clear all saved voice changes for ${speaker}? Existing rendered audio is not changed.`);
            } catch (e) { showActionError("Could not clear voice changes", e, "Reload Voices to check whether the saved changes were cleared before trying again."); }
        }

        // Editor: from this line on, this character sounds different (an aged
        // character, a time skip). The anchor is prefixed to every later line's
        // instruct on the CustomVoice path; measured to hold pitch to 2.5 st
        // over a run where per-line instructs alone wander 3.5.
        async function voiceChangesHere(chunkId) {
            const row = document.querySelector(`#chunks-table-body tr[data-id="${chunkId}"]`);
            const getSpeaker = () => row ? (row.querySelector('.chunk-speaker')?.value || row.querySelector('select')?.value || '') : '';
            const speaker = getSpeaker();
            if (!speaker) { showToast('Pick the line\'s speaker first.', 'warning'); return; }
            const book = currentBookFilename;
            const current = (window._voicesByName && window._voicesByName[speaker])?.config || {};
            try {
                const values = await showPresetEditor({title: `Voice change from line ${chunkId + 1} for ${speaker}`,
                    nameLabel: 'Character style', name: current.character_style || current.default_style || '',
                    allowEmptyName: true, includeDescription: false, actionLabel: 'Save change point',
                    helperText: 'Describe how the character sounds from this line on. Leave empty to remove this change point. Regenerate later lines to hear the change.'});
                if (!values) { return; }
                if (book !== currentBookFilename || row?.isConnected === false || getSpeaker() !== speaker) {
                    showToast('The book or line speaker changed. Review it before saving the change point.', 'warning'); return;
                }
                await API.post(`/api/voices/${encodeURIComponent(speaker)}/style_timeline`, { from_index: chunkId, character_style: values.name });
                if (book === currentBookFilename) { await loadVoices(); }
                showToast(values.name ? `${speaker} changes from line ${chunkId + 1}. Regenerate the later lines to hear it.` : `Change point at line ${chunkId + 1} removed.`, 'success', 8000);
            } catch (e) {
                showActionError("Could not save the change", e, "Review the current book, line speaker and saved change point before saving again.");
            }
        }

        function collectVoiceConfig() {
            const cards = document.querySelectorAll('.voice-card');
            const config = {};
            const modelsById = getLoraModelsById();

            cards.forEach(card => {
                const name = getVoiceCardKey(card);
                const metadata = getVoiceCardMetadata(card);
                const alias = card.querySelector('.alias-select') ? card.querySelector('.alias-select').value : '';
                const type = card.querySelector('.voice-type:checked').value;

                if (type === 'ensemble') {
                    config[name] = {
                        type: 'ensemble',
                        members: Array.from(card.querySelectorAll('.ensemble-member:checked')).map(cb => cb.value),
                        seed: "-1"
                    };
                } else if (type === 'custom') {
                    config[name] = {
                        type: 'custom',
                        voice: card.querySelector('.voice-select').value,
                        character_style: card.querySelector('.character-style').value,
                        seed: "-1"
                    };
                } else if (type === 'clone') {
                    config[name] = {
                        type: 'clone',
                        ref_text: card.querySelector('.ref-text').value,
                        ref_audio: card.querySelector('.ref-audio').value,
                        character_style: metadata.type === 'clone' ? (metadata.character_style || '') : '',
                        default_style: metadata.type === 'clone' ? (metadata.default_style || '') : '',
                        description: getVoiceCardDescription(card),
                        seed: "-1"
                    };
                } else if (type === 'builtin_lora') {
                    const adapterId = card.querySelector('.builtin-lora-select').value;
                    const adapterEntry = modelsById.get(adapterId);
                    config[name] = {
                        type: 'builtin_lora',
                        adapter_id: adapterId,
                        adapter_path: adapterEntry?.adapter_path || adapterEntry?.path || '',
                        character_style: card.querySelector('.builtin-lora-style').value,
                        seed: "-1"
                    };
                } else if (type === 'lora') {
                    const adapterId = card.querySelector('.lora-adapter-select').value;
                    const adapterEntry = modelsById.get(adapterId);
                    config[name] = {
                        type: 'lora',
                        adapter_id: adapterId,
                        adapter_path: adapterEntry?.adapter_path || (adapterId ? `lora_models/${adapterId}` : ''),
                        character_style: card.querySelector('.lora-character-style').value,
                        seed: "-1"
                    };
                } else if (type === 'design') {
                    config[name] = {
                        type: 'design',
                        description: card.querySelector('.design-description').value,
                        seed: "-1"
                    };
                }
                // Explicitly clear aliases; omitted fields retain their saved values.
                config[name].alias_of = alias || null;
                const readyBox = card.querySelector('.voice-ready');
                if (readyBox) {
                    config[name].ready = !!readyBox.checked;
                }
                const preserved = { ...metadata };
                // Form fields describe the selected voice type; retain all
                // other server metadata without copying inactive voice inputs.
                for (const key of ['type', 'voice', 'character_style', 'default_style', 'seed', 'ref_audio', 'ref_text', 'adapter_id', 'adapter_path', 'description', 'members', 'alias_of', 'ready']) {
                    delete preserved[key];
                }
                config[name] = { ...preserved, ...config[name], seed: String(card.querySelector('.voice-seed')?.value ?? metadata.seed ?? config[name].seed).trim() || "-1" };
            });
            const merged = {};
            cards.forEach(card => {
                const name = card.dataset.voice;
                if (!card.dataset.version) { merged[name] = config[getVoiceCardKey(card)]; }
            });
            cards.forEach(card => {
                if (!card.dataset.version) { return; }
                const name = card.dataset.voice;
                const fields = config[getVoiceCardKey(card)];
                fields.description = getVoiceCardDescription(card);
                delete fields.alias_of;
                const metadata = getVoiceCardMetadata(card);
                // Pending cards must not become generated voices merely by rendering.
                const source = window._voicesByName?.[name]?.persona_states?.find(state => state.version_id === card.dataset.version);
                if (!metadata.persona_state || (source && !isPersonaStateCurrent(metadata.persona_state, source))) { return; }
                merged[name] ||= {...(window._voicesByName?.[name]?.config || {})};
                merged[name].versions = {...(merged[name].versions || {}), [card.dataset.version]: fields};
            });
            return merged;
        }

        function onVoiceReadyChange(box) {
            const card = box.closest('.voice-card');
            if (card) {
                card.dataset.ready = box.checked ? '1' : '0';
                card.classList.toggle('border-success', box.checked);
            }
            renderReadyCount();
            onToggleHideReady();
        }

        function renderReadyCount() {
            const el = document.getElementById('voices-ready-count');
            if (!el) {
                return;
            }
            const cards = document.querySelectorAll('.voice-card');
            const ready = Array.from(cards).filter(c => c.dataset.ready === '1').length;
            el.textContent = cards.length ? `${ready} / ${cards.length} ready` : '';
        }

        function onToggleHideReady() {
            const hide = !!document.getElementById('voices-hide-ready')?.checked;
            const filter = document.getElementById('voices-state-filter')?.value || 'all';
            document.querySelectorAll('.voice-card').forEach(card => {
                const states = card.dataset.hasStates === '1';
                const filtered = (filter === 'states' && !states) || (filter === 'single' && states);
                card.style.display = filtered || (hide && card.dataset.ready === '1') ? 'none' : '';
            });
            const groups = new Set(Array.from(document.querySelectorAll('.voice-card'))
                .map(card => card.closest?.('.voice-character-group')).filter(Boolean));
            groups.forEach(group => {
                group.style.display = Array.from(group.querySelectorAll('.voice-card')).every(card => card.style.display === 'none') ? 'none' : '';
            });
        }

        let _voiceStatusClearTimer = null;
        let _voiceSaveSnapshot = null;
        const voiceDraftPrefix = 'alexandria.voice-draft.v1.';
        let _voiceDraftRecord = null;
        let _voiceDraftStorageError = null;
        let _voiceRecoveryDrafts = [];

        function saveVoiceDraftRecord(record) {
            try {
                const value = JSON.stringify(record);
                window.localStorage.setItem(record.key, value);
                if (window.localStorage.getItem(record.key) !== value) { throw new Error('Draft storage did not retain the edit.'); }
                _voiceDraftStorageError = null;
                return true;
            } catch (error) {
                _voiceDraftStorageError = error;
                return false;
            }
        }

        function removeVoiceDraftRecord(record) {
            try {
                // Another tab may have recovered this draft; only remove our exact version.
                if (window.localStorage.getItem(record.key) === JSON.stringify(record)) {
                    window.localStorage.removeItem(record.key);
                }
            } catch (error) {
                _voiceDraftStorageError = error;
            }
        }

        function enqueueVoiceDraft(voices, recovered = null) {
            const key = _voiceDraftRecord?.key || voiceDraftPrefix +
                (window.crypto?.randomUUID?.() || Date.now() + '-' + Math.random().toString(36).slice(2));
            const record = { version: 1, key, book_token: _voiceSaveSnapshot.book_token,
                revision: _voiceSaveSnapshot.revision, book_id: _voiceSaveSnapshot.book_id || '',
                generation: (_voiceDraftRecord?.generation || 0) + 1,
                voices: JSON.parse(JSON.stringify(voices)), updated: new Date().toISOString() };
            _voiceDraftRecord = record;
            // Synchronous storage precedes the debounce timer and any network request.
            saveVoiceDraftRecord(record);
            voiceSaveQueue.enqueue({ book_token: record.book_token, voices: record.voices,
                draft_generation: record.generation, recovered });
        }

        function renderVoiceDrafts() {
            const panel = document.getElementById('voice-save-drafts');
            if (!panel) { return; }
            _voiceRecoveryDrafts = [];
            let invalid = false;
            try {
                const storage = window.localStorage;
                for (let index = 0; index < storage.length; index++) {
                    const key = storage.key(index);
                    if (!key?.startsWith(voiceDraftPrefix) || key === _voiceDraftRecord?.key) { continue; }
                    try {
                        const record = JSON.parse(storage.getItem(key));
                        if (record.version !== 1 || record.key !== key || !/^[0-9a-f]{64}$/.test(record.book_token) ||
                            !/^[0-9a-f]{64}$/.test(record.revision) || !record.voices || Array.isArray(record.voices) ||
                            typeof record.voices !== 'object') { throw new Error('Invalid saved voice draft'); }
                        _voiceRecoveryDrafts.push(record);
                    } catch (error) { invalid = true; }
                }
                panel.innerHTML = _voiceRecoveryDrafts.map((record, index) => {
                    const matches = record.book_token === _voiceSaveSnapshot?.book_token;
                    return `<div class="alert alert-warning"><strong>Unsaved voice draft</strong> ${escapeHtml(record.book_id || 'unnamed book')} · ${escapeHtml(record.updated || '')}
                        <details><summary>View saved edits</summary><pre class="text-wrap">${escapeHtml(JSON.stringify(record.voices, null, 2))}</pre></details>
                        ${matches ? `<button type="button" class="btn btn-sm btn-warning" onclick="recoverVoiceDraft(${index})">Recover edits</button>` : 'Load the original book version to recover this draft.'}
                        <button type="button" class="btn btn-sm btn-link" onclick="discardStoredVoiceDraft(${index})">Discard draft</button></div>`;
                }).join('') + (invalid ? '<div class="alert alert-danger">A saved voice draft could not be read. It has been retained in browser storage.</div>' : '');
            } catch (error) {
                _voiceDraftStorageError = error;
                panel.textContent = 'Browser draft storage is unavailable. Keep this tab open until voice edits are saved.';
            }
        }

        async function recoverVoiceDraft(index) {
            const record = _voiceRecoveryDrafts[index];
            if (!record || voiceSaveQueue.isDirty()) { showToast('Save or discard current edits before recovering a draft.', 'warning'); return; }
            const localRevision = voiceSaveQueue.getRevision();
            const snapshot = _voiceSaveSnapshot;
            if (!snapshot || record.book_token !== snapshot.book_token) { showToast('Load the original book version before recovering these edits.', 'warning'); return; }
            if (record.revision !== snapshot.revision) {
                showToast('Saved voices have changed since this draft. View the saved edits and copy the changes you want into the current voices; the draft has been retained.', 'warning');
                return;
            }
            if (!await showConfirm('Recover these voice edits?\n' + JSON.stringify(record.voices, null, 2), {title: 'Recover saved voice draft?', actionLabel: 'Recover edits', danger: false})) { return; }
            if (voiceSaveQueue.getRevision() !== localRevision || _voiceSaveSnapshot !== snapshot) {
                showToast('Voices changed during recovery. The draft has been retained.', 'warning'); return;
            }
            enqueueVoiceDraft(record.voices, record);
            try { await flushVoiceSaves(); await loadVoices(); }
            catch (error) { showActionError('Voice draft recovery failed', error, 'The draft is retained. Check the current book and saved voices before recovering the draft again.', 'warning'); }
        }

        async function discardStoredVoiceDraft(index) {
            const record = _voiceRecoveryDrafts[index];
            if (!record || !await showConfirm('Discard this saved voice draft?\n' + JSON.stringify(record.voices, null, 2), {title: 'Discard saved voice draft?', actionLabel: 'Discard draft', danger: true})) { return; }
            removeVoiceDraftRecord(record);
            renderVoiceDrafts();
        }

        const voiceSaveQueue = createSerializedSaveQueue({
            delay: 800,
            write: async draft => {
                if (!_voiceSaveSnapshot || _voiceSaveSnapshot.book_token !== draft.book_token) {
                    throw new Error('The active book changed. Your unsaved voice edits were retained.');
                }
                const result = await API.post('/api/voice_config/save', {
                    revision: _voiceSaveSnapshot.revision,
                    book_token: draft.book_token,
                    voices: draft.voices,
                });
                if (result.book_token !== draft.book_token || !/^[0-9a-f]{64}$/.test(result.revision)) {
                    throw new Error('The voice save could not be confirmed. Your edits were retained.');
                }
                _voiceSaveSnapshot = { ..._voiceSaveSnapshot, revision: result.revision };
                if (draft.recovered) { removeVoiceDraftRecord(draft.recovered); }
                if (_voiceDraftRecord?.generation === draft.draft_generation) {
                    removeVoiceDraftRecord(_voiceDraftRecord);
                    _voiceDraftRecord = null;
                } else if (_voiceDraftRecord) {
                    _voiceDraftRecord = { ..._voiceDraftRecord, revision: result.revision };
                    saveVoiceDraftRecord(_voiceDraftRecord);
                }
            },
            onDirty: () => {
                clearTimeout(_voiceStatusClearTimer);
                const statusEl = document.getElementById('voice-save-status');
                if (statusEl) { statusEl.textContent = _voiceDraftStorageError ? 'unsaved — browser draft unavailable; keep this tab open' : 'unsaved — draft retained in this browser'; }
            },
            onSaved: () => {
                const statusEl = document.getElementById('voice-save-status');
                if (statusEl) {
                    statusEl.innerHTML = _voiceDraftStorageError ? 'saved — browser draft cleanup failed' : '<i class="fas fa-check text-success me-1"></i>saved';
                    if (!_voiceDraftStorageError) { _voiceStatusClearTimer = setTimeout(() => { statusEl.innerHTML = ''; }, 4000); }
                }
            },
            onError: error => {
                console.error('Failed to save voice config:', error);
                const statusEl = document.getElementById('voice-save-status');
                if (statusEl) {
                    statusEl.innerHTML = '<i class="fas fa-times text-danger me-1"></i>save failed — edits retained <button type="button" class="btn btn-link btn-sm" onclick="discardVoiceEditsAndReload()">Discard edits and reload</button>';
                }
            },
        });

        function saveVoicesDebounced() {
            if (document.querySelectorAll('.voice-card').length === 0) { return; }
            if (!_voiceSaveSnapshot) {
                showToast('Wait for voices to finish loading before editing them.', 'warning');
                return;
            }
            enqueueVoiceDraft(collectVoiceConfig());
        }

        async function flushVoiceSaves() {
            await voiceSaveQueue.flush();
        }

        async function discardVoiceEditsAndReload() {
            if (!await showConfirm('Discard all unsaved voice edits and reload the saved voices?', {title: 'Discard unsaved voice edits?', actionLabel: 'Discard edits', danger: true})) { return; }
            try {
                await voiceSaveQueue.discard();
                if (_voiceDraftRecord) { removeVoiceDraftRecord(_voiceDraftRecord); _voiceDraftRecord = null; }
                await loadVoices();
            } catch (error) {
                showActionError('Voice reload failed', error, 'Check the current book and saved voices before retrying. Review any unsaved edits shown in Voices.', 'warning');
            }
        }

        // pagehide is best-effort; the synchronous draft is the reload/closure safety net.
        window.addEventListener('pagehide', () => { flushVoiceSaves().catch(() => {}); });
        window.addEventListener('beforeunload', event => {
            if (voiceSaveQueue.isDirty()) { event.preventDefault(); event.returnValue = ''; }
        });

        // Auto-save on any change inside the voices list
        document.getElementById('voices-list').addEventListener('change', () => {
            saveVoicesDebounced();
        });
        document.getElementById('voices-list').addEventListener('input', () => {
            saveVoicesDebounced();
        });

        // --- Editor Tab: text integrity (issue #522 s7.4/7.5) ---
        let editorIntegrityView = 0;
        let textDiffRequest = 0;

        function invalidateEditorIntegrity(message = 'Source check pending') {
            editorIntegrityView++;
            const badge = document.getElementById('editor-integrity-badge');
            if (badge) {
                badge.textContent = message;
                badge.className = 'badge bg-secondary';
            }
        }

        async function refreshEditorIntegrity() {
            const badge = document.getElementById('editor-integrity-badge');
            if (!badge) { return; }
            if (pendingChunkEdits.size || failedChunkEdits.size) {
                invalidateEditorIntegrity('Save edits before checking source');
                return;
            }
            const view = ++editorIntegrityView;
            const edits = chunkEditsRevision;
            try {
                const report = await API.get('/api/editor/integrity');
                if (view !== editorIntegrityView || edits !== chunkEditsRevision) { return; }
                if (report.status === 'verified') {
                    badge.textContent = 'Saved text matches source';
                    badge.className = 'badge bg-success';
                } else if (report.status === 'differences' && Array.isArray(report.hunks)) {
                    badge.textContent = `${report.hunks.length} word differences`;
                    badge.className = 'badge bg-warning text-dark';
                } else {
                    badge.textContent = 'Source comparison unavailable';
                    badge.className = 'badge bg-secondary';
                }
            } catch (e) {
                if (view === editorIntegrityView) {
                    badge.textContent = 'Source comparison unavailable';
                    badge.className = 'badge bg-secondary';
                }
            }
        }

        async function ensureMergeIntegrityApproval() {
            await ensureEditorRenderSnapshot();
            const report = await API.get('/api/editor/integrity');
            if (!report || !['verified', 'differences', 'unavailable'].includes(report.status)
                    || typeof report.snapshot !== 'string' || !/^[0-9a-f]{64}$/.test(report.snapshot)
                    || !Array.isArray(report.hunks)) {
                throw new Error('Source comparison is unavailable; refresh and try again.');
            }
            renderTextDiff(report);
            const message = report.status === 'verified'
                ? 'Merge all valid audio chunks into final audiobook?'
                : report.status === 'differences'
                    ? `${report.hunks.length} word differences are shown in Text integrity. Continue with this exact text? Cancel to fix them first.`
                    : `Source comparison unavailable: ${report.reason || 'No original source is on record.'} Continue without a verified source match?`;
            if (!await showConfirm(message, {title: 'Continue with this source text?', actionLabel: 'Continue with text', danger: false})) { return null; }
            return report.snapshot;
        }

        async function openTextDiff() {
            const panel = document.getElementById('text-diff-panel');
            const summary = document.getElementById('text-diff-summary');
            if (!panel) {
                return;
            }
            const request = ++textDiffRequest;
            const book = currentBookFilename;
            const edits = chunkEditsRevision;
            const isCurrent = () => request === textDiffRequest
                && book === currentBookFilename && edits === chunkEditsRevision;
            if (panel.style.display !== 'none') {
                panel.style.display = 'none';
                return;
            }
            summary.textContent = 'Comparing…';
            try {
                const diff = await API.get('/api/editor/integrity');
                if (!isCurrent()) { return; }
                renderTextDiff(diff);
            } catch (e) {
                if (!isCurrent()) { return; }
                summary.textContent = '';
                showActionError("Text integrity unavailable", e, "Refresh the Editor and run Text integrity again. A failed check does not establish that the source matches.", "error");
            }
        }

        function renderTextDiff(diff) {
            const panel = document.getElementById('text-diff-panel');
            const summary = document.getElementById('text-diff-summary');
            if (diff.status === 'unavailable') {
                summary.textContent = 'Source comparison unavailable';
                panel.style.display = '';
                panel.textContent = diff.reason || 'No original source is on record.';
                textDiffRequest++;
                return;
            }
            const t = diff.totals || {};
            const hunks = diff.hunks || [];
            summary.textContent = `${t.script_words} script words vs ${t.source_words} source · ${t.deleted} dropped, ${t.inserted} added, ${t.replaced} changed · ${hunks.length} place${hunks.length === 1 ? '' : 's'}`;
            const kindClass = { delete: 'table-danger', insert: 'table-success', replace: 'table-warning' };
            const rows = hunks.map(h => `
                <tr class="${kindClass[h.kind] || ''}">
                    <td class="text-nowrap small">${escapeHtml(h.kind)}<br><span class="text-muted">chunk ${escapeHtml(String(h.chunk))}</span></td>
                    <td class="small"><span class="text-muted">${escapeHtml(h.source_before)}</span> <strong>${escapeHtml(h.source_words) || '∅'}</strong> <span class="text-muted">${escapeHtml(h.source_after)}</span></td>
                    <td class="small"><strong>${escapeHtml(h.script_words) || '∅'}</strong></td>
                    <td class="text-nowrap">${h.entry_index != null ? `<button class="btn btn-sm btn-link p-0" onclick="scrollToChunkRow(${Number(h.entry_index)})">entry ${Number(h.entry_index) + 1}</button>` : ''}</td>
                </tr>`).join('');
            panel.style.display = '';
            panel.innerHTML = `
                <div class="card-body p-2">
                    ${hunks.length ? `
                    <div class="table-responsive" style="max-height: 40vh; overflow-y: auto;">
                        <table class="table table-sm mb-0">
                            <thead><tr><th>Kind</th><th>Source</th><th>Script</th><th></th></tr></thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>` : '<div class="small text-success mb-0">Every source word is in the script, in order.</div>'}
                </div>`;
            textDiffRequest++;
        }

        function scrollToChunkRow(id) {
            const tr = document.querySelector(`tr[data-id="${id}"]`);
            if (!tr) {
                showToast(`Entry ${id + 1} is not in the table yet.`, 'warning');
                return;
            }
            if (tr.style.display === 'none') {
                const filter = document.getElementById('chk-drift-only');
                if (filter?.checked) {
                    filter.checked = false;
                    applyDriftFilter();
                    showToast(`Showing all chunks to reveal entry ${id + 1}.`, 'info');
                }
                if (tr.style.display === 'none') {
                    showToast(`Entry ${id + 1} is hidden. Show all chunks, then try its link again.`, 'warning');
                    return;
                }
            }
            tr.scrollIntoView({ behavior: 'smooth', block: 'center' });
            tr.classList.add('table-info');
            setTimeout(() => { tr.classList.remove('table-info'); }, 2000);
        }

        // --- Editor Tab ---
        let isPlayingSequence = false;
        let isRenderingAll = false;
        let cachedChunks = []; // Cache to track changes
        let chunkSnapshotRevision = null;
        let chunkSnapshotBook = null;
        let loadChunksTimer = null; // Pending standalone editor poll
        let chunkRefreshPromise = null;
        let chunkRefreshAgain = false;
        let chunkRefreshForced = false;

        function buildSpeakerSelect(chunk) {
            const current = (chunk.speaker || '').trim();
            const names = Array.isArray(window._voicesNames) ? window._voicesNames : [];
            const normalized = [...new Set(names.map(n => (n || '').trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
            const options = normalized.map(name => `<option value="${escapeHtml(name)}" ${name === current ? 'selected' : ''}>${escapeHtml(name)}</option>`).join('');
            const unknownOption = current && !normalized.includes(current)
                ? `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)} (custom)</option>`
                : '';

            return `<select class="form-select form-select-sm" onchange="updateChunk(${chunk.id}, 'speaker', this.value)">${unknownOption}${options}</select>`;
        }

        // Check if any audio is currently playing
        function isAudioPlaying() {
            const audios = document.querySelectorAll('audio');
            for (const audio of audios) {
                if (!audio.paused && !audio.ended) { return true; }
            }
            return false;
        }

        // Update only changed rows instead of full redraw
        // --- Voice drift: a rendered chunk that doesn't sound like its speaker's reference ---
        function _driftKey(drift) {
            return drift ? `${drift.score}|${drift.flagged}|${drift.error || ''}` : '';
        }

        function _driftBadge(chunk) {
            const d = chunk.drift;
            if (!d || !d.flagged) { return ''; }
            const title = `Voice drift: similarity ${d.score} to ${d.reference} (threshold ${d.threshold}). Regenerate with Gen.`;
            return ` <span class="badge bg-warning text-dark drift-badge" title="${escapeHtml(title)}"><i class="fas fa-fingerprint me-1"></i>drift ${d.score}</span>`;
        }

        function applyDriftFilter() {
            const only = document.getElementById('chk-drift-only');
            const flaggedOnly = !!(only && only.checked);
            document.querySelectorAll('#chunks-table-body tr[data-id]').forEach(tr => {
                const flagged = !!tr.querySelector('.drift-badge');
                tr.style.display = (flaggedOnly && !flagged) ? 'none' : '';
            });
        }

        async function runDriftCheck(indices) {
            const btn = document.getElementById('btn-drift-check');
            if (btn && btn.disabled) { return; }
            if (btn) { btn.disabled = true; }
            try {
                const res = await API.post('/api/chunks/drift_check', indices ? { indices } : {});
                if (!res.measured) {
                    if (btn) { btn.disabled = false; }
                    showToast('Voice check not measured: no speechbrain interpreter configured (Voice Lab → rocm_python).', 'warning');
                    return;
                }
                _startPolling('logs:drift_check', () => API.get('/api/status/drift_check'), {
                    doneCheck: s => !s.running,
                    onTick: () => {},
                    onDone: async (s) => {
                        if (btn) { btn.disabled = false; }
                        await loadChunks(true);
                        const last = (s.logs || []).slice(-1)[0] || '';
                        showToast(last.startsWith('NOT MEASURED') ? last : `Voice check: ${last}`, /\b0 flagged\b/.test(last) ? 'success' : 'warning');
                    },
                });
            } catch (e) {
                if (btn) { btn.disabled = false; }
                showActionError("Voice check failed", e, "Check the Voice Lab interpreter and speechbrain setup, then check the voice-check task status before starting again.", "error");
            }
        }

        function updateChunkRow(chunk) {
            const tr = document.querySelector(`tr[data-id="${chunk.id}"]`);
            if (!tr) { return false; }

            const statusColor = chunk.status === 'done' ? 'success' :
                              chunk.status === 'generating' ? 'warning' :
                              chunk.status === 'error' ? 'danger' : 'secondary';

            // Update status badge
            const badge = tr.querySelector('.badge');
            if (badge) {
                badge.className = `badge bg-${statusColor}`;
                badge.innerText = chunk.status;
                const oldDrift = tr.querySelector('.drift-badge');
                if (oldDrift) { oldDrift.remove(); }
                badge.insertAdjacentHTML('afterend', _driftBadge(chunk));
                tr.classList.toggle('drift-flagged', !!(chunk.drift && chunk.drift.flagged));
            }
            applyDriftFilter();

            // Update action area (button/progress)
            const actionContainer = tr.querySelector('.chunk-actions');
            if (actionContainer) {
                const existingBtn = actionContainer.querySelector('button');
                const existingProgress = actionContainer.querySelector('.progress');

                if (chunk.status === 'generating') {
                    if (existingBtn && !existingProgress) {
                        const progressBar = document.createElement('div');
                        progressBar.className = 'progress';
                        progressBar.style.width = '100px';
                        progressBar.style.height = '20px';
                        progressBar.innerHTML = '<div class="progress-bar progress-bar-striped progress-bar-animated bg-warning flex-grow-1" role="status" aria-label="Generating audio">Generating…</div>';
                        actionContainer.replaceChild(progressBar, existingBtn);
                    }
                } else {
                    if (existingProgress && !existingBtn) {
                        const btn = document.createElement('button');
                        btn.className = 'btn btn-sm btn-primary';
                        btn.onclick = () => generateChunk(chunk.id);
                        btn.innerHTML = '<i class="fas fa-play"></i> Gen';
                        actionContainer.replaceChild(btn, existingProgress);
                    }
                }

                // Update audio player when status is done - always refresh src to bust cache
                if (chunk.status === 'done' && chunk.audio_path) {
                    const existingAudio = actionContainer.querySelector('audio');
                    const existingNoAudio = actionContainer.querySelector('.text-muted');
                    const newSrc = encodeURI(`/${chunk.audio_path}`) + `?t=${encodeURIComponent(chunk.audio_revision || Date.now())}`;

                    if (existingNoAudio) {
                        // No audio element yet, create one
                        const audioHtml = `<audio class="chunk-audio" data-id="${chunk.id}" controls src="${newSrc}" style="width: 200px; height: 30px;" onplay="stopOthers(${chunk.id})"></audio>`;
                        existingNoAudio.outerHTML = audioHtml;
                    } else if (existingAudio) {
                        // Audio exists - just update the src with new cache-busting timestamp
                        // This forces browser to fetch the regenerated file
                        existingAudio.src = newSrc;
                        existingAudio.load(); // Force reload
                    }
                }
            }
            return true;
        }

        function ensureChunkRefresh(forceFullRedraw = false) {
            chunkRefreshForced = chunkRefreshForced || forceFullRedraw;
            if (chunkRefreshPromise) {
                chunkRefreshAgain = true;
                return chunkRefreshPromise;
            }
            chunkRefreshPromise = (async () => {
                let chunks;
                do {
                    chunkRefreshAgain = false;
                    const forced = chunkRefreshForced;
                    chunkRefreshForced = false;
                    chunks = await refreshChunkSnapshot(forced);
                } while (chunkRefreshAgain);
                return chunks;
            })().finally(() => { chunkRefreshPromise = null; });
            return chunkRefreshPromise;
        }

        let deliveryReviewView = 0;
        let deliveryRetryClaim = null;
        let deliveryRetryPending = false;

        function renderDeliveryReview(info) {
            const panel = document.getElementById('delivery-review-panel');
            if (!panel) { return; }
            if (!info || !Number.isInteger(info.count) || !Array.isArray(info.rows)) {
                panel.style.display = '';
                panel.textContent = 'Delivery review is unavailable; refresh before relying on its warnings.';
                return;
            }
            panel.style.display = info.count || deliveryRetryClaim ? '' : 'none';
            const rows = info.rows.map(row => `<li><strong>Entry ${escapeHtml(String(row.entry))} · ${escapeHtml(row.speaker)}</strong>: ${escapeHtml(row.text)}<br><small>${escapeHtml(row.instruct)}</small></li>`).join('');
            panel.innerHTML = `<strong>Needs delivery review: ${info.count} entries</strong>
                <p class="small mb-2">Step 3 exhausted its retries for these entries and used generic delivery instructions. Text and speakers were retained.</p>
                <div style="max-height:16rem;overflow:auto"><ol>${rows}</ol></div>
                ${info.retry_refusal ? `<p class="small">${escapeHtml(info.retry_refusal)}</p>` : ''}
                <button class="btn btn-sm btn-outline-warning" onclick="retryDeliveryInstructions()" ${!info.retry_available || deliveryRetryPending || deliveryRetryClaim ? 'disabled' : ''}>Retry delivery only</button>
                ${deliveryRetryClaim ? '<button class="btn btn-sm btn-outline-danger ms-2" onclick="cancelDeliveryInstructions()">Cancel delivery retry</button>' : ''}`;
        }

        async function refreshDeliveryReview() {
            const view = ++deliveryReviewView;
            try {
                const info = await API.get('/api/annotated_script/delivery_review');
                if (view !== deliveryReviewView) { return null; }
                renderDeliveryReview(info);
                return info;
            } catch (e) {
                if (view === deliveryReviewView) { renderDeliveryReview(null); }
                return null;
            }
        }

        window.retryDeliveryInstructions = async () => {
            if (deliveryRetryPending || deliveryRetryClaim) { return; }
            deliveryRetryPending = true;
            try {
                await ensureEditorRenderSnapshot();
                const info = await refreshDeliveryReview();
                if (!info || !info.count || !info.retry_available) { return; }
                if (!await confirmIfRemote('this delivery-only retry', true)) { return; }
                const started = await API.post('/api/annotated_script/delivery_review/retry', {snapshot: info.snapshot});
                deliveryRetryClaim = started.claim_id;
                renderDeliveryReview(info);
                const claim = deliveryRetryClaim;
                _startPolling('delivery_retry', () => API.get('/api/annotated_script/delivery_review/status/' + encodeURIComponent(claim)), {
                    intervalMs: 1500,
                    doneCheck: status => !status.running,
                    onDone: async () => {
                        if (deliveryRetryClaim !== claim) { return; }
                        deliveryRetryClaim = null;
                        await loadChunks(true);
                        await refreshDeliveryReview();
                        showToast('Delivery retry is no longer running. Review the refreshed flagged entries before generating audio.', 'info');
                    },
                });
            } catch (e) {
                showActionError("Delivery retry failed", e, "Check the delivery retry status and refreshed flagged entries before retrying. For an LLM refusal, use Setup \u2192 Test Connection.", "warning");
            } finally {
                deliveryRetryPending = false;
                await refreshDeliveryReview();
            }
        };

        window.cancelDeliveryInstructions = async () => {
            if (!deliveryRetryClaim) { return; }
            const claim = deliveryRetryClaim;
            try {
                await API.post('/api/annotated_script/delivery_review/cancel', {claim_id: claim});
                showToast('Cancellation queued; the provider call must return before ownership is released.', 'info');
            } catch (e) {
                showActionError("Delivery cancellation failed", e, "Check delivery retry status before cancelling again; the provider call may still be running.", "warning");
            }
        };

        async function loadChunks(forceFullRedraw = false) {
            invalidateEditorIntegrity();
            ++deliveryReviewView;
            try {
                const chunks = await ensureChunkRefresh(forceFullRedraw);
                const status = document.getElementById('chunk-load-status');
                if (status) { status.innerHTML = ''; }
                void refreshEditorIntegrity();
                await refreshDeliveryReview();
                return chunks;
            } catch (e) {
                console.error("Error loading chunks:", e);
                const status = document.getElementById('chunk-load-status');
                if (status) {
                    status.innerHTML = '<span class="text-warning">Could not load editor chunks. Displayed rows may be out of date. Check that Alexandria is running, then retry.</span> <button type="button" class="btn btn-sm btn-outline-secondary" onclick="loadChunks(true)">Retry chunks</button>';
                }
            }
        }

        function getChunkAudioIdentity(chunk) {
            const revision = chunk.audio_revision || null;
            return JSON.stringify([chunk.audio_path, revision,
                ...(revision ? [] : [chunk.text, chunk.instruct, chunk.speaker])]);
        }

        async function refreshChunkSnapshot(forceFullRedraw = false) {
            const refreshBook = currentBookFilename;
            // Cancel any pending poll so re-entrant calls don't stack up
            if (loadChunksTimer) {
                clearTimeout(loadChunksTimer);
                loadChunksTimer = null;
            }

            const tbody = document.getElementById('chunks-table-body');

            // Show loading only if empty
            if (tbody.children.length === 0 || (tbody.children.length === 1 && tbody.children[0].children.length === 1)) {
                tbody.innerHTML = '<tr><td colspan="6" class="text-center">Loading chunks...</td></tr>';
                forceFullRedraw = true;
            }

            const query = !forceFullRedraw && chunkSnapshotRevision
                ? `?revision=${encodeURIComponent(chunkSnapshotRevision)}` : '';
            const snapshot = await API.get('/api/chunks/status' + query);
            if (refreshBook !== currentBookFilename) { return cachedChunks; }
            if (!snapshot || typeof snapshot.revision !== 'string' || typeof snapshot.full !== 'boolean'
                    || !Array.isArray(snapshot.chunks) || !Array.isArray(snapshot.changed_ids)
                    || !Number.isInteger(snapshot.total) || snapshot.total < 0) {
                chunkSnapshotRevision = null;
                throw new Error('Editor snapshot is unavailable.');
            }
            let chunks;
            if (snapshot.full) {
                chunks = snapshot.chunks;
            } else {
                const changed = new Map(snapshot.chunks.map(chunk => [chunk.id, chunk]));
                const previous = new Map(cachedChunks.map(chunk => [chunk.id, chunk]));
                if (snapshot.total !== cachedChunks.length || changed.size !== snapshot.chunks.length
                        || snapshot.chunks.some(chunk => !previous.has(chunk.id) || previous.get(chunk.id).uid !== chunk.uid)) {
                    chunkSnapshotRevision = null;
                    throw new Error('Editor snapshot changed; refresh required.');
                }
                chunks = cachedChunks.map(chunk => changed.get(chunk.id) || chunk);
            }
            if (chunks.length !== snapshot.total) {
                chunkSnapshotRevision = null;
                throw new Error('Editor snapshot is incomplete.');
            }
            if (chunks.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="text-center">No chunks found. Please generate script first.</td></tr>';
                cachedChunks = [];
                chunkSnapshotBook = refreshBook;
                chunkSnapshotRevision = snapshot.revision;
                return chunks;
            }

            // Update Full Progress Bar
            const completed = chunks.filter(c => c.status === 'done').length;
            const total = chunks.length;
            const percentage = total > 0 ? Math.round((completed / total) * 100) : 0;
            const progressBar = document.getElementById('full-progress-bar');
            if (progressBar) {
                progressBar.style.width = `${percentage}%`;
                progressBar.innerText = `${percentage}% (${completed}/${total})`;
            }

            // Skip redraw if playing audio (unless forced)
            if (!forceFullRedraw && (isPlayingSequence || isAudioPlaying())) {
                // Only update status badges and progress indicators
                chunks.forEach(chunk => {
                    if (snapshot.full || snapshot.changed_ids.includes(chunk.id)) { updateChunkRow(chunk); }
                });
                cachedChunks = chunks;
                chunkSnapshotBook = refreshBook;
                chunkSnapshotRevision = snapshot.revision;

                // Continue polling if generating
                if (!isRenderingAll && chunks.some(c => c.status === 'generating')) {
                    loadChunksTimer = setTimeout(() => loadChunks(false), 2000);
                }
                return chunks;
            }

            // Check if we can do incremental update
            const canIncrement = !forceFullRedraw && !snapshot.full &&
                                cachedChunks.length === chunks.length &&
                                tbody.children.length === chunks.length &&
                                chunks.every((chunk, i) => cachedChunks[i].id === chunk.id
                                    && tbody.children[i].dataset.id === String(chunk.id));

            if (canIncrement) {
                // Incremental update - only update changed rows
                chunks.forEach((chunk, i) => {
                    const cached = cachedChunks[i];
                    if (!cached || cached.status !== chunk.status || getChunkAudioIdentity(cached) !== getChunkAudioIdentity(chunk)
                            || _driftKey(cached.drift) !== _driftKey(chunk.drift)) {
                        updateChunkRow(chunk);
                    }
                });
            } else {
                const editorFields = [
                    ['.chunk-text', 'text'], ['.chunk-instruct', 'instruct'],
                    ['.chunk-pause-after', 'pause_after'], ['select', 'speaker']
                ];
                const drafts = new Map();
                if (chunkSnapshotBook === refreshBook) {
                    const previous = new Map(cachedChunks.map(chunk => [String(chunk.id), chunk]));
                    for (const row of tbody.querySelectorAll?.('.chunk-row') || []) {
                        const chunk = previous.get(row.dataset.id);
                        if (!chunk?.uid) { continue; }
                        const controls = editorFields.map(([selector, key]) => {
                            const field = row.querySelector(selector);
                            if (!field) { return null; }
                            return {selector, value: field.value,
                                dirty: field.value !== String(chunk[key] ?? ''),
                                focused: field === document.activeElement,
                                selectionStart: field.selectionStart, selectionEnd: field.selectionEnd,
                                selectionDirection: field.selectionDirection};
                        }).filter(Boolean);
                        drafts.set(chunk.uid, {expanded: row.classList.contains('expanded'), controls});
                    }
                }
                // Keep an unchanged audition when rebuilding the surrounding rows.
                const playingAudio = Array.from(document.querySelectorAll('#chunks-table-body .chunk-audio'))
                    .find(audio => !audio.paused && !audio.ended);
                const playingChunk = playingAudio
                    ? cachedChunks.find(chunk => String(chunk.id) === playingAudio.dataset.id) : null;
                const retainedChunk = refreshBook === currentBookFilename && playingChunk?.uid && chunks.find(chunk => chunk.uid === playingChunk.uid
                    && chunk.audio_path && getChunkAudioIdentity(chunk) === getChunkAudioIdentity(playingChunk));
                const playbackTime = playingAudio?.currentTime;
                // Full redraw needed
                tbody.innerHTML = chunks.map(chunk => {
                    const statusColor = chunk.status === 'done' ? 'success' :
                                      chunk.status === 'generating' ? 'warning' :
                                      chunk.status === 'error' ? 'danger' : 'secondary';

                    const audioPlayer = chunk.audio_path ?
                        `<audio class="chunk-audio" data-id="${chunk.id}" controls src="${encodeURI(`/${chunk.audio_path}`)}?t=${encodeURIComponent(chunk.audio_revision || Date.now())}" style="width: 200px; height: 30px;" onplay="stopOthers(${chunk.id})"></audio>` :
                        '<span class="text-muted small">No audio</span>';

                    const actionArea = chunk.status === 'generating' ?
                        `<div class="progress" style="width: 100px; height: 20px;">
                            <div class="progress-bar progress-bar-striped progress-bar-animated bg-warning flex-grow-1" role="status" aria-label="Generating audio">Generating…</div>
                         </div>` :
                        `<button class="btn btn-sm btn-primary" onclick="generateChunk(${chunk.id})"><i class="fas fa-play"></i> Gen</button>`;

                    return `
                        <tr data-id="${chunk.id}" class="chunk-row">
                            <td class="text-center align-middle" style="white-space:nowrap;">
                                <button class="chunk-action-btn chunk-toggle-btn" onclick="toggleChunkExpand(this)" title="Expand/collapse"><i class="fas fa-chevron-down"></i></button><button class="chunk-action-btn" onclick="insertChunkAfter(${chunk.id})" title="Insert line below"><i class="fas fa-plus"></i></button><button class="chunk-action-btn" onclick="deleteChunk(${chunk.id})" title="Delete line"><i class="fas fa-trash" style="color:#dc3545;"></i></button><button class="chunk-action-btn" onclick="voiceChangesHere(${chunk.id})" title="Voice changes here: from this line on, this character sounds different (time skip, older)"><i class="fas fa-user-clock"></i></button>
                            </td>
                            <td>${buildSpeakerSelect(chunk)}</td>
                            <td><textarea class="form-control form-control-sm chunk-text" rows="2" onchange="updateChunk(${chunk.id}, 'text', this.value)">${escapeHtml(chunk.text)}</textarea></td>
                            <td>
                                <textarea class="form-control form-control-sm chunk-instruct" rows="2" onchange="updateChunk(${chunk.id}, 'instruct', this.value)" title="Delivery instruction (3-8 words)">${escapeHtml(chunk.instruct || '')}</textarea>
                                <div class="chunk-pause-row d-none mt-1 align-items-center gap-1">
                                    <small class="text-muted text-nowrap">Pause after (ms):</small>
                                    <input type="number" class="form-control form-control-sm chunk-pause-after" style="width:80px;" value="${chunk.pause_after ?? ''}" placeholder="default" min="0" step="50" onchange="updateChunk(${chunk.id}, 'pause_after', this.value === '' ? null : parseInt(this.value))">
                                </div>
                            </td>
                            <td><span class="badge bg-${statusColor}">${escapeHtml(chunk.status)}</span>${_driftBadge(chunk)}</td>
                            <td>
                                <div class="chunk-actions d-flex align-items-center gap-2">
                                    ${actionArea}
                                    ${audioPlayer}
                                </div>
                            </td>
                        </tr>
                    `;
                }).join('');
                for (const chunk of chunks) {
                    const draft = drafts.get(chunk.uid);
                    const row = draft && tbody.querySelector(`tr[data-id="${chunk.id}"]`);
                    if (!row) { continue; }
                    for (const saved of draft.controls) {
                        const field = row.querySelector(saved.selector);
                        if (!field) { continue; }
                        if (saved.dirty) {
                            if (saved.selector === 'select' && !Array.from(field.options).some(option => option.value === saved.value)) {
                                const option = document.createElement('option');
                                option.value = saved.value;
                                option.textContent = saved.value + ' (custom)';
                                field.appendChild(option);
                            }
                            field.value = saved.value;
                        }
                        if (saved.focused) {
                            field.focus({preventScroll: true});
                            if (typeof saved.selectionStart === 'number' && field.setSelectionRange) {
                                field.setSelectionRange(saved.selectionStart, saved.selectionEnd, saved.selectionDirection);
                            }
                        }
                    }
                    if (draft.expanded) { window.toggleChunkExpand(row.querySelector('.chunk-toggle-btn')); }
                }
                if (retainedChunk) {
                    const replacement = tbody.querySelector(`audio.chunk-audio[data-id="${retainedChunk.id}"]`);
                    if (replacement) {
                        playingAudio.dataset.id = String(retainedChunk.id);
                        playingAudio.setAttribute('onplay', `stopOthers(${retainedChunk.id})`);
                        playingAudio.addEventListener('loadedmetadata', () => {
                            if (refreshBook !== currentBookFilename || !playingAudio.isConnected) { return; }
                            playingAudio.currentTime = playbackTime;
                            void playingAudio.play().then(() => {
                                if (refreshBook !== currentBookFilename || !playingAudio.isConnected) { playingAudio.pause(); }
                            }).catch(() => {
                                showToast('The editor refreshed, but playback could not resume. Press Play on the chunk to continue listening.', 'warning');
                            });
                        }, {once: true});
                        replacement.replaceWith(playingAudio);
                        playingAudio.load();
                    }
                }
            }

            cachedChunks = chunks;
            chunkSnapshotBook = refreshBook;
            chunkSnapshotRevision = snapshot.revision;

            // If any chunk is generating, poll (without full redraw)
            if (!isRenderingAll && chunks.some(c => c.status === 'generating')) {
                loadChunksTimer = setTimeout(() => loadChunks(false), 2000);
            }

            return chunks;
        }

        window.toggleChunkExpand = (btn) => {
            const row = btn.closest('tr');
            const expanding = !row.classList.contains('expanded');
            row.classList.toggle('expanded');

            row.querySelectorAll('.chunk-text, .chunk-instruct').forEach(ta => {
                if (expanding) {
                    // Auto-size to content
                    ta.style.height = 'auto';
                    ta.style.height = ta.scrollHeight + 'px';
                    ta.style.overflow = 'visible';
                } else {
                    // Collapse back to 2 rows
                    ta.style.height = '';
                    ta.style.overflow = '';
                }
            });

            // Show/hide pause_after control
            row.querySelectorAll('.chunk-pause-row').forEach(el => {
                if (expanding) {
                    el.classList.remove('d-none');
                    el.classList.add('d-flex');
                } else {
                    el.classList.remove('d-flex');
                    el.classList.add('d-none');
                }
            });
        };

        window.insertChunkAfter = async (id) => {
            try {
                await API.post(`/api/chunks/${id}/insert`, {});
                await loadChunks(true);
            } catch (e) {
                showActionError("Failed to insert line", e, "Refresh the Editor and check whether the new line exists before inserting again.", "error");
            }
        };

        let _lastDeleted = null;
        let _undoTimer = null;
        let _undoToastSequence = 0;

        window.deleteChunk = async (id) => {
            try {
                const res = await fetch(`/api/chunks/${id}`, { method: 'DELETE' });
                await API._handleError(res);
                const data = await res.json();

                // Store for undo
                const toastId = 'toast-undo-' + Date.now() + '-' + (++_undoToastSequence);
                _lastDeleted = { chunk: data.deleted, at_index: id, undo_token: data.undo_token, toastId };
                clearTimeout(_undoTimer);

                // Show toast with undo action
                const container = document.getElementById('toast-container');
                const wrapper = document.createElement('div');
                wrapper.innerHTML = `
                    <div id="${toastId}" class="toast align-items-center text-white bg-warning border-0" role="alert">
                        <div class="d-flex">
                            <div class="toast-body text-dark"><span class="deleted-line-summary"></span>
                                <a href="#" class="ms-2 fw-bold text-dark">Undo</a>
                            </div>
                            <button type="button" class="btn-close me-2 m-auto" data-bs-dismiss="toast"></button>
                        </div>
                    </div>`;
                const el = wrapper.firstElementChild;
                el.querySelector('.deleted-line-summary').textContent =
                    `Line deleted (${data.deleted.speaker}: "${(data.deleted.text || '').substring(0, 40)}...")`;
                el.querySelector('a').addEventListener('click', (event) => {
                    event.preventDefault();
                    undoDeleteChunk(toastId);
                });
                container.appendChild(el);
                const toast = new bootstrap.Toast(el, { delay: 8000 });
                toast.show();
                el.addEventListener('hidden.bs.toast', () => { el.remove(); });

                // Clear undo data after timeout
                _undoTimer = setTimeout(() => { _lastDeleted = null; }, 8000);

                await loadChunks(true);
            } catch (e) {
                showActionError("Failed to delete line", e, "Refresh the Editor and check whether the line was removed before deleting again. Use Undo if the completed deletion is still offered.", "error");
            }
        };

        window.undoDeleteChunk = async (toastId) => {
            if (!_lastDeleted || _lastDeleted.toastId !== toastId) {
                showToast('Nothing to undo', 'warning');
                return;
            }

            const deleted = _lastDeleted;
            _lastDeleted = null;
            clearTimeout(_undoTimer);
            try {
                await API.post('/api/chunks/restore', {
                    chunk: deleted.chunk,
                    at_index: deleted.at_index,
                    undo_token: deleted.undo_token
                });

                // Dismiss the toast
                const el = document.getElementById(toastId);
                if (el) {
                    const toast = bootstrap.Toast.getInstance(el);
                    if (toast) { toast.hide(); }
                }

                if (_lastDeleted === deleted) {
                    _lastDeleted = null;
                    clearTimeout(_undoTimer);
                }
                showToast('Line restored', 'success');
                await loadChunks(true);
            } catch (e) {
                showActionError("Undo failed", e, "Refresh the Editor and check whether the deleted line was restored before using Undo again.", "error");
            }
        };

        window.stopOthers = (id) => {
            if (isPlayingSequence) { return; } // Sequence player handles its own logic
            document.querySelectorAll('audio').forEach(audio => {
                if (audio.dataset.id != id) {
                    audio.pause();
                }
            });
        };

        window.playSequence = async () => {
            isPlayingSequence = true;
            const btn = document.getElementById('btn-play-seq');
            btn.innerHTML = '<i class="fas fa-stop me-1"></i>Stop';
            btn.onclick = stopSequence;
            btn.classList.replace('btn-primary', 'btn-danger');

            let currentIndex = 0;
            let skippedCount = 0;

            const playNext = () => {
                if (!isPlayingSequence) { return; }

                const audios = Array.from(document.querySelectorAll('.chunk-audio'));

                // Find next valid audio
                while (currentIndex < audios.length) {
                    const audio = audios[currentIndex];
                    if (audio.getAttribute('src')) {
                        break;
                    }
                    currentIndex++;
                }

                if (currentIndex >= audios.length) {
                    stopSequence();
                    if (skippedCount > 0) {
                        showToast(`Play Sequence finished - ${skippedCount} chunk(s) skipped due to playback errors`, 'warning');
                    }
                    return;
                }

                const audio = audios[currentIndex];
                const tr = audio.closest('tr');

                // Visual feedback
                document.querySelectorAll('tr').forEach(r => r.classList.remove('table-primary'));
                tr.classList.add('table-primary');
                tr.scrollIntoView({ behavior: 'smooth', block: 'center' });

                // Guards against onerror and the play() promise rejection both
                // firing for this same audio element - only the first should
                // count as a skip and advance currentIndex, or both firing
                // would double the skip count and skip over the next track too.
                let advanced = false;
                const advanceOnce = (isSkip) => {
                    if (advanced) { return; }
                    advanced = true;
                    if (isSkip) { skippedCount++; }
                    currentIndex++;
                    playNext();
                };

                const playPromise = audio.play();

                if (playPromise !== undefined) {
                    playPromise.catch(e => {
                        console.error("Play failed (empty or skipped):", e);
                        advanceOnce(true);
                    });
                }

                audio.onended = () => {
                    advanceOnce(false);
                };

                audio.onerror = () => {
                     console.error("Audio error, skipping");
                     advanceOnce(true);
                }
            };

            playNext();
        };

        window.stopSequence = () => {
            isPlayingSequence = false;
            document.querySelectorAll('audio').forEach(a => {
                a.pause();
                a.currentTime = 0;
                a.onended = null;
            });
            document.querySelectorAll('tr').forEach(r => r.classList.remove('table-primary'));

            const btn = document.getElementById('btn-play-seq');
            if (btn) {
                btn.innerHTML = '<i class="fas fa-play me-1"></i>Play Sequence';
                btn.onclick = playSequence;
                btn.classList.replace('btn-danger', 'btn-primary');
            }
        };

        const pendingChunkEdits = new Map();
        const failedChunkEdits = new Map();
        let chunkEditsRevision = 0;

        function applyChunkEdits(id, data) {
            const previous = pendingChunkEdits.get(id);
            const captured = JSON.parse(JSON.stringify(data));
            chunkEditsRevision++;
            invalidateEditorIntegrity('Edits awaiting save');
            const request = (async () => {
                if (previous) { await previous.catch(() => {}); }
                try {
                    await API.post(`/api/chunks/${id}`, captured);
                    cachedChunks = cachedChunks.map(chunk => chunk.id === id ? { ...chunk, ...captured } : chunk);
                    // The server can normalize an edit without changing its revision.
                    chunkSnapshotRevision = null;
                    failedChunkEdits.delete(id);
                } catch (error) {
                    failedChunkEdits.set(id, error);
                    throw error;
                }
            })();
            pendingChunkEdits.set(id, request);
            request.finally(() => {
                if (pendingChunkEdits.get(id) === request) { pendingChunkEdits.delete(id); }
                void refreshEditorIntegrity();
            }).catch(() => {});
            return request;
        }

        async function flushChunkEdits() {
            while (pendingChunkEdits.size) {
                await Promise.allSettled(Array.from(pendingChunkEdits.values()));
            }
            if (failedChunkEdits.size) { throw failedChunkEdits.values().next().value; }
        }

        async function ensureEditorRenderSnapshot() {
            while (true) {
                // Includes the focused textarea whose onchange has not fired yet.
                await Promise.all(Array.from(document.querySelectorAll('#chunks-table-body tr[data-id]'))
                    .map(row => saveRowEdits(Number(row.dataset.id), true)));
                await flushChunkEdits();
                const revision = chunkEditsRevision;
                const chunks = await API.get('/api/chunks');
                if (revision === chunkEditsRevision) { return chunks; }
            }
        }

        window.updateChunk = async (id, field, value) => {
            try {
                const data = {};
                data[field] = value;
                await applyChunkEdits(id, data);
                // Don't reload entire table to preserve focus, but maybe update status badge if needed
                // For now, next loadChunks will show updated status (pending)
            } catch (e) {
                console.error("Update failed", e);
                showToast("Failed to update chunk", 'error');
            }
        };

        // Save all pending edits from a row before generation
        async function saveRowEdits(id, changedOnly = false) {
            const tr = document.querySelector(`tr[data-id="${id}"]`);
            if (!tr) { return; }

            const inputs = tr.querySelectorAll('input, textarea, select');
            const data = {};

            inputs.forEach(input => {
                const changeHandler = input.getAttribute('onchange');
                if (changeHandler) {
                    // Extract field name from onchange="updateChunk(id, 'field', this.value)"
                    const match = changeHandler.match(/updateChunk\(\d+,\s*'(\w+)'/);
                    if (match) {
                        data[match[1]] = input.value;
                    }
                }
            });

            // Coerce pause_after: empty string means clear the override
            if ('pause_after' in data) {
                data.pause_after = data.pause_after === '' ? null : parseInt(data.pause_after);
            }

            if (changedOnly && !pendingChunkEdits.has(id) && !failedChunkEdits.has(id)) {
                const cached = cachedChunks.find(chunk => chunk.id === id);
                if (cached) {
                    for (const field of Object.keys(data)) {
                        const saved = field === 'pause_after' ? (cached[field] ?? null) : (cached[field] ?? '');
                        if (data[field] === saved) { delete data[field]; }
                    }
                }
            }

            // Save all fields at once
            if (Object.keys(data).length > 0) {
                console.log(`Saving chunk ${id} with data:`, data);
                await applyChunkEdits(id, data);
                console.log(`Chunk ${id} saved successfully`);
            }
        }

        window.generateChunk = async (id) => {
            try {
                // First, save any pending edits in this row
                await saveRowEdits(id);

                // Skip empty lines
                const tr = document.querySelector(`tr[data-id="${id}"]`);
                if (tr) {
                    const textArea = tr.querySelector('.chunk-text');
                    if (textArea && !textArea.value.trim()) {
                        showToast('Cannot generate audio for an empty line', 'error');
                        return;
                    }
                }

                // Optimistic UI update
                if (tr) {
                    const statusBadge = tr.querySelector('.badge');
                    statusBadge.className = 'badge bg-warning';
                    statusBadge.innerText = 'generating';

                    // Replace button with progress bar
                    const container = tr.querySelector('.d-flex');
                    const btn = container.querySelector('button');
                    if (btn) {
                         const progressBar = document.createElement('div');
                         progressBar.className = 'progress';
                         progressBar.style.width = '100px';
                         progressBar.style.height = '20px';
                         progressBar.innerHTML = '<div class="progress-bar progress-bar-striped progress-bar-animated bg-warning flex-grow-1" role="status" aria-label="Generating audio">Generating…</div>';
                         container.replaceChild(progressBar, btn);
                    }
                }

                await API.post(`/api/chunks/${id}/generate`, {});

                // Start polling with incremental updates (no full redraw)
                setTimeout(() => loadChunks(false), 1000);
            } catch (e) {
                showActionError("Failed to start generation", e, "Check the audio task status and the line audio before starting again. If refused, review the selected voice and TTS configuration.", "error");
                loadChunks(true); // Revert UI with full redraw
            }
        };

        window.cancelRender = async (skipApi = false) => {
            isRenderingAll = false;
            document.getElementById('btn-batch-fast').style.display = 'inline-block';
            document.getElementById('btn-regen-all').style.display = 'inline-block';
            document.getElementById('btn-cancel-render').style.display = 'none';
            if (!skipApi) {
                await cancelTask('/api/cancel_audio', { onSuccess: async () => {
                    const byId = new Map(cachedChunks.map(chunk => [String(chunk.id), chunk]));
                    document.querySelectorAll('#chunks-table-body tr.table-info').forEach(row => {
                        const chunk = byId.get(row.dataset.id);
                        if (chunk) { updateChunkRow(chunk); }
                        row.classList.remove('table-info');
                    });
                    await loadChunks(false);
                } });
            }
        };

        window.startRender = (regenerateAll = false) => {
            const mode = document.getElementById('tts-mode').value;
            if (mode === 'external') {
                renderAll(regenerateAll);
            } else {
                renderBatchFast(regenerateAll);
            }
        };

        // Shared by renderAll/renderBatchFast - identical except the endpoint
        // and how each one's response describes what it started (the two
        // endpoints return different fields, so that can't be a static
        // template). See FIXED.md F-065.
        async function _runBatchRender(endpoint, regenerateAll, { label, describeStart }) {
            const batchBook = currentBookFilename;
            let rowsMarked = false;
            isRenderingAll = true;
            document.getElementById('btn-batch-fast').style.display = 'none';
            document.getElementById('btn-regen-all').style.display = 'none';
            document.getElementById('btn-cancel-render').style.display = 'inline-block';

            try {
                const chunks = await ensureEditorRenderSnapshot();
                await refreshDeliveryReview();
                let toProcess = (regenerateAll ? chunks : chunks.filter(c => c.status !== 'done'))
                    .filter(c => c.text && c.text.trim());

                if (toProcess.length === 0) {
                    showToast("No non-empty chunks to render!", 'warning');
                    cancelRender(true);
                    return;
                }

                if (regenerateAll) {
                    if (!await showConfirm(`Regenerate all ${toProcess.length} non-empty chunks? This will replace existing audio.`, {title: 'Replace all rendered audio?', actionLabel: 'Regenerate all', danger: true})) {
                        cancelRender(true);
                        return;
                    }
                    // Re-fetch after the user confirms - showConfirm only
                    // resolves on a click, so server-side chunk state can
                    // have changed in the meantime (e.g. another tab
                    // finished a chunk). Using the pre-confirm snapshot here
                    // could send indices for chunks that already moved on.
                    const freshChunks = await ensureEditorRenderSnapshot();
                    toProcess = freshChunks.filter(c => c.text && c.text.trim());
                }

                if (batchBook !== currentBookFilename) {
                    throw new Error('Active book changed; restart rendering for the current book.');
                }
                rowsMarked = true;
                // Mark all chunks as generating in UI
                const indices = toProcess.map(c => c.id);
                const selectedUids = new Map(toProcess.map(chunk => [chunk.id, chunk.uid]));
                for (const id of indices) {
                    const tr = document.querySelector(`tr[data-id="${id}"]`);
                    if (tr) {
                        tr.classList.add('table-info');
                        const badge = tr.querySelector('.badge');
                        if (badge) {
                            badge.className = 'badge bg-warning';
                            badge.innerText = 'generating';
                        }
                    }
                }

                const response = await API.post(endpoint, { indices });
                console.log(`${label} started: ${describeStart(response)}`);

                // Poll for completion via the shared engine - gets the
                // staleness guard and bounded-retry-then-toast behavior every
                // other poller in this file already has, instead of a 9th
                // bespoke setInterval loop with console-only error handling.
                _startPolling('render_batch', async () => {
                    const audio = await API.get('/api/status/audio');
                    if (typeof audio.running !== 'boolean') { throw new Error('Audio task status is unavailable.'); }
                    const chunks = await ensureChunkRefresh(false);
                    return { audio, chunks };
                }, {
                    intervalMs: 2000,
                    doneCheck: updated => !isRenderingAll || !updated.audio.running,
                    onDone: async (updated) => {
                        if (!isRenderingAll) { return; }
                        document.querySelectorAll('tr').forEach(r => r.classList.remove('table-info'));
                        cancelRender(true);

                        if (batchBook !== currentBookFilename) {
                            showToast('Render finished for a previous book. Open that book to check its audio.', 'warning');
                            return;
                        }
                        const selected = updated.chunks.filter(chunk => indices.includes(chunk.id)
                            && selectedUids.get(chunk.id) === chunk.uid);
                        const outcome = getBatchOutcome(selected, indices.length);
                        const label = outcome.complete ? 'complete' : 'incomplete';
                        showToast(`Batch ${label}: ${outcome.completed} succeeded, ${outcome.failed} failed, ${outcome.cancelled} cancelled, ${outcome.unfinished} unfinished`,
                            outcome.complete ? 'success' : 'warning');
                        if (outcome.completed > 0) {
                            runDriftCheck(selected.filter(chunk => chunk.status === 'done').map(chunk => chunk.id));
                        }
                    },
                });

            } catch (e) {
                console.error(`${label} error:`, e);
                showActionError("Error during batch rendering", e, "Check the audio task status and refreshed chunks before starting another render; some audio may already have been generated.", "error");
                cancelRender(true);
                if (rowsMarked) { await loadChunks(true); }
            }
        }

        window.renderAll = (regenerateAll = false) => _runBatchRender('/api/generate_batch', regenerateAll, {
            label: 'Batch generation',
            describeStart: r => `${r.total_chunks} chunks with ${r.workers} workers`
        });

        window.renderBatchFast = (regenerateAll = false) => _runBatchRender('/api/generate_batch_fast', regenerateAll, {
            label: 'Fast batch',
            describeStart: r => `${r.total_chunks} chunks (batch_size=${r.batch_size}, seed=${r.batch_seed})`
        });

        document.getElementById('btn-merge').addEventListener('click', async () => {
             try {
                 const confirmation = await ensureMergeIntegrityApproval();
                 if (confirmation === null) { return; }
                 await API.post('/api/merge', { integrity_confirmation: confirmation });
                 // Switch to Result tab and poll
                 document.querySelector('[data-tab="audio"]').click();
                 const cancelBtn = document.getElementById('btn-cancel-merge');
                 cancelBtn.style.display = '';
                 pollLogs('audio', 'audio-logs', () => { cancelBtn.style.display = 'none'; });
             } catch (e) {
                 showActionError("Merge failed", e, "Check the audio task status and Result output before merging again. Review any source-match refusal below.", "error");
             }
        });
        document.getElementById('btn-cancel-merge').addEventListener('click', async () => {
            await cancelTask('/api/cancel_audio');
        });


        // --- Audacity Export ---
        function isExportComplete(status) {
            return status.result?.status === 'done' && typeof status.result.message === 'string';
        }

        function pollExport(taskName) {
            const exports = {
                audacity_export: { statusId: 'audacity-status', url: '/api/export_audacity', filename: 'audacity_export.zip' },
                m4b_export: { statusId: 'm4b-status', cancelId: 'm4b-cancel-btn', url: '/api/audiobook_m4b', filename: 'audiobook.m4b' },
                chapter_export: { statusId: 'chapter-status', cancelId: 'chapter-cancel-btn' },
            };
            const config = exports[taskName];
            if (!config) { throw new Error('Unknown export task'); }
            claimTaskStart(taskName);
            const statusEl = document.getElementById(config.statusId);
            const cancelBtn = config.cancelId ? document.getElementById(config.cancelId) : null;
            statusEl.textContent = 'Exporting...';
            if (cancelBtn) { cancelBtn.style.display = ''; }
            _startPolling(taskName, () => API.get(`/api/status/${taskName}`), {
                doneCheck: status => !status.running,
                onTick: status => {
                    const last = status.logs[status.logs.length - 1] || '';
                    if (cancelBtn && (last.startsWith('Writing') || last.startsWith('Encoding M4B:'))) { statusEl.textContent = last; }
                },
                onDone: status => {
                    releaseTaskStart(taskName);
                    if (cancelBtn) { cancelBtn.style.display = 'none'; }
                    const result = status.result;
                    const message = typeof result?.message === 'string' ? result.message : 'Export result unavailable. Check the logs before downloading.';
                    const complete = isExportComplete(status);
                    if (!complete) {
                        statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(message)}</span>`;
                    } else if (taskName === 'chapter_export') {
                        statusEl.innerHTML = `<span class="text-success"><i class="fas fa-check me-1"></i>${escapeHtml(message)}</span>`;
                        loadChapterExports();
                    } else {
                        statusEl.innerHTML = '<span class="text-success"><i class="fas fa-check me-1"></i>Done!</span>';
                        const a = document.createElement('a');
                        a.href = `${config.url}?t=${Date.now()}`;
                        a.download = config.filename;
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        setTimeout(() => { statusEl.innerHTML = ''; }, 5000);
                    }
                },
            });
        }

        window.exportAudacity = async () => {
            if (!claimTaskStart('audacity_export')) { return; }
            const statusEl = document.getElementById('audacity-status');
            statusEl.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Exporting...</span>';

            try {
                await API.post('/api/export_audacity', {});

                pollExport('audacity_export');
            } catch (e) {
                releaseTaskStart('audacity_export');
                statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(getActionErrorMessage('Audacity export start is unconfirmed', e, 'Check the export task status and output list before starting again. If the server refused the request, review the validation details below.'))}</span>`;
            }
        };

        // --- Chapter-by-chapter export ---
        const CHAPTER_PRESETS_KEY = 'alexandria.chapter-template-presets';
        function getLocalStorageValue(key, fallback = null) {
            try { return localStorage.getItem(key) ?? fallback; } catch (e) { return fallback; }
        }
        function setLocalStorageValue(key, value) {
            try { localStorage.setItem(key, value); return true; } catch (e) { return false; }
        }
        function getChapterTemplatePresets() {
            try {
                const parsed = JSON.parse(getLocalStorageValue(CHAPTER_PRESETS_KEY, '{}'));
                return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
                    ? Object.assign(Object.create(null), parsed) : Object.create(null);
            } catch (e) { return Object.create(null); }
        }
        function renderChapterTemplatePresets() {
            const select = document.getElementById('chapter-template-preset');
            if (!select) { return; }
            const current = select.value;
            select.innerHTML = '<option value="">Saved presets</option>';
            Object.keys(getChapterTemplatePresets()).sort().forEach(name => {
                const option = document.createElement('option');
                option.value = name;
                option.textContent = name;
                select.appendChild(option);
            });
            select.value = current;
        }
        window.loadChapterTemplatePreset = function loadChapterTemplatePreset(name) {
            if (!name) { return; }
            const preset = getChapterTemplatePresets()[name];
            if (preset) {
                document.getElementById('chapter-template').value = preset.template || '';
                document.getElementById('chapter-padding').value = String(preset.padding ?? 2);
                document.getElementById('chapter-selection').value = preset.selection || '';
            }
        };
        window.saveChapterTemplatePreset = async function saveChapterTemplatePreset() {
            const getSnapshot = () => JSON.stringify({presets: getChapterTemplatePresets(),
                template: document.getElementById('chapter-template').value,
                padding: document.getElementById('chapter-padding').value,
                selection: document.getElementById('chapter-selection').value});
            const snapshot = getSnapshot();
            const values = await showPresetEditor({title: 'Save chapter filename preset', includeDescription: false});
            if (!values) { return; }
            if (getSnapshot() !== snapshot) {
                showToast('The chapter settings or presets changed. Review them before saving.', 'warning');
                return;
            }
            const {name} = values;
            const presets = getChapterTemplatePresets();
            if (Object.hasOwn(presets, name.trim())) {
                if (!await showConfirm(`Replace chapter filename preset "${name.trim()}"? Its saved template, padding and chapter selection will be replaced.`, {title: 'Replace chapter filename preset?', actionLabel: 'Replace preset', danger: true})) { return; }
                if (getSnapshot() !== snapshot) {
                    showToast('The chapter settings or presets changed. Review them before saving.', 'warning');
                    return;
                }
            }
            presets[name.trim()] = {
                template: document.getElementById('chapter-template').value.trim(),
                padding: parseInt(document.getElementById('chapter-padding').value, 10),
                selection: document.getElementById('chapter-selection').value.trim(),
            };
            if (setLocalStorageValue(CHAPTER_PRESETS_KEY, JSON.stringify(presets))) {
                renderChapterTemplatePresets();
                document.getElementById('chapter-template-preset').value = name.trim();
                showToast('Chapter filename preset saved.', 'success');
            } else { showToast('Could not save preset: browser storage is unavailable.', 'error'); }
        };
        let chapterPresetDeletePending = false;
        window.deleteChapterTemplatePreset = async function deleteChapterTemplatePreset() {
            if (chapterPresetDeletePending) { return; }
            const select = document.getElementById('chapter-template-preset');
            const name = select?.value;
            if (!name) { return; }
            const presets = getChapterTemplatePresets();
            if (!Object.hasOwn(presets, name)) { return; }
            const snapshot = JSON.stringify(presets);
            chapterPresetDeletePending = true;
            try {
                if (!await showConfirm(`Delete chapter filename preset "${name}"? This cannot be undone unless you exported your presets.`, {title: 'Delete chapter filename preset?', actionLabel: 'Delete preset', danger: true})) { return; }
                if (select.value !== name || JSON.stringify(getChapterTemplatePresets()) !== snapshot) {
                    showToast('The presets changed. Review the current selection before deleting.', 'warning');
                    return;
                }
                delete presets[name];
                if (setLocalStorageValue(CHAPTER_PRESETS_KEY, JSON.stringify(presets))) {
                    renderChapterTemplatePresets();
                    showToast('Chapter filename preset deleted.', 'success');
                } else { showToast('Could not delete preset: browser storage is unavailable.', 'error'); }
            } finally {
                chapterPresetDeletePending = false;
            }
        };
        window.exportChapterTemplatePresets = function exportChapterTemplatePresets() {
            const blob = new Blob([JSON.stringify(getChapterTemplatePresets(), null, 2)], {type: 'application/json'});
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = 'alexandria-chapter-presets.json';
            link.click();
            URL.revokeObjectURL(link.href);
        };
        window.importChapterTemplatePresets = function importChapterTemplatePresets(input) {
            const file = input.files?.[0];
            input.value = '';
            if (!file) { return; }
            const request = {};
            window._chapterPresetImportRequest = request;
            const isCurrent = () => window._chapterPresetImportRequest === request;
            if (file.size > 1048576) { showToast('Could not import presets: file exceeds the 1 MiB limit.', 'error'); return; }
            const reader = new FileReader();
            reader.onload = () => {
                if (!isCurrent()) { return; }
                try {
                    const imported = JSON.parse(reader.result);
                    if (!imported || typeof imported !== 'object' || Array.isArray(imported)) { throw new Error('expected an object'); }
                    const valid = Object.fromEntries(Object.entries(imported).filter(([name, preset]) =>
                        name.trim() && preset && typeof preset === 'object' && typeof preset.template === 'string'));
                    if (!Object.keys(valid).length) { throw new Error('no valid presets found'); }
                    const merged = {...getChapterTemplatePresets(), ...valid};
                    if (!setLocalStorageValue(CHAPTER_PRESETS_KEY, JSON.stringify(merged))) { throw new Error('browser storage is unavailable'); }
                    renderChapterTemplatePresets();
                    showToast(`Imported ${Object.keys(valid).length} chapter preset(s).`, 'success');
                } catch (e) { showActionError("Could not import presets", e, "Keep the preset file. Check that it contains valid chapter presets and that browser storage is available, then import again.", "error"); }
            };
            reader.onerror = () => {
                if (isCurrent()) { showToast('Could not import presets: file could not be read.', 'error'); }
            };
            reader.readAsText(file);
        };
        renderChapterTemplatePresets();
        function parseChapterSelection(value) {
            const text = value || '';
            if (text.length > 65536) { throw new Error('Chapter selection exceeds the 65,536-character input limit.'); }
            const selected = new Set();
            let expanded = 0;
            text.split(',').forEach(part => {
                const bits = part.trim().split('-').map(Number);
                if (bits.some(n => Number.isInteger(n) && !Number.isSafeInteger(n))) {
                    throw new Error('Chapter numbers must be safe whole numbers.');
                }
                let first, last;
                if (bits.length === 1 && Number.isSafeInteger(bits[0]) && bits[0] > 0) { first = last = bits[0]; }
                if (bits.length === 2 && Number.isSafeInteger(bits[0]) && Number.isSafeInteger(bits[1]) && bits[0] > 0 && bits[1] >= bits[0]) {
                    [first, last] = bits;
                }
                if (first === undefined) { return; }
                const count = last - first + 1;
                if (count > 10000 - expanded) { throw new Error('Chapter selection exceeds the 10,000-entry expansion limit.'); }
                expanded += count;
                for (let n = first; n <= last; n += 1) { selected.add(n - 1); }
            });
            return selected.size ? Array.from(selected).sort((a, b) => a - b) : null;
        }
        function chapterExportParams() {
            return {
                format: document.getElementById('chapter-format').value,
                per_chunk_chapters: document.getElementById('chapter-per-chunk').checked,
                template: document.getElementById('chapter-template').value.trim() || '{chapter_number} - {chapter_name}',
                padding: parseInt(document.getElementById('chapter-padding').value, 10),
                book_name: document.getElementById('chapter-book-name').value.trim(),
                series_name: document.getElementById('chapter-series-name').value.trim(),
                volume_number: document.getElementById('chapter-volume').value.trim(),
                chapters: parseChapterSelection(document.getElementById('chapter-selection').value),
                changed_only: document.getElementById('chapter-changed-only').checked,
                require_ready: document.getElementById('chapter-require-ready').checked
            };
        }
        function renderChapterList(rows, exported) {
            const el = document.getElementById('chapter-list');
            document.getElementById('chapter-zip-link').style.display = exported && rows.some(r => r.exists) ? '' : 'none';
            if (!rows.length) { el.innerHTML = `<span class="text-muted">${exported ? 'No exported chapter files found.' : 'No chapter files would be written.'}</span>`; return; }
            el.innerHTML = '<ol class="mb-0 ps-3">' + rows.map(r => {
                const name = escapeHtml(r.file);
                if (exported && r.exists) {
                    return `<li><a href="/api/chapter_exports/file/${encodeURIComponent(r.file)}" download="${name}">${name}</a> <span class="text-muted">${(r.bytes / 1048576).toFixed(1)} MB</span></li>`;
                }
                return `<li><span class="font-monospace">${name}</span></li>`;
            }).join('') + '</ol>';
        }
        async function loadChapterExports() {
            try {
                const m = await API.get('/api/chapter_exports');
                if (Array.isArray(m.chapters)) { renderChapterList(m.chapters, true); }
            } catch (e) { /* nothing exported yet */ }
        }
        async function getChapterExportPreview(params) {
            const q = new URLSearchParams({ format: params.format, per_chunk_chapters: params.per_chunk_chapters, template: params.template,
                padding: params.padding, book_name: params.book_name, series_name: params.series_name, volume_number: params.volume_number,
                changed_only: params.changed_only, require_ready: params.require_ready });
            if (params.chapters !== null) {
                params.chapters.forEach(index => q.append('chapters', index));
            }
            return API.get('/api/export_chapters/preview?' + q.toString());
        }
        let chapterPreviewRequest = 0;
        document.getElementById('chapter-preview-btn').addEventListener('click', async () => {
            const request = ++chapterPreviewRequest;
            const book = currentBookFilename;
            try {
                const params = chapterExportParams();
                const result = await getChapterExportPreview(params);
                if (request !== chapterPreviewRequest || book !== currentBookFilename ||
                    JSON.stringify(params) !== JSON.stringify(chapterExportParams())) { return; }
                renderChapterList(result.chapters, false);
                document.getElementById('chapter-status').textContent = `${result.chapters.length} chapter(s) would be written${params.changed_only ? ' with Changed only enabled' : ''}.`;
            } catch (e) {
                if (request === chapterPreviewRequest && book === currentBookFilename) {
                    showActionError("Preview failed", e, "Review the chapter range and filename template, then preview the export again.", "error");
                }
            }
        });
        document.getElementById('chapter-export-btn').addEventListener('click', async () => {
            if (!claimTaskStart('chapter_export')) { return; }
            chapterPreviewRequest += 1;
            const statusEl = document.getElementById('chapter-status');
            const cancelBtn = document.getElementById('chapter-cancel-btn');
            statusEl.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Exporting...</span>';
            cancelBtn.style.display = '';
            try {
                const book = currentBookFilename;
                const params = chapterExportParams();
                const preview = await getChapterExportPreview(params);
                if (book !== currentBookFilename || JSON.stringify(params) !== JSON.stringify(chapterExportParams())) {
                    throw new Error('The book or chapter settings changed. Preview the current selection before exporting.');
                }
                statusEl.textContent = `Exporting ${preview.chapters.length} chapter(s)${params.changed_only ? ' with Changed only enabled' : ''}…`;
                await API.post('/api/export_chapters', params);
                pollExport('chapter_export');
            } catch (e) {
                releaseTaskStart('chapter_export');
                cancelBtn.style.display = 'none';
                statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(getActionErrorMessage('Chapter export start is unconfirmed', e, 'Check the export task status and output list before starting again. If the server refused the request, review the validation details below.'))}</span>`;
            }
        });
        document.getElementById('chapter-cancel-btn').addEventListener('click', async () => {
            await cancelTask('/api/export_chapters/cancel');
        });
        loadChapterExports();

        // Handle M4B cover image upload
        document.getElementById('m4b-cover-input').addEventListener('change', async (e) => {
            const file = e.target.files[0];
            const statusEl = document.getElementById('m4b-cover-status');
            if (!file) { return; }
            const formData = new FormData();
            formData.append('file', file);
            try {
                const resp = await fetch('/api/m4b_cover', { method: 'POST', body: formData });
                if (!resp.ok) { throw new Error((await resp.json()).detail || resp.statusText); }
                statusEl.textContent = 'Uploaded';
                statusEl.className = 'small text-success';
            } catch (err) {
                console.error('Failed to upload M4B cover:', err);
                statusEl.textContent = err.message;
                statusEl.className = 'small text-danger';
            }
        });

        window.removeM4bCover = async () => {
            const statusEl = document.getElementById('m4b-cover-status');
            try {
                await API.del('/api/m4b_cover');
                document.getElementById('m4b-cover-input').value = '';
                statusEl.textContent = 'Removed';
                statusEl.className = 'small text-muted';
            } catch (err) {
                console.error('Failed to remove M4B cover:', err);
                statusEl.textContent = err.message;
                statusEl.className = 'small text-danger';
            }
        };

        window.cancelM4B = () => cancelTask('/api/merge_m4b/cancel');

        window.exportM4B = async () => {
            if (!claimTaskStart('m4b_export')) { return; }
            const statusEl = document.getElementById('m4b-status');
            const perChunk = document.getElementById('m4b-per-chunk').checked;
            statusEl.innerHTML = '<span class="text-info"><i class="fas fa-spinner fa-spin me-1"></i>Exporting M4B...</span>';

            try {
                await API.post('/api/merge_m4b', {
                    per_chunk_chapters: perChunk,
                    title: document.getElementById('m4b-title').value,
                    author: document.getElementById('m4b-author').value,
                    narrator: document.getElementById('m4b-narrator').value,
                    year: document.getElementById('m4b-year').value,
                    description: document.getElementById('m4b-description').value,
                    require_ready: document.getElementById('m4b-require-ready').checked
                });

                pollExport('m4b_export');
            } catch (e) {
                releaseTaskStart('m4b_export');
                statusEl.innerHTML = `<span class="text-danger"><i class="fas fa-times me-1"></i>${escapeHtml(getActionErrorMessage('M4B export start is unconfirmed', e, 'Check the export task status and output list before starting again. If the server refused the request, review the validation details below.'))}</span>`;
            }
        };

        // --- Polling Logic ---
        // Shared polling engine: every hand-rolled status poller in this file
        // used to implement its own setInterval/setTimeout + try/catch with a
        // different, undocumented error policy (some retried forever
        // silently, some gave up on the first error, one showed a toast and
        // gave up). This is the ONE consistent policy: retry silently up to
        // MAX_SILENT_ERRORS times, then a visible-but-non-blocking warning
        // toast (polling keeps going either way - nothing here needs "give
        // up forever", since every poller's done-condition is server-driven).
        // Also generalizes pollLogs's stale-response generation-counter guard
        // to any poller. See FIXED.md F-053/058/072/073/079.
        const _pollGen = {};
        function _startPolling(key, fetchFn, { intervalMs = 1000, doneCheck, onTick, onDone, immediate = true, pauseWhenHidden = false, displayLabel = null } = {}) {
            const myGen = (_pollGen[key] = (_pollGen[key] || 0) + 1);
            let consecutiveErrors = 0;
            const MAX_SILENT_ERRORS = 3;
            let pending = false;
            const tick = async () => {
                if (myGen !== _pollGen[key] || pending) { return; }
                if (pauseWhenHidden && document.hidden) {
                    setTimeout(tick, intervalMs);
                    return;
                }
                pending = true;
                try {
                    const data = await fetchFn();
                    if (myGen !== _pollGen[key]) { return; }
                    consecutiveErrors = 0;
                    if (onTick) { await onTick(data); }
                    if (doneCheck(data)) {
                        if (onDone) { onDone(data); }
                        return;
                    }
                } catch (e) {
                    consecutiveErrors++;
                    console.error(`Poll error (${key}):`, e);
                    // Re-toast every MAX_SILENT_ERRORS failures, not just the
                    // first time the threshold is crossed - a permanently
                    // broken endpoint would otherwise warn once and then go
                    // silent for the rest of the failure streak.
                    if (consecutiveErrors % MAX_SILENT_ERRORS === 0) {
                        const taskKey = key.replace(/^reattach:/, '');
                        const label = displayLabel || (typeof TASK_LABELS !== 'undefined' && TASK_LABELS[taskKey]) || 'Task';
                        showToast(`Having trouble reaching Alexandria for ${label} status updates. It is still retrying and has not cancelled the run from this page. Save your work, check that Alexandria is still running, then reopen the page if this continues.`, 'warning');
                    }
                } finally {
                    pending = false;
                }
                if (myGen === _pollGen[key]) { setTimeout(tick, intervalMs); }
            };
            if (immediate) { tick(); } else { setTimeout(tick, intervalMs); }
            // Callers that need to stop a poll before its own doneCheck fires
            // (e.g. a user-initiated cancel) can call the returned function -
            // it just bumps the generation counter, which the next tick (in
            // flight or scheduled) will see and exit on.
            return () => { _pollGen[key] = (_pollGen[key] || 0) + 1; };
        }

        // A run can pause ITSELF (retries exhausted with "pause and wait for me"
        // set), so the Pause/Resume button follows the server's `paused` flag
        // rather than only its own clicks.
        const PAUSE_BUTTON_FOR_TASK = { script: 'btn-pause-script', batch_script: 'btn-pause-batch-script',
                                        review: 'btn-pause-review', batch_review: 'btn-pause-batch-review',
                                        nicknames: 'btn-pause-nick', voicelab: 'btn-vl-pause' };
        const _autoPauseNotified = {};
        function syncPauseButton(taskName, status) {
            const btn = document.getElementById(PAUSE_BUTTON_FOR_TASK[taskName] || '');
            if (!btn) { return; }
            const showsResume = btn.classList.contains('btn-outline-success');
            if (status.paused && !showsResume) {
                btn.innerHTML = '<i class="fas fa-play me-1"></i>Resume';
                btn.classList.remove('btn-outline-warning');
                btn.classList.add('btn-outline-success');
                if (!_autoPauseNotified[taskName] && status.logs.some(l => l.includes('[AUTO-PAUSE]'))) {
                    _autoPauseNotified[taskName] = true;
                    showToast(`${TASK_LABELS[taskName] || taskName} paused itself: retries ran out. Fix the provider, then press Resume.`, 'warning', 8000);
                    notifyJobDone(taskName, 'Paused: API retries ran out. Press Resume when the provider is back.', 'paused');
                }
            } else if (!status.paused && showsResume) {
                _resetPauseBtn(PAUSE_BUTTON_FOR_TASK[taskName]);
                _autoPauseNotified[taskName] = false;
            }
            if (!status.running) { _autoPauseNotified[taskName] = false; }
        }

        // What a run is waiting on, and for how long. A slow model can take
        // minutes per reply and the log window stops moving; the log's own
        // markers ("Step 2 (speakers): window 2/4", "Retrying... (attempt 3
        // of 4)") say what is in flight, and the clock since the log last
        // grew says how long. Client-side only (#588).
        const ACTIVITY_MARKER = /Step \d \([a-z]+\): |Retrying\.\.\.|Reviewing batch \d+\/\d+|Progress: \d+\/\d+/;
        const ACTIVITY_QUIET_MS = 10000;
        function renderActivity(el, status, track) {
            if (!el) { return; }
            if (!status.running) { el.hidden = true; return; }
            const logs = status.logs || [];
            const now = Date.now();
            if (logs.length !== track.count) { track.count = logs.length; track.changedAt = now; }
            let marker = null, retry = null;
            for (let i = logs.length - 1; i >= 0 && !(marker && retry !== null); i--) {
                const line = logs[i];
                if (marker === null && ACTIVITY_MARKER.test(line) && !line.startsWith('Retrying')) { marker = line.trim(); }
                if (retry === null && line.startsWith('Retrying')) { retry = line.trim(); }
                if (marker !== null && line.startsWith('Step')) { break; }
            }
            const quietMs = now - (track.changedAt || now);
            const waitingForReply = !!status.manual_request;
            const paused = !!status.paused;
            const parts = [paused ? 'Paused (use Resume to continue)' : waitingForReply ? 'Waiting for your reply (manual reply panel at the top of the page)' : 'Working'];
            if (marker) { parts.push(marker); }
            if (retry && (!marker || logs.lastIndexOf(retry) > logs.lastIndexOf(marker))) { parts.push(retry); }
            if (!paused && !waitingForReply && status.eta && status.eta.eta_seconds != null) { parts.push(`about ${formatDuration(status.eta.eta_seconds)} left`); }
            if (!paused && !waitingForReply && quietMs >= ACTIVITY_QUIET_MS) { parts.push(`waiting on the model for ${formatDuration(quietMs / 1000)}`); }
            el.textContent = parts.join(' \u00b7 ');
            el.hidden = false;
        }

        // Every task that renders into the Script tab's log window gets the
        // activity line above it.
        // The Save-snapshot button follows the run, not the path that started
        // it (#612): Generate showed it, Resume failed run never did, and the
        // manual-recovery flow goes through Resume. One rule - visible while
        // the script task is running - applied on every status tick.
        function syncSnapshotButton(status) {
            const snap = document.getElementById('btn-snapshot-script');
            if (snap) { snap.style.display = status && status.running ? 'inline-block' : 'none'; }
        }

        function pollScriptLogs(taskName, onDone) {
            return pollLogs(taskName, 'script-logs', status => {
                syncSnapshotButton({ running: false });
                if (taskName === 'script') { clearScriptCancellation('script'); }
                if (onDone) { onDone(status); }
            }, 'script-activity');
        }

        // Manual transport (#593): the run is waiting for the user to answer a
        // request. /api/status/<task> says which one (id + where the run is);
        // the full prompt is fetched only when the id changes.
        let _manualShown = null;
        let _manualTask = null;
        let _manualRequested = null;
        let _manualLoadWarningFor = null;
        let _manualReplyFor = null;
        let _manualReplySubmitting = null;
        let _manualReplyAcknowledged = null;
        let _manualAcknowledgedText = null;
        async function renderManualRequest(status, taskName) {
            const panel = document.getElementById('manual-llm-panel');
            if (!panel) { return; }
            const req = status.running ? status.manual_request : null;
            if (!req) {
                if (_manualTask !== taskName) { return; }
                _manualTask = null;
                _manualRequested = null;
                _manualShown = null;
                window._manualPending = null;
                const reply = document.getElementById('manual-llm-reply');
                const retained = document.getElementById('manual-llm-retained-replies');
                panel.hidden = !reply.value.trim() && !retained.children.length;
                document.getElementById('manual-llm-submit').disabled = true;
                reply.disabled = false;
                document.getElementById('manual-llm-status').textContent = 'The pipeline is no longer waiting for this request. Any reply text is retained below; check the task activity before continuing.';
                return;
            }
            _manualTask = taskName;
            _manualRequested = req.id;
            if (_manualShown === req.id) { return; }
            let full;
            try { full = (await API.get('/api/manual_llm/pending')).pending; } catch (e) {
                if (_manualRequested === req.id && _manualTask === taskName && _manualLoadWarningFor !== req.id && _manualShown !== req.id) {
                    _manualLoadWarningFor = req.id;
                    document.getElementById('manual-llm-status').textContent = 'Could not load this model request. Automatic checks will continue; if it does not appear, check that Alexandria is still running.';
                }
                console.error('Failed to load manual model request:', e);
                return;
            }
            if (!full || full.id !== req.id || _manualRequested !== req.id ||
                _manualTask !== taskName || _manualShown === req.id) { return; }
            _manualShown = req.id;
            window._manualPending = full;
            document.getElementById('manual-llm-title').textContent = `request ${full.sequence}`;
            document.getElementById('manual-llm-hint').textContent = full.stage_hint || '';
            document.getElementById('manual-llm-prompt').textContent = manualPromptText(full);
            const reply = document.getElementById('manual-llm-reply');
            if (reply.value.trim() && _manualReplyFor !== full.id && (_manualReplyAcknowledged !== _manualReplyFor || reply.value !== _manualAcknowledgedText)) {
                const retained = document.createElement('details');
                const title = document.createElement('summary');
                title.textContent = 'Retained reply from an earlier request';
                const draft = document.createElement('textarea');
                draft.value = reply.value;
                draft.readOnly = true;
                draft.className = 'form-control form-control-sm';
                draft.setAttribute('aria-label', 'Retained earlier model reply');
                retained.append(title, draft);
                document.getElementById('manual-llm-retained-replies').appendChild(retained);
            }
            _manualReplyFor = full.id;
            _manualLoadWarningFor = null;
            reply.value = '';
            reply.disabled = false;
            document.getElementById('manual-llm-submit').disabled = false;
            panel.hidden = false;
            document.getElementById('manual-llm-status').textContent = `Your turn: request ${full.sequence}. Paste the model’s answer and submit it. Earlier unsent replies, if any, are retained below.`;
            const message = `The pipeline needs your reply to request ${full.sequence}. Use the manual reply panel at the top of the page.`;
            showToast(message, 'warning', 8000);
            notifyJobDone(taskName, message, 'needs your reply');
        }

        function manualPromptText(req) {
            return (req.messages || []).map(m => `${(m.role || '').toUpperCase()}:\n${m.content}`).join('\n\n');
        }

        async function copyManualPrompt() {
            const req = window._manualPending;
            if (!req) { return; }
            await copyToClipboard(manualPromptText(req), 'Prompt');
        }

        async function submitManualReply() {
            const req = window._manualPending;
            const reply = document.getElementById('manual-llm-reply');
            const btn = document.getElementById('manual-llm-submit');
            if (_manualReplySubmitting) { return; }
            if (!req || req.id !== _manualRequested || _manualReplyFor !== req.id) {
                showToast('This request is no longer waiting. Your reply is still here; check the task activity.', 'warning');
                return;
            }
            if (!reply.value.trim()) { showToast('Paste the answer first.', 'warning'); return; }
            const submittedText = reply.value;
            _manualReplySubmitting = req.id;
            btn.disabled = true;
            reply.disabled = true;
            try {
                await API.post('/api/manual_llm/response', { id: req.id, content: submittedText });
                _manualReplyAcknowledged = req.id;
                _manualAcknowledgedText = submittedText;
                if (window._manualPending?.id === req.id) {
                    document.getElementById('manual-llm-status').textContent = `Reply to request ${req.sequence} sent. The pipeline will validate it; wait for the next task update.`;
                }
                showToast(`Reply to request ${req.sequence} sent to the pipeline.`, 'success');
            } catch (e) {
                console.error('Failed to submit manual model reply:', e);
                if (window._manualPending?.id !== req.id) { return; }
                const message = 'Could not confirm submission. Your text is still here. Check the task activity; if it is still waiting for this request, select Submit reply again.';
                document.getElementById('manual-llm-status').textContent = message;
                showToast(message, 'error');
                btn.disabled = false;
                reply.disabled = false;
            } finally {
                _manualReplySubmitting = null;
            }
        }

        let finalAudioRequest = 0;
        async function loadFinalAudio() {
            const request = ++finalAudioRequest;
            const player = document.getElementById('audio-player-container');
            const empty = document.getElementById('audio-empty-state');
            try {
                const response = await fetch('/api/audiobook', { method: 'HEAD', cache: 'no-store' });
                if (request !== finalAudioRequest) { return; }
                if (!response.ok && response.status !== 404) {
                    throw new Error(`Audiobook check failed (${response.status})`);
                }
                player.style.display = response.ok ? 'block' : 'none';
                empty.style.display = response.ok ? 'none' : '';
                empty.textContent = 'No final audiobook is loaded here yet. In Editor, choose Merge All to build the MP3.';
                const audio = document.getElementById('main-audio');
                const download = document.getElementById('download-link');
                if (response.ok) {
                    audio.src = `/api/audiobook?t=${Date.now()}`;
                    download.href = audio.src;
                } else {
                    audio.pause();
                    audio.removeAttribute('src');
                    download.removeAttribute('href');
                }
            } catch (error) {
                if (request !== finalAudioRequest) { return; }
                console.error('Failed to check final audiobook:', error);
                empty.style.display = '';
                empty.textContent = 'Could not check the final audiobook. Check the connection and reopen Result to retry.';
            }
        }

        async function pollLogs(taskName, elementId, onDone, activityId) {
            const el = document.getElementById(elementId);
            const activityEl = activityId ? document.getElementById(activityId) : null;
            const track = { count: -1, changedAt: Date.now() };
            const renderLogs = createTaskLogRenderer(el);
            _startPolling(`logs:${taskName}`, () => API.get(`/api/status/${taskName}`), {
                doneCheck: status => !status.running,
                onTick: status => {
                    renderLogs(status);
                    syncPauseButton(taskName, status);
                    if (taskName === 'script') { syncSnapshotButton(status); }
                    renderActivity(activityEl, status, track);
                    if (activityId) { renderManualRequest(status, taskName); }
                },
                onDone: status => {
                    if (activityEl) { activityEl.hidden = true; }
                    if (activityId) { renderManualRequest({ running: false }, taskName); }
                    notifyJobDone(taskName, '', 'finished', status);
                    if (onDone) { onDone(status); }
                    if (taskName === 'audio' && getTaskCompletionOutcome(status) === 'finished' && status.logs.some(l => l.includes("complete"))) {
                        loadFinalAudio();
                    }
                    // Refresh editor chunks when script generation or review completes
                    if ((taskName === 'script' || taskName === 'review') && status.logs.some(l => l.includes("completed successfully"))) {
                        // Clear cached chunks table so next load shows fresh data
                        const tbody = document.getElementById('chunks-table-body');
                        if (tbody) { tbody.innerHTML = ''; }
                        // If editor tab is visible, refresh immediately
                        if (document.getElementById('editor-tab').style.display !== 'none') {
                            loadChunks();
                        }
                    }
                }
            });
        }
