let setupSearchStatus = [];

function getSetupUiStorageNotice() {
    return t('setup.locale.storage_notice', 'Browser storage is unavailable; this UI preference applies only to this tab until reload.');
}

function getSetupFinderMessage(form, values = {}) {
    if (form === 'one') { return t('setup.finder.one', '{count} matching setting.', values); }
    if (form === 'many') { return t('setup.finder.many', '{count} matching settings.', values); }
    if (form === 'none') { return t('setup.finder.none', 'No matching settings for your current services. Try another label or description.', values); }
    if (form === 'limited') { return t('setup.finder.limited', 'Showing the first {limit}; refine your search for more specific results.', values); }
    if (form === 'opened') { return t('setup.finder.opened', 'Showing the original setting. Save Configuration to apply any edits.', values); }
    throw new Error('Unknown setting finder message: ' + form);
}

function changeUiLocale(locale) {
    const applied = applyUiLocaleAndSearchStatus(locale);
    applySystemStatusText('setup-ui-locale-status', setLocalStorageValue(UI_LOCALE_STORAGE_KEY, getUiLocale()) ? '' : getSetupUiStorageNotice());
    return applied;
}

// JS-built chrome does not carry data-i18n attributes, so the locale change
// repaints it here: the finder status and the Home task-guide cards. Guide
// text is rebuilt through translateUiMessage on every render.
function renderUiLocaleChrome() {
    renderSetupSearchStatus();
    if (typeof renderTaskGuides === 'function') { renderTaskGuides(); }
}

function applyUiLocaleAndSearchStatus(locale) {
    const applied = applyUiLocale(locale);
    renderUiLocaleChrome();
    return applied.then(renderSetupSearchStatus);
}

