/* Explicit, read-only requests; no model initialization or task dispatch. */
let ttsDiagnosticsEpoch = 0;
let ttsDiagnosticsReport = null;
async function refreshTtsDiagnostics() {
    const epoch = ++ttsDiagnosticsEpoch;
    const status = document.getElementById('tts-diagnostics-status');
    const rows = document.getElementById('tts-diagnostics-records');
    const download = document.getElementById('tts-diagnostics-download');
    const preview = document.getElementById('tts-diagnostics-preview');
    ttsDiagnosticsReport = null;
    download.disabled = true;
    rows.replaceChildren();
    preview.textContent = '';
    status.textContent = 'Reading recorded timings…';
    try {
        const report = await API.get('/api/tts/diagnostics');
        if (epoch !== ttsDiagnosticsEpoch) { return; }
        const records = report?.sections?.tts?.records;
        if (!Array.isArray(records)) { throw new Error('Timing records unavailable.'); }
        status.textContent = records.length ? `${records.length} recent operation(s). Stage times overlap; unavailable means unmeasured.` : 'No TTS operations recorded in this app process yet.';
        const residency = report?.sections?.tts?.residency;
        const collection = report?.sections?.tts?.collection;
        const unavailable = Object.entries(collection || {}).filter(([_name, state]) => state === 'unavailable').map(([name]) => name);
        if (unavailable.length) {
            const row = document.createElement('p');
            row.textContent = 'Report incomplete: unavailable collectors: ' + unavailable.join(', ') + '. Recorded operations are retained; refresh does not rerun them.';
            rows.appendChild(row);
        }
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
            row.textContent = device.state === 'observed' ? (device.gpu_backend ? `Torch GPU available: ${device.gpu_backend}. Memory capacity is not measured here.` : 'No supported Torch GPU is available in the observed runtime.') : (device.state === 'unavailable' ? 'Torch device probe failed; GPU availability is unknown.' : 'Torch device availability is unobserved. This report does not initialize the runtime.');
            rows.appendChild(row);
        }
        for (const record of records.slice(-10).reverse()) {
            const row = document.createElement('p');
            const stages = Object.entries(record.stages_seconds || {}).map(([name, value]) => `${name.replaceAll('_', ' ')}: ${value === null ? 'unavailable' : `${value}s`}`).join(' · ');
            row.textContent = `${record.operation} · ${record.backend} · ${record.outcome} · total ${record.elapsed_seconds}s · audio ${record.audio_seconds === null ? 'unavailable' : `${record.audio_seconds}s`}. ${stages}`;
            rows.appendChild(row);
        }
        const serialized = JSON.stringify(report, null, 2);
        preview.textContent = serialized;
        ttsDiagnosticsReport = serialized;
        download.disabled = false;
    } catch (_error) {
        if (epoch !== ttsDiagnosticsEpoch) { return; }
        ttsDiagnosticsReport = null;
        download.disabled = true;
        preview.textContent = '';
        const httpStatus = Number.isInteger(_error?.status) && _error.status >= 400 && _error.status <= 599 ? 'HTTP ' + _error.status : 'network or report-format error';
        status.textContent = 'Could not read report (' + httpStatus + '). Download cleared. Check server logs privately before trying again.';
    }
}
function downloadTtsDiagnostics() {
    if (!ttsDiagnosticsReport) { return; }
    const blob = new Blob([ttsDiagnosticsReport], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'tts-support-report.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}
