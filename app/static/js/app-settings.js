// Setup discovery reads labels and help, never configuration values.
function isSetupSettingAvailable(control) {
    const form = document.getElementById('config-form');
    if (!form?.contains(control) || control.disabled || control.type === 'hidden') { return false; }
    for (let node = control; node && node !== form; node = node.parentElement) {
        // An inactive workspace category is navigable; conditional backend groups are not.
        if (node.matches('[data-settings-section]')) { continue; }
        if (node.hidden || node.classList.contains('collapsing')) { return false; }
        if (getComputedStyle(node).display === 'none' && !node.classList.contains('collapse')) { return false; }
    }
    return true;
}

function getSetupSettingsMatches(query) {
    const terms = query.trim().toLowerCase().slice(0, 120).split(/\s+/).filter(Boolean);
    if (!terms.length) { return []; }
    const matches = [];
    for (const control of document.querySelectorAll('#config-form input, #config-form select, #config-form textarea')) {
        if (!control.id || !isSetupSettingAvailable(control)) { continue; }
        const label = Array.from(control.labels || []).map(node => node.textContent.trim()).join(' ').replace(/\s+/g, ' ');
        if (!label) { continue; }
        const section = control.closest('[data-settings-section]')?.getAttribute('data-settings-section') || 'Setup';
        const description = getItemHelpText(control);
        const text = `${label} ${section} ${description}`.toLowerCase();
        if (terms.every(term => text.includes(term))) { matches.push({id: control.id, label, section}); }
    }
    return matches;
}

function renderSetupSettingsSearch() {
    const search = document.getElementById('setup-settings-search');
    const results = document.getElementById('setup-settings-results');
    const status = document.getElementById('setup-settings-search-status');
    const query = search.value.trim();
    results.replaceChildren();
    results.hidden = !query;
    document.getElementById('setup-settings-clear').hidden = !query;
    if (!query) { status.textContent = ''; return; }
    const matches = getSetupSettingsMatches(query);
    status.textContent = !matches.length ? 'No matching settings for your current services. Try another label or description.'
        : `${matches.length} matching setting${matches.length === 1 ? '' : 's'}.${matches.length > 8 ? ' Showing the first 8; refine your search for more specific results.' : ''}`;
    for (const match of matches.slice(0, 8)) {
        const row = document.createElement('div');
        row.className = 'settings-search-hit';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-outline-primary';
        button.setAttribute('data-help-key', 'openSetupSetting');
        button.textContent = `${match.label} · ${match.section}`;
        button.addEventListener('click', () => openSetupSetting(match.id));
        row.appendChild(button);
        results.appendChild(row);
    }
}

function openSetupSetting(id) {
    const control = document.getElementById(id);
    if (!control || !isSetupSettingAvailable(control)) {
        renderSetupSettingsSearch();
        return;
    }
    const results = document.getElementById('setup-settings-results');
    results.replaceChildren();
    results.hidden = true;
    document.getElementById('setup-settings-search-status').textContent = 'Showing the original setting. Save Configuration to apply any edits.';
    openConfigField(control);
}

function openSetupSettingsSection(id, focusHeading = true) {
    const section = document.getElementById(id);
    if (!section?.matches('[data-settings-section]')) { return; }
    for (const panel of document.querySelectorAll('#config-form [data-settings-section]')) {
        panel.hidden = panel !== section;
    }
    for (const button of document.querySelectorAll('[data-settings-target]')) {
        button.setAttribute('aria-current', String(button.getAttribute('data-settings-target') === id));
    }
    document.getElementById('setup-settings-category').value = id;
    if (focusHeading) {
        const heading = section.querySelector('h5');
        heading.focus({preventScroll: true});
        heading.scrollIntoView({block: 'start', behavior: 'instant'});
    }
}

// Browser constraint validation happens before submit. Reveal only the first
// rejected control so later invalid events cannot hide the browser's focus target.
function onSetupSettingsInvalid(event) {
    const form = document.getElementById('config-form');
    if (form.querySelector('input:invalid, select:invalid, textarea:invalid') === event.target) { openConfigField(event.target); }
}

document.getElementById('config-form').addEventListener('invalid', onSetupSettingsInvalid, true);

function clearSetupSettingsSearch() {
    const search = document.getElementById('setup-settings-search');
    search.value = '';
    renderSetupSettingsSearch();
    search.focus();
}

function onSetupSettingsSearchKeydown(event) {
    if (event.key === 'Escape') {
        event.preventDefault();
        clearSetupSettingsSearch();
    }
}

document.getElementById('config-form').addEventListener('change', () => {
    if (!document.getElementById('setup-settings-results').hidden) { renderSetupSettingsSearch(); }
});

// Preview is read-only. A new edit/save invalidates this comparison snapshot.
let setupPreviewRequest = 0;
let setupPreviewPending = null;

