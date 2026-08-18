#!/usr/bin/bash
# Re-run the identity gate now that its clips can actually be read.
#
# The overnight attempt failed all 67 adapters with "System error" opening val
# clips that were present the whole time: ecapa_pairs runs its subprocess with
# cwd=APP, so the relative --dataset path resolved against app/. Two GPU hours,
# no measurement, and the chain reported COMPLETE. Both are fixed - absolute
# paths at the cwd boundary, and a strict gate that fails when adapters do.
#
# Goal 2.7 is built on 87 gate artifacts that carry no provenance at all. This
# is the run that lets that goal cite something replayable.
#
# Restartable: each adapter skips its own completed artifact, so a run cut off
# at the deadline resumes rather than restarts.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
STAGE_LOG_DIR="$runtime/logs/regate_rerun"
source "$REPO/run_chains/lib/stage.sh"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"

# Stop at 11:15, five minutes before the machine is wanted back.
DEADLINE=$(date -d "2026-08-18 11:15" +%s)
LEFT=$(( DEADLINE - $(date +%s) ))
stage_note "regate re-run has ${LEFT}s before the deadline"

if [ "$LEFT" -lt 600 ]; then
    stage_note "not enough time left to be worth starting"
    exit 0
fi

run_stage regate_rerun "${LEFT}s" -- \
    "$REPO/run_chains/regate_with_provenance_20260817.sh"
stage_commit_artifacts regate_rerun "$REPO"
stage_summary regate_rerun
