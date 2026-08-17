#!/usr/bin/env bash

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
PREP=${PREP:-"$SCRIPT_DIR/alexandria_preparer_rocm_compatible.py"}
PY=${PY:-"$SCRIPT_DIR/../alexandria-audiobook.git/app/env/bin/python"}
export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
MODEL=${MODEL:-Qwen2.5-14B-Instruct-Q6_K.gguf}
AUDIO_DIR=${AUDIO_DIR:?Set AUDIO_DIR to the audiobook directory}
BOOKS_DIR=${BOOKS_DIR:?Set BOOKS_DIR to the source-book directory}
OUT_BASE=${OUT_BASE:?Set OUT_BASE to the output directory}

echo "[$(date)] Starting remaining batch — 5 narrators"

run_narrator() {
  local stem="$1"
  local audio="$2"
  local epub="$3"
  local output="$OUT_BASE/$stem/$stem"
  echo "[$(date)] === $stem ==="
  mkdir -p "$OUT_BASE/$stem"

  # First attempt (pass --resume if checkpoint already exists)
  $PY $PREP --audio "$audio" --model "$MODEL" --output "$output" --source "$epub" --resume
  local exit_code=$?

  # Auto-resume loop: retry up to 5 times on crash
  local attempt=1
  while [ $exit_code -ne 0 ] && [ $attempt -le 5 ]; do
    echo "[$(date)] WARNING: $stem crashed (exit $exit_code), auto-resuming (attempt $attempt/5)..."
    sleep 5
    $PY $PREP --audio "$audio" --model "$MODEL" --output "$output" --source "$epub" --resume
    exit_code=$?
    attempt=$((attempt + 1))
  done

  if [ $exit_code -ne 0 ]; then
    echo "[$(date)] ERROR: $stem failed after 5 resume attempts, skipping."
  else
    echo "[$(date)] DONE: $stem"
  fi
}

run_narrator \
  "Steven Pacey The Blade Itself-converted" \
  "$AUDIO_DIR/Steven Pacey The Blade Itself-converted.wav" \
  "$BOOKS_DIR/The Blade Itself (Joe Abercrombie) (z-library.sk, 1lib.sk, z-lib.sk).epub"

run_narrator \
  "Suzie Yeung Even If These Tears Disappear Tonight-converted" \
  "$AUDIO_DIR/Suzie Yeung Even If These Tears Disappear Tonight-converted.wav" \
  "$BOOKS_DIR/Even If These Tears Disappear Tonight - Complete [Yen Press][Kobo].epub"

run_narrator \
  "Suzie Yeung Stephen Fu 86--converted" \
  "$AUDIO_DIR/Suzie Yeung Stephen Fu 86--converted.wav" \
  "$BOOKS_DIR/86--Eighty-Six V11 - Dies Passionis.epub"

run_narrator \
  "Suzy Jackson Skyward-converted" \
  "$AUDIO_DIR/Suzy Jackson Skyward-converted.wav" \
  "$BOOKS_DIR/Skyward (Brandon Sanderson) (z-library.sk, 1lib.sk, z-lib.sk).epub"

run_narrator \
  "Tim Gerard Reynolds Age of War-converted" \
  "$AUDIO_DIR/Tim Gerard Reynolds Age of War-converted.wav" \
  "$BOOKS_DIR/Age of War Book Three of The Legends of the First Empire (Michael J. Sullivan) (z-library.sk, 1lib.sk, z-lib.sk).epub"

echo "[$(date)] All 5 narrators complete."
