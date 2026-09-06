#!/usr/bin/env bash
# Measure goal 1.1's per-book target on the local model, in ONE artifact.
#
# THE TARGET SAYS "every book >= 75% on the local model" and records "two of
# four already clear it". No local artifact covers all four books. The 22 local
# light-novel artifacts are one, two or three books each, and the three-book
# ones are {grimgar03, mushoku16, owarimonogatari3} - they omit index18,
# because locally index18 was the corrupt copy. On the cloud the omitted book
# was grimgar03, because its input text was never shipped.
#
# So "two of four" is assembled from runs that never shared a corpus, a model
# build or a date. That is not a claim any single measurement supports, and
# 2026-09-06's coverage audit found the same shape in the cloud artifacts.
#
# THIS IS A BASELINE, NOT AN INTERVENTION. --base-only, the untuned local
# server, all four books, one run. It is the number the target should be read
# against, and nothing here tries to improve it.
#
# THE INPUTS ARE THE CLEAN ONES, copied from tnr-2 rather than reconstructed:
# index18 there carries 0 replacement characters against the corrupt copy's
# 6,662, and the chain refuses if that is not true of the file it finds.
#
# Resumable by artifact. rc after a pipeline is ${PIPESTATUS[0]}, per #505.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
BASE_URL="${LN_BASE_URL:-http://127.0.0.1:8090/v1}"
MODEL="${LN_MODEL:-qwen3-14b}"
GOLD="$R/ab_test_runtime/local_gold"
[ -d "$GOLD/inputs" ] || GOLD="$MAIN/ab_test_runtime/local_gold"
TAG="local-4book-base-$(date +%Y%m%d)"
ART="$R/ab_test_runtime/experiments/lora_serving_eval__${TAG}.json"
[ -s "$ART" ] && { echo "SKIP - artifact exists"; exit 0; }

curl -s --max-time 10 "${BASE_URL%/v1}/props" >/dev/null 2>&1 || {
    echo "no llama.cpp at $BASE_URL - start a server for $MODEL first" >&2
    exit 1
}
for b in grimgar03 index18 mushoku16 owarimonogatari3; do
    test -s "$GOLD/inputs/$b.txt" || { echo "input missing: $b" >&2; exit 1; }
    test -s "$GOLD/checkpoints/${b}__three_pass.json.threepass_checkpoint.json" \
        || { echo "checkpoint missing: $b" >&2; exit 1; }
done
bad=$(grep -c $'\xef\xbf\xbd' "$GOLD/inputs/index18.txt" 2>/dev/null) || true
[ "${bad:-0}" -eq 0 ] || {
    echo "index18 holds $bad replacement characters - that is the corrupt copy," >&2
    echo "which has 6,662 of them and zero quote marks. Use the clean text." >&2
    exit 1
}
echo "gold: $GOLD"
"$PY" -u app/experiments/lora_serving_eval.py \
    --books grimgar03 index18 mushoku16 owarimonogatari3 \
    --model "$MODEL" --base_url "$BASE_URL" --base-only \
    --input-dir "$GOLD/inputs" --checkpoint-dir "$GOLD/checkpoints" \
    --tag "$TAG" 2>&1 | tail -40
rc=${PIPESTATUS[0]}
echo "[$(date -Is)] RUN rc=$rc"
[ "$rc" -eq 0 ] || exit "$rc"
echo "ALL DONE $(date -Is)"
