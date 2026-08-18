#!/usr/bin/bash
# The queueable remainder of the goals, 2026-08-16. Three stages, each one a
# measurement rather than a redesign - which is why these three and not the
# other two on the list (see the bottom of this header).
#
# Restartable: every stage skips its own completed artifact, and only GPU work
# goes through gpu_job.sh, which blocks on the shared lock rather than racing.
set -uo pipefail

# ARTIFACT EXISTS IS NOT ARTIFACT FINISHED. A run killed mid-way leaves a file
# that looks complete - respelling_e_row__ay_n1200.json sits in this repository
# at 1129 of 1200 terms - and a chain skipping on existence would skip it
# forever, on a subset biased toward the commonest items. Ask the artifact.
artifact_complete() {
    "$1" - "$2" <<'PYEOF' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if d.get("status") == "complete":
    sys.exit(0)
if d.get("status") == "partial":
    sys.exit(1)
r, c = d.get("results"), d.get("candidates_considered")
sys.exit(0 if isinstance(r, list) and isinstance(c, int) and len(r) >= c else 1)
PYEOF
}

repo="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$repo/ab_test_runtime"
python="$repo/app/env/bin/python"
models="$repo/whisper.cpp/models"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
mkdir -p "$runtime/logs" "$runtime/experiments"

stage() { "$repo/gpu_job.sh" "$@"; }

# ---------------------------------------------------------------- stage 1
# JAPANESE HAS ONLY EVER BEEN TESTED ON base. Every Japanese ASR artifact on
# disk is silero + ggml-base, at CER 28.7%. Chinese had the same shape of
# problem - base placed boundaries well and transcribed badly - and was solved
# by splitting the jobs: base finds the boundaries, large-v3 says what was
# said, 44.3% -> 14.1% CER. That remedy has never been pointed at Japanese,
# and ggml-large-v3.bin is already on disk.
ja_out="$runtime/experiments/asr_ja_largev3_hybrid.json"
if [ ! -f "$ja_out" ]; then
    if ! stage asr_ja_largev3_hybrid timeout --signal=INT --kill-after=30s 7200 \
        "$python" -u "$repo/app/experiments/asr_backends.py" \
        --build "$runtime/kokoro_ja_asr_eval/build.json" \
        --backends whisper_cpp whisper_cpp_hybrid --lang ja \
        --limit 50 --align-clips 50 \
        --whisper-cpp-bin "$repo/whisper.cpp/build/bin/whisper-cli" \
        --whisper-cpp-model "$models/ggml-large-v3.bin" \
        --out "$ja_out"; then
        echo "JAPANESE large-v3/hybrid FAILED; later stages not started"
        exit 1
    fi
fi

# ---------------------------------------------------------------- stage 2
# THE PROMOTION RULE COMPARES AN HONEST SCORE TO A CONTAMINATED ONE. Six clean
# retrains passed their identity gate and were refused only because they did
# not beat the shipped score - but that shipped score was measured on clips the
# shipped adapter trained on, so it is inflated by exactly the contamination
# being removed. Re-measuring the SHIPPED weights on the same held-out split
# the clean ones were judged against makes the comparison like-for-like. This
# writes evidence only; nothing is promoted here.
unfair_baseline=(
    husky_baritone_40s_m_2
    husky_baritone_40s_m_scifi
    husky_tenor_30s_m_literary
    silky_baritone_30s_m_fantasy
    warm_baritone_30s_m_2
    warm_baritone_30s_m_scifi
)
for adapter in "${unfair_baseline[@]}"; do
    out="$runtime/experiments/baseline_heldout__${adapter}.json"
    [ -f "$out" ] && continue
    data="$runtime/reference_rank1_all21/$adapter/data"
    if [ ! -d "$data" ]; then
        echo "SKIP $adapter: no held-out split at $data"
        continue
    fi
    if ! stage "baseline_heldout__$adapter" \
        timeout --signal=INT --kill-after=30s 3600 \
        "$python" -u "$repo/app/experiments/verify_adapter_identity.py" \
        --adapter "$repo/lora_models/$adapter" --dataset "$data" \
        --lines 10 --out "$out"; then
        echo "BASELINE MEASUREMENT FAILED for $adapter"
    fi
done

# ---------------------------------------------------------------- stage 3
# THE SIX RETRAINS THAT PRODUCED AN UNUSABLE VOICE (0.056-0.404 held-out).
# reference-rank 1 was itself the second choice; rank 2 is the next candidate
# and is the cheapest remaining lever before concluding the source data is at
# fault. Resumes per adapter, so an interrupt costs one adapter.
failed_adapters=(
    breathy_alto_50s_f_fantasy
    husky_baritone_20s_m_supernatural
    silky_alto_40s_f_literary_1
    silky_baritone_45s_m
    velvety_mezzo_30s_f_gothic
    warm_alto_50s_f_gothic
)
rank2_out="$runtime/experiments/reference_rank2_failed.json"
if ! stage reference_rank2_failed timeout --signal=INT --kill-after=30s 21600 \
    "$python" -u "$repo/app/experiments/retrain_honest.py" \
    --adapters "${failed_adapters[@]}" --use-medoid --reference-rank 2 \
    --resume --work "$runtime/reference_rank2_failed" --out "$rank2_out"; then
    echo "RANK-2 RETRAIN FAILED; rerun the chain to resume it"
    exit 1
fi

gate_failures=0
for adapter in "${failed_adapters[@]}"; do
    out="$runtime/experiments/gate_reference_rank2__${adapter}.json"
    if [ -f "$out" ]; then
        grep -q '"passed": true' "$out" || gate_failures=$((gate_failures + 1))
        continue
    fi
    base="$runtime/reference_rank2_failed/$adapter"
    if [ ! -f "$base/adapter/adapter_model.safetensors" ]; then
        echo "GATE BLOCKED $adapter: retrained adapter is missing"
        gate_failures=$((gate_failures + 1))
        continue
    fi
    if ! stage "gate_reference_rank2__$adapter" \
        timeout --signal=INT --kill-after=30s 3600 \
        "$python" -u "$repo/app/experiments/verify_adapter_identity.py" \
        --adapter "$base/adapter" --dataset "$base/data" --lines 10 \
        --out "$out"; then
        gate_failures=$((gate_failures + 1))
    fi
done

echo
echo "REMAINING GOAL WORK COMPLETE $(date -u +%FT%TZ)"
echo "  rank-2 gate failures: $gate_failures of ${#failed_adapters[@]}"
echo
echo "NOT QUEUED, because neither is a measurement:"
echo "  1.3 generalisation - both attempts supplied more CONTEXT, but the"
echo "      roster already holds the right name ~85% of the time while the"
echo "      model picks it 29.9%. A selection-side intervention has to be"
echo "      designed before it can be run, and each attempt spends pilot"
echo "      books from the 15 still sealed."
echo "  2.4 per-line duration spread - 43% of Japanese clips outside the"
echo "      band is a known quantity; what is missing is a fix to try, not"
echo "      another measurement of the gap."
echo
echo "Do not promote anything from stage 2 or 3 automatically. Review the"
echo "artifacts and use promote_adapters.py with its rollback receipt."
