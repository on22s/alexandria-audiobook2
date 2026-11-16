# UI/UX Batch E acceptance

Date: 2026-11-16. Branch: ui/batch-e-clarity-scale. Baseline: 46663c30 (Batches A-D). Native Windows 11; supplied windows-hook-venv Python; installed Edge 157.0.4341.60 via local Playwright. No framework, new static asset, markup baseline increase, production book, or real voice recording.

## Acceptance checklist

| Audit item | Result | Evidence and boundary |
| --- | --- | --- |
| UX-21: three speakers, pick -> listen -> explicit Ready before advanced controls | PASS | Three synthetic clone cards selected the synthetic reference, observed actual audio playback, stopped it, and explicitly checked Ready while both advanced disclosures remained closed. Saved persona/voice statuses did not become approved. Playback uses a generated tone, not synthesized speech; this is UI/player qualification, not TTS quality. Seed advice now follows cards. |
| UX-21: retain advanced controls, distinct approvals, next blocker | PASS | Original persona, approval, versions, alias, seed and book-wide controls retained; closed/revealed screenshots and full matrix. Next-step guidance distinguishes explicit Ready from approvals. |
| UX-22: required Designer/Preparer feedback | PASS | Actual app functions with empty fields: persistent role=alert, aria-invalid, aria-describedby, first-invalid focus, retained sample/description values and zero doomed POSTs. Second Designer required field checked separately. |
| UX-22: malformed/valid Setup URLs and target policy | PASS | Empty, malformed, FTP and credential-bearing URLs rejected locally without POST; entered value retained; editing clears invalid state. Valid HTTP loopback URL follows original /api/llm/test path and returns its existing connection failure, without inline syntax error. Server saved-target/security policy unchanged. |
| UX-23: several comparable five-/1000-row probes, separated network | PASS measured / budget sign-off OPEN | Same Edge/machine and synthetic fixtures, baseline HTML/JS from git show, three runs after warmup. Performance API and long-task observer; table below. Engineering budget set before final probes: render task <250ms, scroll/filter task <1000ms, full redraw <15s. These are implementation acceptance targets, not independent owner agreement. |
| UX-23: no comparable 2451ms render/scroll/filter task | PASS | 1000-row render maximum 102-165ms, scroll no reported >=50ms task, filter maximum 364-523ms. All rows remain in DOM/cache; no pagination or scope reduction. Full redraw completion is slower, explicitly reported below. |
| UX-23: drafts, focus, selection, inspector, UID and scope | PASS with boundary | Real 1000-row forced refresh preserves unsaved draft, focused control, selection [3,9], expanded row, inspector selection/book token and valid target; exactly 1000 rows/cache/unique UIDs. Native harness proves 20-row yields, mutation busy state and cancellation on book switch. No actual GPU render/export was started; existing scope paths and guard suites remain exercised. |
| UX-25: restore >8s, content/position and repeated deletes | PASS | Two actual deletions, 9-second wait, durable count-bearing button, two LIFO restores, exact UID/text order. Unit receipt test also advances monotonic age to 120s. |
| UX-25: incompatible mutation, book/generation switch, refusal | PASS | Real server 409 after incompatible text change and generation change, byte-identical chunks on refusal; client book switch sends no restore. Unit tests independently cover book identity, generation, expiry, out-of-order restore, one-use replay and forged content. |
| UX-25: preserve bulk/destructive confirmations | PASS scoped | No bulk/destructive path or confirmation removed; single-row delete remains reversible without an extra dialog. Existing safety suites retained. |
| Six themes / all locales / widths / revealed state | PASS scoped | 288 Voices states: six fresh themes, five locales (including qps-ploc), closed/open controls, 1440/390/360 widths, plus live theme switches. Zero viewport overflow; enabled button/select/summary targets >=44px on phones. Labelled checkbox targets retain prior phone rules. |
| Contrast and translated inline feedback | PASS scoped | 60 fresh/live theme+locale states: axe color-contrast on essential help, next-step text, both disclosure summaries and field errors; no violations in these scopes. Persistent feedback repaints in all locales. |
| Inline error theme/locale/layout matrix | PASS scoped | 270 actual Setup/Designer/Preparer validation states: six themes, five locales, 1440/390/360 widths; first-invalid focus, aria-invalid, visible associated errors and no viewport overflow. |
| Durable Undo contrast/layout/locales | PASS scoped | 180 theme/mode/width/locale states, plus two generation/valid-URL checks; zero overflow, >=44px phone button, scoped contrast checks pass. |
| Full readiness / hosted qualification | OPEN | Readiness stopped at the 25-minute cap with failures and incomplete counts (see below); targeted/browser results do not establish release readiness or hosted status. |

