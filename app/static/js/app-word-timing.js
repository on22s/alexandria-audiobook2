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
};

function applyWordTimingTaskActivity(running) {
    wordTimingReattached = running;
    document.getElementById('editor-word-cancel').disabled = !isWordTimingTaskActive();
    if (!isWordTimingTaskActive() && !wordTimingState) {
        document.getElementById('editor-word-status').textContent = 'Timing task finished. Request timing again to load its saved result or inspect any reported failure.';
    }
    window.applyWordTimingInspector();
}

async function createEditorWordTiming() {
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
    document.getElementById('editor-word-status').textContent = 'Timing one English line using the installed CPU ASR model…';
    window.applyWordTimingInspector();
    try {
        const result = await API.post(`/api/chunks/${target.chunk.id}/word_timing`, {
            expected_book_token: expectedBook, expected_uid: target.chunk.uid,
            expected_audio_revision: target.chunk.audio_revision || null,
        });
        await ensureEditorRenderSnapshot();
        const current = getEditorReviewTarget();
        if (sequence !== wordTimingSequence || identity !== getWordTimingTargetIdentity(current) || isEditorEditPending()) { return; }
        if (result.binding.book_token !== expectedBook || result.binding.uid !== target.chunk.uid
                || (result.binding.row.audio_revision || null) !== (target.chunk.audio_revision || null)) {
            throw new Error('Timing result belongs to another saved take.');
        }
        wordTimingState = {identity, result};
        renderWordTimingListening(current, result);
    } catch (error) {
        if (sequence === wordTimingSequence) {
            document.getElementById('editor-word-status').textContent = 'Timing is unavailable. Existing line audio is unchanged.';
            showActionError('Could not time this line', error, 'The initial provider supports English audio and needs an already installed whisper.cpp Small.en model. No download starts automatically.');
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
