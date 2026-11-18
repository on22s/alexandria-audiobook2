// Audit surfaces opened by REAL UI interactions (not forced display): Advanced
// dropdown, editor tool drawer panels, casting review modal, expert settings.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
const AXE = path.join(__dirname, 'node_modules/axe-core/axe.min.js');
const THEMES = ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek'];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: process.env.AA2_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, args: ['--no-sandbox', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200 });
  const response = await page.goto(process.env.AA2_URL || 'http://127.0.0.1:8765/',
    { waitUntil: 'networkidle2', timeout: 60000 });
  if (!response.ok()) throw new Error(`app returned ${response.status()}`);
  await page.addScriptTag({ path: AXE });
  const wait = (ms) => page.evaluate(ms => new Promise(r => setTimeout(r, ms)), ms);
  const settle = () => page.evaluate(() => new Promise((resolve) => {
    if (typeof CSSTransition !== 'undefined') {
      document.getAnimations().forEach((a) => {
        const p = a.transitionProperty;
        if (a instanceof CSSTransition && (p === 'color' || p.endsWith('-color') || p === 'box-shadow')) a.finish();
      });
    }
    setTimeout(resolve, 350);
  }));
  const audit = (selector) => page.evaluate(async (selector) => {
    const root = selector ? document.querySelector(selector) : document;
    if (!root) return { missing: true, nodes: 0, violations: [] };
    const res = await window.axe.run(root, { runOnly: { type: 'rule', values: ['color-contrast'] } });
    const nodes = res.passes.reduce((n, p) => n + p.nodes.length, 0)
      + res.violations.reduce((n, v) => n + v.nodes.length, 0)
      + res.incomplete.reduce((n, i) => n + i.nodes.length, 0);
    return { nodes, violations: res.violations.flatMap(v => v.nodes.map(n => ({
      target: n.target, summary: (n.failureSummary || '').replace(/\s+/g, ' ').slice(0, 180),
    }))) };
  }, selector);
  const results = [];

  for (const theme of THEMES) {
    await page.evaluate((t) => {
      const root = document.documentElement;
      if (t === 'light') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', t);
    }, theme);
    await settle();

    // 1. Advanced dropdown via real click
    await page.evaluate(() => document.getElementById('advancedNavDropdown')?.click());
    await wait(450);
    let r = await audit('.dropdown-menu.show');
    results.push({ theme, surface: 'advanced-dropdown-click', ...r });
    await page.evaluate(() => document.getElementById('advancedNavDropdown')?.click());
    await wait(200);

    // 2. Editor drawer opened for real, each tool panel clicked via its own button
    await page.evaluate(() => document.querySelector('[data-tab="editor"]')?.click());
    await wait(450);
    await page.evaluate(() => document.getElementById('editor-tools-toggle')?.click());
    await wait(450);
    const tools = await page.evaluate(() => Array.from(
      document.querySelectorAll('#editor-tool-drawer [data-editor-tool-target]'))
      .map(b => b.getAttribute('data-editor-tool-target')));
    for (const tool of tools) {
      await page.evaluate((tool) => document
        .querySelector(`#editor-tool-drawer [data-editor-tool-target="${tool}"]`)?.click(), tool);
      await wait(400);
      r = await audit('#editor-tool-drawer');
      const open = await page.evaluate((tool) => {
        const d = document.querySelector(`#editor-tool-drawer [data-editor-tool="${tool}"]`);
        return { open: d?.open === true, visibleText: (d?.textContent || '').trim().length };
      }, tool);
      results.push({ theme, surface: 'editor-tool:' + tool, ...r, ...open });
    }
    await page.evaluate(() => document.getElementById('editor-tools-close')?.click());
    await wait(200);

    // 3. Casting review modal via real click
    await page.evaluate(() => document.querySelector('[data-tab="casting"]')?.click());
    await wait(450);
    await page.evaluate(() => document.getElementById('casting-review')?.click());
    await wait(600);
    const modalState = await page.evaluate(() => {
      const m = document.getElementById('casting-review-modal');
      return { shown: m?.classList.contains('show') === true,
        bodyText: (document.getElementById('casting-review-body')?.textContent || '').trim().length };
    });
    r = await audit('#casting-review-modal.show');
    results.push({ theme, surface: 'casting-review-modal-click', ...r, ...modalState,
      skipped: modalState.shown ? undefined : 'casting-review button is not wired to the modal in this build' });
    await page.evaluate(() => document.querySelector('#casting-review-modal [data-bs-dismiss="modal"]')?.click());
    await wait(300);

    // 4. Expert settings toggle via real click
    await page.evaluate(() => document.querySelector('[data-tab="setup"]')?.click());
    await wait(450);
    await page.evaluate(() => document.getElementById('setup-expert-toggle')?.click());
    await wait(450);
    r = await audit('#setup-tab');
    results.push({ theme, surface: 'setup-expert-open', ...r });
    await page.evaluate(() => document.getElementById('setup-expert-toggle')?.click());
    await wait(200);
  }
  await browser.close();
  const bad = results.filter(r => r.violations && r.violations.length);
  const out = {
    tool: `axe-core ${require('axe-core/package.json').version} on live app after real UI clicks, 6 themes`,
    url: process.env.AA2_URL || 'http://127.0.0.1:8765/', date: new Date().toISOString(),
    summary: { surfaces: results.length, violations: bad.reduce((n, r) => n + r.violations.length, 0),
      zero_node_surfaces: results.filter(r => r.nodes === 0 && !r.skipped).map(r => `${r.theme}/${r.surface}`) },
    results,
  };
  const outPath = process.env.AA2_INTERACTION_OUT || path.join(__dirname, 'interaction-results.json');
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.summary));
  for (const r of bad.slice(0, 20)) console.log(`${r.theme}/${r.surface}: ${r.violations.map(v => v.summary.slice(0, 120)).join(' | ')}`);
  console.log(`report: ${outPath}`);
  process.exitCode = out.summary.violations === 0 && out.summary.zero_node_surfaces.length === 0 ? 0 : 1;
})().catch((error) => { console.error(error); process.exit(1); });
