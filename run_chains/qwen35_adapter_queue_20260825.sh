#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 MODEL_PATH MODEL_SUFFIX [--load_in_4bit]" >&2
  exit 2
fi

ROOT=/home/ubuntu/alexandria-training-20260823
TRAINER="$ROOT/distill_train_qwen35_20260825.py"
MODEL=$1
SUFFIX=$2
QUANT=${3:-}
COMMIT=dfa340386
STATUS="$ROOT/logs/qwen35_${SUFFIX}_queue.status"
LOCK=/home/ubuntu/qwen35_training_gpu.lock

mkdir -p "$ROOT/logs" "$ROOT/output_qwen35"
exec 9>"$LOCK"
if ! flock -n 9; then
  printf '%s REFUSED gpu lock held\n' "$(date --iso-8601=seconds)" > "$STATUS"
  exit 3
fi

used=$(nvidia-smi --query-compute-apps=used_memory --format=csv,noheader,nounits \
  2>/dev/null | awk '{s += $1} END {print s + 0}')
if (( used > 512 )); then
  printf '%s REFUSED gpu already uses %s MiB\n' \
    "$(date --iso-8601=seconds)" "$used" > "$STATUS"
  exit 4
fi
if [[ ! -s "$TRAINER" || ! -s "$MODEL/config.json" ]]; then
  printf '%s REFUSED missing trainer or model\n' "$(date --iso-8601=seconds)" > "$STATUS"
  exit 5
fi

run_recipe() {
  local recipe=$1 epochs=$2 data=$3
  local name="qwen35_${SUFFIX}_${recipe}"
  local out="$ROOT/output_qwen35/$name"
  local log="$ROOT/logs/${name}.log"
  if [[ -s "$out/adapter_model.safetensors" ]]; then
    printf '%s SKIP %s complete commit=%s\n' \
      "$(date --iso-8601=seconds)" "$name" "$COMMIT" > "$STATUS"
    return
  fi
  printf '%s START %s commit=%s model=%s quant=%s\n' \
    "$(date --iso-8601=seconds)" "$name" "$COMMIT" "$MODEL" \
    "${QUANT:-bf16}" > "$STATUS"
  # shellcheck disable=SC2086
  env PYTHONPATH=/home/ubuntu/alexandria-audiobook2.git/app \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    python3 -u "$TRAINER" --data $data --model "$MODEL" --out "$out" \
      --epochs "$epochs" --lr 1e-4 --lora_r 16 --lora_alpha 32 \
      --batch_size 1 --grad_accum 8 --max_len 2048 --save_steps 100 \
      --label_field teacher $QUANT > "$log" 2>&1
  printf '%s OK %s commit=%s\n' \
    "$(date --iso-8601=seconds)" "$name" "$COMMIT" > "$STATUS"
}

cd "$ROOT"
run_recipe author_heldout_balanced 3 'data/train__pdnc_*.jsonl'
run_recipe speaker_hardcases_split_nonmajor 3 \
  'data/train_hardcases_split_nonmajor.jsonl'
run_recipe speaker_longcontext_tophalf_5epoch 5 \
  'data/train_longcontext_tophalf.jsonl'
printf '%s COMPLETE qwen35_%s all recipes commit=%s\n' \
  "$(date --iso-8601=seconds)" "$SUFFIX" "$COMMIT" > "$STATUS"
