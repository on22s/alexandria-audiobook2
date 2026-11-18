# Theme contrast and focus audit (2026-11-18)

Work on #2057. Two independent passes: a repeatable **static token-pair audit**
and a **rendered axe-core run** against the live app.

## 1. Rendered axe-core run

- Tool: **axe-core 4.14.1** driven by **puppeteer-core 25.18.1** on local Edge,
  against `uvicorn app:app` at `http://127.0.0.1:8765/`.
- Scope: **6 themes × 13 tab panels** (home, setup, script, voices, audio,
  designer, casting, preparer, dataset-builder, training, voicelab, editor,
  reports), `runOnly: color-contrast`, viewport 1600×1200.
- Critical method detail: `body` has a 0.25s color transition. Setting
  `data-theme` and measuring immediately makes axe report the *previous*
  theme's colors (false violations). The runner finishes animations and waits
  350 ms per theme. A static inlined copy of `index.html` is **not** equivalent.
- Reproduce: `cd tools/audit/theme-contrast && npm install`, then
  `AA2_URL=http://127.0.0.1:8765/ npm run audit` (set `AA2_BROWSER` if Edge is
  elsewhere). Results are written to `axe-results.json`; exit code 1 on any
  violation.

**Before the fix:** light 6 nodes, night 1, super-night 1, cyberpunk 1, bread 1,
leek 1 (16 total). All were Bootstrap's literal `#0d6efd` leaking through:
`.btn-outline-primary` text on light surfaces (e.g. Refresh buttons, the
"Open Voices/Designer" buttons) and the plain "Download full log." link.

**Fix:** in `app/static/index.html`, add `a, a:hover { color: var(--accent-strong); }`
and a `.btn-outline-primary` override mapping color/border/hover/active to the
theme's `--accent-strong` and `--bg-card`. Bootstrap 5.3 anchors use
`--bs-link-color-rgb`, so overriding `--bs-link-color` alone does not work.

**After the fix:** **0 `color-contrast` violations** in all 6 themes × 13 tabs.

**Exceptions:** axe reported 136 `incomplete` color-contrast nodes (22 per
theme, 26 in cyberpunk). These are not violations: axe could not determine a
deterministic background, most likely because of gradients/shadows/overlays
(the cyberpunk body uses a fixed grid gradient and radial tint). They need a
manual visual check, not a rule change.

## 2. Static token-pair audit

- Source: inline theme tokens in `index.html` (`:root` for light; first
  `html[data-theme="…"]` block per theme).
- Formula: WCAG relative luminance with sRGB linearization,
  `(L_light + 0.05)/(L_dark + 0.05)`. Shared helper: `contrast()` in
  `app/tests/test_settings_discovery_js.py`. Tool: Python 3.11.9, stdlib only.
- Repeat: `PYTHONUTF8=1 python -m unittest tests.test_settings_discovery_js.DarkThemeContrastTests -v`
  from `app/`. A new test pins every pair for all six themes.

| Theme | primary/card | muted/card | muted/card-header | border-input/input | accent-strong/card |
| --- | ---: | ---: | ---: | ---: | ---: |
| light | 15.43 | 5.60 | 5.31 | 3.44 | 6.44 |
| night | 11.14 | 5.61 | 5.11 | 3.13 | 8.12 |
| super-night | 17.12 | 8.51 | 8.01 | 4.58 | 11.44 |
| cyberpunk | 16.53 | 6.72 | 6.22 | 4.78 | 14.61 |
| bread | 11.89 | 6.29 | 5.06 | 4.07 | 8.32 |
| leek | 12.72 | 7.59 | 6.45 | 4.00 | 9.70 |

All pass (text ≥ 4.5, UI ≥ 3). Cyberpunk accents on `--bg-card`: cyan 12.92,
yellow 15.16, pink 5.69 — the concerns that prompted #2057 are not reproduced.

`--badge-muted-bg` is excluded: defined in all six blocks, consumed by no CSS
rule or JS. If a badge ever uses it, measure the badge's real text/background
pair and extend the test.

## Still unverified

- Focus indicators: `:focus-visible` rules exist globally and in the workflow
  tabs, but "visible in every theme" needs a browser check per control.
- The 136 `incomplete` axe nodes need a manual look.
- Disabled-control text (Bootstrap opacity blending), placeholders, modals,
  the Advanced dropdown and the Editor tool drawer are not separately audited;
  the 13 tab panels were forced visible one at a time, which covers their
  static markup but not dynamically opened surfaces.
