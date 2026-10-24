// Immutable master/stem provenance. History and keep flags never grant approval.
const SoundRenditions = (() => {
    let sequence = 0, busy = false, view = null, assets = [], offset = 0, filter = null;
    let selected = new Set(), preview = null, bridgeSequence = 0;
    const el = id => document.getElementById(id);
    const make = (tag, label = '') => { const node = document.createElement(tag); node.textContent = label; return node; };
    const message = value => { el('sound-renditions-status').textContent = value; };
    const digest = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
    const text = (value, max) => typeof value === 'string' && value.trim().length > 0 && value.length <= max * 2 && Array.from(value).length <= max;
    const integer = (value, lo, hi) => Number.isSafeInteger(value) && value >= lo && value <= hi;
    const nullableDigest = value => value === null || digest(value);
    const book = value => value && text(value.book_id, 1024) && digest(value.source_sha256) && digest(value.script_sha256);
    const sameBook = (a, b) => a.book_id === b.book_id && a.source_sha256 === b.source_sha256 && a.script_sha256 === b.script_sha256;
    function button(label, action, help) {
        const node = make('button', label); node.type = 'button'; node.className = 'btn btn-sm btn-outline-secondary me-2 my-1';
        node.onclick = action; node.title = help; return node;
    }
    function setBusy(value) {
        busy = value;
        el('sound-renditions-content').querySelectorAll('button,input,select').forEach(node => { node.disabled = value || node.dataset.locked === 'true'; });
        el('sound-renditions-refresh').disabled = value;
    }
    function artifact(value, rate = null) {
        const c = value?.canonical, source = value?.source;
        if (!value || !/^[0-9a-f]{32}$/.test(value.asset_id) || !source || !digest(source.sha256)
            || !integer(source.bytes, 1, 256 * 1024 ** 2) || !c || !digest(c.sha256)
            || !integer(c.frame_rate, 1, 192000) || !integer(c.frames, 1, c.frame_rate * 86400)
            || ![1, 2].includes(c.channels) || c.sample_width !== 4 || c.bytes !== c.frames * c.channels * 4
            || !integer(c.bytes, 1, 1024 ** 3) || !text(c.conversion, 128) || !text(c.codec, 64)
            || !['wav', 'flac', 'mp3', 'ogg'].includes(c.format)
            || !integer(value.offset_frames, rate === null ? 0 : -rate * 86400, rate === null ? 0 : rate * 86400)) {
            throw new Error('Invalid recorded media facts.');
        }
    }
    function record(value, bookId) {
        if (!value || value.schema !== 1 || !digest(value.rendition_id) || !value.source
            || !text(value.source.cue_id, 128) || !digest(value.source.version) || !book(value.source.book)
            || value.source.book.book_id !== bookId || !value.composition
            || !nullableDigest(value.composition.scaffold_sha256) || !nullableDigest(value.composition.arrangement_sha256)
            || !(value.composition.family_id === null || text(value.composition.family_id, 128))
            || !value.origin || !['imported_audio', 'declared_external_render'].includes(value.origin.kind)
            || !value.derivation || !['import', 'render', 'revoice', 'mixdown', 'processing'].includes(value.derivation.kind)
            || !Array.isArray(value.stems) || value.stems.length > 16) { throw new Error('Invalid rendition history.'); }
        const origin = value.origin, d = value.derivation, renderer = origin.renderer;
        if (origin.kind === 'imported_audio' ? renderer !== null : !renderer || !text(renderer.component_id, 256)
            || !text(renderer.component_revision, 512) || !digest(renderer.request_sha256) || !digest(renderer.result_sha256)) {
            throw new Error('Invalid declared renderer.');
        }
        if (['import', 'render'].includes(d.kind) ? d.parent !== null || origin.kind !== (d.kind === 'import' ? 'imported_audio' : 'declared_external_render') : !digest(d.parent)) {
            throw new Error('Invalid rendition ancestry.');
        }
        artifact(value.master);
        const ids = new Set();
        for (const stem of value.stems) {
            if (!stem || !text(stem.stem_id, 128) || ids.has(stem.stem_id) || !text(stem.label, 256)) { throw new Error('Invalid recorded stem.'); }
            ids.add(stem.stem_id); artifact(stem.artifact, value.master.canonical.frame_rate);
        }
    }
    function validate(data, page = offset) {
        if (!data || !digest(data.context_revision) || !nullableDigest(data.registry_revision) || !nullableDigest(data.ledger_revision)
            || !digest(data.book_token) || !text(data.book_id, 1024) || data.approval_checked !== false
            || !Array.isArray(data.records) || data.records.length > 100 || !integer(data.total_records, 0, 1024)
            || data.records.length > 0 && (data.ledger_revision === null || page + data.records.length > data.total_records)
            || !(data.next_offset === null || integer(data.next_offset, page + 1, data.total_records - 1))
            || !Array.isArray(data.keep) || data.keep.length > 1024 || !Array.isArray(data.draft_selected) || data.draft_selected.length > 128) {
            throw new Error('Invalid rendition history response.');
        }
        const ids = new Set();
        for (const item of data.records) {
            record(item, data.book_id);
            if (ids.has(item.rendition_id)) { throw new Error('Repeated rendition identity.'); }
            ids.add(item.rendition_id);
        }
        for (const values of [data.keep, data.draft_selected]) {
            if (values.some(value => !digest(value)) || new Set(values).size !== values.length) { throw new Error('Invalid rendition selection.'); }
        }
        return data;
    }
    async function json(url, body) {
        let options = {};
        if (body !== undefined) {
            const encoded = JSON.stringify(body);
            if (new TextEncoder().encode(encoded).byteLength > 16384) { throw new Error('This record exceeds 16 KiB. Shorten labels or select fewer stems.'); }
            options = {method: 'POST', headers: {'Content-Type': 'application/json'}, body: encoded};
        }
        const response = await fetch(url, options); await API._handleError(response); return response.json();
    }
    const common = value => ({context_revision: value.context_revision, registry_revision: value.registry_revision, ledger_revision: value.ledger_revision});
    async function refresh(page = 0, cueId = filter) {
        if (busy || !integer(page, 0, 1024) || !(cueId === null || text(cueId, 128))) { return; }
        const expected = ++sequence; offset = page; filter = cueId; view = null; preview = null; selected = new Set();
        el('sound-renditions-content').replaceChildren(); message('Loading recorded renditions…');
        const query = `offset=${page}&limit=100${cueId === null ? '' : `&cue_id=${encodeURIComponent(cueId)}`}`;
        const results = await Promise.allSettled([json(`/api/sound_design/renditions?${query}`), json('/api/sound_design/assets')]);
        if (expected !== sequence) { return; }
        try {
            if (results[0].status !== 'fulfilled') { throw results[0].reason; }
            view = validate(results[0].value, page); assets = [];
            if (results[1].status === 'fulfilled') {
                try { assets = SoundAssets.validate(results[1].value).filter(row => row.state === 'ready'); } catch (_error) { assets = []; }
            }
            render(); message(assets.length ? 'History loaded. Recording and keep flags do not approve a mix.' : 'History loaded. Import or repair audio assets before recording or using a master.');
        } catch (error) { view = null; el('sound-renditions-content').replaceChildren(); message(error.message); }
    }
    async function action(work) {
        if (busy || !view) { return; }
        const captured = view, expected = sequence; setBusy(true);
        try { await work(captured, expected); }
        catch (error) {
            if (expected === sequence) { view = null; preview = null; el('sound-renditions-content').replaceChildren(); message(`${error.message} Refresh history to inspect saved state.`); }
        } finally { setBusy(false); }
    }
    async function mutate(path, body, captured, expected, identity = null) {
        const result = await json(`/api/sound_design/renditions/${path}`, {...common(captured), ...body});
        if (expected !== sequence) { return; }
        if (!result || result.status !== ({record: 'recorded', keep: 'saved', cleanup: 'removed'})[path]
            || result.book_token !== captured.book_token || !digest(result.context_revision) || !digest(result.ledger_revision)
            || result.refresh_required !== true || identity !== null && result.rendition_id !== identity
            || path === 'record' && !digest(result.rendition_id) || path === 'keep' && result.keep !== body.keep
            || path === 'cleanup' && JSON.stringify(result.removed_ids) !== JSON.stringify(body.rendition_ids)) {
            throw new Error('Rendition operation could not be confirmed.');
        }
        if (typeof SoundMixes !== 'undefined') { SoundMixes.invalidate(); }
        setBusy(false); await refresh(offset, filter);
        if (sequence === expected + 1 && view?.book_token === captured.book_token) { message('History saved. Refresh cues and review the full mix before approval.'); }
    }
    function render() {
        const box = el('sound-renditions-content'); box.replaceChildren();
        box.append(make('p', `Book: ${view.book_id} · ${view.total_records} recorded renditions${filter ? ` for ${filter}` : ''}`));
        const paging = make('div');
        if (offset > 0) { paging.append(button('Previous records', () => refresh(Math.max(0, offset - 100)), 'Show the previous page.')); }
        if (view.next_offset !== null) { paging.append(button('Next records', () => refresh(view.next_offset), 'Show the next page.')); }
        if (filter !== null) { paging.append(button('All cue histories', () => refresh(0, null), 'Show this book’s recorded renditions across cues.')); }
        box.append(paging, make('p', 'To record a file, choose an exact source revision in Sound cues. Imported files, declared external renders and derived files remain distinguishable.'));
        const form = make('div'); form.id = 'sound-renditions-form'; form.setAttribute('id', 'sound-renditions-form'); box.append(form);
        for (const item of view.records) {
            const card = make('div'); card.className = 'border rounded p-2 my-2';
            card.append(make('strong', `${item.source.cue_id} · ${item.rendition_id.slice(0, 12)}`));
            const origin = item.origin.kind === 'imported_audio' ? 'Imported audio' : `Declared external render · ${item.origin.renderer.component_id} ${item.origin.renderer.component_revision} (unverified declaration)`;
            card.append(make('p', `${origin} · ${item.derivation.kind}${item.derivation.parent ? ` from ${item.derivation.parent.slice(0, 12)}` : ''}`));
            card.append(make('p', `Source revision ${item.source.version.slice(0, 12)} · ${item.stems.length} stems · master ${item.master.canonical.frame_rate} frames/sec`));
            const masterName = assets.find(row => row.asset_id === item.master.asset_id)?.display_name || 'Recorded master';
            card.append(make('p', `${masterName} · ${(item.master.canonical.frames / item.master.canonical.frame_rate).toFixed(2)} seconds · family: ${item.composition.family_id || 'not specified'}`));
            const facts = make('details'); facts.append(make('summary', 'Source and composition fingerprints'));
            for (const [label, value] of [['Rendition', item.rendition_id], ['Original master', item.master.source.sha256],
                ['Canonical master', item.master.canonical.sha256], ['Score sketch', item.composition.scaffold_sha256], ['Arrangement', item.composition.arrangement_sha256]]) {
                facts.append(make('p', `${label}: ${value || 'not provided'}`));
            }
            card.append(facts);
            card.append(make('p', `${view.draft_selected.includes(item.rendition_id) ? 'Selected in a draft cue. ' : ''}${view.keep.includes(item.rendition_id) ? 'Kept from cleanup. ' : ''}Approval is checked separately in Mixed audio review.`));
            for (const stem of item.stems) { card.append(make('p', `${stem.label} · offset ${stem.artifact.offset_frames} master frames · asset ${stem.artifact.asset_id.slice(0, 12)}`)); }
            card.append(button('Use master in cue', () => SoundCues.useRendition(item, view), 'Review current dialogue and timing before selecting this recorded master.'));
            card.append(button(view.keep.includes(item.rendition_id) ? 'Remove keep flag' : 'Keep this record', () => action((captured, expected) => mutate('keep', {rendition_id: item.rendition_id, keep: !captured.keep.includes(item.rendition_id)}, captured, expected, item.rendition_id)), 'Change cleanup protection; this does not approve audio.'));
            card.append(button('Record derived file', () => action(async (captured, expected) => {
                const cues = SoundCues.validate(await json('/api/sound_design/cues?offset=0&limit=100'), 0);
                if (expected !== sequence) { return; }
                if (cues.context_revision !== captured.context_revision || cues.book_token !== captured.book_token) { throw new Error('Source history changed.'); }
                const source = cues.registry.versions.find(row => row.version === item.source.version && row.cue_id === item.source.cue_id);
                if (!source || !sameBook(source.recipe.book, item.source.book)) { throw new Error('Retained source revision is unavailable.'); }
                recordForm(source, item);
            }), 'Document a separately imported revoice, mixdown or processing result. No generation or processing runs here.'));
            const label = make('label', ' Select for cleanup review '), check = make('input'); check.type = 'checkbox';
            check.setAttribute('data-rendition-cleanup', item.rendition_id);
            check.onchange = () => { if (check.checked) { selected.add(item.rendition_id); } else { selected.delete(item.rendition_id); } preview = null; el('sound-renditions-cleanup').replaceChildren(); };
            label.prepend(check); card.append(label); box.append(card);
        }
        box.append(button('Preview selected cleanup', () => action(previewCleanup), 'Inspect protected records and exact removal impact before deleting metadata.'));
        const cleanup = make('div'); cleanup.id = 'sound-renditions-cleanup'; cleanup.setAttribute('id', 'sound-renditions-cleanup'); box.append(cleanup);
    }
    async function previewCleanup(captured, expected) {
        const ids = [...selected];
        if (!ids.length || ids.length > 128) { throw new Error('Select between one and 128 records for cleanup review.'); }
        const result = await json('/api/sound_design/renditions/cleanup_preview', {...common(captured), rendition_ids: ids});
        if (expected !== sequence) { return; }
        const reasons = ['retained_cue_history', 'draft_selected', 'kept', 'retained_child', 'retained_candidate', 'retained_approval'];
        if (!result || result.context_revision !== captured.context_revision || result.registry_revision !== captured.registry_revision
            || result.ledger_revision !== captured.ledger_revision || result.book_token !== captured.book_token || !digest(result.impact_revision)
            || !Array.isArray(result.rows) || result.rows.length !== ids.length || !Array.isArray(result.removable_ids)) { throw new Error('Invalid cleanup impact.'); }
        result.rows.forEach((row, index) => {
            if (row.rendition_id !== ids[index] || typeof row.protected !== 'boolean' || !Array.isArray(row.reasons)
                || row.reasons.some(reason => !reasons.includes(reason)) || row.protected !== (row.reasons.length > 0)) { throw new Error('Invalid cleanup protection.'); }
        });
        const removable = result.rows.filter(row => !row.protected).map(row => row.rendition_id);
        if (JSON.stringify(removable) !== JSON.stringify(result.removable_ids)) { throw new Error('Invalid cleanup selection.'); }
        preview = {ids, impact: result.impact_revision, allRemovable: removable.length === ids.length};
        const box = el('sound-renditions-cleanup'); box.replaceChildren();
        for (const row of result.rows) { box.append(make('p', `${row.rendition_id.slice(0, 12)} · ${row.protected ? `Protected: ${row.reasons.join(', ')}` : 'Record metadata may be removed'}`)); }
        const remove = button('Delete reviewed record metadata', () => action((current, currentSequence) => {
            if (!preview || !preview.allRemovable || JSON.stringify([...selected]) !== JSON.stringify(preview.ids)) { throw new Error('Cleanup selection changed. Preview again.'); }
            return mutate('cleanup', {rendition_ids: preview.ids, impact_revision: preview.impact}, current, currentSequence);
        }), 'Remove only the reviewed record metadata. Ready audio assets remain retained.');
        remove.setAttribute('data-locked', String(removable.length !== ids.length)); remove.disabled = removable.length !== ids.length; box.append(remove);
    }
    function field(box, key, label, value = '', choices = null, max = 512) {
        const wrapper = make('label', label); wrapper.className = 'form-label d-block mt-2';
        const input = make(choices ? 'select' : 'input'); input.className = 'form-control form-control-sm'; input.title = label;
        input.setAttribute('data-rendition-field', key);
        if (choices) { for (const [id, name] of choices) { const option = make('option', name); option.value = id; input.append(option); } }
        else { input.type = 'text'; input.maxLength = max * 2; }
        input.value = String(value); wrapper.append(input); box.append(wrapper); return input;
    }
    function recordForm(source, parent = null) {
        const box = el('sound-renditions-form'); box.replaceChildren();
        box.append(make('h5', `Record a file for ${source.cue_id} · source ${source.version.slice(0, 12)}`));
        box.append(make('p', 'This records how existing audio was made. It does not generate, revoice, mix or process audio. External renderer details remain unverified declarations.'));
        const choices = assets.map(row => [row.asset_id, row.display_name || row.asset_id.slice(0, 12)]);
        const fields = {}, add = (key, label, value, options = null, max = 512) => (fields[key] = field(box, key, label, value, options, max));
        add('master', 'Imported master audio', parent?.master.asset_id || source.recipe.asset.asset_id, choices);
        const clock = make('p'); box.append(clock);
        const updateClock = () => {
            const master = assets.find(row => row.asset_id === fields.master.value);
            clock.textContent = master ? `Master clock: ${master.canonical.frame_rate} frames/sec. Positive stem offsets start later; negative offsets start earlier.` : 'Choose an available master to review its frame clock.';
        };
        fields.master.onchange = updateClock; updateClock();
        add('origin', 'Origin', 'imported_audio', [['imported_audio', 'Audio import'], ['declared_external_render', 'Declared external render']]);
        add('derivation', 'Derivation', parent ? 'processing' : 'new', [['new', 'New file'], ['revoice', 'Revoice result'], ['mixdown', 'Mixdown result'], ['processing', 'Processing result']]);
        const parents = view.records.filter(row => row.source.version === source.version && sameBook(row.source.book, source.recipe.book));
        if (parent && !parents.some(row => row.rendition_id === parent.rendition_id)) { parents.push(parent); }
        add('parent', 'Earlier parent (required for a derived file)', parent?.rendition_id || '', [['', 'Choose a parent'], ...parents.map(row => [row.rendition_id, row.rendition_id.slice(0, 12)])]);
        add('family', 'Family label (optional)', '', null, 128);
        add('scaffold', 'Score sketch SHA-256 (optional)', '', null, 64);
        add('arrangement', 'Arrangement SHA-256 (optional)', '', null, 64);
        const renderer = make('details'); renderer.append(make('summary', 'External renderer declaration')); box.append(renderer);
        fields.origin.onchange = () => { renderer.open = fields.origin.value === 'declared_external_render'; };
        for (const [key, label, max] of [['component', 'Tool name', 256], ['revision', 'Tool version', 512], ['request', 'Request SHA-256', 64], ['result', 'Result SHA-256', 64]]) {
            fields[key] = field(renderer, key, label, '', null, max);
        }
        const stemsBox = make('div'); box.append(stemsBox); const stems = []; let stemSequence = 0;
        box.append(button('Add stem', () => {
            if (busy || stems.length >= 16) { return; }
            const card = make('div'); card.className = 'border rounded p-2 my-2'; const id = `stem-${++stemSequence}`;
            const row = {id, card, label: field(card, `${id}-label`, 'Stem label', '', null, 256),
                asset: field(card, `${id}-asset`, 'Imported stem audio', assets[0]?.asset_id || '', choices),
                offset: field(card, `${id}-offset`, 'Offset in master frames (signed integer)', '0')};
            row.offset.type = 'number'; row.offset.step = '1'; stems.push(row);
            card.append(button('Remove stem', () => { if (!busy) { stems.splice(stems.indexOf(row), 1); card.remove(); } }, 'Remove this stem from the unsaved record.'));
            stemsBox.append(card);
        }, 'Add up to sixteen imported stems with explicit offsets on the master clock.'));
        box.append(button('Record immutable rendition', () => {
            let payload;
            try {
                const chosen = id => { const row = assets.find(item => item.asset_id === id); if (!row || !digest(row.source?.sha256)) { throw new Error('Choose an available imported audio asset.'); } return row; };
                const selection = row => ({asset_id: row.asset_id, source_sha256: row.source.sha256, canonical_sha256: row.canonical.sha256});
                const master = chosen(fields.master.value), optional = input => { const value = input.value.trim(); if (value && !digest(value)) { throw new Error('Fingerprints must be full lowercase SHA-256 values.'); } return value || null; };
                const family = fields.family.value.trim(); if (family && !text(family, 128)) { throw new Error('Use a family label of at most 128 characters.'); }
                const kind = fields.derivation.value;
                const derivation = kind === 'new' ? {kind: fields.origin.value === 'imported_audio' ? 'import' : 'render', parent: null} : {kind, parent: fields.parent.value};
                if (kind !== 'new' && !digest(derivation.parent)) { throw new Error('Choose an earlier parent from this exact source revision.'); }
                let declaration = null;
                if (fields.origin.value === 'declared_external_render') {
                    if (!text(fields.component.value, 256) || !text(fields.revision.value, 512) || !digest(fields.request.value) || !digest(fields.result.value)) { throw new Error('Complete the external renderer declaration.'); }
                    declaration = {component_id: fields.component.value, component_revision: fields.revision.value, request_sha256: fields.request.value, result_sha256: fields.result.value};
                }
                const stemFacts = stems.map(row => {
                    const raw = row.offset.value, number = Number(raw);
                    if (!text(row.label.value, 256) || !raw.trim() || !integer(number, -master.canonical.frame_rate * 86400, master.canonical.frame_rate * 86400)) { throw new Error('Each stem needs a label and bounded integer offset in master frames.'); }
                    return {stem_id: row.id, label: row.label.value, ...selection(chosen(row.asset.value)), offset_frames: number};
                });
                payload = {source: {cue_id: source.cue_id, version: source.version}, master: selection(master), stems: stemFacts,
                    composition: {scaffold_sha256: optional(fields.scaffold), arrangement_sha256: optional(fields.arrangement), family_id: family || null},
                    origin: {kind: fields.origin.value, renderer: declaration}, derivation};
            } catch (error) { message(error.message); return; }
            return action((captured, expected) => mutate('record', payload, captured, expected));
        }, 'Retain one immutable record using verified imported source/master/stem identities.'));
    }
    function openPanel() {
        const panel = el('sound-renditions-content').closest?.('details');
        if (panel && !panel.open) {
            return new Promise(resolve => { panel.addEventListener('toggle', () => resolve(panel.open), {once: true}); panel.open = true; });
        }
        return null;
    }
    async function showHistory(cueId) {
        if (busy || !text(cueId, 128)) { return; }
        const invocation = ++bridgeSequence;
        const opening = openPanel();
        if (opening && !await opening) { return; }
        if (invocation === bridgeSequence) { await refresh(0, cueId); }
    }
    async function recordFromCue(source, cueView) {
        if (busy || !source || !text(source.cue_id, 128) || !digest(source.version)) { return; }
        const invocation = ++bridgeSequence;
        const opening = openPanel();
        if (opening && !await opening) { return; }
        if (invocation !== bridgeSequence) { return; }
        const expected = sequence + 1; await refresh(0, source.cue_id);
        if (invocation !== bridgeSequence || sequence !== expected) { return; }
        if (!view || view.context_revision !== cueView.context_revision || view.registry_revision !== cueView.registry_revision
            || view.book_token !== cueView.book_token) { message('Cue history changed. Refresh Sound cues before recording.'); return; }
        recordForm(source);
    }
    return {refresh, showHistory, recordFromCue, validate};
})();
