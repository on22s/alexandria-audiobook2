// Source choices fill untouched blanks; entered and intentionally cleared fields win.
let m4bPublicationEpoch = 0, m4bPublicationEdit = 0, m4bPublicationBusy = false;
let m4bPublicationPreview = null, m4bPublicationCover = null, m4bPublicationBook = null;
const m4bPublicationEdited = new Set(), m4bPublicationApplied = new Map();
const m4bPublicationFields = ['title', 'author', 'narrator', 'year', 'description'];
const m4bPub = id => document.getElementById('m4b-publication-' + id);
const m4bField = name => document.getElementById('m4b-' + name);

function getM4bPublicationRow() {
    return m4bPublicationPreview?.candidates.find(row => row.id === m4bPub('origin').value) || null;
}
function isM4bPublicationReviewRequired(row) {
    return !!row && (m4bPublicationPreview.binding_status !== 'current'
        || m4bPublicationPreview.candidates.length > 1 || row.other_creators.length > 0
        || Object.values(row.fields).some(values => values.length > 1));
}

function updateM4bPublicationControls() {
    const row = getM4bPublicationRow();
    m4bPub('review').disabled = m4bPublicationBusy;
    m4bPub('origin').disabled = m4bPublicationBusy || !m4bPublicationPreview?.candidates.length;
    let chosen = false;
    for (const name of ['title', 'author']) {
        const select = m4bPub(name + '-choice');
        const manual = m4bPublicationEdited.has(name) || m4bField(name).value !== '';
        if (manual) { select.value = ''; }
        select.disabled = m4bPublicationBusy || !row?.fields[name].length || manual;
        chosen ||= !!row && !manual && select.value !== '';
    }
    const cover = m4bPub('cover-choice');
    const usableCover = row?.cover_status === 'available' && hasM4bPublicationImage(row)
        && !m4bPublicationPreview?.uploaded_cover.present;
    cover.disabled = m4bPublicationBusy || !usableCover;
    if (!usableCover) { cover.checked = false; }
    const sameCover = m4bPublicationCover?.origin_id === row?.id
        && m4bPublicationCover?.expected_revision === m4bPublicationPreview?.revision;
    chosen ||= usableCover && cover.checked && !sameCover;
    m4bPub('reviewed').disabled = m4bPublicationBusy || !row;
    m4bPub('review-required').hidden = !isM4bPublicationReviewRequired(row);
    m4bPub('apply').disabled = m4bPublicationBusy || !chosen
        || (isM4bPublicationReviewRequired(row) && !m4bPub('reviewed').checked);
    m4bPub('clear-cover').disabled = !m4bPublicationCover && !cover.checked;
    m4bPub('clear').disabled = m4bPublicationBusy || (!m4bPublicationApplied.size && !m4bPublicationCover);
    m4bPub('cover-status').textContent = m4bPublicationCover ? 'Source cover selected for export.' : 'No source cover selected.';
}
function clearM4bPublicationOwnedFields() {
    for (const [name, value] of m4bPublicationApplied) {
        if (m4bField(name).value === value) { m4bField(name).value = ''; m4bPublicationEdited.delete(name); }
    }
    m4bPublicationApplied.clear(); m4bPublicationCover = null; m4bPublicationBook = null;
}
function resetM4bPublicationBook() {
    m4bPublicationEpoch += 1; m4bPublicationEdit += 1; m4bPublicationPreview = null;
    clearM4bPublicationOwnedFields();
    m4bPub('info').replaceChildren(); m4bPub('image').hidden = true; m4bPub('image').removeAttribute('src');
    m4bPub('feedback').textContent = 'Book changed. Review source suggestions again.'; updateM4bPublicationControls();
}
function invalidateM4bPublicationCover() {
    m4bPublicationEpoch += 1; m4bPublicationEdit += 1; m4bPublicationPreview = null; m4bPublicationCover = null;
    if (!m4bPublicationApplied.size) { m4bPublicationBook = null; }
    m4bPub('info').replaceChildren(); m4bPub('image').hidden = true; m4bPub('image').removeAttribute('src');
    m4bPub('feedback').textContent = 'Cover options changed. Review source suggestions again.'; updateM4bPublicationControls();
}
function clearM4bSourceCover() {
    m4bPublicationEdit += 1; m4bPublicationCover = null; m4bPub('cover-choice').checked = false;
    if (!m4bPublicationApplied.size) { m4bPublicationBook = null; }
    m4bPub('feedback').textContent = 'Source-cover choice cleared.'; updateM4bPublicationControls();
}
function clearM4bAppliedSuggestions() {
    m4bPublicationEdit += 1; clearM4bPublicationOwnedFields(); renderM4bPublicationCandidate();
    m4bPub('feedback').textContent = 'Source-applied suggestions cleared. Entered values were kept.';
}
function renderM4bPublicationCandidate() {
    const row = getM4bPublicationRow(), info = m4bPub('info'); info.replaceChildren();
    const line = text => { const node = document.createElement('p'); node.className = 'small mb-1'; node.textContent = text; info.appendChild(node); };
    for (const name of ['title', 'author']) {
        const select = m4bPub(name + '-choice'); select.replaceChildren();
        const keep = document.createElement('option'); keep.value = ''; keep.textContent = 'Keep current ' + name; select.appendChild(keep);
        for (const [index, value] of (row?.fields[name] || []).entries()) {
            const option = document.createElement('option'); option.value = String(index); option.textContent = value; select.appendChild(option);
        }
        select.value = '';
    }
    const sameCover = !!m4bPublicationCover && m4bPublicationCover.origin_id === row?.id
        && m4bPublicationCover.expected_revision === m4bPublicationPreview?.revision;
    m4bPub('cover-choice').checked = sameCover; m4bPub('reviewed').checked = sameCover && m4bPublicationCover.reviewed;
    const image = m4bPub('image'); image.hidden = true; image.removeAttribute('src');
    if (row) {
        line('Captured from ' + row.origin.filename + ' · ' + row.origin.sha256);
        line('Selected input: ' + m4bPublicationPreview.source_name + '. '
            + (m4bPublicationPreview.binding_status === 'current' ? 'Capture matches this input.' : 'Capture is from an earlier input; review it before use.'));
        for (const name of ['title', 'author']) { line('Source ' + name + ': ' + (row.fields[name].join(' / ') || 'Not provided')); }
        for (const creator of row.other_creators) { line('Other creator: ' + creator.name + ' (' + creator.roles.join(', ') + ')'); }
        for (const warning of row.warnings) { line(warning); }
        if (hasM4bPublicationImage(row)) { image.setAttribute('src', row.cover.preview_data_url); image.hidden = false; }
        if (m4bPublicationPreview.uploaded_cover.present) { line('An uploaded cover has priority. Remove it and review again to choose a source cover.'); }
    }
    updateM4bPublicationControls();
}

