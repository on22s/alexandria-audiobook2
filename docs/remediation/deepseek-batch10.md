# DeepSeek bug fixes — batch 10

Approved scope: audit and Voice Lab tools. Keep explicit malformed-evidence classification, extraction completeness, archive limits and source-aware cache checks.

- [x] #987 Reject non-boolean correctness values visibly rather than counting generic truthiness.
- [x] #988 Classify malformed legacy rows before scoring or arm enumeration.
- [x] #989 Preserve index collection with a visible note for malformed finished timestamps.
- [x] #990 Publish terminal failure state for empty dedup input.
- [x] #991 Recognize WAV extensions without changing archive member spelling or train-folder preference.
- [x] #992 Preserve prior narrator output on partial extraction; publish only after clean extraction.
- [x] #993 Recognize uppercase/mixed-case ASIN identifiers.
- [x] #994 Extract readable passage text from a single-spine EPUB.
- [x] 27 focused artifact/rejection tests pass.
- [x] ready.sh: 7,062 unit tests pass; isolated API checks: 70 pass, 12 generation checks skipped.

Actual CSV, JSON, ZIP and EPUB artifacts are checked. Embedding inference is substituted in the existing CPU harness; no GPU or production corpus run is claimed.
