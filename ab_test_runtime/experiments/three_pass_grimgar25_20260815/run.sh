#!/usr/bin/bash
set -uo pipefail
repo=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
run_dir="$repo/ab_test_runtime/experiments/three_pass_grimgar25_20260815"
mkdir -p "$run_dir"; exec > >(tee -a "$run_dir/run.log") 2>&1
server_pid=
cleanup() { if [ -n "${server_pid:-}" ]; then kill "$server_pid" 2>/dev/null || true; fi; echo "RUN_EXIT $(date -u +%FT%TZ)"; }
trap cleanup EXIT INT TERM
/usr/bin/llama-server -m /home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf \
  --port 8090 --host 127.0.0.1 -c 32768 -np 1 -ngl 999 --alias qwen3-14b \
  --chat-template-kwargs '{"enable_thinking":false}' > "$run_dir/llama-server.log" 2>&1 &
server_pid=$!
for unused in $(seq 1 60); do curl -fsS -m 5 http://127.0.0.1:8090/v1/models | grep -q qwen3 && break; sleep 10; done
cd "$repo/app"
timeout --signal=TERM --kill-after=30 7200 env/bin/python -u experiments/three_pass_chunk_probe.py \
  --source ../ab_test_runtime/results/collect_all_20260722-155801/inputs/grimgar06.txt \
  --chunk 25 --out "$run_dir/result.json"
