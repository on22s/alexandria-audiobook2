# Batch 07: Datasets, Voice Lab and training

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#785](https://github.com/on22s/alexandria-audiobook2/issues/785) — LoRA benchmark accepts fewer training samples than requested through supplied manifests
- [ ] [#799](https://github.com/on22s/alexandria-audiobook2/issues/799) — Deleted dataset-builder projects still return a successful empty status
- [ ] [#800](https://github.com/on22s/alexandria-audiobook2/issues/800) — Nested dataset ZIP flattening silently replaces conflicting root members
- [ ] [#801](https://github.com/on22s/alexandria-audiobook2/issues/801) — LoRA audition returns failure after publishing when old-file retention fails
- [ ] [#802](https://github.com/on22s/alexandria-audiobook2/issues/802) — Dataset ZIP metadata directory produces opaque HTTP 500 instead of validation error
- [ ] [#811](https://github.com/on22s/alexandria-audiobook2/issues/811) — Voice Lab inspect counts ZIP-named directories that preflight excludes
- [ ] [#812](https://github.com/on22s/alexandria-audiobook2/issues/812) — Voice Lab CUDA-only probe refuses supported MPS device selection
- [ ] [#813](https://github.com/on22s/alexandria-audiobook2/issues/813) — Voice Lab rounds free disk bytes before applying critical admission threshold
- [ ] [#814](https://github.com/on22s/alexandria-audiobook2/issues/814) — Voice Lab name-only preflight requires an unrelated dataset folder
- [ ] [#870](https://github.com/on22s/alexandria-audiobook2/issues/870) — Training reference diagnostic crashes on multilingual text with non-UTF-8 stdout
- [ ] [#871](https://github.com/on22s/alexandria-audiobook2/issues/871) — Training publishes NaN adapter weights and reports successful completion
- [ ] [#872](https://github.com/on22s/alexandria-audiobook2/issues/872) — Malformed ready preflight receipt escapes as TypeError instead of failed receipt

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
