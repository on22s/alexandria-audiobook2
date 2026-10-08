# DeepSeek bug fixes — batch 3

Approved scope: script repair and LLM recovery. Preserve retry limits, source evidence checks and incomplete-manifest visibility.

- [x] #942 Restore prior output if final repair-report publication fails; retain a recoverable backup if rollback fails.
- [x] #969 Bind evidence to accepted canonical alias variants, excluding rejected and conflicting evidence.
- [x] #970 Retain nested split paths in live attempt telemetry.
- [x] #971 Omit reasoning sampling fields from SDK extra_body as well as typed options, without changing caller data.
- [x] #973 Persist and check foreign manual-request owner process birth identity.
- [x] #974 Preserve source-backed Cyrillic words inside underscore emphasis.
- [x] #975 Share source block support between periodic repair and duplicate classification.
- [x] #976 Retry failed attribution on resume and replace historical failure diagnostics.
- [x] #977 Reject duplicate normalized canonical cast names before saving.
- [x] Focused rejection/recovery checks: 317 tests passed; runtime-profile follow-up: 36 passed.
- [x] ready.sh: 7,060 unit tests passed; quick API checks: 70 passed, 12 skipped.

Process ownership tests use a real Linux child and simulated Windows API handles. Native Windows/macOS and live LLM inference have not been tested.
