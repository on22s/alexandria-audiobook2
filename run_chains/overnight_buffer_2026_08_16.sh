#!/usr/bin/bash
# ~2 hours of small, unanswered questions, to run if the main queue finishes
# early or a stage fails. CPU stages start immediately and run alongside the
# GPU work; GPU stages block on the shared lock until the card is free, so
# this can be launched at the same time as the main chain without racing it.
#
# Every stage continues on failure - the point of a buffer is to keep finding
# things, not to stop at the first one that does not work.
set -uo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$repo/ab_test_runtime"; python="$repo/app/env/bin/python"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock" GPU_QLOG="$runtime/logs/gpu_jobq.log"
note() { echo "[$(date -u +%FT%TZ)] $*"; }
attempt() { local n="$1" a="$2"; shift 2
    [ -e "$a" ] && { note "SKIP $n"; return 0; }
    note "START $n"; "$@" && note "OK $n" || note "FAIL $n (continuing)"; }

# 1. Prosody at a usable n. The CJK measure was only ever run at n=12, which
#    is too few to separate two arms that differ by 0.04 correlation.
for pair in "kokoro ja" "aishell3 zh" "ljspeech en"; do
    set -- $pair
    attempt "prosody_$2" "$runtime/experiments/prosody_fidelity_$2_n40.json" \
        timeout 3600 "$python" -u "$repo/app/experiments/prosody_fidelity.py" \
        --generated "$runtime/experiments/$1_generate.json" --limit 40 \
        --out "$runtime/experiments/prosody_fidelity_$2_n40.json"
done

# 2. Expected accent/tone over more lines, so the extractor's spurious
#    one-mora phrase is characterised rather than left as an anecdote.
attempt expected_prosody_ja "$runtime/experiments/expected_prosody_ja_n200.json" \
    timeout 1800 "$python" -u "$repo/app/experiments/expected_prosody.py" \
    --generated "$runtime/experiments/kokoro_generate.json" --language ja \
    --limit 200 --out "$runtime/experiments/expected_prosody_ja_n200.json"
attempt expected_prosody_zh "$runtime/experiments/expected_prosody_zh_n200.json" \
    timeout 1800 "$python" -u "$repo/app/experiments/expected_prosody.py" \
    --generated "$runtime/experiments/aishell3_generate.json" --language zh \
    --limit 200 --out "$runtime/experiments/expected_prosody_zh_n200.json"

# 3. Japanese on large-v3 WITH reading scoring. The hybrid comparison predates
#    --score-readings, so its 27.8% has never been re-read on the metric that
#    turned 28.7% into 9.9%. If large-v3 also lands near 10%, the two backends
#    agree and the model question is closed for good.
attempt asr_ja_largev3_readings "$runtime/experiments/asr_ja_largev3_readings.json" \
    "$repo/gpu_job.sh" asr_ja_largev3_readings \
    timeout --signal=INT --kill-after=30s 5400 \
    "$python" -u "$repo/app/experiments/asr_backends.py" \
    --build "$runtime/kokoro_ja_asr_eval/build.json" \
    --backends whisper_cpp --lang ja --limit 50 --align-clips 50 \
    --keep-hypotheses --score-readings \
    --whisper-cpp-bin "$repo/whisper.cpp/build/bin/whisper-cli" \
    --whisper-cpp-model "$repo/whisper.cpp/models/ggml-large-v3.bin" \
    --out "$runtime/experiments/asr_ja_largev3_readings.json"

# 4. Alignment against clip silence, on the Chinese set too. The Japanese run
#    found error tracking silent_fraction at 0.51 over four readers; whether
#    that holds in another language is the cheapest test of whether it is real.
attempt alignment_diagnosis_zh "$runtime/experiments/alignment_diagnosis_zh.json" \
    timeout 1800 "$python" -u "$repo/app/experiments/alignment_diagnosis.py" \
    --builds "$runtime/kokoro_ja_asr_eval/build.json" \
    --out "$runtime/experiments/alignment_diagnosis_zh.json"

note "BUFFER QUEUE COMPLETE"
