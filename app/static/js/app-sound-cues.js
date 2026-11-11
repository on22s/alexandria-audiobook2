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
            throw new Error(t('sound.cues.invalid-response', 'Invalid cue editor response.'));
        }
        const uids = new Set(), versions = new Map();
        for (const row of data.chunks) {
            if (!row || !text(row.chunk_uid, 128) || uids.has(row.chunk_uid) || !boundedText(row.speaker, 128)
                || !boundedText(row.text, 500) || typeof row.has_recorded_take !== 'boolean') {
                throw new Error(t('sound.cues.invalid-dialogue', 'Invalid dialogue response.'));
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
                throw new Error(t('sound.cues.invalid-history', 'Invalid cue history.'));
            }
            validateParameters(r); versions.set(row.version, row);
        }
        const selected = Object.entries(data.registry.selected);
        if (selected.length > 128 || selected.some(([id, version]) => versions.get(version)?.cue_id !== id)) {
            throw new Error(t('sound.cues.invalid-selection', 'Invalid cue selection.'));
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
            throw new Error(t('sound.cues.invalid-parameters', 'Check cue timing, duration, fades and levels.'));
        }
    }
    async function json(url, options) {
        const response = await fetch(url, options); await API._handleError(response); return response.json();
    }
    async function refresh(page = 0) {
        if (busy) { return; }
        if (!Number.isInteger(page) || page < 0 || page > 100000) { return; }
        const expected = ++sequence; offset = page; view = null;
        el('sound-cues-content').replaceChildren(); message(t('sound.cues.loading', 'Loading cue history and dialogue…'));
        try {
            const [data, library] = await Promise.all([json(`/api/sound_design/cues?offset=${page}&limit=100`), json('/api/sound_design/assets')]);
            if (expected !== sequence) { return; }
            view = validate(data); assets = SoundAssets.validate(library).filter(row => row.state === 'ready');
            render(); message(t('sound.cues.loaded', 'Cue history loaded. Review a dialogue take before saving a revision.'));
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
                message(t('sound.cues.refresh-after-error', '{detail} Refresh the cue editor to inspect current saved state.', {detail: error.message}));
            }
        } finally { setBusy(false); }
    }
    async function anchor(captured, uid) {
        const data = await json(`/api/sound_design/cues/anchor?context_revision=${captured.context_revision}&chunk_uid=${encodeURIComponent(uid)}`);
        const s = data?.selection;
        if (!data || data.context_revision !== captured.context_revision || !digest(data.anchor_revision)
            || !s || s.chunk_uid !== uid || !digest(s.generation_sha256) || !digest(s.audio_sha256)
            || !(s.audio_revision === null || text(s.audio_revision, 128))) { throw new Error(t('sound.cues.invalid-review', 'Invalid dialogue review response.')); }
        return data;
    }
    async function publish(captured, expected, path, payload) {
        if (expected !== sequence) { return; }
        const result = await json(`/api/sound_design/cues/${path}`, {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({context_revision: captured.context_revision, registry_revision: captured.registry_revision, ...payload})});
        if (expected !== sequence) { return; }
        if (!result || result.status !== 'saved' || result.cue_id !== payload.cue_id || result.book_token !== captured.book_token
            || !digest(result.registry_revision) || !(result.version === null || digest(result.version))) { throw new Error(t('sound.cues.save-unconfirmed', 'Cue save could not be confirmed.')); }
        if (typeof SoundMixes !== 'undefined') { SoundMixes.invalidate(); }
        setBusy(false); await refresh(offset);
        if (sequence === expected + 1 && view?.book_token === captured.book_token) { message(t('sound.cues.saved', 'Cue selection saved. Audio audition and delivery approval are still required.')); }
    }
    function render() {
        const box = el('sound-cues-content'); box.replaceChildren();
        box.append(make('p', t('sound.cues.book', 'Book: {book}', {book: view.book.book_id})));
        const paging = make('div');
        if (offset > 0) { paging.append(button(t('sound.cues.previous', 'Previous dialogue'), () => refresh(Math.max(0, offset - 100)), t('sound.cues.previous-title', 'Show the previous hundred dialogue rows.'))); }
        if (view.next_offset !== null) { paging.append(button(t('sound.cues.next', 'Next dialogue'), () => refresh(view.next_offset), t('sound.cues.next-title', 'Show the next hundred dialogue rows.'))); }
        paging.append(button(t('sound.cues.new', 'New cue'), () => edit(null), t('sound.cues.new-title', 'Create a cue using a completed dialogue take and an imported sound asset.')));
        box.append(paging, make('p', t('sound.cues.range', 'Dialogue {first}–{last} of {total}', {first: offset + 1, last: offset + view.chunks.length, total: view.total_chunks})));
        const form = make('div'); form.id = 'sound-cues-form'; form.setAttribute('id', 'sound-cues-form'); box.append(form);
        const groups = new Map();
        for (const row of view.registry.versions) { if (!groups.has(row.cue_id)) { groups.set(row.cue_id, []); } groups.get(row.cue_id).push(row); }
        for (const [id, rows] of groups) {
            const card = make('div'); card.className = 'border rounded p-2 my-2'; card.append(make('strong', id));
            const selected = Object.hasOwn(view.registry.selected, id) ? view.registry.selected[id] : null;
            card.append(make('p', selected ? t('sound.cues.selected', 'One revision selected for mixing.') : t('sound.cues.excluded', 'Excluded from mixing.')));
            for (const [index, row] of rows.entries()) {
                const r = row.recipe, line = make('div');
                line.append(make('p', t('sound.cues.revision', 'Revision {number}{selected} · {kind} · {boundary} {offset}s · {review}', {number: index + 1, selected: selected === row.version ? ' · ' + t('sound.cues.selected-label', 'selected') : '', kind: same(r.book, view.book) ? r.kind : r.kind, boundary: r.boundary, offset: r.offset_frames / r.frame_rate, review: same(r.book, view.book) ? t('sound.cues.review-required', 'Dialogue/audio review required') : t('safety.sound.cues.source-changed', 'Source changed — revise explicitly')})));
                const sound = assets.find(asset => asset.asset_id === r.asset.asset_id);
                line.append(make('p', t('sound.cues.sound-line', '{sound} · {length} · sound {gain} dB · dialogue {duck} dB', {sound: sound?.display_name || t('sound.assets.unnamed', 'Sound asset'), length: r.duration.mode === 'native' ? t('sound.cues.original-length', 'original length') : t('sound.cues.length-seconds', '{seconds}s', {seconds: r.duration.frames / r.frame_rate}), gain: r.mix.gain_db, duck: r.mix.duck_db})));
                line.append(button(t('sound.cues.review-binding', 'Review dialogue binding'), () => action(async captured => {
                    const actual = await anchor(captured, r.selection.chunk_uid);
                    message(same(r.book, captured.book) && same(r.selection, actual.selection)
                        ? t('sound.cues.binding-current', 'This revision matches the current source and selected dialogue take. Mix audition is still required.')
                        : t('safety.sound.cues.stale', 'This cue is stale: its source or selected dialogue take changed. Create an explicit revision.'));
                }), t('sound.cues.review-binding-title', 'Check this saved revision against current source and selected dialogue audio.')));
                if (r.rendition_id) { line.append(make('p', t('sound.cues.rendition', 'Uses recorded rendition {id} · full mix review required', {id: r.rendition_id.slice(0, 12)}))); }
                if (typeof SoundRenditions !== 'undefined') {
                    line.append(button(t('sound.cues.record-rendition', 'Record a rendition'), () => SoundRenditions.recordFromCue(row, view),
                        t('sound.cues.record-rendition-title', 'Document an imported or externally produced file against this exact source revision.')));
                    line.append(button(t('sound.cues.rendition-history', 'Rendition history'), () => SoundRenditions.showHistory(id),
                        t('sound.cues.rendition-history-title', 'Inspect recorded master/stem provenance for this cue.')));
                }
                line.append(button(t('sound.cues.revise', 'Revise'), () => edit(row), t('sound.cues.revise-title', 'Create a new revision bound to the explicitly chosen current dialogue take. History is preserved.')));
                line.append(button(t('sound.cues.select-revision', 'Select revision'), () => action((captured, expected) => publish(captured, expected, 'select', {cue_id: id, version: row.version})), t('sound.cues.select-revision-title', 'Select this revision only if its source, take and sound asset still match.')));
                card.append(line);
            }
            if (selected) { card.append(button(t('sound.cues.exclude', 'Exclude cue'), () => action((captured, expected) => publish(captured, expected, 'select', {cue_id: id, version: null})), t('sound.cues.exclude-title', 'Exclude this cue from mixing while keeping its revision history.'))); }
            box.append(card);
        }
    }
    function edit(parent, rendition = null) {
        if (busy || !view) { return; }
        const form = el('sound-cues-form'); form.replaceChildren();
        const fields = {}, r = parent?.recipe, displayRate = r?.frame_rate || 1;
        form.append(make('h5', rendition ? t('sound.cues.form.use-master', 'Use recorded master for {cue}', {cue: parent.cue_id}) : parent ? t('sound.cues.form.revise', 'Revise {cue}', {cue: parent.cue_id}) : t('sound.cues.form.new', 'New cue')));
        form.append(make('p', t('sound.cues.form.intro', 'Saving selects a new revision tied to the chosen current take. Review the dialogue and sound first.')));
        function field(key, label, value, choices = null) {
            const wrapper = make('label', label); wrapper.className = 'form-label d-block mt-2';
            const input = make(choices ? 'select' : 'input'); input.className = 'form-control form-control-sm'; input.title = label;
            if (choices) { for (const [id, name] of choices) { const option = make('option', name); option.value = id; input.append(option); } }
            else { input.type = key === 'name' ? 'text' : 'number'; if (key === 'name') { input.maxLength = 128; } else { input.step = 'any'; } }
            input.value = String(value); input.setAttribute('data-cue-field', key); fields[key] = input; wrapper.append(input); form.append(wrapper); return input;
        }
        field('name', t('sound.cues.field.name', 'Cue name'), parent?.cue_id || '');
        if (rendition) { fields.name.readOnly = true; }
        const choices = view.chunks.filter(row => row.has_recorded_take).map(row => [row.chunk_uid, `${row.speaker}: ${row.text}`]);
        if (r && !choices.some(([id]) => id === r.selection.chunk_uid)) { choices.unshift([r.selection.chunk_uid, t('sound.cues.field.saved-anchor', 'Saved dialogue anchor — review its current take before saving')]); }
        field('dialogue', t('sound.cues.field.dialogue', 'Dialogue marked complete (verify its current take)'), r?.selection.chunk_uid || choices[0]?.[0] || '', choices);
        if (rendition) {
            const identity = rendition.record.master.asset_id;
            const sound = assets.find(row => row.asset_id === identity);
            field('asset', t('sound.cues.field.recorded-master', 'Recorded master'), identity, [[identity, sound?.display_name || t('sound.cues.field.master-verify', 'Recorded master — verify availability before use')]]);
            form.append(make('p', t('sound.cues.form.recorded-intro', 'Choose the current dialogue and placement explicitly. The recorded master and its provenance stay fixed.')));
        } else {
            field('asset', t('sound.cues.field.imported-sound', 'Imported sound'), r?.asset.asset_id || assets[0]?.asset_id || '', assets.map(row => [row.asset_id, row.display_name || t('sound.assets.unnamed', 'Sound asset')]));
        }
        field('kind', t('sound.cues.field.kind', 'Sound type'), r?.kind || 'effect', [['effect', t('sound.cues.kind.effect', 'Effect')], ['ambience', t('sound.cues.kind.ambience', 'Ambience')]]);
        field('boundary', t('sound.cues.field.boundary', 'Place at dialogue'), r?.boundary || 'start', [['start', t('sound.cues.boundary.start', 'Start')], ['end', t('sound.cues.boundary.end', 'End')]]);
        const clock = field('rate', t('sound.cues.field.clock', 'Cue frame clock in Hz (match the dry dialogue mix)'), r?.frame_rate ?? '');
        clock.min = '1'; clock.max = '192000'; clock.step = '1'; clock.required = true;
        form.append(make('p', t('sound.cues.form.clock-help', 'Enter the dry dialogue mix sample rate, not the imported sound rate. For multiple dialogue sources, use the highest selected-take rate or 11025 Hz, whichever is higher. If the mix clock changes, explicitly revise this field and save a new revision; saved history is never rescaled.')));
        field('offset', t('sound.cues.field.offset', 'Offset in seconds (negative means before)'), (r?.offset_frames || 0) / displayRate);
        field('duration', t('sound.cues.field.duration', 'Duration'), r?.duration.mode || 'native', [['native', t('sound.cues.duration.native', 'Original sound length')], ['fixed', t('sound.cues.duration.fixed', 'Explicit length')]]);
        field('length', t('sound.cues.field.length', 'Explicit length in seconds'), (r?.duration.frames || displayRate) / displayRate);
        field('loop', t('sound.cues.field.loop', 'Loop crossfade in seconds (0 disables looping; ambience only)'), (r?.duration.loop_crossfade_frames || 0) / displayRate);
        field('gain', t('sound.cues.field.gain', 'Sound level in dB (−96 to +24)'), r?.mix.gain_db ?? 0);
        field('duck', t('sound.cues.field.duck', 'Dialogue reduction in dB (−60 to 0)'), r?.mix.duck_db ?? 0);
        field('fadein', t('sound.cues.field.fadein', 'Fade in seconds'), (r?.mix.fade_in_frames || 0) / displayRate);
        field('fadeout', t('sound.cues.field.fadeout', 'Fade out seconds'), (r?.mix.fade_out_frames || 0) / displayRate);
        form.append(button(t('sound.cues.review-chosen', 'Review chosen dialogue'), () => action(async captured => {
            await anchor(captured, fields.dialogue.value); message(t('sound.cues.chosen-verified', 'Current dialogue take verified. Saving will bind to this chosen take; audition the mix before delivery.'));
        }), t('sound.cues.review-chosen-title', 'Verify the chosen dialogue still has a completed selected take.')));
        form.append(button(t('sound.cues.save', 'Save and select revision'), () => {
            let parameters, selected, name;
            try {
                name = fields.name.value; if (!text(name, 128) || !name.trim() || parent && name !== parent.cue_id) { throw new Error(t('sound.cues.error.name', 'Keep the existing cue name when revising, or create a new cue.')); }
                selected = assets.find(row => row.asset_id === fields.asset.value); if (!selected) { throw new Error(t('sound.cues.error.asset', 'Choose an imported sound asset.')); }
                const n = key => { const raw = fields[key].value; if (!raw.trim() || !Number.isFinite(Number(raw))) { throw new Error(t('sound.cues.error.numbers', 'Enter valid numbers for cue timing and levels.')); } return Number(raw); };
                const rate = Number(fields.rate.value);
                if (!Number.isSafeInteger(rate) || rate < 1 || rate > 192000) { throw new Error(t('sound.cues.error.clock', 'Enter an explicit cue clock from 1 to 192000 Hz matching the dry dialogue mix.')); }
                const frames = key => Math.round(n(key) * rate), native = fields.duration.value === 'native';
                if (n('fadein') < 0 || n('fadeout') < 0 || !native && (n('length') <= 0 || n('loop') < 0)) { throw new Error(t('sound.cues.error.duration', 'Duration must be positive; fades and loop crossfade cannot be negative.')); }
                if (n('loop') !== 0 && (native || fields.kind.value !== 'ambience')) { throw new Error(t('sound.cues.error.loop', 'Looping requires ambience with an explicit length. Set crossfade to 0 to disable it.')); }
                parameters = {frame_rate: rate, boundary: fields.boundary.value, kind: fields.kind.value, offset_frames: frames('offset'),
                    duration: {mode: fields.duration.value, frames: native ? null : frames('length'), loop_crossfade_frames: native || n('loop') === 0 ? null : frames('loop')},
                    mix: {gain_db: n('gain'), duck_db: n('duck'), fade_in_frames: frames('fadein'), fade_out_frames: frames('fadeout')}};
                validateParameters(parameters);
                const audio = selected.canonical;
                const nativeFrames = Math.floor((audio.frames - 1) * rate / audio.frame_rate) + 1;
                const durationFrames = native ? nativeFrames : parameters.duration.frames;
                const crossfade = parameters.duration.loop_crossfade_frames;
                if (crossfade !== null && (crossfade >= nativeFrames || durationFrames <= nativeFrames)) { throw new Error(t('sound.cues.error.loop-short', 'An ambience loop must extend the original sound, with a crossfade shorter than that sound.')); }
                if (durationFrames > nativeFrames && crossfade === null) { throw new Error(t('sound.cues.error.extend', 'Extending a sound requires an explicit ambience loop.')); }
                if (parameters.mix.fade_in_frames + parameters.mix.fade_out_frames > durationFrames) { throw new Error(t('sound.cues.error.fades', 'The combined fades must fit within the cue duration.')); }
            } catch (error) { message(error.message); return; }
            return action(async (captured, expected) => {
                const current = await anchor(captured, fields.dialogue.value);
                if (rendition) {
                    const history = rendition.history;
                    if (captured.context_revision !== history.context_revision || captured.registry_revision !== history.registry_revision
                        || captured.book_token !== history.book_token) { throw new Error(t('safety.sound.cues.history-changed', 'History changed. Refresh recorded renditions and review the cue again.')); }
                    const result = await json('/api/sound_design/renditions/consume', {method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({context_revision: captured.context_revision, registry_revision: captured.registry_revision,
                            ledger_revision: history.ledger_revision, cue_id: parent.cue_id, parent_version: parent.version,
                            chunk_uid: fields.dialogue.value, anchor_revision: current.anchor_revision,
                            rendition_id: rendition.record.rendition_id, parameters})});
                    if (expected !== sequence) { return; }
                    if (result?.status !== 'saved' || result.cue_id !== parent.cue_id || result.rendition_id !== rendition.record.rendition_id
                        || result.book_token !== captured.book_token || !digest(result.version) || !digest(result.context_revision)
                        || !digest(result.registry_revision) || result.ledger_revision !== history.ledger_revision || result.refresh_required !== true) {
                        throw new Error(t('safety.sound.cues.master-unconfirmed', 'Recorded master selection could not be confirmed.'));
                    }
                    if (typeof SoundMixes !== 'undefined') { SoundMixes.invalidate(); }
                    setBusy(false); await refresh(offset);
                    if (typeof SoundRenditions !== 'undefined') { await SoundRenditions.refresh(); }
                    message(t('sound.cues.master-selected', 'Recorded master selected in a new cue revision. Review the full mix before approval.'));
                    return;
                }
                await publish(captured, expected, 'revise', {cue_id: name, parent_version: parent?.version || null,
                    chunk_uid: fields.dialogue.value, anchor_revision: current.anchor_revision,
                    asset_id: selected.asset_id, asset_sha256: selected.canonical.sha256, parameters});
            });
        }, t('sound.cues.save-title', 'Save a new immutable revision using current source, take and sound identities, then select it for mixing.')));
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
            || view.book_token !== history.book_token) { message(t('safety.sound.cues.history-changed-master', 'History changed. Refresh recorded renditions before choosing a master.')); return; }
        const selected = view.registry.selected[record.source.cue_id];
        const parent = view.registry.versions.find(row => row.version === selected && row.cue_id === record.source.cue_id);
        if (!parent) { message(t('safety.sound.cues.select-before-master', 'Select and review an existing cue revision before using this master.')); return; }
        edit(parent, {record, history});
    }
    return {refresh, validate, useRendition};
})();
