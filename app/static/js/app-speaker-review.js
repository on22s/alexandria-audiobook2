// Opt-in comparison of speaker labels. Opening the panel never starts a model call.
(() => {
    'use strict';
    const element = name => document.getElementById(`speaker-review-${name}`);
    const state = {epoch: 0, options: null, preview: null, run: null, pending: null,
        timer: null, polling: false, reliable: true, fingerprint: '', uncertain: false, previousRun: null};
    const active = () => ['running', 'cancelling'].includes(state.run?.status);
    const context = () => ({epoch: state.epoch, book: currentBookFilename});
    const current = owner => owner.epoch === state.epoch && owner.book === currentBookFilename;
    const selection = () => ({source: element('source').value,
        reference_name: element('reference').value, max_pairs: Number(element('max-pairs').value),
        allow_partial: element('partial').checked});
    const validSelection = () => state.options?.sources?.some(source =>
        source.id === element('source').value && source.available)
        && !!element('reference').value && Number.isInteger(Number(element('max-pairs').value))
        && Number(element('max-pairs').value) >= 1 && Number(element('max-pairs').value) <= 20;
    const stopPolling = () => { clearTimeout(state.timer); state.timer = null; };
    const status = (message, level = 'secondary') => {
        element('status').className = `alert alert-${level} small mb-0`;
        element('status').textContent = message;
    };
    function node(tag, text, className) {
        const result = document.createElement(tag);
        if (text !== undefined) { result.textContent = String(text); }
        if (className) { result.className = className; }
        return result;
    }
    function syncControls() {
        const busy = !!state.pending || active();
        element('load').disabled = busy;
        for (const name of ['source', 'reference', 'max-pairs', 'partial']) { element(name).disabled = busy; }
        element('preview').disabled = busy || !validSelection() || state.uncertain;
        element('consent').disabled = busy || !state.preview || !state.reliable || state.uncertain;
        element('start').disabled = busy || !state.preview || !element('consent').checked
            || !state.preview.candidates?.length || !state.options?.profile?.available
            || !state.reliable || state.uncertain || !!state.previousRun;
        element('cancel').hidden = !active();
        element('cancel').disabled = !!state.pending || state.run?.status === 'cancelling';
        element('cancel-previous').hidden = !state.previousRun;
        element('cancel-previous').disabled = !!state.pending;
        element('refresh').hidden = !state.run;
        element('refresh').disabled = !!state.pending || state.polling;
        element('results').querySelectorAll('[data-apply]').forEach(button => {
            button.disabled = busy || state.polling || !state.reliable || !!state.run?.stale || state.uncertain;
        });
        element('results').querySelectorAll('[data-direction]').forEach(select => { select.disabled = busy; });
    }
    function sourceHelp() {
        const source = state.options?.sources?.find(item => item.id === element('source').value);
        element('source-help').textContent = !source ? '' : !source.available ? source.reason || 'Unavailable.'
            : `${source.entries ?? '?'} of ${source.total ?? '?'} entries. ${source.complete ? 'Complete source.' : 'Incomplete source; explicit partial consent is required.'}`;
    }
    function invalidatePreview() {
        if (state.pending || active()) { return; }
        state.epoch++;
        stopPolling();
        state.preview = null; state.run = null; state.fingerprint = ''; state.reliable = true; state.polling = false;
        element('consent').checked = false;
        element('consent-panel').hidden = true;
        element('results').replaceChildren();
        sourceHelp();
        status('Selection changed. Preview the evidence before starting a model review.');
        syncControls();
    }
    window.resetSpeakerLabelReview = () => {
        const wasRunning = active() || state.pending === 'start';
        state.epoch++;
        stopPolling();
        Object.assign(state, {options: null, preview: null, run: null, pending: null,
            polling: false, reliable: true, fingerprint: '', uncertain: false, previousRun: null});
        element('options').hidden = true;
        element('consent-panel').hidden = true;
        element('consent').checked = false;
        element('results').replaceChildren();
        element('book').textContent = '';
        status(wasRunning
            ? 'The active book changed. Previous results are hidden; an already-sent model request may still finish. Load options to check server state before starting again.'
            : 'Load options for this book when you are ready. Opening this panel makes no model requests.');
        syncControls();
    };
    async function loadOptions() {
        if (state.pending || active() || window._existingUploadSelectionPending) { return; }
        window.resetSpeakerLabelReview();
        const owner = context();
        state.pending = 'options'; syncControls();
        status('Reading saved sources, reference scripts and model settings…');
        try {
            const result = await API.get('/api/speaker_review/options');
            if (!current(owner)) { return; }
            state.options = result;
            if (result.active_run && result.active_run.book_id !== result.book_id) {
                state.previousRun = result.active_run;
            }
            element('source').replaceChildren();
            for (const source of result.sources || []) {
                const option = node('option', `${source.label}${source.available ? '' : ' (unavailable)'}`);
                option.value = source.id; option.disabled = !source.available;
                element('source').append(option);
            }
            element('source').value = result.sources?.find(source => source.available)?.id || '';
            element('reference').replaceChildren(node('option', 'Choose a provisional reference…'));
            element('reference').firstChild.value = '';
            for (const reference of result.references || []) {
                const option = node('option', reference.name); option.value = reference.name;
                element('reference').append(option);
            }
            element('max-pairs').value = '20'; element('partial').checked = false;
            element('options').hidden = false;
            element('book').textContent = `Active book: ${owner.book || '(none selected)'}`;
            sourceHelp();
            const missing = (result.sources || []).filter(source => !source.available)
                .map(source => `${source.label}: ${source.reason || 'unavailable'}`).join(' ');
            status(!result.profile?.available ? `Model unavailable: ${result.profile?.reason || 'Check and save your LLM settings.'} You can still preview evidence. Configure and save a supported profile, then load options again before starting a model review.`
                : !result.references?.length ? 'No saved reference scripts found. Save a provisional reference in Saved Scripts first.'
                : `Choose the labels and provisional reference, then preview evidence. ${missing}`,
            !result.profile?.available || !result.references?.length ? 'warning' : 'secondary');
            const recover = result.active_run?.book_id === result.book_id ? result.active_run : result.recent_run;
            if (recover?.book_id === result.book_id) {
                state.run = recover;
                status('Recovering the saved review status. This does not start or resume model requests…', 'info');
            } else if (state.previousRun) {
                status('A previous book has a review in progress. It will stop when changed evidence is detected; an already-sent request may finish. You can request cancellation here, then reload options.', 'warning');
            }
        } catch (error) {
            if (current(owner)) { status(getActionErrorMessage('Review options could not be loaded', error, 'Retry Load review options.'), 'danger'); }
        } finally {
            if (current(owner)) {
                state.pending = null; syncControls();
                if (state.run) { await refreshStatus(); }
            }
        }
    }
    function showProfile(report) {
        element('profile').textContent = `Saved model: ${report.profile?.model || '(not configured)'}. Endpoint: ${report.profile?.endpoint || '(not configured)'}.${report.profile?.context_length ? ` Context: ${report.profile.context_length} tokens (${report.profile.context_source || 'configured'}).` : ''}`;
        const calls = Number(report.call_count) || 0;
        element('budget').textContent = `This preview has ${report.candidates?.length || 0} label pairs and up to ${calls} model requests: one “none” and one “low” reasoning judgment per pair. Hard limit: 20 pairs / 40 attempts. No automatic retries or aliases. Each request includes the evidence shown below.`;
        element('consent').checked = false;
        element('consent-panel').hidden = false;
    }
    async function preview() {
        if (state.pending || active() || state.uncertain || !validSelection() || window._existingUploadSelectionPending) { return; }
        invalidatePreview();
        const owner = context(), inputs = selection();
        state.pending = 'preview'; syncControls();
        status('Checking source alignment and matching reference text. No model requests are being made…');
        try {
            const result = await API.post('/api/speaker_review/preview', inputs);
            if (!current(owner)) { return; }
            if (result.book_id !== state.options.book_id) { throw new Error('The active book changed. Load review options again.'); }
            if (typeof result.profile?.available === 'boolean') { state.options.profile = result.profile; }
            state.preview = {...result, inputs};
            showProfile(result); renderResults(result);
            status(`${result.candidates?.length || 0} pairs are ready to inspect. ${result.warning || ''} ${result.source_alignment || ''} ${result.phase === 'provisional' ? 'Partial checkpoint: results are provisional.' : 'The reference and all suggestions remain provisional.'} ${!state.options.profile?.available ? 'Save a supported model profile and load options again before starting.' : result.candidates?.length ? 'Review the excerpts, then give consent to start.' : 'No eligible differing label pairs were found.'}`, 'info');
        } catch (error) {
            if (current(owner)) { status(getActionErrorMessage('Evidence preview failed', error, 'Review the source/reference selection. If saved data changed, load options again.'), 'danger'); }
        } finally {
            if (current(owner)) { state.pending = null; syncControls(); }
        }
    }
    async function start() {
        if (element('start').disabled || state.pending || active() || !state.preview || window._existingUploadSelectionPending) { return; }
        const owner = context(), previewed = state.preview;
        if (JSON.stringify(previewed.inputs) !== JSON.stringify(selection())) { invalidatePreview(); return; }
        state.pending = 'start'; syncControls();
        status('Starting the explicitly approved model review…', 'info');
        try {
            const result = await API.post('/api/speaker_review/start', {...previewed.inputs,
                snapshot: previewed.snapshot, allow_network: true});
            if (!current(owner)) { return; }
            if (!result.run_id) { throw new Error('The server did not return a review run ID.'); }
            state.run = {...previewed, ...result, reviews: [], attempts: 0, stale: false};
            state.preview = null;
            element('consent-panel').hidden = true;
            state.pending = null;
            await refreshStatus();
        } catch (error) {
            if (!current(owner)) { return; }
            state.uncertain = true; state.reliable = false;
            element('consent').checked = false;
            status(getActionErrorMessage('Review start was not confirmed', error,
                'Do not immediately start another run. Load review options to check server state first.'), 'danger');
        } finally {
            if (current(owner)) { state.pending = null; syncControls(); }
        }
    }
    function reportStatus(report) {
        const judged = (report.reviews || []).filter(review => review.verdict).length;
        const errors = (report.reviews || []).filter(review => review.error).length;
        const progress = `${Number(report.attempts) || 0}/40 attempts used; ${judged} judgments received${errors ? `; ${errors} review errors` : ''}.`;
        const phase = report.phase === 'provisional' ? ' Partial checkpoint; provisional evidence.' : ' Reference and suggestions are provisional.';
        if (report.stale || report.status === 'stale') { status(`This report is stale. ${progress} Load options and preview again before any further Apply. ${report.error || ''}`, 'warning'); }
        else if (report.status === 'running' || report.status === 'cancelling') {
            status(`${report.status === 'cancelling' ? 'Cancellation requested; the current request may still finish.' : 'Review in progress.'} ${progress}${phase}`, 'info');
        } else if (report.status === 'completed') {
            status(`Review finished. ${progress}${phase}${errors ? ' Some judgments are missing; inspect each result.' : ''}${Number(report.attempts) >= 40 ? ' The attempt budget is exhausted.' : ''} No aliases have been applied automatically.`, errors ? 'warning' : 'success');
        } else {
            status(`Review ${report.status || 'state unknown'}. ${progress}${phase} ${report.error || ''} No aliases have been applied automatically.`, 'warning');
        }
    }
    async function refreshStatus() {
        if (!state.run || state.polling || state.pending) { return; }
        const owner = context(), runId = state.run.run_id;
        state.polling = true; syncControls(); stopPolling();
        try {
            const result = await API.get(`/api/speaker_review/${encodeURIComponent(runId)}`);
            if (!current(owner) || state.run?.run_id !== runId) { return; }
            if (result.run_id !== runId || result.book_id !== state.options?.book_id) {
                state.run.stale = true;
                throw new Error('The review belongs to a different book or run. Load review options again.');
            }
            if (result.cancel_requested && result.status === 'running') { result.status = 'cancelling'; }
            state.run = result; state.reliable = true;
            renderResults(result); reportStatus(result);
        } catch (error) {
            if (current(owner) && state.run?.run_id === runId) {
                state.reliable = false;
                status(getActionErrorMessage('Review status is unavailable', error,
                    active() ? 'The run may still be active. Apply is disabled. Status checks will retry; use Cancel review to request a stop.'
                        : 'Apply is disabled. Use Refresh review status to retry, or load options again.'), 'warning');
            }
        } finally {
            if (current(owner) && state.run?.run_id === runId) {
                state.polling = false; syncControls();
                if (active()) { state.timer = setTimeout(refreshStatus, 2000); }
            }
        }
    }
    async function cancel() {
        if (state.pending || !active()) { return; }
        const owner = context(), runId = state.run.run_id;
        state.pending = 'cancel'; syncControls();
        try {
            await API.post(`/api/speaker_review/${encodeURIComponent(runId)}/cancel`, {});
            if (!current(owner) || state.run?.run_id !== runId) { return; }
            state.run.status = 'cancelling'; reportStatus(state.run);
        } catch (error) {
            if (current(owner)) { status(getActionErrorMessage('Cancellation was not confirmed', error, 'Refresh review status before trying Cancel again. The current model request may still finish.'), 'warning'); }
        } finally {
            if (current(owner)) { state.pending = null; syncControls(); await refreshStatus(); }
        }
    }
    async function cancelPrevious() {
        if (state.pending || !state.previousRun) { return; }
        const owner = context(), runId = state.previousRun.run_id;
        state.pending = 'cancel-previous'; syncControls();
        try {
            await API.post(`/api/speaker_review/${encodeURIComponent(runId)}/cancel`, {});
            if (!current(owner)) { return; }
            status('Cancellation requested for the previous book. Its current model request may still finish. Load review options to check server state.', 'info');
        } catch (error) {
            if (current(owner)) { status(getActionErrorMessage('Previous review cancellation was not confirmed', error, 'Load review options to check server state before trying again.'), 'warning'); }
        } finally {
            if (current(owner)) { state.pending = null; syncControls(); }
        }
    }
    function appendContext(container, title, rows) {
        const details = node('details', undefined, 'mt-2');
        details.append(node('summary', title));
        const list = node('ol', undefined, 'small mt-2 mb-0');
        for (const row of rows || []) {
            const item = node('li', undefined, 'mb-2');
            item.append(node('strong', `${row.speaker || '(unattributed)'} · ${row.type || ''}: `));
            item.append(node('span', row.text || ''));
            item.style.whiteSpace = 'pre-wrap'; list.append(item);
        }
        details.append(list); container.append(details);
    }
    function getEligibleDirections(candidate, report) {
        if (!candidate || candidate.can_apply !== true || report.stale || report.status !== 'completed'
            || !['none', 'low'].every(mode => (report.reviews || []).some(review =>
                review.candidate_id === candidate.id && review.mode === mode
                && !review.error && review.verdict?.same_identity === true))) { return []; }
        return (Array.isArray(candidate.apply_directions) ? candidate.apply_directions : []).filter(choice =>
            choice?.can_apply === true && ['forward', 'reverse'].includes(choice.direction)
            && choice.alias === (choice.direction === 'reverse' ? candidate.reference_label : candidate.prediction_label)
            && choice.canonical === (choice.direction === 'reverse' ? candidate.prediction_label : candidate.reference_label));
    }
    function renderResults(report) {
        const fingerprint = JSON.stringify(report);
        if (fingerprint === state.fingerprint) { return; }
        state.fingerprint = fingerprint;
        const container = element('results');
        const expanded = [...container.querySelectorAll('details')].map(detail => detail.open);
        const directions = new Map([...container.querySelectorAll('[data-direction]')]
            .map(select => [select.dataset.candidateId, select.value]));
        const focused = document.activeElement?.id;
        container.replaceChildren();
        if (report.run_id) {
            container.append(node('p', `Reviewed model: ${report.profile?.model || '(unavailable)'}. Endpoint: ${report.profile?.endpoint || '(unavailable)'}. Reference: ${report.reference_name || '(unavailable)'}.`, 'small text-muted'));
        }
        if (report.skipped?.length) {
            const skipped = node('details', undefined, 'small mb-2');
            skipped.append(node('summary', `${report.skipped_count ?? report.skipped.length} entries skipped (unmatched, ambiguous or outside context limits)`));
            const list = node('ul');
            for (const entry of report.skipped) { list.append(node('li', `Entry ${Number(entry.entry_index) + 1}: ${entry.reason || 'unmatched'}`)); }
            if (report.skipped_count > report.skipped.length) { list.append(node('li', `Showing the first ${report.skipped.length} skipped entries.`)); }
            skipped.append(list); container.append(skipped);
        }
        for (const [index, candidate] of (report.candidates || []).entries()) {
            const choices = getEligibleDirections(candidate, report);
            const card = node('section', undefined, 'border rounded p-3 mb-2');
            const title = node('h6', `Entry ${Number(candidate.entry_index) + 1}: ${candidate.prediction_label} → ${candidate.reference_label}`);
            title.id = `speaker-review-pair-${index}`; card.setAttribute('aria-labelledby', title.id);
            card.append(title, node('p', 'Current/source label → provisional reference label. Check both contexts before deciding.', 'small text-muted mb-1'));
            appendContext(card, 'Source evidence sent to the model', candidate.context);
            appendContext(card, 'Provisional reference evidence sent to the model', candidate.reference_context);
            for (const mode of ['none', 'low']) {
                const review = (report.reviews || []).find(row => row.candidate_id === candidate.id && row.mode === mode);
                const label = review?.error ? `Error: ${review.error}` : !review?.verdict ? (report.run_id ? 'No judgment received.' : 'Not requested yet.')
                    : `${review.verdict.same_identity === true ? 'Same identity suggested' : review.verdict.same_identity === false ? 'Different identities suggested' : 'Uncertain'}: ${review.verdict.reason}`;
                card.append(node('p', `${mode} reasoning${review?.cached ? ' (cached)' : ''}: ${label}`, 'small mt-2 mb-1'));
            }
            if ((report.applied || []).includes(candidate.id)) {
                card.append(node('p', 'Alias saved in the shared registry. Future Review runs can use it; the script has not been rewritten here.', 'small text-success mt-2 mb-0'));
            } else if (report.run_id && choices.length) {
                const controls = node('div', undefined, 'd-flex flex-wrap gap-2 align-items-center mt-2');
                const direction = node('select', undefined, 'form-select form-select-sm');
                direction.style.width = 'auto'; direction.style.maxWidth = '100%'; direction.dataset.direction = 'true';
                direction.dataset.candidateId = candidate.id;
                direction.id = `speaker-review-direction-${index}`;
                const label = node('label', 'Alias direction', 'small'); label.htmlFor = direction.id;
                for (const choice of choices) {
                    const option = node('option', `${choice.alias} → ${choice.canonical}`);
                    option.value = choice.direction; direction.append(option);
                }
                if (choices.some(choice => choice.direction === directions.get(candidate.id))) {
                    direction.value = directions.get(candidate.id);
                }
                const button = node('button', 'Apply alias…', 'btn btn-sm btn-outline-primary');
                button.type = 'button'; button.dataset.apply = 'true'; button.id = `speaker-review-apply-${index}`;
                button.setAttribute('aria-label', `Review and apply alias for entry ${Number(candidate.entry_index) + 1}`);
                button.addEventListener('click', () => apply(candidate.id, direction.value));
                controls.append(label, direction, button); card.append(controls);
            } else if (report.run_id) {
                card.append(node('p', report.stale ? 'Apply disabled: this report is stale.'
                    : candidate.apply_refusal || 'Apply requires both reasoning modes to suggest the same identity and a safe alias pair.', 'small text-muted mt-2 mb-0'));
            }
            container.append(card);
        }
        container.querySelectorAll('details').forEach((detail, index) => { detail.open = !!expanded[index]; });
        if (focused && container.querySelector(`[id="${focused.replace(/[^\w-]/g, '')}"]`)) { document.getElementById(focused).focus(); }
        syncControls();
    }
    async function apply(candidateId, direction) {
        if (state.pending || state.polling || active() || !state.reliable || !state.run
            || window._existingUploadSelectionPending) { return; }
        const owner = context(), run = state.run;
        const candidate = run.candidates?.find(item => item.id === candidateId);
        const choice = getEligibleDirections(candidate, run).find(item => item.direction === direction);
        if (!choice) { return; }
        const {alias, canonical} = choice;
        state.pending = 'apply'; syncControls();
        try {
            const confirmed = await showConfirm(`Save alias “${alias}” → “${canonical}” in the shared character registry? Later Review runs can merge these names using this registry. This does not rewrite the current script. The reference and model judgments are provisional. This single change makes the remaining report stale.`,
                {title: 'Apply this one alias?', actionLabel: 'Save alias'});
            if (!confirmed || !current(owner) || state.run !== run
                || !getEligibleDirections(run.candidates?.find(item => item.id === candidateId), run)
                    .some(item => item.direction === direction
                    && item.alias === alias && item.canonical === canonical)
                || window._existingUploadSelectionPending) { return; }
            const result = await API.post(`/api/speaker_review/${encodeURIComponent(run.run_id)}/apply`,
                {snapshot: run.snapshot, candidate_id: candidate.id, alias, canonical});
            if (!current(owner) || state.run !== run) { return; }
            run.applied = result.applied || [candidate.id]; run.stale = true;
            state.preview = null;
            renderResults(run);
            status(`Saved alias “${alias}” → “${canonical}” in the shared registry. It will be used by later Review runs; this did not rewrite your script. Load options and preview again before another Apply.`, 'success');
            if (typeof clearCharacterAliases === 'function') { clearCharacterAliases(); }
        } catch (error) {
            if (!current(owner)) { return; }
            // Even a lost successful response must never offer a duplicate/stale Apply.
            run.stale = true; state.reliable = false; renderResults(run);
            status(getActionErrorMessage('Alias save was not confirmed', error,
                'Apply is disabled. Check Edit aliases, then load options and preview again before another change.'), 'warning');
        } finally {
            if (current(owner)) { state.pending = null; syncControls(); }
        }
    }
    element('load').addEventListener('click', loadOptions);
    element('preview').addEventListener('click', preview);
    element('start').addEventListener('click', start);
    element('cancel').addEventListener('click', cancel);
    element('cancel-previous').addEventListener('click', cancelPrevious);
    element('refresh').addEventListener('click', refreshStatus);
    element('consent').addEventListener('change', syncControls);
    for (const name of ['source', 'reference', 'max-pairs', 'partial']) { element(name).addEventListener('change', invalidatePreview); }
    syncControls();
})();
