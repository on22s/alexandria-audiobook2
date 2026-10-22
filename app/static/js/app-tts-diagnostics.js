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
        const residency = report?.sections?.tts?.residency;
        if (residency) {
            const row = document.createElement('p');
            if (residency.state === 'busy') {
                row.textContent = 'Local model details are busy. Refresh when the current operation finishes.';
            } else if (residency.state === 'not_initialized') {
                row.textContent = 'The speech engine has not been initialized. Reading this report does not load it.';
            } else if (Array.isArray(residency.residents)) {
                const models = residency.residents.map(model => `${model.family} (${model.device || 'device unknown'}; ${model.load_capture ? 'identity recorded at load, current state not checked' : 'load identity unavailable'})`);
                row.textContent = models.length ? `Loaded local models: ${models.join(' · ')}` : 'No local speech models are loaded in this engine.';
            } else {
                row.textContent = 'Local model state is unavailable.';
            }
            rows.appendChild(row);
            if (residency.cleanup_pending) {
                const pending = document.createElement('p');
                pending.textContent = 'Model memory cleanup is incomplete. Another GPU task must wait for cleanup to succeed.';
                rows.appendChild(pending);
            }
        }
        const assets = report?.sections?.tts?.assets;
        if (Array.isArray(assets)) {
            const labels = {required_assets_observed: 'required files present; contents not verified', not_cached: 'not found in local cache', unavailable: 'cache inspection unavailable'};
            for (const asset of assets) {
                const row = document.createElement('p');
                row.textContent = `${asset.model_id}: ${labels[asset.state] || 'cache status unknown'}. Base weights only; adapters are separate.`;
                rows.appendChild(row);
            }
        }
        const device = report?.sections?.tts?.device;
        if (device) {
            const row = document.createElement('p');
            row.textContent = device.state === 'observed' ? (device.gpu_backend ? `Torch GPU available: ${device.gpu_backend}. Memory capacity is not measured here.` : 'No supported Torch GPU is available in the observed runtime.') : 'Torch device availability is unobserved. This report does not initialize the runtime.';
            rows.appendChild(row);
        }
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
