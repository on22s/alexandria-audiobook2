#!/usr/bin/bash
# Re-gate every adapter so its verdict can name the code that produced it.
#
# WHY. 87 gate artifacts carry no provenance at all - no commit, no host, no
# model, not even a dirty flag - and goal 2.7 is built on them:
#
#     "All 21 were retrained on the honest split and independently gated;
#      9 were promoted with a rollback receipt"
#     "breathy_alto_50s_f_fantasy failed at rank 1 (0.404), passed at rank 2
#      (0.503)"
#
# Those figures are read out of files that cannot say what made them, and
# promote_adapters.py ships voices on their strength. verify_adapter_identity
# now records provenance, so re-running puts goal 2.7 on evidence instead of
# recollection.
#
# THIS OVERWRITES THE OLD ARTIFACTS ON PURPOSE, and that is safe because they
# were committed first - `git diff` after this run is the interesting output,
# not a loss. Two outcomes, both worth having:
#
#   verdicts unchanged -> the originals were sound, and now they are
#                         attributable as well.
#   verdicts changed   -> something moved between August and now, and goal 2.7
#                         has been resting on numbers that no longer hold.
#
# I do not know which. That is the point of running it.
#
# ~2.0-2.7 min per adapter measured on this card, 67 adapters, so roughly
# 2.5-3 hours. It blocks on the GPU lock, so it can be launched while the
# PR #308 remeasurement is still running and will simply wait its turn.
set -uo pipefail

# Operational ceiling, not a predicted runtime: measured gates took 2.0-2.7
# minutes on the original card. Keep ample startup headroom, but never wait
# forever for one adapter. An override must still impose a positive deadline.
GATE_TIMEOUT="${REGATE_GATE_TIMEOUT-15m}"
if ! [[ "$GATE_TIMEOUT" =~ ^[1-9][0-9]*[smh]$ ]]; then
    echo "REGATE FAILED: REGATE_GATE_TIMEOUT must be a positive duration (s/m/h)" >&2
    exit 1
fi

REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
    exec "$REPO/gpu_job.sh" regate_with_provenance \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
else
    bash "$REPO/gpu_job.sh" --check-lock-owner "${ALEXANDRIA_GPU_LOCK_PID:-}" || exit 1
fi

PY="$REPO/app/env/bin/python"
EXP="$REPO/ab_test_runtime/experiments"
LOG="$REPO/ab_test_runtime/logs/regate_provenance"
source "$REPO/run_chains/lib/stage.sh" || exit 1
STAGE_LOG_DIR="$LOG"
mkdir -p "$LOG"
cd "$REPO"
QUEUE=$(mktemp "${TMPDIR:-/tmp}/regate_queue.XXXXXX") || exit 1
trap 'rm -f -- "$QUEUE"' EXIT
JOURNAL="$EXP/regate_provenance_campaign.json"
CAMPAIGN="$REPO/app/gate_campaign.py"

# Rebuild the queue from the artifacts themselves rather than a stored list:
# each one records the adapter path it gated, so the set re-gated is exactly
# the set that exists, and an adapter deleted since August drops out loudly
# rather than being silently skipped.
if ! "$PY" - <<'PYEOF' > "$QUEUE"
import glob, json, os
missing = []
for path in sorted(glob.glob("ab_test_runtime/experiments/gate_promote__*.json")):
    name = os.path.basename(path)[len("gate_promote__"):-len(".json")]
    adapter = json.load(open(path)).get("adapter")
    if not adapter or not os.path.isdir(adapter):
        missing.append(name)
        continue
    # The dataset is <adapter-parent>/data, holding train/ and val/. Verified
    # across all 67 before this ran: my first guess was <adapter-parent>
    # itself, which has no val/metadata.jsonl anywhere, and would have failed
    # every adapter in the queue about three hours in.
    data = os.path.join(os.path.dirname(adapter), "data")
    if not os.path.exists(os.path.join(data, "val", "metadata.jsonl")):
        missing.append(f"{name} (no val split at {data})")
        continue
    print(f"{name}\t{adapter}\t{data}")
for name in missing:
    print(f"# MISSING ADAPTER {name}", flush=True)
PYEOF
then
    echo "REGATE FAILED: could not build adapter queue" >&2
    exit 1
fi

total=$(grep -vc '^#' "$QUEUE")
echo "REGATE START $(date -u +%FT%TZ)  adapters=$total"
grep '^#' "$QUEUE" || true

run_stage campaign_start 0 -- "$PY" "$CAMPAIGN" start "$QUEUE" "$JOURNAL"
if ! is_stage_successful campaign_start; then
    stage_summary regate_campaign
    exit 1
fi
stage_commit_artifacts campaign_start "$REPO" "$JOURNAL" || exit 1

done_n=0
failed_n=0
rejected_n=0
while IFS=$'\t' read -r name adapter data; do
    case "$name" in \#*) continue;; esac
    out="$EXP/gate_promote__$name.json"
    run_stage "regate_$name" "$GATE_TIMEOUT" -- \
        "$PY" -u app/experiments/verify_adapter_identity.py \
        --adapter "$adapter" --dataset "$data" --lines 6 --out "$out"
    result="${STAGE_RESULT[regate_$name]}"
    rc=0
    if [ "$result" != ok ]; then
        rc="${result#failed:}"
    fi
    if [ "$rc" -eq 3 ]; then
        rejected_n=$((rejected_n + 1))
        echo "REJECTED $name (measured identity verdict)"
    fi
    done_n=$((done_n + 1))
    [ "$rc" -ne 0 ] && failed_n=$((failed_n + 1))
    echo "[$done_n/$total] rc=$rc $name"
    run_stage "campaign_result_$name" 0 -- "$PY" "$CAMPAIGN" result "$JOURNAL" "$name" "$rc"
    stage_commit_artifacts "regate_$name" "$REPO" "$out" "$JOURNAL"
done < "$QUEUE"

run_stage campaign_complete 0 -- "$PY" "$CAMPAIGN" complete "$JOURNAL"
if is_stage_successful campaign_complete; then
    stage_commit_artifacts campaign_complete "$REPO" "$JOURNAL"
fi

# A STRICT GATE, BECAUSE THIS LOOP LIED FOR TWO HOURS. Bash discards each
# iteration's exit status, and `set -e` does not apply inside a loop body, so
# on 2026-08-18 all 67 adapters failed with rc=2 and this script still printed
# REGATE COMPLETE and exited 0 - the caller logged "OK regate" and moved on.
# Databricks names the same shape SUCCESS_WITH_FAILURES: a run green enough to
# notify success while containing real failures, and their fix is the one used
# here - end in a gate that fails when the parts did, and never put a
# reporting step after it that can restore a zero exit.
if [ "$failed_n" -gt 0 ]; then
    echo "REGATE FAILED $(date -u +%FT%TZ): $failed_n of $total adapters" >&2
    echo "  Measured rejections: $rejected_n; execution failures: $((failed_n - rejected_n))." >&2
    echo "  Read per-adapter evidence before re-running: $LOG/regate_<adapter>.log" >&2
    exit 1
fi

stage_summary regate_artifacts || exit "$?"
echo "REGATE COMPLETE $(date -u +%FT%TZ)"
echo
echo "WHAT TO READ:"
echo "  git diff --stat ab_test_runtime/experiments/gate_promote__*.json"
echo "    Every file should gain a provenance block. Any file whose"
echo "    median_ecapa or passed flag ALSO changed is the real finding -"
echo "    it means goal 2.7's promotion record moved."
echo "  git diff ab_test_runtime/experiments | grep -E '^[-+] *\"(passed|median_ecapa)\"'"
