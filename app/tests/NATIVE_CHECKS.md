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

## Book transaction, recovery archive and cancellation handles

Windows-only checks for these areas live in a script that unit discovery
does not collect, so the Linux release shards (which fail on any skipped test)
never see them:

```text
cd app
python tests/native_windows_transaction_checks.py
```

Set `PYTHONPATH` to `.`. The `windows-owner` job runs it after installing the
script-router dependencies it imports. It rejects a non-Windows host with exit 2
and accepts only 25 successful tests with zero skips: recovery-archive directory
guards and unsafe-entry observations (11), native staging identities and handles in the book transaction (5),
book-move cleanup and sharing handles (4), published-script cancellation
through the owned job (3), and short-name/ancestor path admission (2). The
short-name check requires a volume providing a distinct DOS short spelling;
absence fails native qualification rather than passing a skipped check.
Do not add `skipUnless` Windows guards to discovered
`test_*.py` modules; put native-only checks here instead.
