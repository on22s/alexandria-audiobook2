# Batch 06: Script generation, review and LLM handling

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

This batch fixes speech normalization, checkpoint validation, source-backed repair, and advanced persona fallback context.

- [x] [#824](https://github.com/on22s/alexandria-audiobook2/issues/824) — Speech risk classification mistakes paired emphasis markers for a list or table
- [x] [#825](https://github.com/on22s/alexandria-audiobook2/issues/825) — Speech normalization duplicates copyright when the symbol precedes the word
- [x] [#891](https://github.com/on22s/alexandria-audiobook2/issues/891) — Stage 4 cross-drive WAV rejection escapes the validation-error wrapper
- [x] [#909](https://github.com/on22s/alexandria-audiobook2/issues/909) — Deterministic repair blocks faithful Cyrillic words in mixed-language sources
- [x] [#912](https://github.com/on22s/alexandria-audiobook2/issues/912) — Advanced persona fallback compilation ignores --context-lines and always selects eight samples

Validation: 167 focused tests and 6,904 full unit tests passed without skips; quick API checks passed 70 with 12 full-mode checks skipped. Real source-repair preview/apply routes preserve faithful mixed-script text and refuse introduced words. Actual persona orchestration publishes reference samples matching requested context counts, bounded by the existing maximum. Cross-drive refusal is simulated with Windows path semantics on Linux; native Windows execution is not claimed.
