/* Reports are explicit reads, tied to the current book and verified output bytes. */
let exportMasteringEpoch = 0;
function getExportMasteringOptions() {
    if (!document.getElementById('export-mastering-enabled').checked) { return null; }
    const loudness = Number(document.getElementById('export-mastering-loudness').value);
    const true_peak = Number(document.getElementById('export-mastering-peak').value);
    if (!Number.isFinite(loudness) || loudness < -24 || loudness > -14 || !Number.isFinite(true_peak) || true_peak < -9 || true_peak > -1) {
        throw new Error('Mastering needs a loudness target from -24 to -14 LUFS and a true peak from -9 to -1 dBTP.');
    }
    return {loudness, true_peak};
}
function resetExportMasteringView() {
    exportMasteringEpoch++;
    document.getElementById('export-mastering-reports').replaceChildren();
    document.getElementById('export-mastering-status').textContent = 'Measurements cleared. Export explicitly, then verify results for this book.';
}
async function loadExportMasteringReports() {
    const epoch = ++exportMasteringEpoch;
    const book = chunkSnapshotBookToken;
    try {
        if (!book || chunkSnapshotBook !== currentBookFilename) { throw new Error('Refresh Editor for the current book first.'); }
        document.getElementById('export-mastering-reports').replaceChildren();
        const result = await API.get(`/api/export_mastering?expected_book_token=${encodeURIComponent(book)}`);
        if (epoch !== exportMasteringEpoch || book !== chunkSnapshotBookToken || result.book_token !== book) { return; }
        for (const report of result.reports) {
            const card = document.createElement('p'); card.className = 'text-break';
            const show = metrics => `${metrics.integrated_lufs} LUFS · ${metrics.true_peak_dbtp} dBTP · ${metrics.range_lu} LU range`;
            card.textContent = `${report.file}: before ${show(report.before)}; after ${show(report.after)}. ${report.mode} processing. Targets: ${report.settings.loudness} LUFS, ${report.settings.true_peak} dBTP. SHA-256 ${report.sha256}.`;
            document.getElementById('export-mastering-reports').appendChild(card);
        }
        document.getElementById('export-mastering-status').textContent = result.reports.length
            ? `${result.reports.length} verified export measurement(s). These are measured results, not a distribution compliance certificate.`
            : 'No verified measurements for this book. Export with mastering enabled, wait for completion and retry. Reports are retained for this app session.';
        if (result.unavailable) { document.getElementById('export-mastering-status').textContent += ` ${result.unavailable} report(s) omitted because output files changed or are unavailable.`; }
    } catch (error) {
        if (epoch === exportMasteringEpoch) {
            document.getElementById('export-mastering-status').textContent = 'Measurements unavailable. Existing audio has not been changed.';
            showActionError('Could not verify export measurements', error, 'Refresh Editor and check that the export completed.', 'error');
        }
    }
}
