#!/usr/bin/bash
# Three ideas from the literature, in the order that de-risks them.
#
# WHERE THIS COMES FROM. arXiv 2608.02359 (2026) reports 99.3% on EXPLICIT
# quotations of PDNC with an encoder, and 94.9% for a one-pass Llama-3-8b.
# This project's arm gets 52.9% on the same category of the same corpus.
# Explicit quotes are the EASY case - PDNC defines them as introduced by a
# named mention - so a coin-flip there is a defect, not a frontier.
#
# STAGE 1 tests the cheapest explanation, which is already measured to be
# true: our context window is 400 characters either side and contains the
# speaker's name for only 68.5% of explicit quotes. At 3200 it contains it for
# 98.2% (pdnc_context_audit.json). Accuracy splits on exactly that property -
# 64.8% when the name is visible, 26.9% when it is not - so this stage should
# move Explicit a long way if the diagnosis is right, and if it does not, the
# diagnosis is wrong and the next two stages are the interesting ones.
#
# The rebuilt fixtures differ from the shipped ones ONLY in window size: ids,
# speakers, aliases, roster and quote types are copied verbatim, and every one
# of the 2,494 entries was located by PDNC quote id rather than by searching
# for its text, which silently mislocated a third of them in a first attempt.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/attribution_context_20260820"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

running() { pgrep -f "run_chains/$1" 2>/dev/null | grep -qv -e "^$$\$" -e "^$PPID\$"; }
for chain in overnight_20260820b.sh settle_2_6_20260820.sh second_english_eval_20260820.sh; do
    stage_note "waiting for $chain"
    while running "$chain"; do sleep 120; done
done

# The fixtures are rebuilt here rather than committed pre-built, so the window
# is a parameter of the run and not a fact about the repository.
run_stage rebuild_fixtures 20m -- \
    "$python" -u "$REPO/app/experiments/pdnc_gold_rebuild.py" \
    --pdnc "$runtime/pdnc/data" --window 3200

run_stage two_stage_w3200 8h -- \
    env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
    "$REPO/gpu_job.sh" two_stage_w3200 \
    "$python" -u "$REPO/app/experiments/two_stage_attribution.py" \
    --fixtures "$REPO/app/fixtures/attribution_gold_pdnc_prideandprejudice_w3200.json" \
               "$REPO/app/fixtures/attribution_gold_pdnc_theawakening_w3200.json" \
               "$REPO/app/fixtures/attribution_gold_pdnc_thesignofthefour_w3200.json" \
    --limit 1300 --keep-prompts --tag w3200 \
    --out "$runtime/experiments/two_stage_attribution_w3200.json"
stage_commit_artifacts two_stage_w3200 "$REPO"

# Same rows, same model, one variable. Scored against the 400-char run already
# on disk so the comparison is paired rather than two headline percentages.
run_stage compare_windows 20m -- \
    "$python" -u "$REPO/app/experiments/two_stage_selection_gap.py" \
    --artifact "$runtime/experiments/two_stage_attribution_w3200.json" \
    --out "$runtime/experiments/two_stage_selection_gap_w3200.json"
stage_commit_artifacts compare_windows "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary attribution_context_20260820
