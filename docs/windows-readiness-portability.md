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
The portable suite also requires distinct native DOS short directory spellings
on Windows. ZIP admission checks each ancestor for actual symlinks/reparse
points rather than equating a different resolved spelling with a link. Admitted paths
are canonicalized before family locks and merge-destination collision checks so
short and long spellings retain one identity. A real native junction regression
also checks source and destination rejection at the output leaf, parent, and
deeper ancestor; this supplements the separate real-symlink gate. Checkpoint
crash fixtures compare canonical parent spellings so each injected interruption
still reaches the real publication/recovery move under short TEMP paths.
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

## Isolated local unit discovery

`ready.sh` selects the verifier's `--local-units` mode. On native Windows it
uses six CI partitions with **two** simultaneous workers, regardless of CPU count.
`--unit-workers N` explicitly changes that count; higher values raise memory,
driver and IO pressure. Qualification of this change uses at most two workers.
Linux/macOS default to serial discovery; explicitly selecting workers uses the
same isolated mechanism. Direct `verify_release.py` without local options and
CI's explicit `--shard I/N` keep their existing path. Non-unit gates still run
once in the original checkout, in their existing order; a failed unit gate still
prevents later API gates from being claimed as run.

From PowerShell, use native Git Bash explicitly (not the WindowsApps WSL alias):

```powershell
$env:AA2_PYTHON = 'C:/Users/Mitch/Documents/ChatGPT/aa2 2/windows-hook-venv/Scripts/python.exe'
$env:PYTHONUTF8 = '1'
& 'C:/Program Files/Git/bin/bash.exe' ./ready.sh --unit-workers 2 --json-report "$env:TEMP/readiness.json"
& 'C:/Program Files/Git/bin/bash.exe' ./ready.sh --unit-workers 0 --unit-shard-timeout 7200
& 'C:/Program Files/Git/bin/bash.exe' ./ready.sh --serial-on-low-disk
```

`--unit-workers 0` explicitly selects serial execution. `--serial-on-low-disk`
allows that fallback only when snapshot disk admission refuses; there is no
silent fallback. Worker counts above six are capped by the six partitions.
The configurable `--unit-shard-timeout` defaults to 3600 seconds per parallel
shard or 7200 seconds for unsharded serial execution (including low-disk
fallback), including discovery, not per production operation. This avoids
reintroducing a one-hour cutoff for the already hour-long serial suite. An
explicit bound overrides either default. Production
timeouts, retries, identity rechecks and locking are unchanged.

Every local unit child (including serial fallback) receives empty
`CUDA_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES` and `ROCR_VISIBLE_DEVICES` values.
These hide GPU devices from CUDA/HIP/ROCr in the worker and its descendants.
The parent environment is unchanged. Hardware-dependent qualification remains a
separate gate; an intentional GPU-dependent skip is still recorded and fails
the existing strict local skip policy, rather than silently qualifying hardware.

### Snapshot and isolation contract

The coordinator copies the caller's index to a private temporary index, disables
split-index storage there, and runs `git add -A` / `git write-tree` against that
index. A temporary `git commit-tree` object has the original HEAD as parent;
**no ref, caller index or stash stack is changed**. Staged content, subsequent
unstaged edits, deletions and non-ignored untracked files are captured. Untracked
test files still need staging and inventory regeneration under the existing
inventory policy; adding them to a snapshot does not waive that check.

Git attributes can normalize bytes. The coordinator therefore captures raw
file digests, retains a private byte overlay where Git's blob differs, and
applies it after each `git worktree add --detach`. It checks each checkout's
actual file bytes against the captured manifest before launching tests.
Each shard's own index is then refreshed under the normal Git attributes and
configuration, with a tree-identity check; this avoids false dirty status from
CRLF overlays without changing the caller's index or the captured tree. Source
contents/versions are observed before and after capture, and index/HEAD stability
is checked; concurrent changes refuse the snapshot rather than being silently
mixed. Normal executable permissions are preserved. Inodes and checkout times
are necessarily different, as with any new worktree. The Git commit plus raw
manifest describes the tested snapshot; the commit alone is not a claim that
normalized and original bytes are identical.

