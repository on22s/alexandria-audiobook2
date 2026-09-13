#!/usr/bin/bash
# Character speech vs narration as LoRA training data, one reader, one
# instrument. Piits et al. (LREC 2022) trained three same-speaker corpora and
# found the character-speech voice scored lowest with every synthesiser,
# blaming its acoustic variability; LibriQuote (Findings of ACL 2026) argues
# the opposite direction for expressivity. Both are about the same choice this
# pipeline makes silently: a narrator dataset is a mixture of that reader's
# narration and every character they perform (dataset_tone_spread, r=0.58).
#
# LibriQuote-test reader 2033 reads four books; libriquote_fetch.py wrote the
# reader's quotations and their matched narration utterances as two
# LJSpeech-shaped corpora. Each trains an adapter with the eval-set recipe
# (RECIPES.md: 6 epochs, lr 1e-6, r 32, alpha 128, 200 clips, seed 1234), and
# both adapters are then generated against BOTH held-out sets - the third
# book's quotes and its narration - so the comparison is on identical lines.
# Prediction, written first: the narration adapter scores higher ECAPA on both
# held-out sets (Piits); if the quotes adapter matches it on fidelity and wins
# on prosody_fidelity for held-out quotes, LibriQuote's claim holds on this
# stack too.
#
# Licence: CC BY-NC 4.0 - evidence only, nothing here ships.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="${PYTHON:-$REPO/app/env/bin/python}"
[ -x "$python" ] || python="/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python"
config="${CONFIG:-$REPO/app/config.json}"
[ -f "$config" ] || config="/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/config.json"
STAGE_LOG_DIR="$runtime/logs/libriquote_2033_20260913"
corpus="$runtime/corpora/libriquote/2033"
work="$runtime/libriquote_2033_eval"
TEST_BOOK="${TEST_BOOK:-3585}"
mkdir -p "$STAGE_LOG_DIR" "$work"
source "$REPO/run_chains/lib/stage.sh"

for arm in quotes narration; do
    [ -s "$corpus/$arm/metadata.csv" ] || { stage_note "REFUSING: no corpus at $corpus/$arm - run libriquote_fetch.py first"; exit 1; }
    # Quotations run short; 30 chars keeps the same floor for both arms so
    # the only difference between them is what kind of speech they are.
    [ -s "$corpus/$arm/split.json" ] || \
    run_stage prepare_$arm 10m -- \
        "$python" -u "$REPO/app/experiments/ljspeech_prepare.py" \
        --root "$corpus/$arm" --out "$corpus/$arm/split.json" \
        --test-books "$TEST_BOOK" --min-chars 30
    [ -s "$work/$arm/build.json" ] || \
    run_stage build_$arm 30m -- \
        "$python" -u "$REPO/app/experiments/ljspeech_build.py" \
        --split "$corpus/$arm/split.json" --out "$work/$arm"
    [ -s "$work/$arm/adapter/adapter_model.safetensors" ] || \
    run_stage train_$arm 1h --needs-vram -- \
        "$REPO/gpu_job.sh" "libriquote_2033_train_$arm" \
        "$python" -u "$REPO/app/train_lora.py" \
        --data_dir "$work/$arm/train" --output_dir "$work/$arm/adapter" \
        --epochs 6 --lr 1e-6 --lora_r 32 --lora_alpha 128 --seed 1234
    [ -s "$work/$arm/stop_check/verify_adapter_stops.json" ] || \
    run_stage stop_gate_$arm 30m --needs-vram -- \
        "$REPO/gpu_job.sh" "libriquote_2033_stop_gate_$arm" \
        "$python" -u "$REPO/app/experiments/verify_adapter_stops.py" \
        --build "$work/$arm/build.json" --adapter "$work/$arm/adapter" --config "$config" \
        --lines 5 --seed 1234 --max-ratio 3.0 --out "$work/$arm/stop_check/verify_adapter_stops.json"
done

# Cross-generate: adapter A on held-out set H, for A and H in {quotes, narration}.
for adapter in quotes narration; do
    for heldout in quotes narration; do
        tag="libriquote_2033_${adapter}_adapter_on_${heldout}"
        [ -s "$runtime/experiments/${tag}_generate.json" ] || \
        run_stage generate_${adapter}_on_${heldout} 2h --needs-vram -- \
            "$REPO/gpu_job.sh" "$tag" \
            "$python" -u "$REPO/app/experiments/ljspeech_generate.py" \
            --build "$work/$heldout/build.json" --adapter "$work/$adapter/adapter" --config "$config" \
            --out-dir "$work/generated_${adapter}_on_${heldout}" --limit 100 --arms lora clone --seed 1234 \
            --out "$runtime/experiments/${tag}_generate.json"
        run_stage prosody_${adapter}_on_${heldout} 1h -- \
            "$python" -u "$REPO/app/experiments/prosody_fidelity.py" \
            --generated "$runtime/experiments/${tag}_generate.json" --limit 0 \
            --out "$runtime/experiments/prosody_${tag}.json"
        run_stage score_${adapter}_on_${heldout} 1h -- \
            "$python" -u "$REPO/app/experiments/ljspeech_score.py" \
            --generated "$runtime/experiments/${tag}_generate.json" --limit 0 \
            --out "$runtime/experiments/${tag}_score.json"
    done
done
stage_commit_artifacts libriquote_2033 "$REPO"
stage_summary libriquote_2033_20260913
