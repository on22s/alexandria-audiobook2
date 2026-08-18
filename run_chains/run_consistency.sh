#!/bin/bash
# Speaker consistency across the datasets. CPU only - no gpu_job.sh wrapper,
# because it takes no GPU and should not queue behind work that does.
#
# WAS: the exit code was echoed and discarded, so a crashed run and a clean one
# were indistinguishable to whatever ran this.
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
STAGE_LOG_DIR="$REPO/ab_test_runtime/logs"
source "$REPO/run_chains/lib/stage.sh"
cd "$REPO/app"

run_stage dataset_speaker_consistency 3h -- \
    "$REPO/app/env/bin/python" -u experiments/dataset_speaker_consistency.py

stage_summary run_consistency
