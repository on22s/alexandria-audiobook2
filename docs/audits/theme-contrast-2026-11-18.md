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
leek 1 (**11 total**). All were Bootstrap's literal `#0d6efd` leaking through:
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
  `(L_light + 0.05)/(L_dark + 0.05)`. The all-six-themes guard is
  `UIContractTests.test_theme_text_and_input_border_contrast` in
  `app/tests/test_ui_contract.py`; `DarkThemeContrastTests` in
  `test_settings_discovery_js.py` still covers only night/super-night and owns
  the local `contrast()` helper. Tool: Python 3.11.9, stdlib only.
- Repeat: `PYTHONUTF8=1 python -m unittest tests.test_ui_contract tests.test_settings_discovery_js -v`
  from `app/`.

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

## 3. Focus-indicator audit

- Tool: CDP `CSS.forcePseudoState` on Edge via `tools/audit/theme-contrast/focus-audit.js`
  (`npm run focus`), against the live app. Repeat with `AA2_URL` set as for the
  contrast audit.
- Scope: 6 themes × 13 tab panels × up to 10 visible interactive controls each
  = **780 controls**. For each, require a non-`none` outline or box-shadow, and
  compute the outline's ratio against the adjacent (possibly inherited)
  background; translucent outlines are reported as undeterminable.
- **Before:** 52 findings, four recurring controls: `#theme-toggle` in light
  (2.40 on the dark navbar) and bread (1.39), and `#nav-link-setup` when active
  in night (1.88) and super-night (2.15) against the green active background.
- **Fix:** a light ring on those dark surfaces —
  `#theme-toggle:focus-visible` and `#navbarNav .nav-link.nav-pipeline.active:focus-visible`
  use `outline-color: #fff` (≥4.5:1 on every navbar and on the active green).
  A static test pins both rules.
- **After:** 0 findings across all 780 controls.

## 4. Incomplete nodes and dynamic surfaces

- `npm run incomplete` measures every axe `incomplete` color-contrast node and
  writes a **per-node** artifact: all **136 nodes** measured, minimum 4.5008:1
  (the `.step-indicator.step-active` solid pair). By axe reason: 96
  undeterminable background-image, 30 text too short, 6 non-text-only, 4
  background-gradient. **Method limit:** where axe could not determine a
  background because of an image or gradient, the script substitutes the nearest
  non-transparent *ancestor solid* color. That is not the rendered pixels, so
  this measurement cannot clear those 100 image/gradient nodes; they still need
  a human visual look. The 4.5008 lower bound applies to the heuristic.
  Artifact: `docs/audits/theme-contrast-2026-11-18-incomplete-nodes.json`.
- To write the artifacts directly to this folder, set `AA2_DYNAMIC_OUT` and
  `AA2_INCOMPLETE_OUT` to the `docs/audits/` paths before `npm run dynamic` /
  `npm run incomplete` (the default output is gitignored in the tool folder).
- `npm run dynamic` audits the Advanced dropdown, the Editor drawer with **all
  seven `<details data-editor-tool>` panels open**, and every `.modal` with its
  owning tab panel shown. Each surface records the number of axe-evaluated
  nodes and is flagged if zero, so a hidden surface can never report clean.
  Results: 42 surfaces, **39–40 nodes** in each drawer pass, 7 in each dropdown,
  and **3–17 nodes per modal per theme** (5 modals, 44 per theme, 264 total),
  **0 color-contrast violations**; 546 placeholders measured (min 5.1129:1).
  Per-surface minimum assertions (drawer ≥30, dropdown ≥5, modal ≥3) fail a
  silently collapsed surface. `#casting-review-body` is empty in this state, so
  dynamically populated casting controls are out of scope.
  Disabled controls are inactive UI and exempt from WCAG 1.4.3; their recorded
  inputs include the captured opacity. Raw tool output:
  `docs/audits/theme-contrast-2026-11-18-dynamic-results.json`.
