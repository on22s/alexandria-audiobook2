// The 136 axe `incomplete` color-contrast nodes were previously measured with a
// nearest-ancestor-solid-color heuristic. This audit samples the ACTUAL rendered
// pixels behind each node from clipped screenshots, so image/gradient
// backgrounds are measured rather than inferred.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
const { PNG } = require('pngjs');
const AXE = path.join(__dirname, 'node_modules/axe-core/axe.min.js');
const THEMES = ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek'];
const TABS = ['home', 'setup', 'script', 'voices', 'audio', 'designer', 'casting',
  'preparer', 'dataset-builder', 'training', 'voicelab', 'editor', 'reports'];

function channels(css) {
  const m = css.match(/rgba?\(([^)]+)\)/); if (!m) return null;
  const p = m[1].split(',').map(Number);
  return { rgb: p.slice(0, 3), a: p.length > 3 ? p[3] : 1 };
}
function luminance(rgb) {
  const l = rgb.map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
  return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
}
function ratio(a, b) { const x = luminance(a), y = luminance(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }

(async () => {
  const browser = await puppeteer.launch({
    executablePath: process.env.AA2_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, args: ['--no-sandbox', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: 1 });
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
  const records = [];

  for (const theme of THEMES) {
    await page.evaluate(t => {
      const root = document.documentElement;
      if (t === 'light') root.removeAttribute('data-theme'); else root.setAttribute('data-theme', t);
    }, theme);
    await settle();
    for (const tab of TABS) {
      await page.evaluate(tab => document.querySelectorAll('.tab-content').forEach(el =>
        el.style.setProperty('display', el.id === tab + '-tab' ? 'block' : 'none', 'important')), tab);
      const axeResult = await page.evaluate(async () => await window.axe.run(document,
        { runOnly: { type: 'rule', values: ['color-contrast'] } }));
      for (const inc of axeResult.incomplete) {
        for (const n of inc.nodes) {
          const reason = (n.any || []).map(a => a.message).join(' ');
          if (!/background image|background gradient|background-color could not be determined/i.test(reason)) continue;
          const info = await page.evaluate((target) => {
            const el = document.querySelector(target[0]);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2) return null;
            const s = getComputedStyle(el);
            return { x: r.x, y: r.y, width: r.width, height: r.height, color: s.color };
          }, n.target);
          if (!info || info.y < 0 || info.y + info.height > 1200) continue;
          let shot;
          try {
            shot = await page.screenshot({ clip: { x: Math.round(info.x), y: Math.round(info.y),
              width: Math.max(2, Math.round(info.width)), height: Math.max(2, Math.round(info.height)) } });
          } catch (e) { continue; }
          const png = PNG.sync.read(Buffer.from(shot));
          const counts = new Map();
          const text = channels(info.color);
          for (let i = 0; i < png.data.length; i += 4) {
            const r = png.data[i], g = png.data[i + 1], b = png.data[i + 2];
            if (text && Math.abs(r - text.rgb[0]) + Math.abs(g - text.rgb[1]) + Math.abs(b - text.rgb[2]) < 60) continue;
            const key = `${r},${g},${b}`;
            counts.set(key, (counts.get(key) || 0) + 1);
          }
          if (!counts.size) continue;
          const [bgKey] = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
          const bg = bgKey.split(',').map(Number);
          const pr = text ? ratio(text.rgb, bg) : null;
          records.push({ theme, tab, target: n.target, reason: reason.slice(0, 160),
            textColor: info.color, sampledBg: `rgb(${bg.join(', ')})`, pixel_ratio: pr,
            pass: pr != null && pr >= 4.5 });
        }
      }
    }
  }
  await browser.close();
  const bad = records.filter(r => !r.pass);
  const out = {
    tool: `axe-core incomplete nodes + screenshot pixel sampling on Edge, live app, ${THEMES.length} themes`,
    url: process.env.AA2_URL || 'http://127.0.0.1:8765/', date: new Date().toISOString(),
    summary: { nodes_sampled: records.length, failures: bad.length,
      min_ratio: Math.min(...records.filter(r => r.pixel_ratio != null).map(r => r.pixel_ratio)).toFixed(4) },
    records,
  };
  const outPath = process.env.AA2_PIXEL_OUT || path.join(__dirname, 'incomplete-pixel-results.json');
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.summary));
  for (const r of bad.slice(0, 20)) console.log(`${r.theme}/${r.tab} ${r.target.join(' ')} ${r.pixel_ratio?.toFixed(2)} ${r.textColor} on ${r.sampledBg}`);
  console.log(`report: ${outPath}`);
  process.exitCode = bad.length === 0 ? 0 : 1;
})().catch(error => { console.error(error); process.exit(1); });
