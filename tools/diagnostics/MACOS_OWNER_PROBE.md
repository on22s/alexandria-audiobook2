# Native macOS ownership prerequisite diagnostic

This CPU-only diagnostic investigates review finding 369. It does not enable a macOS process-ownership backend or certify exclusive lifetime ownership.

The PR-scoped `macOS ownership prerequisite probe` workflow runs it as an ordinary user on `macos-15`. It creates one UUID-labelled temporary launchd job and its own CPU children. Signals target the exact audit identity of the known child; cleanup removes only the diagnostic job label. It does not signal an entire observed coalition, use elevated privileges, load app/model code, or run SSH/GPU work.

The result and error log are retained even when native APIs, permissions, isolation or cleanup fail. A positive result verifies only the measured prerequisites: detached child coalition membership, stale PID-version rejection, STOP/CONT behavior, TERM refusal, KILL exhaustion and an unrelated surviving sentinel. Initial singleton membership does not prove that unrelated future jobs can never enter the same coalition. Production admission, retained lease descriptors, complete supervision and lifetime isolation remain unimplemented.

Run `python3 tools/diagnostics/macos_369_native_probe.py` on native macOS. Linux refuses explicitly without reporting a successful or skipped native test. Missing/private kernel APIs and ordinary-user permission failures are evidence against the candidate on that runner, not a reason to silently substitute a process-group implementation.