## UX-23 timings

PerformanceObserver long-task durations include rendering/layout work after DOM updates. End-to-end elapsed is awaited refresh completion, not pure CPU time; ResourceTiming network duration is separate and is not subtracted to infer CPU. Observer entries start within the measured interval and are drained for 100ms afterwards. 0 means no task >=50ms reported, not zero work. Background local load was not controlled; these are observed local runs, not a clean benchmark distribution.

| Phase | Rows | Run | Refresh elapsed ms | Network ms | Maximum long task ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| before | 5 | 1 | 136.7 | 132.6 | 0 |
| before | 5 | 2 | 15.0 | 10.9 | 0 |
| before | 5 | 3 | 14.0 | 10.0 | 0 |
| before | 1000 | 1 | 4605.2 | 49.6 | 5098 |
| before | 1000 | 2 | 6708.0 | 53.4 | 7097 |
| before | 1000 | 3 | 5660.1 | 47.4 | 6033 |
| after | 5 | 1 | 19.5 | 9.2 | 0 |
| after | 5 | 2 | 192.8 | 188.8 | 0 |
| after | 5 | 3 | 13.1 | 9.4 | 0 |
| after | 1000 | 1 | 6286.2 | 50.8 | 105 |
| after | 1000 | 2 | 10319.5 | 49.6 | 165 |
| after | 1000 | 3 | 6540.4 | 48.9 | 102 |

Responsiveness improved; total completion did not: baseline 4.61-6.71s versus incremental 6.29-10.32s for 1000 rows. The table stays inert/aria-busy during replacement, and navigation/the rest of the page can run between batches. Do not describe this as faster total rendering. A browser regression probe caught focus loss while inert; focus capture/restoration was repaired and the real 1000-row probe rerun successfully.

| Operation (1000 rows) | Before maximum task ms, runs 1/2/3 | After maximum task ms, runs 1/2/3 |
| --- | --- | --- |
| Scroll through six positions | 0 / 0 / 0 | 0 / 0 / 0 |
| Drift filter on then off | 648 / 575 / 458 | 523 / 519 / 364 |

## Regression tests

