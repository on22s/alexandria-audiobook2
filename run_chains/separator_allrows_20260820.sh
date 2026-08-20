#!/usr/bin/bash
# Complete the separator comparison at a scale that can carry a decision.
#
# WHERE THE GAP IS. Two of the four separator forms have all-rows data at 1,582
# terms: the shipped hyphen (rescues 15.2% of the words the plain reading
# fails, breaks 69.7% of the ones it already says) and none (10.2% / 72.8%).
# `space` and `dot` have only ever been measured on the --only-e-row subset at
# 119 terms - the narrowest slice in the project, and the one whose findings
# have twice failed to generalise.
#
# WHAT IT DECIDES. The e-row data says the pause damage is graded - none is
# indistinguishable from un-respelled audio, space pauses, dot pauses hardest -
# while the all-rows data says the hyphen rescues most. Those two orderings
# disagree about which form to ship, and the disagreement rests on 119 terms
# for half the arms. Measuring space and dot at the same 1,600 terms as the
# other two makes it one comparison instead of two incompatible ones.
#
# It reuses each arm's own work directory, so clips already generated are
# skipped rather than re-synthesised.
#
# COST, measured: the none arm took 7,346s for 1,600 terms and the hyphen arm
# 8.3s/term, so each of these is about two hours. Four hours of generation,
# plus a few minutes to re-score pauses across all four arms together.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/separator_allrows_20260820"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

for sep in space dot; do
    run_stage "allrows_$sep" 4h --needs-vram -- \
        "$REPO/gpu_job.sh" "allrows_$sep" \
        "$python" -u "$REPO/app/experiments/measure_respellings.py" \
        --min-books 5 --separator "$sep" --limit 1600 \
        --work "$runtime/respelling_${sep}_allrows" \
        --out "$runtime/experiments/respelling_${sep}_allrows_n1600.json"
    stage_commit_artifacts "allrows_$sep" "$REPO"
done

# All four forms, same terms, one table. Refuses rather than writing an empty
# artifact if an arm has no clips.
run_stage pauses_four_arms 1h -- \
    "$python" -u "$REPO/app/experiments/measure_pauses.py" --limit 800 \
    --arm none=respelling_none_allrows \
    --arm space=respelling_space_allrows \
    --arm dot=respelling_dot_allrows \
    --arm hyphen_wide=respelling_hyphen_allrows \
    --out "$runtime/experiments/respelling_pauses_allrows_4arm.json"
stage_commit_artifacts pauses_four_arms "$REPO"

# Selectivity for every arm that now has all-rows data, so the rescue/breakage
# split is stated on the same footing for all four.
run_stage selectivity_all 30m -- \
    "$python" -u "$REPO/app/experiments/respelling_selectivity.py" \
    "$runtime/experiments/respelling_hyphen_allrows_n1600.json" \
    "$runtime/experiments/respelling_none_allrows_n1600.json" \
    "$runtime/experiments/respelling_space_allrows_n1600.json" \
    "$runtime/experiments/respelling_dot_allrows_n1600.json" \
    --out "$runtime/experiments/respelling_selectivity_4arm.json"
stage_commit_artifacts selectivity_all "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary separator_allrows_20260820