- The first honest drawer/modal pass exposed four real violations on Bootstrap
  outline semantic buttons in `#casting-review-modal` (warning yellow 1.63:1 on
  white; bread success/danger 4.21:1; bread warning 1.51:1). Those are fixed by
  darker per-theme `--bs-btn-*` colors on light surfaces, including hover/active.

## 5. Hover/active and screen-reader naming

- `npm run hover` forces `:hover` and `:active` via CDP on all visible text
  controls of each panel and computes the WCAG ratio from the **rendered
  computed styles** (with alpha-composited backgrounds and the actual nested
  text-node colors, not the control's own color). It deliberately does not use
  axe here: axe mixes the resting foreground with the forced hover fill and
  reported 38 false positives in a cross-check. Native inputs, disabled
  controls (including `fieldset[disabled]` descendants), closed-`<details>`
  content and icon-only controls are excluded. There is no per-panel cap, and
  the run fails if any eligible control is not measured.
- Result: 6 themes × 13 panels × 2 states, **over 1,100 controls measured, 0
  failures, minimum 4.50:1** (the committed artifact records the exact figure,
  about 216 per theme). The exact count varies with asynchronous content, so it
  is not a contract. The one genuine issue found was cyberpunk
  `.btn-primary:hover` / `.btn-info:hover` (white on `#00a6b8` = 2.94:1); the
  shared hover fill was darkened to `#0a7183` (5.67:1). Artifact:
  `docs/audits/theme-contrast-2026-11-18-hover-results.json`.
- `npm run axtree` reads Chrome's accessibility tree and checks that each of the
  78 theme/panel combinations exposes exactly one `main` landmark and a `region`
  whose computed name equals its nav link's text (the proxy for a screen reader
  announcing "3. Voices, region"). Result: **78/78 pass**. The Advanced links
  are in a closed dropdown with ignored AX nodes, so the expected name is read
  from the link's DOM text, which is what `aria-labelledby` actually resolves.
  Artifact: `docs/audits/theme-contrast-2026-11-18-axtree-results.json`.

## 6. Real UI interactions (dynamic surfaces)

- The earlier dynamic audit forced surfaces open via CSS, which can miss
  state that only a real click produces. `npm run interaction` drives the page
  with actual clicks in each theme: the Advanced dropdown, the Editor drawer
  opened by its toggle, **all seven tool panels selected through their own
  buttons**, the Expert-settings switch, and the casting-review button.
- Result: **60 surfaces, 0 axe color-contrast violations, 0 zero-node
  surfaces**. Editor tool panels contribute 15–31 evaluated nodes each and all
  report `open=true` after their click. Artifact:
  `docs/audits/theme-contrast-2026-11-18-interaction-results.json`.
- The casting-review modal is recorded as not-applicable, not as a clean pass.
  `#casting-review` has no event listener, `onclick`, or Bootstrap
  `data-bs-toggle`/`data-bs-target` in this build, and no JS opens
  `#casting-review-modal`, so the control is dead and the modal is unreachable
  through real clicks regardless of saved data. Populated casting-review content
  therefore remains unverified — and more fundamentally than a data gap.

## 7. Pixel check of the image/gradient `incomplete` nodes

- The earlier per-node measurement substituted the nearest non-transparent
  ancestor solid color, which is not the rendered pixels for backgrounds built
  from images or gradients (100 of the 136 incomplete nodes). `npm run pixels`
  now takes clipped screenshots of each such node, samples the dominant
  non-text pixel color, and computes the ratio against the rendered text color.
- Result: **100 nodes sampled, 0 failures, minimum 7.2643:1** — well above AA
  and higher than the heuristic's 4.5008 lower bound. Artifact:
  `docs/audits/theme-contrast-2026-11-18-incomplete-pixel-results.json`.

## Still unverified

- Dynamically populated casting-review content (its trigger button is unwired
  in this build, so the modal is unreachable by real clicks) and a real
  NVDA/VoiceOver run — the AX tree is a proxy.

