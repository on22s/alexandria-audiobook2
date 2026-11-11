/* Reports are explicit reads, tied to the current book and verified output bytes. */
let exportMasteringEpoch = 0;
function getExportMasteringOptions() {
    if (!document.getElementById('export-mastering-enabled').checked) { return null; }
    const loudness = Number(document.getElementById('export-mastering-loudness').value);
    const true_peak = Number(document.getElementById('export-mastering-peak').value);
    if (!Number.isFinite(loudness) || loudness < -24 || loudness > -14 || !Number.isFinite(true_peak) || true_peak < -9 || true_peak > -1) {
        throw new Error(t('audio.mastering.threshold-error', 'Mastering needs a loudness target from -24 to -14 LUFS and a true peak from -9 to -1 dBTP.'));
    }
    return {loudness, true_peak};
}
function resetExportMasteringView() {
    exportMasteringEpoch++;
    document.getElementById('export-mastering-reports').replaceChildren();
    document.getElementById('export-mastering-status').textContent = t('audio.mastering.cleared', 'Measurements cleared. Export explicitly, then verify results for this book.');
}
async function loadExportMasteringReports() {
    const epoch = ++exportMasteringEpoch;
    const book = chunkSnapshotBookToken;
    try {
        if (!book || chunkSnapshotBook !== currentBookFilename) { throw new Error(t('audio.mastering.refresh-first', 'Refresh Editor for the current book first.')); }
        document.getElementById('export-mastering-reports').replaceChildren();
        const result = await API.get(`/api/export_mastering?expected_book_token=${encodeURIComponent(book)}`);
        if (epoch !== exportMasteringEpoch || book !== chunkSnapshotBookToken || result.book_token !== book) { return; }
        for (const report of result.reports) {
            const card = document.createElement('p'); card.className = 'text-break';
            const show = metrics => t('audio.mastering.metrics', '{lufs} LUFS · {peak} dBTP · {range} LU range', {lufs: metrics.integrated_lufs, peak: metrics.true_peak_dbtp, range: metrics.range_lu});
            card.textContent = t('audio.mastering.card', '{file}: before {before}; after {after}. {mode} processing. Targets: {loudness} LUFS, {peak} dBTP. SHA-256 {sha}.', {file: report.file, before: show(report.before), after: show(report.after), mode: report.mode, loudness: report.settings.loudness, peak: report.settings.true_peak, sha: report.sha256});
            document.getElementById('export-mastering-reports').appendChild(card);
        }
        const status = document.getElementById('export-mastering-status');
        status.textContent = result.reports.length
            ? t('audio.mastering.verified', '{count} verified export measurement(s). These are measured results, not a distribution compliance certificate.', {count: result.reports.length})
            : t('audio.mastering.none', 'No verified measurements for this book. Export with mastering enabled, wait for completion and retry. Reports are retained for this app session.');
        if (result.unavailable) { status.textContent = joinUiSentences([status.textContent, t('audio.mastering.omitted', '{count} report(s) omitted because output files changed or are unavailable.', {count: result.unavailable})]); }
    } catch (error) {
        if (epoch === exportMasteringEpoch) {
            document.getElementById('export-mastering-status').textContent = t('audio.mastering.unavailable', 'Measurements unavailable. Existing audio has not been changed.');
            showActionError(t('audio.mastering.error.title', 'Could not verify export measurements'), error, t('audio.mastering.error.recovery', 'Refresh Editor and check that the export completed.'), 'error');
        }
    }
}
