#!/usr/bin/env bash
# Re-run the rank comparison on ALL FOUR light novels.
#
# WHY. Every rank-seed-control artifact is 383 rows over three books. grimgar03
# is 396 of the 793 gold rows - more than the other three combined - and is
# absent from 54 of 58 multi-book evaluations. The rank comparison that flipped
# sign across seeds was measured without it:
#
#     seed 20260904   r32 beat r16 by +2.87   p=0.117
#     seed 20260905   r32 LOST to r16 by -1.04
#
# THE CAUSE WAS A MISSING FILE, NOT A FLAG. tnr-3 was the only box in the fleet
# without grimgar03's three-pass checkpoint - it had the input text and not the
# checkpoint the evaluator needs - so every run there silently covered three
# books. The file is 1.6 MB and has been copied. Nothing about the command was
# ever wrong, which is why nobody found it by reading the chains.
#
# WHAT THIS COULD CHANGE. grimgar03's base arm reads 89.1% against 68-75% for
# the other three, so it is the easiest book as well as the biggest. Adding it
# raises both arms and may or may not preserve the ordering; that is the point.
# These results are NOT comparable to the 383-row artifacts and must not be
# pooled with them.
#
# SCOPE. r16 and r32 at the shared seed only. That is the pair whose comparison
# reversed, at about 4.5 hours an arm-pair on this box; all seven adapters would
# be 30 hours to answer a question two of them settle.
#
# Resumable by artifact. Never write "rc=$?" after a pipeline.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
MODEL="${RANK_MODEL:-/home/ubuntu/models/Qwen3-14B}"
ROOT="${RANK_ROOT:-/home/ubuntu/output_rank_seed_control_20260904}"
INPUT="${RANK_INPUT:-/home/ubuntu/clean_gold_20260827/inputs}"
CKPT="${RANK_CKPT:-/home/ubuntu/clean_gold_20260827/checkpoints}"
SEED="${RANK_SEED:-20260904}"
HOST_TAG="${RANK_HOST_TAG:-h100-tnr3-20260906}"
PY="${RANK_PY:-python3}"
ARMS="${RANK_ARMS:-r16 r32}"

test -s "$MODEL/config.json" || { echo "base model missing: $MODEL" >&2; exit 1; }
# THE CHECKPOINT, not just the input. Its absence is what made every previous
# run three-book, and it failed silently: the evaluator skips a book it cannot
# load rather than refusing, so a missing 1.6 MB file looked like a smaller
# experiment.
for b in grimgar03 index18 mushoku16 owarimonogatari3; do
    test -s "$INPUT/$b.txt" || { echo "input missing: $b" >&2; exit 1; }
    test -s "$CKPT/${b}__three_pass.json.threepass_checkpoint.json" || {
        echo "checkpoint missing: $b - this is what silently made previous" >&2
        echo "runs three-book. Copy it before running." >&2
        exit 1
    }
done
grep -q "merge_validation_into_diagnostics" app/experiments/distill_eval.py 2>/dev/null || {
    echo "this checkout predates #503: a rejected batch would not record which" >&2
    echo "rule fired. Update before running." >&2
    exit 1
}
echo "repo commit: $(git -C "$R" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "model: $MODEL"

for arm in $ARMS; do
    A="$ROOT/${arm}_seed${SEED}"
    tag="rank4book-${arm}-seed${SEED}-${HOST_TAG}"
    art="$R/ab_test_runtime/experiments/distill_eval__${tag}.json"
    [ -s "$art" ] && { echo "SKIP $arm"; continue; }
    [ -f "$A/adapter_model.safetensors" ] || { echo "MISSING adapter $A"; continue; }
    "$PY" -u app/experiments/distill_eval.py \
        --adapter "$A" --model "$MODEL" \
        --books grimgar03 index18 mushoku16 owarimonogatari3 \
        --input-dir "$INPUT" --checkpoint-dir "$CKPT" \
        --max_tokens 2000 --tag "$tag" \
        > "/home/ubuntu/${tag}.eval.log" 2>&1
    rc=$?
    echo "[$(date -Is)] EVAL $arm rc=$rc"
    [ "$rc" -eq 0 ] && [ -s "$art" ] || echo "  WARNING: $arm produced no artifact"
done
echo "ALL DONE $(date -Is)"
