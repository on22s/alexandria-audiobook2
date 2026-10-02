#!/usr/bin/bash
set -uo pipefail

REPO="/home/ubuntu/alexandria-goals-be3e7ea"
MODEL="/home/ubuntu/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf"
PORT=8090
LOG="$REPO/ab_test_runtime/logs/cloud_pdnc_resume_20260824"
mkdir -p "$LOG" "$REPO/ab_test_runtime/experiments"

source "$REPO/run_chains/lib/managed_server.sh" || exit 1
ensure_managed_server_lease cloud_pdnc_resume_20260824 "$0" "$@" || exit 1
start_managed_server "$PORT" "$LOG/server.log" 120 2 llama-server -m "$MODEL" --host 127.0.0.1 --port "$PORT" -ngl 99 \
    -c 32768 -np 1 --flash-attn on || exit 1

cd "$REPO"
python3 -u app/experiments/three_pass_vs_single.py \
    --books pdnc_thegambler pdnc_thesignofthefour \
    --inputs ab_test_runtime/dialogue_map_5_3_inputs \
    --work ab_test_runtime/dialogue_map_5_3 --reuse-complete \
    --pass2-on-exhaustion fallback \
    --out ab_test_runtime/experiments/three_pass_vs_single_pdnc_resumed.json
