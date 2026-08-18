#!/usr/bin/bash
# Goal 5.4: confirm the Japanese Silero+whisper.cpp result at the sample size
# English and Chinese were held to (50 clips), so the goal can move to MET.
#
# The held-out n=10 arm already clears both target conditions (CER 7.67% vs
# 20%, alignment median 39 ms vs 150 ms). What was missing was sample size,
# and that was blocked on corpus rather than compute: the same-speaker build's
# novel has 6,294 transcript rows but only 34 downloaded clips, so no
# re-slicing of it reaches 50.
#
# 5.4 measures TRANSCRIPTION AND ALIGNMENT, not voice identity, so it does not
# need the same-speaker design that build inherits from the voice goals. The
# set is therefore cut from the four Japanese novels whose LibriVox audio is
# already on disk - no network, and four readers instead of one, which is the
# broader claim rather than a weaker one.
#
# Cutting is idempotent: existing clips are reused, so a rerun costs nothing.
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
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
mkdir -p "$runtime/logs" "$runtime/experiments"

clips="${JA_CONFIRM_CLIPS:-50}"
build_dir="$runtime/kokoro_ja_asr_eval"
out="$runtime/experiments/asr_silero_whisper_ja_confirmation.json"

if [ -f "$out" ]; then
    echo "already confirmed: $out"
    exit 0
fi

# CPU only, and outside the GPU lock - cutting audio must not hold the card.
if ! "$python" -u "$repo/app/experiments/kokoro_ja_asr_set.py" \
    --clips "$clips" --out-dir "$build_dir"; then
    echo "JAPANESE CLIP SET FAILED; ASR stage not started"
    exit 1
fi

if ! "$repo/gpu_job.sh" asr_silero_whisper_ja_confirmation \
    timeout --signal=INT --kill-after=30s 3600 \
    "$python" -u "$repo/app/experiments/asr_backends.py" \
    --build "$build_dir/build.json" \
    --backends silero_whisper_cpp --lang ja --limit "$clips" \
    --align-clips "$clips" \
    --whisper-cpp-bin "$repo/whisper.cpp/build/bin/whisper-cli" \
    --whisper-cpp-model "$repo/whisper.cpp/models/ggml-base.bin" \
    --out "$out"; then
    echo "JAPANESE ASR CONFIRMATION FAILED"
    exit 1
fi

echo "wrote $out"
echo "If CER <= 20% and alignment median <= 150 ms, goal 5.4 moves to MET."
