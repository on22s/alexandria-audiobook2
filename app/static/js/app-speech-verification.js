/* Optional checks affect subsequent renders only after configuration is saved. */
let speechVerificationControlsLoaded = false;
let speechVerificationBusy = false;
let speechVerificationReattached = false;

function setSpeechVerificationControls(settings) {
    const enabled = document.getElementById('speech-verification-enabled');
    if (!enabled) { return; }
    enabled.checked = settings?.enabled === true;
    enabled.disabled = false;
    speechVerificationControlsLoaded = true;
}

function getSpeechVerificationConfig() {
    if (!speechVerificationControlsLoaded) { return {}; }
    return {speech_verification: {enabled: document.getElementById('speech-verification-enabled').checked}};
}

function isSpeechVerificationTaskActive() {
    return speechVerificationBusy || speechVerificationReattached;
}

function getSpeechVerificationTargetIdentity(target) {
    return target && JSON.stringify([chunkSnapshotBookToken,target.chunk.uid,
        getChunkAudioIdentity(target.chunk),target.chunk.speech_verification?.input_digest,
        target.chunk.text,target.chunk.instruct,target.chunk.speaker,target.chunk.take,
        target.chunk.voice_revision,target.chunk.pronunciation_rules]);
}

window.applySpeechVerificationInspector = () => {
    const button=document.getElementById('editor-line-speech-recheck');
    if (!button) { return; }
    const target=getEditorReviewTarget();
    const receipt=target?.chunk.speech_verification;
    button.disabled=isSpeechVerificationTaskActive() || isEditorEditPending() || !target?.chunk.audio_path
        || !receipt?.expected_text || !receipt.audio?.sha256 || receipt.uid !== target.chunk.uid
        || receipt.audio_revision !== target.chunk.audio_revision || receipt.book_token !== chunkSnapshotBookToken;
    document.getElementById('editor-line-speech-cancel').disabled=!isSpeechVerificationTaskActive();
};

function applySpeechVerificationTaskActivity(running) {
    speechVerificationReattached=running;
    document.getElementById('editor-line-speech-status').textContent=running
        ? 'A speech recheck is running. Cancellation waits for owned provider cleanup.'
        : 'Speech recheck task finished. Refresh review to see current evidence or inspect task logs.';
    window.applySpeechVerificationInspector();
}

async function recheckEditorSpeech() {
    if (isSpeechVerificationTaskActive()) { return; }
    try { await ensureEditorRenderSnapshot(); }
    catch (error) { showActionError('Editor is not ready for a speech recheck',error,'Save pending edits and refresh the selected line.');return; }
    if (isSpeechVerificationTaskActive()) { return; }
    const target=getEditorReviewTarget();
    window.applySpeechVerificationInspector();
    if (!target || document.getElementById('editor-line-speech-recheck').disabled) {
        showToast('Choose a saved take with recorded synthesis input. Regenerate older takes with speech checks enabled.','warning');return;
    }
    const identity=getSpeechVerificationTargetIdentity(target);
    const book=chunkSnapshotBookToken;
    speechVerificationBusy=true;window.applySpeechVerificationInspector();
    document.getElementById('editor-line-speech-status').textContent='Rechecking this take with cached CPU models. Audio stays selected; checks can take time.';
    try {
        const result=await API.post(`/api/chunks/${target.chunk.id}/speech_verification`,{
            expected_book_token:book,expected_uid:target.chunk.uid,expected_audio_revision:target.chunk.audio_revision,
        });
        await ensureEditorRenderSnapshot();
        if (identity !== getSpeechVerificationTargetIdentity(getEditorReviewTarget()) || isEditorEditPending()) { return; }
        if (result.book_token !== book || result.uid !== target.chunk.uid
                || result.receipt?.audio_revision !== target.chunk.audio_revision) { throw new Error('Speech result belongs to another take.'); }
        // The render barrier saves edits and returns rows; it does not install
        // a poll snapshot. Refresh the actual cached rows/revision before
        // requesting quality facts for the newly recorded evidence.
        await ensureChunkRefresh(false);
        if (identity !== getSpeechVerificationTargetIdentity(getEditorReviewTarget()) || isEditorEditPending()) { return; }
        await refreshEditorReview();
        if (identity !== getSpeechVerificationTargetIdentity(getEditorReviewTarget()) || isEditorEditPending()) { return; }
        document.getElementById('editor-line-speech-status').textContent=editorReviewFresh
            ? 'Speech recheck recorded. Review the evidence and listen before accepting the take.'
            : 'Speech recheck recorded, but the review snapshot changed. Refresh review for current evidence.';
    } catch (error) {
        if (identity === getSpeechVerificationTargetIdentity(getEditorReviewTarget())) {
            document.getElementById('editor-line-speech-status').textContent='Speech recheck unavailable or cancelled. Saved audio is retained.';
            showActionError('Could not recheck speech',error,'Refresh review, inspect the recorded input, and check cached CPU model availability.');
        }
    } finally {
        speechVerificationBusy=false;window.applySpeechVerificationInspector();
        if (identity !== getSpeechVerificationTargetIdentity(getEditorReviewTarget())) {
            document.getElementById('editor-line-speech-status').textContent='Recheck finished for a different selection. Refresh review for current evidence.';
        }
    }
}

async function cancelEditorSpeechVerification() {
    try { await API.post('/api/speech_verification/cancel',{}); }
    catch (error) { showActionError('Speech-check cancellation is unconfirmed',error,'Inspect speech verification task status before retrying.'); }
}
