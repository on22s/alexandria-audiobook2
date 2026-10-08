# DeepSeek bug fixes — batch 2

Approved scope: validation and cached input integrity.

- [x] #939 Keep measured queue ETA available when named timezone data is missing, using system-local time.
- [x] #940 Reject negative selection counts and non-positive batch sizes before loading gold-set inputs; keep count=0 valid.
- [x] #953 Refuse non-object narrator overrides with a validation error.
- [x] #968 Reject boolean/fractional ratings without truncation; retain integer strings and whole floats.
- [x] #972 Track prompt file identity beyond mtime while retaining cache reuse for unchanged inputs.
- [x] Run failing regressions before the fixes and the focused suite afterward: 91 tests passed, no skips.
- [x] Complete ready.sh: 7,056 unit tests passed; quick API checks: 70 passed, 12 skipped.

Missing timezone behavior was simulated on Linux, not tested on native Windows. No GPU inference required.

Validation: 91 focused tests; ready.sh passed with 7,068 unit tests and 70 isolated API checks. The quick gate skipped 12 generation checks. No production GPU inference or native browser run is claimed.
