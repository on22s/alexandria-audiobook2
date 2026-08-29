#!/usr/bin/bash
set -uo pipefail

REPO="/home/ubuntu/alexandria-goals-be3e7ea"
MODEL="/home/ubuntu/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf"
PORT=8090
LOG="$REPO/ab_test_runtime/logs/cloud_pdnc_resume_20260824"
mkdir -p "$LOG" "$REPO/ab_test_runtime/experiments"

llama-server -m "$MODEL" --host 127.0.0.1 --port "$PORT" -ngl 99 \
    -c 32768 -np 1 --flash-attn on >"$LOG/server.log" 2>&1 &
server_pid=$!
cleanup() { kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

ready=0
for _ in $(seq 1 120); do
    curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && { ready=1; break; }
    kill -0 "$server_pid" 2>/dev/null || break
    sleep 2
done
[ "$ready" -eq 1 ] || { echo "llama-server did not become healthy" >&2; exit 1; }

cd "$REPO"
python3 -u app/experiments/three_pass_vs_single.py \
    --books pdnc_thegambler pdnc_thesignofthefour \
    --inputs ab_test_runtime/dialogue_map_5_3_inputs \
    --work ab_test_runtime/dialogue_map_5_3 --reuse-complete \
    --pass2-on-exhaustion fallback \
    --out ab_test_runtime/experiments/three_pass_vs_single_pdnc_resumed.json
