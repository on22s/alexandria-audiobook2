#!/usr/bin/bash
set -uo pipefail
repo=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
run_dir="$repo/ab_test_runtime/experiments/three_pass_grimgar_full_20260815"
mkdir -p "$run_dir"; exec > >(tee -a "$run_dir/run.log") 2>&1
server_pid=
cleanup() {
  if [ -n "${server_pid:-}" ] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null || true
    for unused in 1 2 3 4 5; do kill -0 "$server_pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$server_pid" 2>/dev/null && kill -9 "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  echo "RUN_EXIT $(date -u +%FT%TZ)"
}
trap cleanup EXIT INT TERM
/usr/bin/llama-server -m /home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf \
  --port 8090 --host 127.0.0.1 -c 32768 -np 1 -ngl 999 --alias qwen3-14b \
  --chat-template-kwargs '{"enable_thinking":false}' > "$run_dir/llama-server.log" 2>&1 &
server_pid=$!
ready=0
for unused in $(seq 1 60); do
  if curl -fsS -m 5 http://127.0.0.1:8090/v1/models | grep -q qwen3; then ready=1; break; fi
  sleep 10
done
[ "$ready" -eq 1 ] || exit 1
cd "$repo/app"
timeout --signal=TERM --kill-after=30 42300 env/bin/python -u three_pass_generate.py \
  ../ab_test_runtime/results/collect_all_20260722-155801/inputs/grimgar06.txt \
  --output "$run_dir/grimgar06.json" --pass2-on-exhaustion fail
