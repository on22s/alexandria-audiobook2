#!/usr/bin/bash
# Tail of the 2026-08-16 night. GPU stages block on the shared lock, so this
# can be launched while the respelling measurement is still running - it will
# simply wait its turn. CPU stages start immediately.
#
# Every stage continues on failure. Two stages in the earlier buffer chain
# died instantly because expected_prosody.py did not exist on the checked-out
# branch; it has been restored, and they are retried here.
set -uo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$repo/ab_test_runtime"; python="$repo/app/env/bin/python"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock" GPU_QLOG="$runtime/logs/gpu_jobq.log"
note() { echo "[$(date -u +%FT%TZ)] $*"; }
attempt() { local n="$1" a="$2"; shift 2
    [ -e "$a" ] && { note "SKIP $n"; return 0; }
    note "START $n"; "$@" && note "OK $n" || note "FAIL $n (continuing)"; }

# 1. THE CAUSAL TEST for goal 5.4's alignment axis. The diagnosis found error
#    tracking silence at 0.51 across four readers - a correlation over four
#    points, which is weak and easy to over-read. This measures the SAME clips
#    with edge silence removed (15.3% of the audio) and nothing else changed.
#    If alignment improves, silence was the mechanism. If not, the correlation
#    was a property of the recordings and should stop being quoted as a lead.
attempt asr_ja_trimmed "$runtime/experiments/asr_ja_trimmed.json" \
    "$repo/gpu_job.sh" asr_ja_trimmed \
    timeout --signal=INT --kill-after=30s 5400 \
    "$python" -u "$repo/app/experiments/asr_backends.py" \
    --build "$runtime/kokoro_ja_trimmed/build.json" \
    --backends silero_whisper_cpp --lang ja --limit 50 --align-clips 50 \
    --keep-hypotheses --score-readings \
    --whisper-cpp-bin "$repo/whisper.cpp/build/bin/whisper-cli" \
    --whisper-cpp-model "$repo/whisper.cpp/models/ggml-base.bin" \
    --out "$runtime/experiments/asr_ja_trimmed.json"

# 2. Clip properties of the trimmed set, so the comparison has both halves.
attempt alignment_diagnosis_trimmed "$runtime/experiments/alignment_diagnosis_trimmed.json" \
    timeout 1800 "$python" -u "$repo/app/experiments/alignment_diagnosis.py" \
    --builds "$runtime/kokoro_ja_trimmed/build.json" \
    --out "$runtime/experiments/alignment_diagnosis_trimmed.json"

# 3. English prosody at n=100. It is the least stable of the three languages -
#    clone moved 0.278 -> 0.403 between n=12 and n=40, a large jump that means
#    neither figure can be quoted yet.
attempt prosody_en_n100 "$runtime/experiments/prosody_fidelity_en_n100.json" \
    timeout 5400 "$python" -u "$repo/app/experiments/prosody_fidelity.py" \
    --generated "$runtime/experiments/ljspeech_generate.json" --limit 100 \
    --out "$runtime/experiments/prosody_fidelity_en_n100.json"

# 4. The two stages that died on a missing file, retried now it is restored.
attempt expected_prosody_ja "$runtime/experiments/expected_prosody_ja_n200.json" \
    timeout 1800 "$python" -u "$repo/app/experiments/expected_prosody.py" \
    --generated "$runtime/experiments/kokoro_generate.json" --language ja \
    --limit 200 --out "$runtime/experiments/expected_prosody_ja_n200.json"
attempt expected_prosody_zh "$runtime/experiments/expected_prosody_zh_n200.json" \
    timeout 1800 "$python" -u "$repo/app/experiments/expected_prosody.py" \
    --generated "$runtime/experiments/aishell3_generate.json" --language zh \
    --limit 200 --out "$runtime/experiments/expected_prosody_zh_n200.json"

# 5. Japanese prosody at n=100 as well, since the CJK split (clone wins ja,
#    lora wins zh) is currently resting on 40 lines each.
attempt prosody_ja_n100 "$runtime/experiments/prosody_fidelity_ja_n100.json" \
    timeout 5400 "$python" -u "$repo/app/experiments/prosody_fidelity.py" \
    --generated "$runtime/experiments/kokoro_generate.json" --limit 100 \
    --out "$runtime/experiments/prosody_fidelity_ja_n100.json"
attempt prosody_zh_n100 "$runtime/experiments/prosody_fidelity_zh_n100.json" \
    timeout 5400 "$python" -u "$repo/app/experiments/prosody_fidelity.py" \
    --generated "$runtime/experiments/aishell3_generate.json" --limit 100 \
    --out "$runtime/experiments/prosody_fidelity_zh_n100.json"

note "TAIL QUEUE COMPLETE"
echo
echo "READ FIRST: asr_ja_trimmed.json against asr_silero_whisper_ja_confirmation.json"
echo "  272 ms was the untrimmed alignment median. If the trimmed run comes in"
echo "  materially lower on the SAME clips, edge silence was the mechanism and"
echo "  goal 5.4's last open axis has an answer. If it does not move, the 0.51"
echo "  correlation was recording style, not cause."
