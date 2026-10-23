// Explicit character identity review. All names/quotes render as text, never HTML.
(() => {
    'use strict';
    const el = id => document.getElementById('roster-' + id), endpoint = '/api/character_roster';
    const copy = value => JSON.parse(JSON.stringify(value));
    const idPattern = /^character_[0-9a-f]{20}$/;
    let epoch = 0, writes = 0, recovery = null;
    let s = fresh();
    function fresh() { return {meta: null, base: null, rows: [], origins: {}, selected: new Set(), page: 0,
        edit: 0, dirty: false, pending: null, preview: null, undo: null, imported: null, mapping: null,
        candidate: null, importBackup: null, uncertain: false}; }
    const owner = () => ({epoch, book: currentBookFilename, edit: s.edit});
    const current = (o, edits = true) => o.epoch === epoch && o.book === currentBookFilename && (!edits || o.edit === s.edit);
    const status = text => { el('status').textContent = text; };
    function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) { n.textContent = String(text); } if (cls) { n.className = cls; } return n; }
    function button(text, action) { const n = node('button', text, 'btn btn-sm btn-outline-secondary m-1'); n.type = 'button'; n.onclick = action; return n; }
    function newId() { const raw = new Uint8Array(10); crypto.getRandomValues(raw); return 'character_' + [...raw].map(b => b.toString(16).padStart(2, '0')).join(''); }
    function draft() { return {rows: s.rows, origins: s.origins, base: s.base, imported: s.imported, mapping: s.mapping, importBackup: s.importBackup}; }
    function fingerprint() { return JSON.stringify(draft()); }
    function remember() { if (s.meta && s.dirty) { recovery = {source: s.meta.source_sha256, saved: copy(s.meta.roster), draft: copy(draft())}; } }
    function sync() {
        const busy = !!s.pending || writes > 0;
        el('load').disabled = busy; el('fields').disabled = busy || !s.meta || s.uncertain;
        el('export').disabled = busy || !s.meta?.roster;
        el('recover').hidden = !recovery;
        el('recover').disabled = busy || !s.meta;
        el('preview').disabled = busy || !s.meta || !s.rows.length || s.uncertain;
        el('consent').disabled = busy || !s.preview?.review.can_approve || s.uncertain;
        el('approve').disabled = busy || !s.preview?.review.can_approve || !el('consent').checked
            || !!s.preview?.undo_available || !s.undo || s.undo.available || s.uncertain;
        el('undo').disabled = busy || !s.meta || !s.undo?.can_undo || s.uncertain;
        el('discard').disabled = busy || !s.undo?.revision || s.uncertain;
        el('previous').disabled = busy || s.page === 0;
        el('next').disabled = busy || (s.page + 1) * 20 >= s.rows.length;
        el('undo-status').textContent = !s.undo ? 'Load saved undo status before approving.' : !s.undo.available
            ? 'No saved character undo.' : s.undo.can_undo ? 'An exact undo is saved. Undo it or discard it before another approval.'
            : `The saved undo needs inspection: ${s.undo.reason || 'current files differ'}. ${s.undo.revision ? 'You can explicitly discard this record.' : 'It is preserved on disk; automatic discard is unavailable.'}`;
    }
    function invalidate(message) { s.edit++; s.dirty = true; s.preview = null; el('consent').checked = false; el('review').replaceChildren(); if (message) { status(message); } sync(); }
    window.resetCharacterRosterBook = () => {
        remember(); epoch++; s = fresh(); el('rows').replaceChildren(); el('review').replaceChildren(); el('import-review').replaceChildren();
        el('book').textContent = ''; el('consent').checked = false; status('Load character review for the selected book.'); sync();
    };
    window.ensureCharacterRosterEditsDiscardable = async () => {
        if (writes || s.uncertain) { showToast('Reload character review to confirm any pending approval before switching books.', 'warning'); return false; }
        if (!s.dirty) { return true; }
        const o = owner(), before = fingerprint();
        if (!await showConfirm('Discard unsaved character-review edits?', {title: 'Leave character review?', actionLabel: 'Discard draft', danger: true})) { return false; }
        return current(o) && before === fingerprint();
    };
    function setRows(base, rows) {
        s.base = base ? copy(base) : null; s.rows = copy(rows); s.origins = {};
        for (const row of s.rows) { s.origins[row.id] = base?.entries.some(e => e.id === row.id) ? [row.id] : []; }
        s.selected.clear(); s.page = 0;
    }
    async function load({keepDraft = null} = {}) {
        if (s.pending || writes || window._existingUploadSelectionPending) { return; }
        if (!keepDraft && s.dirty && !s.uncertain && !await window.ensureCharacterRosterEditsDiscardable()) { return; }
        const unresolved = s.uncertain;
        window.resetCharacterRosterBook(); const o = owner(), token = {}; s.pending = token; s.uncertain = unresolved; sync();
        try {
            const meta = await API.get(endpoint);
            if (!current(o, false)) { return; }
            if (!/^[0-9a-f]{64}$/.test(meta.context_revision) || !/^[0-9a-f]{64}$/.test(meta.source_sha256)
                || !(meta.undo_revision === null || (typeof meta.undo_revision === 'string' && /^[0-9a-f]{64}$/.test(meta.undo_revision)))
                || !Array.isArray(meta.cast) || (meta.roster !== null && !Array.isArray(meta.roster?.entries))) { throw new Error('Invalid character review response'); }
            s.meta = meta; setRows(meta.roster, meta.roster?.entries || []);
            el('book').textContent = `${meta.source_name}: ${meta.approval_current ? 'approved roster matches the current cast' : 'review or reconcile a draft for this source'}.`;
            if (keepDraft && keepDraft.source === meta.source_sha256 && JSON.stringify(keepDraft.saved) === JSON.stringify(meta.roster)) {
                Object.assign(s, copy(keepDraft.draft)); renderImportCancel();
                invalidate('Saved undo discarded. Draft edits kept; review them again before approval.');
            } else { status('Edit names and aliases, then choose source locations and review the draft. Finding a quote locates words; you decide which person it supports.'); }
            render();
        } catch (error) { if (current(o, false)) { status(`Character review could not load: ${error.message}`); } }
        try {
            const undo = await API.get(endpoint + '/undo');
            if (!undo || typeof undo.available !== 'boolean' || typeof undo.can_undo !== 'boolean'
                || !(undo.revision === null || (typeof undo.revision === 'string' && /^[0-9a-f]{64}$/.test(undo.revision)))
                || (undo.can_undo && (!undo.available || !undo.revision))) { throw new Error('Invalid saved undo response'); }
            if (current(o, false)) { s.undo = undo; }
        }
        catch (error) { if (current(o, false)) { status(`Saved undo status could not load: ${error.message}. Reload before approving or restoring.`); } }
        finally {
            if (current(o, false)) {
                if (s.meta && s.undo?.revision && s.meta.undo_revision !== s.undo.revision) {
                    s.undo = null; status('Book, source or saved undo changed during loading. Reload character review before another action.');
                }
                s.uncertain = unresolved && !(s.meta && s.undo);
                if (s.uncertain) { status('Saved-state readback is incomplete. Reload character review before switching books or trying another write.'); }
            }
            if (s.pending === token) { s.pending = null; } sync();
        }
    }
    function addCharacter() {
        if (s.rows.length >= 200) { throw new Error('A review supports at most 200 characters.'); }
        const row = {id: newId(), canonical_name: '', aliases: [], evidence: [{source_quote: '', start_char: null, end_char: null}]};
        s.rows.push(row); s.origins[row.id] = []; s.page = Math.floor((s.rows.length - 1) / 20); invalidate(); render();
    }
    function combine(rows, kept) {
        const aliases = [...new Set(rows.flatMap(r => [r.canonical_name, ...r.aliases]).filter(a => a.trim() && a.trim().toUpperCase() !== kept.canonical_name.trim().toUpperCase()).map(a => a.trim().toUpperCase()))];
        const evidence = [...new Map(rows.flatMap(r => r.evidence).map(e => [JSON.stringify(e), copy(e)])).values()];
        if (aliases.length > 32 || evidence.length > 8) { throw new Error('Keep at most 32 aliases and eight quotes when combining characters. Remove unneeded items first.'); }
        return {...copy(kept), aliases, evidence};
    }
    function merge() {
        const rows = s.rows.filter(r => s.selected.has(r.id)), keep = rows.find(r => r.id === el('survivor').value);
        if (rows.length < 2 || !keep) { throw new Error('Select at least two characters and choose the identity to keep.'); }
        const origins = [...new Set(rows.flatMap(r => s.origins[r.id]))];
        if (origins.length && !origins.includes(keep.id)) { throw new Error('Keep an existing identity when merging reviewed characters.'); }
        const merged = combine(rows, keep); s.rows = s.rows.filter(r => !s.selected.has(r.id) || r.id === keep.id).map(r => r.id === keep.id ? merged : r);
        if (s.mapping) { for (const [id, target] of Object.entries(s.mapping)) { if (s.selected.has(target)) { s.mapping[id] = keep.id; } } }
        s.origins[keep.id] = origins; s.selected.clear(); invalidate('Merged in the draft. Review the identities and aliases before approval.'); render();
    }
    function split() {
        const rows = s.rows.filter(r => s.selected.has(r.id)), name = el('split-name').value.trim();
        if (rows.length !== 1 || !name) { throw new Error('Select one character and enter the new character name.'); }
        const old = rows[0], origins = s.origins[old.id];
        if (origins.length > 1) { throw new Error('Approve this merge before splitting the merged identity.'); }
        if (s.rows.length >= 200) { throw new Error('A review supports at most 200 characters.'); }
        const added = {id: newId(), canonical_name: name, aliases: [], evidence: [{source_quote: '', start_char: null, end_char: null}]};
        if (el('split-new').checked) { const first = {...copy(old), id: newId()}; s.rows = s.rows.map(r => r.id === old.id ? first : r); s.origins[first.id] = [...origins]; }
        s.rows.push(added); s.origins[added.id] = [...origins]; s.selected.clear(); invalidate('Split in the draft. Check each character’s names and source quotes.'); render();
    }
    function getDecisions() {
        const base = s.base?.entries || [], old = new Map(base.map(r => [r.id, r])), pending = new Set(old.keys()), result = [];
        for (const row of s.rows.filter(r => !s.origins[r.id]?.length)) { result.push({action: 'add', from_ids: [], to_ids: [row.id]}); }
        while (pending.size) {
            const group = new Set([pending.values().next().value]); let rows = [], changed = true;
            while (changed) { changed = false; rows = s.rows.filter(r => (s.origins[r.id] || []).some(id => group.has(id))); for (const row of rows) { for (const id of s.origins[row.id]) { if (!group.has(id)) { group.add(id); changed = true; } } } }
            for (const id of group) { pending.delete(id); }
            const from = [...group], to = rows.map(r => r.id);
            if (!to.length) { for (const id of from) { result.push({action: 'retire', from_ids: [id], to_ids: []}); } }
            else if (from.length === 1 && to.length === 1 && from[0] === to[0]) {
                if (s.imported || !s.meta.approval_current || JSON.stringify(old.get(from[0])) !== JSON.stringify(rows[0])) { result.push({action: 'update', from_ids: from, to_ids: to}); }
            } else if (from.length >= 2 && to.length === 1 && group.has(to[0])) { result.push({action: 'merge', from_ids: from, to_ids: to}); }
            else if (from.length === 1 && to.length >= 2) { result.push({action: 'split', from_ids: from, to_ids: to}); }
            else { throw new Error('Approve a merge or split separately before combining both transformations.'); }
        }
        return result;
    }
    function request() { return {context_revision: s.meta.context_revision, proposal: {source_sha256: s.meta.source_sha256, entries: copy(s.rows), decisions: getDecisions()}, imported_roster: s.imported, import_id_mapping: s.mapping}; }
    function render() {
        const area = el('rows'); area.replaceChildren(); const o = owner(); el('survivor').replaceChildren();
        for (const row of s.rows.filter(r => s.selected.has(r.id))) { const option = node('option', row.canonical_name || 'Unnamed character'); option.value = row.id; el('survivor').append(option); }
        s.page = Math.min(s.page, Math.max(0, Math.ceil(s.rows.length / 20) - 1));
        for (const row of s.rows.slice(s.page * 20, s.page * 20 + 20)) {
            const box = node('div', undefined, 'border rounded p-2 mb-2'), selected = node('input'); selected.type = 'checkbox'; selected.checked = s.selected.has(row.id);
            selected.onchange = () => { if (!current(o, false)) { return; } if (selected.checked) { s.selected.add(row.id); } else { s.selected.delete(row.id); } render(); };
            const label = node('label', ' Select character ', 'small'); label.prepend(selected); box.append(label);
            for (const [caption, value, save, max] of [
                ['Character name', row.canonical_name, v => { row.canonical_name = v; }, 2048],
                ['Aliases, one per line', row.aliases.join('\n'), v => { row.aliases = v.split('\n').map(a => a.trim()).filter(Boolean); }, 65568]]) {
                const field = node('textarea'); field.value = value; field.rows = caption.startsWith('Aliases') ? 2 : 1; field.maxLength = max; field.className = 'form-control form-control-sm mb-2';
                const l = node('label', caption, 'small d-block'); l.append(field); box.append(l);
                field.oninput = () => { if (current(o, false)) { save(field.value); invalidate(); } };
            }
            for (const evidence of row.evidence) {
                const quote = node('textarea'); quote.value = evidence.source_quote; quote.maxLength = 8000; quote.rows = 2; quote.className = 'form-control form-control-sm';
                const l = node('label', 'Supporting source quote', 'small d-block'); l.append(quote); box.append(l);
                const location = node('div', evidence.start_char === null ? 'Choose a source location.' : `Characters ${evidence.start_char + 1}–${evidence.end_char}.`, 'small'); box.append(location);
                quote.oninput = () => { if (current(o, false)) { evidence.source_quote = quote.value; evidence.start_char = evidence.end_char = null; location.textContent = 'Choose a source location.'; invalidate(); } };
                const choices = node('div'); box.append(button('Find quote in source', () => findQuote(row, evidence, choices)), button('Remove quote', () => { if (current(o, false)) { row.evidence = row.evidence.filter(e => e !== evidence); invalidate(); render(); } }), choices);
            }
            box.append(button('Add supporting quote', () => { if (!current(o, false)) { return; } if (row.evidence.length >= 8) { status('Each character supports at most eight quotes.'); return; } row.evidence.push({source_quote: '', start_char: null, end_char: null}); invalidate(); render(); }));
            area.append(box);
        }
        el('page').textContent = `${s.rows.length ? s.page * 20 + 1 : 0}–${Math.min((s.page + 1) * 20, s.rows.length)} of ${s.rows.length} characters`; sync();
    }
    async function findQuote(row, evidence, area, offset = 0) {
        if (s.pending || !evidence.source_quote.trim()) { return; }
        const o = owner(), token = {}; s.pending = token; sync();
        try {
            const result = await API.post(endpoint + '/source', {context_revision: s.meta.context_revision, quote: evidence.source_quote, offset});
            if (!current(o)) { return; } area.replaceChildren();
            for (const match of result.matches) { area.append(button(`Use characters ${match.start_char + 1}–${match.end_char}: …${match.before}${match.source_quote}${match.after}…`, () => {
                if (!current(o) || !s.rows.includes(row) || !row.evidence.includes(evidence)) { return; }
                Object.assign(evidence, {source_quote: match.source_quote, start_char: match.start_char, end_char: match.end_char}); invalidate(); render();
            })); }
            if (result.next_offset !== null) { area.append(button('More occurrences', () => findQuote(row, evidence, area, result.next_offset))); }
            if (!result.matches.length) { area.append(node('p', 'This quote was not found. Check it against the current source.')); }
        } catch (error) { if (current(o)) { status(`Quote lookup failed: ${error.message}`); } }
        finally { if (s.pending === token) { s.pending = null; } sync(); }
    }
    function validateImport(value) {
        const text = (v, max) => typeof v === 'string' && v.trim() && [...v].length <= max && ![...v].some(c => c.codePointAt(0) >= 0xD800 && c.codePointAt(0) <= 0xDFFF);
        const keys = (v, names) => v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).sort().join('|') === [...names].sort().join('|');
        if (!keys(value, ['schema', 'source_sha256', 'source_characters', 'entries', 'retired_ids']) || value.schema !== 1 || typeof value.source_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(value.source_sha256)
            || !Number.isInteger(value.source_characters) || value.source_characters < 1 || value.source_characters > 1200000 || !Array.isArray(value.entries) || !value.entries.length || value.entries.length > 200
            || !Array.isArray(value.retired_ids) || value.retired_ids.length > 2000 || value.retired_ids.some(id => typeof id !== 'string' || !idPattern.test(id)) || new Set(value.retired_ids).size !== value.retired_ids.length) { throw new Error('Choose an exported approved roster with valid source coordinates and character IDs.'); }
        const seen = new Set(value.retired_ids);
        for (const row of value.entries) {
            if (!keys(row, ['id', 'canonical_name', 'aliases', 'evidence']) || typeof row.id !== 'string' || !idPattern.test(row.id) || seen.has(row.id) || !text(row.canonical_name, 1024) || !Array.isArray(row.aliases) || row.aliases.length > 32 || row.aliases.some(a => !text(a, 1024))
                || !Array.isArray(row.evidence) || !row.evidence.length || row.evidence.length > 8) { throw new Error('An imported character needs inspection.'); }
            seen.add(row.id);
            for (const e of row.evidence) { if (!keys(e, ['source_quote', 'start_char', 'end_char']) || !text(e.source_quote, 4000) || !((e.start_char === null && e.end_char === null) || (Number.isInteger(e.start_char) && Number.isInteger(e.end_char) && e.start_char >= 0 && e.end_char > e.start_char && e.end_char <= value.source_characters))) { throw new Error('Imported quote coordinates are invalid.'); } }
        }
        return value;
    }
    async function importFile() {
        const file = el('import').files[0]; if (!file || s.pending) { return; }
        if (s.imported) { status('Finish or cancel the current import before opening another.'); return; }
        const o = owner(), token = {}; s.pending = token; sync();
        try {
            if (file.size > 2 * 1024 ** 2) { throw new Error('The import exceeds 2 MiB.'); }
            const imported = validateImport(JSON.parse(await file.text())); if (!current(o)) { return; }
            s.candidate = {roster: imported, choices: {}}; const area = el('import-review'); area.replaceChildren();
            area.append(node('p', s.meta.roster ? 'Choose the identity for each imported character. Matching names are not merged automatically.' : 'Use the imported identities as a starting point, then reconcile every quote with the current source.'));
            for (const row of imported.entries) {
                const label = node('label', row.canonical_name, 'small d-block'), select = node('select'); select.className = 'form-select form-select-sm';
                const choices = [[row.id, 'Keep imported identity'], [newId(), 'Use a new identity'], ...s.rows.map(r => [r.id, `Use current character: ${r.canonical_name || 'Unnamed'}`])];
                for (const [id, name] of choices) { const option = node('option', name); option.value = id; select.append(option); }
                select.value = row.id; s.candidate.choices[row.id] = select.value;
                select.onchange = () => { if (current(o, false) && s.candidate?.roster === imported) { s.candidate.choices[row.id] = select.value; } };
                select.disabled = !s.meta.roster; label.append(select); area.append(label);
            }
            area.append(button('Use this import in the draft', () => safe(useImport)));
        } catch (error) { if (current(o)) { status(`Import could not load: ${error.message}`); } }
        finally { if (s.pending === token) { s.pending = null; } el('import').value = ''; sync(); }
    }
    function useImport() {
        if (!s.candidate || s.pending) { return; } const imported = s.candidate.roster;
        s.importBackup = copy(draft());
        if (!s.meta.roster) { setRows(imported, imported.entries); s.mapping = null; }
        else {
            const rows = copy(s.rows), origins = copy(s.origins), mapping = copy(s.candidate.choices);
            for (const item of imported.entries) {
                const target = mapping[item.id]; let at = rows.findIndex(r => r.id === target);
                if (at < 0) { const added = {...copy(item), id: target}; rows.push(added); origins[target] = s.base.entries.some(r => r.id === target) ? [target] : []; }
                else { rows[at] = combine([rows[at], item], rows[at]); }
            }
            if (rows.length > 200) { throw new Error('This import would exceed 200 characters.'); }
            s.rows = rows; s.origins = origins; s.mapping = mapping;
        }
        s.imported = copy(imported); s.candidate = null; renderImportCancel(); invalidate('Import is a draft. Review mapped identities, retired IDs and current source quotes before approval.'); render();
    }
    function renderImportCancel() {
        el('import-review').replaceChildren();
        if (!s.imported || !s.importBackup) { return; }
        el('import-review').append(button('Cancel this import and its draft changes', async () => {
            const o = owner(), f = fingerprint(); if (!await showConfirm('Discard this import and changes made after it?', {danger: true}) || !current(o) || f !== fingerprint()) { return; }
            Object.assign(s, s.importBackup); s.imported = null; s.mapping = null; s.importBackup = null; s.selected.clear(); el('import-review').replaceChildren(); invalidate(); render();
        }));
    }
    async function preview() {
        if (s.pending || !s.meta || s.uncertain || window._existingUploadSelectionPending) { return; }
        const payload = request(), o = owner(), token = {}; s.pending = token; s.preview = null; el('consent').checked = false; sync();
        try {
            const plan = await API.post(endpoint + '/preview', payload); if (!current(o)) { return; }
            s.preview = plan; const area = el('review'); area.replaceChildren();
            area.append(node('p', plan.cast_changed ? 'Approval will update the cast used for future script generation.' : 'The cast projection stays the same; this records the reviewed character evidence.'));
            const names = new Map([...(s.base?.entries || []), ...s.rows].map(r => [r.id, r.canonical_name]));
            for (const d of plan.review.decisions) { area.append(node('p', `${d.action}: ${d.from_ids.map(id => names.get(id) || 'Retired character').join(', ') || 'New'} → ${d.to_ids.map(id => names.get(id)).join(', ') || 'Retired'}`, 'small')); }
            if (plan.review.import_review) { area.append(node('p', `This import retains ${plan.review.import_review.retired_ids.length} retired or remapped identities in the history.`, 'small')); }
            for (const m of plan.review.import_review?.mappings || []) { area.append(node('p', `Imported ${m.imported_name} → ${m.target_name}`, 'small')); }
            for (const row of plan.review.evidence.entries) { for (const e of row.evidence) { area.append(node('p', `${row.canonical_name}: ${e.status} — ${e.source_quote}`, 'small')); } }
            status(plan.review.can_approve ? 'Review these identity decisions and quote locations, then check the confirmation before approving.' : 'Resolve the unanchored, ambiguous or unsupported quotes before approval.');
        } catch (error) { if (current(o)) { status(`Draft review failed: ${error.message}`); } }
        finally { if (s.pending === token) { s.pending = null; } sync(); }
    }
    async function mutate(kind) {
        if (s.pending || writes || s.uncertain || window._existingUploadSelectionPending) { return; }
        if (kind === 'apply' && (!s.preview?.review.can_approve || !el('consent').checked || s.preview.undo_available || !s.undo || s.undo.available)) { return; }
        if (kind !== 'apply' && !s.undo?.revision) { return; }
        if (kind === 'undo' && (!s.meta || !s.undo.can_undo)) { return; }
        const o = owner(), f = fingerprint(), undo = s.undo;
        if (kind === 'undo' && s.dirty && (!await window.ensureCharacterRosterEditsDiscardable() || !current(o) || f !== fingerprint() || s.undo !== undo)) { return; }
        if (kind === 'discard_undo' && (!await showConfirm('Discard the saved exact character undo? Current characters and cast will stay.', {danger: true}) || !current(o) || f !== fingerprint() || s.undo !== undo)) { return; }
        const keepDraft = kind === 'discard_undo' && s.dirty ? {source: s.meta.source_sha256, saved: copy(s.meta.roster), draft: copy(draft())} : null;
        const payload = kind === 'apply' ? {...request(), expected_revision: s.preview.revision} : {expected_revision: s.meta ? s.meta.undo_revision : undo.revision};
        const token = {}; s.pending = token; s.preview = null; writes++; sync();
        try {
            await API.post(endpoint + '/' + kind, payload);
            if (!current(o)) { return; } if (kind !== 'apply') { remember(); } s.dirty = false; status(kind === 'apply' ? 'Roster approved. Reloading its saved state…' : 'Saved character undo updated. Reloading…');
        } catch (error) { if (current(o, false)) { s.uncertain = true; status(`The write could not be confirmed: ${error.message}. Reload to inspect saved state before another action.`); } }
        finally { writes--; if (s.pending === token) { s.pending = null; } sync(); }
        if (current(o) && !s.uncertain) { await load({keepDraft}); }
    }
    function safe(action) { try { const result = action(); if (result?.catch) { result.catch(error => status(error.message)); } } catch (error) { status(error.message); } }
    el('load').onclick = () => load(); el('add').onclick = () => safe(addCharacter); el('merge').onclick = () => safe(merge); el('split').onclick = () => safe(split);
    el('retire').onclick = () => safe(() => { s.rows = s.rows.filter(r => !s.selected.has(r.id)); s.selected.clear(); invalidate('Retired in the draft. Review before approval.'); render(); });
    el('seed').onclick = () => safe(() => { if (s.rows.length || s.base) { throw new Error('Start with an empty draft to use the current cast.'); } if (s.meta.cast.length > 200) { throw new Error('Add a reviewed subset of this cast; the roster supports 200 characters.'); } setRows(null, s.meta.cast.map(r => ({id: newId(), canonical_name: r.name, aliases: [...r.aliases], evidence: [{source_quote: '', start_char: null, end_char: null}]}))); invalidate(); render(); });
    el('previous').onclick = () => { s.page--; render(); }; el('next').onclick = () => { s.page++; render(); };
    el('import').onchange = importFile; el('preview').onclick = () => safe(preview); el('consent').onchange = sync;
    el('approve').onclick = () => safe(() => mutate('apply')); el('undo').onclick = () => safe(() => mutate('undo')); el('discard').onclick = () => safe(() => mutate('discard_undo'));
    el('recover').onclick = () => safe(() => { if (!recovery || !s.meta || recovery.source !== s.meta.source_sha256 || JSON.stringify(recovery.saved) !== JSON.stringify(s.meta.roster)) { throw new Error('The saved draft belongs to a different source or roster. Keep it for inspection; load its original book to restore it.'); } Object.assign(s, copy(recovery.draft)); s.selected.clear(); s.page = 0; renderImportCancel(); invalidate('Previous unsaved draft restored. Review it against this source before approval.'); render(); });
    el('export').onclick = () => { if (!s.meta?.roster || s.pending) { return; } const blob = new Blob([JSON.stringify(s.meta.roster, null, 2)], {type: 'application/json'}), url = URL.createObjectURL(blob), link = node('a'); link.href = url; link.download = s.meta.source_name.replace(/[\\/]/g, '_') + '.characters.json'; link.click(); URL.revokeObjectURL(url); };
    sync();
})();
