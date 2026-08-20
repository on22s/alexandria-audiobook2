#!/usr/bin/bash
# 5.3 on fresh runs, on private AND public books, both arms.
#
# WHY FRESH. The first answer came from retrofitting the source-derived map
# onto scripts generated on 2026-08-09, and it split 2-1: single-pass better on
# index18 and owarimonogatari3, three-pass much better on mushoku16. Those
# scripts predate a fortnight of changes to both generators - near-miss repair,
# narrator hints, source speaker labels, the dialogue map itself - so a verdict
# taken on them describes a pipeline that no longer exists.
#
# WHY PUBLIC BOOKS. Every method result in this project rests on four Japanese
# light novels from one person's library. PDNC is 28 public-domain novels with
# quotation annotations published by other researchers, so a result there is on
# record and checkable by someone else - and it carries GOLD SPEAKER LABELS,
# which lets both axes be measured on the same run: did the arm attribute the
# line to anyone, and was that anyone correct.
#
# WHICH PUBLIC BOOKS, AND WHY THESE. Chosen by PDNC's own quote types rather
# than by feel. Explicit quotations name the speaker beside the line and are
# the easy case; Anaphoric and Implicit do not. These four are the hard end of
# the corpus:
#
#   TheGambler                    12% Explicit, 50% Anaphoric, 39% Implicit
#   TheSignOfTheFour              13% Explicit, 51% Implicit
#   TheMysteriousAffairAtStyles   13% Explicit, 68% Implicit, 1861 quotes
#   AHandfulOfDust                18% Explicit, 74% Implicit, 104 characters
#
# AHandfulOfDust is the extreme on both axes at once - three quarters of its
# dialogue names nobody, across a cast of 104. AlicesAdventuresInWonderland, at
# 82% Explicit, is deliberately NOT here; it would flatter both arms.
#
# COST, scaled from mushoku16's measured 75.5 min single / 39.7 min three-pass
# at 0.29 MB: the four PDNC books total 1.27 MB, so roughly 5.5h single and 3h
# three-pass, plus the three light novels re-run. Call it 12-14 hours. The user
# has said the card is available for this; the alternative is deciding 5.3 on a
# pipeline that no longer exists.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/dialogue_map_5_3_20260826"
work="$runtime/dialogue_map_5_3"
mkdir -p "$STAGE_LOG_DIR" "$work"
source "$REPO/run_chains/lib/stage.sh"

missing=""
for f in app/experiments/dialogue_map_compare.py \
         app/experiments/script_text_fidelity.py \
         app/experiments/retrofit_dialogue_map.py; do
    [ -e "$REPO/$f" ] || missing="$missing\n    $f (missing)"
done
grep -q 'dialogue_spans' "$REPO/app/three_pass_generate.py" 2>/dev/null \
    || missing="$missing\n    app/three_pass_generate.py (not wired to dialogue_spans)"
if [ -n "$missing" ]; then
    echo "REFUSING to start: the live checkout does not have what this chain needs."
    printf "%b\n" "$missing"
    echo "  These are on PR #361. Merge it and 'git pull --ff-only', then re-launch."
    echo "  Nothing was generated and no GPU time was spent."
    exit 1
fi

# PDNC texts are staged beside the light novels so one --inputs directory
# serves both, and so the harness needs no new notion of where a book lives.
inputs="$runtime/dialogue_map_5_3_inputs"
mkdir -p "$inputs"
for b in TheGambler TheSignOfTheFour TheMysteriousAffairAtStyles AHandfulOfDust; do
    src="$runtime/pdnc/data/$b/novel_text.txt"
    [ -f "$src" ] && cp -f "$src" "$inputs/$b.txt"
done
for b in index18 mushoku16 owarimonogatari3; do
    for d in "$runtime/results/collect_all_20260722-155801/inputs" "$runtime/tpvs_inputs"; do
        [ -f "$d/$b.txt" ] && { cp -f "$d/$b.txt" "$inputs/$b.txt"; break; }
    done
done
echo "staged $(ls "$inputs"/*.txt 2>/dev/null | wc -l) books"

# The hard public books first: if the arms separate there, the light novels are
# confirmation rather than the whole basis, and a chain that dies overnight has
# still produced the result that is not already in the document.
run_stage generate_pdnc 10h -- \
    env REQUIRE_LLM=1 "$REPO/gpu_job.sh" map_5_3_pdnc \
    "$python" -u "$REPO/app/experiments/three_pass_vs_single.py" \
    --books TheGambler TheSignOfTheFour TheMysteriousAffairAtStyles AHandfulOfDust \
    --inputs "$inputs" --work "$work" --reuse-complete \
    --pass2-on-exhaustion fallback \
    --out "$runtime/experiments/three_pass_vs_single_pdnc.json"
stage_commit_artifacts generate_pdnc "$REPO"

run_stage generate_light_novels 6h -- \
    env REQUIRE_LLM=1 "$REPO/gpu_job.sh" map_5_3_ln \
    "$python" -u "$REPO/app/experiments/three_pass_vs_single.py" \
    --books index18 mushoku16 owarimonogatari3 \
    --inputs "$inputs" --work "$work" --reuse-complete \
    --pass2-on-exhaustion fallback \
    --out "$runtime/experiments/three_pass_vs_single_mapped.json"
stage_commit_artifacts generate_light_novels "$REPO"

# Both axes, one run. dialogue_map_compare asks "did the arm name anyone";
# three_pass_vs_single above already scored "was the name right" against gold.
run_stage dialogue_map_compare 30m -- \
    "$python" -u "$REPO/app/experiments/dialogue_map_compare.py" \
    --work "$work" --out "$runtime/experiments/dialogue_map_compare_fresh.json"
stage_commit_artifacts dialogue_map_compare "$REPO"

run_stage text_fidelity 30m -- \
    "$python" -u "$REPO/app/experiments/script_text_fidelity.py" \
    --work "$work" \
    --source mushoku16="$inputs/mushoku16.txt" \
    --source owarimonogatari3="$inputs/owarimonogatari3.txt" \
    --source index18="$inputs/index18.txt" \
    --source TheGambler="$inputs/TheGambler.txt" \
    --source AHandfulOfDust="$inputs/AHandfulOfDust.txt" \
    --out "$runtime/experiments/script_text_fidelity_fresh.json"
stage_commit_artifacts text_fidelity "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary dialogue_map_5_3_20260826

echo
echo "HOW TO READ IT. Two axes, and they disagreed on the retrofitted run:"
echo "  named anyone    dialogue_map_compare, counting NARRATOR and UNKNOWN"
echo "                  alike as unattributed. Retrofit gave single 84.5/58.9/86.5"
echo "                  against three-pass 75.7/84.5/77.1 - split 2-1."
echo "  named correctly three_pass_vs_single against gold. On PDNC that gold is"
echo "                  a third party's annotation rather than ours."
echo
echo "If the split persists on four hard public books, 5.3's target stops being"
echo "'wire it in or delete it' and becomes a routing question. If one arm wins"
echo "on both axes across seven books, that is the answer and the loser goes."
