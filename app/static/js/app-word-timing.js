/* Word timings belong to one saved take; highlighting never rewrites its text. */
let wordTimingState = null;
let wordTimingSequence = 0;
let wordTimingBusy = false;
let wordTimingReattached = false;
let wordTimingExpectedIdentity = null;
let wordTimingAudio = null;
let wordTimingListener = null;
let wordTimingErrorListener = null;

function isWordTimingTaskActive() {
    return wordTimingBusy || wordTimingReattached;
}

function getWordTimingTargetIdentity(target) {
    return target && JSON.stringify([chunkSnapshotBookToken, target.chunk.uid,
        getChunkAudioIdentity(target.chunk), target.chunk.text, target.chunk.instruct, target.chunk.speaker]);
}

function resetWordTimingListening() {
    wordTimingSequence++;
    wordTimingState = null;
    wordTimingExpectedIdentity = null;
    if (wordTimingAudio && wordTimingListener) {
        for (const event of ['timeupdate', 'seeked', 'pause', 'ended']) { wordTimingAudio.removeEventListener(event, wordTimingListener); }
    }
    if (wordTimingAudio && wordTimingErrorListener) { wordTimingAudio.removeEventListener('error', wordTimingErrorListener); }
    wordTimingAudio = null;
    wordTimingListener = null;
    wordTimingErrorListener = null;
    document.getElementById('editor-word-spans').replaceChildren();
    const gaps = document.getElementById('editor-sentence-gaps');
    if (gaps) { gaps.replaceChildren(); gaps.hidden = true; }
}

window.applyWordTimingInspector = () => {
    const target = getEditorReviewTarget();
    const button = document.getElementById('editor-word-timing');
    if (!button) { return; }
    if ((wordTimingState || wordTimingExpectedIdentity) && (
            (wordTimingState?.identity || wordTimingExpectedIdentity) !== getWordTimingTargetIdentity(target)
            || isEditorEditPending() || (wordTimingAudio && target?.row.querySelector('.chunk-audio') !== wordTimingAudio))) {
        resetWordTimingListening();
        document.getElementById('editor-word-status').textContent = 'The selected line or take changed. Request timing again.';
    }
    button.disabled = isWordTimingTaskActive() || !target?.chunk.audio_path || isEditorEditPending();
    const gapsButton = document.getElementById('editor-sentence-gap-analyze');
    if (gapsButton) { gapsButton.disabled = button.disabled; }
};

function applyWordTimingTaskActivity(running) {
    wordTimingReattached = running;
    document.getElementById('editor-word-cancel').disabled = !isWordTimingTaskActive();
    if (!isWordTimingTaskActive() && !wordTimingState) {
        document.getElementById('editor-word-status').textContent = 'Timing task finished. Request timing again to load its saved result or inspect any reported failure.';
    }
    window.applyWordTimingInspector();
}

function analyzeEditorSentenceGaps() { return createEditorWordTiming(true); }

