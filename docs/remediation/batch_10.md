# Batch 10: Audio generation, exports and voice persistence

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#875](https://github.com/on22s/alexandria-audiobook2/issues/875) — Design voice rendering drops the configured seed in single and batch paths
- [ ] [#876](https://github.com/on22s/alexandria-audiobook2/issues/876) — External TTS admits a timeline-selected LoRA version into the local batch path
- [ ] [#877](https://github.com/on22s/alexandria-audiobook2/issues/877) — Failed design voice copy leaks its generated preview and leaves partial temporary audio
- [ ] [#878](https://github.com/on22s/alexandria-audiobook2/issues/878) — VRAM benchmark does not create its documented benchmark_summary.txt output
- [ ] [#881](https://github.com/on22s/alexandria-audiobook2/issues/881) — Speaker-model legacy sibling fallback misses Windows virtual-environment layout
- [ ] [#910](https://github.com/on22s/alexandria-audiobook2/issues/910) — Dynamic Narrator alias silently uses the base voice instead of the selected chapter version
- [ ] [#913](https://github.com/on22s/alexandria-audiobook2/issues/913) — Voice-config repair overwrites a configured ensemble with an automatic custom voice

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
