// Audit the surfaces the 13 forced-visible tab panels do not cover: the
// Advanced dropdown, Editor tool drawer, every modal, placeholders and
// disabled controls. Run with `npm run dynamic` while the app is served at
// AA2_URL. Each surface records how many nodes axe actually evaluated so an
// accidentally hidden surface can never be reported as clean.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
const AXE = path.join(__dirname, 'node_modules/axe-core/axe.min.js');
const THEMES = ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek'];

function channels(css) {
  const m = css.match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const p = m[1].split(',').map(Number);
  return { rgb: p.slice(0, 3), a: p.length > 3 ? p[3] : 1 };
}
function composite(foreground, background) {
  const f = channels(foreground), b = channels(background);
  if (!f || !b) return null;
  return f.rgb.map((v, i) => v * f.a + b.rgb[i] * (1 - f.a));
}
function luminance(rgb) {
  const l = rgb.map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
  return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
}
function ratio(fg, bg, opacity = 1) {
  const rgb = composite(fg, bg);
  if (!rgb) return null;
  // An element's opacity blends its rendered text back over the background.
  const effective = rgb.map((v, i) => v * opacity + channels(bg).rgb[i] * (1 - opacity));
  const a = luminance(effective), b = luminance(channels(bg).rgb);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}
function nodeCount(res) {
  return res.passes.reduce((n, p) => n + p.nodes.length, 0)
    + res.violations.reduce((n, v) => n + v.nodes.length, 0)
    + res.incomplete.reduce((n, i) => n + i.nodes.length, 0);
}
function violationsOf(res) {
  return res.violations.flatMap(v => v.nodes.map(n => ({
    target: n.target, summary: (n.failureSummary || '').slice(0, 160),
  })));
}

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
  const run = (selector) => page.evaluate(async (selector) => await window.axe.run(
    selector ? document.querySelector(selector) : document,
    { runOnly: { type: 'rule', values: ['color-contrast'] } }), selector);
  const showTab = (tab) => page.evaluate((tab) => {
    document.querySelectorAll('.tab-content').forEach((el) =>
      el.style.setProperty('display', el.id === tab + '-tab' ? 'block' : 'none', 'important'));
  }, tab);
  const surface = async (theme, name, selector) => {
    // Theme transitions are settled once per theme; forcing a surface's display
    // is synchronous layout. Finishing animations here collapses the drawer.
    const res = await run(selector);
    return { theme, surface: name, nodes_evaluated: nodeCount(res), violations: violationsOf(res) };
  };
  const report = [];
  const zeroNode = [];
  const record = (r) => {
    report.push(r);
    if (r.nodes_evaluated === 0) zeroNode.push(`${r.theme}/${r.surface}`);
  };

  for (const theme of THEMES) {
    await page.evaluate((t) => {
      const root = document.documentElement;
      if (t === 'light') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', t);
    }, theme);
    await settle();
    await showTab('home');

    // 1. Advanced dropdown
    await page.evaluate(() => {
      const menu = document.querySelector('#advancedNavDropdown')?.parentElement?.querySelector('.dropdown-menu');
      if (menu) { menu.classList.add('show'); menu.style.setProperty('display', 'block', 'important'); }
    });
    record(await surface(theme, 'advanced-dropdown', '.dropdown-menu.show'));
    await page.evaluate(() => {
      const menu = document.querySelector('#advancedNavDropdown')?.parentElement?.querySelector('.dropdown-menu');
      if (menu) { menu.classList.remove('show'); menu.style.removeProperty('display'); }
    });

    // 2. Editor tool drawer — every <details data-editor-tool> must be open,
    //    not merely visible, or its contents have no layout for axe to audit.
    await showTab('editor');
    await page.evaluate(() => {
      if (typeof toggleEditorTools === 'function') toggleEditorTools(true);
      document.querySelectorAll('#editor-tool-drawer [data-editor-tool]').forEach((el) => {
        el.hidden = false;
        el.open = true;
        el.style.setProperty('display', 'block', 'important');
      });
    });
    record(await surface(theme, 'editor-drawer', '#editor-tool-drawer'));
    await page.evaluate(() => {
      if (typeof toggleEditorTools === 'function') toggleEditorTools(false);
      document.querySelectorAll('#editor-tool-drawer [data-editor-tool]').forEach((el) => {
        el.open = false; el.style.removeProperty('display');
      });
    });

    // 3. Modals — show the owning tab panel first, otherwise a modal forced
    //    display:block still has a display:none ancestor and 0 audited nodes.
    const modals = await page.evaluate(() => Array.from(document.querySelectorAll('.modal')).map(m => ({
      id: m.id, owner: m.closest('.tab-content')?.id || null,
    })).filter(m => m.id));
    for (const modal of modals) {
      if (modal.owner) await showTab(modal.owner.replace(/-tab$/, ''));
      await page.evaluate((id) => {
        const m = document.getElementById(id);
        m.classList.add('show'); m.style.setProperty('display', 'block', 'important');
        m.setAttribute('aria-hidden', 'false'); m.removeAttribute('inert');
      }, modal.id);
      record(await surface(theme, 'modal:' + modal.id, '#' + modal.id));
      await page.evaluate((id) => {
        const m = document.getElementById(id);
        m.classList.remove('show'); m.style.removeProperty('display');
        m.setAttribute('aria-hidden', 'true'); m.setAttribute('inert', '');
      }, modal.id);
    }

    // 4. Placeholders and disabled controls (axe skips both); record inputs.
    const colors = await page.evaluate(() => {
      const out = { placeholders: [], disabled: [] };
      for (const el of document.querySelectorAll('input[placeholder], textarea[placeholder]')) {
        const s = getComputedStyle(el, '::placeholder');
        out.placeholders.push({ id: el.id, color: s.color, bg: getComputedStyle(el).backgroundColor });
      }
      for (const el of document.querySelectorAll('button:disabled, input:disabled, select:disabled')) {
        const s = getComputedStyle(el);
        out.disabled.push({ id: el.id, color: s.color, bg: s.backgroundColor, opacity: s.opacity });
      }
      return out;
    });
    for (const p of colors.placeholders) report.push({ theme, surface: 'placeholder:' + (p.id || '?'),
      inputs: { color: p.color, bg: p.bg }, ratio: ratio(p.color, p.bg) });
    // Disabled controls are WCAG-exempt and their opacity blends toward the
    // parent background, not their own; record the inputs, not a misleading ratio.
    for (const d of colors.disabled) report.push({ theme, surface: 'disabled:' + (d.id || '?'),
      inputs: { color: d.color, bg: d.bg, opacity: d.opacity } });
  }
  await browser.close();

  const bad = report.filter(r => r.violations && r.violations.length);
  const ph = report.filter(r => r.surface.startsWith('placeholder:') && r.ratio != null).map(r => r.ratio);
  // A silently collapsed surface can still yield a few nodes, so require a
  // minimum count per surface family, not merely a non-zero one.
  const minimumFor = (surface) => surface.startsWith('modal:') ? 3
    : surface === 'editor-drawer' ? 30
    : surface === 'advanced-dropdown' ? 5 : 0;
  const underCovered = report.filter(r => r.nodes_evaluated != null &&
    r.nodes_evaluated < minimumFor(r.surface)).map(r => `${r.theme}/${r.surface}:${r.nodes_evaluated}`);
  const out = {
    tool: `axe-core ${require('axe-core/package.json').version} + CDP on Edge, live app; every surface records nodes evaluated and must meet a minimum`,
    url: process.env.AA2_URL || 'http://127.0.0.1:8765/', date: new Date().toISOString(),
    summary: { surfaces_with_axe: report.filter(r => r.nodes_evaluated != null).length,
      violations: bad.reduce((n, r) => n + r.violations.length, 0),
      zero_node_surfaces: zeroNode, under_covered_surfaces: underCovered,
      placeholders: ph.length, min_placeholder_ratio: ph.length ? Math.min(...ph) : null },
    report,
  };
  const outPath = process.env.AA2_DYNAMIC_OUT || path.join(__dirname, 'dynamic-results.json');
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.summary));
  console.log(`report: ${outPath}`);
  process.exitCode = out.summary.violations === 0 && zeroNode.length === 0 &&
    underCovered.length === 0 ? 0 : 1;
})().catch((error) => { console.error(error); process.exit(1); });
