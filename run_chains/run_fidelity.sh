#!/bin/bash
# Library-wide voice fidelity, through the shared stage runner.
#
# WAS: `gpu_job.sh ... ; echo "rc=$?"`. The exit code was printed and thrown
# away, so this script always succeeded - the shape that let the re-gate report
# COMPLETE while all 67 of its adapters failed on 2026-08-18.
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
export GPU_LOCK="${GPU_LOCK:-$HOME/.alexandria_gpu.lock}"
export GPU_QLOG="$REPO/ab_test_runtime/logs/gpu_jobq.log"
STAGE_LOG_DIR="$REPO/ab_test_runtime/logs"
source "$REPO/run_chains/lib/stage.sh"
cd "$REPO/app"

run_stage library_voice_fidelity 4h -- \
    "$REPO/gpu_job.sh" library_voice_fidelity \
    "$REPO/app/env/bin/python" -u experiments/library_voice_fidelity.py --lines 4

stage_summary run_fidelity
