#!/usr/bin/env bash
# run_random_corpus.sh — verify ROCm pipeline on 3 random book pairs.

set -u
: "${HF_TOKEN:?Set HF_TOKEN in the environment before running this corpus test}"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
AUDIO_DIR=${AUDIO_DIR:?Set AUDIO_DIR to the audiobook directory}
SOURCE_DIR=${SOURCE_DIR:?Set SOURCE_DIR to the source-book directory}
OUT_DIR="$SCRIPT_DIR/random_test_output"
MODEL="Qwen2.5-14B-Instruct-Q6_K.gguf"
mkdir -p "$OUT_DIR"

PAIRS=(
    "$AUDIO_DIR/Cliff Kurt Mushoku Tensei-converted.wav|$SOURCE_DIR/Mushoku Tensei - Volume 01.epub"
    "$AUDIO_DIR/Paul Boehmer Vampire Hunter D-converted.wav|$SOURCE_DIR/Vampire Hunter D - Volume 01.epub"
    "$AUDIO_DIR/J Michael Tatum Spice and Wolf, Vol. 10-converted.wav|$SOURCE_DIR/Spice and Wolf - Volume 10 [Yen Press][Kobo].epub"
)

start_epoch=$(date +%s)
i=0
for pair in "${PAIRS[@]}"; do
    i=$((i+1))
    audio="${pair%%|*}"
    source="${pair#*|}"
    stem=$(basename "$audio" | sed 's/\.[^.]*$//')
    output="$OUT_DIR/dataset.zip"

    echo ""
    echo "═════════════════════════════════════════════════════════════════"
    echo "[$i/${#PAIRS[@]}] $stem"
    echo "  audio  : $audio"
    echo "  source : $source"
    echo "═════════════════════════════════════════════════════════════════"

    # Running with --limit 5 to keep the test fast but verify the full GPU handoff
    "$SCRIPT_DIR/run_with_restart.sh" \
        --audio "$audio" \
        --model "$MODEL" \
        --source "$source" \
        --output "$output" \
        --limit 5 \
        --diarize \
        --hf-token "$HF_TOKEN" \
        --lang en

    rc=$?
    if (( rc == 130 )); then
        exit 130
    fi
done

elapsed=$(( $(date +%s) - start_epoch ))
echo ""
echo "Random corpus test complete in $(( elapsed / 60 ))m."
