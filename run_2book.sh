#!/usr/bin/env bash
# run_2book.sh — 2-book corpus run: Spice & Wolf Vol.10 + Mushoku Tensei Vol.01
# Subset of run_subset.sh; reproducible with hardcoded pairs.

set -u

# Enable Triton-based Flash Attention on ROCm so Wav2Vec2 / Whisper attention
# kernels stop falling back to the slower SDPA math path. Safe no-op on
# non-ROCm builds; the variable is only read when AOTriton is compiled in.
export TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
AUDIO_DIR=${AUDIO_DIR:?Set AUDIO_DIR to the audiobook directory}
SOURCE_DIR=${SOURCE_DIR:?Set SOURCE_DIR to the source-book directory}
OUT_DIR="$SCRIPT_DIR/test_corpus_output"
MODEL="Qwen2.5-14B-Instruct-Q6_K.gguf"
FALLBACK="Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf"
mkdir -p "$OUT_DIR"

# audio | source pairs (separator is `|`)
PAIRS=(
    "$AUDIO_DIR/J Michael Tatum Spice and Wolf, Vol. 10-converted.wav|$SOURCE_DIR/Spice and Wolf - Volume 10 [Yen Press][Kobo].epub"
    "$AUDIO_DIR/Cliff Kurt Mushoku Tensei-converted.wav|$SOURCE_DIR/Mushoku Tensei - Volume 01.epub"
)

notify() {
    local title="$1"; local body="$2"
    notify-send --app-name "Alexandria 2-book" -u normal "$title" "$body" 2>/dev/null || true
    printf '\a' >&2
}

start_epoch=$(date +%s)
i=0
for pair in "${PAIRS[@]}"; do
    i=$((i+1))
    audio="${pair%%|*}"
    source="${pair#*|}"
    stem=$(basename "$audio" | sed 's/\.[^.]*$//')
    # Pass the generic 'dataset.zip' placeholder so the preparer's auto-naming
    # derives the final filename from the --source ePub's title/author. The
    # directory portion is preserved by maybe_autoname_output().
    output="$OUT_DIR/dataset.zip"

    echo ""
    echo "═════════════════════════════════════════════════════════════════"
    echo "[$i/${#PAIRS[@]}] $stem"
    echo "  audio  : $audio"
    echo "  source : $source"
    echo "  output : $output"
    echo "═════════════════════════════════════════════════════════════════"

    "$SCRIPT_DIR/run_with_restart.sh" \
        --audio "$audio" \
        --model "$MODEL" \
        --fallback-model "$FALLBACK" \
        --source "$source" \
        --output "$output" \
        --chunk-size 10.0 \
        --lang en

    rc=$?
    if (( rc == 130 )); then
        echo ""
        echo "Wrapper exited 130 (user aborted). Stopping run."
        echo "Stopped after $i/${#PAIRS[@]}" > "$OUT_DIR/ABORTED.flag"
        notify "Alexandria 2-book aborted" "Stopped after $i/${#PAIRS[@]} books. See $OUT_DIR/"
        exit 130
    fi
    if (( rc != 0 )); then
        echo "Wrapper failed for book $i (exit $rc)." >&2
        exit "$rc"
    fi
done

elapsed=$(( $(date +%s) - start_epoch ))
hrs=$(( elapsed / 3600 ))
mins=$(( (elapsed % 3600) / 60 ))
echo ""
echo "All ${#PAIRS[@]} runs complete in ${hrs}h ${mins}m."

{
    echo "Completed: $(date -Iseconds)"
    echo "Elapsed:   ${hrs}h ${mins}m"
    echo "Books:     ${#PAIRS[@]}"
    echo "Outputs:"
    ls -lh "$OUT_DIR"/*.zip 2>/dev/null | awk '{print "  " $NF, "(" $5 ")"}'
} > "$OUT_DIR/DONE.flag"

notify "Alexandria 2-book complete" "${#PAIRS[@]} books done in ${hrs}h ${mins}m. See $OUT_DIR/"
