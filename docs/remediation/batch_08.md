# Batch 08: Datasets, Voice Lab and training

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#895](https://github.com/on22s/alexandria-audiobook2/issues/895) — --verify-hash exits successfully when adapter weights cannot be verified
- [ ] [#902](https://github.com/on22s/alexandria-audiobook2/issues/902) — Batch LoRA resume skips an untrained dataset when another adapter shares its prefix
- [ ] [#903](https://github.com/on22s/alexandria-audiobook2/issues/903) — Batch LoRA default interpreter ignores Windows venv layout
- [ ] [#904](https://github.com/on22s/alexandria-audiobook2/issues/904) — Batch LoRA retries skip a completed adapter after failed manifest registration
- [ ] [#905](https://github.com/on22s/alexandria-audiobook2/issues/905) — Stale training split overrides a newly extracted batch dataset ZIP
- [ ] [#906](https://github.com/on22s/alexandria-audiobook2/issues/906) — Analyze leaves running phase state after returning for missing or empty inputs
- [ ] [#907](https://github.com/on22s/alexandria-audiobook2/issues/907) — Voice-analysis CLI returns normally after partial dedup extraction

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
