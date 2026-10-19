/* Explicit, read-only requests; no model initialization or task dispatch. */
let ttsDiagnosticsEpoch = 0;
let ttsDiagnosticsReport = null;
async function refreshTtsDiagnostics() {
    const epoch = ++ttsDiagnosticsEpoch;
    const status = document.getElementById('tts-diagnostics-status');
    const rows = document.getElementById('tts-diagnostics-records');
    const download = document.getElementById('tts-diagnostics-download');
    ttsDiagnosticsReport = null;
    download.disabled = true;
    rows.replaceChildren();
    status.textContent = 'Reading recorded timings…';
    try {
        const report = await API.get('/api/tts/diagnostics');
        if (epoch !== ttsDiagnosticsEpoch) { return; }
        const records = report?.sections?.tts?.records;
        if (!Array.isArray(records)) { throw new Error('Timing records unavailable.'); }
        ttsDiagnosticsReport = report;
        download.disabled = false;
        status.textContent = records.length ? `${records.length} recent operation(s). Stage times overlap; unavailable means unmeasured.` : 'No TTS operations recorded in this app process yet.';
        for (const record of records.slice(-10).reverse()) {
            const row = document.createElement('p');
            const stages = Object.entries(record.stages_seconds || {}).map(([name, value]) => `${name.replaceAll('_', ' ')}: ${value === null ? 'unavailable' : `${value}s`}`).join(' · ');
            row.textContent = `${record.operation} · ${record.backend} · ${record.outcome} · total ${record.elapsed_seconds}s · audio ${record.audio_seconds === null ? 'unavailable' : `${record.audio_seconds}s`}. ${stages}`;
            rows.appendChild(row);
        }
    } catch (_error) {
        if (epoch !== ttsDiagnosticsEpoch) { return; }
        status.textContent = 'Could not read timings. Refresh to try again.';
    }
}
function downloadTtsDiagnostics() {
    if (!ttsDiagnosticsReport) { return; }
    const blob = new Blob([JSON.stringify(ttsDiagnosticsReport, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'tts-support-report.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}
