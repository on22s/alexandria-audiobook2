// Focus-visible audit of the real app: force :focus-visible on interactive
// controls via CDP and require a visible indicator with >=3:1 against the
// adjacent background. Run with `npm run focus` while the app is served at
// AA2_URL (default http://127.0.0.1:8765/).
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const URL = process.env.AA2_URL || 'http://127.0.0.1:8765/';
const BROWSER = process.env.AA2_BROWSER ||
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const OUT = process.env.AA2_FOCUS_OUT || path.join(__dirname, 'focus-results.json');
const THEMES = ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek'];
const TABS = ['home', 'setup', 'script', 'voices', 'audio', 'designer', 'casting',
  'preparer', 'dataset-builder', 'training', 'voicelab', 'editor', 'reports'];

function luminance(color) {
  const m = color.match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const parts = m[1].split(',').map((x) => parseFloat(x));
  const alpha = parts.length > 3 ? parts[3] : 1;
  if (alpha < 0.5) return null;             // translucent: cannot pin a ratio
  const ch = parts.slice(0, 3).map((v) => v / 255);
  const lin = ch.map((c) => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}
function ratio(a, b) {
  const la = luminance(a), lb = luminance(b);
  if (la == null || lb == null) return null;
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: BROWSER, headless: true, args: ['--no-sandbox', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200 });
  const response = await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  if (!response.ok()) throw new Error(`app returned ${response.status()}`);
  const cdp = await page.createCDPSession();
  await cdp.send('DOM.enable');
  await cdp.send('CSS.enable');
  const settle = () => page.evaluate(() => new Promise((resolve) => {
    if (typeof CSSTransition !== 'undefined') {
      document.getAnimations().forEach((a) => {
        const p = a.transitionProperty;
        if (a instanceof CSSTransition && (p === 'color' || p.endsWith('-color') || p === 'box-shadow')) a.finish();
      });
    }
    setTimeout(resolve, 350);
  }));
  const findings = [];
  let checked = 0;

  for (const theme of THEMES) {
    await page.evaluate((t) => {
      const root = document.documentElement;
      if (t === 'light') root.removeAttribute('data-theme');
      else root.setAttribute('data-theme', t);
    }, theme);
    await settle();
    for (const tab of TABS) {
      await page.evaluate((tab) => {
        document.querySelectorAll('.tab-content').forEach((el) => {
          el.style.setProperty('display', el.id === tab + '-tab' ? 'block' : 'none', 'important');
        });
      }, tab);
      const controls = await page.evaluate(() => {
        for (const el of document.querySelectorAll('[data-focus-audit-id]')) el.removeAttribute('data-focus-audit-id');
        const found = [];
        for (const el of document.querySelectorAll('a[href], button, input, select, textarea, summary, [tabindex]')) {
          const rect = el.getBoundingClientRect();
          const style = getComputedStyle(el);
          if (rect.width < 1 || rect.height < 1 || style.visibility === 'hidden' || style.display === 'none') continue;
          if (found.length >= 10) break;
          el.setAttribute('data-focus-audit-id', String(found.length));
          found.push({ tag: el.tagName.toLowerCase(), id: el.id || null,
            text: (el.textContent || '').trim().slice(0, 50) });
        }
        return found;
      });
      const { root } = await cdp.send('DOM.getDocument');
      for (let i = 0; i < controls.length; i++) {
        const { nodeId } = await cdp.send('DOM.querySelector', {
          nodeId: root.nodeId, selector: `[data-focus-audit-id="${i}"]`,
        });
        if (!nodeId) continue;
        await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: ['focus-visible'] });
        const style = await page.evaluate((i) => {
          const el = document.querySelector(`[data-focus-audit-id="${i}"]`);
          const s = getComputedStyle(el);
          let bg = s.backgroundColor, node = el;
          while ((!bg || bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent') && node.parentElement) {
            node = node.parentElement; bg = getComputedStyle(node).backgroundColor;
          }
          return { outlineStyle: s.outlineStyle, outlineWidth: s.outlineWidth,
            outlineColor: s.outlineColor, boxShadow: s.boxShadow, bg };
        }, i);
        checked++;
        const hasOutline = style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0;
        const hasShadow = style.boxShadow && style.boxShadow !== 'none';
        if (!hasOutline && !hasShadow) {
          findings.push({ theme, tab, control: controls[i], reason: 'no visible focus indicator', style });
        } else if (hasOutline) {
          const r = ratio(style.outlineColor, style.bg);
          if (r == null) findings.push({ theme, tab, control: controls[i], reason: 'translucent outline color (ratio undeterminable)', style });
          else if (r < 3) findings.push({ theme, tab, control: controls[i], reason: `outline contrast ${r.toFixed(2)} < 3`, style, ratio: r });
        }
        await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: [] });
      }
    }
  }
  await browser.close();
  fs.writeFileSync(OUT, JSON.stringify({ tool: 'CDP forced :focus-visible on Edge, live app',
    url: URL, date: new Date().toISOString(), checked, findings }, null, 2));
  console.log(`checked=${checked} findings=${findings.length}; report: ${OUT}`);
  for (const f of findings.slice(0, 20)) {
    console.log(` ${f.theme}/${f.tab} ${f.control.tag}${f.control.id ? '#' + f.control.id : ''} :: ${f.reason}`);
  }
  process.exitCode = findings.length === 0 ? 0 : 1;
})().catch((error) => { console.error(error); process.exit(1); });
