// Read-only workflow summaries; canonical settings and actions stay in their tabs.
function getChunkRowState(chunk) {
    if (chunk.status === 'error') { return 'failed'; }
    if (chunk.status === 'generating') { return 'generating'; }
    if (chunk.status === 'done') { return _driftBadge(chunk) ? 'flagged' : 'rendered'; }
    return 'pending';
}

let renderPendingValue = null;

function renderPendingCount(chunks) {
    renderPendingValue = Array.isArray(chunks) ? chunks.filter(isChunkPendingRender).length : null;
    paintRenderPendingCount();
}

function paintRenderPendingCount() {
    const label = document.getElementById('render-pending-label');
    if (!label) { return; }
    if (renderPendingValue === null) {
        label.setAttribute('data-i18n', 'editor.toolbar.render-pending');
        label.removeAttribute('data-i18n-count');
        label.textContent = t('editor.toolbar.render-pending', 'Render Pending');
        return;
    }
    // Same admission predicate as Render Pending, including retryable failures.
    const count = renderPendingValue;
    // Dynamic chrome is repainted by the existing locale-change hook. A
    // markup node's cached English cannot safely be re-keyed to add {count}.
    label.removeAttribute('data-i18n');
    label.removeAttribute('data-i18n-count');
    label.textContent = t('editor.toolbar.render-pending-count', 'Render Pending ({count})', {count});
}

function openBasicWorkflowControl(tab, selector) {
    activateTab(tab);
    revealWorkflowTarget(document.querySelector(selector));
}

function revealWorkflowTarget(target) {
    if (!target) { return; }
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') { parent.open = true; }
    }
    target.scrollIntoView({block: 'center', behavior: 'smooth'});
    if (!target.matches('button, input, select, textarea, a, summary, [tabindex]')) { target.setAttribute('tabindex', '-1'); }
    target.focus({preventScroll: true});
}

function troubleshootLlmConnection() {
    const note = document.getElementById('llm-test-help');
    note.hidden = false;
    note.focus();
}

let scriptIngestRequest = 0;
let scriptIngestFacts = null;
let scriptIngestPending = null;
let scriptIngestFeedback = 'unknown';
let scriptIngestDetail = '';

function resetScriptIngestFacts() {
    scriptIngestRequest++;
    scriptIngestFacts = null;
    scriptIngestPending = null;
    scriptIngestFeedback = 'unknown';
    scriptIngestDetail = '';
    for (const id of ['source', 'cast', 'script']) {
        const node = document.getElementById(`script-ingest-${id}-state`);
        if (node) {
            node.setAttribute('data-i18n', 'script.ingest.unknown');
            node.textContent = t('script.ingest.unknown', 'Not checked');
        }
    }
    const metadata = document.getElementById('script-ingest-metadata');
    if (metadata) { metadata.textContent = ''; }
    const status = document.getElementById('script-ingest-feedback');
    if (status) { status.textContent = ''; }
}

function renderScriptIngestFacts(facts) {
    scriptIngestFacts = facts;
    for (const id of ['source', 'cast', 'script']) {
        document.getElementById(`script-ingest-${id}-state`).removeAttribute('data-i18n');
    }
    document.getElementById('script-ingest-source-state').textContent = t('script.ingest.accepted', 'Selected source verified');
    document.getElementById('script-ingest-cast-state').textContent = facts.cast.status === 'available'
        ? t('script.ingest.cast-saved', 'Saved roster: {count} characters; review before Voices', {count: facts.cast.count})
        : facts.cast.status === 'unavailable' ? t('script.ingest.cast-unavailable', 'Saved roster could not be verified')
            : t('script.ingest.cast-missing', 'Not built');
    document.getElementById('script-ingest-script-state').textContent = facts.script.status === 'verified'
        ? t('script.ingest.script-verified', 'Script matches the prepared source')
        : facts.script.status === 'not_built' ? t('script.ingest.script-missing', 'Script not built')
        : facts.script.status === 'differences'
            ? t('script.ingest.script-differences', 'Source differences: review required')
            : t('script.ingest.script-unknown', 'Not verified against this source');
    document.getElementById('script-ingest-metadata').textContent = t('script.ingest.metadata', '{name} · {chars} characters · {words} whitespace-delimited words · {chapters} chapter headings · about {minutes} min at 150 words/min (estimate)', {
        name: facts.source.source_name, chars: facts.source.total_chars,
        words: facts.source.word_count, chapters: facts.source.chapter_count,
        minutes: facts.source.estimated_minutes});
}

function renderFollowupLocaleChrome() {
    paintRenderPendingCount();
    if (scriptIngestFacts) { renderScriptIngestFacts(scriptIngestFacts); }
    renderScriptIngestFeedback();
}

function renderScriptIngestFeedback() {
    const node = document.getElementById('script-ingest-feedback');
    if (!node) { return; }
    const text = scriptIngestFeedback === 'checking' ? t('script.ingest.checking', 'Checking saved source, roster and script…')
        : scriptIngestFeedback === 'snapshot' ? t('script.ingest.snapshot', 'Saved-state snapshot. Refresh after changes; these links do not start work or certify voice quality.')
        : scriptIngestFeedback === 'unavailable' ? t('script.ingest.unavailable', 'Saved progress could not be verified. Refresh after resolving the reported problem; no completion is inferred.') : '';
    node.textContent = [text, scriptIngestDetail].filter(Boolean).join(' ');
}

function refreshScriptIngestFacts() {
    const book = currentBookFilename;
    const selection = window._selectedUploadIdentity;
    if (scriptIngestPending?.book === book && scriptIngestPending.selection === selection) { return scriptIngestPending.promise; }
    resetScriptIngestFacts();
    const request = scriptIngestRequest;
    if (!document.getElementById('script-ingest-feedback')) { return Promise.resolve(); }
    scriptIngestFeedback = 'checking';
    renderScriptIngestFeedback();
    const pending = {book, selection, promise: null};
    scriptIngestPending = pending;
    pending.promise = (async () => { try {
        const query = selection?.token ? '?expected_book_token=' + encodeURIComponent(selection.token) : '';
        const facts = await API.get('/api/script/ingest-status' + query);
        if (request !== scriptIngestRequest || book !== currentBookFilename) { return; }
        if (selection?.token && facts.book_token !== selection.token) { throw Error('Obsolete ingest snapshot'); }
        renderScriptIngestFacts(facts);
        scriptIngestFeedback = 'snapshot';
        scriptIngestDetail = facts.script.reason || '';
        renderScriptIngestFeedback();
    } catch (error) {
        if (request !== scriptIngestRequest || book !== currentBookFilename) { return; }
        resetScriptIngestFacts();
        scriptIngestFeedback = 'unavailable';
        scriptIngestDetail = describeApiError(error);
        renderScriptIngestFeedback();
    } })().finally(() => { if (scriptIngestPending === pending) { scriptIngestPending = null; } });
    return pending.promise;
}
