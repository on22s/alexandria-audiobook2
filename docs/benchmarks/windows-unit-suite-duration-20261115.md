# Native Windows unit-suite duration: measurements and isolated local shards

## Final implementation and qualification, 2026-11-16

The approved change is **A (isolated local CI shards) plus C (fixture timer
cleanup)**. FFmpeg and the Windows failure backlog are unchanged by this PR.
Before final qualification, fetched main was merged at
`f19ce7546b837f66f3849a51bd7c01d9024ff3c5` (#1974, following #1972/#1973).

Native Windows defaults to **two workers**, independent of CPU count. An explicit
`--unit-workers N` changes it; higher counts raise memory, driver and IO pressure.
Qualification used at most two concurrent full-suite partitions and no overlapping
heavy qualification jobs. Every worker, including serial fallback, receives empty
`CUDA_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES` and `ROCR_VISIBLE_DEVICES`. The
selected CPython 3.10.22 interpreter also has Torch `2.10.0+cpu`.

### Measured readiness attempts

Both runs use the same six CI partitions and checked-in weights: one worker runs
them sequentially, two workers run them concurrently. This is a same-code serial
control, rather than the old unsharded implementation. The two-worker attempt ran
`ready.sh` in the original implementation worktree; the serial control ran it in
a byte-faithful detached snapshot of that tree. Both tested staged changes.

| Execution | Full `ready.sh` wall s | Verifier s | Unit coordinator s | Ran | Failures | Errors | Skips |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| One worker, six sequential partitions | 3867.258 | 3812.782 | 3798.859 | 14120 | 121 | 295 | 962 |
| Two workers, six partitions | 2221.877 | 2166.500 | 2152.094 | 14120 | 121 | 294 | 962 |

The completed readiness invocation took **64m 27.258s versus 37m 1.877s**, an
observed **42.546% wall-time reduction**. Run order was two workers, then one.
One preparatory regeneration/staging refusal preceded each completed invocation:
54.343s for two workers and 57.123s for one. Those exit-2 setup attempts are
retained separately and excluded from the table. Snapshot setup and cleanup
performed by the actual unit coordinator are included in its wall time.

Both invocations returned **exit 1 at `unit_tests`**. Regeneration ran once per
invocation; compilation and inventory checks passed. The four evidence rechecks
were marked skipped by `ready.sh`'s existing `--skip-evidence-recheck` policy.
Later API gates were not reached. These are failing readiness attempts, not green
release qualifications or predictions of runtime after the backlog is fixed.

### Exact aggregation and the parity limit

Both runs completed all **14,120 unique top-level test IDs**, with exactly the same
ID multiset and `counts_complete=true`. Every aggregate counter was checked
against the sum of the six shard receipts. Both recorded 14,120 completed tests,
zero expected failures and zero unexpected successes. Failure-ID multisets match.

| Shard | Ran | Failures, both | Serial errors | Two-worker errors | Skips, both |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1/6 | 2225 | 15 | 113 | 113 | 160 |
| 2/6 | 2454 | 9 | 33 | 33 | 167 |
| 3/6 | 2443 | 29 | 26 | 26 | 182 |
| 4/6 | 2515 | 11 | 40 | 40 | 128 |
| 5/6 | 2152 | 21 | 48 | 47 | 118 |
| 6/6 | 2331 | 36 | 35 | 35 | 207 |

**Exact outcome-counter parity was not achieved.** The strict comparison correctly
rejected the single extra serial error in
`tests.test_book_preflight_ownership.BookPreflightOwnershipTests.test_real_child_cancel_keeps_claim_until_child_reaped`.
Its thread remained alive after the fixture's wait, followed by WinError 32 while
removing `.task_ownership/task-script.lock`. The same method errored in the
unchanged-main baseline. It was left unchanged under the Windows-backlog scope.
The raw error-ID lists also retain three command-boundary subtests whose IDs embed
different per-run temporary paths; these are the same three parameterized cases,
and their original IDs have not been rewritten in receipts.

All currently failing methods also failed in the historical main baseline. The
ASR fixture's open-log WinError 32 occurs in both current runs, although it was
absent from that earlier unsharded baseline. Shard 1's exact test, failure and error
ID multisets match between the current runs. This distinguishes existing fixture
behavior exposed by partitioning from a concurrency-induced failure. The excluded
backlog includes taskkill/cancellation fixtures, empty data-root path spelling,
selected-interpreter admission and API snapshot CRLF; this PR does not repair them.

The prior main baseline at `c08636db7d90fab18d73d9ebd692431e71f3106d` reported
14,064 tests, 127 failures, 294 errors and 962 skips, with verifier time 3621.750s.
Its full `ready.sh` wall time was not separately recorded. Main subsequently added
29 tests and this change adds 27, explaining the current inventory's 56 additional
tests. Use the matched table above for the controlled wall-time comparison.

### Isolation, shared Git state and verification

The implementation reuses `get_test_shards` and `ci_env --shard`, a private index
and temporary commit object plus a raw-byte overlay, separate detached worktrees,
disk admission, and the existing native subprocess owners. It captures staged,
unstaged and non-ignored untracked inputs without changing the caller's index,
refs or stash stack. Checkout bytes are verified and each shard index is refreshed
without changing the captured tree. Git long-path handling is command-scoped.
Ignored private inputs and unsupported submodules/special files are explicit
limits in [the configuration documentation](../windows-readiness-portability.md#isolated-local-unit-discovery).

The hook installer previously wrote three shared settings unconditionally from
the readiness parent. It now writes only differing settings, and a worker's
`AA2_LOCAL_UNIT_COMMON_DIR` prevents installation into its protected common repo.
Independent fixture repositories still exercise real installation. All internal
`git worktree` operations use one kernel-owned common-directory lock. Workers
are reaped through existing process ownership before owned worktrees are removed.
Both final runs left **no owned registered worktrees**; interrupted-run leftovers
were removed by their exact paths, preserving other registrations.

The two-worker run's shared-config SHA-256 stayed
`071f8dd7d9670f7e69644a8bc1046609e48969c1a6e2ce0ad1266887caae772b`.
The serial run ended with
`47be8ec32f7166bf3c5b3db95ecc7bbb5a3c744f045a919837eebeab98786d7a`:
the byte difference is exactly the added `ui/batch-b-accessible-editing` tracking
stanza. Removing that stanza in memory reproduces the original hash. The current
config has no NUL bytes. Neither observation proves the cause of the earlier
truncation or the reported kernel crashes. External Git clients do not acquire
the coordinator's cooperative lock automatically.

The identical code-input SHA-256 for both measurements is
`9983004315f5cd765b714514bee0d9e63260bc932260d1f5dda3850bcf6d266a`.
Focused validation on merged main: **49 native Windows tests and 28 WSL tests**,
zero failures, errors or skips. WSL covers real serial and parallel fixture runs;
the full WSL suite and native macOS were not qualified. Aggregation, missing-shard,
crash, timeout, interrupt, identity, disk, source/index fidelity, shared-config and
common-lock regressions are included. The failed-gate counting regression also
failed against the frozen original CI checker. Default-worker/GPU-mask and shared
config/lock regressions failed before their respective implementation changes.

The timer repair clears only the fixture's unused owned timer in `finally`.
Its 15-second deadline and resolved/rejected/stalled-load assertions are retained.
Production timeouts, retries and locks are unchanged.

Raw logs, receipts, manifests and comparison JSON are local-only under
`C:/Users/Mitch/AppData/Local/Temp/aa2-windows-duration/` (not committed):
`parallel-main-2/`, `serial-main-control/`, `two-worker-comparison.json`,
`baseline-method-comparison.json`, `config-change-analysis.json`, and the initial
`phase1-bundle/`. Earlier interrupted or zero-test setup runs are not counted as
completed qualification. Only this Markdown report is included from the evidence
bundle. These single-run measurements come from a shared development host; cache
and external activity were not experimentally controlled. No run above two workers
is claimed as final qualification.

The remainder preserves the historical first-phase measurement on
`f5da3e30200755d3b4eaca39e62594c098d6dcf3`, including its 50 slowest tests/modules.

## Historical initial measurement

Date: 2026-11-15. Branch: `perf/windows-unit-suite-duration`.
Measured checkout: `f5da3e30200755d3b4eaca39e62594c098d6dcf3`.

## Decision and limits

**The initial phase stopped after Step 1 under the task's design-change condition.**
At that checkpoint no production code, test behavior, production timeout, retry,
lock, identity check, coverage declaration or readiness policy had been changed,
and no implementation commit, push, PR or merge had been made.

There is a small, independently reproduced fixture timer leak, but its two
15-second waits are not enough to solve a roughly hour-long gate. A substantial
improvement needs a decision about isolated local shards and/or qualified native
encoder dependencies. Those choices are described below rather than guessed.

This is an instrumented **unit-discovery baseline**, not a successful release or
`ready.sh` result. It includes failures, errors, and platform skips. Early failures
and skips can avoid later work: these measurements do not predict an all-passing,
fully exercised suite's duration. This shared development machine was not a
controlled benchmark host; other agents were working concurrently.

## Authoritative readiness-equivalent measurement

| Run | Wall seconds | unittest seconds | Ran | Failures | Errors | Skips | Exit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Unchanged source, native Python from native Git Bash | 3507.108 | 3497.826 | 14025 | 130 | 301 | 960 | 1 |

Wall time: **58 minutes 27.108 seconds**.
Final console verdict: `FAILED (failures=130, errors=301, skipped=960)`.
Failure/error totals include subtest events and must not be subtracted from Ran
to invent a passed-test count.

The instrument recorded **14,025 unique test identities**, exactly matching the
14,025 inventoried identities: **zero missing, zero unknown, zero duplicates**.
Identity equality includes skipped tests; it is not proof their bodies executed.
Per-test intervals total 3490.575 seconds.
The remaining 16.533 seconds
are outside those intervals, including discovery, between-test fixtures,
instrumentation and output bookkeeping; they were not exclusively attributed.

The 50 slowest individual tests total **1395.370
seconds (39.79% of wall time)**.
The 50 slowest modules total **2084.507
seconds (59.44% of wall time)**.
These two totals overlap and **must not be added together**.

## Environment, command, and instrument validation

- Windows 11 Pro, version `10.0.26300`, build `26300`; native CPython `3.10.22`.
- Interpreter: `C:/Users/Mitch/Documents/ChatGPT/aa2 2/windows-hook-venv/Scripts/python.exe`.
- `AA2_PYTHON` selected that interpreter; `PYTHONUTF8=1`.
- Native Git Bash entry: `C:/Program Files/Git/bin/bash.exe`, matching the
  shell used for setup `ready.sh`. Native Python resolved child Bash to
  `C:/Program Files/Git/usr/bin/bash.EXE`, not the WindowsApps WSL alias.
- Versions observed during diagnostics: Torch `2.10.0+cpu`, Transformers
  `4.57.3`, PEFT `0.21.2`, NumPy `2.2.6`, FastAPI `0.143.3`,
  Uvicorn `0.54.0`, safetensors `0.8.0`, httpx `0.28.1`, Node `26.12.1`.
- Native Defender antivirus and real-time protection were enabled. No antivirus
  exclusion or security-policy change was made. Antivirus time was not measured.
- The harness calls the existing `ci_env.main` with
  `discover -s . -p test_*.py -v` from `app`, the same discovery entry and
  options used by `verify_release.py`. It does not run the verifier's other gates.
- It extends the unittest result for timing only, preserves unittest execution
  and exit status, and isolates recording to the outer result so nested
  synthetic unittest runs do not contaminate counts.
- Windows multiprocessing requires a `__main__` guard. Initial probe attempts
  before that guard were discarded; they are not baseline measurements.
- A 34-test sharding-module sanity run reported 34 tests, zero failures/errors/
  skips; the recorder also recorded exactly 34. Full-run identity equality
  above provides the independent inventory check for the actual baseline.
- Replaying the archived instrument through its archived native-shell launcher
  independently reported 34 tests in 2.870 seconds, zero failures/errors/skips.
  The archived instrument matches the measured instrument byte-for-byte:
  SHA-256 `a4cb8ef20d8e5a51007d9f509d087875d42d599cac78b17dd53f76ef132d85a8`.
- A diagnostic watchdog records stacks after a test lasts 60 seconds and would
  terminate the diagnostic process at 180 seconds per test. This is not a new
  production or verifier timeout. It did **not** fire its termination bound.
  Log records are flushed per test so interruption leaves a last-started ID.
- Timing includes the recorder's logging overhead. This was not an uninstrumented
  full-suite A/B benchmark.

Historical reproduction from the unchanged f5da3e30 checkout, inside **native
Git Bash**, using the retained local-only instrument:

```bash
export AA2_PYTHON='C:/Users/Mitch/Documents/ChatGPT/aa2 2/windows-hook-venv/Scripts/python.exe'
export PYTHONUTF8=1
"$AA2_PYTHON" 'C:/Users/Mitch/AppData/Local/Temp/aa2-windows-duration/phase1-bundle/measure_unit_duration.py' --app "$(pwd -W)/app" --output 'C:/Users/Mitch/AppData/Local/Temp/aa2-windows-duration/reproduction.json'
```

The original native-shell launcher used the same interpreter and this exact
instrument from its temporary location. Reproduction creates new output; it
does not overwrite the archived baseline.

## Rejected first full measurement: wrong child-shell environment

The first full invocation inherited PowerShell PATH, which resolved bare
`bash` to the WindowsApps WSL launcher. It took **3801.795 seconds**, with
14,025 tests, 299 failures, 329 errors, and 960 skips. It produced errors such as
WSL `/bin/bash` receiving a native `C:\\...` fixture path.

That invocation is retained separately as exploratory data, **not** the
readiness-equivalent baseline. The correct-shell invocation above was then
run in full. No source fix occurred between those runs, and caches/load were
not controlled. Their timing difference is **not an implementation speedup**.

## What is measured, and what is inferred

### Repeated worker dependency initialization

Measured module totals:
- Checkpoint transactions: **362.155 seconds**, 18 test cases.
- Promotion/publication transactions: **316.368 seconds**, 13 test cases.
- These total **678.524 seconds** of test intervals, not an exclusive import attribution.

The long-test stack dumps show the main thread waiting in
`start_adapter_fixture_process` for worker readiness. That existing helper
imports real PEFT and safetensors before setting its readiness event; crash
injections require fresh workers. An independent native interpreter probe
measured importing PEFT at **5.481 seconds**. Importing the checkpoint test
module and then PEFT in another probe took **0.427 seconds** and **5.658 seconds
total**, respectively.

Inference: repeated dependency initialization is an important contributor in
these modules. Its exact exclusive share of their totals was **not** measured.
The real PEFT/tensor parser and crash/recovery behavior must not be replaced by
mocks or omitted to make the number smaller.

### Native encoder identity checks, not just process creation

The selected Windows FFmpeg executable is **227,398,656 bytes**; FFprobe is
**227,193,344 bytes**. Existing encoder identity admission hashes the actual
FFmpeg file. Three independent identity captures took **0.140968, 0.141783,
and 0.140906 seconds**. WSL's installed `/usr/bin/ffmpeg` is **453,104 bytes**.
That is a binary-size observation, **not** a same-version or full-suite
Windows-versus-WSL speed comparison.

A separate, unmodified real delivery test was profiled after full discovery:
`SoundDeliveryExportTests.test_real_delivery_and_late_source_change_preserve_prior_output`.
It reported **1 test in 19.647 seconds, OK, zero failures/errors/skips**.
The complete profile, including imports, took **21.211 seconds**.

| Profiled operation | Calls | Exclusive seconds | Cumulative seconds |
| --- | ---: | ---: | ---: |
| SHA hash updates | 104826 | 6.086 | 6.086 |
| Buffered reads | 15892 | 2.069 | 2.069 |
| FFmpeg binary identity capture | 64 | 0.001 | 9.270 |
| FFmpeg runtime identity capture | 32 | 0.001 | 13.770 |
| Windows owner startup sleep | 229 | 3.604 | 3.604 |
| Windows process wait primitive | 294 | 4.259 | 4.259 |
| Windows process creation primitive | 46 | 0.326 | 0.326 |
| Native stat calls | 74441 | 1.576 | 1.576 |

Cumulative rows overlap. In particular, binary identity work is nested inside
runtime identity work; do not sum those rows or apply their proportions to the
entire suite. The 64 complete FFmpeg identity captures logically read/hash
about **14.55 GB** of executable bytes, not necessarily physical disk reads.
All recorded startup sleeps in this profile were called by
`windows_subprocess_owner.start_windows_owned_subprocess`; these were startup
polls, not evidence of a whole production timeout being waited out.

Measured conclusion for this one profile: direct `CreateProcess` time is a
small share, while repeated hashing, owned-worker startup/waits, and filesystem
checks are substantial. No claim is made that this profile explains every
module. Removing identity rechecks, caching on size/mtime alone, weakening
ownership/cleanup, or changing production grace periods was rejected.

The full baseline observed **13,154 `subprocess.Popen` audit events**.
This is not a total process-tree count: Windows multiprocessing uses native
creation directly, and descendants are not all audited by the parent.
A zero Popen count in a crash test therefore does not mean it spawned no workers.
The audit's executable field was often `None`, so executable attribution from
that field is not usable; counts and test attribution remain recorded.

### A concrete, smaller fixture bug

`test_1724_voice_navigation_js.py` races an already-settled load against a
15-second timeout and never clears that timeout. Both HTTP navigation scenarios
therefore keep the Node process alive until the unused timer expires.
The whole three-test module measured **30.546 seconds**.

An external deterministic timer-ownership regression failed against unchanged
source with `resolved: fixture must release the owned deadline timer`
(actual live timers: 1; expected: 0). The probe also contains rejected and
stalled-load controls with the original 15-second deadline, but it stops at
the first failed assertion; those later controls have **not** passed unchanged
source. A later repair should clear only the fixture's owned timer in
`finally`, retain the original deadline and rejection behavior, and run all
three controls. This is not a production timeout reduction.

### Suspected single-narrator stall

The actual `test_single_narrator_http_ui` module completed **12 tests**
in the full native-shell run; their summed test intervals were
**4.350 seconds**. None is listed among the baseline failure/error IDs.
An isolated run also reported **12 tests, zero failures/errors/skips**, with
**5.648 seconds** of total instrumented wall time.

The earlier reported single-narrator stall was **not reproduced** on the pinned
checkout. There is no evidence here identifying a production single-narrator
hang. The separate navigation fixture timer above is a confirmed 15-second
delay, not evidence that the single-narrator module hung.

## Tests exceeding diagnostic thresholds

No test reached the 180-second termination bound. Seven exceeded the 60-second
stack-dump threshold and completed. The slowest individual test took 120.451
seconds. These are long multi-scenario tests; a stack snapshot taken during an
initialization wait does not by itself prove a deadlock.

| Seconds | Test |
| ---: | --- |
| 120.451 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_interrupted_recovery_and_terminal_cleanup_are_repeatable` |
| 102.669 | `tests.test_sound_design_delivery_export.SoundDeliveryExportTests.test_repeated_success_does_not_exhaust_retained_stage_slots` |
| 101.638 | `tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_hard_exit_before_commit_restores_original_after_commit_keeps_new_on_fresh_cli` |
| 79.596 | `tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_interrupted_recovery_resumes_after_each_native_restore_and_terminal_cleanup` |
| 72.463 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_promotion_crashes_recover_exact_old_or_durably_committed_bundle` |
| 63.210 | `tests.test_adapter_checkpoint_transaction.CheckpointManifestTransactionTests.test_hard_exit_recovers_both_artifacts_before_commit_and_keeps_both_after_commit` |
| 61.372 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_rollback_crashes_preserve_pre_rollback_bundle_until_commit` |

## Before-change failures, errors, and skips

The authoritative baseline failed before any implementation. The archived raw
JSON lists every failure/error ID, including subtests; these are observations
on this host, **not 431 independently adjudicated product defects**.

The four failures assigned to other agents were left untouched:
1. Taskkill cancellation fixture: `tests.test_word_timing.WordTimingTests.test_real_cancelled_child_is_reaped_and_timeout_is_finite`.
2. Empty data-root spelling: `tests.test_runtime_regressions.RuntimeTests.test_runtime_data_dir_ignores_empty_environment_override`.
3. Selected-interpreter admission: `tests.test_voicelab_health.VoiceLabMpsProbeTests.test_actual_selected_interpreter_probe_and_backend_specific_admission`.
4. API snapshot CRLF: `tests.test_api_contract_regressions.ApiContractTests.test_api_contract_snapshots_match_and_regenerate_deterministically`.

All four were independently rerun **without the timing instrument**, alongside
one real symlink fixture. That run reported **5 tests in 7.125 seconds,
5 failure events, 2 errors, zero skips**. This confirms those failures are not
introduced by the timing-result subclass. No repair was attempted.

Additional baseline limitations include:
- 179 final traceback blocks reported `WinError 1314` symlink privilege refusal.
  This is an observed host qualification failure, not a reason to waive symlink tests.
- Other traceback examples include unsupported `fork`/POSIX signal assumptions,
  Windows file-sharing failures, native-shell fixture/path issues, and missing
  `romkan2`/`pykakasi`. These remaining IDs were not individually adjudicated
  or repaired in this duration task.
- **960 skips** remain, including the repository's explicitly Linux-only queue
  declarations. The existing strict verifier rejects unittest skips. Faster
  execution would not turn this baseline into a successful native release check.

## WSL comparison limit

WSL Ubuntu has Python **3.14.4**, but its selected `python3` could not find
Torch, NumPy, FastAPI, httpx, or Uvicorn. No dependencies or new WSL environment
were installed. A comparable full WSL discovery was therefore **not run**.

A small interpreter-startup probe took **0.660 seconds** for 20 native
CPython-3.10 launches and **0.187 seconds** for 20 WSL CPython-3.14 launches.
Different Python versions/environments make this exploratory only; it does not
attribute the full-suite difference. The historical CLAUDE.md number
(~20 seconds for 1,592 tests) is not a current 14,025-test Linux measurement.

## Design options requiring a decision

### A. Isolated local shards using the CI implementation

Reuse `unit_test_sharding.get_test_shards` / `ci_env --shard I/N`, not a second
assignment scheme. CI expressly uses **separate checkouts/VMs** because tests
write checkout files. Running six processes against this same checkout would
not preserve its isolation contract.

A local design must choose faithful snapshots of the current working tree,
a storage limit and worker limit, preserve native ownership/cancellation, run
non-unit gates once, retain the inventory check, and aggregate exact test IDs,
counts, skips, and failures. A failed or missing shard must fail readiness, and
failed shards still need useful count/status artifacts.

A tracked-file scan measured **6,971 files / 1,175,550,777 bytes**. Six independent
full tracked-source copies would occupy approximately **6.57 GiB** before Git
metadata, temporary data and dependency/process memory. No snapshots were made.

For planning only, grouping this run's measured serial case durations with the
**existing checked-in CI six-shard assignment** produces the following sums:

| Existing CI shard | Measured serial case seconds assigned to it |
| --- | ---: |
| 1/6 | 744.293 |
| 2/6 | 1003.741 |
| 3/6 | 444.567 |
| 4/6 | 494.914 |
| 5/6 | 368.096 |
| 6/6 | 434.964 |

These are **not parallel run times or a measured speedup**. They exclude
snapshot/discovery overhead and do not model CPU, memory, disk contention or
changed outcomes. Linux-derived weights are imperfect for these Windows
costs. The same existing assignment function can consume measured weights;
the inventory/identity contract must stay independent of balance.

### B. Qualify a different native FFmpeg distribution, preserving checks

A smaller shared-library native build could reduce the bytes repeatedly hashed
without deleting an identity check. This needs an explicit dependency/version/
codec qualification decision and a new full native run. No binary was downloaded,
installed, replaced, or selected for the baseline.

A stat-only or stale digest cache would change the identity/safety contract
and was **not** used as a shortcut.

### C. Narrow fixture cleanup only

Repair and regress the unused navigation timer, and separately examine whether
recovery-only workers unnecessarily initialize ML dependencies. Preserve actual
parser/crash coverage and the current startup/operation deadline split.
The timer repair alone saves roughly 30 seconds, not the thousands of seconds
needed to make this gate short. No after-run result is claimed.

**Recommended next decision:** choose an isolated-local-shard capture/storage/
worker policy before implementation; keep the strict readiness result fail-closed.
Encoder packaging and the small fixture cleanup can then be qualified separately.

## Setup and readiness result

The requested `git fetch origin main` immediately preceded branch creation.
`./ready.sh` was run once through native Git Bash with the requested interpreter
and installed the hooks (`core.hooksPath=.githooks`).
It regenerated derived data, then exited **2** at the unstaged-derived-file gate
for `ab_test_runtime/audit/goal_evidence_audit.json`; it **did not reach the
release verifier**. This is not a readiness pass.

`GOALS.md` appeared modified with line-ending-only changes. The whitespace-
insensitive diff was empty, it was restored with `git checkout -- GOALS.md`,
and `python app/experiments/goal_evidence_audit.py` was rerun with clean source
provenance. The remaining generated audit diff was metadata-only.
The measurement hold does not involve staging or committing generated data.

Since the task stopped after measurement, no implementation remeasurement,
pre-commit full readiness attempt, commit, push, PR, hosted CI, or review
qualification is claimed.

## 50 slowest tests

Popen events are parent audit events, not complete process-tree counts.

| Rank | Seconds | Popen events | Test |
| ---: | ---: | ---: | --- |
| 1 | 120.451 | 0 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_interrupted_recovery_and_terminal_cleanup_are_repeatable` |
| 2 | 102.669 | 227 | `tests.test_sound_design_delivery_export.SoundDeliveryExportTests.test_repeated_success_does_not_exhaust_retained_stage_slots` |
| 3 | 101.638 | 20 | `tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_hard_exit_before_commit_restores_original_after_commit_keeps_new_on_fresh_cli` |
| 4 | 79.596 | 8 | `tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_interrupted_recovery_resumes_after_each_native_restore_and_terminal_cleanup` |
| 5 | 72.463 | 0 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_promotion_crashes_recover_exact_old_or_durably_committed_bundle` |
| 6 | 63.210 | 12 | `tests.test_adapter_checkpoint_transaction.CheckpointManifestTransactionTests.test_hard_exit_recovers_both_artifacts_before_commit_and_keeps_both_after_commit` |
| 7 | 61.372 | 0 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_rollback_crashes_preserve_pre_rollback_bundle_until_commit` |
| 8 | 42.941 | 4 | `tests.test_adapter_checkpoint_transaction.CheckpointManifestTransactionTests.test_manifest_and_adapter_recovery_remain_repeatable_after_recovery_hard_exit` |
| 9 | 42.754 | 0 | `tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_corrupt_original_and_unowned_journal_paths_refuse_without_mutation` |
| 10 | 40.178 | 198 | `tests.test_chapter_export.MalformedChapterManifestTests.test_corrupt_previous_rows_render_complete_audio_then_reuse_it` |
| 11 | 31.853 | 296 | `tests.test_chapter_selection_integrity.ChapterSelectionIntegrityTests.test_decoder_rejected_selection_preserves_prior_delivery_cold_and_warm` |
| 12 | 29.263 | 216 | `tests.test_project_narrator_context.ProjectNarratorContextTests.test_single_parallel_and_fast_batch_use_every_supported_selector_and_narrator_alias` |
| 13 | 25.563 | 6 | `tests.test_isolated_api_auth.IsolatedApiAuthTests.test_authenticated_native_runner_and_client_keep_server_guard_enabled` |
| 14 | 25.432 | 5 | `tests.test_preparer_volume_publication.VolumePublicationTests.test_recovery_validates_all_evidence_before_any_mutation` |
| 15 | 25.144 | 1 | `tests.test_verifier_output_retention.VerifierOutputRetentionTests.test_report_streams_all_lines_without_retaining_full_output` |
| 16 | 24.821 | 164 | `tests.test_pcm_mux_worker.PCMMuxWorkerTests.test_native_packets_full_pcm_and_gapless_metadata_match_for_raw_and_mastered` |
| 17 | 24.727 | 6 | `tests.test_cors_auth_order.CorsAuthOrderTests.test_native_allowed_denied_origins_and_optional_auth` |
| 18 | 21.493 | 9 | `tests.test_cloud_reference_chain.CloudReferenceChainTests.test_arm0_is_checked_before_any_expensive_worker` |
| 19 | 21.203 | 0 | `tests.test_adapter_checkpoint_transaction.CheckpointManifestTransactionTests.test_changed_live_manifest_or_corrupt_snapshot_refuses_recovery_before_adapter_moves` |
| 20 | 20.731 | 4 | `tests.test_m4b_cancellation.M4bCancellationTests.test_timeout_and_progress_callback_failure_reap_real_encoder` |
| 21 | 20.673 | 6 | `tests.test_preparer_admission_cancel.PreparerAdmissionCancelTests.test_cancel_rejects_idle_and_signals_only_active_disposable_child` |
| 22 | 19.755 | 45 | `tests.test_sound_design_delivery_export.SoundDeliveryExportTests.test_real_delivery_and_late_source_change_preserve_prior_output` |
| 23 | 18.990 | 47 | `tests.test_sound_design_delivery_export.SoundDeliveryExportTests.test_uploaded_cover_is_approved_exactly_and_replacement_refuses_delivery` |
| 24 | 18.047 | 769 | `tests.test_legacy_attribution_audit.LegacyAttributionAuditTests.test_current_inventory_is_exhaustive_bounded_and_rendered_once` |
| 25 | 17.463 | 12 | `tests.test_script_recovery_transaction.ScriptRecoveryTransactionTests.test_process_death_recovery_is_idempotent_through_real_reader` |
| 26 | 16.992 | 14 | `tests.test_generate_personas.PersonaAliasGraphCliTests.test_actual_cli_validates_alias_graph_and_preserves_latest_human_cast` |
| 27 | 16.047 | 2 | `tests.test_windows_import_boundary.WindowsImportBoundaryTests.test_application_and_preparer_import_with_fcntl_unavailable` |
| 28 | 15.328 | 3 | `tests.test_preparer_volume_publication.VolumePublicationTests.test_owned_process_death_at_publication_phases_is_fenced_and_recovers_old_set` |
| 29 | 15.288 | 10 | `tests.test_character_roster_failures.CharacterRosterFailureTests.test_abrupt_process_death_rolls_back_precommit_and_keeps_committed_apply_and_undo` |
| 30 | 15.285 | 1 | `tests.test_1724_voice_navigation_js.VoiceNavigationTests.test_guide_read_only_voices_open_writes_no_defaults` |
| 31 | 15.223 | 1 | `tests.test_1724_voice_navigation_js.VoiceNavigationTests.test_ordinary_voices_navigation_still_autosaves_defaults` |
| 32 | 14.712 | 37 | `tests.test_sound_design_delivery_export.SoundDeliveryExportTests.test_source_cover_uses_charged_stage_and_late_companion_edit_refuses_publication` |
| 33 | 13.484 | 8 | `tests.test_project_scope_failures.ScopeFailureTests.test_real_process_death_recovers_precommit_and_preserves_committed_apply_and_undo` |
| 34 | 13.375 | 1 | `tests.test_artifact_provenance_integrity.ArtifactIntegrityTests.test_tracked_artifacts_are_parseable_reproducible_and_consistent` |
| 35 | 12.948 | 2 | `tests.test_chain_book_fixtures.ArchivedBatchInterpreterTests.test_new_batch_uses_checkout_interpreter_and_preserves_explicit_override` |
| 36 | 12.872 | 29 | `tests.test_sound_design_candidate_routes.SoundCandidateRouteTests.test_delivery_source_differences_require_exact_report_confirmation` |
| 37 | 12.761 | 0 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_cli_checks_scores_only_after_shared_manifest_lock_admission` |
| 38 | 12.385 | 0 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_missing_original_cannot_be_mistaken_for_already_completed_recovery` |
| 39 | 12.185 | 2 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_fresh_cli_recover_entrypoint_restores_then_is_idempotent` |
| 40 | 12.137 | 0 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_corrupt_snapshot_fails_before_restoring_any_live_resources` |
| 41 | 12.006 | 0 | `tests.test_promotion_publication_transaction.PromotionPublicationTransactionTests.test_dry_run_does_not_recover_and_cli_refuses_pending_http_checkpoint` |
| 42 | 11.776 | 0 | `tests.test_dataset_builder_foreign_ownership.DatasetBuilderForeignOwnershipTests.test_mutations_refuse_foreign_generation_and_preserve_project_bytes` |
| 43 | 11.736 | 1 | `tests.test_native_windows_readiness_runner.NativePathSpellingTests.test_checkpoint_crash_fixture_matches_native_short_directory_spelling` |
| 44 | 11.677 | 0 | `tests.test_lora_backup_scan.LoraBackupScanTests.test_status_counts_current_file_sizes_without_reading_weight_payloads` |
| 45 | 11.520 | 55 | `tests.test_chapter_output_integrity.RetainedChapterIntegrityTests.test_damaged_selected_wav_and_mp3_rebuild_even_with_restored_size_and_mtime` |
| 46 | 10.843 | 0 | `tests.test_generation_model_binding.GenerationModelBindingTests.test_both_cli_artifacts_expose_configured_failover_and_reject_mixed_model_reuse` |
| 47 | 10.696 | 0 | `tests.test_speaker_review_api.SpeakerReviewApiTests.test_attempt_ceiling_and_resume_cache` |
| 48 | 10.649 | 0 | `tests.test_attribution_adapter.AdapterTransportTest.test_actual_http_rejects_oversized_valid_json_and_accepts_normal_response` |
| 49 | 10.618 | 3 | `tests.test_script_publication_cancellation.PublicationCancellationTests.test_published_worker_and_descendant_stop_without_cancelling_the_script` |
| 50 | 10.393 | 2 | `tests.test_book_preflight_ownership.BookPreflightOwnershipTests.test_real_child_cancel_keeps_claim_until_child_reaped` |

## 50 slowest modules

These are sums of individual test intervals, not separately measured module
import/setUpModule/tearDownModule times.

| Rank | Seconds | Tests | Popen events | Module |
| ---: | ---: | ---: | ---: | --- |
| 1 | 362.155 | 18 | 45 | `tests.test_adapter_checkpoint_transaction` |
| 2 | 316.368 | 13 | 2 | `tests.test_promotion_publication_transaction` |
| 3 | 203.299 | 10 | 477 | `tests.test_sound_design_delivery_export` |
| 4 | 72.778 | 24 | 21 | `tests.test_preparer_volume_publication` |
| 5 | 68.603 | 31 | 341 | `tests.test_chapter_export` |
| 6 | 51.984 | 15 | 262 | `tests.test_chapter_output_integrity` |
| 7 | 44.807 | 7 | 23 | `tests.test_cloud_reference_chain` |
| 8 | 37.609 | 25 | 91 | `tests.test_sound_design_cue_api` |
| 9 | 37.593 | 7 | 279 | `tests.test_project_narrator_context` |
| 10 | 37.289 | 7 | 358 | `tests.test_chapter_selection_integrity` |
| 11 | 36.216 | 18 | 54 | `tests.test_blinded_listening` |
| 12 | 32.797 | 15 | 0 | `tests.test_speaker_review_api` |
| 13 | 31.401 | 5 | 76 | `tests.test_sound_design_delivery` |
| 14 | 30.546 | 3 | 3 | `tests.test_1724_voice_navigation_js` |
| 15 | 29.782 | 10 | 86 | `tests.test_sound_design_approval` |
| 16 | 28.470 | 4 | 171 | `tests.test_pcm_mux_worker` |
| 17 | 27.296 | 8 | 69 | `tests.test_sound_design_candidate_routes` |
| 18 | 26.540 | 72 | 0 | `tests.test_audiobookshelf_delivery` |
| 19 | 26.146 | 14 | 152 | `tests.test_m4b_source_publication` |
| 20 | 26.080 | 4 | 6 | `tests.test_isolated_api_auth` |
| 21 | 25.958 | 4 | 6 | `tests.test_verifier_output_retention` |
| 22 | 24.727 | 1 | 6 | `tests.test_cors_auth_order` |
| 23 | 23.748 | 4 | 129 | `tests.test_chapter_plan_offsets` |
| 24 | 22.588 | 2 | 8 | `tests.test_project_scope_failures` |
| 25 | 22.317 | 51 | 18 | `tests.test_generate_personas` |
| 26 | 21.854 | 8 | 5 | `tests.test_m4b_cancellation` |
| 27 | 21.273 | 9 | 112 | `tests.test_chapter_export_quality` |
| 28 | 21.110 | 17 | 935 | `tests.test_legacy_attribution_audit` |
| 29 | 20.883 | 5 | 5 | `tests.test_benchmark_local_process_cancel` |
| 30 | 20.745 | 3 | 6 | `tests.test_preparer_admission_cancel` |
| 31 | 20.512 | 6 | 56 | `tests.test_sound_design_publication` |
| 32 | 19.982 | 2 | 13 | `tests.test_character_roster_failures` |
| 33 | 19.154 | 17 | 12 | `tests.test_script_recovery_transaction` |
| 34 | 18.156 | 48 | 448 | `tests.test_derived_file_hooks` |
| 35 | 18.132 | 4 | 798 | `tests.test_voice_drift_batches` |
| 36 | 17.525 | 14 | 87 | `tests.test_derived_metadata` |
| 37 | 17.043 | 25 | 99 | `tests.test_export_quality` |
| 38 | 16.711 | 9 | 105 | `tests.test_chapter_timing_cache` |
| 39 | 16.546 | 41 | 1 | `tests.test_adapter_id_aliases` |
| 40 | 16.047 | 1 | 2 | `tests.test_windows_import_boundary` |
| 41 | 15.787 | 23 | 184 | `tests.test_dialogue_continuity_eval` |
| 42 | 15.780 | 7 | 166 | `tests.test_chunk_audio_save_failure` |
| 43 | 15.769 | 5 | 6 | `tests.test_windows_native_ci` |
| 44 | 15.710 | 4 | 44 | `tests.test_audio_pcm_mux` |
| 45 | 15.479 | 11 | 0 | `tests.test_tts_external_pool` |
| 46 | 14.958 | 13 | 23 | `tests.test_retrain_artifact_completion` |
| 47 | 14.907 | 11 | 19 | `tests.test_external_tts_engine_lifetime` |
| 48 | 14.700 | 148 | 66 | `tests.test_qwen_review_v2_repairs` |
| 49 | 14.399 | 2 | 0 | `tests.test_dataset_builder_foreign_ownership` |
| 50 | 14.248 | 6 | 7 | `tests.test_chain_book_fixtures` |

## Local-only initial artifacts (not committed)

Under `C:/Users/Mitch/AppData/Local/Temp/aa2-windows-duration/phase1-bundle/`:
- `baseline-native.json`: exact final recorder output, all failure/error IDs,
  both top-50 lists and their contributions.
- `analysis.json`: inventory reconciliation, all module totals, long-test list,
  failure traceback examples and the focused media profile.
- `exploratory-powershell.json`: rejected wrong-child-shell run; not the baseline.
- `measure_unit_duration.py`: exact guarded recording instrument used for the
  authoritative run.
- `instrument-sanity.json`: the archived launcher's 34-test count/result check.
- `reproduce.sh`: original in-checkout convenience launcher, retained for
  provenance; after moving the bundle, use the explicit command above instead.
- `navigation_timer_probe.cjs`: deterministic failing timer-ownership probe.
- `preexisting-uninstrumented.log` and `media-profile-summary.txt`: narrow
  independent checks and profile summary.

Original full logs, per-test JSONL and stack dumps remain local in
`C:/Users/Mitch/AppData/Local/Temp/aa2-windows-duration/`, with authoritative
prefix `baseline-git-bash`. They are not copied wholesale into the evidence
patch because they contain megabytes of temporary paths and repeated logs.
