#!/usr/bin/bash
# Finish the re-gate after the first pass stops at its deadline, then leave the
# indexes correct.
#
# The 11:15 cap on the first pass was chosen to hand the machine back, not
# because the work fits: 67 adapters at the measured ~1.7 min each is about
# 115 minutes and the window was 101. Whatever is left is a handful of
# adapters, and each one skips its own completed artifact, so this resumes
# rather than restarts.
#
# It waits for the first pass to EXIT rather than racing it - gpu_job.sh would
# serialise them anyway, but two chains queued on one lock makes the queue log
# unreadable.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
STAGE_LOG_DIR="$runtime/logs/regate_finish"
source "$REPO/run_chains/lib/stage.sh"
source "$REPO/run_chains/lib/queue.sh"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"

# 11:45, so everything including the index refresh is done before noon.
DEADLINE=$(date -d "2026-08-18 11:45" +%s)

# Match the actual shell script, not unrelated command-line mentions.
first_pass_running() { chain_running "regate_rerun_20260818.sh"; }

stage_note "waiting for the first re-gate pass to exit"
while first_pass_running; do
    [ "$(date +%s)" -ge "$DEADLINE" ] && { stage_note "deadline reached while waiting"; exit 0; }
    sleep 60
done

LEFT=$(( DEADLINE - $(date +%s) ))
if [ "$LEFT" -gt 600 ]; then
    stage_note "finishing the remaining adapters with ${LEFT}s"
    run_stage regate_finish "${LEFT}s" -- \
        "$REPO/run_chains/regate_with_provenance_20260817.sh"
else
    stage_note "SKIP regate_finish: only ${LEFT}s left"
fi

# THE INDEXES LAST, AND ONLY HERE. 67 rewritten artifacts make all three stale,
# and refreshing them mid-run would dirty tracked files for no benefit while
# more artifacts were still being written.
run_stage refresh_indexes 0 -- \
    "$REPO/app/env/bin/python" "$REPO/refresh_indexes.py"
if ! is_stage_successful refresh_indexes; then
    stage_summary regate_finish
    exit 1
fi

git -C "$REPO" add ab_test_runtime/experiments/ ab_test_runtime/audit/ \
    RESULTS_INDEX.md results_index.csv LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md >/dev/null 2>&1
if ! git -C "$REPO" diff --cached --quiet; then
    git -C "$REPO" commit -q -m "Re-gated adapters and refreshed indexes

Every artifact here was measured against clips the harness could
actually read, and carries a commit, a clean-tree flag and a harness
hash - which is what goal 2.7 has been citing without.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" && stage_note "committed"
fi

stage_summary regate_finish