function invalidateSetupPreview() {
    setupPreviewRequest++;
    document.getElementById('setup-preview-results').hidden = true;
    applySystemStatusText('setup-preview-status', 'Settings or saved state may have changed. Preview again before relying on this comparison.');
}

function closeSetupPreview() {
    invalidateSetupPreview();
    document.getElementById('setup-preview-panel').hidden = true;
}

function renderSetupPreviewRows(container, rows) {
    container.replaceChildren();
    for (const row of rows) {
        const item = document.createElement('div'); item.className = 'settings-preview-row';
        const label = document.createElement('strong'); label.textContent = row.label;
        const scope = document.createElement('div'); scope.className = 'small'; scope.textContent = row.scope;
        const values = document.createElement('div'); values.className = 'settings-preview-values small';
        for (const [name, text] of [['Saved', row.before + (row.was_default ? ' (default)' : '')], ['Would save', row.after]]) {
            const column = document.createElement('div');
            const heading = document.createElement('strong'); heading.textContent = name;
            const value = document.createElement('div'); value.textContent = text;
            column.append(heading, value); values.appendChild(column);
        }
        item.append(label, scope, values); container.appendChild(item);
    }
}

function isSetupPreviewValid(data) {
    const count = value => Number.isSafeInteger(value) && value >= 0;
    const rows = items => Array.isArray(items) && items.length <= 500 && items.every(row => row
        && ['path', 'label', 'scope', 'before', 'after'].every(key => typeof row[key] === 'string')
        && typeof row.was_default === 'boolean');
    return !!data && rows(data.changes) && rows(data.defaults) && ['local', 'remote'].includes(data.active_after)
        && ['read', 'missing', 'damaged'].includes(data.baseline) && count(data.warning_count)
        && count(data.unmanaged_fields) && typeof data.mirror_alignment === 'boolean'
        && typeof data.notice === 'string' && typeof data.timing === 'string';
}

async function previewSetupChanges() {
    if (configSavePending) { return; }
    invalidateSetupPreview();
    let proposal;
    try { proposal = getConfigPreviewProposal(); }
    catch (error) { showConfigValidationError(error); return; }
    const captured = JSON.stringify(proposal);
    const request = ++setupPreviewRequest;
    document.getElementById('setup-preview-panel').hidden = false;
    document.getElementById('setup-preview-results').hidden = true;
    applySystemStatusText('setup-preview-status', 'Comparing the proposed settings with the saved configuration…');
    if (setupPreviewPending) { await setupPreviewPending.catch(() => {}); }
    if (request !== setupPreviewRequest) { return; }
    const pending = Promise.resolve().then(() => API.post('/api/config/preview', proposal));
    setupPreviewPending = pending;
    try {
        const data = await pending;
        if (request !== setupPreviewRequest) { return; }
        if (JSON.stringify(getConfigPreviewProposal()) !== captured) { invalidateSetupPreview(); return; }
        if (!isSetupPreviewValid(data)) { throw new Error('Incomplete settings preview'); }
        const baseline = data.baseline === 'read' && !data.warning_count ? ''
            : data.baseline === 'missing' ? 'No saved configuration was found. '
            : 'Saved configuration is damaged or incomplete; recovered/default values are shown. ';
        applySystemStatusText('setup-preview-status', baseline + `${data.changes.length} supported settings would change; ${data.defaults.length} missing settings would be filled with defaults. No settings were applied.`);
        document.getElementById('setup-preview-active').textContent = 'Active model after Save: ' + (data.active_after === 'local' ? 'Local' : 'Remote');
        document.getElementById('setup-preview-notice').textContent = data.notice;
        document.getElementById('setup-preview-timing').textContent = data.timing;
        document.getElementById('setup-preview-unmanaged').textContent = data.unmanaged_fields
            ? `${data.unmanaged_fields} additional saved fields are outside the supported form and would not be retained by the existing Save. Review the saved file before saving.` : '';
        renderSetupPreviewRows(document.getElementById('setup-preview-changes'), data.changes);
        document.getElementById('setup-preview-default-summary').textContent = `${data.defaults.length} missing settings filled with defaults`;
        document.getElementById('setup-preview-default-details').hidden = !data.defaults.length;
        renderSetupPreviewRows(document.getElementById('setup-preview-defaults'), data.defaults);
        document.getElementById('setup-preview-results').hidden = false;
        document.getElementById('setup-preview-title').focus({preventScroll: true});
    } catch (error) {
        if (request !== setupPreviewRequest) { return; }
        document.getElementById('setup-preview-results').hidden = true;
        applySystemStatusText('setup-preview-status', 'Preview unavailable. Check model profiles, prompt presets, number ranges and JSON fields, then preview again. No settings were applied by Preview.');
    } finally { if (setupPreviewPending === pending) { setupPreviewPending = null; } }
}

window.invalidateSetupPreview = invalidateSetupPreview;
document.getElementById('config-form').addEventListener('input', invalidateSetupPreview);
document.getElementById('config-form').addEventListener('change', invalidateSetupPreview);
