// Audit the populated casting-review modal with synthetic voice data.
// Seeds /api/casting/voices via request interception, then clicks the real
// Review button and walks prev/next/status actions, running axe each time.
// Status saves are intercepted with a success response so the actions are real,
// and each action is asserted to change the visible review state.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
const AXE = path.join(__dirname, 'node_modules/axe-core/axe.min.js');
const THEMES = ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek'];

const tags = (gender, ages, tones, textures = []) => ({gender, ages, tones, textures});
const VOICES = [
  {id: 'persona/book/abel', source: 'persona', book_id: 'BOOK', name: 'ABEL',
   description: 'Young male; calm, academic.', ref_text: 'Hello.',
   preview_url: null, audio_present: false,
   tags: tags('Male', ['Young Adult'], ['calm', 'academic'])},
  {id: 'persona/book/aichel', source: 'persona', book_id: 'BOOK', name: 'AICHEL',
   description: 'Female; warm, polite.', ref_text: 'A pleasure.',
   preview_url: null, audio_present: false,
   tags: tags('Female', ['Adult'], ['warm', 'polite'], ['smooth'])},
  {id: 'persona/book/corin', source: 'persona', book_id: 'BOOK', name: 'CORIN',
   description: 'Older male; grave, deliberate.', ref_text: 'Indeed.',
   preview_url: null, audio_present: false,
   tags: tags('Male', ['Older / Elderly'], ['grave', 'deliberate'])},
];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: process.env.AA2_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, args: ['--no-sandbox', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200 });
  await page.setRequestInterception(true);
  page.on('request', req => {
    const url = req.url();
    if (url.includes('/api/casting/voices')) {
      return req.respond({status: 200, contentType: 'application/json',
        body: JSON.stringify({voices: VOICES, marks: {}, skipped: []})});
    }
    if (url.includes('/api/casting/marks/batch')) {
      // A success response so status actions are not silent no-ops: echo a mark
      // per change with the requested status so renderReview() reflects it.
      let changes = [], action = 'Set status';
      try { const body = JSON.parse(req.postData() || '{}'); changes = body.changes || []; action = body.action || action; } catch (e) {}
      const marks = {};
      for (const ch of changes) { marks[ch.voice_id] = {...(ch.expected || {}), ...(ch.patch || {})}; }
      return req.respond({status: 200, contentType: 'application/json',
        body: JSON.stringify({marks, activity: changes.length ?
          [{at: new Date().toISOString(), action, count: changes.length}] : []})});
    }
    if (url.includes('/api/voice_config/snapshot')) {
      return req.respond({status: 200, contentType: 'application/json',
        body: JSON.stringify({book_token: 'a'.repeat(64), voices: [{name: 'ADAM'}, {name: 'EVE'}]})});
    }
    if (url.includes('/api/voice_library')) {
      return req.respond({status: 200, contentType: 'application/json',
        body: JSON.stringify({casts: [], shared: []})});
    }
    return req.continue();
  });
  await page.goto(process.env.AA2_URL || 'http://127.0.0.1:8765/', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.addScriptTag({ path: AXE });
  const settle = () => page.evaluate(() => new Promise(resolve => {
    if (typeof CSSTransition !== 'undefined') {
      document.getAnimations().forEach(a => {
        const p = a.transitionProperty;
        if (a instanceof CSSTransition && (p === 'color' || p.endsWith('-color') || p === 'box-shadow')) a.finish();
      });
    }
    setTimeout(resolve, 350);
  }));
  const audit = () => page.evaluate(async () => {
    const root = document.getElementById('casting-review-modal');
    const res = await window.axe.run(root, { runOnly: { type: 'rule', values: ['color-contrast'] } });
    const nodes = res.passes.reduce((n, p) => n + p.nodes.length, 0)
      + res.violations.reduce((n, v) => n + v.nodes.length, 0)
      + res.incomplete.reduce((n, i) => n + i.nodes.length, 0);
    return { nodes, violations: res.violations.flatMap(v => v.nodes.map(n => ({
      target: n.target, summary: (n.failureSummary || '').replace(/\s+/g, ' ').slice(0, 170),
    }))) };
  });
  const state = () => page.evaluate(() => ({
    shown: document.getElementById('casting-review-modal')?.classList.contains('show') === true,
    bodyText: (document.getElementById('casting-review-body')?.textContent || '').trim().length,
    position: (document.getElementById('casting-review-position')?.textContent || '').trim(),
    reviewed: document.getElementById('casting-review-progress')?.getAttribute('aria-valuenow'),
    head: (document.querySelector('#casting-review-body > .mb-2')?.textContent || '').trim().replace(/\s+/g, ' '),
  }));
  const signature = s => `${s.position}|${s.reviewed}|${s.head}`;
  const results = [];
  const statusAssertions = [];

  for (const theme of THEMES) {
    await page.evaluate(t => {
      const root = document.documentElement;
      if (t === 'light') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', t);
    }, theme);
    await settle();
    // Real navigation to the casting tab triggers loadCastingLibrary().
    await page.evaluate(() => document.querySelector('[data-tab="casting"]')?.click());
    await page.waitForFunction(() => document.querySelectorAll('#casting-results .card').length > 0,
      { timeout: 10000 });
    await settle();
    const cards = await page.evaluate(() => document.querySelectorAll('#casting-results .card').length);
    if (cards !== VOICES.length) { throw new Error(`${theme}: expected ${VOICES.length} seeded cards, saw ${cards}`); }

    // Real click on Review.
    await page.evaluate(() => document.getElementById('casting-review')?.click());
    await settle();
    let st = await state();
    if (!st.shown) { throw new Error(`${theme}: Review click did not show the modal`); }
    let r = await audit();
    results.push({ theme, step: 'open', cards, ...st, ...r });

    if (st.shown) {
      for (const step of ['next', 'prev', 'shortlist', 'callback', 'reject']) {
        const before = await state();
        if (step === 'shortlist' || step === 'callback' || step === 'reject') {
          await page.evaluate(s => document.querySelector(`#casting-review-modal [data-casting-status="${s}"]`)?.click(), step);
        } else {
          const sel = step === 'next' ? '#casting-review-next' : '#casting-review-prev';
          await page.evaluate(s => document.querySelector(s)?.click(), sel);
        }
        await settle();
        const after = await state();
        r = await audit();
        results.push({ theme, step, cards, ...after, ...r });
        if (step === 'shortlist' || step === 'callback' || step === 'reject') {
          statusAssertions.push({ theme, step,
            before: signature(before), after: signature(after),
            effective: signature(before) !== signature(after) });
        }
      }
      await page.evaluate(() => document.querySelector('#casting-review-modal [data-bs-dismiss="modal"]')?.click());
      await settle();
    }
  }
  await browser.close();
  const bad = results.filter(r => r.violations && r.violations.length);
  const ineffective = statusAssertions.filter(a => !a.effective);
  const distinctPerTheme = THEMES.map(t => ({
    theme: t, distinct: new Set(results.filter(r => r.theme === t).map(r => signature(r))).size,
  }));
  const out = {
    tool: `axe-core ${require('axe-core/package.json').version} on populated casting-review modal, synthetic voices, 6 themes`,
    url: process.env.AA2_URL || 'http://127.0.0.1:8765/', date: new Date().toISOString(),
    summary: { surfaces: results.length, violations: bad.reduce((n, r) => n + r.violations.length, 0),
      zero_node_surfaces: results.filter(r => r.nodes === 0).map(r => `${r.theme}/${r.step}`),
      modals_opened: results.filter(r => r.shown).length,
      status_actions: statusAssertions.length, status_actions_effective: statusAssertions.length - ineffective.length,
      distinct_states_per_theme: distinctPerTheme },
    statusAssertions, results,
  };
  const outPath = process.env.AA2_CAST_OUT || path.join(__dirname, 'casting-results.json');
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.summary));
  for (const r of bad.slice(0, 20)) console.log(`${r.theme}/${r.step}: ${r.violations.map(v => v.summary.slice(0, 120)).join(' | ')}`);
  for (const a of ineffective) console.log(`INEFFECTIVE ${a.theme}/${a.step}: ${a.before} -> ${a.after}`);
  console.log(`report: ${outPath}`);
  process.exitCode = out.summary.violations === 0 && out.summary.zero_node_surfaces.length === 0
    && out.summary.modals_opened > 0 && ineffective.length === 0 ? 0 : 1;
})().catch(error => { console.error(error); process.exit(1); });
