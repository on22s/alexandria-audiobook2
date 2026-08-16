#!/usr/bin/bash
# Goal 5.4: confirm the Japanese Silero+whisper.cpp result at the sample size
# English and Chinese were held to (50 clips), so the goal can move to MET.
#
# WHY THIS IS NOT JUST A RERUN. The held-out n=10 arm already clears both
# target conditions (CER 7.67% <= 20%, alignment median 39 ms <= 150 ms). What
# is missing is sample size, and that is blocked on corpus rather than compute:
# the test novel has 6,294 transcript rows but only 34 downloaded audio clips,
# 31 of which pass the length filter. No re-slicing of the existing build can
# reach 50, which is why this script refuses rather than quietly measuring a
# smaller set and reporting it as confirmation.
#
# TO UNBLOCK, either:
#   a) fetch this novel's LibriVox audio (app/experiments/kokoro_fetch.py
#      downloads and cuts; ab_test_runtime/corpora/kokoro/librivox already has
#      405 MB for four OTHER novels and none for this one), or
#   b) cut a 50-clip Japanese set from the novels already downloaded. 5.4
#      measures transcription and alignment, not voice identity, so it does
#      not need the same-speaker design this build inherits from the voice
#      goals - and this path needs no network.
#
# The ASR run itself is ~3 minutes; the n=10 arm took 36 seconds.
set -uo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$repo/ab_test_runtime"
python="$repo/app/env/bin/python"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
mkdir -p "$runtime/logs" "$runtime/experiments"

target_clips="${JA_CONFIRM_CLIPS:-50}"
build_dir="$runtime/kokoro_ja_confirmation_eval"
out="$runtime/experiments/asr_silero_whisper_ja_confirmation.json"

if [ -f "$out" ]; then
    echo "already confirmed: $out"
    exit 0
fi

# Refuse before spending the GPU lock if the corpus cannot support the claim.
eligible="$("$python" - "$target_clips" <<'PY'
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(".")), "app"))
REPO = os.getcwd()
CORPUS = os.path.join(REPO, "ab_test_runtime", "corpora", "kokoro")
NOVEL = "kokoro-by-soseki-natsume"
texts = {}
meta = os.path.join(CORPUS, f"{NOVEL}.metadata.txt")
if os.path.exists(meta):
    with open(meta, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("|")
            if len(parts) >= 5:
                texts[parts[0]] = parts[4].replace(" ", "").strip()
count = 0
for path in glob.glob(os.path.join(CORPUS, "wavs", f"{NOVEL}-*.flac")):
    text = texts.get(os.path.splitext(os.path.basename(path))[0], "")
    if 18 <= len(text) <= 70:
        count += 1
print(count)
PY
)"

if [ "${eligible:-0}" -lt "$target_clips" ]; then
    echo "REFUSING: $eligible eligible Japanese clips on disk, $target_clips needed."
    echo "The n=10 result already clears the target; what is missing is sample"
    echo "size, and measuring a smaller set would not confirm anything."
    echo "See the header of this script for the two ways to unblock it."
    exit 1
fi

"$python" -u "$repo/app/experiments/kokoro_same_speaker_build.py" \
    --out-dir "$build_dir" || exit 1

if ! "$repo/gpu_job.sh" asr_silero_whisper_ja_confirmation \
    timeout --signal=INT --kill-after=30s 3600 \
    "$python" -u "$repo/app/experiments/asr_backends.py" \
    --build "$build_dir/build.json" \
    --backends silero_whisper_cpp --lang ja --limit "$target_clips" \
    --align-clips "$target_clips" \
    --whisper-cpp-bin "$repo/whisper.cpp/build/bin/whisper-cli" \
    --whisper-cpp-model "$repo/whisper.cpp/models/ggml-base.bin" \
    --out "$out"; then
    echo "JAPANESE ASR CONFIRMATION FAILED"
    exit 1
fi

echo "wrote $out"
echo "If CER <= 20% and alignment median <= 150 ms, goal 5.4 moves to MET."
