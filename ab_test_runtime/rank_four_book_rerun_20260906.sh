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
# THE CAUSE, diagnosed on 2026-09-05 in g3smoke.sh and not by me: grimgar03's
# INPUT TEXT was never shipped to the cloud boxes. The gold fixture - 396 rows,
# the largest of the four - was on every box all along, so 25 chain scripts
# hardcode `--books index18 mushoku16 owarimonogatari3` because that was the
# largest set the input directory could actually serve. distill_eval's own
# default is all four. The workaround outlived the cause: the text has since
# been shipped, and the newer chains do pass four books, but 25 older ones
# still carry the three-book list.
#
# TWO WRONG EXPLANATIONS WERE TRIED FIRST, both by me and both fitting the
# evidence I had gathered rather than the evidence I had not. That the
# evaluator silently skipped an unloadable book: it does not, load_book raises.
# That tnr-3 lacked grimgar03's CHECKPOINT: it did lack it, but the checkpoint
# is not what the old chains were missing, and copying it fixed nothing. The
# real answer was sitting in a chain header on the box.
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
# THE INPUT TEXT is the file whose absence caused this, so it is named first
# and by name. A box without grimgar03.txt cannot serve the book, which is why
# 25 chains hardcoded a three-book list around it. The checkpoint is checked
# too because the evaluator RAISES on a missing one - a crash rather than a
# quiet three-book run, but still worth catching before the model loads.
for b in grimgar03 index18 mushoku16 owarimonogatari3; do
    test -s "$INPUT/$b.txt" || {
        echo "input text missing: $b.txt - THIS is what made 25 chains" >&2
        echo "hardcode a three-book list. Ship the text before running." >&2
        exit 1
    }
    test -s "$CKPT/${b}__three_pass.json.threepass_checkpoint.json" || {
        echo "checkpoint missing: $b (the evaluator would raise on this)" >&2
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
