#!/usr/bin/bash
# Do the two big attribution levers add, or do they attack the same errors?
#
# Both are measured, separately, and never together:
#
#   wide context   400 -> 3200 chars   52.9% -> 65.6%   two_stage_attribution_w3200
#   narrator prior first-person meta   61.7% -> 79.4%   pdnc_narrator_prior__clean-3book
#
# THERE IS REASON TO EXPECT THEY OVERLAP. The wide-context run's errors are
# concentrated exactly where the prior acts: it predicts DR. WATSON 216 times
# against a gold of 148, under-predicts MR. SHERLOCK HOLMES 13 times against
# 237, and Holmes -> Watson is the single largest confusion at 76 of 857. Watson
# narrates The Sign of the Four; his alias group literally contains NARR. If the
# prior fixes those, the combination gains far less than 12.7 + 17.8, and a
# document adding them would be wrong.
#
# ONE BOOK, DELIBERATELY. Of the three fixtures at the wide window, only
# TheSignOfTheFour is first person - Pride and Prejudice and The Awakening are
# third person and the prior does not apply to them. Running all three would
# dilute the effect across two books it cannot touch and report a smaller
# number for the wrong reason.
#
# THE CONTROL ALREADY EXISTS. two_stage_attribution_w3200.json is the same
# fixture, same window, same model, without the prior - so this run is the
# treatment arm of a paired comparison rather than a fresh pair.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/narrator_x_context_20260821"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"
source "$REPO/run_chains/lib/queue.sh"

refuse_if_dirty "$REPO" || exit 1
python=$(resolve_python "$REPO") || { echo "no interpreter" >&2; exit 1; }

for chain in dialogue_map_5_3_20260826.sh second_english_eval_20260820.sh; do
    wait_for_chain "$chain"
done

# REQUIRE_VRAM_GB=0: this generates through llama-server, which holds the card.
# The 4 GB gate is for TTS stages and would refuse this one.
run_stage narrator_x_context 4h -- \
    env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 "$REPO/gpu_job.sh" narrator_x_context \
    "$python" -u "$REPO/app/experiments/two_stage_attribution.py" \
    --fixtures "$REPO/app/fixtures/attribution_gold_pdnc_thesignofthefour_w3200.json" \
    --narrator "thesignofthefour=DR. WATSON" \
    --keep-prompts --tag narrator_w3200 \
    --out "$runtime/experiments/two_stage_attribution_narrator_w3200.json"
stage_commit_artifacts narrator_x_context "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary narrator_x_context_20260821

echo
echo "HOW TO READ IT. Compare against the SAME BOOK's rows in"
echo "two_stage_attribution_w3200.json - same fixture, same window, no prior."
echo "  If accuracy rises by roughly the 17.8 points the prior earned alone,"
echo "  the levers are independent and both should ship."
echo "  If it rises far less, they attack the same errors and the document"
echo "  must stop treating them as additive."
echo "  If Holmes -> Watson (76 of 857) does not shrink, the prior is not"
echo "  doing what the confusion analysis predicted and the mechanism is wrong."
