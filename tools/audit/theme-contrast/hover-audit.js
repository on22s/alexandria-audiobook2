// Hover/active contrast audit. axe-core misreads the foreground under a CDP
// forced pseudo-class (it mixes the resting text color with the hover fill), so
// this audit forces :hover/:active and computes the ratio from the *rendered*
// computed styles instead. Run with `npm run hover` while the app is served.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
const THEMES = ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek'];
const TABS = ['home', 'setup', 'script', 'voices', 'audio', 'designer', 'casting',
  'preparer', 'dataset-builder', 'training', 'voicelab', 'editor', 'reports'];

function channels(css) {
  const m = css.match(/rgba?\(([^)]+)\)/);
  if (!m) return null;
  const p = m[1].split(',').map(Number);
  return { rgb: p.slice(0, 3), a: p.length > 3 ? p[3] : 1 };
}
function flatten(layers) {
  // Composite translucent layers over the nearest opaque ancestor background.
  let base = null;
  for (let i = layers.length - 1; i >= 0; i--) {
    const layer = channels(layers[i]);
    if (!layer) continue;
    base = base ? layer.rgb.map((v, k) => v * layer.a + base[k] * (1 - layer.a)) : layer.rgb;
  }
  return base;
}
function luminanceOf(rgb) {
  const l = rgb.map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
  return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
}
function contrast(fgCss, bgLayers) {
  const f = channels(fgCss);
  const bg = flatten(bgLayers);
  if (!f || !bg) return null;
  const fg = f.rgb.map((v, k) => v * f.a + bg[k] * (1 - f.a));
  const a = luminanceOf(fg), b = luminanceOf(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

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
  // Let async home/setup content settle before enumerating controls.
  await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 500)));
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
  const results = [];

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
      const controls = await page.evaluate(() => {
        for (const el of document.querySelectorAll('[data-hover-audit-id]')) el.removeAttribute('data-hover-audit-id');
        const out = [];
        for (const el of document.querySelectorAll('.tab-content a[href], .tab-content button, .tab-content summary')) {
          const r = el.getBoundingClientRect(), s = getComputedStyle(el);
          if (r.width < 1 || r.height < 1 || s.visibility === 'hidden' || s.display === 'none') continue;
          // Exclude disabled controls (including inherited fieldset[disabled])
          // and content inside closed <details>, matching the project's
          // disabled-control exemption and axe's own visibility rules.
          // A <summary> stays visible and clickable while its <details> is
          // closed, so only exclude other descendants of a closed details.
          if (el.matches(':disabled') || el.closest('fieldset[disabled]') ||
              (el.tagName.toLowerCase() !== 'summary' && el.closest('details:not([open])'))) continue;
          if (!(el.textContent || '').trim()) continue;
          el.setAttribute('data-hover-audit-id', String(out.length));
          out.push({ id: el.id || null, tag: el.tagName.toLowerCase(), text: (el.textContent || '').trim().slice(0, 40) });
        }
        return out;
      });
      // Independent visibility definition: enumerate all elements of the
      // eligible tags and apply the same rules without the tagging selector,
      // so a filter mistake cannot hide controls from the completeness guard.
      const expectedCount = await page.evaluate(() => {
        const tags = new Set(['A', 'BUTTON', 'SUMMARY']);
        let count = 0;
        for (const el of document.querySelectorAll('.tab-content *')) {
          if (!tags.has(el.tagName) || (el.tagName === 'A' && !el.hasAttribute('href'))) continue;
          const r = el.getBoundingClientRect(), s = getComputedStyle(el);
          if (r.width < 1 || r.height < 1 || s.visibility === 'hidden' || s.display === 'none' ||
              !(el.textContent || '').trim()) continue;
          if (el.matches(':disabled') || el.closest('fieldset[disabled]')) continue;
          if (el.tagName !== 'SUMMARY' && el.closest('details:not([open])')) continue;
          count++;
        }
        return count;
      });
      const { root } = await cdp.send('DOM.getDocument');
      for (const pseudo of ['hover', 'active']) {
        const forced = [];
        for (let i = 0; i < controls.length; i++) {
          const q = await cdp.send('DOM.querySelector', {
            nodeId: root.nodeId, selector: `[data-hover-audit-id="${i}"]`,
          });
          if (q.nodeId) {
            await cdp.send('CSS.forcePseudoState', { nodeId: q.nodeId, forcedPseudoClasses: [pseudo] });
            forced.push(q.nodeId);
          }
        }
        // Inherited colors can lag one frame after a forced pseudo-class.
        await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 400)));
        const measured = await page.evaluate((count) => {
          const out = [];
          for (let i = 0; i < count; i++) {
            const el = document.querySelector(`[data-hover-audit-id="${i}"]`);
            if (!el) continue;
            const layers = [];
            let node = el;
            while (node) {
              const bg = getComputedStyle(node).backgroundColor;
              if (bg && !/rgba\(0, 0, 0, 0\)|transparent/.test(bg)) layers.push(bg);
              node = node.parentElement;
            }
            // Measure the actual rendered text, not the control's own color:
            // nested spans (e.g. .text-muted) can differ from the parent.
            const colors = new Set();
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            let textNode;
            while ((textNode = walker.nextNode())) {
              if (!textNode.nodeValue.trim()) continue;
              const parent = textNode.parentElement;
              const ps = getComputedStyle(parent);
              if (ps.display === 'none' || ps.visibility === 'hidden' ||
                  parent.closest('.sr-only, .visually-hidden, [hidden]')) continue;
              colors.add(ps.color);
            }
            if (!colors.size) colors.add(getComputedStyle(el).color);
            out.push({ colors: [...colors], layers });
          }
          return out;
        }, controls.length);
        for (const nodeId of forced) {
          await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: [] });
        }
        for (let i = 0; i < measured.length; i++) {
          const ratios = measured[i].colors.map(color => contrast(color, measured[i].layers));
          const r = ratios.length ? Math.min(...ratios) : null;
          results.push({ theme, tab, pseudo, control: controls[i],
            colors: measured[i].colors, background_layers: measured[i].layers, ratio: r,
            pass: r != null && r >= 4.5 });
        }
        if (measured.length !== controls.length || controls.length !== expectedCount) {
          results.push({ theme, tab, pseudo, completeness_error:
            `${measured.length} measured / ${controls.length} tagged / ${expectedCount} eligible`, pass: false });
        }
      }
    }
  }
  await browser.close();
  const bad = results.filter(r => !r.pass);
  const out = {
    tool: `CDP forced :hover/:active + computed-style WCAG ratio on Edge, live app; ${THEMES.length} themes x ${TABS.length} panels`,
    url: process.env.AA2_URL || 'http://127.0.0.1:8765/', date: new Date().toISOString(),
    summary: { controls_measured: results.filter(r => r.ratio != null).length,
      failures: bad.length,
      min_ratio: Math.min(...results.filter(r => r.ratio != null).map(r => r.ratio)).toFixed(4) },
    results,
  };
  const outPath = process.env.AA2_HOVER_OUT || path.join(__dirname, 'hover-results.json');
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.summary));
  for (const r of bad.slice(0, 20)) console.log(`${r.theme}/${r.tab}/${r.pseudo} ${r.control ? (r.control.id || r.control.tag) : 'INCOMPLETE'} ${r.ratio?.toFixed(2)} ${r.colors?.join(' / ')} on ${r.background_layers?.join(' + ')} ${r.completeness_error || ''}`);
  console.log(`report: ${outPath}`);
  process.exitCode = bad.length === 0 ? 0 : 1;
})().catch((error) => { console.error(error); process.exit(1); });
