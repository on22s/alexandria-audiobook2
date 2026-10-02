#!/bin/bash
# Overnight: answer goals 5.3 and 3.1 in one run, on the shipped model.
#
# WHY ONE RUN ANSWERS BOTH. 5.3 has NO BASELINE because the only three-pass
# comparison on disk recorded timing but never accuracy, and its single arm
# failed outright on grimgar03 and index18. 3.1 is OPEN because no saved book
# was ever generated with qwen3-14b, so its failure figures come from a model
# that does not ship. Generating all four books through both arms produces the
# accuracy comparison 5.3 wants AND a chunk-completion record attributable to
# qwen3-14b, which is exactly what 3.1 is missing.
#
# THE MODEL SETUP, AND WHY IT IS NOT THE DEFAULT ONE.
#   - Both generation arms explicitly request qwen3-14b for this run.
#     The user's config.json and any existing backup remain untouched.
#   - llama-server runs with --chat-template-kwargs '{"enable_thinking":false}'.
#     Qwen3 is a thinking model: asked for 16 tokens it spent all of them on
#     reasoning and returned empty content, which the pipeline would see as a
#     failed chunk. Disabling thinking server-side means no app code changes
#     and no token budget spent on reasoning. Verified before launching:
#     content 'ready', reasoning absent, finish_reason stop.
#
# WHAT WOULD MAKE THIS RUN WORTHLESS. If the server dies, every chunk fails and
# the morning shows a 0% completion rate that says nothing about qwen3-14b. So
# the endpoint is re-checked before the long stage starts, and the run refuses
# rather than producing a number that would be read as a model result.
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
L="$REPO/ab_test_runtime/logs"
PY="$REPO/app/env/bin/python"
# NO GPU_LOCK EXPORT. This line used to name $HOME/.alexandria_gpu.lock, a
# third lock file that serialised against neither the repo lock the other
# chains use nor gpu_job.sh's own - and it sat BELOW the self-re-exec above,
# so this chain's outer wrapper and its inner jobs took different locks.
# gpu_job.sh now defaults to the repo lock; letting it decide is the point.
export GPU_QLOG="$L/gpu_jobq.log"
mkdir -p "$L"
cd "$REPO/app"

echo "=== endpoint check $(date -u +%FT%TZ) ==="
if ! curl -fsS -m 20 http://127.0.0.1:8090/v1/models | "$PY" -c '
import json, sys
try:
    response = json.load(sys.stdin)
    models = response.get("data") if isinstance(response, dict) else None
    valid = (isinstance(models, list) and
             all(isinstance(model, dict) and isinstance(model.get("id"), str)
                 for model in models) and
             any(model["id"] == "qwen3-14b" for model in models))
except (ValueError, OSError):
    valid = False
sys.exit(0 if valid else 1)
'; then
    echo "ABORT: endpoint does not advertise exact model qwen3-14b on8090"
    exit 1
fi
echo "  qwen3-14b responding"

# 4 books x 2 arms. The previous partial run took 103 and 177 minutes for the
# single arm on two books, so this is a genuinely long job; the timeout is
# per-book inside the harness, not for the whole chain.
echo ""
echo "=== three_pass_vs_single  $(date -u +%FT%TZ) ==="
"$REPO/gpu_job.sh" three_pass_qwen3 timeout 72000 "$PY" -u experiments/three_pass_vs_single.py \
    --books grimgar03 index18 mushoku16 owarimonogatari3 --model qwen3-14b \
    --out "$REPO/ab_test_runtime/experiments/three_pass_vs_single_qwen3.json" \
    > "$L/three_pass_qwen3.log" 2>&1
echo "  rc=$?"
tail -12 "$L/three_pass_qwen3.log" | sed 's/^/  /' | cut -c1-115

# Re-read chunk completion including whatever the run above just wrote, so the
# morning has goal 3.1's number attributable to a named model.
echo ""
echo "=== chunk completion re-read  $(date -u +%FT%TZ) ==="
"$REPO/gpu_job.sh" chunk_recount "$PY" -u experiments/chunk_completion.py \
    --scripts "$REPO/ab_test_runtime/three_pass_vs_single" \
    --out "$REPO/ab_test_runtime/experiments/chunk_completion_qwen3.json" \
    > "$L/chunk_recount.log" 2>&1
echo "  rc=$?"
tail -14 "$L/chunk_recount.log" | sed 's/^/  /' | cut -c1-115

echo ""
echo "OVERNIGHT DONE $(date -u +%FT%TZ)"
