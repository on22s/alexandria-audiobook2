# Batch 14: Setup, dataset, training and report UI

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#856](https://github.com/on22s/alexandria-audiobook2/issues/856) — Older chapter preset import can overwrite a later selected file
- [ ] [#857](https://github.com/on22s/alexandria-audiobook2/issues/857) — Older report-list refresh can hide the newer result
- [ ] [#859](https://github.com/on22s/alexandria-audiobook2/issues/859) — Overlapping saved-book loads let an older acknowledgement replace the current filename
- [ ] [#861](https://github.com/on22s/alexandria-audiobook2/issues/861) — LoRA training progress stays red and static after a failed previous run
- [ ] [#862](https://github.com/on22s/alexandria-audiobook2/issues/862) — Successful adapter download stays disabled as Downloading when list refresh fails
- [ ] [#863](https://github.com/on22s/alexandria-audiobook2/issues/863) — Late LoRA comparison response replaces the newer adapter selection
- [ ] [#864](https://github.com/on22s/alexandria-audiobook2/issues/864) — Late Dataset Builder sample result marks edited text done with old audio
- [ ] [#865](https://github.com/on22s/alexandria-audiobook2/issues/865) — Batch preparer poll paints old-run statuses beside newly selected files
- [ ] [#867](https://github.com/on22s/alexandria-audiobook2/issues/867) — Dataset project switch can save the previous project's rows into the destination
- [ ] [#868](https://github.com/on22s/alexandria-audiobook2/issues/868) — Older LM Studio status response replaces completed optimization feedback
- [ ] [#869](https://github.com/on22s/alexandria-audiobook2/issues/869) — Scientific-notation Dataset Builder seeds are truncated before generation and export
- [ ] [#915](https://github.com/on22s/alexandria-audiobook2/issues/915) — Restoring the Voices tab duplicates in-flight startup resource requests

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
