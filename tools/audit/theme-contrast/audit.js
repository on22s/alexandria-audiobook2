// axe-core color-contrast audit of the real app, one pass per theme and tab panel.
//
// Usage (from this directory after `npm install`):
//   AA2_URL=http://127.0.0.1:8765/ AA2_BROWSER=/path/to/chrome-or-edge node audit.js
// Output: axe-results.json in this directory (override with AA2_OUT).
//
// The app must be running locally (e.g. `uvicorn app:app --port 8765` from app/).
// A static copy of index.html is NOT equivalent: its CSS transition makes the
// body lag one theme behind, which produces false violations.
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const URL = process.env.AA2_URL || 'http://127.0.0.1:8765/';
const BROWSER = process.env.AA2_BROWSER ||
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const OUT = process.env.AA2_OUT || path.join(__dirname, 'axe-results.json');
const AXE = path.join(__dirname, 'node_modules/axe-core/axe.min.js');
const THEMES = ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek'];
const TABS = ['home', 'setup', 'script', 'voices', 'audio', 'designer', 'casting',
  'preparer', 'dataset-builder', 'training', 'voicelab', 'editor', 'reports'];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: BROWSER, headless: true, args: ['--no-sandbox', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200 });
  const response = await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  if (!response.ok()) throw new Error(`app returned ${response.status()}`);
  await page.addScriptTag({ path: AXE });

  // Theme changes animate color over 0.25s; finish animations and wait before
  // measuring, or axe sees the previous theme's colors.
  const settle = () => page.evaluate(() => new Promise((resolve) => {
    // Same filter as the app's own applyTheme(): finishing a CSS transition on
    // color is safe, but finishing an infinite animation (e.g. an indeterminate
    // progress bar) throws InvalidStateError and changes what axe sees.
    if (typeof CSSTransition !== 'undefined') {
      document.getAnimations().forEach((animation) => {
        const property = animation.transitionProperty;
        if (animation instanceof CSSTransition &&
            (property === 'color' || property.endsWith('-color') || property === 'box-shadow')) {
          animation.finish();
        }
      });
    }
    setTimeout(resolve, 350);
  }));
  const runAxe = () => page.evaluate(async () =>
    await window.axe.run(document, { runOnly: { type: 'rule', values: ['color-contrast'] } }));

  const results = [];
  for (const theme of THEMES) {
    await page.evaluate((theme) => {
      const root = document.documentElement;
      if (theme === 'light') root.removeAttribute('data-theme');
      else root.setAttribute('data-theme', theme);
    }, theme);
    await settle();
    const body = await page.evaluate(() => ({
      color: getComputedStyle(document.body).color,
      bg: getComputedStyle(document.body).backgroundColor,
    }));
    const surfaces = [];
    for (const tab of TABS) {
      await page.evaluate((tab) => {
        document.querySelectorAll('.tab-content').forEach((el) => {
          el.style.setProperty('display', el.id === tab + '-tab' ? 'block' : 'none', 'important');
        });
      }, tab);
      const res = await runAxe();
      surfaces.push({
        tab,
        violations: res.violations.map((v) => ({
          id: v.id, impact: v.impact, nodes: v.nodes.length,
          samples: v.nodes.slice(0, 10).map((n) => ({
            target: n.target,
            summary: (n.failureSummary || '').split('\n').slice(0, 3).join(' '),
            html: (n.html || '').slice(0, 140),
          })),
        })),
        incomplete: res.incomplete.map((i) => ({ id: i.id, nodes: i.nodes.length })),
      });
    }
    results.push({ theme, body, surfaces });
  }
  await browser.close();

  const report = {
    tool: 'axe-core ' + require('axe-core/package.json').version +
      ' via puppeteer-core on ' + path.basename(BROWSER),
    url: URL, date: new Date().toISOString(), results,
  };
  fs.writeFileSync(OUT, JSON.stringify(report, null, 2));
  let total = 0;
  for (const r of results) {
    const n = r.surfaces.reduce((s, x) => s + x.violations.reduce((v, y) => v + y.nodes, 0), 0);
    total += n;
    console.log(`${r.theme}: ${n} color-contrast violations`);
  }
  console.log(`total: ${total}; report: ${OUT}`);
  process.exitCode = total === 0 ? 0 : 1;
})().catch((error) => { console.error(error); process.exit(1); });
