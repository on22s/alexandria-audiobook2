// Retained asset library; choosing an audition never changes selected dialogue.
const SoundAssets = (() => {
    let sequence = 0;
    let upload = null;
    const element = id => document.getElementById(id);
    const status = message => { element('sound-assets-status').textContent = message; };
    const make = (tag, text, className = '') => {
        const node = document.createElement(tag);
        node.textContent = text;
        node.className = className;
        return node;
    };
    function validate(data) {
        if (!data || !Array.isArray(data.assets) || data.assets.length > 256) throw new Error('Invalid asset library response.');
        const identities = new Set();
        for (const row of data.assets) {
            if (!row || typeof row.asset_id !== 'string' || !/^[0-9a-f]{32}$/.test(row.asset_id)
                || identities.has(row.asset_id) || !['ready', 'incomplete'].includes(row.state)
                || (row.display_name !== undefined && (typeof row.display_name !== 'string' || row.display_name.length > 256))) {
                throw new Error('Invalid asset library entry.');
            }
            identities.add(row.asset_id);
            const audio = row.canonical;
            if (row.state === 'ready' && (!audio || !/^[0-9a-f]{64}$/.test(audio.sha256)
                || !Number.isSafeInteger(audio.frames) || audio.frames < 1
                || !Number.isInteger(audio.frame_rate) || audio.frame_rate < 1 || audio.frame_rate > 192000
                || audio.frames > audio.frame_rate * 86400
                || ![1, 2].includes(audio.channels) || audio.sample_width !== 4)) throw new Error('Invalid audio details.');
        }
        return data.assets;
    }
    function render(rows) {
        const list = element('sound-assets-list');
        list.querySelectorAll('audio').forEach(audio => { audio.pause(); audio.removeAttribute('src'); audio.load(); });
        list.replaceChildren();
        if (!rows.length) list.append(make('p', 'No sound assets yet.', 'small'));
        for (const row of rows) {
            const card = make('div', '', 'border rounded p-2 my-2');
            const name = row.display_name || 'Sound asset';
            card.append(make('strong', name));
            if (row.state === 'incomplete') {
                card.append(make('p', 'Import incomplete. Its reserved storage remains charged until cleanup.', 'small'));
                const cleanup = make('button', 'Clean up incomplete import', 'btn btn-sm btn-outline-danger');
                cleanup.type = 'button'; cleanup.dataset.helpKey = 'soundAssetsCleanup';
                cleanup.onclick = async () => {
                    if (!window.confirm(`Remove the incomplete import “${name}”?`)) return;
                    cleanup.disabled = true;
                    try {
                        const response = await fetch(`/api/sound_design/assets/${row.asset_id}/purge_incomplete`, {method: 'POST'});
                        await API._handleError(response);
                        status('Incomplete import cleaned up.');
                    } catch (error) { status(error.message); }
                    finally { await refresh(false); }
                };
                card.append(cleanup);
            } else {
                const facts = row.canonical;
                card.append(make('p', `${(facts.frames / facts.frame_rate).toFixed(2)} seconds · ${facts.channels === 1 ? 'Mono' : 'Stereo'}`, 'small mb-1'));
                const label = make('label', 'Start at seconds: ', 'small me-2');
                const start = document.createElement('input');
                start.type = 'number'; start.min = '0'; start.step = '0.01'; start.value = '0';
                start.max = String((facts.frames - 1) / facts.frame_rate);
                start.className = 'form-control form-control-sm'; start.dataset.helpKey = 'soundAssetsStart';
                label.append(start);
                const play = make('button', 'Listen for up to 10 seconds', 'btn btn-sm btn-outline-secondary');
                play.type = 'button'; play.dataset.helpKey = 'soundAssetsListen';
                const audio = document.createElement('audio');
                audio.controls = true; audio.preload = 'none'; audio.hidden = true; audio.className = 'mw-100 mt-2';
                audio.setAttribute('aria-label', `Audition ${name}`);
                play.onclick = () => {
                    const seconds = Number(start.value);
                    const frame = Math.round(seconds * facts.frame_rate);
                    if (!start.value.trim() || !Number.isFinite(seconds) || seconds < 0 || frame >= facts.frames) {
                        status('Choose a start time within this sound.'); return;
                    }
                    audio.pause();
                    audio.src = `/api/sound_design/assets/${row.asset_id}/audition?sha256=${facts.sha256}&start_frame=${frame}`;
                    audio.hidden = false;
                    const expected = sequence;
                    audio.play().catch(() => { if (expected === sequence) status('Could not play the audition. Refresh the library and try again.'); });
                };
                const rendered = sequence;
                audio.onerror = () => { if (rendered === sequence) status('Audition could not be verified or played. Refresh the library.'); };
                card.append(label, play, audio);
            }
            list.append(card);
        }
    }
    async function refresh(announce = true) {
        const expected = ++sequence;
        try {
            const response = await fetch('/api/sound_design/assets');
            await API._handleError(response);
            const rows = validate(await response.json());
            if (expected !== sequence) return;
            render(rows);
            if (announce) status('Asset library refreshed.');
        } catch (error) { if (expected === sequence) status(error.message); }
    }
    async function importFile() {
        if (upload) return;
        const file = element('sound-assets-file').files[0];
        if (!file) { status('Choose an audio file first.'); return; }
        upload = new AbortController();
        element('sound-assets-import').disabled = true;
        element('sound-assets-cancel').hidden = false;
        status('Importing and verifying audio…');
        const body = new FormData(); body.append('file', file);
        try {
            const response = await fetch('/api/sound_design/assets/import', {method: 'POST', body, signal: upload.signal});
            await API._handleError(response);
            status('Sound asset ready.');
        } catch (error) {
            status(error.name === 'AbortError' ? 'Cancellation requested. Cleanup may still be running; refresh again before retrying.' : error.message);
        } finally {
            upload = null;
            element('sound-assets-import').disabled = false;
            element('sound-assets-cancel').hidden = true;
            await refresh(false);
        }
    }
    return {refresh, importFile, validate, cancel: () => upload?.abort()};
})();
