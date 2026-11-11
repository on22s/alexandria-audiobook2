// UI locale authority and message catalogs.
//
// English is not a runtime catalog: it is authored in the markup and passed to
// t() at every JS call site, so a partial catalog still renders English and an
// unreviewed locale can never leave a raw key on screen. One flat JSON file per
// translated locale lives in /static/i18n/; app/static/i18n/ui.en.json is
// generated from this source by tools/i18n/extract_ui_catalog.py.
const UI_LOCALE_STORAGE_KEY = 'alex-ui-locale';
const UI_LOCALE_DEFAULT = 'en';
const UI_LOCALE_PSEUDO = 'qps-ploc';
const UI_SUPPORTED_LOCALES = Object.freeze([UI_LOCALE_DEFAULT, 'zh-CN', 'ja', 'es-419', UI_LOCALE_PSEUDO]);
const UI_CATALOG_URLS = Object.freeze({
    'zh-CN': '/static/i18n/ui.zh-CN.json',
    'ja': '/static/i18n/ui.ja.json',
    'es-419': '/static/i18n/ui.es-419.json',
});
const uiCatalogs = new Map();
const uiCatalogLoads = new Map();
const uiCatalogMisses = [];
const uiAuthoredText = new WeakMap();
const uiAuthoredHtml = new WeakMap();
const uiAuthoredAttrs = new WeakMap();
let uiLocale = UI_LOCALE_DEFAULT;

function normalizeUiLocale(locale) {
    if (typeof locale !== 'string' || !UI_SUPPORTED_LOCALES.includes(locale)) {
        return UI_LOCALE_DEFAULT;
    }
    return locale;
}

// The only mapping from browser language tags to supported UI locales. A
// Traditional Chinese tag is deliberately not mapped: this catalog is
// Simplified and offering it silently would misread the user's preference.
function getBrowserUiLocale(languages) {
    for (const language of Array.isArray(languages) ? languages : []) {
        if (typeof language !== 'string') { continue; }
        const tag = language.trim().toLowerCase();
        if (tag === 'zh' || tag === 'zh-cn' || tag === 'zh-hans' || tag.startsWith('zh-hans-')) {
            return 'zh-CN';
        }
        if (tag === 'ja' || tag.startsWith('ja-')) {
            return 'ja';
        }
        if (tag === 'es' || tag.startsWith('es-')) {
            return 'es-419';
        }
    }
    return UI_LOCALE_DEFAULT;
}

function resolveUiLocale(stored, languages = undefined) {
    if (stored === null || stored === undefined || stored === '') {
        return getBrowserUiLocale(languages === undefined && typeof navigator !== 'undefined' ? navigator.languages : languages);
    }
    return normalizeUiLocale(stored);
}

// The only reader of the UI locale preference. Pure read: it never writes, and
// it reports a blocked storage API instead of silently pretending it is empty.
function readUiLocalePreference() {
    try {
        return {stored: localStorage.getItem(UI_LOCALE_STORAGE_KEY), available: true};
    } catch (_) {
        return {stored: null, available: false};
    }
}

function getUiLocale() {
    return uiLocale;
}

function getIntlUiLocale(locale = getUiLocale()) {
    return locale === UI_LOCALE_PSEUDO ? UI_LOCALE_DEFAULT : locale;
}

function pluralForm(count, locale = getUiLocale()) {
    if (locale === 'zh-CN' || locale === 'ja') {
        return 'many';
    }
    return count === 1 ? 'one' : 'many';
}

function formatUiNumber(value) {
    return new Intl.NumberFormat(getIntlUiLocale()).format(value);
}

function formatUiDate(value) {
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.getTime())) {
        return String(value);
    }
    return new Intl.DateTimeFormat(getIntlUiLocale()).format(date);
}

function uiPlaceholderSet(text) {
    return [...new Set(text.match(/\{[a-zA-Z][a-zA-Z0-9_]*\}/g) || [])].sort().join(',');
}

function formatUiMessage(english, translated, values = {}) {
    const template = typeof translated === 'string' && translated.trim() && uiPlaceholderSet(translated) === uiPlaceholderSet(english) ? translated : english;
    return template.replace(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g, (match, name) => {
        const value = Object.hasOwn(values, name) ? values[name] : undefined;
        if (typeof value !== 'string' && !(typeof value === 'number' && Number.isFinite(value))) {
            throw new Error('Missing or invalid UI message value: ' + name);
        }
        return String(value);
    });
}

