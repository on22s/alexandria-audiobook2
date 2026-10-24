# CI and release-check consolidation (#1557)

The consolidation removes repeated setup and checks while retaining distinct
production entrypoints, recovery boundaries, signal cases, and native media
validation. It follows the export-pause change merged in #1558; that change is
part of the base and is not changed here.

## Scope and review

The audit covered `ready.sh`, `verify_release.py`, every GitHub workflow, the
unit inventory, and the full bodies of 1,293 tracked test-source files. Frozen
source was split into seven packets for DeepSeek Flash and Kimi K3. Candidate
claims were checked against production code and surviving tests, then reviewed
by GPT-6.1 Sol at medium reasoning. A roster scan alone was not treated as a
complete source review. Provider claims are hypotheses, not deletion authority.

## Pipeline changes

* `ready.sh` still regenerates indexes and refuses unstaged derived files before
  expensive tests. The shared verifier now owns the evidence checks instead of
  repeating them in the shell script and a separate CI step.
* `--check-evidence` enforces all four evidence checks before unit tests,
  including `ready.sh --shard 2/3` and `3/3`. Flagged evidence failures retain
  exit 2; plain verifier failures retain exit 1. Inventory and compilation
  remain primary-shard gates. Goal evidence is now checked by primary CI too.
* The two native audio workflows become one three-OS workflow. It preserves the
  exact 34-path trigger union, all nine original test modules, the finished M4B
  cover probe, pixel-budget checks, native tool version logging, and dependency
  versions. The shared output-sink suite runs once per platform instead of twice.
* Module weights are refreshed from the valid complete baseline and focused
  measurements. All 1,278 executed modules have weights; 438 were absent from
  the old file. The primary-shard allowance is recalibrated from the latest
  green CI reports, replacing the obsolete 165-second local-weight allowance
  with 45 seconds. Assignment remains deterministic, whole-module, and checked
  for exactly-once execution. Weights change balance, never selection coverage.

## Test dispositions

| Area | Removed or consolidated work | Surviving proof |
| --- | --- | --- |
| Artifact provenance | Four complete corpus traversals become one streaming traversal | More-than-20 non-vacuity guard; separate parse, script and completion subtests for every artifact |
| Legacy attribution audit | Repeated `build_audit` corpus construction | Inventory semantics and markdown membership checked on the same real audit |
| Added-commit identity | Repeated Git topology construction and a copied new algorithm | Actual `added_commit` at both parent-order vantage points, two full-history adds, known-bad historical negative control, untracked-file refusal |
| Benchmark hashing | Second seven-validator setup loop | Bounded reads and 1 MiB witness, exact hashes, tamper refusal, missing-model refusal, input restoration; actual clone/LoRA worker admission-order test retained unchanged |
| Chapter export | Separate changed-only export fixture | Real loader sees exactly the changed chapter's original chunk rows inside the existing manifest/reuse scenario |
| LLM regression tests | Identical catastrophic retry-budget test | Exact-five-attempt exhaustion and separate near-miss bonus cases |
| GPU wrapper | Identical lifecycle-order test; fixed pause/descendant waits | Existing QUEUED/IDENT/START/OK oracle; three live production pause iterations, free authoritative lease, no START; stable live pidfd before/after termination |
| GPU status markers | NO_LLM subset test; one-second fixture sleeps | Complete terminal-marker matrix, bare-START positive control, explicit process readiness and owned process-group cleanup |
| Overnight campaign | Second successful retry in an already-qualified failed-restore scenario | Failure/backup/server-stop assertions stay; actual failed-restore-then-retry entrypoint remains in `test_config_backup_resume` |
| Active LLM profile | Two historical test-local implementations | Real helper with explicit local/remote results, both profiles, stale mirror, missing/None/empty profile and migration inputs |
| LoRA arm selection | Repeated default/base-only tuple test | Both actual arm-selection cases remain in `test_lora_serving_eval_modes`; unique metadata assertions stay |
| Disk admission | Second disk-probe failure scenario | Existing tranche test covers the identical exception branch plus insufficient/sufficient-space controls |
| LoRA cleanup | Smaller cleanup scenario | Admitted-snapshot case asserts actual rejected-directory absence, selected directory preservation and later training directory preservation |
| Shipped lexicon coverage | Repeated three-state and gap fixtures | Real loader; casefold in all buckets, empty and populated entry dictionaries, two plain-OK values, disjointness, sole uncovered term |
| Serving CLI | Flag-name presence in source | Real `--help` execution with the identical complete flag tuple; defaults and historical safety guards stay |
| Experiment manifest | Second duplicate-ID and invalid-write scenarios | Real validation/write refusal plus explicit output-file absence; distinct empty-environment case stays |
| Training split | Copied metadata selector and fixture-only assertions | Real trainer reader checks 180 train/20 validation rows, exact selected rows, relative paths; original root-only 40-row legacy shape; invalid split/native validation cases stay |
| Classification policy | Three source-string tests | One real policy-builder matrix checks default classification/instruction temperatures, global overrides and model-profile precedence |
| Two-step attribution | Callable-only validator assertion | Real provider called with stubbed stage-one response; rejecting validator called with exact entries; provider returns no entries |
| Miscellaneous | Identical trigram/prompt-variant checks; shadowed same-name Unicode method; prose-phrase, local arithmetic, stdlib slicing/abspath and always-true contamination assertions | Existing real production checks retained; Unicode deletion changes no executed test count; unanswered-arm historical negative control moved into the real-record test |

