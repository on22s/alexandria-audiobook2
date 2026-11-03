const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
const root = process.argv[2], cataloguePath = process.argv[3];
const fixture = fs.readFileSync(path.join(root, 'app/tests/casting_ui_fixture.js'), 'utf8');
const boundary = fixture.indexOf('(async () => {');
assert(boundary > 0);
const {setup} = new Function('require', 'process', fixture.slice(0, boundary) + '\nreturn {setup};')(require, process);
const catalogue = JSON.parse(fs.readFileSync(cataloguePath, 'utf8'));
(async () => {
    const env = setup({voices: catalogue.voices, skipped: catalogue.skipped});
    const started = performance.now();
    await env.w.loadCastingLibrary();
    const renderedMs = performance.now() - started;
    assert.equal(env.errors.length, 0);
    assert.equal(env.get('results').querySelectorAll('strong').length, Math.min(48, catalogue.voices.length));
    const library = env.w.CastingLibrary;
    const metricsStart = performance.now();
    const health = library.getHealthCounts(catalogue.voices, catalogue.skipped);
    const gender = library.getCoverage(catalogue.voices, 'gender');
    library.getCoverage(catalogue.voices, 'age');
    assert.equal(gender.rows.reduce((sum, row) => sum + row.counts.reduce((total, count) => total + count, 0), 0), catalogue.voices.length);
    console.log(JSON.stringify({voices: catalogue.voices.length, books: gender.rows.length, health,
        fake_dom_load_render_ms: renderedMs, pure_metrics_ms: performance.now() - metricsStart,
        limitation: 'dependency-free fake DOM, not a browser or audio similarity benchmark'}));
})().catch(error => { console.error(error); process.exit(1); });
