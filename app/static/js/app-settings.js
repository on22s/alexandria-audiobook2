// Setup discovery reads labels and help, never configuration values.
function isSetupSettingAvailable(control) {
    const form = document.getElementById('config-form');
    if (!form?.contains(control) || control.disabled || control.type === 'hidden') { return false; }
    for (let node = control; node && node !== form; node = node.parentElement) {
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

function openSetupSettingsSection(id) {
    const section = document.getElementById(id);
    if (!section?.matches('[data-settings-section]')) { return; }
    const heading = section.querySelector('h5');
    heading.focus();
    heading.scrollIntoView({block: 'start'});
}

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
