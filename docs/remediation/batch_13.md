# Batch 13: Setup, dataset, training and report UI

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

All twelve fixes are implemented on main `42a52db5`. Integrated focused verification passed 55 tests without skips. Full release verification passed 7,019 unit tests and 70 quick API checks; 12 full-mode API checks were explicitly skipped.

- [x] [#808](https://github.com/on22s/alexandria-audiobook2/issues/808) — Non-object state.json crashes the configuration endpoint
- [x] [#826](https://github.com/on22s/alexandria-audiobook2/issues/826) — Late existing-upload responses overwrite newer frontend book selection
- [x] [#827](https://github.com/on22s/alexandria-audiobook2/issues/827) — Stale saved-script refresh overwrites the newer Batch Review picker
- [x] [#828](https://github.com/on22s/alexandria-audiobook2/issues/828) — Prompt preset save posts stale LLM profile instead of edited Setup fields
- [x] [#829](https://github.com/on22s/alexandria-audiobook2/issues/829) — Pending Auto-Configure overwrites newer edits to TTS form settings
- [x] [#836](https://github.com/on22s/alexandria-audiobook2/issues/836) — Attribution prompt preview accepts stale responses after newer prompt edits
- [x] [#837](https://github.com/on22s/alexandria-audiobook2/issues/837) — Pass prompt preset named default collides with built-in selection and loses contents
- [x] [#838](https://github.com/on22s/alexandria-audiobook2/issues/838) — Configuration load retry discards edits entered after the initial load failure
- [x] [#839](https://github.com/on22s/alexandria-audiobook2/issues/839) — Navigation auto-close uses 768px despite navbar-expand-lg collapsing below 992px
- [x] [#843](https://github.com/on22s/alexandria-audiobook2/issues/843) — Task outcome fallback interprets Errors: 0 summaries as failures
- [x] [#848](https://github.com/on22s/alexandria-audiobook2/issues/848) — Cancel plus lost batch-start acknowledgement falsely reports generation never started
- [x] [#855](https://github.com/on22s/alexandria-audiobook2/issues/855) — Empty chapter-export listing leaves stale chapter and ZIP download controls

The targeted regressions reproduce the original failures and verify corrected behavior, including stale errors, rejection paths and normal recovery controls.

Implementation: retain malformed-state diagnostics without crashing config GET; serialize upload selection writes; reject stale picker/preview replies; save the edited active LLM profile; preserve TTS edits during auto-configuration; reserve the checked-in prompt identity while preserving colliding legacy presets; confirm configuration-draft replacement and reject edits during reload; match the navbar breakpoint; recognize zero-error summaries; monitor uncertain batch starts and retain cancellation; clear empty chapter exports.

Validation uses actual JavaScript handlers in Node with controlled DOM and transport boundaries, plus native HTTP/state and export-list tests. Native browser, hardware probes and GPU inference are not claimed.
