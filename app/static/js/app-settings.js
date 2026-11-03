const setupUiEnglish = Object.freeze({
    label: 'Find a setting',
    placeholder: 'Try context, memory, language or retries',
    clear: 'Clear',
    hint: 'Search labels and help for your current services.',
    none: 'No matching settings for your current services. Try another label or description.',
    one: '{count} matching setting.',
    many: '{count} matching settings.',
    limited: 'Showing the first {limit}; refine your search for more specific results.',
    opened: 'Showing the original setting. Save Configuration to apply any edits.',
    previewSaved: 'Saved',
    previewWouldSave: 'Would save',
    previewAction: 'Preview changes',
    previewTitle: 'Settings change preview',
    previewClose: 'Close preview',
    previewDefaults: '{count} missing settings filled with defaults',
});
let setupUiLocale = 'en';
let setupSearchStatus = [];
const setupUiStorageNotice = 'Browser storage is unavailable; this UI preference applies only to this tab until reload.';

function formatUiMessage(english, translated, values = {}) {
    const placeholders = text => [...new Set(text.match(/\{[a-zA-Z][a-zA-Z0-9_]*\}/g) || [])].sort().join(',');
    const template = typeof translated === 'string' && translated.trim() && placeholders(translated) === placeholders(english) ? translated : english;
    return template.replace(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g, (match, name) => {
        const value = Object.hasOwn(values, name) ? values[name] : undefined;
        if (typeof value !== 'string' && !(typeof value === 'number' && Number.isFinite(value))) {
            throw new Error('Missing or invalid UI message value: ' + name);
        }
        return String(value);
    });
}

function getSetupUiMessage(key, values = {}, locale = setupUiLocale) {
    if (!Object.hasOwn(setupUiEnglish, key)) { return key; }
    const english = setupUiEnglish[key];
    const pseudo = locale === 'qps-ploc' ? '[' + english.split(/(\{[a-zA-Z][a-zA-Z0-9_]*\})/g).map(part =>
        part.startsWith('{') ? part : part.replace(/[aeiouAEIOU]/g, vowel => vowel + vowel)).join('') + ']' : english;
    return formatUiMessage(english, pseudo, values);
}

function applySetupUiLocale(locale) {
    setupUiLocale = locale === 'qps-ploc' ? locale : 'en';
    const selector = document.getElementById('setup-ui-locale');
    if (selector) { selector.value = setupUiLocale; }
    for (const node of document.querySelectorAll('[data-setup-message]')) {
        if (!Object.hasOwn(setupUiEnglish, node.getAttribute('data-setup-message'))) { continue; }
        const values = node.hasAttribute('data-setup-count') ? {count: Number(node.getAttribute('data-setup-count'))} : {};
        const message = getSetupUiMessage(node.getAttribute('data-setup-message'), values);
        if (node.getAttribute('data-setup-message') === 'placeholder') { node.placeholder = message; }
        else { node.textContent = message; }
    }
    renderSetupSearchStatus();
}

function changeSetupUiLocale(locale) {
    applySetupUiLocale(locale);
    applySystemStatusText('setup-ui-locale-status', setLocalStorageValue('alex-ui-locale', setupUiLocale) ? '' : setupUiStorageNotice);
}

function renderSetupSearchStatus() {
    const text = setupSearchStatus.map(message => getSetupUiMessage(message.key, message.values)).join(' ');
    applySystemStatusText('setup-settings-search-status', text);
}

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
    const query = search.value.trim();
    results.replaceChildren();
    results.hidden = !query;
    document.getElementById('setup-settings-clear').hidden = !query;
    setupSearchStatus = [];
    if (!query) { renderSetupSearchStatus(); return; }
    const matches = getSetupSettingsMatches(query);
    setupSearchStatus = [{key: !matches.length ? 'none' : matches.length === 1 ? 'one' : 'many', values: {count: matches.length}}];
    if (matches.length > 8) { setupSearchStatus.push({key: 'limited', values: {limit: 8}}); }
    renderSetupSearchStatus();
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
    setupSearchStatus = [{key: 'opened'}];
    renderSetupSearchStatus();
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
        for (const [key, text] of [['previewSaved', row.before + (row.was_default ? ' (default)' : '')], ['previewWouldSave', row.after]]) {
            const column = document.createElement('div');
            const heading = document.createElement('strong');
            heading.setAttribute('data-setup-message', key);
            heading.textContent = getSetupUiMessage(key);
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
        const defaultSummary = document.getElementById('setup-preview-default-summary');
        defaultSummary.setAttribute('data-setup-count', String(data.defaults.length));
        defaultSummary.textContent = getSetupUiMessage('previewDefaults', {count: data.defaults.length});
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

if (document.getElementById('setup-ui-locale')) {
    let locale = 'en';
    try { locale = localStorage.getItem('alex-ui-locale'); }
    catch (_) { applySystemStatusText('setup-ui-locale-status', setupUiStorageNotice); }
    applySetupUiLocale(locale);
    window.addEventListener('storage', event => {
        if (event.key !== 'alex-ui-locale' && event.key !== null) { return; }
        let localEvent;
        try { localEvent = event.storageArea === localStorage; }
        catch (_) { applySystemStatusText('setup-ui-locale-status', setupUiStorageNotice); return; }
        if (localEvent) { applySetupUiLocale(event.newValue); }
    });
}