No cases were removed merely because they were slow or looked similar.
Mixed/stratified campaign aliases and INT/TERM cases exercise different wiring.
The native timing subset's 120-row case is distinct from the six-row comparison.
Queue self/parent exclusion guards are not subsumed by tests of other processes.
The narrator-specific ordering guard requires assignment/read presence that the
more general AST check does not. JavaScript callback and real form-handler
refresh tests exercise different entrypoints. These cases remain.

The `test_m4b_cover_admission.py` native-workflow trigger was already present
without a corresponding native command before consolidation. The test remains
in Linux unit discovery; no executed native test was removed. Treat any added
platform admission coverage as a separate enhancement with dependency and
runtime qualification, rather than relabeling it as a consolidation regression.

## Measurements and qualification

The valid clean baseline is main `ca50db238884dfed79888a3f62235894db75de15`:
9,918 unique tests, zero skips, 1,163.082 seconds of unit execution. A first
unguarded multiprocessing profiler run was invalidated and is excluded from all
measurements. The replacement observer was checked for exact unique test IDs.

The 28 initially edited modules took 145.845 seconds in that complete baseline
and 110.483 seconds in a combined focused run: all 483 tests passed. This is one
local observation, not a promised end-to-end speedup. The later training-reader
consolidation is qualified separately and by the complete candidate run.

The green #1558 CI unit job reports show 1,853.376 seconds of aggregate unit
execution and 72.447 seconds of primary-only gates. Local measured unit time /
CI measured unit time is 0.627, yielding a rounded 45-second local-weight
allowance. Runner speed and contention vary; the new CI run is the end-to-end
qualification. A separate repeated evidence step took approximately 46 seconds
in the prior green run and is removed. Native setup reductions affect aggregate
runner work; they do not imply an equal reduction in wall-clock critical path.

The full 9,885-test local precursor ran in 1,099.564 seconds with two failing
expectations: the shell fixture still counted four removed checks, and the
sharding fixture still required a 100-second gap after the allowance changed
to 45. It had zero errors/skips. This run is recorded as **failed**, not a
qualification pass. Both expectations were corrected, and two independently
verified duplicate cases were then removed. All remaining method bodies match
the precursor except those two corrected fixtures.

The final inventory is **9,883 tests**: 39 old identities removed and four
consolidated identities added, a net reduction of 35. The repaired fixture and
surviving duplicate witnesses passed in a 90-test focused run (1.775 seconds).
`./ready.sh --shard 1/3` then passed 3,140 unique tests and every release gate in
374.808 seconds, including the corrected shell fixture, all four evidence
indexes, compilation/inventory, API contract and 86 API checks. Twelve API
checks retain the existing `--full` exclusion. The local shard is not described
as a full 9,883-test run. GitHub's three shards and exactly-once aggregate check
qualify the final complete inventory, alongside the native three-OS jobs.

Exact-head CI results and before/after job timings are recorded in the follow-up
PR after execution. No GPU synthesis, pitch, or listening-quality claim follows
from these CPU tests.

The first follow-up CI attempt exposed an existing intermittent failure in
`test_managed_server_chain`'s healthy-listener fixture. Sixty unchanged repetitions
reproduced two failures. A separate changing-state CPU process reproduced
corrupted `/proc/PID/status` field boundaries with line-by-line shell reads:
all three old-reader probes lost `PPid:`, while three snapshot-reader probes
each passed 2,000 reads. The ownership guard now captures the status text
before parsing. It retains every ancestry, descriptor, inode and exclusive
lock check, and still refuses missing or malformed parent data. These probes
are diagnostic evidence, not a claim that the original CI attempt passed.
The existing `test_gpu_lock_owner` direct/nested real-owner fixture now rejects
line reads from live procfs while permitting reads from captured text. Running
that fixture against the old script fails both subcases; the new script passes.
This deterministic policy check complements the separately recorded live-process
probe, rather than simulating the kernel race with a new timed CI stress test.

The default failure path uploads the primary verification report immediately;
it no longer reruns the complete failed shard on the base before publishing
that report. The failed verifier still fails the job. Base comparison provides
diagnostic evidence, rather than an acceptance gate, and remains available
manually with the same interpreter and installed CI dependencies:

```bash
# Set BASE_SHA to the PR's recorded base commit and SHARD to its failed shard.
# Select the absolute interpreter with the CI dependencies installed.
CI_PYTHON=$(command -v python)
# Use a disposable directory outside the live application checkout.
comparison=$(mktemp -d)
git worktree add --detach "$comparison/base" "$BASE_SHA"
status=0
(cd "$comparison/base/app" && "$CI_PYTHON" verify_release.py --shard "$SHARD" \
  --json-report "$comparison/base-release-report.json") || status=$?
git worktree remove --force "$comparison/base"
printf 'Base comparison exit status: %s; evidence: %s\n' "$status" "$comparison"
```

Keep the original failed report alongside this evidence. A green base run
does not by itself prove causation when a test or runner is intermittent.
The same shard number may contain different tests after a partition change;
compare report identities. Using the PR's dependency environment also differs
from rebuilding the base's environment, which matters for dependency failures.
