# Batch 04: Script generation, review and LLM handling

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#770](https://github.com/on22s/alexandria-audiobook2/issues/770) — Cast-list CLI documented bare output filename fails after generation
- [ ] [#772](https://github.com/on22s/alexandria-audiobook2/issues/772) — Chunk quality rejects Cyrillic case-only changes despite full source preservation
- [ ] [#773](https://github.com/on22s/alexandria-audiobook2/issues/773) — Content repair preview offers whitespace-only directions that apply refuses
- [ ] [#774](https://github.com/on22s/alexandria-audiobook2/issues/774) — Dialogue mapping gives excess duplicate entries already-consumed source spans
- [ ] [#775](https://github.com/on22s/alexandria-audiobook2/issues/775) — An inch mark shifts straight-quote dialogue spans onto narration
- [ ] [#776](https://github.com/on22s/alexandria-audiobook2/issues/776) — Printed speaker attribution ignores roster-confirmed names beginning with non-ASCII letters
- [ ] [#780](https://github.com/on22s/alexandria-audiobook2/issues/780) — Generation publication overwrites the input source when output and input paths coincide
- [ ] [#781](https://github.com/on22s/alexandria-audiobook2/issues/781) — Persona recovery with a different age group overwrites that saved version with the active persona
- [ ] [#783](https://github.com/on22s/alexandria-audiobook2/issues/783) — Manual LLM process liveness probe terminates the owner on Windows
- [ ] [#784](https://github.com/on22s/alexandria-audiobook2/issues/784) — Manual LLM queue mistakes a reused thread identifier for a stale request owner
- [ ] [#786](https://github.com/on22s/alexandria-audiobook2/issues/786) — Straight-quoted source-credit titles become spoken regions
- [ ] [#787](https://github.com/on22s/alexandria-audiobook2/issues/787) — Quote fidelity gate rejects a fully preserved one-word segment inside dialogue

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
