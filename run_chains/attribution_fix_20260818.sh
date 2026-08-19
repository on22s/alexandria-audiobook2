#!/usr/bin/bash
# Does telling the model who narrates fix the dialogue-to-NARRATOR failure?
#
# THE SYMPTOM, on books nothing here was developed against: 70.6% of spoken
# lines in mushoku18 and 78.0% in mushoku23 were left attributed to NARRATOR,
# and 85% of those had the speaker named in an adjacent line. The information
# was present and unused.
#
# THE INTERVENTION IS ALREADY MEASURED. On PDNC gold, supplying the
# first-person narrator's identity took attribution from 61.7% to 79.4% over
# 720 rows (pdnc_narrator_prior__clean-3book.json). The helper has existed
# since; generate_script never called it. It does now, and this re-runs the
# gold measurement against the current code so the claim is about what ships
# rather than about an experiment branch.
#
# MEASURED ON PDNC, NOT ON THE LIGHT NOVELS. mushoku has no answer key: it can
# show the symptom and cannot score the remedy. Every figure here comes from
# books with human labels.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/attribution_fix"
source "$REPO/run_chains/lib/stage.sh"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"

# The narrator arm needs an LLM; the preflight refuses the run rather than
# scoring an empty result, which is how a PR #308 remeasurement once recorded
# rc=1 and an artifact with no rows.
run_stage narrator_prior_current 3h --  \
    env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
    "$REPO/gpu_job.sh" narrator_prior_current \
    "$python" -u "$REPO/app/experiments/pdnc_narrator_prior.py" \
    --tag current-code --arms baseline narrator

stage_commit_artifacts narrator_prior_current "$REPO"
stage_summary attribution_fix

echo
echo "HOW TO READ IT. baseline is the production prompt as it ships; narrator"
echo "is the same prompt told who narrates. The 2026-08-08 run of this pair"
echo "gave 61.7% against 79.4%. A repeat near that says the intervention"
echo "survives the current code, and a repeat far from it says something in"
echo "the pipeline moved since - which is worth more than the headline."
