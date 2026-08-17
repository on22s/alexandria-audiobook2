#!/bin/bash
# Does zero-shot cloning reach what a trained LoRA achieves?
#
# If it does, goal 2.7's 60 contaminated adapters, the Voice Lab chain and the
# reference-clip work all become optional - they exist to produce something a
# zero-shot model would give for free. If it does not, the question closes.
# One run answers it either way, which is why it is worth the card time.
#
# SCOPE, AND IT MATTERS. This scores single-speaker identity match only. MOSS
# is built for dialogue flow, turn-taking and 60-minute consistency, and it is
# capped at 5 speakers - measured against these books, 12% of index18 scenes
# and 41% of grimgar03 scenes exceed that. A weak score here means "do not
# replace the LoRA pipeline with it", not "the model is weak", and that
# distinction has to survive into the write-up.
#
# LAST IN THE QUEUE on purpose: it downloads ~16 GB of weights and loads an 8B
# model onto a card that already struggles to hold the LLM and TTS together.
# Everything ahead of it answers a question about the app as it exists.
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git

# HOLD THE REAL LOCK INSTEAD OF GUESSING WHO IS RUNNING.
#
# This used to poll: `while pgrep -f "three_pass_generate.py" ...; do sleep;
# done`. Three faults, and the third is the one that bit:
#
#   1. It races. Between "nothing is running" and "start mine", something else
#      can start - which is the concurrency this is meant to prevent.
#   2. `pgrep -f` matches any command line CONTAINING the string, including the
#      shell that ran it.
#   3. IT ENUMERATES SCRIPT NAMES, so a GPU job nobody listed is invisible. On
#      2026-08-17 `measure_respellings.py` held the card for seventeen hours and
#      appears in none of these lists; this chain would have launched straight
#      into it.
#
# gpu_job.sh's flock is the actual mutex - it does not care what the other job
# is called - and re-execing through it also brings the dirty-tree gate and the
# provenance stamp. The sentinel prevents infinite re-exec. Same idiom as
# run_chains/pdnc_context_evidence.sh, which already did this correctly.
if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
    exec "$REPO/gpu_job.sh" "moss_vs_lora" \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
fi
L="$REPO/ab_test_runtime/logs"
PY="$REPO/app/env/bin/python"
cd "$REPO/app"


# The 8B model needs the card to itself. Stop the LLM server if it is still up:
# nothing behind this point needs it, and 9 GB of held VRAM is the difference
# between this loading and OOMing.
if pgrep -x llama-server >/dev/null 2>&1; then
    echo "stopping llama-server to free VRAM for the 8B model"
    pkill -x llama-server 2>/dev/null
    sleep 10
fi

timeout 21600 "$PY" -u experiments/moss_vs_lora.py \
    --adapters warm_tenor_25s_m_military breathy_tenor_18s_m_supernatural \
               warm_mezzo_30s_f_fantasy_2 \
    --lines 10 \
    --out "$REPO/ab_test_runtime/experiments/moss_vs_lora.json" \
    > "$L/moss_vs_lora.log" 2>&1
echo "  rc=$?"
tail -12 "$L/moss_vs_lora.log" | sed 's/^/  /' | cut -c1-105
echo ""
echo "  For reference, these adapters' retrained held-out scores:"
echo "    warm_tenor_25s_m_military        0.692"
echo "    breathy_tenor_18s_m_supernatural 0.537"
echo "    warm_mezzo_30s_f_fantasy_2       0.519"
echo "MOSS COMPARISON DONE $(date -u +%FT%TZ)"
