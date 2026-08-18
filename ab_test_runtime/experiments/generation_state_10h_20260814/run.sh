#!/usr/bin/bash
set -uo pipefail
repo=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
run_dir="$repo/ab_test_runtime/experiments/generation_state_10h_20260814"
log_dir="$run_dir/logs"; mkdir -p "$log_dir"
exec > >(tee -a "$log_dir/campaign.log") 2>&1
server_pid=
stop_server() {
  if [ -n "${server_pid:-}" ] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null || true
    for unused in 1 2 3 4 5; do kill -0 "$server_pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$server_pid" 2>/dev/null && kill -9 "$server_pid" 2>/dev/null || true
  fi
  server_pid=
}
cleanup() { stop_server; echo "CAMPAIGN_EXIT $(date -u +%FT%TZ)"; }
trap cleanup EXIT INT TERM
start_server() {
  cache_flag=$1
  /usr/bin/llama-server -m /home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf \
    --port 8090 --host 127.0.0.1 -c 32768 -np 1 -ngl 999 --alias qwen3-14b \
    --chat-template-kwargs '{"enable_thinking":false}' "$cache_flag" \
    >> "$log_dir/llama-server.log" 2>&1 &
  server_pid=$!
  for unused in $(seq 1 60); do
    curl -fsS -m 5 http://127.0.0.1:8090/v1/models | grep -q qwen3 && return 0
    sleep 10
  done
  return 1
}
campaign_end=$(( $(date +%s) + 35400 ))
echo "CAMPAIGN_START $(date -u +%FT%TZ) end_epoch=$campaign_end"
cd "$repo/app" || exit 1
trial=1; cycle=1
run_probe() {
  condition=$1; seed=$2
  remaining=$((campaign_end - $(date +%s))); [ "$remaining" -le 60 ] && return 2
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  base=$(printf "%04d_%s_seed%s_%s" "$trial" "$condition" "$seed" "$stamp")
  echo "TRIAL_START $base $(date -u +%FT%TZ)"
  timeout --signal=TERM --kill-after=20 1200 env/bin/python -u experiments/generation_state_probe.py \
    --source ../ab_test_runtime/results/collect_all_20260722-155801/inputs/grimgar06.txt \
    --chunk 25 --condition "$condition" --seed "$seed" --out "$run_dir/$base.json" \
    > "$log_dir/$base.log" 2>&1
  rc=$?; echo "TRIAL_END $base rc=$rc $(date -u +%FT%TZ)"
  [ "$rc" -eq 0 ] || return "$rc"
  trial=$((trial + 1)); return 0
}
while [ "$(date +%s)" -lt "$campaign_end" ]; do
  for cache in on off; do
    stop_server
    if [ "$cache" = on ]; then flag=--cache-prompt; else flag=--no-cache-prompt; fi
    start_server "$flag" || exit 1
    for index in $(seq 1 10); do
      run_probe "warm_cache_${cache}_fixed" 7 || break 3
      run_probe "warm_cache_${cache}_vary" $((cycle * 1000 + index)) || break 3
    done
  done
  for index in $(seq 1 10); do
    for seed_kind in fixed vary; do
      stop_server; start_server --cache-prompt || exit 1
      if [ "$seed_kind" = fixed ]; then seed=7; else seed=$((cycle * 1000 + 100 + index)); fi
      run_probe "cold_${seed_kind}" "$seed" || break 3
    done
  done
  cycle=$((cycle + 1))
done
echo "CAMPAIGN_DONE completed_trials=$((trial - 1)) cycles=$((cycle - 1)) $(date -u +%FT%TZ)"
