// Opt-in local translation preview: a reviewable proposal, never an approval.
let translatedEditionEpoch = 0, translatedEditionBusy = false, translatedEditionProposal = null;
const translatedEditionElement = id => document.getElementById('translated-edition-' + id);

function updateTranslatedEditionControls() {
    const pairReady = !!translatedEditionElement('pair').value;
    translatedEditionElement('create').disabled = translatedEditionBusy || !pairReady;
    translatedEditionElement('refresh').disabled = translatedEditionBusy;
    translatedEditionElement('pair').disabled = translatedEditionBusy;
}

function resetTranslatedEditionBook() {
    translatedEditionEpoch += 1;
    translatedEditionProposal = null;
    translatedEditionElement('review').replaceChildren();
    translatedEditionElement('pair').replaceChildren();
    translatedEditionElement('feedback').textContent = 'Book changed. Refresh the installed pairs and build a new preview.';
    updateTranslatedEditionControls();
    if (translatedEditionElement('panel').open && !translatedEditionBusy) {
        refreshTranslatedEditionPairs();
    }
}

function onTranslatedEditionPanelToggle() {
    translatedEditionEpoch += 1;
    if (translatedEditionElement('panel').open) {
        refreshTranslatedEditionPairs();
    }
}

async function refreshTranslatedEditionPairs() {
    if (translatedEditionBusy) {
        return;
    }
    const epoch = ++translatedEditionEpoch;
    const selection = window._existingUploadSelectionRequest;
    translatedEditionBusy = true;
    updateTranslatedEditionControls();
    translatedEditionElement('feedback').textContent = 'Checking installed language pairs...';
    try {
        const pairs = await API.get('/api/translated_edition/pairs');
        if (epoch !== translatedEditionEpoch || selection !== window._existingUploadSelectionRequest) {
            return;
        }
        const select = translatedEditionElement('pair');
        select.replaceChildren();
        if (!pairs.available) {
            translatedEditionElement('feedback').textContent = 'Translation package is unavailable on this machine. Install it separately to build a preview; nothing was downloaded.';
            updateTranslatedEditionControls();
            return;
        }
        if (!pairs.pairs.length) {
            translatedEditionElement('feedback').textContent = 'No language pair is installed. Install one separately; nothing was downloaded.';
            updateTranslatedEditionControls();
            return;
        }
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = 'Choose one installed pair';
        select.appendChild(placeholder);
        for (const pair of pairs.pairs) {
            const option = document.createElement('option');
            option.value = `${pair.from_code}->${pair.to_code}`;
            option.textContent = `${pair.from_name || pair.from_code} -> ${pair.to_name || pair.to_code}`;
            select.appendChild(option);
        }
        select.value = '';
        translatedEditionElement('feedback').textContent = `${pairs.pairs.length} installed pairs listed. Choosing one does not change any language setting.`;
    } catch (error) {
        if (epoch === translatedEditionEpoch) {
            translatedEditionElement('feedback').textContent = error.message;
        }
    } finally {
        translatedEditionBusy = false;
        updateTranslatedEditionControls();
    }
}

function renderTranslatedEditionProposal(proposal) {
    const review = translatedEditionElement('review');
    review.replaceChildren();
    const heading = document.createElement('p');
    heading.className = 'small';
    heading.textContent = `Proposal, not approved. Status: ${proposal.status}. ${proposal.counts.translated} translated, ${proposal.counts.flagged} flagged, ${proposal.counts.unsupported} unsupported, ${proposal.counts.failed} failed of ${proposal.excerpt.row_count} excerpt rows (script SHA ${proposal.source_script_sha256.slice(0, 12)}...). Target ${proposal.target_language}.`;
    review.appendChild(heading);
    if (proposal.stale) {
        const stale = document.createElement('p');
        stale.id = 'translated-edition-stale';
        stale.className = 'small text-warning';
        stale.textContent = 'Stale: the saved Script changed after this preview was built. Build a new preview.';
        review.appendChild(stale);
    }
    for (const row of proposal.rows) {
        const item = document.createElement('div');
        item.className = 'border rounded p-2 mb-2';
        const meta = document.createElement('p');
        meta.className = 'small mb-1';
        meta.textContent = `#${row.index + 1} ${row.speaker || ''} [${row.status}${row.flags.length ? `, review: ${row.flags.join(', ')}` : ''}]`;
        item.appendChild(meta);
        const source = document.createElement('p');
        source.className = 'mb-1';
        source.textContent = `Source: ${row.source_text}`;
        item.appendChild(source);
        const translated = document.createElement('p');
        translated.className = 'mb-0';
        translated.textContent = `Proposal: ${row.translated_text || '(no translation)'}`;
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
    translatedEditionBusy = true;
    updateTranslatedEditionControls();
    translatedEditionElement('feedback').textContent = 'Building translation preview...';
    try {
        const proposal = await API.post('/api/translated_edition/proposals',
            {source_language: sourceLanguage, target_language: targetLanguage});
        if (epoch !== translatedEditionEpoch || selection !== window._existingUploadSelectionRequest) {
            return;
        }
        translatedEditionProposal = proposal;
        translatedEditionElement('feedback').textContent = `Proposal ${proposal.proposal_id.slice(0, 12)} built (status ${proposal.status}). It is saved with the book as a proposal, not an approved translation, and changes nothing else.`;
        renderTranslatedEditionProposal(proposal);
    } catch (error) {
        if (epoch === translatedEditionEpoch) {
            translatedEditionElement('feedback').textContent = error.message;
        }
    } finally {
        translatedEditionBusy = false;
        updateTranslatedEditionControls();
    }
}
