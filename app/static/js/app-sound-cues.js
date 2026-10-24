// Versioned cue authoring. Saving metadata never approves or replaces audio.
const SoundCues = (() => {
    let sequence = 0, busy = false, view = null, assets = [], offset = 0, useSequence = 0;
    const el = id => document.getElementById(id);
    const message = text => { el('sound-cues-status').textContent = text; };
    const digest = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
    const boundedText = (value, max) => typeof value === 'string' && value.length <= max * 2 && Array.from(value).length <= max;
    const text = (value, max) => boundedText(value, max) && value.length > 0;
    const make = (tag, label = '') => { const node = document.createElement(tag); node.textContent = label; return node; };
    function button(label, action, help) {
        const node = make('button', label); node.type = 'button';
        node.className = 'btn btn-sm btn-outline-secondary me-2 my-1';
        node.title = help; node.onclick = action; return node;
    }
    function setBusy(value) {
        busy = value;
        el('sound-cues-content').querySelectorAll('button,input,select').forEach(node => { node.disabled = value; });
        el('sound-cues-refresh').disabled = value;
    }
    function same(a, b) { return Object.keys(a).every(key => a[key] === b[key]) && Object.keys(a).length === Object.keys(b).length; }
    function validate(data, page = offset) {
        const book = value => value && text(value.book_id, 1024) && digest(value.source_sha256) && digest(value.script_sha256);
        if (!data || !digest(data.context_revision) || !(data.registry_revision === null || digest(data.registry_revision))
            || !digest(data.book_token) || !book(data.book) || !Array.isArray(data.chunks) || data.chunks.length > 200
            || !Number.isInteger(data.total_chunks) || data.total_chunks < 1 || data.total_chunks > 100000
            || !(data.next_offset === null || Number.isInteger(data.next_offset) && data.next_offset > page && data.next_offset < data.total_chunks)
            || !data.registry || !Array.isArray(data.registry.versions) || data.registry.versions.length > 1024
            || !data.registry.selected || typeof data.registry.selected !== 'object' || Array.isArray(data.registry.selected)) {
            throw new Error('Invalid cue editor response.');
        }
        const uids = new Set(), versions = new Map();
        for (const row of data.chunks) {
            if (!row || !text(row.chunk_uid, 128) || uids.has(row.chunk_uid) || !boundedText(row.speaker, 128)
                || !boundedText(row.text, 500) || typeof row.has_recorded_take !== 'boolean') {
                throw new Error('Invalid dialogue response.');
            }
            uids.add(row.chunk_uid);
        }
        for (const row of data.registry.versions) {
            const r = row?.recipe, s = r?.selection;
            if (!row || ![1, 2].includes(row.schema) || row.schema === 2 && !digest(r?.rendition_id) || !text(row.cue_id, 128) || !digest(row.version) || versions.has(row.version)
                || !(row.parent_version === null || digest(row.parent_version)) || !book(r?.book)
                || !s || !text(s.chunk_uid, 128) || !digest(s.generation_sha256) || !digest(s.audio_sha256)
                || !(s.audio_revision === null || text(s.audio_revision, 128))
                || !r.asset || typeof r.asset.asset_id !== 'string' || !/^[0-9a-f]{32}$/.test(r.asset.asset_id) || !digest(r.asset.sha256)
                || !Number.isSafeInteger(r.asset.frame_rate) || r.asset.frame_rate < 1 || r.asset.frame_rate > 192000
                || !Number.isSafeInteger(r.asset.frames) || r.asset.frames < 1 || r.asset.frames > r.asset.frame_rate * 86400
                || ![1, 2].includes(r.asset.channels) || ![1, 2, 4].includes(r.asset.sample_width)) {
                throw new Error('Invalid cue history.');
            }
            validateParameters(r); versions.set(row.version, row);
        }
        const selected = Object.entries(data.registry.selected);
        if (selected.length > 128 || selected.some(([id, version]) => versions.get(version)?.cue_id !== id)) {
            throw new Error('Invalid cue selection.');
        }
        return data;
    }
    function validateParameters(p) {
        const integer = (n, lo, hi) => Number.isSafeInteger(n) && n >= lo && n <= hi;
        const number = (n, lo, hi) => typeof n === 'number' && Number.isFinite(n) && n >= lo && n <= hi;
        const d = p.duration, m = p.mix;
        if (!integer(p.frame_rate, 1, 192000) || !integer(p.offset_frames, -192000 * 86400, 192000 * 86400)
            || !['start', 'end'].includes(p.boundary) || !['effect', 'ambience'].includes(p.kind)
            || !d || !['native', 'fixed'].includes(d.mode) || !m
            || !number(m.gain_db, -96, 24) || !number(m.duck_db, -60, 0)
            || !integer(m.fade_in_frames, 0, p.frame_rate * 86400) || !integer(m.fade_out_frames, 0, p.frame_rate * 86400)
            || (d.mode === 'native' ? d.frames !== null || d.loop_crossfade_frames !== null
                : !integer(d.frames, 1, p.frame_rate * 86400) || !(d.loop_crossfade_frames === null
                    || p.kind === 'ambience' && integer(d.loop_crossfade_frames, 1, p.frame_rate * 10)))) {
            throw new Error('Check cue timing, duration, fades and levels.');
        }
    }
    async function json(url, options) {
        const response = await fetch(url, options); await API._handleError(response); return response.json();
    }
    async function refresh(page = 0) {
        if (busy) { return; }
        if (!Number.isInteger(page) || page < 0 || page > 100000) { return; }
        const expected = ++sequence; offset = page; view = null;
        el('sound-cues-content').replaceChildren(); message('Loading cue history and dialogue…');
        try {
            const [data, library] = await Promise.all([json(`/api/sound_design/cues?offset=${page}&limit=100`), json('/api/sound_design/assets')]);
            if (expected !== sequence) { return; }
            view = validate(data); assets = SoundAssets.validate(library).filter(row => row.state === 'ready');
            render(); message('Cue history loaded. Review a dialogue take before saving a revision.');
        } catch (error) {
            if (expected === sequence) { view = null; message(error.message); }
        }
    }
    async function action(work) {
        if (busy || !view) { return; }
        const expected = sequence, captured = view; setBusy(true);
        try { await work(captured, expected); }
        catch (error) {
            if (expected === sequence) {
                view = null; el('sound-cues-content').replaceChildren();
                message(`${error.message} Refresh the cue editor to inspect current saved state.`);
            }
        } finally { setBusy(false); }
    }
    async function anchor(captured, uid) {
        const data = await json(`/api/sound_design/cues/anchor?context_revision=${captured.context_revision}&chunk_uid=${encodeURIComponent(uid)}`);
        const s = data?.selection;
        if (!data || data.context_revision !== captured.context_revision || !digest(data.anchor_revision)
            || !s || s.chunk_uid !== uid || !digest(s.generation_sha256) || !digest(s.audio_sha256)
            || !(s.audio_revision === null || text(s.audio_revision, 128))) { throw new Error('Invalid dialogue review response.'); }
        return data;
    }
    async function publish(captured, expected, path, payload) {
        if (expected !== sequence) { return; }
        const result = await json(`/api/sound_design/cues/${path}`, {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({context_revision: captured.context_revision, registry_revision: captured.registry_revision, ...payload})});
        if (expected !== sequence) { return; }
        if (!result || result.status !== 'saved' || result.cue_id !== payload.cue_id || result.book_token !== captured.book_token
            || !digest(result.registry_revision) || !(result.version === null || digest(result.version))) { throw new Error('Cue save could not be confirmed.'); }
        if (typeof SoundMixes !== 'undefined') { SoundMixes.invalidate(); }
        setBusy(false); await refresh(offset);
        if (sequence === expected + 1 && view?.book_token === captured.book_token) { message('Cue selection saved. Audio audition and delivery approval are still required.'); }
    }
    function render() {
        const box = el('sound-cues-content'); box.replaceChildren();
        box.append(make('p', `Book: ${view.book.book_id}`));
        const paging = make('div');
        if (offset > 0) { paging.append(button('Previous dialogue', () => refresh(Math.max(0, offset - 100)), 'Show the previous hundred dialogue rows.')); }
        if (view.next_offset !== null) { paging.append(button('Next dialogue', () => refresh(view.next_offset), 'Show the next hundred dialogue rows.')); }
        paging.append(button('New cue', () => edit(null), 'Create a cue using a completed dialogue take and an imported sound asset.'));
        box.append(paging, make('p', `Dialogue ${offset + 1}–${offset + view.chunks.length} of ${view.total_chunks}`));
        const form = make('div'); form.id = 'sound-cues-form'; form.setAttribute('id', 'sound-cues-form'); box.append(form);
        const groups = new Map();
        for (const row of view.registry.versions) { if (!groups.has(row.cue_id)) { groups.set(row.cue_id, []); } groups.get(row.cue_id).push(row); }
        for (const [id, rows] of groups) {
            const card = make('div'); card.className = 'border rounded p-2 my-2'; card.append(make('strong', id));
            const selected = Object.hasOwn(view.registry.selected, id) ? view.registry.selected[id] : null;
            card.append(make('p', selected ? 'One revision selected for mixing.' : 'Excluded from mixing.'));
            for (const [index, row] of rows.entries()) {
                const r = row.recipe, line = make('div');
                line.append(make('p', `Revision ${index + 1}${selected === row.version ? ' · selected' : ''} · ${r.kind} · ${r.boundary} ${r.offset_frames / r.frame_rate}s · ${same(r.book, view.book) ? 'Dialogue/audio review required' : 'Source changed — revise explicitly'}`));
                const sound = assets.find(asset => asset.asset_id === r.asset.asset_id);
                line.append(make('p', `${sound?.display_name || 'Sound asset'} · ${r.duration.mode === 'native' ? 'original length' : `${r.duration.frames / r.frame_rate}s`} · sound ${r.mix.gain_db} dB · dialogue ${r.mix.duck_db} dB`));
                line.append(button('Review dialogue binding', () => action(async captured => {
                    const actual = await anchor(captured, r.selection.chunk_uid);
                    message(same(r.book, captured.book) && same(r.selection, actual.selection)
                        ? 'This revision matches the current source and selected dialogue take. Mix audition is still required.'
                        : 'This cue is stale: its source or selected dialogue take changed. Create an explicit revision.');
                }), 'Check this saved revision against current source and selected dialogue audio.'));
                if (r.rendition_id) { line.append(make('p', `Uses recorded rendition ${r.rendition_id.slice(0, 12)} · full mix review required`)); }
                if (typeof SoundRenditions !== 'undefined') {
                    line.append(button('Record a rendition', () => SoundRenditions.recordFromCue(row, view),
                        'Document an imported or externally produced file against this exact source revision.'));
                    line.append(button('Rendition history', () => SoundRenditions.showHistory(id),
                        'Inspect recorded master/stem provenance for this cue.'));
                }
                line.append(button('Revise', () => edit(row), 'Create a new revision bound to the explicitly chosen current dialogue take. History is preserved.'));
                line.append(button('Select revision', () => action((captured, expected) => publish(captured, expected, 'select', {cue_id: id, version: row.version})), 'Select this revision only if its source, take and sound asset still match.'));
                card.append(line);
            }
            if (selected) { card.append(button('Exclude cue', () => action((captured, expected) => publish(captured, expected, 'select', {cue_id: id, version: null})), 'Exclude this cue from mixing while keeping its revision history.')); }
            box.append(card);
        }
    }
    function edit(parent, rendition = null) {
        if (busy || !view) { return; }
        const form = el('sound-cues-form'); form.replaceChildren();
        const fields = {}, r = parent?.recipe, rate = r?.frame_rate || 48000;
        form.append(make('h5', rendition ? `Use recorded master for ${parent.cue_id}` : parent ? `Revise ${parent.cue_id}` : 'New cue'));
        form.append(make('p', 'Saving selects a new revision tied to the chosen current take. Review the dialogue and sound first.'));
        function field(key, label, value, choices = null) {
            const wrapper = make('label', label); wrapper.className = 'form-label d-block mt-2';
            const input = make(choices ? 'select' : 'input'); input.className = 'form-control form-control-sm'; input.title = label;
            if (choices) { for (const [id, name] of choices) { const option = make('option', name); option.value = id; input.append(option); } }
            else { input.type = key === 'name' ? 'text' : 'number'; if (key === 'name') { input.maxLength = 128; } else { input.step = 'any'; } }
            input.value = String(value); input.setAttribute('data-cue-field', key); fields[key] = input; wrapper.append(input); form.append(wrapper); return input;
        }
        field('name', 'Cue name', parent?.cue_id || '');
        if (rendition) { fields.name.readOnly = true; }
        const choices = view.chunks.filter(row => row.has_recorded_take).map(row => [row.chunk_uid, `${row.speaker}: ${row.text}`]);
        if (r && !choices.some(([id]) => id === r.selection.chunk_uid)) { choices.unshift([r.selection.chunk_uid, 'Saved dialogue anchor — review its current take before saving']); }
        field('dialogue', 'Dialogue marked complete (verify its current take)', r?.selection.chunk_uid || choices[0]?.[0] || '', choices);
        if (rendition) {
            const identity = rendition.record.master.asset_id;
            const sound = assets.find(row => row.asset_id === identity);
            field('asset', 'Recorded master', identity, [[identity, sound?.display_name || 'Recorded master — verify availability before use']]);
            form.append(make('p', 'Choose the current dialogue and placement explicitly. The recorded master and its provenance stay fixed.'));
        } else {
            field('asset', 'Imported sound', r?.asset.asset_id || assets[0]?.asset_id || '', assets.map(row => [row.asset_id, row.display_name || 'Sound asset']));
        }
        field('kind', 'Sound type', r?.kind || 'effect', [['effect', 'Effect'], ['ambience', 'Ambience']]);
        field('boundary', 'Place at dialogue', r?.boundary || 'start', [['start', 'Start'], ['end', 'End']]);
        field('offset', 'Offset in seconds (negative means before)', (r?.offset_frames || 0) / rate);
        field('duration', 'Duration', r?.duration.mode || 'native', [['native', 'Original sound length'], ['fixed', 'Explicit length']]);
        field('length', 'Explicit length in seconds', (r?.duration.frames || rate) / rate);
        field('loop', 'Loop crossfade in seconds (0 disables looping; ambience only)', (r?.duration.loop_crossfade_frames || 0) / rate);
        field('gain', 'Sound level in dB (−96 to +24)', r?.mix.gain_db ?? 0);
        field('duck', 'Dialogue reduction in dB (−60 to 0)', r?.mix.duck_db ?? 0);
        field('fadein', 'Fade in seconds', (r?.mix.fade_in_frames || 0) / rate);
        field('fadeout', 'Fade out seconds', (r?.mix.fade_out_frames || 0) / rate);
        form.append(button('Review chosen dialogue', () => action(async captured => {
            await anchor(captured, fields.dialogue.value); message('Current dialogue take verified. Saving will bind to this chosen take; audition the mix before delivery.');
        }), 'Verify the chosen dialogue still has a completed selected take.'));
        form.append(button('Save and select revision', () => {
            let parameters, selected, name;
            try {
                name = fields.name.value; if (!text(name, 128) || !name.trim() || parent && name !== parent.cue_id) { throw new Error('Keep the existing cue name when revising, or create a new cue.'); }
                selected = assets.find(row => row.asset_id === fields.asset.value); if (!selected) { throw new Error('Choose an imported sound asset.'); }
                const n = key => { const raw = fields[key].value; if (!raw.trim() || !Number.isFinite(Number(raw))) { throw new Error('Enter valid numbers for cue timing and levels.'); } return Number(raw); };
                const frames = key => Math.round(n(key) * rate), native = fields.duration.value === 'native';
                if (n('fadein') < 0 || n('fadeout') < 0 || !native && (n('length') <= 0 || n('loop') < 0)) { throw new Error('Duration must be positive; fades and loop crossfade cannot be negative.'); }
                if (n('loop') !== 0 && (native || fields.kind.value !== 'ambience')) { throw new Error('Looping requires ambience with an explicit length. Set crossfade to 0 to disable it.'); }
                parameters = {frame_rate: rate, boundary: fields.boundary.value, kind: fields.kind.value, offset_frames: frames('offset'),
                    duration: {mode: fields.duration.value, frames: native ? null : frames('length'), loop_crossfade_frames: native || n('loop') === 0 ? null : frames('loop')},
                    mix: {gain_db: n('gain'), duck_db: n('duck'), fade_in_frames: frames('fadein'), fade_out_frames: frames('fadeout')}};
                validateParameters(parameters);
                const audio = selected.canonical;
                const nativeFrames = Math.floor((audio.frames - 1) * rate / audio.frame_rate) + 1;
                const durationFrames = native ? nativeFrames : parameters.duration.frames;
                const crossfade = parameters.duration.loop_crossfade_frames;
                if (crossfade !== null && (crossfade >= nativeFrames || durationFrames <= nativeFrames)) { throw new Error('An ambience loop must extend the original sound, with a crossfade shorter than that sound.'); }
                if (durationFrames > nativeFrames && crossfade === null) { throw new Error('Extending a sound requires an explicit ambience loop.'); }
                if (parameters.mix.fade_in_frames + parameters.mix.fade_out_frames > durationFrames) { throw new Error('The combined fades must fit within the cue duration.'); }
            } catch (error) { message(error.message); return; }
            return action(async (captured, expected) => {
                const current = await anchor(captured, fields.dialogue.value);
                if (rendition) {
                    const history = rendition.history;
                    if (captured.context_revision !== history.context_revision || captured.registry_revision !== history.registry_revision
                        || captured.book_token !== history.book_token) { throw new Error('History changed. Refresh recorded renditions and review the cue again.'); }
                    const result = await json('/api/sound_design/renditions/consume', {method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({context_revision: captured.context_revision, registry_revision: captured.registry_revision,
                            ledger_revision: history.ledger_revision, cue_id: parent.cue_id, parent_version: parent.version,
                            chunk_uid: fields.dialogue.value, anchor_revision: current.anchor_revision,
                            rendition_id: rendition.record.rendition_id, parameters})});
                    if (expected !== sequence) { return; }
                    if (result?.status !== 'saved' || result.cue_id !== parent.cue_id || result.rendition_id !== rendition.record.rendition_id
                        || result.book_token !== captured.book_token || !digest(result.version) || !digest(result.context_revision)
                        || !digest(result.registry_revision) || result.ledger_revision !== history.ledger_revision || result.refresh_required !== true) {
                        throw new Error('Recorded master selection could not be confirmed.');
                    }
                    if (typeof SoundMixes !== 'undefined') { SoundMixes.invalidate(); }
                    setBusy(false); await refresh(offset);
                    if (typeof SoundRenditions !== 'undefined') { await SoundRenditions.refresh(); }
                    message('Recorded master selected in a new cue revision. Review the full mix before approval.');
                    return;
                }
                await publish(captured, expected, 'revise', {cue_id: name, parent_version: parent?.version || null,
                    chunk_uid: fields.dialogue.value, anchor_revision: current.anchor_revision,
                    asset_id: selected.asset_id, asset_sha256: selected.canonical.sha256, parameters});
            });
        }, 'Save a new immutable revision using current source, take and sound identities, then select it for mixing.'));
    }
    async function useRendition(record, history) {
        if (busy) { return; }
        const invocation = ++useSequence;
        const panel = el('sound-cues-content').closest?.('details');
        if (panel && !panel.open) {
            await new Promise(resolve => { panel.addEventListener('toggle', resolve, {once: true}); panel.open = true; });
            if (!panel.open) { return; }
        }
        if (invocation !== useSequence) { return; }
        const expected = sequence + 1; await refresh(offset);
        if (invocation !== useSequence || sequence !== expected) { return; }
        if (!view || view.context_revision !== history.context_revision || view.registry_revision !== history.registry_revision
            || view.book_token !== history.book_token) { message('History changed. Refresh recorded renditions before choosing a master.'); return; }
        const selected = view.registry.selected[record.source.cue_id];
        const parent = view.registry.versions.find(row => row.version === selected && row.cue_id === record.source.cue_id);
        if (!parent) { message('Select and review an existing cue revision before using this master.'); return; }
        edit(parent, {record, history});
    }
    return {refresh, validate, useRendition};
})();
