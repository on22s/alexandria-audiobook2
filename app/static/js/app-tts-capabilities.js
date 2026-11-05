/* Server-owned dispatch capabilities; mode previews never save or replace voices. */
let ttsCapabilityModes = null;
function setTtsCapabilityModes(modes) {
    ttsCapabilityModes = modes && typeof modes === 'object' ? modes : null;
    clearTtsExternalCapabilitiesReport('No current saved-server check. Use Check saved server to inspect this configuration.');
    applyTtsCapabilityGuidance();
}
function applyTtsCapabilityGuidance() {
    const external_panel = document.getElementById('tts-external-capabilities-panel');
    if (external_panel) { external_panel.hidden = document.getElementById('tts-mode')?.value !== 'external'; }
    const mode = document.getElementById('tts-mode')?.value;
    const capability = ttsCapabilityModes?.[mode];
    const summary = document.getElementById('tts-capability-summary');
    if (!capability?.controls || !capability?.voice_types) {
        if (summary) { summary.textContent = 'Backend capabilities unavailable. Existing settings are preserved; reload configuration to retry.'; }
        for (const card of document.querySelectorAll('.voice-card')) {
            const notice = card.querySelector('.tts-capability-notice');
            if (notice) {
                notice.hidden = false;
                notice.textContent = 'Backend capabilities unavailable. The assignment is kept; server validation still applies.';
            }
            for (const input of card.querySelectorAll('.voice-type')) { input.removeAttribute('aria-description'); }
        }
        return;
    }
    if (summary) {
        summary.textContent = `${capability.description} ${capability.language_note} AA2 returns completed audio files; playback is not incremental. Mode edits apply to generation only after Save Configuration.`;
    }
    for (const [control, id] of Object.entries({server: 'tts-url-group', external_timeout: 'tts-external-timeout-group', device: 'tts-device-group', local_options: 'tts-local-options'})) {
        const field = document.getElementById(id);
        if (field) { field.style.display = capability.controls[control] ? '' : 'none'; }
    }
    for (const card of document.querySelectorAll('.voice-card')) {
        const selected = card.querySelector('.voice-type:checked');
        let notice = card.querySelector('.tts-capability-notice');
        if (!notice) {
            notice = document.createElement('p');
            notice.className = 'tts-capability-notice small text-warning mt-2';
            notice.setAttribute('role', 'status');
            card.appendChild(notice);
        }
        const supported = selected && capability.voice_types[selected.value] === true;
        notice.hidden = !selected || supported;
        notice.textContent = selected && !supported ? `This voice type is unsupported by the selected ${mode} mode. Its assignment is kept. Choose a supported voice or switch mode and save before generating. Ensemble members and version timelines are also checked by the server.` : '';
        for (const input of card.querySelectorAll('.voice-type')) {
            const supportedType = capability.voice_types[input.value] === true;
            input.setAttribute('aria-description', supportedType ? 'Supported by the selected TTS mode.' : 'Requires another TTS mode. The saved selection is preserved.');
        }
    }
}
let ttsExternalCapabilitiesEpoch = 0;
let ttsExternalCapabilitiesListenersAttached = false;
function getTtsExternalCapabilitiesSettings(mode, url, pool, timeout) {
    if (typeof mode !== 'string' || typeof url !== 'string' || !Array.isArray(pool) || !pool.every(entry => typeof entry === 'string')) { return null; }
    return {mode, url: url.trim(), externalUrls: pool.map(entry => entry.trim()).filter(Boolean), externalTimeoutSeconds: Number(timeout)};
}
function captureTtsExternalCapabilitiesSnapshot() {
    const mode_element = document.getElementById('tts-mode');
    const url_element = document.getElementById('tts-url');
    const pool_element = document.getElementById('tts-external-urls');
    const number_element = document.getElementById('tts-external-server-number');
    const pool_text = pool_element && typeof pool_element.value === 'string' ? pool_element.value : '';
    const server_number = number_element && typeof number_element.value === 'string' && number_element.value.trim() ? Number(number_element.value) : 1;
    const settings = getTtsExternalCapabilitiesSettings(mode_element?.value || '', url_element?.value || '', pool_text.split(/\r?\n/), getNumFieldValue('tts-external-timeout', 300, true));
    return {...settings, serverIndex: Number.isInteger(server_number) ? server_number - 1 : Number.NaN};
}
function ttsExternalCapabilitiesListsMatch(first_list, second_list) {
    return Array.isArray(first_list) && Array.isArray(second_list) && first_list.length === second_list.length && first_list.every((entry, position) => entry === second_list[position]);
}
function ttsExternalCapabilitiesSnapshotsMatch(first_snapshot, second_snapshot) {
    return first_snapshot.mode === second_snapshot.mode && first_snapshot.url === second_snapshot.url && ttsExternalCapabilitiesListsMatch(first_snapshot.externalUrls, second_snapshot.externalUrls) && Object.is(first_snapshot.externalTimeoutSeconds, second_snapshot.externalTimeoutSeconds) && first_snapshot.serverIndex === second_snapshot.serverIndex;
}
function ttsExternalCapabilitiesSettingsMatch(settings_value, snapshot_value) {
    if (!settings_value || typeof settings_value !== 'object') { return false; }
    const settings = getTtsExternalCapabilitiesSettings(settings_value.mode, settings_value.url, settings_value.external_urls, settings_value.external_timeout_seconds);
    return !!settings && ttsExternalCapabilitiesSnapshotsMatch({...settings, serverIndex: snapshot_value.serverIndex}, snapshot_value);
}
function clearTtsExternalCapabilitiesReport(message_text) {
    ttsExternalCapabilitiesEpoch += 1;
    const status_element = document.getElementById('tts-external-capabilities-status');
    const results_element = document.getElementById('tts-external-capabilities-results');
    if (results_element) {
        if (typeof results_element.replaceChildren === 'function') { results_element.replaceChildren(); }
        results_element.textContent = '';
    }
    if (status_element) { status_element.textContent = message_text || 'No saved server check has run.'; }
}
function attachTtsExternalCapabilitiesListeners() {
    if (ttsExternalCapabilitiesListenersAttached) { return; }
    ttsExternalCapabilitiesListenersAttached = true;
    const field_ids = ['tts-mode', 'tts-url', 'tts-external-urls', 'tts-external-timeout', 'tts-external-server-number'];
    for (const field_id of field_ids) {
        const field_element = document.getElementById(field_id);
        if (field_element && typeof field_element.addEventListener === 'function') {
            const clear_stale = () => { clearTtsExternalCapabilitiesReport('Saved server check cleared because settings changed. Save Configuration, then use Check saved server.'); };
            field_element.addEventListener('input', clear_stale);
            field_element.addEventListener('change', clear_stale);
        }
    }
}
function validateTtsExternalCapabilitiesReport(report_value) {
    if (!report_value || typeof report_value !== 'object' || typeof report_value.fingerprint !== 'string' || !report_value.fingerprint) { return false; }
    if (!report_value.operations || typeof report_value.operations !== 'object') { return false; }
    if (!Array.isArray(report_value.unsupported_operations) || !report_value.unsupported_operations.every(entry => typeof entry === 'string')) { return false; }
    for (const operation_name of ['custom', 'clone']) {
        const operation_value = report_value.operations[operation_name];
        if (!operation_value || typeof operation_value !== 'object') { return false; }
        if (!['supported', 'unsupported', 'unknown'].includes(operation_value.status)) { return false; }
        if (!(operation_value.endpoint === null || typeof operation_value.endpoint === 'string')) { return false; }
        if (!(operation_value.adapter === null || operation_value.adapter === undefined || typeof operation_value.adapter === 'string')) { return false; }
        if (!(operation_value.error === null || operation_value.error === undefined || typeof operation_value.error === 'string')) { return false; }
        if (!Array.isArray(operation_value.parameters) || !operation_value.parameters.every(entry => typeof entry === 'string')) { return false; }
        if (!Array.isArray(operation_value.missing) || !operation_value.missing.every(entry => typeof entry === 'string')) { return false; }
        if (operation_value.status === 'supported' && (!operation_value.endpoint || !operation_value.adapter || !operation_value.parameters.length || operation_value.missing.length)) { return false; }
    }
    return true;
}
function renderTtsExternalCapabilitiesReport(report_value) {
    const results_element = document.getElementById('tts-external-capabilities-results');
    if (!results_element) { return; }
    if (typeof results_element.replaceChildren === 'function') { results_element.replaceChildren(); }
    results_element.textContent = '';
    const heading_element = document.createElement('p');
    heading_element.textContent = 'Schema compatibility report. This is not reachability, audio production, or live qualification. Generation revalidates every call.';
    results_element.appendChild(heading_element);
    const fingerprint_element = document.createElement('p');
    fingerprint_element.textContent = 'Fingerprint: ' + report_value.fingerprint;
    results_element.appendChild(fingerprint_element);
    for (const operation_name of ['custom', 'clone']) {
        const operation_value = report_value.operations[operation_name];
        const operation_element = document.createElement('p');
        const required_text = operation_value.parameters.length ? operation_value.parameters.join(', ') : 'none reported';
        const missing_text = operation_value.missing.length ? operation_value.missing.join(', ') : 'none reported';
        operation_element.textContent = operation_name + ': status ' + operation_value.status + '; endpoint ' + (operation_value.endpoint || 'not reported') + '; adapter ' + (operation_value.adapter || 'not reported') + '; required fields ' + required_text + '; missing fields ' + missing_text;
        if (operation_value.error) { operation_element.textContent += '; ' + operation_value.error; }
        results_element.appendChild(operation_element);
    }
    const unsupported_element = document.createElement('p');
    unsupported_element.textContent = 'Unsupported server operations: ' + (report_value.unsupported_operations.length ? report_value.unsupported_operations.join(', ') : 'none reported') + '. AA2 ensembles can still render supported members separately.';
    results_element.appendChild(unsupported_element);
}
async function checkTtsExternalCapabilities() {
    attachTtsExternalCapabilitiesListeners();
    const check_button = document.getElementById('tts-external-capabilities-check');
    if (check_button?.disabled) { return; }
    const snapshot_value = captureTtsExternalCapabilitiesSnapshot();
    const status_element = document.getElementById('tts-external-capabilities-status');
    const results_element = document.getElementById('tts-external-capabilities-results');
    if (!Number.isInteger(snapshot_value.serverIndex) || snapshot_value.serverIndex < 0) {
        clearTtsExternalCapabilitiesReport('Enter a saved server number of 1 or higher, then use Check saved server.');
        return;
    }
    const request_epoch = ++ttsExternalCapabilitiesEpoch;
    check_button?.setAttribute('disabled', '');
    if (results_element) { results_element.textContent = ''; }
    if (status_element) { status_element.textContent = 'Checking saved server schema…'; }
    try {
        const response_value = await API.post('/api/tts/external-capabilities', {server_index: snapshot_value.serverIndex});
        if (request_epoch !== ttsExternalCapabilitiesEpoch) { return; }
        const current_snapshot = captureTtsExternalCapabilitiesSnapshot();
        if (!response_value || response_value.server_index !== snapshot_value.serverIndex || !ttsExternalCapabilitiesSnapshotsMatch(snapshot_value, current_snapshot) || !ttsExternalCapabilitiesSettingsMatch(response_value.settings, current_snapshot)) {
            clearTtsExternalCapabilitiesReport('Save Configuration first. The saved server check result does not match the current unsaved form, so no compatibility report is shown.');
            return;
        }
        if (!validateTtsExternalCapabilitiesReport(response_value.capabilities)) {
            if (results_element) { results_element.textContent = ''; }
            if (status_element) { status_element.textContent = 'Saved server returned an invalid compatibility report. No report is shown.'; }
            return;
        }
        renderTtsExternalCapabilitiesReport(response_value.capabilities);
        const unknown = Object.values(response_value.capabilities.operations).some(operation => operation.status === 'unknown');
        if (status_element) { status_element.textContent = unknown ? 'Saved server schema could not be fully verified. See unknown operations and diagnostics below.' : 'Saved server schema check completed. Schema compatibility only; generation revalidates every call.'; }
    } catch (_error_value) {
        if (request_epoch !== ttsExternalCapabilitiesEpoch) { return; }
        if (results_element) { results_element.textContent = ''; }
        const messages = {400: 'Check the saved server number and external mode, then Save Configuration before checking again.', 409: 'Another server check is running or saved settings changed. Wait, Save Configuration if needed, then check again.', 503: 'Server worker cleanup failed. Resolve cleanup before checking again.'};
        if (status_element) { status_element.textContent = messages[_error_value?.status] || 'Saved server check failed. Confirm the saved server is reachable and exposes a schema, then try again.'; }
    } finally {
        check_button?.removeAttribute('disabled');
    }
}
attachTtsExternalCapabilitiesListeners();
if (document && typeof document.addEventListener === 'function') {
    document.addEventListener('DOMContentLoaded', attachTtsExternalCapabilitiesListeners);
}
