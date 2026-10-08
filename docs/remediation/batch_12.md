# Batch 12: voice and cast UI ownership

Narrator strategy writes are serialized and book-bound; previews accept only current requests and inputs. Cast matching and submission retain their originating context, and superseded cast-library reads cannot hide newly created casts. Voice suggestions are discarded after cast/book changes. Bulk Apply separates successful books from failures instead of reporting an all-success result.

Upload queue rebuilding keeps remaining narrator drafts by local File identity or stored filename. Generation and preflight use selected-book state rather than message styling. Failed cast-list retrieval keeps editor rows and reports unknown status. Unchanged clone saves preserve existing default style and persona description.

Voice-state Apply allocates distinct bounded IDs without overwriting existing versions, and reuses matching versions on repeat Apply. Version creation, timeline save and clear validate captured book tokens under the shared book-state lock. Optional tokens keep legacy API calls supported; the UI requires a captured token. The OpenAPI snapshot documents the new optional fields and clear query parameter.

Validation: 150 integrated focused tests pass without skips against main `82e0173f`, including API contract snapshots and real book-preflight admission. Full combined-tree checks passed: 7,007 unit tests across three shards without unit skips and 70 quick API checks; 12 full-mode API cases skipped. Tests use actual handlers, routes and persisted artifacts with controlled DOM/transport/model boundaries; no native browser or GPU/TTS inference was run.

Tracker: #917.

- [x] Fixes #830
- [x] Fixes #831
- [x] Fixes #832
- [x] Fixes #833
- [x] Fixes #834
- [x] Fixes #835
- [x] Fixes #844
- [x] Fixes #845
- [x] Fixes #846
- [x] Fixes #847
- [x] Fixes #852
- [x] Fixes #853
