# Batch 07: Datasets, Voice Lab and training

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

This batch fixes workload admission, training numerical failures, dataset status and archive publication, and Voice Lab preflight consistency.

- [x] [#785](https://github.com/on22s/alexandria-audiobook2/issues/785) — LoRA benchmark accepts fewer training samples than requested through supplied manifests
- [x] [#799](https://github.com/on22s/alexandria-audiobook2/issues/799) — Deleted dataset-builder projects still return a successful empty status
- [x] [#800](https://github.com/on22s/alexandria-audiobook2/issues/800) — Nested dataset ZIP flattening silently replaces conflicting root members
- [x] [#801](https://github.com/on22s/alexandria-audiobook2/issues/801) — LoRA audition returns failure after publishing when old-file retention fails
- [x] [#802](https://github.com/on22s/alexandria-audiobook2/issues/802) — Dataset ZIP metadata directory produces opaque HTTP 500 instead of validation error
- [x] [#811](https://github.com/on22s/alexandria-audiobook2/issues/811) — Voice Lab inspect counts ZIP-named directories that preflight excludes
- [x] [#812](https://github.com/on22s/alexandria-audiobook2/issues/812) — Voice Lab CUDA-only probe refuses supported MPS device selection
- [x] [#813](https://github.com/on22s/alexandria-audiobook2/issues/813) — Voice Lab rounds free disk bytes before applying critical admission threshold
- [x] [#814](https://github.com/on22s/alexandria-audiobook2/issues/814) — Voice Lab name-only preflight requires an unrelated dataset folder
- [x] [#870](https://github.com/on22s/alexandria-audiobook2/issues/870) — Training reference diagnostic crashes on multilingual text with non-UTF-8 stdout
- [x] [#871](https://github.com/on22s/alexandria-audiobook2/issues/871) — Training publishes NaN adapter weights and reports successful completion
- [x] [#872](https://github.com/on22s/alexandria-audiobook2/issues/872) — Malformed ready preflight receipt escapes as TypeError instead of failed receipt

Validation: 160 integrated focused tests passed without skips. Tests exercise real archive extraction/publication, selected-interpreter subprocesses, actual CPU autograd/optimizer updates and checkpoint publication. GPU model boundaries are synthetic. MPS availability and strict cp1252 output are simulated on Linux; native macOS/Windows training is not claimed. Full release results are recorded in the PR description.
