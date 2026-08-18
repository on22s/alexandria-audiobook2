#!/usr/bin/bash
set -uo pipefail

repo=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
run_dir="$repo/ab_test_runtime/experiments/unseen_three_pass_20260815"
input_dir="$repo/ab_test_runtime/results/collect_all_20260722-155801/inputs"
model=/home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf
mkdir -p "$run_dir/logs"
exec > >(tee -a "$run_dir/run.log") 2>&1

server_pid=
cleanup() {
    if [ -n "${server_pid:-}" ] && kill -0 "$server_pid" 2>/dev/null; then
        kill "$server_pid" 2>/dev/null || true
        for unused in 1 2 3 4 5; do
            kill -0 "$server_pid" 2>/dev/null || break
            sleep 1
        done
        kill -0 "$server_pid" 2>/dev/null && kill -9 "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    echo "CAMPAIGN_EXIT $(date -u +%FT%TZ)"
}
trap cleanup EXIT INT TERM

echo "CAMPAIGN_START $(date -u +%FT%TZ) commit=$(git -C "$repo" rev-parse --short HEAD)"
/usr/bin/llama-server -m "$model" --port 8090 --host 127.0.0.1 \
    -c 32768 -np 1 -ngl 999 --alias qwen3-14b \
    --chat-template-kwargs '{"enable_thinking":false}' \
    > "$run_dir/logs/llama-server.log" 2>&1 &
server_pid=$!

ready=0
for unused in $(seq 1 60); do
    if curl -fsS -m 5 http://127.0.0.1:8090/v1/models | grep -q qwen3; then
        ready=1
        break
    fi
    kill -0 "$server_pid" 2>/dev/null || break
    sleep 10
done
if [ "$ready" -ne 1 ]; then
    echo "ABORT model server not ready"
    tail -n 20 "$run_dir/logs/llama-server.log"
    exit 1
fi
echo "MODEL_READY $(date -u +%FT%TZ) pid=$server_pid"

overall_rc=0
cd "$repo/app" || exit 1
for book in mushoku18 grimgar06 mushoku23 arc4_volume10wn; do
    output="$run_dir/$book.json"
    log="$run_dir/logs/$book.log"
    echo "BOOK_START $book $(date -u +%FT%TZ)"
    timeout --signal=TERM --kill-after=30 14400 env/bin/python -u three_pass_generate.py \
        "$input_dir/$book.txt" --output "$output" \
        --pass2-on-exhaustion fallback > "$log" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || overall_rc=1
    env/bin/python - "$book" "$rc" "$output.threepass_manifest.json" <<'PY'
import json
import os
import sys

book, rc, path = sys.argv[1:]
if os.path.exists(path):
    manifest = json.load(open(path, encoding="utf-8"))
    progress = manifest.get("progress") or {}
    telemetry = manifest.get("telemetry") or {}
    print(
        f"BOOK_END {book} rc={rc} status={manifest.get('status')} "
        f"stage={progress.get('stage')} completed={progress.get('completed')} "
        f"total={progress.get('total')} model={telemetry.get('model_name')}"
    )
else:
    print(f"BOOK_END {book} rc={rc} manifest=MISSING")
PY
done

echo "CAMPAIGN_DONE rc=$overall_rc $(date -u +%FT%TZ)"
exit "$overall_rc"
