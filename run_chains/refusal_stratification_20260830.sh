#!/bin/bash
# Is the refusal a roster defect, or is it learned?
#
# THE QUESTION THIS EXISTS TO ANSWER. On 2026-08-30 the adapters were found to
# refuse - to return an empty prediction - and to refuse the rows they would
# have got wrong: base accuracy is 11-62% on the rows the tuned arm declined
# against 65-77% on the rows it answered. Two explanations survive:
#
#   roster defect     the model declines because the gold genuinely is not in
#                     the roster it was shown, and declining is correct
#   learned refusal   the adapter declines regardless of whether the answer
#                     was available
#
# They imply different fixes. `in_candidates` separates them, and it was None
# on all 766 rows of every serving evaluation because neither evaluator passed
# `candidates` to record.add. That is fixed; this run produces the first
# artifact that can be stratified.
#
# ONE BOOK, BOTH ARMS. index18 is the smallest of the three at 99 gold entries,
# so this is ~10 minutes at the 16.5 s/batch measured today - it is a question,
# not a campaign. If the answer is "learned", a wider run is worth queueing;
# if it is "roster defect", the finding needs restating rather than extending.
#
# Runs LAST, after goal 1.3 and the fidelity seed, because it is the cheapest
# job of the three and the other two are already measured commitments.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
TAG="${TAG:-refusal-stratification-20260830}"
ADAPTER="$runtime/distill/gguf/new_20260824/adapter_author_heldout_balanced.gguf"
PORT="${LLAMA_PORT:-8090}"
out="$runtime/experiments/lora_serving_eval__${TAG}.json"

if [ -s "$out" ]; then
    echo "[$(date -u +%FT%TZ)] SKIP $TAG (already complete)"; exit 0
fi
[ -s "$ADAPTER" ] || { echo "REFUSING: missing adapter $ADAPTER" >&2; exit 1; }

LLAMA_PORT="$PORT" "$REPO/ensure_llama_server.sh" "$ADAPTER" \
    > "$runtime/logs/${TAG}.server.log" 2>&1 \
    || { echo "REFUSING: llama-server failed to start" >&2; exit 1; }

env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 "$REPO/gpu_job.sh" "$TAG" \
    "$python" -u "$REPO/app/experiments/lora_serving_eval.py" \
    --books index18 \
    --input-dir "$runtime/dialogue_map_5_3_inputs" \
    --checkpoint-dir "$runtime/dialogue_map_5_3" \
    --base_url "http://127.0.0.1:$PORT/v1" \
    --model qwen/qwen3-14b --tag "$TAG" \
    > "$runtime/logs/${TAG}.out" 2>&1
rc=$?

# The point of the run: refusal rate split by whether the answer was there.
"$python" - "$out" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"no artifact to stratify ({type(exc).__name__})"); raise SystemExit(0)
rows = d["rows"]
if not any(r.get("candidates") for r in rows):
    print("REFUSING TO REPORT: candidates still empty, so this run cannot "
          "separate a roster defect from a learned refusal.")
    raise SystemExit(0)
print(f"\n{'arm':8} {'gold in roster':>22} {'gold absent':>22}")
for arm in sorted({r["arm"] for r in rows}):
    cells = []
    for present in (True, False):
        sub = [r for r in rows if r["arm"] == arm
               and bool(r.get("in_candidates")) is present]
        if not sub:
            cells.append("n/a"); continue
        e = sum(1 for r in sub if not (r.get("predicted") or "").strip())
        cells.append(f"{100*e/len(sub):5.1f}% empty of {len(sub):3}")
    print(f"{arm:8} {cells[0]:>22} {cells[1]:>22}")
print("\nSimilar rates on both sides = learned refusal.")
print("Refusals concentrated where the gold is absent = roster defect.")
PYEOF
echo "[$(date -u +%FT%TZ)] COMPLETE $TAG (job rc=$rc)"
