#!/usr/bin/bash
# The three books the 2026-09-14 chain did not run: its grimgar03 stage committed
# its artifact and left the regenerated indexes staged, so the tree was dirty
# and the other stages were REFUSED. Same stages, same flags.
# Does the product's own card run Muse-Glimmer with reasoning on?
#
# tnr-0 (A6000) scored Muse-Glimmer-30B-UD-Q3_K_XL base, reasoning low, JSON
# schema, minor-speaker rule at 81.5% on the four clean-gold books (2026-09-14,
# lora_serving_eval__muse-glimmer-30b-...-reasoninglow-baseonly-hint-20260914).
# The product runs on an RX 9070 XT (16 GB). Same file (sha 820d18e0...), the
# system HIP llama-server (b10903), same flags, 16k context with q8 KV so the
# 13.4 GB of weights fit: the smoke offloaded every layer at 28 tok/s. This
# scores grimgar03 first (the A6000 took 68 min for its 94 windows), then the
# other three, so a per-window rate exists after the first book.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/muse_local_reasoninglow_20260914"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"
MODEL="$runtime/models/muse-q3/Muse-Glimmer-30B-UD-Q3_K_XL.gguf"
BIN="${LLAMA_BIN:-/usr/bin/llama-server}"
PORT=8097
[ -s "$MODEL" ] || { stage_note "REFUSING: no Muse GGUF at $MODEL"; exit 1; }
[ -x "$BIN" ] || { stage_note "REFUSING: no llama-server at $BIN"; exit 1; }
source "$REPO/run_chains/lib/managed_server.sh" || exit 1
LLAMA_BIN="$BIN" LLAMA_PORT="$PORT" GPU_RECLAIM_VRAM=1 ensure_managed_server_lease muse_local_rest_20260915 "$0" "$@" || exit 1
start_managed_server "$PORT" "$STAGE_LOG_DIR/server.log" 180 2 "$BIN" -m "$MODEL" --alias muse-glimmer-30b --host 127.0.0.1 --port $PORT -ngl 99 -c 16384 --parallel 1 \
    --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 \
    --reasoning on --reasoning-format deepseek --chat-template-kwargs '{"reasoning_strength":"low"}' || exit 1

export EXPERIMENT_ENV="{\"available\":true,\"loaded\":true,\"gpu\":\"AMD Radeon RX 9070 XT\",\"model_format\":\"UD-Q3_K_XL base, no adapter\",\"runtime\":\"llama.cpp-hip b10903 (Manjaro package)\",\"context_length\":16384,\"parallel\":1,\"kv_cache\":\"q8_0/q8_0\",\"instrument\":\"in-repo product harness, batch 25, max_tokens 4096, request-level JSON schema, reasoning low\"}"
for book in index18 mushoku16 owarimonogatari3; do
    TAG="muse-glimmer-30b-base-local-9070xt-product-batch25-q3-jsonschema-reasoninglow-${book}-20260914"
    run_stage "muse_local_$book" 6h -- \
        env REQUIRE_VRAM_GB=0 \
        "$python" -u "$REPO/app/experiments/lora_serving_eval.py" \
        --model muse-glimmer-30b --base_url "http://127.0.0.1:$PORT/v1" --books "$book" \
        --batch-size 25 --base-only --max-tokens 4096 --reasoning-effort low --structured-output auto \
        --input-dir "$runtime/clean_gold_20260827/inputs" --checkpoint-dir "$runtime/clean_gold_20260827/checkpoints" \
        --tag "$TAG"
    stage_commit_artifacts "muse_local_$book" "$REPO" "$runtime/experiments/lora_serving_eval__${TAG}.json" "$runtime/experiments/lora_serving_eval__${TAG}.json.ckpt" "$runtime/experiments/lora_serving_eval__${TAG}.json.ckpt.stale"
done
stage_summary muse_local_reasoninglow_20260914
