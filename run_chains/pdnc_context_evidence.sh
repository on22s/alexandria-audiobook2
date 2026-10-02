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

# One canonical lease and lifecycle spans startup, requests and cleanup.
REPO="$repo"
export PDNC_STARTUP_VRAM_GB="${PDNC_STARTUP_VRAM_GB:-${REQUIRE_VRAM_GB:-4}}"
export LLAMA_PORT=8090
export GPU_QLOG="${GPU_QLOG:-$runtime_root/logs/gpu_jobq.log}"
source "$repo/run_chains/lib/llm_campaign.sh" || exit 4
ensure_llm_campaign_lease "pdnc_${intervention}" "$repo/run_chains/pdnc_context_evidence.sh" "$@" || exit $?

env REQUIRE_VRAM_GB="$PDNC_STARTUP_VRAM_GB" bash "$repo/gpu_job.sh" \
    --check-vram "pdnc_${intervention}.startup" || exit $?

mkdir -p "$run_dir"
exec > >(tee -a "$run_dir/run.log") 2>&1
LLAMA_MODEL="$model" LLAMA_PORT=8090 LLAMA_CTX=32768 LLAMA_THINKING=0 \
    LLAMA_ALIAS=qwen3-14b LLAMA_LOG="$run_dir/llama-server.log" \
    "$repo/ensure_llama_server.sh" > "$run_dir/server-start.log" 2>&1 || {
        echo "ABORT: canonical model server not ready; see $run_dir/server-start.log"
        exit 2
    }
LLM_CAMPAIGN_SERVER_READY=1
echo "MODEL_READY $(date -u +%FT%TZ)"

get_pilot_state() {
    "$repo/app/env/bin/python" - "$pilot" \
        "$runtime_root/pdnc_inputs/pdnc_${intervention}__pilot.json" \
        "$intervention" "$repo/app" <<'PY'
import sys
sys.path.insert(0, sys.argv[4])
from experiments.pdnc_context_evidence import get_context_pilot_state
print(get_context_pilot_state(sys.argv[1], sys.argv[2], sys.argv[3]))
PY
}

cd "$repo/app" || exit 2
pilot_state=$(get_pilot_state)
pilot_rc=$?
[ "$pilot_rc" -eq 0 ] || exit "$pilot_rc"
if [ "$pilot_state" = missing ]; then
    echo "PILOT_START $(date -u +%FT%TZ)"
    timeout --signal=TERM --kill-after=30 10800 \
        env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 bash "$repo/run_chains/lib/llm_job.sh" "pdnc_${intervention}_pilot" \
        env/bin/python -u \
        experiments/pdnc_context_evidence.py --phase pilot \
        --intervention "$intervention" \
        > "$run_dir/pilot.log" 2>&1
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "PILOT_FAILED rc=$rc; resume will reuse its validated checkpoint"
        exit "$rc"
    fi
    pilot_state=$(get_pilot_state)
pilot_rc=$?
[ "$pilot_rc" -eq 0 ] || exit "$pilot_rc"
fi
if [ "$pilot_state" = missing ]; then
    echo "PILOT_INCOMPLETE: worker did not publish complete, input-bound evidence"
    exit 1
fi
if [ "$pilot_state" != pass ]; then
    echo "PILOT_GATE_FAIL $(date -u +%FT%TZ); confirmatory set remains sealed"
    exit 0
fi
echo "PILOT_GATE_PASS $(date -u +%FT%TZ)"

if [ -f "$confirmatory" ] && "$repo/app/env/bin/python" \
    experiments/pdnc_context_evidence.py --phase confirmatory --intervention "$intervention" \
    --check-artifact "$confirmatory"; then
    echo "CONFIRMATORY_ALREADY_COMPLETE $(date -u +%FT%TZ)"
    exit 0
fi
echo "CONFIRMATORY_START $(date -u +%FT%TZ)"
timeout --signal=TERM --kill-after=30 21600 \
    env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 bash "$repo/run_chains/lib/llm_job.sh" "pdnc_${intervention}_confirmatory" \
    env/bin/python -u \
    experiments/pdnc_context_evidence.py --phase confirmatory \
    --intervention "$intervention" \
    --pilot-artifact "$pilot" > "$run_dir/confirmatory.log" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "CONFIRMATORY_FAILED rc=$rc; resume will reuse its validated checkpoint"
    exit "$rc"
fi
"$repo/app/env/bin/python" experiments/pdnc_context_evidence.py \
    --phase confirmatory --intervention "$intervention" --check-artifact "$confirmatory" || exit 1
echo "CAMPAIGN_DONE $(date -u +%FT%TZ)"
