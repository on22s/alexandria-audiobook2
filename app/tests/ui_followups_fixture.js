// Real CSS, audio decode, click/key handlers, and synthetic WAV only.
(async () => {
    const output = document.createElement('pre');
    output.id = 'followup-results';
    output.textContent = JSON.stringify({error: 'Synthetic audio check has not completed'});
    document.body.appendChild(output);
    try {
        document.querySelectorAll('.tab-content').forEach(node => { node.style.display = 'none'; });
        document.getElementById('navbarNav').classList.add('show');
        const wrap = document.getElementById('editor-line-waveform-wrap');
        document.body.appendChild(wrap);
        const canvas = document.getElementById('editor-line-waveform');
        const audio = document.createElement('audio');
        audio.className = 'chunk-audio';
        audio.preload = 'auto';
        audio.src = SYNTHETIC_WAV;
        document.body.appendChild(audio);
        await new Promise((resolve, reject) => {
            audio.onloadedmetadata = resolve;
            audio.onerror = () => reject(Error('Synthetic WAV metadata failed'));
            audio.load();
        });
        const target = {chunk: {uid: 'synthetic', audio_revision: 'one'}, row: {querySelector: () => audio}};
        window.getEditorReviewTarget = () => target;
        await renderEditorLineWaveform(target);
        if (canvas.hidden) { throw Error(document.getElementById('editor-line-waveform-status').textContent); }
        const rect = canvas.getBoundingClientRect();
        canvas.dispatchEvent(new MouseEvent('click', {clientX: rect.left + rect.width * .5}));
        const clickSeek = Math.abs(audio.currentTime - audio.duration / 2) < .05;
        canvas.dispatchEvent(new KeyboardEvent('keydown', {key: 'End', cancelable: true}));
        const keyboardSeek = Math.abs(audio.currentTime - audio.duration) < .05;
        const positionAnnounced = Number(canvas.getAttribute('aria-valuenow')) === Math.round(audio.duration);
        const states = ['rendered', 'pending', 'flagged', 'failed'];
        const table = document.createElement('table');
        table.className = 'table';
        table.innerHTML = '<tbody id="followup-rows">' + states.map(state => `<tr data-render-state="${state}"><td>${state}</td></tr>`).join('') + '</tbody>';
        // The production rules deliberately scope the stripes to Editor.
        const originalBody = document.getElementById('chunks-table-body');
        originalBody.id = 'unused-original-body';
        table.firstElementChild.id = 'chunks-table-body';
        document.body.appendChild(table);
        const rows = THEMES.map(theme => {
            applyTheme(theme.key);
            const build = document.getElementById('sys-build');
            const colors = [...table.querySelectorAll('td')].map(cell => getComputedStyle(cell).borderLeftColor);
            return {theme: theme.key,
                buildVisible: getComputedStyle(build).display !== 'none',
                badges: parseFloat(getComputedStyle(build).borderRadius) > 0,
                distinctStates: new Set(colors).size === states.length,
                clickSeek, keyboardSeek, positionAnnounced,
                slider: canvas.getAttribute('role') === 'slider',
                cardShadow: getComputedStyle(document.querySelector('.card')).boxShadow !== 'none'};
        });
        output.textContent = JSON.stringify({rows});
    } catch (error) {
        output.textContent = JSON.stringify({error: String(error.stack || error)});
    }
    document.body.appendChild(output);
})();
