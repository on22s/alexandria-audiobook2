#!/usr/bin/bash
# Pilot one attribution intervention on five diagnostic PDNC books, then open
# the sealed twenty-book confirmatory set only if the fixed pilot gate passes.
set -uo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
runtime_root="${ALEXANDRIA_RUNTIME_ROOT:-$repo/ab_test_runtime}"
intervention="${ALEXANDRIA_PDNC_INTERVENTION:-evidence}"
case "$intervention" in evidence|sequence|targeted_sequence) ;; *) echo "invalid intervention: $intervention" >&2; exit 2;; esac
run_dir="$runtime_root/experiments/pdnc_${intervention}_20260816"
experiment_dir="$runtime_root/experiments"
pilot="$experiment_dir/pdnc_${intervention}__pilot__local-llamacpp.json"
confirmatory="$experiment_dir/pdnc_${intervention}__confirmatory__local-llamacpp.json"
model="${ALEXANDRIA_QWEN3_MODEL:-/home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}"

# Make the lock impossible to forget when this script is launched directly.
if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
    export GPU_QLOG="$runtime_root/logs/gpu_jobq.log"
    exec "$repo/gpu_job.sh" "pdnc_${intervention}" \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
fi

mkdir -p "$run_dir"
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

if pgrep -x llama-server >/dev/null 2>&1; then
    echo "ABORT: an existing llama-server is not owned by this campaign"
    exit 2
fi
echo "CAMPAIGN_START $(date -u +%FT%TZ) commit=$(git -C "$repo" rev-parse --short HEAD)"
/usr/bin/llama-server -m "$model" --port 8090 --host 127.0.0.1 \
    -c 32768 -np 1 -ngl 999 --alias qwen3-14b \
    --chat-template-kwargs '{"enable_thinking":false}' \
    > "$run_dir/llama-server.log" 2>&1 &
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
    echo "ABORT: model server not ready"
    tail -n 20 "$run_dir/llama-server.log"
    exit 2
fi
echo "MODEL_READY $(date -u +%FT%TZ) pid=$server_pid"

get_pilot_state() {
    "$repo/app/env/bin/python" - "$pilot" <<'PY'
import json
import os
import sys
path = sys.argv[1]
if not os.path.exists(path):
    print("missing")
else:
    with open(path, encoding="utf-8") as handle:
        artifact = json.load(handle)
    print("pass" if artifact.get("meta", {}).get("decision", {}).get("advance")
          is True else "fail")
PY
}

cd "$repo/app" || exit 2
pilot_state=$(get_pilot_state)
if [ "$pilot_state" = missing ]; then
    echo "PILOT_START $(date -u +%FT%TZ)"
    timeout --signal=TERM --kill-after=30 10800 env/bin/python -u \
        experiments/pdnc_context_evidence.py --phase pilot \
        --intervention "$intervention" \
        > "$run_dir/pilot.log" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "PILOT_FAILED rc=$rc; resume will reuse its validated checkpoint"
        exit "$rc"
    fi
    pilot_state=$(get_pilot_state)
fi
if [ "$pilot_state" != pass ]; then
    echo "PILOT_GATE_FAIL $(date -u +%FT%TZ); confirmatory set remains sealed"
    exit 0
fi
echo "PILOT_GATE_PASS $(date -u +%FT%TZ)"

if [ -f "$confirmatory" ]; then
    echo "CONFIRMATORY_ALREADY_COMPLETE $(date -u +%FT%TZ)"
    exit 0
fi
echo "CONFIRMATORY_START $(date -u +%FT%TZ)"
timeout --signal=TERM --kill-after=30 21600 env/bin/python -u \
    experiments/pdnc_context_evidence.py --phase confirmatory \
    --intervention "$intervention" \
    --pilot-artifact "$pilot" > "$run_dir/confirmatory.log" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "CONFIRMATORY_FAILED rc=$rc; resume will reuse its validated checkpoint"
    exit "$rc"
fi
echo "CAMPAIGN_DONE $(date -u +%FT%TZ)"
