#!/usr/bin/bash
set -uo pipefail
unit=alexandria-targeted-repair-7h-20260814.service
log=/home/fakemitch/pinokio/api/alexandria-audiobook2.git/ab_test_runtime/experiments/targeted_missing_repair_7h_20260814/logs/watchdog.log
mkdir -p "$(dirname "$log")"; idle_samples=0
echo "WATCHDOG_START $(date -u +%FT%TZ)" >> "$log"
while systemctl --user is-active --quiet "$unit"; do
  values=$(/opt/rocm/bin/rocm-smi --showuse --showmemuse --json 2>/dev/null | /usr/bin/jq -r '.card0 | [."GPU use (%)", ."GPU Memory Allocated (VRAM%)"] | @tsv' 2>/dev/null)
  gpu_use=${values%%$'\t'*}; vram_use=${values##*$'\t'}
  if [[ "$gpu_use" =~ ^[0-9]+$ ]] && [[ "$vram_use" =~ ^[0-9]+$ ]] && [ "$gpu_use" -le 5 ] && [ "$vram_use" -ge 20 ]; then idle_samples=$((idle_samples + 1)); else idle_samples=0; fi
  echo "SAMPLE $(date -u +%FT%TZ) gpu=${gpu_use:-unknown} vram=${vram_use:-unknown} idle_samples=$idle_samples" >> "$log"
  if [ "$idle_samples" -ge 15 ]; then echo "IDLE_ABORT $(date -u +%FT%TZ)" >> "$log"; systemctl --user stop "$unit"; exit 1; fi
  sleep 60
done
echo "WATCHDOG_EXIT $(date -u +%FT%TZ) campaign_inactive" >> "$log"
