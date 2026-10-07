# Batch 15: Benchmarks, evidence and developer tooling

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#761](https://github.com/on22s/alexandria-audiobook2/issues/761) — Generation-attempt analysis reports zero failures for failed manifests without attempt arrays
- [ ] [#764](https://github.com/on22s/alexandria-audiobook2/issues/764) — Thunder checkout preflight mistakes hexadecimal login banners for Git revision output
- [ ] [#765](https://github.com/on22s/alexandria-audiobook2/issues/765) — CPU benchmark preflight accepts a worker checkout observed dirty after its clean check
- [ ] [#766](https://github.com/on22s/alexandria-audiobook2/issues/766) — Benchmark fixture IDs collide for distinct sources sharing a filename
- [ ] [#767](https://github.com/on22s/alexandria-audiobook2/issues/767) — Windows benchmark orchestration builds Linux worker paths with backslashes
- [ ] [#768](https://github.com/on22s/alexandria-audiobook2/issues/768) — Remote LoRA benchmark input staging accumulates without cleanup or a bounded cache
- [ ] [#769](https://github.com/on22s/alexandria-audiobook2/issues/769) — LoRA benchmark rejects contained audio filenames beginning with two dots
- [ ] [#771](https://github.com/on22s/alexandria-audiobook2/issues/771) — Shard coverage verifier accepts duplicated and omitted tests when totals match
- [ ] [#778](https://github.com/on22s/alexandria-audiobook2/issues/778) — Review cleanup reports removals when unlink fails and history remains
- [ ] [#782](https://github.com/on22s/alexandria-audiobook2/issues/782) — Gold fixture merge truncates the previous fixture when publication fails
- [ ] [#819](https://github.com/on22s/alexandria-audiobook2/issues/819) — Runtime branch diagnostics discard branch-name prefixes at slashes
- [ ] [#874](https://github.com/on22s/alexandria-audiobook2/issues/874) — Benchmark UI remains labeled running after background registration fails

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.

#768 also has PR #920; coordinate with that fix and avoid duplicate implementation.
