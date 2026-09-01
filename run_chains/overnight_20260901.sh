#!/bin/bash
# Local card, ~16 hours: the refusal stratification, then fidelity at full coverage.
#
# STAGE 1 is 10 minutes and answers a question nothing else can. The adapters
# refuse, and refuse the rows they would have got wrong - base scores 11-62% on
# declined rows against 65-77% on answered ones. Two readings survive: the gold
# genuinely was not in the roster (a defect, declining is correct), or the
# adapter declines regardless (learned). in_candidates separates them and was
# None on every serving row until #426 made both evaluators record the roster.
# This is the first artifact that can be stratified.
#
# STAGE 2 fills the rest. Seed 20260914 was the first ever to score 74 of 75
# adapters - every earlier seed scored 18, because 56 resolved their dataset to
# the literal string "data" until #422. Two seeds exist at full coverage; the
# 17 before them all describe the same 18 adapters. More seeds at 74 are the
# only ones that widen goal 2.7 rather than re-measuring its narrow slice.
#
# Each seed took ~4-5 h at full coverage, so three fill the night. Every stage
# is skipped if its artifact exists, so an interrupted run resumes.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
STAGE_LOG_DIR="$REPO/ab_test_runtime/logs/overnight_20260901"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

echo "[$(date -u +%FT%TZ)] STAGE 1: refusal stratification"
./run_chains/refusal_stratification_20260830.sh || echo "  stage 1 rc=$?"

# --needs-vram because stage 1 leaves llama-server holding the card:
# ensure_llama_server deliberately outlives its job, and on 2026-08-31 that
# cost seven idle hours when a fidelity seed was refused at 1350 MiB free.
for SEED in 20260916 20260917 20260918; do
    name="library_fidelity_seed_${SEED}_n20"
    out="ab_test_runtime/experiments/${name}.json"
    if [ -s "$out" ]; then
        stage_note "SKIP $name (already complete)"
        continue
    fi
    run_stage "$name" 6h --needs-vram -- \
        ./gpu_job.sh "$name" \
        ./app/env/bin/python -u app/experiments/library_voice_fidelity.py \
        --lines 20 --seed "$SEED" \
        --work "ab_test_runtime/${name}" --out "$out"
done
stage_summary overnight_20260901
echo "[$(date -u +%FT%TZ)] COMPLETE overnight_20260901"
