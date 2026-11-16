# Advanced-tool intros acceptance

Date: 2026-11-16. Base: `46663c30872ba89a8654176c712c6ce396d0391d` (#1988), branch `ui/advanced-tool-intros`.

## Scope and source checks

Seven intros share `app/static/js/app-advanced-intros.js`. Text lives under `advanced.intro.<tool>.*` in all four catalogs; English is generated from authored message tokens. Each disclosure starts open without a saved preference, stores collapse independently per tool, handles unavailable storage, and does not write the navigation hash. Existing h4 tool views contain h5 intro headings; Reports uses h4 above its existing h5 cards.

Claims were checked against the existing controls/help and these implementation paths before writing the copy:

- Designer: `app/routers/voice_design.py` and the Designer handlers in `app/static/js/app-scripts.js`.
- Casting: `app/routers/casting.py`, `app/casting_tags.py`, `app/static/js/app-casting.js`, and `app/static/js/app-casting-research.js`.
- Preparer: `app/routers/preparer.py` and its handlers in `app/static/js/app-workbench.js`.
- Dataset: `app/routers/dataset_builder.py` and the dataset-builder handlers in `app/static/js/app-workbench.js`.
- Training: `app/routers/lora.py` and `app/static/js/app-training.js`.
- Voice Lab: `app/routers/voicelab.py` and `app/static/js/app-voicelab.js`.
- Reports: `app/static/js/app-reports.js`, `app/routers/benchmark.py`, and report/checkpoint routes in `app/routers/editor.py` and run routes in `app/routers/system.py`.

No model, training, assignment, deletion, or editor behavior was changed. Batch E areas were left alone. Taxonomy translation changes presentation only: category/tag keys, query syntax, option values, saved marks and API payloads remain raw. Unknown values use translated `Unknown tag: {value}` while retaining the raw value as literal text. User-authored tags, names, descriptions and collection names are not translated. The Setup badge starts with a keyed checking state, then reports the saved location and any unsaved selection. Casting chip accessible names and titles also repaint after live locale switches using real runtime message values.

## Automated evidence

- Fail-first: all four new intro tests failed before implementation because the actual new modules were absent. Tests then load real i18n and real feature modules, not replacement helpers.
- Focused suite: **122 tests, 0 failures, 0 errors, 0 skips**. Includes intro behavior, all catalogs, help coverage, asset registration, unchanged markup ratchets, Casting handlers, and frontend/guidance regressions.
- Sliced/JS harness scan: **2119 tests, 1 failure, 9 errors, 66 skips** across 231 modules. Selection includes every `test_*_js.py` plus test modules referencing Node execution, VM evaluation, JS fixtures, fixture HTML or browser/headless harnesses. This includes table themes and state-review fixtures. Executed from `app/` with native Python/Git Bash on PATH. This is NOT an all-green suite.
- Wider-run failures were reproduced using unchanged files exported from base `46663c30`: **32 tests, 1 failure, 9 errors, 1 skip**. They include Dataset's Windows-invalid quoted directory (`WinError 123`), four native-Bash launcher subcases (exit 126), three pipe-selector subcases (`WinError 10038`), missing `windows_subprocess_owner` in an asset verifier fixture, and benchmark temporary-directory cleanup (`WinError 32/5`). They were not weakened or fixed in this UI patch. The baseline export could not materialize Linux ECAPA symlinks on Windows; these controls do not depend on those links.
- Both previously broken LLM slices now load actual i18n, with DOM attribute support added to their fixtures. The new taxonomy module is loaded by the real Casting fixtures. Markup baselines were not raised; new modules have zero baselines.

Commands (native interpreter, `AA2_PYTHON` and `PYTHONUTF8=1`):

```
cd app
python -m unittest tests.test_advanced_tool_intros_js tests.test_ui_catalog_parity tests.test_ui_help_js tests.test_local_ui_assets tests.test_ui_markup_gate tests.test_casting_ui_js tests.test_casting_research_js tests.test_frontend_regressions tests.test_ui_guidance_js
```

Broad runner and logs are local TEMP artifacts: `run-intros-harnesses.py`, `advanced-intros-all-harnesses-final.log`, `advanced-intros-harness-summary.json`, `advanced-intros-baseline-controls.log`, `advanced-intros-focused-final.log`.

## Running-app browser evidence

Native Windows app, fresh isolated scratch data, port 60858. Only a synthetic designed-voice manifest and silent WAV were used; no real book, voice recording, credentials, model calls or GPU jobs were used. Tool panels were displayed for measurements/screenshots without starting GPU actions. This is UI evidence, not backend training qualification.

- **630/630 live combinations passed:** seven tools x five locales (`en`, `zh-CN`, `ja`, `es-419`, `qps-ploc`) x six themes (`light`, `night`, `super-night`, `cyberpunk`, `bread`, `leek`) at each of 360px, 390px and 1280px.
- Checked exact translated heading/body against real message tokens, intro bounds/no horizontal overflow, covered help, computed text/card contrast, and >=44px disclosure targets at phone widths. Lowest measured contrast: **11.14:1**. Desktop target height was not treated as a phone requirement.
- **42/42 fresh-load combinations passed:** all seven intros, all six persisted themes, 360px, pseudo-English; no intro overflow and all targets >=44px.
- All seven disclosures toggle and persist through reload; a fresh unset preference opens all seven. A throwing localStorage getter still permits all controls to collapse/reopen. Hash stayed unchanged.
- Real Casting API/UI loaded the synthetic `Female`/`Adult`/`calm`/`warm` voice. Chinese labels appeared while raw option values and filter selections stayed unchanged. A live switch through every locale, including pseudo-English, repainted chip text, accessible names and titles while its raw filter key remained unchanged. The unknown-value fallback retained `future-tag-42`. The LLM badge also repainted under pseudo-English.

### Local screenshots (not committed)

28 PNGs under `ui-intros/`, four per tool. For each of Designer, Casting, Preparer, Dataset, Training, Voice Lab and Reports:

- `<tool>-desktop-light-en.png` (1280px)
- `<tool>-desktop-night-zh-CN.png` (1280px)
- `<tool>-phone-light-en.png` (390px)
- `<tool>-phone-night-zh-CN.png` (390px)

This is paired screenshot coverage, not a full locale/theme Cartesian screenshot set; automated layout checks cover the full live matrix above. Representative full-size screenshots and a 28-image contact sheet were visually inspected. PNGs are excluded locally and are not in the commit.

## Readiness boundary

The initial setup `ready.sh` installed hooks, then its local-unit run was stopped after approximately 25 minutes. Baseline shard 2 completed with 2277 tests, 11 failures, 45 errors and 150 skips; other shards were incomplete.

The final native-Git-Bash `ready.sh` run was stopped by its exact owned root PID after **25m14s**. Shard 1/6 completed **2258 tests, 24 failures, 67 errors, 96 skips**; shard 2/6 completed **2150 tests, 13 failures, 32 errors, 147 skips**. Shards 3/6 and 4/6 were unfinished; 5/6 and 6/6 had not started. This is **not release-ready qualification**. The full-run failure set was not exhaustively triaged in this UI task. Artifacts: local TEMP `alexandria-local-units/5deb64dfb81f46209cf144e9fb7da97f`, with `advanced-intros-final-ready.log` and `advanced-intros-ready-summary.json`.

Readiness uses an immutable source snapshot (`4ce9d9174411c46807e530051e4b81a3f56e61f0`, parent `46663c30`). The later badge-loading/accessibility refinements are covered by the final focused and broad harness runs, not claimed qualified by that earlier snapshot. Derived artifacts were regenerated after the final edits. The scratch app was stopped by PID and its listener closed. Hosted checks/review are separate and are not claimed green.

No native screen-reader, physical-phone, Firefox/Safari, actual GPU/training, audiobook generation or audio-quality qualification was performed.

## English intros (review copy)

### Designer

**What is Designer for?**

> Create a new voice by describing how it should sound, then listen to a short sample and save it for use in Voices. Use this when you want to invent a voice; if you already have a recording to copy, use the import controls in Voices instead.

What you need first:

- A voice description and a sentence to read aloud.
- A speech model that supports voice design, set up in Configuration.

### Casting

**What is Casting for?**

> Browse saved designed voices across books, listen to samples and keep a shortlist. Use this to find or reuse a voice; create a new one in Designer instead. Review marks and collections organize your choices without changing a book until you confirm an assignment.

What you need first:

- Saved designed voices; listening also needs a saved audio sample.
- An active book with characters if you want to assign voices.

### Preparer

**What is Preparer for?**

> Turn a WAV or MP3 recording into short audio clips with matching text, packaged as a training ZIP. Use this when you have a recording to teach a voice; for clips made from a voice description, use Dataset instead.

What you need first:

- A WAV or MP3 recording with speech you want to use for training.
- The preparation tools installed; some options need extra models.

### Dataset

**What is Dataset for?**

> Build a training ZIP by generating and checking short speech samples from a voice description. Use this to make synthetic training examples, not to split an existing recording (use Preparer for that). You can listen, edit sample text and regenerate clips before exporting.

What you need first:

- A dataset project with a voice description and sample sentences.
- A working speech model that supports voice design.

### Training

**What is Training for?**

> Teach the speech model a voice using a training dataset, then test the resulting voice add-on (LoRA adapter). Use this after checking your samples; it does not turn a book into an audiobook. For normal book reading with an existing voice, use Voices and Audio Editor instead.

What you need first:

- An uploaded training ZIP with audio clips and matching text.
- The training tools and model files installed, with enough memory to run them.

### Voice Lab

**What is Voice Lab for?**

> Run several voice-training steps in order, such as checking clips, grouping matching voices, training and comparing results. Use this for a collection of datasets; use Training for a single dataset you want to train by hand. Depending on the steps you choose, it produces combined datasets, trained voice add-ons and reports.

What you need first:

- Dataset ZIPs and the input folders required by your chosen steps.
- Most steps need a supported GPU, installed tools and model files; check Pipeline Settings first.

### Reports

**What are Reports for?**

> Read saved script-review reports, inspect past runs and review checkpoints, or run a benchmark from a manifest. Use this to understand what happened and compare runs; it does not generate your audiobook. It shows saved findings and, when you run a benchmark, creates new benchmark results.

What you need first:

- A saved report, run or review checkpoint to inspect.
- To run a benchmark: a valid benchmark manifest and the tools and models it requires.
