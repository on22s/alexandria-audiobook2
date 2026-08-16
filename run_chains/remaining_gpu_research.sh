#!/usr/bin/bash
# Restartable GPU research queue. Interrupt the process group to game; rerun
# this script afterward. Completed artifacts are skipped and no pause waits
# while holding the GPU lock.
set -uo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$repo/ab_test_runtime"
python="$repo/app/env/bin/python"
pause_file="$runtime/PAUSE_GPU_QUEUE"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
mkdir -p "$runtime/logs" "$runtime/experiments"

wait_if_paused() {
    while [ -e "$pause_file" ]; do
        echo "PAUSED (no GPU lock held): remove $pause_file to continue"
        sleep 10
    done
}

stage() {
    local name="$1"; shift
    wait_if_paused
    "$repo/gpu_job.sh" "$name" "$@"
}

# READ THE REPORT'S OWN VERDICT, NOT ANY VERDICT IN IT.
#
# verify_release.py writes "status" twice: once per gate, and once for the
# whole report. A run where compile_python passed and unit_tests failed
# produces a report whose top-level status is "failed" but which still
# contains a gate object reading "status": "passed".
#
# A substring grep matched that gate, so the queue skipped the final release
# verification on every rerun after a failure and went on to print
# REMAINING GPU RESEARCH COMPLETE - the one gate whose entire job is to
# refuse to say that.
release_passed() {
    [ -f "$1" ] || return 1
    "$python" - "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        report = json.load(handle)
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if report.get("status") == "passed" else 1)
PY
}

if [ ! -f "$runtime/experiments/pdnc_sequence__pilot__local-llamacpp.json" ]; then
    wait_if_paused
    ALEXANDRIA_PDNC_INTERVENTION=sequence \
        "$repo/run_chains/pdnc_context_evidence.sh"
fi

asr_out="$runtime/experiments/asr_silero_whisper_ja_offset20.json"
if [ ! -f "$asr_out" ]; then
    if ! stage asr_silero_whisper_ja timeout --signal=INT --kill-after=30s 7200 \
        "$python" -u "$repo/app/experiments/asr_backends.py" \
        --build "$runtime/kokoro_same_speaker_eval/build.json" \
        --backends silero_whisper_cpp --lang ja --row-offset 20 --limit 10 \
        --align-clips 10 --whisper-cpp-bin "$repo/whisper.cpp/build/bin/whisper-cli" \
        --whisper-cpp-model "$repo/whisper.cpp/models/ggml-base.bin" \
        --out "$asr_out"; then
        echo "JAPANESE ASR FAILED; later research stages not started"
        exit 1
    fi
fi

duration_out="$runtime/experiments/duration_length_intervention.json"
if [ ! -f "$duration_out" ]; then
    if ! stage duration_length_intervention timeout --signal=INT --kill-after=30s 7200 \
        "$python" -u "$repo/app/experiments/duration_length_intervention.py" \
        --out "$duration_out"; then
        echo "DURATION INTERVENTION FAILED; later research stages not started"
        exit 1
    fi
fi

adapter_out="$runtime/experiments/reference_rank1_pilot.json"
if [ ! -f "$adapter_out" ]; then
    if ! stage reference_rank1_pilot timeout --signal=INT --kill-after=30s 21600 \
        "$python" -u "$repo/app/experiments/retrain_honest.py" \
        --adapters silky_baritone_30s_m_fantasy husky_soprano_20s_f \
        warm_baritone_30s_m_2 husky_tenor_30s_m_literary \
        warm_mezzo_20s_f_anime --use-medoid --reference-rank 1 --resume \
        --work "$runtime/reference_rank1_all21" --out "$adapter_out"; then
        echo "REFERENCE-RANK PILOT FAILED; full adapter run not started"
        exit 1
    fi
fi

# Finish Goal 2.7 after the five-adapter reference-rank pilot. The full result
# starts from the pilot artifact, so those expensive retrains are not repeated.
# This list is the 21 adapters whose shipped training_meta.json still records
# 200 samples (train + held-out val) as of 2026-08-16.
contaminated_adapters=(
    breathy_alto_50s_f_fantasy
    husky_baritone_20s_m_supernatural
    husky_baritone_40s_m_2
    husky_baritone_40s_m_scifi
    husky_soprano_20s_f
    husky_tenor_30s_m_literary
    silky_alto_40s_f_literary_1
    silky_baritone_30s_m_fantasy
    silky_baritone_45s_m
    silky_mezzo_30s_f_supernatural
    velvety_mezzo_30s_f_gothic
    warm_alto_50s_f_gothic
    warm_baritone_30s_m_2
    warm_baritone_30s_m_scifi
    warm_baritone_50s_m_gothic
    warm_bass_50s_m_fantasy
    warm_mezzo_20s_f_anime
    warm_mezzo_30s_f
    warm_tenor_20s_m
    warm_tenor_20s_m_scifi
    warm_tenor_30s_m_gothic
)

all_adapters_out="$runtime/experiments/reference_rank1_all21.json"
if [ ! -f "$all_adapters_out" ]; then
    cp "$adapter_out" "$all_adapters_out"
fi
if ! stage reference_rank1_all21 timeout --signal=INT --kill-after=30s 64800 \
    "$python" -u "$repo/app/experiments/retrain_honest.py" \
    --adapters "${contaminated_adapters[@]}" --use-medoid \
    --reference-rank 1 --resume --work "$runtime/reference_rank1_all21" \
    --out "$all_adapters_out"; then
    echo "FULL ADAPTER RETRAIN FAILED; rerun the queue to resume it"
    exit 1
fi

gate_failures=0
for adapter in "${contaminated_adapters[@]}"; do
    gate_out="$runtime/experiments/gate_reference_rank1__${adapter}.json"
    if [ -f "$gate_out" ]; then
        if ! grep -q '"passed": true' "$gate_out"; then
            gate_failures=$((gate_failures + 1))
        fi
        continue
    fi
    base="$runtime/reference_rank1_all21/$adapter"
    if [ ! -f "$base/adapter/adapter_model.safetensors" ]; then
        echo "GATE BLOCKED $adapter: retrained adapter is missing"
        gate_failures=$((gate_failures + 1))
        continue
    fi
    if ! stage "gate_reference_rank1__$adapter" \
        timeout --signal=INT --kill-after=30s 3600 \
        "$python" -u "$repo/app/experiments/verify_adapter_identity.py" \
        --adapter "$base/adapter" --dataset "$base/data" --lines 10 \
        --out "$gate_out"; then
        gate_failures=$((gate_failures + 1))
    fi
done

release_out="$runtime/experiments/final_release_after_remaining_gpu_research.json"
if ! release_passed "$release_out"; then
    if ! stage final_release_verification \
        timeout --signal=INT --kill-after=30s 14400 \
        "$python" -u "$repo/app/verify_release.py" --full \
        --json-report "$release_out"; then
        echo "FINAL RELEASE VERIFICATION FAILED"
        exit 1
    fi
fi

if [ "$gate_failures" -ne 0 ]; then
    echo "REMAINING GPU RESEARCH FINISHED WITH $gate_failures ADAPTER GATE FAILURE(S)"
    exit 1
fi

echo "REMAINING GPU RESEARCH COMPLETE $(date -u +%FT%TZ)"