- Fail-first on baseline: 6 tests run, 4 failures and 2 errors, covering all four UX IDs (missing UI contracts and missing multi-item/revision behavior). No baseline was raised.
- Final focused suites: 111 tests run, 111 passed, 0 failed/errors/skips (Batch E, chunk refresh, Preparer controls, action guidance, catalog parity, help, markup gate, local assets, editor review regression and take-history policy). Final source also ran the editor-take extension: 120 tests total, 119 passed, 0 failures, 1 WinError 1314 symlink-privilege error, 0 skips. The extension is overlapping evidence, not an additional 120 passes.
- Final review caught an audio-retention boundary still using the previous 60-second TTL. A fail-first 120-second ownership assertion reproduced the loss; receipt validation and retained-take ownership now share one 1800-second constant. The existing take-history route test additionally proves archive deletion is refused at 120 seconds and allowed only after expiry.
- Expanded real-source harness census: 305 Python modules, including every _js.py, node/vm/slice/fixture markers and transitive inherited harness consumers. Initial 205-module direct run: 1463 tests, 1 failure, 8 error outcomes, 3 skips. Nine-test Batch E module passes in the focused run. Ten previously missed inherited _js.py modules: 79 tests passed, 0 failures/errors/skips; these overlap the expanded delta. Readiness found the Preparer fixture missing native removeAttribute; repaired its DOM shape without a helper stub.
- Expanded 99-module delta: 1378 tests, 42 failure outcomes, 38 error outcomes, 38 skips; no failures/errors in the inherited _js.py modules. All failing modules are outside the changed test files; this does not prove every failure is baseline-only. Their native fixture/platform/provenance failures remain unqualified. This deliberately overinclusive census also includes native and pure-Python fixture suites.
- The initial broad-run failures were traced to unchanged native fixtures: illegal quoted Windows directory (WinError 123); four Bash executable fixtures (exit 126); three pipe/select cases (WinError 10038); copied Whisper CLI fixture omits windows_subprocess_owner. These are reported, not skipped or fixed away.
- Source dependency fixture fixes load real app-i18n/core helpers; no new helper stubs. DOM fixture shapes now implement the native methods called by the real helper.

## Source relocation

Old audit line numbers were not used as edit targets. Current implementation entry points: app/static/index.html (Voices list before advanced; durable Undo outside tools); app/static/js/app-core.js:274 (field feedback), :5190 (voice card), :8333 (snapshot refresh), :8689 (durable recovery); app/static/js/app-scripts.js (Designer validation); app/static/js/app-workbench.js (Preparer validation); app/routers/editor.py:769 and :795 (receipt/revision fencing).

## Evidence and limitations

Only this ACCEPTANCE.md is committed from ui-batch-e/. Local before/after PNGs, scripts, logs and JSON remain untracked. Representative captures: before-voices-three.png, after-voices-essential.png, before-designer-validation.png, after-designer-inline.png, before-setup-validation.png, after-setup-inline.png, before-editor-delete.png, undo-light-fresh.png, before-editor-1000.png, after-editor-1000.png; matrix-* captures include phone and pseudo-English revealed states. performance.json, scale-interactions.json, browser.json, contrast.json, undo-matrix.json and validation-matrix.json retain structured observations.

Shared preview automation failed twice with "Preview automation evaluate timed out after 15000ms." The shared tab was closed and local Playwright/Edge used instead. No native screen-reader, physical touch, Firefox/Safari, synthesized-speech quality, GPU admission, completed audio export or cross-tab concurrency qualification is claimed. Existing guards were retained; these are distinct from full release readiness.

## Final readiness

Final ./ready.sh ran through Git-for-Windows Bash with the supplied AA2_PYTHON, from 10:18:19 to the approximately 25-minute cap at 10:43:20 (America/Chicago). Hook installation, Python compile, derived staging and unit inventory passed; release qualification did not. Its shutdown aggregate reports 8790 tests, 312 failure outcomes, 280 error outcomes and 579 skips, counts_complete=false. Shards 1/2/3 completed; shard 4 was interrupted; shards 5/6 did not run. These are incomplete execution counts, not a passing suite. The readiness snapshot predates the repaired Preparer DOM fixture and the final shared Undo/audio-retention TTL correction; final focused tests exercise those fixes. Broad readiness failures have not all been adjudicated against baseline. Some JS subprocesses in the readiness snapshot exited with Windows code 3221225794 without JS diagnostics; the direct harness runs are recorded separately. No full readiness rerun or hosted success is claimed.

Owned app PIDs 11392 and 22508 were stopped by PID; both listeners are closed (cleanup.json and validation-cleanup.json). Readiness processes and workers were checked absent after shutdown. GOALS.md is unchanged; only ACCEPTANCE.md is committed from the evidence directory. Performance budget owner sign-off remains OPEN; no GPU/export or physical-device qualification is inferred.
