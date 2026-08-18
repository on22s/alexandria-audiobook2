#!/usr/bin/bash
set -uo pipefail
unit=alexandria-three-pass-grimgar-full-20260815.service
log=/home/fakemitch/pinokio/api/alexandria-audiobook2.git/ab_test_runtime/experiments/three_pass_grimgar_full_20260815/watchdog.log
idle=0
while systemctl --user is-active --quiet "$unit"; do
  values=$(/opt/rocm/bin/rocm-smi --showuse --showmemuse --json 2>/dev/null | /usr/bin/jq -r '.card0 | [."GPU use (%)", ."GPU Memory Allocated (VRAM%)"] | @tsv')
  gpu=${values%%$'\t'*}; vram=${values##*$'\t'}
  if [ "$gpu" -le 5 ] && [ "$vram" -ge 20 ]; then idle=$((idle + 1)); else idle=0; fi
  echo "SAMPLE $(date -u +%FT%TZ) gpu=$gpu vram=$vram idle=$idle" >> "$log"
  if [ "$idle" -ge 15 ]; then systemctl --user stop "$unit"; exit 1; fi
  sleep 60
done
