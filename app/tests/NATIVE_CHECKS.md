# Native subprocess ownership checks

The Linux release suite covers its kernel ownership implementation and the
controlled Win32 API boundary cases. It does not verify the Windows kernel.

The required `windows-owner` CI job runs these four checks on `windows-latest`:

```text
cd app
python tests/native_windows_owner_checks.py
```

Set `PYTHONPATH` to `.` when invoking the script. The script rejects a non-Windows
host with exit 2 and accepts only four successful tests with zero skips. It uses
Python's standard library; it does not load models or perform GPU inference.
It verifies detached descendants with inherited and closed pipes, high-bit exit
status, controller-death cleanup, and preservation of an unrelated process.

Before that native CI job succeeds, Windows kernel verification remains pending.
The current macOS process-group fallback still lacks exclusive lifetime ownership
of escaped descendants (review finding #369); neither Linux nor Windows results
resolve that implementation gap.
