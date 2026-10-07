# Batch 16: Benchmarks, evidence and developer tooling

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#879](https://github.com/on22s/alexandria-audiobook2/issues/879) — Release verifier has no deadline for git enumeration when a fsmonitor hook stalls
- [ ] [#880](https://github.com/on22s/alexandria-audiobook2/issues/880) — Release compile gate treats Git-quoted Unicode filenames as literal paths
- [ ] [#882](https://github.com/on22s/alexandria-audiobook2/issues/882) — Corpus plan command exits successfully after pair discovery fails
- [ ] [#883](https://github.com/on22s/alexandria-audiobook2/issues/883) — Whisper downloader reports failure after publishing when old-bundle cleanup fails
- [ ] [#885](https://github.com/on22s/alexandria-audiobook2/issues/885) — Cloud comparison provenance waits without a deadline on stalled Git fsmonitor
- [ ] [#888](https://github.com/on22s/alexandria-audiobook2/issues/888) — Queue log clears active START when job name contains LOCK_FAILED or PENDING_FAILED
- [ ] [#889](https://github.com/on22s/alexandria-audiobook2/issues/889) — Dangling --requires-ok loops in stage argument parsing under plain Bash
- [ ] [#890](https://github.com/on22s/alexandria-audiobook2/issues/890) — Artifact commit helper uses root-relative pathspecs from the supplied subdirectory
- [ ] [#893](https://github.com/on22s/alexandria-audiobook2/issues/893) — Subset completion summaries truncate ZIP filenames containing spaces
- [ ] [#894](https://github.com/on22s/alexandria-audiobook2/issues/894) — Subset reruns retain stale DONE and ABORTED flags
- [ ] [#896](https://github.com/on22s/alexandria-audiobook2/issues/896) — Experiment artifact audit crashes with a bare --out filename
- [ ] [#897](https://github.com/on22s/alexandria-audiobook2/issues/897) — Results collector omits valid seed zero from generated CSV

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
