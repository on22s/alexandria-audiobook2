# DeepSeek bug fixes — batch 8

- [x] #978 Normalize grouped hundreds while preserving cardinal/year boundaries.
- [x] #979 Keep alignment quit checkpoints at the accepted prefix.
- [x] #980 Check every GPU for server-release headroom; malformed telemetry stays unknown.
- [x] #981 Reap the owned preparer process group after leader exit and before restart.

52 focused tests pass: actual checkpoints, mixed GPU telemetry, owned child cleanup and unrelated-process controls. No native GPU telemetry run is claimed.

Validation: 52 focused tests; ready.sh passed with 7,059 unit tests and 70 isolated API checks. The quick gate skipped 12 generation checks. No production GPU inference or native browser run is claimed.
