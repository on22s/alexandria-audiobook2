# Batch 17: Benchmarks, evidence and developer tooling

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#898](https://github.com/on22s/alexandria-audiobook2/issues/898) — Repeat rescoring overwrites saved scores when checkpoints are absent or unreadable
- [ ] [#899](https://github.com/on22s/alexandria-audiobook2/issues/899) — Results collector loses UTF-8 attribution rows under legacy default text encoding
- [ ] [#901](https://github.com/on22s/alexandria-audiobook2/issues/901) — Whisper asset verifier has no deadline for Git probes that stall in fsmonitor
- [ ] [#908](https://github.com/on22s/alexandria-audiobook2/issues/908) — Subset watchdog writes completion and notifies success after an aborted empty run

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
