// Opt-in local translation preview: a reviewable proposal, never an approval.
let translatedEditionEpoch = 0, translatedEditionBusy = false, translatedEditionProposal = null;
let translatedEditionCancelToken = null, translatedEditionBusyId = 0;
let translatedEditionPairsPromise = null;
const translatedEditionElement = id => document.getElementById('translated-edition-' + id);

function updateTranslatedEditionControls() {
    const pairReady = !!translatedEditionElement('pair').value;
    translatedEditionElement('create').disabled = translatedEditionBusy || !pairReady;
    translatedEditionElement('refresh').disabled = translatedEditionBusy;
    translatedEditionElement('pair').disabled = translatedEditionBusy;
    translatedEditionElement('cancel').disabled = !translatedEditionBusy || !translatedEditionCancelToken;
}

function newTranslatedEditionToken() {
    const bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
        window.crypto.getRandomValues(bytes);
    } else {
        for (let index = 0; index < bytes.length; index += 1) {
            bytes[index] = Math.floor(Math.random() * 256);
        }
    }
    return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
}

function resetTranslatedEditionBook() {
    translatedEditionEpoch += 1;
    translatedEditionProposal = null;
    translatedEditionCancelToken = null;
    translatedEditionBusy = false;
    translatedEditionElement('review').replaceChildren();
    translatedEditionElement('pair').replaceChildren();
    translatedEditionElement('feedback').textContent = t('translation.panel-cleared', 'Panel cleared. Re-opening the panel shows the most recent saved preview for the current book, if one is still on disk; a preview from another book is never shown for this book.');
    updateTranslatedEditionControls();
}

async function onTranslatedEditionPanelToggle() {
    // Toggling never bumps the epoch: a running build must keep its result,
    // and a build that finished while the panel was closed is rendered here.
    if (!translatedEditionElement('panel').open) {
        return;
    }
    // A running build owns the panel: re-opening it must not re-check an
    // older proposal over the build's own feedback. Only the build blocks
    // here (a cancel token is held for exactly that); a pair refresh does
    // not, so opening the panel during one still loads the saved proposal.
    if (translatedEditionCancelToken !== null) {
        return;
    }
    // Wait for an in-flight pair refresh instead of dropping the saved
    // proposal (an early return while busy used to leave the panel empty
    // until the next toggle), then read the proposal. Reading pairs first
    // keeps a slow pair list from answering last and replacing the saved
    // preview's message (stale, or unreadable files) with a pair count.
    const pairNote = await refreshTranslatedEditionPairs();
    if (!translatedEditionElement('panel').open || translatedEditionCancelToken !== null) {
        return;
    }
    const epoch = translatedEditionEpoch;
    if (translatedEditionProposal) {
        await refreshTranslatedEditionProposal();
    } else {
        await loadTranslatedEditionLatestProposal();
    }
    // A pair problem (package unavailable, nothing installed, or the check
    // failing) is a fact the user still needs next to the saved preview's
    // message, so it comes after the proposal load instead of being lost.
    const feedback = translatedEditionElement('feedback').textContent;
    if (pairNote && epoch === translatedEditionEpoch && feedback) {
        translatedEditionElement('feedback').textContent = joinUiSentences([feedback, pairNote]);
    }
}

function refreshTranslatedEditionPairs() {
    // While busy the in-flight operation owns the panel; when it is a pairs
    // refresh, callers await its shared promise so none of them silently
    // skips the saved proposal that opens behind it.
    if (translatedEditionBusy) {
        return translatedEditionPairsPromise;
    }
    const promise = (async () => {
        const epoch = ++translatedEditionEpoch;
        const selection = window._existingUploadSelectionRequest;
        const busyId = ++translatedEditionBusyId;
        translatedEditionBusy = true;
        updateTranslatedEditionControls();
        translatedEditionElement('feedback').textContent = t('translation.checking-pairs', 'Checking installed language pairs...');
        try {
            const pairs = await API.get('/api/translated_edition/pairs');
            if (epoch !== translatedEditionEpoch || selection !== window._existingUploadSelectionRequest) {
                return null;
            }
            const select = translatedEditionElement('pair');
            select.replaceChildren();
            if (!pairs.available) {
                const note = t('translation.package-unavailable', 'Translation package is unavailable on this machine. Install it separately to build a preview; nothing was downloaded.');
                translatedEditionElement('feedback').textContent = note;
                updateTranslatedEditionControls();
                return note;
            }
            if (!pairs.pairs.length) {
                const note = t('translation.no-pairs', 'No language pair is installed. Install one separately; nothing was downloaded.');
                translatedEditionElement('feedback').textContent = note;
                updateTranslatedEditionControls();
                return note;
            }
            const placeholder = document.createElement('option');
            placeholder.value = '';
            placeholder.textContent = t('translation.choose-pair', 'Choose one installed pair');
            select.appendChild(placeholder);
            for (const pair of pairs.pairs) {
                const option = document.createElement('option');
                option.value = `${pair.from_code}->${pair.to_code}`;
                option.textContent = `${pair.from_name || pair.from_code} -> ${pair.to_name || pair.to_code}`;
                select.appendChild(option);
            }
            select.value = '';
            translatedEditionElement('feedback').textContent = t('translation.pairs-listed', '{count} installed pairs listed. Choosing one does not change any language setting.', {count: pairs.pairs.length});
            return null;
        } catch (error) {
            if (epoch === translatedEditionEpoch && selection === window._existingUploadSelectionRequest) {
                translatedEditionElement('feedback').textContent = error.message;
                return error.message;
            }
            return null;
        } finally {
            if (busyId === translatedEditionBusyId) {
                translatedEditionBusy = false;
                updateTranslatedEditionControls();
            }
        }
    })();
    translatedEditionPairsPromise = promise;
    return promise;
}

