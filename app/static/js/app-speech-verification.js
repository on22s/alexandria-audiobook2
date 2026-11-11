/* Optional checks affect subsequent renders only after configuration is saved. */
let speechVerificationControlsLoaded = false;
let speechVerificationBusy = false;
let speechVerificationReattached = false;

function updateSpeechRetryControls() {
    const verificationEnabled = document.getElementById('speech-verification-enabled');
    const speechRetryEnabled = document.getElementById('speech-retry-enabled');
    if (!speechRetryEnabled) { return; }
    const verificationOn = verificationEnabled?.checked === true;
    if (speechVerificationControlsLoaded && verificationOn) {
        speechRetryEnabled.disabled = false;
    } else {
        speechRetryEnabled.disabled = true;
        speechRetryEnabled.checked = false;
    }
}

function setSpeechVerificationControls(settings, retrySettings) {
    const enabled = document.getElementById('speech-verification-enabled');
    if (!enabled) { return; }
    const speechRetryEnabled = document.getElementById('speech-retry-enabled');
    enabled.checked = settings?.enabled === true;
    enabled.disabled = false;
    speechVerificationControlsLoaded = true;
    if (speechRetryEnabled) {
        speechRetryEnabled.checked = retrySettings?.enabled === true;
    }
    updateSpeechRetryControls();
}

function getSpeechVerificationConfig() {
    if (!speechVerificationControlsLoaded) { return {}; }
    const verificationEnabled = document.getElementById('speech-verification-enabled');
    const speechRetryEnabled = document.getElementById('speech-retry-enabled');
    const verificationOn = verificationEnabled?.checked === true;
    const retryOn = speechRetryEnabled?.checked === true && verificationOn;
    return {speech_verification: {enabled: verificationOn},
        speech_retry: {enabled: retryOn}};
}

function isSpeechVerificationTaskActive() {
    return speechVerificationBusy || speechVerificationReattached;
}

function getSpeechVerificationTargetIdentity(target) {
    return target && JSON.stringify([chunkSnapshotBookToken,target.chunk.uid,
        getChunkAudioIdentity(target.chunk),
        target.chunk.speech_verification?.input_digest
            || target.chunk.speech_verification_historical?.input_digest,
        target.chunk.text,target.chunk.instruct,target.chunk.speaker,target.chunk.take,
        target.chunk.voice_revision,target.chunk.pronunciation_rules]);
}

window.applySpeechVerificationInspector = () => {
    const button=document.getElementById('editor-line-speech-recheck');
    if (!button) { return; }
    const target=getEditorReviewTarget();
    const receipt=target?.chunk.speech_verification || target?.chunk.speech_verification_historical;
    const historicalReason=target?.chunk.speech_verification_historical_reason;
    const historicalUncheckable=target?.chunk.speech_verification_historical
        && !target?.chunk.speech_verification && !['legacy','changed'].includes(historicalReason);
    button.disabled=isSpeechVerificationTaskActive() || isEditorEditPending() || !target?.chunk.audio_path
        || historicalUncheckable || !receipt?.expected_text || !receipt.audio?.sha256
        || receipt.uid !== target.chunk.uid || receipt.audio_revision !== target.chunk.audio_revision
        || receipt.book_token !== chunkSnapshotBookToken;
    document.getElementById('editor-line-speech-cancel').disabled=!isSpeechVerificationTaskActive();
};

function applySpeechVerificationTaskActivity(running) {
    speechVerificationReattached=running;
    document.getElementById('editor-line-speech-status').textContent=running
        ? t('editor.speech.running', 'A speech recheck is running. Cancellation waits for owned provider cleanup.')
        : t('editor.speech.finished', 'Speech recheck task finished. Refresh review to see current evidence or inspect task logs.');
    window.applySpeechVerificationInspector();
}

async function recheckEditorSpeech() {
    if (isSpeechVerificationTaskActive()) { return; }
    try { await ensureEditorRenderSnapshot(); }
    catch (error) { showActionError(t('editor.speech.not-ready', 'Editor is not ready for a speech recheck'),error,t('editor.speech.not-ready-recovery', 'Save pending edits and refresh the selected line.'));return; }
    if (isSpeechVerificationTaskActive()) { return; }
    const target=getEditorReviewTarget();
    window.applySpeechVerificationInspector();
    if (!target || document.getElementById('editor-line-speech-recheck').disabled) {
        showToast(t('editor.speech.choose-take', 'Choose a saved take with recorded synthesis input. Takes without recorded input must be regenerated with speech checks enabled.'),'warning');return;
    }
    const identity=getSpeechVerificationTargetIdentity(target);
    const book=chunkSnapshotBookToken;
    speechVerificationBusy=true;window.applySpeechVerificationInspector();
    document.getElementById('editor-line-speech-status').textContent=t('editor.speech.rechecking', 'Rechecking this take with cached CPU models. Audio stays selected; checks can take time.');
    try {
        const result=await API.post(`/api/chunks/${target.chunk.id}/speech_verification`,{
            expected_book_token:book,expected_uid:target.chunk.uid,expected_audio_revision:target.chunk.audio_revision,
        });
        await ensureEditorRenderSnapshot();
        if (identity !== getSpeechVerificationTargetIdentity(getEditorReviewTarget()) || isEditorEditPending()) { return; }
        if (result.book_token !== book || result.uid !== target.chunk.uid
                || result.receipt?.audio_revision !== target.chunk.audio_revision) { throw new Error(t('editor.speech.wrong-take', 'Speech result belongs to another take.')); }
        // The render barrier saves edits and returns rows; it does not install
        // a poll snapshot. Refresh the actual cached rows/revision before
        // requesting quality facts for the newly recorded evidence.
        await ensureChunkRefresh(false);
        if (identity !== getSpeechVerificationTargetIdentity(getEditorReviewTarget()) || isEditorEditPending()) { return; }
        await refreshEditorReview();
        if (identity !== getSpeechVerificationTargetIdentity(getEditorReviewTarget()) || isEditorEditPending()) { return; }
        document.getElementById('editor-line-speech-status').textContent=editorReviewFresh
            ? t('editor.speech.recorded', 'Speech recheck recorded. Review the evidence and listen before accepting the take.')
            : formatUiSafetyText('safety.editor.speech.snapshot-changed', 'Speech recheck recorded, but the review snapshot changed. Refresh review for current evidence.');
    } catch (error) {
        if (identity === getSpeechVerificationTargetIdentity(getEditorReviewTarget())) {
            document.getElementById('editor-line-speech-status').textContent=t('editor.speech.unavailable', 'Speech recheck unavailable or cancelled. Saved audio is retained.');
            showActionError(t('editor.speech.error.title', 'Could not recheck speech'),error,t('editor.speech.error.recovery', 'Refresh review, inspect the recorded input, and check cached CPU model availability.'));
        }
    } finally {
        speechVerificationBusy=false;window.applySpeechVerificationInspector();
        if (identity !== getSpeechVerificationTargetIdentity(getEditorReviewTarget())) {
            document.getElementById('editor-line-speech-status').textContent=t('editor.speech.other-selection', 'Recheck finished for a different selection. Refresh review for current evidence.');
        }
    }
}

async function cancelEditorSpeechVerification() {
    try { await API.post('/api/speech_verification/cancel',{}); }
    catch (error) { showActionError(t('safety.editor.speech.cancel-unconfirmed', 'Speech-check cancellation is unconfirmed'),error,t('editor.speech.cancel-recovery', 'Inspect speech verification task status before retrying.')); }
}
