# Native Windows portability checks

Run the focused CPU regression gate from `app` in an environment with the
repository's pinned CPU test dependencies and native Git Bash installed:

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
python tests/native_windows_readiness_checks.py --json-report native_windows_readiness_result.json
python tests/native_windows_readiness_checks.py --suite-group symlink --json-report native_windows_symlink_result.json
```

The runner binds its report to the checkout commit and requires a nonempty
suite, every loaded test to execute, zero failures/errors, and zero skips. It
refuses non-Windows hosts. Its explicit suites exercise spawned checkpoint and
promotion crashes/recovery, real file-lock admission, shared-delete replacement
races, native shell launchers, HTTP framing, terminal escaping and path handling.
Spawned adapter workers have a bounded 60-second dependency initialization
handshake; their transaction hang deadline remains ten seconds after readiness.
It does not use live LLM, TTS or GPU inference.

The `windows-portability` job in `tests.yml` runs this gate and retains its JSON
report, including failed setup evidence when loading the suite fails. The
symlink step also runs after a portable-gate failure unless the job was cancelled.
A separate required step in the same job runs the three checkpoint tests
that construct actual symlinks. These tests fail when symlink creation is refused;
no skip, junction, or hardlink substitutes for that containment evidence. A local
`WinError 1314` identifies a missing host privilege and leaves those tests
unqualified even when the focused portable suite passes.

The existing native Windows process-owner and transaction checks remain separate
requirements. The Linux queue tests depend on `/proc`, `flock`, and Linux process
groups; their audited platform declarations retain execution on Linux. Their
skips off Linux do not establish equivalent native Windows coverage.

Before committing, run `./ready.sh` through native Git Bash with `AA2_PYTHON` set
to the existing interpreter's absolute path. The focused gate supplements the
release verifier; it does not certify a full shard, the full release suite,
symlink containment, inference, or the final hosted commit. Report each result
and any remaining errors/skips separately.
