#!/usr/bin/bash
set -uo pipefail
repo=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
run_dir="$repo/ab_test_runtime/experiments/targeted_missing_repair_7h_20260814"
log_dir="$run_dir/logs"; mkdir -p "$log_dir"
exec > >(tee -a "$log_dir/campaign.log") 2>&1
server_pid=
cleanup() {
  if [ -n "${server_pid:-}" ] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null || true
    for unused in 1 2 3 4 5; do kill -0 "$server_pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$server_pid" 2>/dev/null && kill -9 "$server_pid" 2>/dev/null || true
  fi
  echo "CAMPAIGN_EXIT $(date -u +%FT%TZ)"
}
trap cleanup EXIT INT TERM
campaign_end=$(( $(date +%s) + 24600 ))
echo "CAMPAIGN_START $(date -u +%FT%TZ) end_epoch=$campaign_end"
/usr/bin/llama-server -m /home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf \
  --port 8090 --host 127.0.0.1 -c 32768 -np 1 -ngl 999 --alias qwen3-14b \
  --chat-template-kwargs '{"enable_thinking":false}' > "$log_dir/llama-server.log" 2>&1 &
server_pid=$!
ready=0
for unused in $(seq 1 60); do
  if curl -fsS -m 5 http://127.0.0.1:8090/v1/models | grep -q qwen3; then ready=1; break; fi
  sleep 10
done
[ "$ready" -eq 1 ] || { echo "ABORT model server not ready"; exit 1; }
cd "$repo/app" || exit 1
echo "MODEL_READY $(date -u +%FT%TZ) pid=$server_pid"
pair=1
while [ "$(date +%s)" -lt "$campaign_end" ]; do
  remaining=$((campaign_end - $(date +%s))); [ "$remaining" -le 60 ] && break
  limit=10800; [ "$remaining" -lt "$limit" ] && limit=$remaining
  if [ $((pair % 2)) -eq 1 ]; then order=baseline-first; else order=repair-first; fi
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  base=$(printf "%03d_grimgar06_chunk25_%s_%s" "$pair" "$order" "$stamp")
  echo "PAIR_START $base limit=$limit $(date -u +%FT%TZ)"
  timeout --signal=TERM --kill-after=30 "$limit" env/bin/python -u experiments/targeted_missing_repair.py \
    --source ../ab_test_runtime/results/collect_all_20260722-155801/inputs/grimgar06.txt \
    --chunk 25 --order "$order" --out "$run_dir/$base.json" > "$log_dir/$base.log" 2>&1
  rc=$?; echo "PAIR_END $base rc=$rc $(date -u +%FT%TZ)"
  [ "$rc" -eq 124 ] && break; [ "$rc" -eq 0 ] || exit "$rc"
  pair=$((pair + 1))
done
echo "CAMPAIGN_DONE completed_pairs=$((pair - 1)) $(date -u +%FT%TZ)"