Each shard has its own working directory/index while sharing Git objects and
common repository metadata. This is checkout isolation, not a security sandbox
for hostile test code. Internal Git operations disable hooks without changing
the user's hook configuration.
Internal commands also enable Git's long-path handling for nested Windows
artifact directories, without changing the user's Git configuration.
All coordinator `git worktree` commands, including add/remove/list/prune, take
the same kernel-owned `alexandria-local-unit-git.lock` in the common Git directory.
This serializes cooperating readiness coordinators across linked worktrees; the
lock releases on process death and its persistent file is never age-reclaimed.
External Git clients must cooperate to share that lock.

Workers receive `AA2_LOCAL_UNIT_COMMON_DIR`. The hook installer refuses to
write settings when its target resolves to that protected common directory.
Independent disposable fixture repositories still exercise real installation.
The readiness parent installs hooks before unit execution and writes only settings
whose current values differ; already installed hooks cause no configuration write.
Detached worktree creation does not create tracking branches or set upstreams,
and coordinator overrides use command-scoped `git -c` exclusively.
Inherited Git checkout/index-routing overrides
are cleared so every operation binds to the named checkout. Native Windows
hidden per-drive environment entries and the selected interpreter are retained.
Existing native process-owner/job-object helpers
own workers and their descendants. On interruption, failure or timeout all owned
workers are reaped before their worktrees are removed; no command-line process
matching or broad worktree pruning is used. Cleanup problems fail readiness and
record the remaining owned paths.

Limits are explicit: ignored untracked configuration, environments, models and
private data are not copied; review `.gitignore` if a source helper is needed.
Submodules, special files and incomplete sparse/conflicted indexes refuse
capture. Unsupported checkout/filter transformations refuse materialization
instead of testing different bytes. Snapshot commits are unreferenced after
worktree cleanup and are eligible for ordinary Git garbage collection; byte
overlays are removed after execution, while the raw manifest remains as evidence.

Before creating snapshots, disk admission reserves
`(2 * simultaneous_workers + 1) * source_bytes + 1 GiB` on the artifact volume
and `source_bytes + 1 GiB` on the Git/source volume. This deliberately allows
checkout/EOL overhead, the private overlay and new Git objects; it is an estimate,
not a filesystem quota. Worktrees are created on demand and removed as each shard
finishes, so the worker limit also bounds concurrently checked-out trees.

### Results and failure policy

The coordinator calls the existing `get_test_shards` implementation; workers use
`ci_env --shard I/N` and the captured checked-in weight file. Balance does not
define coverage. The existing inventory gate is retained, and aggregation reuses
the CI report checker's exact-ID, duplicate, omission and count checks. The full
discovery floor remains enforced. A nonzero worker/owner exit, missing/malformed
receipt, incomplete count, missing shard, timeout, failure, error, unexpected
success or skip fails readiness. Known Windows failures and intentional platform
skips are reported, not waived.

Logs and JSON remain in the printed temporary artifact directory, outside the
checkout, or in a new/empty `--unit-report-dir` supplied by the caller. Each shard
has `output.log`, `tests.json` and `shard.json`; `aggregate.json` records exact
IDs, summed unittest counters, status and cleanup. `ci_env` atomically checkpoints
counts about every five seconds and writes a final receipt even when tests fail.
A crash/timeout may leave only last-known partial counts: `counts_complete=false`
must never be interpreted as a successful or complete run. The verifier retains
these results in its failed `unit_tests` gate as well. All shards continue after
ordinary test failures so the aggregate is useful; infrastructure failures may
leave other shards unrun, which also fails the exact inventory contract.
