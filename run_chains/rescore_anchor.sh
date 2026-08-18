#!/bin/bash
# Re-score the three anchor corpora.
#
# WAS: a loop printing "rc=$?" per corpus and exiting 0 regardless - three
# chances to fail silently. Bash discards a loop iteration's status, so the
# capture was decorative; run_stage counts them and stage_summary is the gate.
#
# The greps are kept: ANCHOR INVALID is the line that says the measurement is
# meaningless even when the process exits 0, and it belongs in the log next to
# the stage result rather than in a separate file nobody opens.
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
STAGE_LOG_DIR="$REPO/ab_test_runtime/logs"
source "$REPO/run_chains/lib/stage.sh"
cd "$REPO/app"

for t in aishell3 kokoro ljspeech; do
    run_stage "rescore_$t" 2h -- \
        "$REPO/app/env/bin/python" -u experiments/ljspeech_score.py \
        --generated "$REPO/ab_test_runtime/experiments/${t}_generate.json" \
        --out "$REPO/ab_test_runtime/experiments/${t}_score.json"
    grep -E 'human_vs_human|ANCHOR INVALID' \
        "$STAGE_LOG_DIR/rescore_$t.log" 2>/dev/null | tail -2
done

stage_summary rescore_anchor
