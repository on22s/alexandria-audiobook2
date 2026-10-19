/* Preview only a bounded prefix; importing/starting remains an explicit action. */
window.createAudioChannelPicker = (container, file, purpose) => {
    container.replaceChildren();
    const label = document.createElement('label');
    label.className = 'form-label small';
    label.textContent = 'Audio channel';
    const select = document.createElement('select');
    select.className = 'form-select form-select-sm mb-2';
    select.setAttribute('aria-label', `Audio channel for ${file.name}`);
    for (const [value, text] of [['mix', 'Mixed (default)'], ['left', 'Left'], ['right', 'Right']]) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = text;
        select.appendChild(option);
    }
    label.appendChild(select);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm btn-outline-secondary';
    button.textContent = 'Preview first 10 seconds';
    const audio = document.createElement('audio');
    audio.controls = true;
    audio.preload = 'none';
    audio.hidden = true;
    audio.className = 'mt-2 mw-100';
    audio.setAttribute('aria-label', `Channel preview for ${file.name}`);
    const status = document.createElement('div');
    status.className = 'form-text';
    status.setAttribute('role', 'status');
    status.textContent = 'Choose a channel, then preview. This does not start TTS or preparation.';
    container.append(label, button, audio, status);
    let token = null;
    let staging = null;
    let closed = false;
    let disabled = false;
    let previewActive = false;
    let sequence = 0;
    let objectUrl = null;
    let controller = null;
    const removeToken = async value => {
        if (value) {
            await fetch(`/api/audio_channels/stage/${encodeURIComponent(value)}`, {method: 'DELETE'}).catch(() => {});
        }
    };
    const stop = () => {
        sequence++;
        controller?.abort();
        audio.pause();
        audio.removeAttribute('src');
        audio.hidden = true;
        if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
    };
    const stage = async () => {
        if (token) { return token; }
        if (!staging) {
            const body = new FormData();
            body.append('file', file);
            body.append('purpose', purpose);
            staging = (async () => {
                const response = await fetch('/api/audio_channels/stage', {method: 'POST', body});
                await API._handleError(response);
                const result = await response.json();
                if (closed) { await removeToken(result.token); throw new Error('Preview closed.'); }
                token = result.token;
                return token;
            })().finally(() => { staging = null; });
        }
        return staging;
    };
    select.onchange = () => { stop(); previewActive = false; button.disabled = disabled; status.textContent = 'Channel changed. Preview again before continuing.'; };
    button.onclick = async () => {
        if (disabled) { return; }
        stop();
        previewActive = true;
        const expected = sequence;
        const channel = select.value;
        controller = new AbortController();
        const signal = controller.signal;
        button.disabled = true;
        status.textContent = 'Uploading and decoding a short preview…';
        try {
            const value = await stage();
            if (closed || expected !== sequence) { return; }
            const response = await fetch(`/api/audio_channels/preview?token=${encodeURIComponent(value)}&channel=${encodeURIComponent(channel)}`, {signal});
            await API._handleError(response);
            const blob = await response.blob();
            if (closed || expected !== sequence) { return; }
            objectUrl = URL.createObjectURL(blob);
            audio.src = objectUrl;
            audio.hidden = false;
            status.textContent = `${response.headers.get('X-Audio-Channels') || '?'} source channel(s). Preview uses the selected channel; full import/start remains separate.`;
        } catch (error) {
            if (!closed && expected === sequence && error.name !== 'AbortError') { status.textContent = error.message || 'Preview failed. Select the file again if it expired.'; }
        } finally {
            if (!closed && expected === sequence) { previewActive = false; button.disabled = disabled; }
        }
    };
    const dispose = () => {
        closed = true;
        stop();
        void removeToken(token);
        container.replaceChildren();
        window.removeEventListener('pagehide', dispose);
    };
    window.addEventListener('pagehide', dispose);
    return {
        getSelection: () => ({channel: select.value, token, pending: staging}),
        setDisabled: value => { disabled = !!value; select.disabled = disabled; button.disabled = disabled || previewActive; },
        dispose,
    };
};

window.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('prep-audio-file');
    const container = document.getElementById('prep-channel-picker');
    input?.addEventListener('change', () => {
        window.preparerChannelPicker?.dispose();
        window.preparerChannelPicker = null;
        if (input.files[0]) { window.preparerChannelPicker = window.createAudioChannelPicker(container, input.files[0], 'preparer'); }
    });
});
