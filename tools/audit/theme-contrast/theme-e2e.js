// E2E for #2058: real page, CDP-emulated prefers-color-scheme, fresh profiles.
// Verifies resolution order (stored > OS > light), live OS switching while no
// choice is stored, the explicit theme button stopping OS following, and that
// the navbar palette differs across all six themes.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
const URL_BASE = process.env.AA2_URL || 'http://127.0.0.1:8765/';
const EXE = process.env.AA2_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const AXE = path.join(__dirname, 'node_modules/axe-core/axe.min.js');

const readTheme = page => page.evaluate(() => ({
  theme: document.documentElement.getAttribute('data-theme') || 'light',
  stored: localStorage.getItem('alex-theme'),
  prefersDark: matchMedia('(prefers-color-scheme: dark)').matches,
}));
const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({executablePath: EXE, headless: true,
    args: ['--no-sandbox', '--disable-gpu']});
  const results = {}, palette = {}, failures = [];
  const expect = (name, actual, expected) => {
    if (actual !== expected) { failures.push(`${name}: got ${actual}, expected ${expected}`); }
  };

  for (const scheme of ['light', 'dark']) {
    const context = await browser.createBrowserContext();
    const page = await context.newPage();
    await page.setViewport({width: 1600, height: 1000});
    await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: scheme}]);
    await page.goto(URL_BASE, {waitUntil: 'networkidle2', timeout: 60000});
    results['fresh_' + scheme] = await readTheme(page);
    await context.close();
  }

  // Live OS switch while no explicit choice is stored.
  {
    const context = await browser.createBrowserContext();
    const page = await context.newPage();
    await page.setViewport({width: 1600, height: 1000});
    await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: 'dark'}]);
    await page.goto(URL_BASE, {waitUntil: 'networkidle2', timeout: 60000});
    results.before_os_switch = await readTheme(page);
    await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: 'light'}]);
    await wait(250);
    results.after_os_switch_no_choice = await readTheme(page);

    // Real click on the theme toggle -> explicit stored choice.
    await page.click('#theme-toggle');
    await wait(250);
    results.after_explicit_click = await readTheme(page);

    // Further OS switches must be ignored once a choice exists.
    for (const scheme of ['dark', 'light']) {
      await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: scheme}]);
      await wait(250);
      results['after_os_switch_with_choice_' + scheme] = await readTheme(page);
    }
    await context.close();
  }

  // Navbar palette: computed colours per theme on the real page.
  {
    const context = await browser.createBrowserContext();
    const page = await context.newPage();
    await page.setViewport({width: 1600, height: 1000});
    await page.goto(URL_BASE, {waitUntil: 'networkidle2', timeout: 60000});
    await page.addScriptTag({path: AXE});
    for (const theme of ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek']) {
      await page.evaluate(t => {
        if (t === 'light') { document.documentElement.removeAttribute('data-theme'); }
        else { document.documentElement.setAttribute('data-theme', t); }
      }, theme);
      await wait(100); // let colour transitions settle
      palette[theme] = await page.evaluate(async () => {
        const link = document.querySelector('.navbar .nav-link:not(.active):not(.dropdown-item)');
        const toggle = document.getElementById('theme-toggle');
        const axeResult = await window.axe.run(document.querySelector('.navbar'),
          {runOnly: {type: 'rule', values: ['color-contrast']}});
        return {
          link: link ? getComputedStyle(link).color : null,
          toggle: toggle ? getComputedStyle(toggle).color : null,
          toggleBorder: toggle ? getComputedStyle(toggle).borderTopColor : null,
          navbar: getComputedStyle(document.querySelector('.navbar')).backgroundColor,
          contrastViolations: axeResult.violations.flatMap(v => v.nodes.map(n => ({
            target: n.target, summary: (n.failureSummary || '').replace(/\s+/g, ' ').slice(0, 160),
          }))),
        };
      });
    }
    await context.close();
  }
  await browser.close();

  expect('fresh light theme', results.fresh_light.theme, 'light');
  expect('fresh light stored-empty', results.fresh_light.stored, null);
  expect('fresh dark theme', results.fresh_dark.theme, 'night');
  expect('fresh dark stored-empty', results.fresh_dark.stored, null);
  expect('before switch', results.before_os_switch.theme, 'night');
  expect('live switch -> light', results.after_os_switch_no_choice.theme, 'light');
  expect('live switch stored-empty', results.after_os_switch_no_choice.stored, null);
  const clicked = results.after_explicit_click;
  if (!clicked.stored) { failures.push('explicit click did not store a theme'); }
  expect('explicit click stored matches applied', clicked.theme, clicked.stored);
  for (const scheme of ['dark', 'light']) {
    expect('OS ignored after choice (' + scheme + ')',
      results['after_os_switch_with_choice_' + scheme].theme, clicked.stored);
  }
  const linkColors = new Set(Object.values(palette).map(p => p.link));
  if (linkColors.size !== 6) { failures.push('navbar link colours not theme-specific: ' + JSON.stringify([...linkColors])); }
  for (const theme of Object.keys(palette)) {
    if (palette[theme].contrastViolations.length) {
      failures.push(theme + ' navbar contrast: ' + JSON.stringify(palette[theme].contrastViolations));
    }
  }

  const out = {
    tool: `Puppeteer CDP prefers-color-scheme emulation on the live app; 6 themes`,
    url: URL_BASE, date: new Date().toISOString(),
    // 12 state/override assertions + 6 per-theme navbar contrast checks.
    summary: {checks: 18, distinct_link_colors: linkColors.size, failures: failures.length},
    results, palette, failures,
  };
  const outPath = process.env.AA2_THEME_E2E_OUT || path.join(__dirname, 'os-theme-results.json');
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.summary));
  for (const f of failures) { console.log('FAIL ' + f); }
  console.log(`report: ${outPath}`);
  process.exitCode = failures.length ? 1 : 0;
})().catch(error => { console.error(error); process.exit(1); });
