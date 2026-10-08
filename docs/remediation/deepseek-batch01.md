# DeepSeek bug fixes — batch 1

Approved scope: text and JSON helpers. Preserve the apostrophe damage gate, Unicode mismatch rejection, path traversal checks and malformed-JSON refusal.

- [x] #937 Preserve uppercase contraction/clitic suffixes.
- [x] #938 Match source-backed accented Latin letters across case changes.
- [x] #941 Recover a valid later JSON object after an unmatched prose brace.
- [x] #943 Recognize children of a filesystem root without accepting sibling prefixes.
- [x] Verify regressions before fixes and rejection controls afterward: 95 focused tests passed, no skips.
- [x] Run ready.sh: 7,054 unit tests passed; quick API checks: 70 passed, 12 skipped.
- [ ] Commit, push and publish the draft PR.

Each issue must have a real regression plus negative controls. No claim of GPU, browser or native Windows testing from CPU helper tests.