function expandPseudoUiMessage(english) {
    return '[' + english.split(/(\{[a-zA-Z][a-zA-Z0-9_]*\})/g).map(part =>
        part.startsWith('{') ? part : part.replace(/[aeiouAEIOU]/g, vowel => vowel + vowel)).join('') + ']';
}

function reportUiCatalogMiss(key, locale = getUiLocale()) {
    uiCatalogMisses.push({key, locale});
}

function getUiCatalogMisses() {
    return uiCatalogMisses.slice();
}

function t(key, english, values = {}, locale = getUiLocale()) {
    if (typeof english !== 'string' || !english.trim()) {
        throw new Error('Missing English text for UI message: ' + key);
    }
    return translateUiMessage(key, english, values, locale);
}

// The translation core. Authored call sites use t(), whose literal English the
// source harvest can see; server refusals arrive with runtime text and use this
// entry point instead, because their English is harvested from the AppError
// message rather than from a t() argument.
function translateUiMessage(key, english, values = {}, locale = getUiLocale()) {
    if (locale === UI_LOCALE_PSEUDO) {
        return formatUiMessage(english, expandPseudoUiMessage(english), values);
    }
    const catalog = uiCatalogs.get(locale);
    if (!catalog || !Object.hasOwn(catalog.messages, key)) {
        return formatUiMessage(english, english, values);
    }
    const translated = catalog.messages[key];
    if (typeof translated !== 'string' || uiPlaceholderSet(translated) !== uiPlaceholderSet(english)) {
        reportUiCatalogMiss(key, locale);
        return formatUiMessage(english, english, values);
    }
    return formatUiMessage(english, translated, values);
}

// Safety messages keep the authored English on screen under the translation:
// a mistranslated refusal can never remove the user's ability to read it.
function tSafety(key, english, values = {}, locale = getUiLocale()) {
    const original = formatUiMessage(english, english, values);
    const primary = t(key, english, values, locale);
    return {primary, secondary: primary === original ? null : original};
}

// A string sink (a confirm body, which keeps its own pre-line CSS) cannot hold
// two spans, so this is the same tSafety contract in the one line it can show:
// the translation first, the authored English on the next line when it differs.
function formatUiSafetyText(key, english, values = {}, locale = getUiLocale()) {
    const {primary, secondary} = tSafety(key, english, values, locale);
    return secondary === null ? primary : primary + '\n' + secondary;
}

function renderUiSafety(target, key, english, values = {}) {
    const {primary, secondary} = tSafety(key, english, values);
    const translated = document.createElement('span');
    translated.className = 'ui-safety-primary';
    translated.textContent = primary;
    const replacement = [translated];
    if (secondary !== null) {
        const original = document.createElement('span');
        original.className = 'ui-safety-secondary';
        original.setAttribute('lang', UI_LOCALE_DEFAULT);
        original.textContent = secondary;
        replacement.push(original);
    }
    target.replaceChildren(...replacement);
}

function loadUiCatalog(locale) {
    const normalized = normalizeUiLocale(locale);
    if (uiCatalogs.has(normalized)) {
        return Promise.resolve(uiCatalogs.get(normalized));
    }
    if (uiCatalogLoads.has(normalized)) {
        return uiCatalogLoads.get(normalized);
    }
    const url = UI_CATALOG_URLS[normalized];
    if (!url || typeof fetch !== 'function') {
        return Promise.resolve(null);
    }
    const pending = fetch(url, {cache: 'no-store'})
        .then(response => {
            if (!response.ok) {
                throw new Error('HTTP ' + response.status);
            }
            return response.json();
        })
        .then(payload => {
            if (!payload || typeof payload !== 'object' || !payload._meta || typeof payload._meta !== 'object') {
                throw new Error('UI catalog ' + normalized + ' has no _meta');
            }
            const catalog = {meta: payload._meta, messages: {}};
            for (const [key, value] of Object.entries(payload)) {
                if (key !== '_meta') {
                    catalog.messages[key] = value;
                }
            }
            uiCatalogs.set(normalized, catalog);
            uiCatalogLoads.delete(normalized);
            return catalog;
        })
        .catch(() => {
            uiCatalogLoads.delete(normalized);
            return null;
        });
    uiCatalogLoads.set(normalized, pending);
    return pending;
}

function preloadUiCatalogs() {
    return Promise.all(Object.keys(UI_CATALOG_URLS).map(locale => loadUiCatalog(locale)))
        .then(() => { renderUiLocaleOptions(); });
}

