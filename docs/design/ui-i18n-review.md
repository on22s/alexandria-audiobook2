# Reviewing a UI translation

This is the procedure for a native reviewer taking one locale from machine draft
to reviewed. The terminology to check against is
[the UI translation glossary](ui-i18n-glossary.md).

## Where the catalogs live

- `app/static/i18n/ui.zh-CN.json`, `ui.ja.json` and `ui.es-419.json` are the
  three translated catalogs. Each is flat JSON: one dotted key per message, the
  translated string as the value, plus a `_meta` object.
- `app/static/i18n/ui.en.json` is **generated** from `app/static/index.html` and
  every `t('key', 'English')` call site. Never hand-edit it. Regenerate with
  `python tools/i18n/extract_ui_catalog.py`; `--check` reports a stale file.
- English is the runtime fallback and is not a catalog: a missing, empty or
  placeholder-mismatched value renders the authored English instead of a raw
  key. A translation phase can therefore merge safely.

## What `_meta.review_status` means

```json
"review_status": {
  "all": "machine-draft",
  "safety": "not-reviewed"
}
```

- `all` is the whole-catalog verdict: `machine-draft` or `human-reviewed`.
  `app/static/js/app-i18n.js`'s `isUiLocaleDraft()` marks the picker option
  **(draft)** unless `all` is `human-reviewed`; it fails closed while the
  catalog is still loading.
- `safety` covers the `safety.*` keys only: `not-reviewed` or
  `human-reviewed`. The parity test refuses a locale whose `all` is
  `human-reviewed` while its `safety` is not.
- `reviewers` is the list of people who signed off. Record names there in the
  same change that flips the flags.
- To remove the **(draft)** label: review every value, then set `all` to
  `human-reviewed`, set `safety` to `human-reviewed` once the safety keys below
  have been reviewed too, and add the reviewer names. `test_ui_catalog_parity.py`
  has a test (`test_no_locale_claims_a_review_it_does_not_have`) that currently
  pins all three catalogs to `machine-draft`; update that assertion and the
  draft wording in the READMEs in the same change.

## Safety keys need extra care

Every key starting with `safety.` protects data or describes an outcome that is
unknown, unconfirmed or destructive. Review these first:

- The translation must keep the same consequence and the same strength: a
  refusal stays a refusal, "cannot be undone" is not softened, and an
  "unconfirmed / outcome unknown" line must not read as success.
- A translated safety message is shown with the authored English beneath it
  (`tSafety` / `renderUiSafety`). Read both lines together and check that the
  English under it still reads as the same warning to someone who cannot read
  the translation.
- Placeholders (`{count}`, `{detail}`, …) must match the English set exactly.
  Backticked identifiers, API paths, file extensions and `env:` names are
  protected tokens: never translate them.
- Destructive confirm titles and action labels name the control; the body
  carries the consequence. Keep that split.
- The glossary's per-phase `safety.*` tables list every safety key with its
  English source; review them phase by phase rather than as one flat list.

## Drift checks that apply to the whole catalog

Beyond the glossary table, these are the failures earlier phases hit:

- Japanese "voice" is **声**; ボイス is allowed only inside ボイスデザイン and
  ボイスラボ. "Export" is **書き出し**, never エクスポート.
- Chinese "voice" is **声音**, never 配音; a take is **录音**.
- Spanish addresses the reader with **tú** (Déjalo, Verifica, no Déjelo /
  Verifique). "Source text" is **texto fuente**, not `origen`; `origen capturado`
  for a captured cover/catalog origin is correct.
- A translated value must not end in an ASCII period in zh-CN or ja: use the
  full-width mark (。！？) or the ellipsis `…`.
- Separately translated sentences are joined by `joinUiSentences(parts)` in
  `app-i18n.js`, never by hand-adding `' '`. For markup, the whitespace between
  two adjacent translated inline elements is collapsed for zh-CN and ja by
  `applyUiSentenceJoins()`, the same rule's DOM form.

## Run the checks

```bash
python tools/i18n/extract_ui_catalog.py --check
cd app
python -m unittest tests.test_ui_catalog_parity -v
python -m unittest tests.test_ui_i18n_js -v
```

`test_ui_catalog_parity.py` enforces key-set equality in both directions,
placeholder and protected-token parity, the inline-markup allowlist, no
untranslated copy, equal `.one`/`.many` values for zh-CN and ja, and the safety
review rule. Its `KEEP_ENGLISH` set lists the values allowed to equal English
(brand names and format-only messages); adding an entry needs a written reason.
Then run the whole suite (`python -m unittest` from `app/`) because several
modules load `app-i18n.js` and `index.html` directly.
