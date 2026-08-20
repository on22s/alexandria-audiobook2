#!/usr/bin/bash
# Does a longer, more typical reference clip move goals 2.1, 2.5 and 2.6?
#
# THE CASE FOR RUNNING IT. instrument_null_test.py refuted the comfortable
# explanation: the bands are achievable, the rulers are sound, and cloning's
# misses - Chinese vtl 1.0607x, English f0 median 0.81x - are real. What
# survived is an INPUT. reference_audit.py found every eval set clones from a
# reference far short of the 10-15s band Qwen's guide publishes (3.45s, 5.17s,
# 6.15s), and off-centre from its own speaker in the same direction as the
# arm's failure in both languages that fail.
#
# reference_rebuild.py fixes both defects at once, and the new references sit
# almost exactly on their speakers:
#
#     en  6.15s -> 10.52s   f0 212.6 vs centre 212.6   vtl 14.68 vs 14.53
#     ja  5.17s -> 10.84s   f0 122.8 vs centre 122.3   vtl 14.65 vs 14.75
#     zh  3.45s -> 11.80s   f0 179.8 vs centre 180.3   vtl 13.23 vs 13.17
#
# WHAT IT CANNOT TELL US. Length and typicality changed together, so a
# difference here does not attribute to either alone. That is deliberate - the
# first question is whether the input matters at all, and separating the two
# costs another two arms for a distinction nobody needs unless this moves.
#
# CLONE ONLY. The LoRA arm never reads the reference clip; running it would
# spend the card reproducing a number we already have.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/longref_arm_20260826"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

running() { pgrep -f "run_chains/$1" 2>/dev/null | grep -qv -e "^$$\$" -e "^$PPID\$"; }
for chain in second_english_eval_20260820.sh unseen_books_20260819b.sh \
             attribution_context_20260820.sh; do
    stage_note "waiting for $chain"
    while running "$chain"; do sleep 120; done
done

declare -A EVAL=( [en]=ljspeech_eval [ja]=kokoro_eval [zh]=aishell3_eval )

for lang in en ja zh; do
    dir="${EVAL[$lang]}"
    build="$runtime/$dir/build_longref.json"
    if [ ! -f "$build" ]; then
        stage_note "SKIP $lang: no $build - run reference_rebuild.py first"
        continue
    fi
    run_stage "longref_gen_$lang" 3h --needs-vram -- \
        "$REPO/gpu_job.sh" "longref_$lang" \
        "$python" -u "$REPO/app/experiments/ljspeech_generate.py" \
        --build "$build" --arms clone --limit 0 \
        --out-dir "$runtime/$dir/longref" \
        --out "$runtime/experiments/longref__${lang}_generate.json"
    stage_commit_artifacts "longref_gen_$lang" "$REPO"
done

# Score with the SAME probe the goals use, so the new numbers are comparable to
# the committed ones rather than to a second implementation ([[Rule 15]]).
run_stage longref_quality 1h -- \
    "$python" -u "$REPO/app/experiments/pitch_quality_probe.py" \
    --lines 150 \
    --manifest en=longref__en_generate.json \
    --manifest ja=longref__ja_generate.json \
    --manifest zh=longref__zh_generate.json \
    --out "$runtime/experiments/pitch_quality_longref.json"
stage_commit_artifacts longref_quality "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary longref_arm_20260826

echo
echo "HOW TO READ IT. The cells that matter are zh clone vtl_cm (was 1.0607x,"
echo "band 0.95-1.05) and en clone f0_median (was 0.81x). The human-vs-human"
echo "null puts those measures' own spread at 0.9588-1.0337 and 0.9410-1.0658,"
echo "so a move has to clear that to be a move. If neither budges, the"
echo "reference is not the mechanism and the 2.6 product decision is the"
echo "answer after all."