function isUiLocaleDraft(locale) {
    const normalized = normalizeUiLocale(locale);
    if (!Object.hasOwn(UI_CATALOG_URLS, normalized)) {
        return false;
    }
    const catalog = uiCatalogs.get(normalized);
    if (!catalog || !catalog.meta || !catalog.meta.review_status) {
        // Fail closed: a translated locale is a draft until its own catalog
        // says otherwise, including while that catalog is still loading.
        return true;
    }
    return catalog.meta.review_status.all !== 'human-reviewed';
}

// The picker's honesty marker comes from the catalog it offers, never from a
// hardcoded list: a locale loses "(draft)" only when its catalog says reviewed.
function renderUiLocaleOptions() {
    const selector = document.getElementById('setup-ui-locale');
    if (!selector) {
        return;
    }
    for (const option of selector.querySelectorAll('option')) {
        const base = option.textContent.replace(/\s*\(draft\)$/, '');
        option.textContent = isUiLocaleDraft(option.getAttribute('value')) ? base + ' (draft)' : base;
    }
}

function readUiMessageValues(node, english) {
    if (node.hasAttribute('data-i18n-count')) {
        return {count: Number(node.getAttribute('data-i18n-count'))};
    }
    // Markup braces are literal template tokens ("{roster}", "{batch}"), not
    // runtime values: data-i18n-count is the only runtime value a markup node
    // carries, and a missing {count} stays a throw (a real bug). Supplying the
    // other braces as their own text keeps them on screen while formatUiMessage
    // still validates the placeholder sets.
    const values = {};
    for (const match of String(english).matchAll(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g)) {
        if (match[1] !== 'count') {
            values[match[1]] = match[0];
        }
    }
    return values;
}

// One repaint function for every keyed node. The authored English is captured
// on the first sweep and kept here (never in the DOM), so switching back to
// English restores the source text instead of reusing a translation.
function applyUiCatalog(root = document) {
    const locale = getUiLocale();
    for (const node of root.querySelectorAll('[data-i18n]')) {
        if (!uiAuthoredText.has(node)) {
            uiAuthoredText.set(node, node.textContent);
        }
        const key = node.getAttribute('data-i18n');
        if (key.startsWith('safety.')) {
            renderUiSafety(node, key, uiAuthoredText.get(node), readUiMessageValues(node, uiAuthoredText.get(node)));
            continue;
        }
        node.textContent = t(key, uiAuthoredText.get(node), readUiMessageValues(node, uiAuthoredText.get(node)), locale);
    }
    // The allowlisted data-i18n-html nodes keep <strong>/<code>/<em>; English is
    // the captured innerHTML, so switching back restores the original markup.
    for (const node of root.querySelectorAll('[data-i18n-html]')) {
        if (!uiAuthoredHtml.has(node)) {
            uiAuthoredHtml.set(node, node.innerHTML);
        }
        node.innerHTML = t(node.getAttribute('data-i18n-html'), uiAuthoredHtml.get(node), readUiMessageValues(node, uiAuthoredHtml.get(node)), locale);
    }
    for (const node of root.querySelectorAll('[data-i18n-attr]')) {
        if (!uiAuthoredAttrs.has(node)) {
            uiAuthoredAttrs.set(node, new Map());
        }
        const authored = uiAuthoredAttrs.get(node);
        for (const entry of node.getAttribute('data-i18n-attr').split(',')) {
            const [attribute, key] = entry.split(':').map(part => part.trim());
            if (!attribute || !key) { continue; }
            if (!authored.has(attribute)) {
                authored.set(attribute, node.getAttribute(attribute) || '');
            }
            node.setAttribute(attribute, t(key, authored.get(attribute), readUiMessageValues(node, authored.get(attribute)), locale));
        }
    }
}

// A keyed node created by JS (preview headings). The English is registered
// before painting so the next sweep keeps the source text, not the translation.
function createUiMessageNode(tag, key, english) {
    const node = document.createElement(tag);
    node.setAttribute('data-i18n', key);
    node.textContent = english;
    uiAuthoredText.set(node, english);
    node.textContent = t(key, english);
    return node;
}

function applyUiLocale(locale) {
    uiLocale = normalizeUiLocale(locale);
    if (document.documentElement) {
        document.documentElement.setAttribute('lang', uiLocale);
    }
    const selector = document.getElementById('setup-ui-locale');
    if (selector) {
        selector.value = uiLocale;
    }
    applyUiCatalog();
    if (!uiCatalogs.has(uiLocale) && Object.hasOwn(UI_CATALOG_URLS, uiLocale)) {
        return loadUiCatalog(uiLocale).then(catalog => {
            if (catalog) {
                applyUiCatalog();
            }
            return catalog;
        });
    }
    return Promise.resolve(uiCatalogs.get(uiLocale) || null);
}
