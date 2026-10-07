# Batch 06: Script generation, review and LLM handling

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#824](https://github.com/on22s/alexandria-audiobook2/issues/824) — Speech risk classification mistakes paired emphasis markers for a list or table
- [ ] [#825](https://github.com/on22s/alexandria-audiobook2/issues/825) — Speech normalization duplicates copyright when the symbol precedes the word
- [ ] [#891](https://github.com/on22s/alexandria-audiobook2/issues/891) — Stage 4 cross-drive WAV rejection escapes the validation-error wrapper
- [ ] [#909](https://github.com/on22s/alexandria-audiobook2/issues/909) — Deterministic repair blocks faithful Cyrillic words in mixed-language sources
- [ ] [#912](https://github.com/on22s/alexandria-audiobook2/issues/912) — Advanced persona fallback compilation ignores --context-lines and always selects eight samples

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