async function createEditorWordTiming(analyzeSentenceGaps = false) {
    if (isWordTimingTaskActive()) { return; }
    try { await ensureEditorRenderSnapshot(); }
    catch (error) { showActionError('Editor is not ready for word timing', error, 'Save pending edits and refresh the selected line.'); return; }
    if (isWordTimingTaskActive()) { return; }
    const target = getEditorReviewTarget();
    if (!target?.chunk.audio_path || isEditorEditPending()) { showToast('Choose a saved line with generated audio.', 'warning'); return; }
    resetWordTimingListening();
    const sequence = wordTimingSequence;
    const identity = getWordTimingTargetIdentity(target);
    const expectedBook = chunkSnapshotBookToken;
    wordTimingExpectedIdentity = identity;
    wordTimingBusy = true;
    document.getElementById('editor-word-cancel').disabled = false;
    document.getElementById('editor-word-status').textContent = analyzeSentenceGaps
        ? 'Analyzing sentence gaps in this saved take. Capturing English word timings if needed…'
        : 'Timing one English line using the installed CPU ASR model…';
    window.applyWordTimingInspector();
    try {
        const result = await API.post(`/api/chunks/${target.chunk.id}/word_timing`, {
            expected_book_token: expectedBook, expected_uid: target.chunk.uid,
            expected_audio_revision: target.chunk.audio_revision || null,
            analyze_sentence_gaps: analyzeSentenceGaps,
        });
        await ensureEditorRenderSnapshot();
        const current = getEditorReviewTarget();
        if (sequence !== wordTimingSequence || identity !== getWordTimingTargetIdentity(current) || isEditorEditPending()) { return; }
        if (result.binding.book_token !== expectedBook || result.binding.uid !== target.chunk.uid
                || (result.binding.row.audio_revision || null) !== (target.chunk.audio_revision || null)) {
            throw new Error('Timing result belongs to another saved take.');
        }
        if (analyzeSentenceGaps) { validateSentenceGapReport(result); }
        wordTimingState = {identity, result};
        renderWordTimingListening(current, result);
        if (analyzeSentenceGaps) { renderSentenceGapReport(result); }
    } catch (error) {
        if (sequence === wordTimingSequence) {
            document.getElementById('editor-word-status').textContent = 'Timing is unavailable. Existing line audio is unchanged.';
            showActionError(analyzeSentenceGaps ? 'Could not analyze sentence gaps' : 'Could not time this line', error, 'The initial provider supports English audio and needs an already installed whisper.cpp Small.en model. No download starts automatically.');
        }
    } finally {
        wordTimingBusy = false;
        wordTimingExpectedIdentity = null;
        document.getElementById('editor-word-cancel').disabled = !isWordTimingTaskActive();
        window.applyWordTimingInspector();
    }
}

function renderWordTimingListening(target, result) {
    const area = document.getElementById('editor-word-spans');
    area.replaceChildren();
    const audio = target.row.querySelector('.chunk-audio');
    const params = new URLSearchParams({expected_book_token: result.binding.book_token,
        expected_uid: result.binding.uid, sha256: result.binding.audio_sha256});
    if (result.binding.row.audio_revision) { params.set('expected_audio_revision', result.binding.row.audio_revision); }
    const verifiedSource = `/api/chunks/${target.chunk.id}/word_timing/audio?${params}`;
    audio.pause();
    audio.src = verifiedSource;
    audio.load();
    const buttons = [];
    const characters = Array.from(result.display_text);
    let offset = 0;
    for (const span of result.spans) {
        area.appendChild(document.createTextNode(characters.slice(offset, span.from).join('')));
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-link btn-sm p-0 word-timing-span';
        button.textContent = characters.slice(span.from, span.to).join('');
        button.setAttribute('aria-label', `Seek to ${button.textContent}, ${span.start.toFixed(1)} seconds`);
        button.onclick = () => {
            if (!wordTimingState || wordTimingState.identity !== getWordTimingTargetIdentity(getEditorReviewTarget()) || isEditorEditPending()) {
                resetWordTimingListening();
                return;
            }
            audio.currentTime = span.start;
            audio.focus();
        };
        buttons.push({button, span});
        area.appendChild(button);
        offset = span.to;
    }
    area.appendChild(document.createTextNode(characters.slice(offset).join('')));
    wordTimingAudio = audio;
    wordTimingListener = () => {
        if (!wordTimingState || wordTimingState.identity !== getWordTimingTargetIdentity(getEditorReviewTarget()) || isEditorEditPending()) {
            resetWordTimingListening();
            return;
        }
        for (const {button, span} of buttons) {
            const active = !audio.ended && audio.currentTime >= span.start && audio.currentTime < span.end;
            button.classList.toggle('word-timing-active', active);
            if (active) { button.setAttribute('aria-current', 'true'); }
            else { button.removeAttribute('aria-current'); }
        }
    };
    for (const event of ['timeupdate', 'seeked', 'pause', 'ended']) { audio.addEventListener(event, wordTimingListener); }
    wordTimingErrorListener = () => { resetWordTimingListening(); document.getElementById('editor-word-status').textContent = 'The timed audio could not be verified or loaded. Refresh Editor and request timing again.'; };
    audio.addEventListener('error', wordTimingErrorListener);
    document.getElementById('editor-word-status').textContent = `${result.spans.length} timed span(s) · ${result.cached ? 'saved timing' : 'new timing'}. ${result.notice}`;
    wordTimingListener();
}

async function cancelEditorWordTiming() {
    try { await API.post('/api/word_timing/cancel', {}); }
    catch (error) { showActionError('Timing cancellation is unconfirmed', error, 'Check word-timing task status before retrying.'); }
}

