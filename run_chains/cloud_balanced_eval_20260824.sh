#!/usr/bin/bash
set -uo pipefail

REPO="/home/ubuntu/alexandria-audiobook2.git"
MODEL="/home/ubuntu/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf"
ADAPTER="/home/ubuntu/alexandria-training-20260823/output/gguf_20260824/adapter_author_heldout_balanced.gguf"
PORT=8090
LOG="$REPO/ab_test_runtime/logs/cloud_balanced_eval_20260824.log"

mkdir -p "$(dirname "$LOG")"
for path in "$MODEL" "$ADAPTER"; do
    [ -f "$path" ] || { echo "missing required input: $path" >&2; exit 1; }
done

llama-server -m "$MODEL" --lora "$ADAPTER" --host 127.0.0.1 --port "$PORT" \
    -ngl 99 -c 32768 -np 1 --flash-attn on >"$LOG.server" 2>&1 &
server_pid=$!
cleanup() {
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

ready=0
for _ in $(seq 1 120); do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        ready=1
        break
    fi
    kill -0 "$server_pid" 2>/dev/null || break
    sleep 2
done
[ "$ready" -eq 1 ] || { echo "llama-server did not become healthy" >&2; exit 1; }

cd "$REPO"
EXPERIMENT_ENV="$(python3 - <<'PY'
import json, platform, subprocess
gpu = subprocess.check_output([
    "nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"
], text=True).strip()
print(json.dumps({"host": platform.node(), "gpu": gpu, "backend": "CUDA"}))
PY
)" \
python3 -u app/experiments/lora_serving_eval_20260824.py \
    --books index18 mushoku16 owarimonogatari3 \
    --base_url "http://127.0.0.1:$PORT/v1" \
    --model qwen/qwen3-14b \
    --tag new-author_heldout_balanced-remaining3
