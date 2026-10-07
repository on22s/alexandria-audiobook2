# Batch 11: preserve UI drafts and response ownership

This batch fixes background UI responses that disturbed active edits, reported the wrong book's render outcome, or saved voice metadata against stale preview audio.

- [x] #777: reject invalid or duplicate editor chunk IDs with a controlled repair response, preserving saved state and audio.
- [x] #840: update the chunk action container when an expanded row finishes generating without replacing its pause label.
- [x] #841: reconcile optimistic generating badges after cancellation while retaining the existing row controls.
- [x] #842: preserve unsaved text, delivery, pause and speaker choices, focus/selection and expansion during a full snapshot refresh, by book and row UID. Keep the server's normalized saved fields in the cache.
- [x] #849: show paused activity without a misleading active ETA or model-wait message.
- [x] #850: retain focus and selection in the active style field after voice suggestion cards reorder.
- [x] #851: refuse stale comparison responses after another request, book change, editor change or panel closure.
- [x] #854: ignore an editor snapshot from the previous book before painting or changing the current cache.
- [x] #858: handle rejected clone playback promises with a visible warning.
- [x] #860: require Designer preview audio to match the description and sample text being saved, including edits made during generation or saved-voice loading. Name-only edits remain allowed.
- [x] #914: count completed batch rows and run drift checks only for the starting book and selected row UIDs.

Validation: 67 focused tests pass without skips against main `3bad0e43` plus these fixes. Tests exercise actual route/handler code with malformed persisted rows, deferred responses, media rejection and DOM/API fixtures. They cover draft retention after an actual successful row save invalidates the snapshot revision, UID reindexing, and refusal to carry drafts into another book or replacement UID. Browser tests use Node fixtures; no native browser or GPU/TTS inference was run. Full release checks passed: 6,966 unit tests across three shards without unit skips and 70 quick API checks; 12 full-mode API cases skipped. Older Designer fixtures now provide preview-input provenance and load the real synthesis-input helper; the action-row fixture targets the scoped action container. Adapter deletion fixtures use their own temporary task-ownership directory, preserving the real ownership guard while preventing cross-shard interference.
