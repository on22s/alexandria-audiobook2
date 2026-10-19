/* Server-owned dispatch capabilities; mode previews never save or replace voices. */
let ttsCapabilityModes = null;
function setTtsCapabilityModes(modes) {
    ttsCapabilityModes = modes && typeof modes === 'object' ? modes : null;
    applyTtsCapabilityGuidance();
}
function applyTtsCapabilityGuidance() {
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
