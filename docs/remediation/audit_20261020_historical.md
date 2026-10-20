# Historical driver boundaries — 2026-10-20

Execution tasks E03 (#1422), E14 (#1423), and E15 (#1424) cover dated tooling. These are archival dispositions for this remediation, not a new scientific campaign. The original producers, outputs and logs remain unchanged. A dated ALL DONE, COMPLETE, nonempty output or zero exit is not a current validated receipt.

## Before any new campaign

Create a separately named current runner with its own output directory and receipts. Keep the original run intact. Use the shared GPU admission and descendant ownership controls; never preempt another job or substitute a private lock for inference. Require successful readiness and an admitted working directory before dispatch. Bound waits and refuse unavailable process/queue inspection.

Use run_chains/lib/stage.sh to record each attempted stage and return the final stage_summary status. run_stage records failures and intentionally returns control so independent work can continue: recording a failure alone does not make the shell exit fail. Use --requires-ok only for genuine data dependencies. Validate cache completion and matching source/model/prompt inputs, not file existence. Retain partial artifacts explicitly as failed/incomplete evidence.

For queue dependencies, wait_for_chain distinguishes stopped status 1 from inspection-error status 2. A direct while chain_running loop discards that distinction. Its optional appearance grace does not cap the later wait for a running chain; a current runner must apply an appropriate outer deadline. An independent stage should still run after another independent stage fails, but the campaign aggregate must preserve the failure.

## E03: dated chunk-probe recipe

`ab_test_runtime/experiments/three_pass_grimgar25_20260815/run.sh` assumes a local absolute checkout, an app/env interpreter, llama-server on port 8090, and its dated input/output paths. Its readiness loop continues after all probes fail, and an unsuccessful cd does not prevent relative worker dispatch. A CPU-only extraction of the original loop/cd/timeout tail reproduces both cases without starting the original server. The native timeout worker exit 7 remains exit 7: a blanket claim that every worker failure becomes success is refuted.

Archive this recipe unchanged. A future probe runner must select the current engine constructor/runtime, explicitly confirm readiness, guard cwd, supervise its own server/worker and write a new receipt tied to the exact source. Do not overwrite result.json or old logs in the dated directory.

## E14: all 18 retained driver observations

The 18 observations map to 17 actual entry points. The cited overnight_20260901.sh already propagates its final stage summary; observation 038-13 belongs to overnight_20260830b.sh. Preserve the original citation and this correction. The existing LEGACY_UNGUARDED exception list only explicitly exempts run_rebuild_retrain.sh among these entries; this catalog does not enlarge or weaken that test exemption.

| Review entry | Archived entry point | Observed boundary | Required before new reuse |
| --- | --- | --- | --- |
| 001-06 | `ab_test_runtime/regate_all75_guarded_20260906.sh` | Build hard failures and clean data refusals both enter the absent-holdout branch, while the dated driver ends ALL DONE without failure aggregation. | Keep build refusal distinct from a worker failure; aggregate every attempted gate and validate fresh holdouts. |
| 001-07 | `ab_test_runtime/regate_nine_guarded_20260906.sh` | The dated gate returns 0 for missing adapters and after verifier failures, and absent holdouts are skipped; final ALL DONE is not an authoritative success receipt. | Missing adapters/holdouts and verifier failures need explicit refused/failed outcomes; the final aggregate must stay nonzero. |
| 001-08 | `ab_test_runtime/rebuild_failing_20260905.sh` | gate explicitly returns 0 after capturing/logging the worker status. This is the historical non-aggregated-status pattern. | Consume the gate worker status and aggregate it; an echoed rc must not become a success receipt. |
| 001-09 | `ab_test_runtime/half_arm_guarded_20260906.sh` | The driver logs each verifier rc then ends ALL DONE without aggregation. | Aggregate all gate results before writing any new success receipt. |
| 001-10 | `ab_test_runtime/thinking_effort_sweep_20260906.sh` | Each evaluation rc is echoed and the loop ends ALL DONE regardless of worker failure. | Aggregate evaluation statuses and validate expected rows before declaring the sweep complete. |
| 001-11 | `ab_test_runtime/rank_four_book_rerun_20260906.sh` | The failing exit/artifact check is consumed by a warning echo, followed by ALL DONE. | A missing result or nonzero evaluator must fail the new campaign aggregate. |
| 001-12 | `ab_test_runtime/measure_five_terms_20260904.sh` | The final five_terms.complete marker is written regardless of per-verdict worker status. | Write a new completion receipt only after all required verdict runs and their artifacts pass. |
| 001-13 | `ab_test_runtime/pipeline_repeats/pipeline_repeats.sh` | Each repeat status is only echoed and the final scorer status is followed by an unconditional scored message. | Validate each repeat and the scorer output; aggregate all outcomes. |
| 001-14 | `ab_test_runtime/pipeline_repeats/pipeline_repeats.sh` | The dated repeat driver waits on a log phrase forever without a deadline or producer-liveness check. | Use a bounded wait with producer identity/liveness and inspection-error refusal; a log phrase is insufficient. |
| 001-15 | `ab_test_runtime/run_collect_all_ab.sh` | The A/B collector logs code but does not aggregate or return it at the end. | Aggregate and return every model/book result, keeping missing/failed runs visible. |
| 038-07 | `run_chains/longref_arm_20260826.sh` | The dated longref caller uses while chain_running directly, treating queue inspection status 2 as clear instead of wait_for_chain refusal. The global GPU lock still serializes actual worker GPU use. | Use wait_for_chain and propagate its inspection-error status; a direct while loop treats status 2 as clear. |
| 038-08 | `run_chains/morning_20260818.sh` | The dated morning driver commits the output path even after recording generation/completion failure. Its stage_summary remains red; partial artifacts are retained historical evidence. | Partial outputs may be retained as failure evidence but must stay labelled partial and outside success caches. |
| 038-11 | `run_chains/overnight_fidelity_replication_20260822.sh` | The dated fidelity driver skips on nonempty file existence; a partial checkpoint artifact can be skipped. Later drivers use completion checks. | Resume only after validating completion and input identity; nonempty file existence is insufficient. |
| 038-13 | `run_chains/overnight_20260830b.sh` | Stage 1 status is only logged; the final COMPLETE is unconditional, including a cached stage 2. The cited 20260901 final summary is a different caller. | Aggregate the independent first and second stages, including cached-stage cases; do not make independent data depend on each other. |
| 039-00 | `run_chains/run_rebuild_retrain.sh` | run_rebuild_retrain.sh only echoes the retrain rc; it is explicitly listed in test_chain_failure_guards.LEGACY_UNGUARDED. | This is the explicit legacy exception; a new runner must consume and aggregate the retrain status. |
| 039-01 | `run_chains/separator_allrows_20260820.sh` | The dated separator driver scores/commits after generation stages without requires-ok dependencies. It does retain stage failures for the final stage_summary, so this is stale/partial evidence debt, not a demonstrated false overall release green. | Require successful producers for their dependent scorers and validate current artifacts; keep independent work runnable. |
| 039-05 | `run_chains/verify_gate_test.sh` | The manual known-good/bad gate script checks worker exit codes but not freshly published artifact bytes. Retain as historical gate improvement, not a verified missing current artifact producer. | Verify expected accept/reject status plus newly published artifact identity and contents. |
| 039-08 | `run_chains/unanswered_stratification_20260830.sh` | The dated stratification driver exits 0 when candidate rows are absent and prints COMPLETE; the source intentionally refuses to report numbers, but its completion marker does not distinguish refusal. | A refusal to report numbers is an explicit refused/inconclusive result, not a new successful measurement. |

## E15: frozen identity verifier

`ab_test_runtime/evidence/thunder_results_20260930/verify_identity.py` is part of the dated evidence snapshot and records its own script hash. Keep that script and its JSON evidence unchanged. Its historical model-hash loop reads ab_test_runtime/ecapa, while the current _ecapa_batch worker resolves cache/ecapa through get_ecapa_model_dir. The old receipt is not recertified for the current worker by this remediation. On accepted main `e6b87326180d16e671320a495ab278afdb4ab1f4`, a tracked source/document search finds one documentation link in docs/results/thunder-campaign-2026-09-30.md and no invoking shell/Python/JavaScript caller in that search. The linked Artifacts and replay paragraph describes the dated runtime. That paragraph now explicitly identifies the frozen runtime and links this disposition. This is a bounded caller search, not a claim about untracked private launchers.

If a current verifier is required later, give it a new name and new output path. Resolve the actual model directory through _ecapa_batch.get_ecapa_model_dir, hash the exact model bytes used, and reject a fixture where current and legacy directories contain different bytes. Preserve the old hashes as historical evidence. This archival batch does not revive the old producer or claim a new ECAPA/inference/listening result.

## Evidence boundary

The [18-entry catalog](audit_20261020_historical_catalog.json) retains each original citation, actual path and source SHA256. The [readiness characterization](audit_20261020_historical_readiness.json) and [frozen-cache characterization](audit_20261020_historical_identity.json) were rerun on accepted main `e6b87326180d16e671320a495ab278afdb4ab1f4`. The [preservation manifest](audit_20261020_historical_preservation.json) binds the producers and dated verifier evidence to that source. The tracked caller search (`git grep -n -F verify_identity.py -- '*.py' '*.sh' '*.js' '*.md'`) records the historical report link and the archival references added here; it does not inspect untracked private launchers.

Reproduce the CPU-only characterizations from the repository root, writing fresh receipts outside the frozen evidence directories:

```bash
python3 docs/remediation/probes/probe_archived_readiness.py --output /tmp/current-readiness.json
python3 docs/remediation/probes/probe_frozen_identity.py --output /tmp/current-frozen-cache.json
```

Neither command starts a server, loads a model or runs the historical campaign. Existing shared-stage, queue-inspection and completion-cache controls remain the contracts for any separately named replacement. Release/CI/merge acceptance is recorded in the [batch plan](audit_20261020_plan.md). Archive markers or file hashes prove scope and preservation; they do not prove model quality or a historical measurement was correct.
