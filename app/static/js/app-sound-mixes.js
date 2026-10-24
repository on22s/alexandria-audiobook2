// Full mixed candidates are private until explicit review and delivery checks.
const SoundMixes = (() => {
    let sequence = 0, busy = false, context = null, profiles = null;
    const el = id => document.getElementById(id);
    const box = () => el('sound-mixes-content');
    const message = value => { el('sound-mixes-status').textContent = value; };
    const make = (tag, text = '') => { const node = document.createElement(tag); node.textContent = text; return node; };
    const digest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
    const integer = (value, low, high) => Number.isSafeInteger(value) && value >= low && value <= high;
    function button(label, callback) {
        const node = make('button', label); node.type = 'button'; node.className = 'btn btn-sm btn-outline-secondary me-2 my-1'; node.onclick = callback; return node;
    }
    function stopAudio() {
        box().querySelectorAll('audio').forEach(audio => { audio.onended = null; audio.pause(); audio.removeAttribute('src'); audio.load(); });
    }
    function setBusy(value) {
        busy = value;
        box().querySelectorAll('button,input,select,textarea').forEach(node => { node.disabled = value; });
        el('sound-mixes-refresh').disabled = value;
    }
    async function json(url, body) {
        const response = await fetch(url, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        await API._handleError(response); return response.json();
    }
    function validate(data) {
        if (!data || !Array.isArray(data.candidates) || data.candidates.length > 16) { throw new Error('Invalid candidate inventory.'); }
        if (!data.delivery_profiles || Object.keys(data.delivery_profiles).sort().join(',') !== 'm4b,mp3,wav') { throw new Error('Invalid delivery profiles.'); }
        for (const profile of Object.values(data.delivery_profiles)) {
            if (!profile || typeof profile.available !== 'boolean'
                || (profile.available ? !digest(profile.revision) : profile.revision !== null)
                || typeof profile.summary !== 'string' || !profile.summary.length || profile.summary.length > 1024) { throw new Error('Invalid delivery profile.'); }
        }
        const seen = new Set();
        for (const row of data.candidates) {
            if (!row || typeof row.stage !== 'string' || !/^render-[A-Za-z0-9_-]{1,128}$/.test(row.stage) || seen.has(row.stage)
                || !digest(row.recovery_revision) || !integer(row.bytes, 0, 4 * 1024 ** 3) || row.media_verified !== false
                || !['incomplete', 'invalid_metadata', 'metadata_ready'].includes(row.status)
                || !['absent', 'invalid', 'recorded'].includes(row.approval_status)) { throw new Error('Invalid candidate inventory.'); }
            seen.add(row.stage);
            if (row.status === 'metadata_ready' && (!digest(row.candidate_revision) || !digest(row.context_revision)
                || typeof row.book_id !== 'string' || row.book_id.length > 2048
                || !integer(row.frame_rate, 1, 192000) || !integer(row.frames, 1, row.frame_rate * 86400))) { throw new Error('Invalid candidate timing.'); }
            if (row.approval_status === 'recorded' && (!digest(row.approval_revision) || !row.delivery || !['mp3','wav','m4b'].includes(row.delivery.format))) { throw new Error('Invalid approval record.'); }
        }
        return data.candidates;
    }
    async function refresh() {
        if (busy) { return; }
        const token = ++sequence; context = null; profiles = null; stopAudio(); box().replaceChildren(); message('Loading saved mixes…');
        try {
            // The cue snapshot needs idle admission; do not race it with the
            // candidate listing's audio task claim.
            let snapshot = null;
            try { snapshot = await json('/api/sound_design/cues?offset=0&limit=1'); } catch (_) { /* Saved candidates remain recoverable without an active book. */ }
            if (token !== sequence) { return; }
            if (snapshot !== null && !digest(snapshot?.context_revision)) { throw new Error('Invalid current book context.'); }
            const inventory = await json('/api/sound_design/candidates');
            const rows = validate(inventory);
            if (token !== sequence) { return; }
            context = snapshot?.context_revision || null;
            profiles = inventory.delivery_profiles;
            if (context) {
                box().append(button('Render full mix', () => action(async () => {
                    await json('/api/sound_design/candidates/render', {context_revision: context});
                })));
            }
            for (const row of rows) { renderRow(row, token); }
            message('Saved mixes loaded. Playback verifies audio; approval rechecks current sources and takes.');
        } catch (error) {
            if (token === sequence) { context = null; box().replaceChildren(); message(error.message); }
        }
    }
    async function action(work, onSuccess = null) {
        if (busy) { return; }
        const token = sequence; stopAudio(); setBusy(true); let failure = null, result = null;
        try { result = await work(); } catch (error) { failure = error.message; }
        finally { setBusy(false); }
        if (token === sequence) {
            const loading = refresh(), refreshToken = sequence;
            await loading;
            if (refreshToken !== sequence) { return; }
            if (failure) { message(`${failure} Refresh saved mixes before retrying.`); }
            else if (onSuccess) { onSuccess(result); }
        }
    }
    async function deliver(row, confirmation = null) {
        const response = await fetch(`/api/sound_design/candidates/${row.stage}/deliver`, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
                candidate_revision: row.candidate_revision, approval_revision: row.approval_revision,
                integrity_confirmation: confirmation})});
        if (response.status === 409) {
            const report = (await response.json())?.detail;
            if (report && ['differences', 'unavailable'].includes(report.status) && digest(report.snapshot)
                && Array.isArray(report.hunks) && report.hunks.length <= 100000
                && (report.reason === undefined || typeof report.reason === 'string')) {
                for (const hunk of report.hunks) {
                    if (!hunk || !['source_before', 'source_words', 'source_after', 'script_words'].every(key => typeof hunk[key] === 'string')) { throw new Error('Invalid source comparison.'); }
                }
                return {sourceReport: report};
            }
            throw new Error('Delivery is stale, busy or unavailable.');
        }
        await API._handleError(response);
        const result = await response.json(), quality = result?.quality;
        if (result?.status !== 'published' || result.file !== `mixed_audiobook.${row.delivery.format}`
            || result.candidate_revision !== row.candidate_revision || result.approval_revision !== row.approval_revision
            || !Array.isArray(result.warnings) || result.warnings.length > 20 || result.warnings.some(w => typeof w !== 'string' || w.length > 2048)
            || (quality !== null && (!quality || quality.status !== 'verified' || !digest(quality.sha256)
                || !integer(quality.output_bytes, 1, 4 * 1024 ** 3)))) { throw new Error('Delivery response could not be verified. Check the saved output before retrying.'); }
        return result;
    }
    function showDelivery(row, result) {
        if (result.sourceReport) {
            const report = result.sourceReport, panel = make('div'); panel.className = 'border rounded p-2 my-2';
            panel.append(make('strong', 'Review source text before delivery'));
            const differences = report.status === 'unavailable' ? (report.reason || 'Original source comparison is unavailable.')
                : report.hunks.map(h => `Source: ${h.source_before} [${h.source_words}] ${h.source_after}\nMixed dialogue: ${h.script_words}`).join('\n\n');
            const text = make('pre', differences); text.style.whiteSpace = 'pre-wrap'; text.style.maxHeight = '40vh'; text.style.overflow = 'auto'; panel.append(text);
            const label = make('label', ' I reviewed this source comparison and want to deliver this exact text. '), consent = make('input');
            consent.type = 'checkbox'; consent.checked = false; label.append(consent); panel.append(label);
            panel.append(button('Confirm source text and deliver', () => {
                if (!consent.checked) { message('Review the source comparison and check its confirmation.'); return; }
                return action(() => deliver(row, report.snapshot), result => showDelivery(row, result));
            }));
            box().append(panel); message('Source review is required; no delivery was published.'); return;
        }
        message(`Published ${result.file}.${result.warnings.length ? ' ' + result.warnings.join(' ') : ''}`);
        if (result.quality !== null) {
            const link = make('a', `Download ${row.delivery.format.toUpperCase()} mixed audiobook`);
            link.href = `/api/sound_design/delivery/${row.delivery.format}/download?sha256=${result.quality.sha256}`;
            link.download = result.file; link.className = 'btn btn-sm btn-primary my-2'; box().append(link);
        }
    }
    function renderRow(row, token) {
        const card = make('div'); card.className = 'border rounded p-2 my-2';
        card.append(make('strong', row.status === 'metadata_ready' ? `Book: ${row.book_id}` : 'Incomplete or damaged mix'));
        card.append(make('p', `${(row.bytes / 1024 ** 2).toFixed(1)} MiB. ${row.approval_status === 'recorded' ? 'Approval recorded; delivery must recheck current sources and settings.' : 'No valid approval recorded.'}`));
        if (row.status === 'metadata_ready') {
            if (row.approval_status === 'recorded') {
                card.append(button(`Deliver saved ${row.delivery.format.toUpperCase()} approval`, () => action(
                    () => deliver(row), result => showDelivery(row, result))));
            }
            const duration = row.frames / row.frame_rate;
            card.append(make('p', `Duration: ${duration.toFixed(2)} seconds. Playback continues in verified sections of up to 30 seconds.`));
            const label = make('label', 'Start listening at seconds '), start = make('input');
            start.type = 'number'; start.min = '0'; start.max = String(duration); start.step = 'any'; start.value = '0'; label.append(start); card.append(label);
            const audio = make('audio'); audio.controls = true; audio.preload = 'none'; let next = 0;
            function playFrame(first) {
                if (token !== sequence || busy) { return; }
                const last = Math.min(row.frames, first + row.frame_rate * 30); next = last;
                audio.src = `/api/sound_design/candidates/${row.stage}/audition?candidate_revision=${row.candidate_revision}&start_frame=${first}&end_frame=${last}`;
                audio.play().catch(() => { if (token === sequence) { message('Use the audio play control to begin listening.'); } });
            }
            audio.onended = () => { if (next < row.frames) { playFrame(next); } };
            audio.onerror = () => { if (token === sequence) { message('Playback could not be verified. Refresh before retrying.'); } };
            card.append(button('Listen', () => {
                const seconds = Number(start.value);
                if (!start.value.trim() || !Number.isFinite(seconds) || seconds < 0 || seconds >= duration) { message('Choose a start within the mix.'); return; }
                playFrame(Math.floor(seconds * row.frame_rate));
            }), audio);
            const formatLabel = make('label', 'Delivery format '), format = make('select');
            for (const name of ['mp3', 'wav', 'm4b']) {
                const option = make('option', name.toUpperCase() + (profiles[name].available ? '' : ' (unavailable)'));
                option.value = name; option.disabled = !profiles[name].available; format.append(option);
            }
            format.value = ['mp3', 'wav', 'm4b'].find(name => profiles[name].available) || 'mp3'; formatLabel.append(format); card.append(formatLabel);
            const profile = make('p', profiles[format.value].summary); card.append(profile);
            card.append(make('p', 'Approval uses default output-quality checks and no loudness mastering. Changing delivery settings requires a new approval.'));
            const reviewLabel = make('label', ' I reviewed the full mixed candidate and approve it with these settings.'), reviewed = make('input');
            reviewed.type = 'checkbox'; reviewed.checked = false; reviewLabel.append(reviewed); card.append(reviewLabel);
            const metadata = make('div'); metadata.style.display = 'none'; metadata.className = 'border rounded p-2 my-2';
            const fields = {};
            for (const [key, label] of [['title', 'Title'], ['author', 'Author'], ['narrator', 'Narrator'], ['year', 'Year'], ['description', 'Description']]) {
                const wrapper = make('label', `${label} `), input = make(key === 'description' ? 'textarea' : 'input');
                wrapper.className = 'd-block my-1'; input.className = 'form-control form-control-sm';
                if (key === 'description') { input.rows = 3; } else { input.type = 'text'; }
                input.maxLength = 20000; input.value = key === 'title' ? 'Audiobook' : '';
                input.setAttribute('data-m4b-field', key); input.oninput = () => { reviewed.checked = false; };
                fields[key] = input; wrapper.append(input); metadata.append(wrapper);
            }
            const chaptersLabel = make('label', ' Make each dialogue chunk a chapter '), perChunk = make('input');
            perChunk.type = 'checkbox'; perChunk.checked = false; perChunk.onchange = () => { reviewed.checked = false; };
            perChunk.setAttribute('data-m4b-field', 'per_chunk_chapters'); chaptersLabel.append(perChunk); metadata.append(chaptersLabel);
            metadata.append(make('p', 'Chapter boundaries use the approved mix timeline. These entered tags are part of this approval.'));
            const cover = createSoundMixCoverControls(() => { reviewed.checked = false; }, () => token === sequence && !busy);
            metadata.append(cover.element);
            card.append(metadata);
            format.onchange = () => {
                profile.textContent = profiles[format.value].summary; reviewed.checked = false;
                metadata.style.display = format.value === 'm4b' ? '' : 'none';
            };
            card.append(button('Record full-mix approval', () => {
                if (!profiles[format.value]?.available) { message('Choose an available delivery format before approval.'); return; }
                if (!reviewed.checked) { message('Review the full mix and check the confirmation before approval.'); return; }
                const delivery = {format: format.value, export_quality: null, mastering: null};
                if (format.value === 'm4b') {
                    delivery.m4b = Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.value]));
                    delivery.m4b.per_chunk_chapters = perChunk.checked;
                    try { Object.assign(delivery.m4b, cover.getSelection()); }
                    catch (error) { message(error.message); return; }
                }
                return action(() => json(`/api/sound_design/candidates/${row.stage}/approve`, {
                    candidate_revision: row.candidate_revision, expected_approval: row.approval_revision || null,
                    expected_encoding: profiles[format.value].revision,
                    full_mix_reviewed: true, delivery}));
            }));
        }
        card.append(button('Discard saved mix', () => {
            if (!confirm('Permanently discard this saved mix and its approval? Original dialogue takes and imported sounds are kept.')) { return; }
            return action(() => json(`/api/sound_design/candidates/${row.stage}/discard?recovery_revision=${row.recovery_revision}`, {}));
        }));
        box().append(card);
    }
    function invalidate() {
        sequence += 1; context = null; profiles = null;
        stopAudio(); box().replaceChildren();
        message('Book or cue history changed. Refresh saved mixes and verify the full mix before delivery.');
    }
    return {refresh, invalidate};
})();
