# Batch 13: Setup, dataset, training and report UI

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#808](https://github.com/on22s/alexandria-audiobook2/issues/808) — Non-object state.json crashes the configuration endpoint
- [ ] [#826](https://github.com/on22s/alexandria-audiobook2/issues/826) — Late existing-upload responses overwrite newer frontend book selection
- [ ] [#827](https://github.com/on22s/alexandria-audiobook2/issues/827) — Stale saved-script refresh overwrites the newer Batch Review picker
- [ ] [#828](https://github.com/on22s/alexandria-audiobook2/issues/828) — Prompt preset save posts stale LLM profile instead of edited Setup fields
- [ ] [#829](https://github.com/on22s/alexandria-audiobook2/issues/829) — Pending Auto-Configure overwrites newer edits to TTS form settings
- [ ] [#836](https://github.com/on22s/alexandria-audiobook2/issues/836) — Attribution prompt preview accepts stale responses after newer prompt edits
- [ ] [#837](https://github.com/on22s/alexandria-audiobook2/issues/837) — Pass prompt preset named default collides with built-in selection and loses contents
- [ ] [#838](https://github.com/on22s/alexandria-audiobook2/issues/838) — Configuration load retry discards edits entered after the initial load failure
- [ ] [#839](https://github.com/on22s/alexandria-audiobook2/issues/839) — Navigation auto-close uses 768px despite navbar-expand-lg collapsing below 992px
- [ ] [#843](https://github.com/on22s/alexandria-audiobook2/issues/843) — Task outcome fallback interprets Errors: 0 summaries as failures
- [ ] [#848](https://github.com/on22s/alexandria-audiobook2/issues/848) — Cancel plus lost batch-start acknowledgement falsely reports generation never started
- [ ] [#855](https://github.com/on22s/alexandria-audiobook2/issues/855) — Empty chapter-export listing leaves stale chapter and ZIP download controls

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