async function loadTranslatedEditionLatestProposal() {
    // Called only when no build is running (see the panel toggle); the
    // epoch and selection guards below discard a stale reply.
    const epoch = translatedEditionEpoch;
    const selection = window._existingUploadSelectionRequest;
    try {
        const result = await API.get('/api/translated_edition/latest-proposal');
        if (epoch !== translatedEditionEpoch || selection !== window._existingUploadSelectionRequest) {
            return;
        }
        const proposal = result.proposal;
        const malformed = result.malformed_proposals || [];
        if (proposal) {
            translatedEditionProposal = proposal;
            renderTranslatedEditionProposal(proposal);
            translatedEditionElement('feedback').textContent = joinUiSentences([(proposal.stale
                ? t('translation.preview.stale', 'This preview is stale: rebuild it from the current saved Script.')
                : t('translation.preview.matches', 'This preview still matches the saved Script. It remains a proposal, not an approved translation.')),
                malformed.length ? t('translation.malformed', '{count} saved proposal file(s) could not be read and were skipped.', {count: malformed.length}) : '']);
        } else if (malformed.length) {
            translatedEditionElement('feedback').textContent = t('translation.no-readable-proposal', 'No readable saved preview for this book. {count} saved proposal file(s) could not be read and were skipped.', {count: malformed.length});
        } else {
            translatedEditionElement('feedback').textContent = t('translation.no-proposal', 'No saved translation preview for this book yet.');
        }
    } catch (error) {
        if (epoch === translatedEditionEpoch && selection === window._existingUploadSelectionRequest) {
            translatedEditionElement('feedback').textContent = error.message;
        }
    }
}

async function refreshTranslatedEditionProposal() {
    const known = translatedEditionProposal;
    if (!known) {
        return;
    }
    const epoch = translatedEditionEpoch;
    const selection = window._existingUploadSelectionRequest;
    try {
        const proposal = await API.get(`/api/translated_edition/proposals/${known.proposal_id}`);
        if (epoch !== translatedEditionEpoch || selection !== window._existingUploadSelectionRequest
            || !translatedEditionProposal || translatedEditionProposal.proposal_id !== known.proposal_id) {
            return;
        }
        translatedEditionProposal = proposal;
        renderTranslatedEditionProposal(proposal);
        translatedEditionElement('feedback').textContent = proposal.stale
            ? t('translation.preview.stale', 'This preview is stale: rebuild it from the current saved Script.')
            : t('translation.preview.matches', 'This preview still matches the saved Script. It remains a proposal, not an approved translation.');
    } catch (error) {
        if (epoch === translatedEditionEpoch && selection === window._existingUploadSelectionRequest
            && translatedEditionProposal && translatedEditionProposal.proposal_id === known.proposal_id) {
            translatedEditionElement('feedback').textContent = error.message;
        }
    }
}

