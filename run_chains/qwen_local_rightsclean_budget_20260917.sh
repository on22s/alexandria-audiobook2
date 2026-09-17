#!/usr/bin/bash
# The working Qwen recipe on the product's own card: Qwen3-14B Q4_K_M +
# rights-clean adapter (f16) + shipped prompt + JSON schema + reasoning with
# the server's --reasoning-budget 1024 (deepseek format). Paired base/LoRA,
# four books, one stage per book. Cloud (A6000, same recipe): base ~73,
# adapter ~81 on three books (tnr-2, 2026-09-17). This is what would ship
# as the local default if it holds here.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/qwen_local_rightsclean_budget_20260917"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"
MODEL="${ALEXANDRIA_QWEN3_MODEL:-$HOME/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}"
ADAPTER="$runtime/adapters/rightsclean.f16.gguf"
BIN="${LLAMA_BIN:-/usr/bin/llama-server}"
PORT=8098
[ -s "$MODEL" ] || { stage_note "REFUSING: no Qwen GGUF at $MODEL"; exit 1; }
[ -s "$ADAPTER" ] || { stage_note "REFUSING: no adapter at $ADAPTER"; exit 1; }
[ "$(sha256sum "$ADAPTER" | cut -c1-16)" = "c0dae304c51802e8" ] || { stage_note "REFUSING: adapter differs from the cloud one"; exit 1; }
pkill -x llama-server 2>/dev/null; sleep 3
"$BIN" -m "$MODEL" --alias qwen/qwen3-14b --host 127.0.0.1 --port $PORT -ngl 99 -c 32768 --parallel 1 --flash-attn on \
    --reasoning on --reasoning-format deepseek --reasoning-budget 1024 --lora "$ADAPTER" > "$STAGE_LOG_DIR/server.log" 2>&1 &
server_pid=$!
trap 'kill -TERM "$server_pid" 2>/dev/null' EXIT
for _ in $(seq 1 180); do curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; kill -0 "$server_pid" 2>/dev/null || { tail -20 "$STAGE_LOG_DIR/server.log"; exit 1; }; sleep 2; done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null || { stage_note "REFUSING: server not healthy"; exit 1; }
sha=$(sha256sum "$ADAPTER" | awk '{print $1}')
export EXPERIMENT_ENV="{\"available\":true,\"loaded\":true,\"gpu\":\"AMD Radeon RX 9070 XT\",\"model_format\":\"Q4_K_M base + f16 LoRA\",\"runtime\":\"llama.cpp-hip (Manjaro package)\",\"context_length\":32768,\"parallel\":1,\"adapter_sha256\":\"$sha\",\"adapter\":\"rights-clean stack (20 PDNC + RiQuA + prose plays), 2026-09-14\",\"instrument\":\"in-repo product harness, batch 25, max_tokens 4096, request-level JSON schema, reasoning low with server --reasoning-budget 1024\"}"
for book in grimgar03 index18 mushoku16 owarimonogatari3; do
    TAG="qwen3-14b-rightsclean-local-9070xt-product-batch25-budget1024-schema-${book}-20260917"
    run_stage "qwen_local_rc_$book" 4h -- \
        env REQUIRE_VRAM_GB=0 \
        "$REPO/gpu_job.sh" "qwen_local_rc_$book" \
        "$python" -u "$REPO/app/experiments/lora_serving_eval.py" \
        --model qwen/qwen3-14b --base_url "http://127.0.0.1:$PORT/v1" --books "$book" \
        --batch-size 25 --max-tokens 4096 --reasoning-effort low --structured-output auto \
        --input-dir "$runtime/clean_gold_20260827/inputs" --checkpoint-dir "$runtime/clean_gold_20260827/checkpoints" \
        --tag "$TAG"
    stage_commit_artifacts "qwen_local_rc_$book" "$REPO"
done
stage_summary qwen_local_rightsclean_budget_20260917