function renderSetupSearchStatus() {
    const text = joinUiSentences(setupSearchStatus.map(message => getSetupFinderMessage(message.form, message.values)));
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

// The finder indexes the displayed text and the authored English together: users
// paste English setting names from issues and RECIPES.md, so an English query
// must still find a translated entry (design open question 8). NFKC makes
// full-width Japanese IME output match the half-width form.
function normalizeSetupSearchText(text) {
    return String(text).normalize('NFKC').toLowerCase().replace(/\s+/g, ' ');
}

function getSetupSettingsMatches(query) {
    const terms = normalizeSetupSearchText(query).trim().slice(0, 120).split(' ').filter(Boolean);
    if (!terms.length) { return []; }
    const matches = [];
    for (const control of document.querySelectorAll('#config-form input, #config-form select, #config-form textarea')) {
        if (!control.id || !isSetupSettingAvailable(control)) { continue; }
        const label = Array.from(control.labels || []).map(node => node.textContent.trim()).join(' ').replace(/\s+/g, ' ');
        if (!label) { continue; }
        const sectionNode = control.closest('[data-settings-section]');
        const section = sectionNode?.getAttribute('data-settings-section') || t('setup.search.section_fallback', 'Setup');
        // The result row shows the translated category heading; the raw
        // attribute value stays in the English search index below.
        const sectionLabel = sectionNode?.querySelectorAll('h5')[0]?.textContent.trim() || section;
        const description = getItemHelpText(control);
        const englishDescription = getItemHelpText(control, 'en');
        const text = normalizeSetupSearchText(`${label} ${section} ${description} ${englishDescription}`);
        if (terms.every(term => text.includes(term))) { matches.push({id: control.id, label, section, sectionLabel}); }
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
    setupSearchStatus = [{form: !matches.length ? 'none' : pluralForm(matches.length), values: {count: matches.length}}];
    if (matches.length > 8) { setupSearchStatus.push({form: 'limited', values: {limit: 8}}); }
    renderSetupSearchStatus();
    for (const match of matches.slice(0, 8)) {
        const row = document.createElement('div');
        row.className = 'settings-search-hit';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-outline-primary';
        button.setAttribute('data-help-key', 'openSetupSetting');
        button.textContent = `${match.label} · ${match.sectionLabel}`;
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
    setupSearchStatus = [{form: 'opened', values: {}}];
    renderSetupSearchStatus();
    revealExpertSettingsFor(control);
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

// Advanced tuning stays behind a switch so a first run shows only the connection
// and the speech engine. The choice is browser-local, like the interface
// language, and never reaches the saved configuration.
const SETUP_EXPERT_STORAGE_KEY = 'alexandria.setupExpert';

function readSetupExpertMode() {
    try { return localStorage.getItem(SETUP_EXPERT_STORAGE_KEY) === '1'; }
    catch (error) { return false; }
}

function applySetupExpertMode(expert) {
    for (const node of document.querySelectorAll('[data-setup-advanced]')) { node.hidden = !expert; }
    if (expert) { return; }
    // Hiding a category must never leave its panel on screen.
    const visible = Array.from(document.querySelectorAll('#config-form [data-settings-section]')).find(panel => !panel.hidden);
    if (visible?.hasAttribute('data-setup-advanced-section')) { openSetupSettingsSection('settings-llm', false); }
}

function setSetupExpertMode(expert) {
    try { localStorage.setItem(SETUP_EXPERT_STORAGE_KEY, expert ? '1' : '0'); }
    catch (error) { /* a blocked store keeps the choice for this page only */ }
    applySetupExpertMode(expert);
}

// A search hit or a validation error can point into advanced tuning; reveal it
// rather than focusing a control the reader cannot see. The finder keeps
// indexing these panels while they are hidden, like any collapsed control.
function revealExpertSettingsFor(control) {
    if (!control.closest('[data-settings-section]')?.hasAttribute('data-setup-advanced-section')) { return; }
    const toggle = document.getElementById('setup-expert-toggle');
    if (toggle && !toggle.checked) { toggle.checked = true; setSetupExpertMode(true); }
}

const setupExpertToggle = document.getElementById('setup-expert-toggle');
if (setupExpertToggle) {
    setupExpertToggle.checked = readSetupExpertMode();
    applySetupExpertMode(setupExpertToggle.checked);
}

// Browser constraint validation happens before submit. Reveal only the first
// rejected control so later invalid events cannot hide the browser's focus target.
function onSetupSettingsInvalid(event) {
    const form = document.getElementById('config-form');
    if (form.querySelector('input:invalid, select:invalid, textarea:invalid') === event.target) {
        revealExpertSettingsFor(event.target);
        openConfigField(event.target);
    }
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
    applySystemStatusText('setup-preview-status', t('setup.preview.invalidated', 'Settings or saved state may have changed. Preview again before relying on this comparison.'));
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
        for (const [heading, text] of [
                [createUiMessageNode('strong', 'setup.preview.saved', 'Saved'), row.before + (row.was_default ? ' (default)' : '')],
                [createUiMessageNode('strong', 'setup.preview.would_save', 'Would save'), row.after]]) {
            const column = document.createElement('div');
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
    applySystemStatusText('setup-preview-status', t('setup.preview.comparing', 'Comparing the proposed settings with the saved configuration…'));
    if (setupPreviewPending) { await setupPreviewPending.catch(() => {}); }
    if (request !== setupPreviewRequest) { return; }
    const pending = Promise.resolve().then(() => API.post('/api/config/preview', proposal));
    setupPreviewPending = pending;
    try {
        const data = await pending;
        if (request !== setupPreviewRequest) { return; }
        if (JSON.stringify(getConfigPreviewProposal()) !== captured) { invalidateSetupPreview(); return; }
        if (!isSetupPreviewValid(data)) { throw new Error(t('setup.preview.incomplete', 'Incomplete settings preview')); }
        const baseline = data.baseline === 'read' && !data.warning_count ? ''
            : data.baseline === 'missing' ? t('setup.preview.baseline_missing', 'No saved configuration was found.')
            : t('setup.preview.baseline_damaged', 'Saved configuration is damaged or incomplete; recovered/default values are shown.');
        applySystemStatusText('setup-preview-status', joinUiSentences([baseline, t('setup.preview.summary',
            '{changes} supported settings would change; {defaults} missing settings would be filled with defaults. No settings were applied.',
            {changes: data.changes.length, defaults: data.defaults.length})]));
        document.getElementById('setup-preview-active').textContent = t('setup.preview.active', 'Active model after Save: {model}', {
            model: data.active_after === 'local' ? t('setup.preview.local', 'Local') : t('setup.preview.remote', 'Remote')});
        document.getElementById('setup-preview-notice').textContent = data.notice;
        document.getElementById('setup-preview-timing').textContent = data.timing;
        const unmanaged = document.getElementById('setup-preview-unmanaged');
        if (data.unmanaged_fields) {
            renderUiSafety(unmanaged, 'safety.setup.unmanaged_fields',
                '{count} additional saved fields are outside the supported form and would not be retained by the existing Save. Review the saved file before saving.',
                {count: data.unmanaged_fields});
        } else {
            unmanaged.textContent = '';
        }
        renderSetupPreviewRows(document.getElementById('setup-preview-changes'), data.changes);
        const defaultSummary = document.getElementById('setup-preview-default-summary');
        defaultSummary.setAttribute('data-i18n-count', String(data.defaults.length));
        defaultSummary.textContent = t('setup.preview.defaults', '{count} missing settings filled with defaults', {count: data.defaults.length});
        document.getElementById('setup-preview-default-details').hidden = !data.defaults.length;
        renderSetupPreviewRows(document.getElementById('setup-preview-defaults'), data.defaults);
        document.getElementById('setup-preview-results').hidden = false;
        document.getElementById('setup-preview-title').focus({preventScroll: true});
    } catch (error) {
        if (request !== setupPreviewRequest) { return; }
        document.getElementById('setup-preview-results').hidden = true;
        applySystemStatusText('setup-preview-status', t('setup.preview.unavailable', 'Preview unavailable. Check model profiles, prompt presets, number ranges and JSON fields, then preview again. No settings were applied by Preview.'));
    } finally { if (setupPreviewPending === pending) { setupPreviewPending = null; } }
}

window.invalidateSetupPreview = invalidateSetupPreview;
document.getElementById('config-form').addEventListener('input', invalidateSetupPreview);
document.getElementById('config-form').addEventListener('change', invalidateSetupPreview);

if (document.getElementById('setup-ui-locale')) {
    const preference = readUiLocalePreference();
    if (!preference.available) { applySystemStatusText('setup-ui-locale-status', getSetupUiStorageNotice()); }
    applyUiLocaleAndSearchStatus(resolveUiLocale(preference.stored));
    renderUiLocaleOptions();
    preloadUiCatalogs().then(() => {
        applyUiCatalog();
        renderUiLocaleChrome();
    });
    window.addEventListener('storage', event => {
        if (event.key !== UI_LOCALE_STORAGE_KEY && event.key !== null) { return; }
        let localEvent;
        try { localEvent = event.storageArea === localStorage; }
        catch (_) { applySystemStatusText('setup-ui-locale-status', getSetupUiStorageNotice()); return; }
        if (localEvent) { applyUiLocaleAndSearchStatus(resolveUiLocale(event.newValue)); }
    });
}
