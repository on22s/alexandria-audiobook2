#!/usr/bin/bash
# A PUBLIC second English reference set for goal 2.9: Hi-Fi TTS reader 9017.
#
# 2.9 asks whether English agreeing with its human reference far worse than
# either CJK language is "a real weakness of that arm or an artifact of that
# eval set". The eight-narrator run of 2026-08-20 (second_english_eval)
# answered part of that on the user's own audiobooks - pooled f0 correlation
# 0.46 / 0.52 against LJSpeech's 0.29 / 0.34 - but those sets are private and
# their transcripts came from ASR. This is the same question on a corpus
# anyone can fetch: one LibriVox reader (John Van Stan, M), seven source
# works, human transcripts, CC BY 4.0, run through the LJSpeech pipeline
# unchanged so the two public English sets are measured by one instrument.
#
# HELD-OUT WORKS ARE CHOSEN, NOT DEFAULTED. ljspeech_prepare's default holds
# out the two smallest works, which here would be a forestry history and a
# true-crime collection. antoinetteromances4 and celebratedcrimesv1 are held
# out instead: both Dumas, both separate LibriVox projects from the three
# D'Artagnan volumes the adapter trains on, so the test lines stay in the
# fiction register the product ships and share no recording session with
# training.
#
# THE TRAINING RECIPE IS LJSPEECH'S, from ljspeech_eval/adapter/training_meta:
# 6 epochs, lr 5e-6, r 32, alpha 128, 200 clips. Same recipe, same seed, so a
# difference between the two English sets is the set and not the adapter.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="${PYTHON:-$REPO/app/env/bin/python}"
[ -x "$python" ] || python="/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python"
# app/config.json is per-machine and untracked, so a worktree has none.
config="${CONFIG:-$REPO/app/config.json}"
[ -f "$config" ] || config="/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/config.json"
STAGE_LOG_DIR="$runtime/logs/hifitts_9017_20260913"
corpus="$runtime/corpora/hifitts/9017"
work="$runtime/hifitts_9017_eval"
mkdir -p "$STAGE_LOG_DIR" "$work"
source "$REPO/run_chains/lib/stage.sh"

[ -s "$corpus/metadata.csv" ] || { stage_note "REFUSING: no corpus at $corpus - run hifitts_fetch.py first"; exit 1; }

# Each stage is skipped when its output exists, so a failed later stage can
# be rerun without retraining (a retrained adapter is not the same adapter).
[ -s "$corpus/split.json" ] || \
run_stage prepare 10m -- \
    "$python" -u "$REPO/app/experiments/ljspeech_prepare.py" \
    --root "$corpus" --out "$corpus/split.json" \
    --test-books antoinetteromances4 celebratedcrimesv1

[ -s "$work/build.json" ] || \
run_stage build 30m -- \
    "$python" -u "$REPO/app/experiments/ljspeech_build.py" \
    --split "$corpus/split.json" --out "$work"

[ -s "$work/adapter/adapter_model.safetensors" ] || \
run_stage train 1h --needs-vram -- \
    "$REPO/gpu_job.sh" "hifitts_9017_train" \
    "$python" -u "$REPO/app/train_lora.py" \
    --data_dir "$work/train" --output_dir "$work/adapter" \
    --epochs 6 --lr 5e-6 --lora_r 32 --lora_alpha 128 --seed 1234

[ -s "$runtime/experiments/hifitts_9017_generate.json" ] || \
run_stage generate 3h --needs-vram -- \
    "$REPO/gpu_job.sh" "hifitts_9017_generate" \
    "$python" -u "$REPO/app/experiments/ljspeech_generate.py" \
    --build "$work/build.json" --adapter "$work/adapter" --config "$config" \
    --out-dir "$work/generated" --limit 0 --arms lora clone --seed 1234 \
    --out "$runtime/experiments/hifitts_9017_generate.json"

run_stage prosody 1h -- \
    "$python" -u "$REPO/app/experiments/prosody_fidelity.py" \
    --generated "$runtime/experiments/hifitts_9017_generate.json" --limit 0 \
    --out "$runtime/experiments/prosody_hifitts_9017.json"

run_stage score 1h -- \
    "$python" -u "$REPO/app/experiments/ljspeech_score.py" \
    --generated "$runtime/experiments/hifitts_9017_generate.json" --limit 0 \
    --out "$runtime/experiments/hifitts_9017_score.json"

stage_commit_artifacts hifitts_9017 "$REPO"
stage_summary hifitts_9017_20260913
