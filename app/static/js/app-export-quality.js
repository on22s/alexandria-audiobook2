/* Completed checks and rejected attempts remain separate, scoped to this book. */
let exportQualityEpoch = 0;
function getExportQualityOptions() {
    const silence_dbfs = Number(document.getElementById('export-quality-noise').value);
    const max_unexpected_silence_seconds = Number(document.getElementById('export-quality-gap').value);
    if (!Number.isFinite(silence_dbfs) || silence_dbfs < -90 || silence_dbfs > -20
        || !Number.isFinite(max_unexpected_silence_seconds) || max_unexpected_silence_seconds < .1 || max_unexpected_silence_seconds > 3600) {
        throw new Error(t('audio.export-quality.threshold-error', 'Silence checks need a threshold from -90 to -20 dBFS and a gap from 0.1 to 3600 seconds.'));
    }
    return {silence_dbfs, max_unexpected_silence_seconds,
        reject_unexpected_silence: document.getElementById('export-quality-reject').checked};
}
function resetExportQualityView() {
    exportQualityEpoch++;
    document.getElementById('export-quality-reports').replaceChildren();
    document.getElementById('export-quality-status').textContent = t('audio.export-quality.idle', 'Show checks after the export finishes. Earlier successful deliveries remain available after a failed attempt.');
}
function renderExportQualityReport(report, rejected) {
    const card = document.createElement('div'); card.className = 'border rounded p-2 mb-2 text-break';
    const add = text => { const line = document.createElement('p'); line.className = 'mb-1'; line.textContent = text; card.appendChild(line); };
    const name = report.file || report.member || report.task;
    const kind = rejected ? t('audio.export-quality.kind.rejected', 'Rejected attempt') : t('audio.export-quality.kind.completed', 'Completed check');
    add(t('audio.export-quality.card', '{kind}: {name} — {status}.', {kind, name, status: report.status}));
    if (report.reason) { add(t('audio.export-quality.stage', 'Stage: {stage}. {reason}', {stage: report.stage || 'export', reason: report.reason})); }
    if (Number.isFinite(report.seconds) && report.seconds >= 0) {
        add(t('audio.export-quality.validation-time', 'Validation time: {seconds} seconds.', {seconds: report.seconds.toFixed(3)}));
    }
    if (report.verification_scope === 'audacity_structure_and_frames') {
        add(t('audio.export-quality.audacity', 'Audacity archive: {tracks} tracks, {members} members checked for structure and PCM frames. Inactive-speaker silence is intentional.', {tracks: report.tracks_checked, members: report.members_checked}));
        for (const track of report.track_summaries || []) {
            add(t('audio.export-quality.track', '{file}: {frames} frames at {rate} Hz, {channels} channel(s).', {file: track.file, frames: track.frames, rate: track.sample_rate, channels: track.channels}));
        }
        if (report.track_summaries_abbreviated) { add(t('audio.export-quality.tracks-abbreviated', 'Track details are abbreviated. All planned tracks were checked.')); }
    } else if (report.expected_frames !== undefined) {
        const decoded = report.decoded_frames ?? t('audio.export-quality.unavailable-word', 'unavailable');
        add(t('audio.export-quality.format', '{format}: expected {expected} frames; decoded {decoded} at {rate} Hz, {channels} channel(s).', {format: report.expected_format, expected: report.expected_frames, decoded, rate: report.sample_rate, channels: report.channels}));
        if (report.chapters_checked !== undefined) { add(t('audio.export-quality.chapters', '{count} chapter markers checked.', {count: report.chapters_checked})); }
        if (report.tags_checked !== undefined) { add(t('audio.export-quality.tags', '{count} book metadata fields checked.', {count: report.tags_checked})); }
        if (report.duration_delta_seconds !== undefined) {
            add(t('audio.export-quality.duration', 'Duration difference: {difference} seconds; codec allowance {allowance} seconds.', {difference: report.duration_delta_seconds.toFixed(6), allowance: report.duration_tolerance_seconds.toFixed(6)}));
        }
    }
    if (report.unexpected_silence_count !== undefined) {
        const policy = report.options.reject_unexpected_silence ? t('audio.export-quality.policy.reject', 'Rejection') : t('audio.export-quality.policy.advisory', 'Advisory');
        add(t('audio.export-quality.silence', '{count} unexpected long silent interval(s); authored pauses excluded. Threshold {threshold} dBFS, gap {gap} seconds. {policy} policy.', {count: report.unexpected_silence_count, threshold: report.options.silence_dbfs, gap: report.options.max_unexpected_silence_seconds, policy}));
        for (const range of report.unexpected_silence_ranges || []) { add(t('audio.export-quality.silence-range', 'Silence: {start}–{end} seconds.', {start: range.start_seconds.toFixed(3), end: range.end_seconds.toFixed(3)})); }
        if (report.longest_unexpected_silence) {
            const range = report.longest_unexpected_silence;
            add(t('audio.export-quality.longest', 'Longest interval: {start}–{end} seconds.', {start: range.start_seconds.toFixed(3), end: range.end_seconds.toFixed(3)}));
        }
        if (report.diagnostics_abbreviated) { add(t('audio.export-quality.intervals-abbreviated', 'Interval details are abbreviated; the count includes every detected interval.')); }
    }
    for (const item of report.diagnostics || []) {
        if (item.kind === 'chapter') { add(t('audio.export-quality.chapter', 'Chapter {chapter}: expected {expected_start}–{expected_end} seconds; found {actual_start}–{actual_end} seconds.', {chapter: item.chapter + 1, expected_start: item.expected_start_seconds, expected_end: item.expected_end_seconds, actual_start: item.actual_start_seconds, actual_end: item.actual_end_seconds})); }
    }
    if (!rejected && report.sha256) { add(t('audio.export-quality.sha', 'SHA-256 at completed check: {sha}', {sha: report.sha256})); }
    return card;
}
async function loadExportQualityReports() {
    const epoch = ++exportQualityEpoch;
    const book = chunkSnapshotBookToken, selectedBook = currentBookFilename;
    const current = () => epoch === exportQualityEpoch && book === chunkSnapshotBookToken
        && selectedBook === currentBookFilename && chunkSnapshotBook === currentBookFilename;
    document.getElementById('export-quality-reports').replaceChildren();
    if (!book || chunkSnapshotBook !== selectedBook) {
        document.getElementById('export-quality-status').textContent = t('audio.export-quality.refresh', 'Refresh Editor for the current book, then show its export checks.');
        return;
    }
    try {
        const result = await API.get(`/api/export_quality?expected_book_token=${encodeURIComponent(book)}`);
        if (!current() || result.book_token !== book) { return; }
        const container = document.getElementById('export-quality-reports');
        for (const report of result.reports) { container.appendChild(renderExportQualityReport(report, false)); }
        for (const report of result.rejected_attempts) { container.appendChild(renderExportQualityReport(report, true)); }
        let text = t('audio.export-quality.summary', '{completed} completed check(s), {rejected} rejected attempt(s) retained for this book in this app session. File metadata is unchanged for the displayed deliveries; this view does not rehash or decode them. Checks do not certify pronunciation.', {completed: result.reports.length, rejected: result.rejected_attempts.length});
        if (result.unavailable) { text += ' ' + t('audio.export-quality.omitted', '{count} earlier receipt(s) omitted because files changed or are unavailable.', {count: result.unavailable}); }
        for (const attempt of result.attempts) {
            text += ' ' + t('audio.export-quality.attempt', '{task}: {status}, {delivered} delivered file(s) checked in its latest attempt.', {task: attempt.task, status: attempt.status, delivered: attempt.completed_files});
        }
        text += ' ' + t('audio.export-quality.history', 'History keeps at most {deliveries} deliveries and {rejections} rejected attempts per export type; older entries may be omitted.', {deliveries: result.reports_per_task_limit, rejections: result.rejections_per_task_limit});
        document.getElementById('export-quality-status').textContent = text;
    } catch (error) {
        if (current()) {
            document.getElementById('export-quality-status').textContent = t('audio.export-quality.unavailable-status', 'Checks unavailable. Refresh Editor and wait for export completion.');
            showActionError(t('audio.export-quality.error.title', 'Could not show export checks'), error, t('audio.export-quality.error.recovery', 'Refresh Editor for the current book and retry.'), 'error');
        }
    }
}
