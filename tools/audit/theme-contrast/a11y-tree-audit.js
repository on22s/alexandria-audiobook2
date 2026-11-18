// Screen-reader naming proxy: read Chrome's accessibility tree and verify each
// visible tab panel is exposed as a named region whose name matches its nav link.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
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
  const cdp = await page.createCDPSession();
  await cdp.send('DOM.enable');
  await cdp.send('Accessibility.enable');
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
      const { root } = await cdp.send('DOM.getDocument');
      const panel = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: `#${tab}-tab` });
      const link = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: `#nav-link-${tab}` });
      if (!panel.nodeId || !link.nodeId) {
        results.push({ theme, tab, error: 'DOM node missing' });
        continue;
      }
      const panelDesc = await cdp.send('DOM.describeNode', { nodeId: panel.nodeId, depth: 0 });
      const linkDesc = await cdp.send('DOM.describeNode', { nodeId: link.nodeId, depth: 0 });
      const { nodes } = await cdp.send('Accessibility.getFullAXTree');
      const byBackend = new Map(nodes.filter(n => n.backendDOMNodeId).map(n => [n.backendDOMNodeId, n]));
      const axPanel = byBackend.get(panelDesc.node.backendNodeId);
      const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const panelRole = axPanel?.role?.value || null;
      const panelName = clean(axPanel?.name?.value);
      // The Advanced links sit in a closed dropdown, so their AX nodes are
      // ignored and nameless; aria-labelledby still uses their text. Compare
      // against the DOM text, which is what the region name is derived from.
      const linkName = clean(await page.evaluate((tab) =>
        document.getElementById('nav-link-' + tab)?.textContent, tab));
      const mains = nodes.filter(n => n.role?.value === 'main' && !n.ignored).length;
      results.push({ theme, tab, panelRole, panelName, linkName, mains,
        ok: panelRole === 'region' && panelName.length > 0 && panelName === linkName && mains === 1 });
    }
  }
  await browser.close();
  const bad = results.filter(r => !r.ok);
  const out = {
    tool: `Chrome accessibility tree via CDP on Edge, live app; ${THEMES.length} themes x ${TABS.length} panels`,
    url: process.env.AA2_URL || 'http://127.0.0.1:8765/', date: new Date().toISOString(),
    summary: { checked: results.length, failed: bad.length,
      regions_named_correctly: results.filter(r => r.ok).length },
    results,
  };
  const outPath = process.env.AA2_AXTREE_OUT || path.join(__dirname, 'a11y-tree-results.json');
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.summary));
  for (const r of bad.slice(0, 20)) console.log(`${r.theme}/${r.tab}: role=${r.panelRole} name=${JSON.stringify(r.panelName)} link=${JSON.stringify(r.linkName)} mains=${r.mains}`);
  console.log(`report: ${outPath}`);
  process.exitCode = bad.length === 0 ? 0 : 1;
})().catch((error) => { console.error(error); process.exit(1); });
