# Batch 14: Setup, dataset, training and report UI

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

All twelve fixes are implemented on main `42a52db5`. Validation: 92 integrated focused tests and full release gates passing 7,018 unit tests plus 70 quick API checks; 12 full-mode API checks explicitly skipped.

- [x] [#856](https://github.com/on22s/alexandria-audiobook2/issues/856) — Older chapter preset import can overwrite a later selected file
- [x] [#857](https://github.com/on22s/alexandria-audiobook2/issues/857) — Older report-list refresh can hide the newer result
- [x] [#859](https://github.com/on22s/alexandria-audiobook2/issues/859) — Overlapping saved-book loads let an older acknowledgement replace the current filename
- [x] [#861](https://github.com/on22s/alexandria-audiobook2/issues/861) — LoRA training progress stays red and static after a failed previous run
- [x] [#862](https://github.com/on22s/alexandria-audiobook2/issues/862) — Successful adapter download stays disabled as Downloading when list refresh fails
- [x] [#863](https://github.com/on22s/alexandria-audiobook2/issues/863) — Late LoRA comparison response replaces the newer adapter selection
- [x] [#864](https://github.com/on22s/alexandria-audiobook2/issues/864) — Late Dataset Builder sample result marks edited text done with old audio
- [x] [#865](https://github.com/on22s/alexandria-audiobook2/issues/865) — Batch preparer poll paints old-run statuses beside newly selected files
- [x] [#867](https://github.com/on22s/alexandria-audiobook2/issues/867) — Dataset project switch can save the previous project's rows into the destination
- [x] [#868](https://github.com/on22s/alexandria-audiobook2/issues/868) — Older LM Studio status response replaces completed optimization feedback
- [x] [#869](https://github.com/on22s/alexandria-audiobook2/issues/869) — Scientific-notation Dataset Builder seeds are truncated before generation and export
- [x] [#915](https://github.com/on22s/alexandria-audiobook2/issues/915) — Restoring the Voices tab duplicates in-flight startup resource requests

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.

Actual UI handlers were tested in Node with controlled DOM, transport, FileReader and storage boundaries, plus existing native HTTP/filesystem and full-suite coverage. Native browser and GPU inference are not claimed.

Changes preserve current-request ownership in imports, report lists and adapter comparisons; serialize saved-book loads; restore training/download controls; bind generated samples to their row definitions; protect Dataset Builder switching and preparer queue selection; preserve scientific-notation seeds; reject old LM Studio status replies; and coalesce pending navigation voice loads while retaining explicit refreshes and save guards.
