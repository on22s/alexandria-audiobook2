#!/usr/bin/bash
set -uo pipefail

NAME="${1:-}"
case "$NAME" in
    speaker_hardcases_split_nonmajor|speaker_longcontext_tophalf_5epoch) ;;
    *) echo "unknown adapter: $NAME" >&2; exit 2 ;;
esac

REPO="/home/ubuntu/alexandria-audiobook2.git"
MODEL="/home/ubuntu/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf"
ADAPTER="/home/ubuntu/alexandria-training-20260823/output/gguf_20260824/adapter_${NAME}.gguf"
PORT=8090
LOG="$REPO/ab_test_runtime/logs/cloud_${NAME}_remaining3_20260824.log"

mkdir -p "$(dirname "$LOG")"
for path in "$MODEL" "$ADAPTER"; do
    [ -f "$path" ] || { echo "missing required input: $path" >&2; exit 1; }
done

source "$REPO/run_chains/lib/managed_server.sh" || exit 1
ensure_managed_server_lease cloud_adapter_eval_remaining3_20260824 "$0" "$@" || exit 1
start_managed_server "$PORT" "$LOG.server" 120 2 llama-server -m "$MODEL" --lora "$ADAPTER" --host 127.0.0.1 --port "$PORT" \
    -ngl 99 -c 32768 -np 1 --flash-attn on || exit 1

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
    --tag "new-${NAME}-remaining3"
