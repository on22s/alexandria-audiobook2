#!/usr/bin/bash
# Re-answer 5.3 on the dialogue map, with both arms carrying it.
#
# WHY THE OLD ANSWER IS NOT ENOUGH. 5.3 compared the arms on attribution
# accuracy alone, paired by a key that deletes every quote, dash and
# apostrophe. It could not see that three-pass removes the outermost quotes
# from every fully-quoted line by design, nor that single-pass follows the same
# instruction unevenly. Both facts were invisible to the metric.
#
# WHY THE ANSWER IS NOW AVAILABLE. Since 1f6be7a `dialogue_spans` maps the
# spoken text from the SOURCE, before any model runs, and generation marks each
# entry with `spoken` and `source_span`. That is a carried fact: no
# normalisation deletes it, and it does not depend on either arm's quote
# habits. Three-pass was wired to the same map alongside this chain, so this
# compares two DESIGNS rather than a patched arm against an unpatched one.
#
# WHY BOTH ARMS MUST BE RE-RUN. Every script on disk predates 1f6be7a and
# carries no `spoken` key. dialogue_map_compare refuses such a pair rather than
# reporting 0% located as an arm failure - correct, and also why there is
# nothing to compare until this runs.
#
# ONE HARNESS, NOT A SECOND COPY OF THE INVOCATION. three_pass_vs_single.py
# already knows how to drive both arms - the positional input, --output, and
# --pass2-on-exhaustion, which only the three-pass arm takes and whose absence
# aborted owarimonogatari3 at 38 minutes last time. Re-deriving those flags
# here would be a second definition of "how to run an arm" ([[Rule 15]]), so
# the chain calls the harness and then adds the fidelity axis on ITS output.
#
# COST, measured on these books in the 5.3 run: single ~76 min and three-pass
# ~40 min on mushoku16; owarimonogatari3's three-pass arm 63 min with
# fallback set. Two books, both arms, so roughly four hours.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/dialogue_map_5_3_20260826"
work="$runtime/dialogue_map_5_3"
mkdir -p "$STAGE_LOG_DIR" "$work"
source "$REPO/run_chains/lib/stage.sh"

# Generation and accuracy in one stage, because they are one run. --reuse-complete
# so an interrupted chain does not regenerate a book it already finished.
run_stage generate_both_arms 6h -- \
    env REQUIRE_LLM=1 "$REPO/gpu_job.sh" dialogue_map_5_3 \
    "$python" -u "$REPO/app/experiments/three_pass_vs_single.py" \
    --books mushoku16 owarimonogatari3 \
    --work "$work" --reuse-complete \
    --pass2-on-exhaustion fallback \
    --out "$runtime/experiments/three_pass_vs_single_mapped.json"
stage_commit_artifacts generate_both_arms "$REPO"

# The new axis, on the SAME run, so accuracy and fidelity cannot be attributed
# to two different generations.
run_stage dialogue_map_compare 30m -- \
    "$python" -u "$REPO/app/experiments/dialogue_map_compare.py" \
    --work "$work" \
    --out "$runtime/experiments/dialogue_map_compare.json"
stage_commit_artifacts dialogue_map_compare "$REPO"

# Text fidelity for the same pair, against each book's own source, so the
# quote-retention figures quoted at 5.3 are from this run rather than from
# artifacts generated in August before the map existed.
run_stage text_fidelity 30m -- \
    "$python" -u "$REPO/app/experiments/script_text_fidelity.py" \
    --work "$work" \
    --source mushoku16="$runtime/results/collect_all_20260722-155801/inputs/mushoku16.txt" \
    --source owarimonogatari3="$runtime/results/collect_all_20260722-155801/inputs/owarimonogatari3.txt" \
    --out "$runtime/experiments/script_text_fidelity_mapped.json"
stage_commit_artifacts text_fidelity "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary dialogue_map_5_3_20260826

echo
echo "HOW TO READ IT."
echo "  located    does the arm's output still know which lines are speech?"
echo "             Single-pass reported 94-96% on three books when the map"
echo "             first shipped. Three-pass has never been measured."
echo "  agreement  on lines BOTH arms located, do they agree about speech?"
echo "             A low rate with a significant McNemar means one arm is"
echo "             systematically wrong, not that they differ by chance."
echo "  accuracy   the old 5.3 number, from the same run, so the two axes"
echo "             cannot be attributed to different generations."
echo
echo "WHAT WOULD CHANGE THE VERDICT. 5.3 says delete three-pass on a 5.2-17.5"
echo "point accuracy deficit. If three-pass locates its lines as well as"
echo "single-pass does, the deficit is the whole case and it still loses. If"
echo "it locates markedly fewer, the case is stronger than recorded. If it"
echo "locates MORE, that is the first evidence in its favour and the goal"
echo "should say so."
