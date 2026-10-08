# Batch 15: benchmark evidence and failure reporting

Failed generation manifests retain a failed-chunk record even when detailed attempt telemetry is absent. Fixture IDs include stable source-path identity, and contained audio names beginning with two dots remain admissible without allowing path escapes.

Remote checkout output is framed so hexadecimal login banners cannot masquerade as Git revisions. Runtime observations must still match the verified clean checkout. Remote Linux staging and worker paths use POSIX semantics even on a Windows controller; local source paths retain native semantics. Runtime diagnostics retain complete branch names. Benchmark registration failures publish a failed status after releasing their claims.

Review cleanup refuses deletion errors instead of reporting files removed. Gold builders, mergers and rejudgers publish atomically with direct-move fallback disabled only for these outputs; other atomic-writer callers retain their existing fallback policy.

Unit shards record executed test identities. The release report validates identity receipts, and the final checker rejects duplicated, omitted, unknown or missing identities alongside its existing count/status checks. Two imported fixture classes previously caused 19 duplicate executions; module imports preserve their fixture methods while discovering each original test once. A discovery regression covers all four affected modules.

Validation against main `857542fa`: 7,025 unique unit tests pass across three shards, with exact-once inventory coverage independently checked. All 70 quick API checks pass; 12 full-mode API cases are explicitly skipped. Focused checks include 125 remote/registration tests, 126 integration tests and 59 discovery/publication/checkpoint tests, without skips. Git and filesystem checks use disposable native Linux artifacts; Windows controller paths are simulated. No native Windows/browser or GPU/model inference is claimed.

Tracker: #917. Remote training staging cleanup (#768) was already merged through #920 and is not reimplemented here.

- [x] Fixes #761
- [x] Fixes #764
- [x] Fixes #765
- [x] Fixes #766
- [x] Fixes #767
- [x] Fixes #769
- [x] Fixes #771
- [x] Fixes #778
- [x] Fixes #782
- [x] Fixes #819
- [x] Fixes #874
