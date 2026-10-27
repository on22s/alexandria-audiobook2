# Native subprocess ownership checks

The Linux release suite covers its kernel ownership implementation and the
controlled Win32 API boundary cases. It does not verify the Windows kernel.

The required `windows-owner` CI job runs these seven checks on `windows-latest`:

```text
cd app
python tests/native_windows_owner_checks.py
```

Set `PYTHONPATH` to `.` when invoking the script. The script rejects a non-Windows
host with exit 2 and accepts only seven successful tests with zero skips. It uses
Python's standard library; it does not load models or perform GPU inference.
It verifies detached descendants with inherited and closed pipes, high-bit exit
status, controller-death cleanup, preservation of an unrelated process, and
private export-stage hard-exit, live-owner and finish behavior.

Before that native CI job succeeds, Windows kernel verification remains pending.
The current macOS process-group fallback still lacks exclusive lifetime ownership
of escaped descendants (review finding #369); neither Linux nor Windows results
resolve that implementation gap.

## Export staging ownership and cleanup

The same `windows-owner` job also runs the existing export staging suite:

```text
python -m unittest tests.test_export_staging -v
```

This exercises normal cleanup, live-owner preservation, abandoned-owner
reclamation, marker validation, and the Windows handle's rejection of directory
retargeting. POSIX-only retirement tests are explicitly skipped on Windows;
their Linux results do not qualify Windows behavior. Private Windows creation/retirement now has hard-exit, finish, live-owner and
recovery checks in this gate. Power-loss directory durability and the legacy
public-stage finalization path remain unqualified.
