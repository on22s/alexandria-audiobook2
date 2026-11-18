// Measure every axe `incomplete` color-contrast node: collect the node and its
// reason from axe, then compute its rendered foreground/background ratio in the
// browser (walking to the nearest non-transparent ancestor background). Writes
// a raw per-node artifact so exceptions are checkable, not hand-transcribed.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
const AXE = path.join(__dirname, 'node_modules/axe-core/axe.min.js');
const THEMES = ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek'];
const TABS = ['home', 'setup', 'script', 'voices', 'audio', 'designer', 'casting',
  'preparer', 'dataset-builder', 'training', 'voicelab', 'editor', 'reports'];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: process.env.AA2_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, args: ['--no-sandbox', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200 });
  const response = await page.goto(process.env.AA2_URL || 'http://127.0.0.1:8765/',
    { waitUntil: 'domcontentloaded', timeout: 60000 });
  if (!response.ok()) throw new Error(`app returned ${response.status()}`);
  await page.addScriptTag({ path: AXE });
  const settle = () => page.evaluate(() => new Promise((resolve) => {
    if (typeof CSSTransition !== 'undefined') {
      document.getAnimations().forEach((a) => {
        const p = a.transitionProperty;
        if (a instanceof CSSTransition && (p === 'color' || p.endsWith('-color') || p === 'box-shadow')) a.finish();
      });
    }
    setTimeout(resolve, 350);
  }));

  const nodes = [];
  for (const theme of THEMES) {
    await page.evaluate((t) => {
      const root = document.documentElement;
      if (t === 'light') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', t);
    }, theme);
    await settle();
    for (const tab of TABS) {
      await page.evaluate((tab) => {
        document.querySelectorAll('.tab-content').forEach((el) =>
          el.style.setProperty('display', el.id === tab + '-tab' ? 'block' : 'none', 'important'));
      }, tab);
      const axeResult = await page.evaluate(async () =>
        await window.axe.run(document, { runOnly: { type: 'rule', values: ['color-contrast'] } }));
      for (const incomplete of axeResult.incomplete) {
        for (const n of incomplete.nodes) {
          const measured = await page.evaluate((target) => {
            const el = document.querySelector(target[0]);
            if (!el) return null;
            const s = getComputedStyle(el);
            let bg = null, node = el, image = null;
            while (node) {
              const ns = getComputedStyle(node);
              if (!image && ns.backgroundImage !== 'none') image = ns.backgroundImage.slice(0, 100);
              const c = ns.backgroundColor;
              if (c && !/rgba\(0, 0, 0, 0\)|transparent/.test(c)) { bg = c; break; }
              node = node.parentElement;
            }
            return { color: s.color, bg, image, text: (el.textContent || '').trim().slice(0, 40) };
          }, n.target);
          const reason = (n.any || []).map(a => a.message).join(' | ').slice(0, 200);
          nodes.push({ theme, tab, target: n.target, reason, ...(measured || { missing: true }) });
        }
      }
    }
  }
  await browser.close();

  // Ratio computation in Node from the captured computed colors.
  function channels(css) {
    const m = css.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(',').map(Number);
    return { rgb: p.slice(0, 3), a: p.length > 3 ? p[3] : 1 };
  }
  function luminance(rgb) {
    const l = rgb.map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
    return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
  }
  for (const n of nodes) {
    if (n.missing || !n.color || !n.bg) { n.ratio = null; continue; }
    const f = channels(n.color), b = channels(n.bg);
    const rgb = f.rgb.map((v, i) => v * f.a + b.rgb[i] * (1 - f.a));
    const a = luminance(rgb), y = luminance(b.rgb);
    n.ratio = (Math.max(a, y) + 0.05) / (Math.min(a, y) + 0.05);
  }
  const ratios = nodes.map(n => n.ratio).filter(r => r != null);
  const out = {
    tool: `axe-core ${require('axe-core/package.json').version} + CDP on Edge, live app`,
    url: process.env.AA2_URL || 'http://127.0.0.1:8765/', date: new Date().toISOString(),
    summary: { total: nodes.length, measured: ratios.length,
      min_ratio: ratios.length ? Math.min(...ratios) : null },
    nodes,
  };
  const outPath = process.env.AA2_INCOMPLETE_OUT || path.join(__dirname, 'incomplete-results.json');
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.summary));
  console.log(`report: ${outPath}`);
  process.exitCode = ratios.length === nodes.length ? 0 : 1;
})().catch((error) => { console.error(error); process.exit(1); });
