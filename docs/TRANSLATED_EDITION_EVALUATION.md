# Translated edition evaluation (#1731)

**Status: evaluated as a bounded, read-only proposal preview. Real
translation quality was not measured.**

## Inventory: what AA2 had before this work

Measured on `main` at `02aadde9` (greps run 2026-11-06):

- AA2 has no translation feature and no translation dependency. Every
  `translat` hit in `app/*.py` is a string `.translate()` call (character
  folding), translator front-matter stripping in `source_normalization.py`
  (removing a human translator's essay/credit from uploaded text), or a
  prose comment. No `argostranslate` appears in any requirements file, app
  module, or router (verified by repository-wide search at `02aadde9`).
- AA2 already has several unrelated language settings: the Setup UI locale
  (`setup-ui-locale`: English vs Pseudo-English for UI finder text), the TTS
  synthesis language (`tts-language`), the Preparer source language
  (`prep-lang`), and the LoRA codec language (`lora-language`). None of them
  is a translation setting and none describes book text in two languages at
  once. This preview adds two local proposal fields (`source_language`,
  `target_language`) and writes no setting of any kind.
- The saved-book artifacts this evaluation builds on were already stable:
  `annotated_script.json` (rows of `{speaker, text}`), the book snapshot
  identity in `app/book_state_transaction.py` (`get_book_snapshot` /
  `get_book_operation_token`), and atomic flat-JSON writes via
  `atomic_json_write` in `app/utils.py`. Book edits are already fenced by
  those mechanisms; the translation preview reuses them instead of
  inventing new ones.

## What was built (this slice)

- `app/translated_edition.py` — proposal builder (no TTS, no setting writes).
  `get_installed_pairs()`
  reports directly installed Argos packages from the local install
  directory only (`argostranslate.package.get_installed_packages`), and
  never touches the remote index, a download, or a pivot through an
  intermediate language. `build_proposal()` takes an injected translator
  and translates a bounded excerpt of the first **25** Script rows, one
  row at a time, checking a caller-supplied cancellation check between rows
  and again immediately before publication. A cancel that arrives before
  that check is saved as `cancelled`; one that arrives after the proposal
  has been written leaves the finished status, so the panel says "cancel
  requested", not "saved as cancelled".
  Speaker labels are carried untranslated; rows with control tags or markup
  are flagged `unsupported` and carried unchanged; dropped/changed numbers
  (numbers matched as `\d+(?:[,.]\d+)*`, so sentence punctuation after a
  number is not a change), empty output, and copied source are flagged for
  review. Any saved-Script row without a string `text` refuses the whole
  build: tracing `generate_script.py`, the manual-injection route and the
  three-pass splitter shows every app path that writes a saved Script makes
  `text` a string, the Editor edits `chunks.json` (not the Script) with a
  non-null string-typed field, and `app/project.py:386`'s coercion operates
  on those chunks — so a non-string row means a hand-edited or corrupt file,
  and the build fails loud rather than coerce-translating it. Partial or
  cancelled runs are never labelled complete, and a fully translated prefix
  of a longer Script is labelled `excerpt_complete`, not `complete`. The
  proposal carries its own id, the source script SHA, both language codes,
  the saved runtime and package versions, and per-row status, and is saved
  atomically under the book's `translation_proposals/` folder — never over
  Script or source. The Script is read and the proposal published under the
  book operation lock (`ensure_book_state`), with the book operation token
  rechecked immediately before publication; a proposal whose `book_id` is
  not the current book is refused on read. Loading a different saved book
  from the library removes that folder after the switch commits (only then:
  `scripts_library._discard_another_book_proposals` runs only when the
  loaded book's id differs), on a best-effort basis (a failed
  removal is logged, never an error: an unreadable or stale folder cannot be
  read as the new book's because the `book_id` check refuses it); reloading
  the same saved book keeps its proposals (the stale check still applies).
  Selecting an uploaded source does **not** remove the folder: old proposal
  files stay on disk until the next different-book library load, and are
  never shown for the new book because the `book_id` check refuses them. A
  corrupt or unreadable active workspace never blocks a load: the previous
  identity is treated as unknown and its proposals are discarded. Proposals
  live in the active book's folder only: saving a book to the library does
  not copy them into the saved book, so switching away and loading the saved
  book again does not bring them back. `GET
  /api/translated_edition/latest-proposal` returns the active book's most
  recent saved proposal (with its `stale` flag), or a clear `proposal:
  null` when there is none; the panel loads it whenever it opens with no
  in-memory proposal, so a kept proposal is reachable again after a reload.
  Malformed proposal files are reported by name in `malformed_proposals`
  and never hide a good one. There is still no proposal history or list UI:
  only the most recent proposal per book is re-openable. Stale source is
  detected by comparing the
  current script SHA; a busy book makes the re-check refuse with "book is
  busy" rather than claiming the proposal is stale.
- `app/routers/translated_edition.py` — `GET /api/translated_edition/pairs`,
  `POST /api/translated_edition/proposals`, and
  `GET /api/translated_edition/proposals/{id}` (reads back one proposal
  with a `stale` flag),
  `GET /api/translated_edition/latest-proposal` (the most recent proposal
  for the active book, or `proposal: null`), plus
  `POST /api/translated_edition/proposals/{cancel_token}/cancel` for a
  running preview. Only one preview runs at a time: a second concurrent
  create gets 409 rather than loading a second translator/model. The
  preview is CPU-only by construction: `build_argos_translator` refuses any
  effective Argos device other than `cpu`, because upstream
  `argostranslate/settings.py` defaults `ARGOS_DEVICE_TYPE` to `cpu` but a
  user setting `cuda` would otherwise run CTranslate2/Stanza GPU inference
  (the only two GPU paths in upstream `translate.py`, both gated on
  `settings.device`) outside `process_state` and the GPU lock. Registered like the other routers in `app/app.py`;
  `app/api_contract/{openapi,routes}.json` regenerated with
  `app/update_api_contract_snapshots.py`. The create endpoint contains no
  approve or publish action.
- Script-tab panel (`app/static/js/app-translated-edition.js`, vanilla JS):
  an explicit pair picker populated from the pairs endpoint, source/proposal
  rows labelled "proposal, not approved", field help entries, live feedback,
  a cancel control for a running preview, a re-check that surfaces the
  stale-source warning, and a reset when the selected book changes. It does
  not change UI, source, or TTS language settings.
- Tests (`app/tests/test_translated_edition*.py`, synthetic text only, fake
  translator since no Argos pair is installed): unsupported pair,
  unavailable package, partial failure, cancellation (including from the
  HTTP route), Script/source/voices/config bytes unchanged, stale source, book
  switch during a build, known-bad translations (dropped number, empty,
  copied source), excerpt bound and `excerpt_complete`, late cancel saved as
  cancelled, script deleted mid-build refused, non-text rows refused,
  proposal-id traversal refused, a proposal from another book refused, the
  translator running off the event-loop thread, the panel's stale and
  cancel paths, duplicate cancel tokens refused, a busy book reported as a
  refusal rather than stale, and book load removing a previous book's
  proposals while a same-book reload keeps them, a corrupt or malformed
  active workspace never blocking the load, and a failed proposal-folder
  removal not failing a committed load, a trailing-punctuation number not
  flagging a correct translation, a non-CPU Argos device refused, a second
  concurrent preview refused, the most-recent-proposal lookup (including
  malformed files not hiding a good one and another book's proposal never
  returned), and toggling the panel during a build not discarding it.

## Licensing notes

From the Argos Translate project's own repository (README "License" section
and `argostranslate/package.py` at
`https://github.com/argosopentech/argos-translate/blob/master/`, fetched
2026-11-06):

- The `argostranslate` library is dual-licensed MIT or CC0 (README
  "License" section, `argosopentech/argos-translate`).
- The `.argosmodel` **packages are separate artifacts**, distributed via the
  project package index as CTranslate2/OpenNMT model bundles. Their
  `metadata.json` schema (as read in the project's `package.py`) contains
  package/version/language fields and **no license field**, so per-model
  license terms cannot be confirmed from the package metadata itself.
  Training corpora for OpenNMT models are typically OPUS-derived, whose
  licenses vary by corpus. **What could not be confirmed:** the license
  terms of any individual `.argosmodel` package, and whether any given
  package's training data may be unsuitable for our use. Before any
  package were installed or any translated output were shipped, each
  package's actual terms would need to be checked individually against
  its distributor's listing. (The public index page renders its package
  metadata client-side; it was not readable as static text at
  evaluation time.)
- Two Argos behaviors matter for honesty regardless of licensing:
  `argostranslate.translate.translate()` **automatically pivots**
  through intermediate languages when no direct package exists, and
  `get_available_packages()` **falls back to downloading the index**.
  This implementation refuses both: direct installed pairs only, and it
  never calls an index or download function.

## Limits

- No Argos pair is installed in the test environment, so every test runs
  through a fake injector. **Nothing here measures real translation
  quality** — not fidelity, not fluency, not speaker-name handling in
  any real model. The checks that exist (numbers, empty output, copied
  source, unsupported rows, stale source) are hygiene signals, not a
  quality claim.
- The excerpt is bounded at 25 rows (a reviewable prefix, not a book).
  There is no batching, no TTS integration, no whole-book run, and no
  path from proposal to approved edition in this slice.
- Speaker labels, names, and numbers are flagged/carried by simple,
  reviewable rules, not by any semantic policy; a human still has to
  read every row.
- Cancelling stops the preview after the current row; a row already sent
  to the translator is not interrupted. There is no list or history of
  older proposals: the panel shows the proposal it just built, or the most
  recent saved proposal for the current book when it opens. Loading a
  different saved book from the library removes the previous book's
  proposals with its other active-book artifacts; selecting an uploaded
  source leaves them on disk (never shown for the new book) until that
  different-book library load.

## Recommendation

Given the inventory above, the learning here is that AA2 already has
every mechanism this idea needs (snapshots, atomic writes, explicit
per-area UI, reviewable artifacts) — and lacked only the translation
step itself. The preview built for this issue is small enough to keep
as an opt-in, evaluation-only surface **provided** it stays labelled a
proposal, keeps no approve/publish path, and imports Argos optionally
with no new dependency. A real translated edition — with model
licensing checked per package, quality measured on known-bad controls
and human review, and a separately admitted generation step — is a
larger, separate decision than this slice and should not be implied by
it. If AA2 does not want to own that decision, the honest outcome is
to keep the evaluation and drop the feature.
