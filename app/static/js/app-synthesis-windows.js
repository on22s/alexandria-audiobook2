/* Explicit future-render controls; server capabilities own the budget categories. */
let synthesisWindowControlsLoaded = false;
let synthesisRecoveryControlsLoaded = false;
const synthesisSeamFields = {
    sentence_gap_ms: ['synthesis-sentence-gap', null, 0, 5000],
    clause_gap_ms: ['synthesis-clause-gap', null, 0, 5000],
    paragraph_gap_ms: ['synthesis-paragraph-gap', null, 0, 5000],
    max_edge_trim_ms: ['synthesis-edge-trim', 0, 0, 2000],
    fade_ms: ['synthesis-fade', 0, 0, 50],
    continuation_crossfade_ms: ['synthesis-crossfade', 0, 0, 50],
    quiet_threshold_db: ['synthesis-quiet-threshold', null, -120, 0],
};

function setSynthesisWindowControls(settings, modes) {
    const body = document.getElementById('synthesis-window-budgets');
    if (!body) { return; }
    synthesisWindowControlsLoaded = false;
    body.replaceChildren();
    const enabled = document.getElementById('synthesis-windows-enabled');
    const options = document.getElementById('synthesis-window-options');
    const status = document.getElementById('synthesis-window-status');
    const categories = Object.entries(modes || {}).flatMap(([mode, capability]) =>
        (capability.voice_categories || []).map(category => [mode, category]));
    enabled.disabled = options.disabled = categories.length === 0;
    if (!categories.length) {
        status.textContent = 'Supported voices are unavailable. Saved settings are kept; reload configuration to retry.';
        return;
    }
    settings = settings || {};
    enabled.checked = settings.enabled === true;
    const names = {custom: 'Stock voice', clone: 'Clone', design: 'Designed voice', lora: 'LoRA', ensemble: 'Ensemble'};
    for (const [mode, category] of categories) {
        const key = `${mode}.${category}`;
        const row = document.createElement('tr');
        row.dataset.budgetKey = key;
        const label = `${mode === 'local' ? 'Local' : mode === 'external' ? 'External' : mode} / ${names[category] || category}`;
        const heading = document.createElement('th');
        heading.scope = 'row'; heading.textContent = label; row.appendChild(heading);
        for (const [name, maximum] of [['max_chars', 1000000], ['max_words', 100000]]) {
            const cell = document.createElement('td');
            const input = document.createElement('input');
            input.type = 'number'; input.min = '1'; input.max = String(maximum); input.step = '1';
            input.id = `synthesis-${key.replaceAll('.', '-')}-${name}`;
            input.className = 'form-control form-control-sm'; input.dataset.limit = name;
            input.value = settings.budgets?.[key]?.[name] ?? '';
            input.placeholder = name === 'max_chars' ? 'Keep existing rendering' : 'No word limit';
            input.title = name === 'max_chars'
                ? 'Maximum spoken-text characters per call. A whole word must fit. Blank keeps existing rendering for this service and voice.'
                : 'Optional maximum whitespace-separated words per call. Set a character limit for this category as well.';
            const accessible = document.createElement('label');
            accessible.htmlFor = input.id; accessible.className = 'visually-hidden';
            accessible.textContent = `${label}: ${name === 'max_chars' ? 'maximum characters' : 'maximum words'}`;
            cell.appendChild(accessible); cell.appendChild(input); row.appendChild(cell);
        }
        body.appendChild(row);
    }
    for (const [key, [id, fallback]] of Object.entries(synthesisSeamFields)) {
        document.getElementById(id).value = settings.seams?.[key] ?? fallback ?? '';
    }
    status.textContent = 'Off by default. Save Configuration to apply these choices to subsequent renders. Existing takes stay selected.';
    synthesisWindowControlsLoaded = true;
}

function getSynthesisWindowNumber(field, fallback, minimum, maximum, integer = true) {
    if (field.value.trim() === '') { return fallback; }
    const number = Number(field.value);
    if (!Number.isFinite(number) || (integer && !Number.isInteger(number)) || number < minimum || number > maximum) {
        throw getConfigValidationError(field.id, `${field.labels?.[0]?.textContent || 'Internal render limit'} must be ${integer ? 'an integer' : 'a number'} from ${minimum} to ${maximum}.`);
    }
    return number;
}

function getSynthesisWindowConfig() {
    if (!synthesisWindowControlsLoaded) { return {}; }
    const budgets = {};
    for (const row of document.getElementById('synthesis-window-budgets').children) {
        const chars = getSynthesisWindowNumber(row.querySelector('[data-limit="max_chars"]'), null, 1, 1000000);
        const words = getSynthesisWindowNumber(row.querySelector('[data-limit="max_words"]'), null, 1, 100000);
        if (chars === null && words !== null) { throw getConfigValidationError(row.querySelector('[data-limit="max_chars"]').id, 'A word limit also needs a character limit for that voice category.'); }
        if (chars !== null) { budgets[row.dataset.budgetKey] = {max_chars: chars, max_words: words}; }
    }
    const enabled = document.getElementById('synthesis-windows-enabled').checked;
    if (enabled && !Object.keys(budgets).length) { throw getConfigValidationError('synthesis-windows-enabled', 'Set at least one character limit before enabling internal render windows.'); }
    const seams = {};
    for (const [key, [id, fallback, minimum, maximum]] of Object.entries(synthesisSeamFields)) {
        seams[key] = getSynthesisWindowNumber(document.getElementById(id), fallback, minimum, maximum, key !== 'quiet_threshold_db');
    }
    return {synthesis_windows: {enabled, budgets, seams}};
}

function setSynthesisRecoveryControls(settings) {
    const enabled = document.getElementById('synthesis-recovery-enabled');
    const budget = document.getElementById('synthesis-recovery-budget');
    if (!enabled || !budget) { return; }
    enabled.checked = settings?.enabled === true;
    budget.value = settings?.max_audio_mb ?? 256;
    enabled.disabled = budget.disabled = false;
    synthesisRecoveryControlsLoaded = true;
}

function getSynthesisRecoveryConfig() {
    if (!synthesisRecoveryControlsLoaded) { return {}; }
    return {synthesis_recovery: {
        enabled: document.getElementById('synthesis-recovery-enabled').checked,
        max_audio_mb: getSynthesisWindowNumber(document.getElementById('synthesis-recovery-budget'), 256, 16, 4096),
    }};
}