// Only display reports from the verified selected take. Text is always rendered
// through textContent, including source excerpts and provider skip reasons.
function validateSentenceGapReport(result) {
    const report = result.sentence_gap_report;
    if (!report || report.kind !== 'sentence_gap_report_only' || report.mutates_audio !== false
            || report.authorizes_edit !== false || !Array.isArray(report.proposals)
            || !/^[0-9a-f]{64}$/.test(result.binding.audio_sha256)
            || report.provenance?.file_sha256 !== result.binding.audio_sha256) {
        throw new Error('Sentence-gap report does not belong to this verified take.');
    }
}

function renderSentenceGapReport(result) {
    const report = result.sentence_gap_report;
    const area = document.getElementById('editor-sentence-gaps');
    area.replaceChildren();
    const add = (parent, tag, text) => {
        const node = document.createElement(tag); node.textContent = text; parent.appendChild(node); return node;
    };
    const suggested = report.proposals.filter(p => p.status === 'proposal').length;
    add(area, 'h4', 'Sentence-gap suggestions').className = 'h6';
    add(area, 'p', suggested + ' suggestion(s). Review by listening; suggestions do not edit audio or certify speech accuracy. Pauses between Editor rows are separate.');
    const characters = Array.from(result.display_text);
    const reasons = {
        explicit_user_pause: 'An explicit pause already exists here.',
        uncertain_sentence_boundary: 'The sentence break is uncertain (for example, an abbreviation or ellipsis).',
        ambiguous_grouped_word_timing_at_boundary: 'A timed word group crosses this sentence break.',
        missing_asr_timing_for_neighbouring_words: 'The words beside this break could not be timed reliably.',
        neighbouring_span_not_confined_to_sentence: 'A timed word group extends beyond this sentence.',
        overlapping_asr_boundaries: 'Word timings overlap at this break.',
        no_gap_in_asr_search_window: 'The word timings leave no measurable gap.',
        unmatched_observed_speech_in_search_window: 'Unmatched speech was detected inside the gap.',
        take_below_quiet_threshold: 'The whole take is too quiet for a reliable pause suggestion.',
        no_quiet_interval_in_search_window: 'No quiet interval was found between these words.',
        quiet_interval_below_minimum: 'The quiet interval is shorter than the minimum.',
        quiet_interval_too_close_to_word_edge: 'The quiet interval is too close to a spoken word.',
        within_word_dip: 'The quiet dip may be inside a spoken word.',
        quiet_interval_rounds_to_zero_ms: 'The usable pause is too short to express in milliseconds.',
    };
    if (!report.proposals.length) { add(area, 'p', 'No internal sentence breaks were found. Try a saved line containing two or more complete sentences.'); }
    const list = document.createElement('ol'); area.appendChild(list);
    for (const proposal of report.proposals) {
        const item = document.createElement('li'); item.className = 'mb-3'; list.appendChild(item);
        const coordinates = proposal.original_coordinates;
        const excerpt = side => characters.slice(side.char_start, side.char_end).join('').trim().slice(0, 180);
        add(item, 'p', excerpt(coordinates.left_sentence) + ' / ' + excerpt(coordinates.right_sentence));
        if (proposal.status === 'proposal') {
            add(item, 'p', 'Suggested extra pause: ' + proposal.proposed_extension_ms + ' ms.');
        } else { add(item, 'p', 'No suggestion: ' + (reasons[proposal.skip_reason] || 'This boundary needs manual listening review.')); }
        const raw = proposal.measured_quiet_interval, safe = proposal.safe_quiet_interval;
        if (raw) { add(item, 'p', 'Measured quiet: ' + raw.start.toFixed(2) + '–' + raw.end.toFixed(2) + ' seconds.'); }
        if (safe) { add(item, 'p', 'Usable quiet after word-edge margins: ' + safe.start.toFixed(2) + '–' + safe.end.toFixed(2) + ' seconds.'); }
    }
    area.hidden = false;
    document.getElementById('editor-word-status').textContent = suggested + ' sentence-gap suggestion(s), '
        + (report.proposals.length - suggested) + ' skipped boundary/boundaries. Audio unchanged. Use the timed words to listen and review.';
}
