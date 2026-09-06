#!/usr/bin/env bash
# Does reasoning effort decide the blank rate, and therefore the Qwen3.8 verdict?
#
# WHY THIS MATTERS MORE THAN THE RANK QUESTION. Blank rows are scored as wrong,
# and reasoning effort moves them enormously. Measured 2026-09-06 on 30 rows,
# same model, same rows, only --thinking-mode differing:
#
#     low     0.0% blank   90.0% accuracy
#     off     6.7%         90.0%
#     medium 23.3%         73.3%
#     xhigh  86.7%         13.3%
#
# The xhigh arm's "13.3% accuracy" is not an attribution result: 26 of its 30
# rows carry no answer at all. And LOW BEATS OFF at identical accuracy, which
# is the opposite of the usual assumption that disabling thinking is safest.
#
# Every Qwen3.8 conclusion in this project - including "3.8 adapters are a
# null", measured over 24 arms at mean -1.26 - was taken at `off` or worse. If
# the setting decides the blank rate, that verdict is about the setting.
#
# The tuned arms blank 4-8x more than base at EVERY level (full/off: 3.4% base
# against 26.4% tuned), so this also tests whether "the adapter is worse" is
# largely "the adapter trips the validator more".
#
# --load-in-4bit IS NOT OPTIONAL. A 27B in bf16 needs about 54 GB and the A6000
# has 48, so without it the run OOMs AFTER a 52 GB transfer. This was spotted
# once, patched in a worktree, and never committed - #504 merged without it -
# which is why the flag now sits next to a comment saying so.
#
# NF4, NOT FP8, BECAUSE THIS RUNS ON AN A6000. The existing full-size thinking
# runs used Qwen3.8-27B-FP8 on an A100. Ampere has no FP8 tensor cores, and a
# missing `kernels-community/finegrained-fp8` is exactly what produced the two
# artifacts holding 766 rows with every prediction None. A sweep is internally
# comparable by construction - one adapter, one machine, four settings - so it
# does not need to match the FP8 numbers, and must not risk that failure.
#
# WHAT #503 ADDS. The artifact now records WHICH validator rule rejected each
# batch, so this answers why effort inflates rejections rather than only that
# it does. Without it the sweep would produce four blank rates and no cause.
#
# Resumable by artifact. Never write "rc=$?" after a command substitution.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
MODEL="${SWEEP_MODEL:-/home/ubuntu/models/Qwen3.8-27B}"
ADAPTER="${SWEEP_ADAPTER:-}"
INPUT="${SWEEP_INPUT:-/home/ubuntu/clean_gold_20260827/inputs}"
CKPT="${SWEEP_CKPT:-/home/ubuntu/clean_gold_20260827/checkpoints}"
HOST_TAG="${SWEEP_HOST_TAG:-a6000-tnr4-20260906}"
PY="${SWEEP_PY:-python3}"
# ROWS PER BOOK. A full four-book pass costs about 6.5 hours per mode on an
# A6000 - measured from tnr-2, where grimgar03's base arm alone took 5,772s for
# 385 lines - so four modes would be 26 hours. This sweep asks for the BLANK
# RATE and which rule causes it, and 100 rows a book answers that at a quarter
# of the cost. Set SWEEP_LIMIT=0 for the full pass.
LIMIT="${SWEEP_LIMIT:-100}"

test -s "$MODEL/config.json" || { echo "base model missing: $MODEL" >&2; exit 1; }
[ -n "$ADAPTER" ] || { echo "set SWEEP_ADAPTER to an NF4-compatible adapter" >&2; exit 1; }
test -s "$ADAPTER/adapter_model.safetensors" || { echo "adapter missing: $ADAPTER" >&2; exit 1; }
for b in grimgar03 index18 mushoku16 owarimonogatari3; do
    test -s "$INPUT/$b.txt" || { echo "input missing: $b" >&2; exit 1; }
done
# THE CLEAN index18, not the one with 6,662 replacement characters and zero
# quote marks. A sweep measured on that file would compare four settings on a
# book with its strongest dialogue cue deleted.
# `grep -c` PRINTS 0 AND EXITS 1 when it matches nothing, so `|| echo 0`
# appended a SECOND zero and the test saw "0\n0" - not an integer. The guard
# then refused a perfectly clean file. It failed safe, which is the right
# direction to fail in, but it was still wrong, and it had never been run
# against either a clean or a dirty file before it shipped.
bad=$(grep -c $'\xef\xbf\xbd' "$INPUT/index18.txt" 2>/dev/null) || true
bad=${bad:-0}
[ "$bad" -eq 0 ] || {
    echo "index18 holds $bad replacement characters. The corrupt copy has" >&2
    echo "6,662 of them and zero quote marks; a sweep measured on it would" >&2
    echo "compare four settings on a book with its dialogue cue deleted." >&2
    exit 1
}
# THE BOX RESTORES FROM A SNAPSHOT, so its checkout is whatever it was when the
# snapshot was taken. Without #503 this script runs perfectly and records NO
# findings - four blank rates and no cause, which is the whole reason for the
# sweep. Refuse rather than produce a weaker artifact that looks complete.
grep -q "merge_validation_into_diagnostics" app/experiments/distill_eval.py 2>/dev/null || {
    echo "this checkout predates #503: a rejected batch would not record WHICH" >&2
    echo "validator rule fired, and the sweep would answer 'how much' but never" >&2
    echo "'why'. Run: git -C \"$R\" pull --ff-only   then retry." >&2
    exit 1
}
# llama.cpp is not used by this path (transformers+peft), but a stale checkout
# usually means a stale everything; say so once rather than debugging it later.
echo "repo commit: $(git -C "$R" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "model:   $MODEL"
echo "adapter: $ADAPTER"

for mode in off low medium xhigh; do
    tag="qwen38-effort-${mode}-${HOST_TAG}"
    art="$R/ab_test_runtime/experiments/distill_eval__${tag}.json"
    if [ -s "$art" ]; then echo "SKIP $mode"; continue; fi
    "$PY" -u app/experiments/distill_eval.py \
        --adapter "$ADAPTER" --model "$MODEL" \
        --books grimgar03 index18 mushoku16 owarimonogatari3 \
        --input-dir "$INPUT" --checkpoint-dir "$CKPT" \
        --thinking-mode "$mode" --max_tokens 2000 --tag "$tag" \
        --load-in-4bit --limit "$LIMIT" \
        > "/home/ubuntu/${tag}.eval.log" 2>&1
    rc=$?; echo "[$(date -Is)] EVAL $mode rc=$rc"
done
echo "ALL DONE $(date -Is)"