function renderTranslatedEditionProposal(proposal) {
    const review = translatedEditionElement('review');
    review.replaceChildren();
    const status = String(proposal.status).replace(/_/g, ' ');
    const heading = document.createElement('p');
    heading.className = 'small';
    heading.textContent = t('translation.proposal.heading', 'Proposal, not approved. Status: {status}. {translated} translated, {flagged} flagged, {unsupported} unsupported, {failed} failed of {rows} excerpt rows (script SHA {sha}...). Target {target}.', {
        status,
        translated: proposal.counts.translated,
        flagged: proposal.counts.flagged,
        unsupported: proposal.counts.unsupported,
        failed: proposal.counts.failed,
        rows: proposal.excerpt.row_count,
        sha: proposal.source_script_sha256.slice(0, 12),
        target: proposal.target_language,
    });
    review.appendChild(heading);
    if (proposal.excerpt.total_rows > proposal.excerpt.row_count) {
        const excerptNote = document.createElement('p');
        excerptNote.className = 'small';
        excerptNote.textContent = t('translation.excerpt-only', 'Excerpt only: the first {shown} of {total} Script rows. The rest of the book was not translated.', {
            shown: proposal.excerpt.row_count,
            total: proposal.excerpt.total_rows,
        });
        review.appendChild(excerptNote);
    }
    const reasons = [];
    if (proposal.counts.flagged) reasons.push(t('translation.reason.flagged', '{count} flagged for review', {count: proposal.counts.flagged}));
    if (proposal.counts.unsupported) reasons.push(t('translation.reason.unsupported', '{count} unsupported', {count: proposal.counts.unsupported}));
    if (proposal.counts.failed) reasons.push(t('translation.reason.failed', '{count} failed', {count: proposal.counts.failed}));
    if (proposal.counts.skipped) reasons.push(t('translation.reason.skipped', '{count} skipped', {count: proposal.counts.skipped}));
    if (proposal.counts.cancelled) reasons.push(t('translation.reason.cancelled', '{count} cancelled', {count: proposal.counts.cancelled}));
    if (reasons.length) {
        const reasonNote = document.createElement('p');
        reasonNote.className = 'small';
        reasonNote.textContent = t('translation.incomplete', 'Why this is not complete: {reasons}.', {reasons: reasons.join(', ')});
        review.appendChild(reasonNote);
    }
    if (proposal.stale) {
        const stale = document.createElement('p');
        stale.id = 'translated-edition-stale';
        stale.className = 'small text-warning';
        stale.textContent = formatUiSafetyText('safety.translation.preview.stale', 'Stale: the saved Script changed after this preview was built. Build a new preview.');
        review.appendChild(stale);
    }
    for (const row of proposal.rows) {
        const item = document.createElement('div');
        item.className = 'border rounded p-2 mb-2';
        const meta = document.createElement('p');
        meta.className = 'small mb-1';
        meta.textContent = t('translation.row.meta', '#{index} {speaker} [{status}{flags}]', {
            index: row.index + 1,
            speaker: row.speaker || '',
            status: row.status,
            flags: row.flags.length ? t('translation.row.flags', ', review: {flags}', {flags: row.flags.join(', ')}) : '',
        });
        item.appendChild(meta);
        const source = document.createElement('p');
        source.className = 'mb-1';
        source.textContent = t('translation.row.source', 'Source: {text}', {text: row.source_text});
        item.appendChild(source);
        const translated = document.createElement('p');
        translated.className = 'mb-0';
        translated.textContent = t('translation.row.proposal', 'Proposal: {text}', {text: row.translated_text || t('translation.row.no-translation', '(no translation)')});
        if (row.error) {
            translated.textContent += ` (${row.error})`;
        }
        item.appendChild(translated);
        review.appendChild(item);
    }
}

async function createTranslatedEditionProposal() {
    if (translatedEditionBusy) {
        return;
    }
    const selected = translatedEditionElement('pair').value;
    if (!selected) {
        return;
    }
    const epoch = ++translatedEditionEpoch;
    const selection = window._existingUploadSelectionRequest;
    const [sourceLanguage, targetLanguage] = selected.split('->');
    const cancelToken = newTranslatedEditionToken();
    const busyId = ++translatedEditionBusyId;
    translatedEditionCancelToken = cancelToken;
    translatedEditionBusy = true;
    updateTranslatedEditionControls();
    translatedEditionElement('feedback').textContent = t('translation.building', 'Building translation preview...');
    try {
        const proposal = await API.post('/api/translated_edition/proposals',
            {source_language: sourceLanguage, target_language: targetLanguage,
             cancel_token: cancelToken});
        if (epoch !== translatedEditionEpoch || selection !== window._existingUploadSelectionRequest) {
            return;
        }
        translatedEditionProposal = proposal;
        translatedEditionElement('feedback').textContent = t('translation.built', 'Proposal {id} built (status {status}). It is saved with the active book as a proposal, not an approved translation, and changes nothing else.', {
            id: proposal.proposal_id.slice(0, 12),
            status: String(proposal.status).replace(/_/g, ' '),
        });
        renderTranslatedEditionProposal(proposal);
    } catch (error) {
        if (epoch === translatedEditionEpoch && selection === window._existingUploadSelectionRequest) {
            translatedEditionElement('feedback').textContent = error.message;
        }
    } finally {
        if (busyId === translatedEditionBusyId) {
            translatedEditionBusy = false;
            if (translatedEditionCancelToken === cancelToken) {
                translatedEditionCancelToken = null;
            }
            updateTranslatedEditionControls();
        }
    }
}

async function cancelTranslatedEditionProposal() {
    const token = translatedEditionCancelToken;
    if (!token) {
        return;
    }
    const epoch = translatedEditionEpoch;
    const selection = window._existingUploadSelectionRequest;
    translatedEditionElement('cancel').disabled = true;
    try {
        const result = await API.post(`/api/translated_edition/proposals/${token}/cancel`, {});
        if (epoch !== translatedEditionEpoch || selection !== window._existingUploadSelectionRequest) {
            return;
        }
        translatedEditionElement('feedback').textContent = result.cancelled
            ? formatUiSafetyText('safety.translation.cancel-requested', 'Cancel requested. If the preview was still building, it stops after the current row and is saved as cancelled; if it had already finished before the request arrived, its finished status stands.')
            : t('translation.cancel.nothing-running', 'No preview was running when the cancel request arrived.');
    } catch (error) {
        if (epoch === translatedEditionEpoch) {
            translatedEditionElement('feedback').textContent = error.message;
        }
        updateTranslatedEditionControls();
    }
}
