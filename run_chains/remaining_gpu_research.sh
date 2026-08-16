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

if [ ! -f "$runtime/experiments/pdnc_sequence__pilot__local-llamacpp.json" ]; then
    wait_if_paused
    ALEXANDRIA_PDNC_INTERVENTION=sequence \
        "$repo/run_chains/pdnc_context_evidence.sh"
fi

asr_out="$runtime/experiments/asr_silero_whisper_ja_offset30.json"
if [ ! -f "$asr_out" ]; then
    stage asr_silero_whisper_ja timeout --signal=INT --kill-after=30s 7200 \
        "$python" -u "$repo/app/experiments/asr_backends.py" \
        --build "$runtime/kokoro_same_speaker_eval/build.json" \
        --backends silero_whisper_cpp --lang ja --row-offset 30 --limit 10 \
        --align-clips 10 --whisper-cpp-bin "$repo/whisper.cpp/build/bin/whisper-cli" \
        --whisper-cpp-model "$repo/whisper.cpp/models/ggml-base.bin" \
        --out "$asr_out"
fi

duration_out="$runtime/experiments/duration_length_intervention.json"
if [ ! -f "$duration_out" ]; then
    stage duration_length_intervention timeout --signal=INT --kill-after=30s 7200 \
        "$python" -u "$repo/app/experiments/duration_length_intervention.py" \
        --out "$duration_out"
fi

adapter_out="$runtime/experiments/reference_rank1_pilot.json"
if [ ! -f "$adapter_out" ]; then
    stage reference_rank1_pilot timeout --signal=INT --kill-after=30s 21600 \
        "$python" -u "$repo/app/experiments/retrain_honest.py" \
        --adapters silky_baritone_30s_m_fantasy husky_soprano_20s_f \
        warm_baritone_30s_m_2 husky_tenor_30s_m_literary \
        warm_mezzo_20s_f_anime --use-medoid --reference-rank 1 --resume \
        --work "$runtime/reference_rank1_pilot" --out "$adapter_out"
fi

echo "REMAINING GPU RESEARCH COMPLETE $(date -u +%FT%TZ)"
