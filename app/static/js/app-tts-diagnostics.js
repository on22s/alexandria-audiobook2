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
    status.textContent = t('tts_diag.reading', 'Reading recorded timings…');
    try {
        const report = await API.get('/api/tts/diagnostics');
        if (epoch !== ttsDiagnosticsEpoch) { return; }
        const records = report?.sections?.tts?.records;
        if (!Array.isArray(records)) { throw new Error('Timing records unavailable.'); }
        status.textContent = records.length
            ? t('tts_diag.recent', '{count} recent operation(s). Stage times overlap; unavailable means unmeasured.', {count: records.length})
            : t('tts_diag.none', 'No TTS operations recorded in this app process yet.');
        const residency = report?.sections?.tts?.residency;
        const collection = report?.sections?.tts?.collection;
        const unavailable = Object.entries(collection || {}).filter(([_name, state]) => state === 'unavailable').map(([name]) => name);
        if (unavailable.length) {
            const row = document.createElement('p');
            row.textContent = t('tts_diag.incomplete', 'Report incomplete: unavailable collectors: {collectors}. Recorded operations are retained; refresh does not rerun them.', {collectors: unavailable.join(', ')});
            rows.appendChild(row);
        }
        if (residency) {
            const row = document.createElement('p');
            if (residency.state === 'busy') {
                row.textContent = t('tts_diag.residency.busy', 'Local model details are busy. Refresh when the current operation finishes.');
            } else if (residency.state === 'not_initialized') {
                row.textContent = t('tts_diag.residency.not-initialized', 'The speech engine has not been initialized. Reading this report does not load it.');
            } else if (Array.isArray(residency.residents)) {
                const models = residency.residents.map(model => t('tts_diag.model',
                    '{family} ({device}; {status})',
                    {family: model.family,
                     device: model.device || t('tts_diag.device.unknown', 'device unknown'),
                     status: model.load_capture
                         ? t('tts_diag.model.status-recorded', 'identity recorded at load, current state not checked')
                         : t('tts_diag.model.status-unavailable', 'load identity unavailable')}));
                row.textContent = models.length
                    ? t('tts_diag.models.loaded', 'Loaded local models: {models}', {models: models.join(' · ')})
                    : t('tts_diag.models.none', 'No local speech models are loaded in this engine.');
            } else {
                row.textContent = t('tts_diag.residency.unavailable', 'Local model state is unavailable.');
            }
            rows.appendChild(row);
            if (residency.cleanup_pending) {
                const pending = document.createElement('p');
                pending.textContent = t('tts_diag.cleanup-pending', 'Model memory cleanup is incomplete. Another GPU task must wait for cleanup to succeed.');
                rows.appendChild(pending);
            }
        }
        const assets = report?.sections?.tts?.assets;
        if (Array.isArray(assets)) {
            const labels = {
                required_assets_observed: t('tts_diag.asset.present', 'required files present; contents not verified'),
                not_cached: t('tts_diag.asset.not-cached', 'not found in local cache'),
                unavailable: t('tts_diag.asset.unavailable', 'cache inspection unavailable'),
            };
            for (const asset of assets) {
                const row = document.createElement('p');
                row.textContent = t('tts_diag.asset', '{model}: {state}. Base weights only; adapters are separate.', {
                    model: asset.model_id,
                    state: labels[asset.state] || t('tts_diag.asset.unknown', 'cache status unknown'),
                });
                rows.appendChild(row);
            }
        }
        const device = report?.sections?.tts?.device;
        if (device) {
            const row = document.createElement('p');
            row.textContent = device.state === 'observed'
                ? (device.gpu_backend
                    ? t('tts_diag.device.available', 'Torch GPU available: {backend}. Memory capacity is not measured here.', {backend: device.gpu_backend})
                    : t('tts_diag.device.none', 'No supported Torch GPU is available in the observed runtime.'))
                : (device.state === 'unavailable'
                    ? t('tts_diag.device.probe-failed', 'Torch device probe failed; GPU availability is unknown.')
                    : t('tts_diag.device.unobserved', 'Torch device availability is unobserved. This report does not initialize the runtime.'));
            rows.appendChild(row);
        }
        for (const record of records.slice(-10).reverse()) {
            const row = document.createElement('p');
            const stages = Object.entries(record.stages_seconds || {}).map(([name, value]) => `${name.replaceAll('_', ' ')}: ${value === null ? t('tts_diag.unavailable', 'unavailable') : `${value}s`}`).join(' · ');
            row.textContent = t('tts_diag.record', '{operation} · {backend} · {outcome} · total {elapsed}s · audio {audio}. {stages}', {
                operation: record.operation,
                backend: record.backend,
                outcome: record.outcome,
                elapsed: record.elapsed_seconds,
                audio: record.audio_seconds === null ? t('tts_diag.unavailable', 'unavailable') : `${record.audio_seconds}s`,
                stages,
            });
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
        const httpStatus = Number.isInteger(_error?.status) && _error.status >= 400 && _error.status <= 599
            ? 'HTTP ' + _error.status
            : t('tts_diag.error.network', 'network or report-format error');
        status.textContent = t('tts_diag.read-failed', 'Could not read report ({status}). Download cleared. Check server logs privately before trying again.', {status: httpStatus});
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