async function previewM4bPublication() {
    if (m4bPublicationBusy) { return; }
    if (window._existingUploadSelectionPending) { m4bPub('feedback').textContent = 'Wait for book selection to finish.'; return; }
    const epoch = ++m4bPublicationEpoch, selection = window._existingUploadSelectionRequest;
    m4bPublicationBusy = true;
    m4bPub('feedback').textContent = 'Reading captured source suggestions…'; updateM4bPublicationControls();
    try {
        const data = await API.get('/api/m4b_publication');
        if (epoch !== m4bPublicationEpoch || selection !== window._existingUploadSelectionRequest) { return; }
        if (!isM4bPublicationPreviewValid(data)) { throw Error('Source suggestions were incomplete. Review again.'); }
        if (m4bPublicationBook && m4bPublicationBook !== data.book_token) { clearM4bPublicationOwnedFields(); }
        if (m4bPublicationCover && (m4bPublicationCover.expected_revision !== data.revision
                || !data.candidates.some(row => row.id === m4bPublicationCover.origin_id))) {
            m4bPublicationCover = null;
            if (!m4bPublicationApplied.size) { m4bPublicationBook = null; }
        }
        m4bPublicationPreview = data; const origins = m4bPub('origin'); origins.replaceChildren();
        const choose = document.createElement('option'); choose.value = ''; choose.textContent = 'Choose a captured origin'; origins.appendChild(choose);
        for (const row of data.candidates) { const option = document.createElement('option'); option.value = row.id; option.textContent = row.origin.filename + ' · ' + row.origin.sha256.slice(0, 12); origins.appendChild(option); }
        origins.value = m4bPublicationCover?.origin_id || (data.candidates.length === 1 ? data.candidates[0].id : ''); renderM4bPublicationCandidate();
        m4bPub('feedback').textContent = data.candidates.length ? 'Choose the suggestions to apply. Entered or cleared fields are kept.' : 'No captured EPUB suggestions. Import an EPUB or enter metadata manually.';
    } catch (error) {
        if (epoch === m4bPublicationEpoch && selection === window._existingUploadSelectionRequest) { m4bPub('feedback').textContent = 'Could not refresh suggestions; any displayed review is from an earlier reading. ' + error.message; }
    } finally { m4bPublicationBusy = false; updateM4bPublicationControls(); }
}
async function applyM4bPublication() {
    if (m4bPublicationBusy || m4bPub('apply').disabled) { return; }
    const preview = m4bPublicationPreview, row = getM4bPublicationRow();
    const epoch = m4bPublicationEpoch, edit = m4bPublicationEdit, selection = window._existingUploadSelectionRequest;
    const values = Object.fromEntries(m4bPublicationFields.map(name => [name, m4bField(name).value]));
    const request = {expected_book_token: preview.book_token, expected_revision: preview.revision,
        origin_id: row.id, reviewed: m4bPub('reviewed').checked, select_cover: m4bPub('cover-choice').checked};
    for (const name of ['title', 'author']) { request[name + '_index'] = m4bPub(name + '-choice').disabled || m4bPub(name + '-choice').value === '' ? null : Number(m4bPub(name + '-choice').value); }
    m4bPublicationBusy = true; updateM4bPublicationControls();
    try {
        const resolved = await API.post('/api/m4b_publication/resolve', request);
        if (epoch !== m4bPublicationEpoch || selection !== window._existingUploadSelectionRequest) { return; }
        if (edit !== m4bPublicationEdit || m4bPublicationFields.some(name => m4bField(name).value !== values[name])) { throw Error('Values changed while resolving. Review and apply choices again.'); }
        const wanted = ['title', 'author'].filter(name => request[name + '_index'] !== null);
        if (resolved.book_token !== preview.book_token || resolved.revision !== preview.revision || resolved.origin_id !== row.id
                || !resolved.fields || Object.keys(resolved.fields).length !== wanted.length
                || wanted.some(name => resolved.fields[name] !== row.fields[name][request[name + '_index']])
                || (request.select_cover ? !resolved.cover_selection || Object.keys(resolved.cover_selection).length !== 4 || ['expected_book_token', 'expected_revision', 'origin_id', 'reviewed'].some(key => resolved.cover_selection[key] !== request[key]) : resolved.cover_selection !== null)) {
            throw Error('Source choices were incomplete. Review again.');
        }
        for (const name of wanted) { m4bField(name).value = resolved.fields[name]; m4bPublicationApplied.set(name, resolved.fields[name]); }
        if (request.select_cover) { m4bPublicationCover = {...resolved.cover_selection}; }
        m4bPublicationBook = preview.book_token; m4bPub('feedback').textContent = 'Choices applied to editable fields and the source-cover selection.';
    } catch (error) {
        if (epoch === m4bPublicationEpoch && selection === window._existingUploadSelectionRequest) { m4bPub('feedback').textContent = error.message; }
    } finally { m4bPublicationBusy = false; updateM4bPublicationControls(); }
}
async function getM4bPublicationPayload() {
    if (m4bPublicationBusy || window._existingUploadSelectionPending) { throw Error('Wait for source review or book selection to finish.'); }
    const epoch = m4bPublicationEpoch, edit = m4bPublicationEdit, selection = window._existingUploadSelectionRequest;
    const values = Object.fromEntries(m4bPublicationFields.map(name => [name, m4bField(name).value]));
    const payload = Object.fromEntries(m4bPublicationFields.map(name => [name,
        values[name] === '' && !m4bPublicationEdited.has(name) ? null : values[name]]));
    const flags = ['m4b-per-chunk', 'm4b-require-ready'].map(id => [id, document.getElementById(id)?.checked]);
    const book = m4bPublicationBook, cover = m4bPublicationCover;
    if (book) {
        const current = await API.get('/api/workspace');
        if (epoch !== m4bPublicationEpoch || edit !== m4bPublicationEdit || selection !== window._existingUploadSelectionRequest
                || m4bPublicationFields.some(name => m4bField(name).value !== values[name])
                || flags.some(([id, checked]) => document.getElementById(id)?.checked !== checked)) { throw Error('M4B options changed. Export again.'); }
        if (current?.book_token !== book) { throw Error('Selected book changed. Review source suggestions again.'); }
        payload.expected_book_token = book;
        if (cover) { payload.source_cover = {...cover}; }
    }
    return payload;
}
function onM4bPublicationFieldEdit(name) {
    m4bPublicationEdit += 1; m4bPublicationEdited.add(name); m4bPublicationApplied.delete(name);
    if (!m4bPublicationApplied.size && !m4bPublicationCover) { m4bPublicationBook = null; }
    updateM4bPublicationControls();
}
function clearM4bTitle() {
    m4bField('title').value = ''; onM4bPublicationFieldEdit('title');
}
for (const name of m4bPublicationFields) {
    m4bField(name).addEventListener('input', () => onM4bPublicationFieldEdit(name));
}
m4bPub('origin').addEventListener('change', () => {
    m4bPublicationEdit += 1; m4bPublicationCover = null;
    if (!m4bPublicationApplied.size) { m4bPublicationBook = null; }
    renderM4bPublicationCandidate();
});
for (const id of ['title-choice', 'author-choice', 'reviewed']) {
    m4bPub(id).addEventListener('change', () => { m4bPublicationEdit += 1; updateM4bPublicationControls(); });
}
m4bPub('cover-choice').addEventListener('change', () => {
    m4bPublicationEdit += 1;
    if (!m4bPub('cover-choice').checked) {
        m4bPublicationCover = null;
        if (!m4bPublicationApplied.size) { m4bPublicationBook = null; }
    }
    updateM4bPublicationControls();
});
updateM4bPublicationControls();
