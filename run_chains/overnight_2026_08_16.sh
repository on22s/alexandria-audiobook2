#!/usr/bin/bash
# Unattended queue for the night of 2026-08-16, ~19 hours.
#
# ONE FAILING STAGE MUST NOT END THE NIGHT. Every stage is skipped if its
# artifact already exists and, unlike the research chains, a failure is logged
# and the queue CONTINUES. Stopping the chain at 2am on one bad stage wastes
# the other seventeen hours, which is the opposite of what this is for.
#
# NO LLM WORK IS QUEUED, deliberately. llama-server was down at 18:00 - which
# is why the PR #308 narration remeasurement failed rc=1 on both books earlier
# and produced an empty artifact. Starting a 14B model unattended would hold
# ~9 GB of the 16 GB card all night and contend with the TTS stages below;
# `run_chains/moss_vs_lora.sh` already kills llama-server for exactly that
# reason. The attribution work therefore waits for a human. To unblock it:
#
#     LLAMA_MODEL=~/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf \
#         ./ensure_llama_server.sh
#
# That model is the one the last successful PDNC run recorded in its
# provenance, so it is the one that reproduces those numbers.
set -uo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$repo/ab_test_runtime"
python="$repo/app/env/bin/python"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
mkdir -p "$runtime/logs" "$runtime/experiments"

failures=0
note() { echo "[$(date -u +%FT%TZ)] $*"; }
attempt() {                     # attempt <name> <artifact> <cmd...>
    local name="$1" artifact="$2"; shift 2
    if [ -e "$artifact" ]; then note "SKIP $name (artifact exists)"; return 0; fi
    note "START $name"
    if "$@"; then note "OK   $name"; else
        failures=$((failures + 1)); note "FAIL $name (continuing)"
    fi
}

# ---------------------------------------------------------------- 1. cheap
# Two minutes, and it catches a repo that cannot even compile before the long
# stages spend the night discovering the same thing.
attempt release_verification "$runtime/experiments/overnight_release.json" \
    "$python" -u "$repo/app/verify_release.py" \
    --json-report "$runtime/experiments/overnight_release.json"

# ---------------------------------------------------------------- 2. the night
# ~17 hours: every candidate term appearing in >= 5 books gets its respelling
# measured. This is the measurement pronunciation.json demands before an entry
# is filled in, and it is what turns 9,381 scanned candidates into a shortlist.
#
# Checkpointed per term, so an interrupt costs one word. Runs under the GPU
# lock because it loads Qwen3-TTS.
attempt respelling_measurement "$runtime/experiments/respelling_measure_done" \
    "$repo/gpu_job.sh" respelling_measurement \
    timeout --signal=INT --kill-after=60s 68400 \
    "$python" -u "$repo/app/experiments/measure_respellings.py" \
    --min-books 5 \
    --out "$runtime/experiments/respelling_measure.json"

# ---------------------------------------------------------------- 3. eyes
# Goal 6.5: five arms measured since 2026-08-06 have never been looked at.
# CPU only - matplotlib and librosa - so it does not wait on the card.
for arm in kokoro_same_speaker aishell3_SSB0748; do
    attempt "view_$arm" "$runtime/voice_compare/$arm.html" \
        timeout 1800 "$python" -u "$repo/app/experiments/voice_compare_view.py" \
        --generated "$runtime/experiments/${arm}_generate.json" \
        --lines 6 --pick spread \
        --out "$runtime/voice_compare/$arm.html"
done

# ---------------------------------------------------------------- 4. spare
# If the night finishes early, widen the measurement rather than idle. The
# same artifact is extended, and terms already measured are skipped.
attempt respelling_widen "$runtime/experiments/respelling_widen_done" \
    "$repo/gpu_job.sh" respelling_widen \
    timeout --signal=INT --kill-after=60s 21600 \
    "$python" -u "$repo/app/experiments/measure_respellings.py" \
    --min-books 3 \
    --out "$runtime/experiments/respelling_measure.json"

note "OVERNIGHT QUEUE COMPLETE with $failures failed stage(s)"
echo
echo "WHAT TO READ FIRST:"
echo "  ab_test_runtime/experiments/respelling_measure.json"
echo "     - terms where 'helps' is true are respellings the Japanese ASR"
echo "       agreed with more after the change. That is evidence the phonemes"
echo "       moved, NOT that it sounds natural - confirm by ear before any"
echo "       entry goes into pronunciation.json."
echo "  ab_test_runtime/experiments/overnight_release.json"
echo
echo "STILL BLOCKED ON A HUMAN:"
echo "  - llama-server is down, so no attribution work ran. Start it with the"
echo "    command in this script's header to unblock goal 1.3 and the PR #308"
echo "    narration remeasurement, which has never once succeeded."
echo "  - the blinded listening test (goal 7.1) needs ears, not the GPU."
