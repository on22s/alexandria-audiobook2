/* Completed checks and rejected attempts remain separate, scoped to this book. */
let exportQualityEpoch = 0;
function getExportQualityOptions() {
    const silence_dbfs = Number(document.getElementById('export-quality-noise').value);
    const max_unexpected_silence_seconds = Number(document.getElementById('export-quality-gap').value);
    if (!Number.isFinite(silence_dbfs) || silence_dbfs < -90 || silence_dbfs > -20
        || !Number.isFinite(max_unexpected_silence_seconds) || max_unexpected_silence_seconds < .1 || max_unexpected_silence_seconds > 3600) {
        throw new Error('Silence checks need a threshold from -90 to -20 dBFS and a gap from 0.1 to 3600 seconds.');
    }
    return {silence_dbfs, max_unexpected_silence_seconds,
        reject_unexpected_silence: document.getElementById('export-quality-reject').checked};
}
function resetExportQualityView() {
    exportQualityEpoch++;
    document.getElementById('export-quality-reports').replaceChildren();
    document.getElementById('export-quality-status').textContent = 'Show checks after the export finishes. Earlier successful deliveries remain available after a failed attempt.';
}
function renderExportQualityReport(report, rejected) {
    const card = document.createElement('div'); card.className = 'border rounded p-2 mb-2 text-break';
    const add = text => { const line = document.createElement('p'); line.className = 'mb-1'; line.textContent = text; card.appendChild(line); };
    const name = report.file || report.member || report.task;
    add(`${rejected ? 'Rejected attempt' : 'Completed check'}: ${name} — ${report.status}.`);
    if (report.reason) { add(`Stage: ${report.stage || 'export'}. ${report.reason}`); }
    if (report.verification_scope === 'audacity_structure_and_frames') {
        add(`Audacity archive: ${report.tracks_checked} tracks, ${report.members_checked} members checked for structure and PCM frames. Inactive-speaker silence is intentional.`);
        for (const track of report.track_summaries || []) {
            add(`${track.file}: ${track.frames} frames at ${track.sample_rate} Hz, ${track.channels} channel(s).`);
        }
        if (report.track_summaries_abbreviated) { add('Track details are abbreviated. All planned tracks were checked.'); }
    } else if (report.expected_frames !== undefined) {
        add(`${report.expected_format}: expected ${report.expected_frames} frames; decoded ${report.decoded_frames ?? 'unavailable'} at ${report.sample_rate} Hz, ${report.channels} channel(s).`);
        if (report.chapters_checked !== undefined) { add(`${report.chapters_checked} chapter markers checked.`); }
        if (report.tags_checked !== undefined) { add(`${report.tags_checked} book metadata fields checked.`); }
        if (report.duration_delta_seconds !== undefined) {
            add(`Duration difference: ${report.duration_delta_seconds.toFixed(6)} seconds; codec allowance ${report.duration_tolerance_seconds.toFixed(6)} seconds.`);
        }
    }
    if (report.unexpected_silence_count !== undefined) {
        add(`${report.unexpected_silence_count} unexpected long silent interval(s); authored pauses excluded. Threshold ${report.options.silence_dbfs} dBFS, gap ${report.options.max_unexpected_silence_seconds} seconds. ${report.options.reject_unexpected_silence ? 'Rejection' : 'Advisory'} policy.`);
        for (const range of report.unexpected_silence_ranges || []) { add(`Silence: ${range.start_seconds.toFixed(3)}–${range.end_seconds.toFixed(3)} seconds.`); }
        if (report.longest_unexpected_silence) {
            const range = report.longest_unexpected_silence;
            add(`Longest interval: ${range.start_seconds.toFixed(3)}–${range.end_seconds.toFixed(3)} seconds.`);
        }
        if (report.diagnostics_abbreviated) { add('Interval details are abbreviated; the count includes every detected interval.'); }
    }
    for (const item of report.diagnostics || []) {
        if (item.kind === 'chapter') { add(`Chapter ${item.chapter + 1}: expected ${item.expected_start_seconds}–${item.expected_end_seconds} seconds; found ${item.actual_start_seconds}–${item.actual_end_seconds} seconds.`); }
    }
    if (!rejected && report.sha256) { add(`SHA-256 at completed check: ${report.sha256}`); }
    return card;
}
async function loadExportQualityReports() {
    const epoch = ++exportQualityEpoch;
    const book = chunkSnapshotBookToken, selectedBook = currentBookFilename;
    const current = () => epoch === exportQualityEpoch && book === chunkSnapshotBookToken
        && selectedBook === currentBookFilename && chunkSnapshotBook === currentBookFilename;
    document.getElementById('export-quality-reports').replaceChildren();
    if (!book || chunkSnapshotBook !== selectedBook) {
        document.getElementById('export-quality-status').textContent = 'Refresh Editor for the current book, then show its export checks.';
        return;
    }
    try {
        const result = await API.get(`/api/export_quality?expected_book_token=${encodeURIComponent(book)}`);
        if (!current() || result.book_token !== book) { return; }
        const container = document.getElementById('export-quality-reports');
        for (const report of result.reports) { container.appendChild(renderExportQualityReport(report, false)); }
        for (const report of result.rejected_attempts) { container.appendChild(renderExportQualityReport(report, true)); }
        let text = `${result.reports.length} completed check(s), ${result.rejected_attempts.length} rejected attempt(s) retained for this book in this app session. File metadata is unchanged for the displayed deliveries; this view does not rehash or decode them. Checks do not certify pronunciation.`;
        if (result.unavailable) { text += ` ${result.unavailable} earlier receipt(s) omitted because files changed or are unavailable.`; }
        for (const attempt of result.attempts) {
            text += ` ${attempt.task}: ${attempt.status}, ${attempt.completed_files} delivered file(s) checked in its latest attempt.`;
        }
        text += ` History keeps at most ${result.reports_per_task_limit} deliveries and ${result.rejections_per_task_limit} rejected attempts per export type; older entries may be omitted.`;
        document.getElementById('export-quality-status').textContent = text;
    } catch (error) {
        if (current()) {
            document.getElementById('export-quality-status').textContent = 'Checks unavailable. Refresh Editor and wait for export completion.';
            showActionError('Could not show export checks', error, 'Refresh Editor for the current book and retry.', 'error');
        }
    }
}
