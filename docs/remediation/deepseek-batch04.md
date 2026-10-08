# DeepSeek bug fixes — batch 4

Approved scope: benchmark remote execution and resume. Retain remote shutdown proof, durable case publication, fixture hashes and containment checks.

- [x] #944 Use POSIX paths for the remote supervisor and receipt imports regardless of controller OS.
- [x] #945 Publish active batch state before execution, preserve completed cases and report failure/cancellation accurately.
- [x] #946 Allocate private remote dedup staging; clean it only after taking validated ownership. Share allocation with training staging.
- [x] #947 Load only pending script-generation/review sources while validating their hashes and containment.
- [x] Focused ownership, cancellation and resume checks: 60 tests passed.
- [x] ready.sh: 7,056 unit tests passed; quick API checks: 70 passed, 12 skipped.

Tests use actual local files/processes with controlled SSH transport. They do not demonstrate a real Thunder run or native Windows execution.

Validation: 60 focused tests; ready.sh passed with 7,068 unit tests and 70 isolated API checks. The quick gate skipped 12 generation checks. No production GPU inference or native browser run is claimed.
