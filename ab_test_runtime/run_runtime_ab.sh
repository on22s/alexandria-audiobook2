#!/usr/bin/env bash
# Runtime backend A/B: run the FULL three-pass A/B suite (gemma, qwen27b, qwen9b)
# on Volume 10wn, entirely under Vulkan, then switch the LM Studio GGUF runtime to
# ROCm and run the entire suite again. Temp 0.6 (config default), 3 repeats per
# arm to average stochastic variation. Every run writes its own manifest
# (per-pass elapsed + resolution counts) and run.log (per-call tokens/sec), so
# analyze_runtime_ab.py can rank speed + quality across all runs afterward.
#
# Outputs never overwrite: ab_test_runtime/<backend>/<model>/rep<N>/. Resumable -
# a run whose final <book>.json exists is skipped. VRAM freed before every run and
# before every runtime switch.
set -eu
APP="${APP:-/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app}"
OUT="${OUT:-/home/fakemitch/pinokio/api/alexandria-audiobook2.git/ab_test_runtime}"
PY="${PY:-$APP/env/bin/python}"; LMS="${LMS:-/home/fakemitch/.lmstudio/bin/lms}"
MASTER="$OUT/run.log"
REPEATS="${REPEATS:-3}"
cd "$APP" || exit 1
BOOK="Arc 4 - Volume 10wn"
GEMMA="gemma-4-e4b-uncensored-hauhaucs-aggressive"
QWEN9="qwen3.5-9b-uncensored-hauhaucs-aggressive"
QWEN27="qwen3.6-27b-uncensored-hauhaucs-aggressive"
VULKAN="llama.cpp-linux-x86_64-vulkan-avx2@2.26.0"
ROCM="llama.cpp-linux-x86_64-amd-rocm-avx2@2.26.0"

set_model () { "$PY" - "$1" <<'PY'
import json, os, sys, tempfile
p = "config.json"
with open(p, encoding="utf-8") as fh:
    c = json.load(fh)
c.setdefault("llm", {})["model_name"] = sys.argv[1]
fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=".")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(c, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
}

select_runtime () {  # engine alias
  "$LMS" unload --all >/dev/null 2>&1
  local got selected
  got="$("$LMS" runtime select "$1" 2>&1)"
  echo "### runtime -> $got" | tee -a "$MASTER"
  selected="$("$LMS" runtime ls 2>&1 | grep "✓")" || {
    echo "ERROR: no selected runtime reported after requesting $1" | tee -a "$MASTER" >&2
    return 1
  }
  echo "$selected" | tee -a "$MASTER"
  case "$selected" in
    *"$1"*) ;;
    *) echo "ERROR: selected runtime does not match requested alias $1" | tee -a "$MASTER" >&2; return 1 ;;
  esac
}

run_arm () {  # backend tag model
  local backend="$1" tag="$2" model="$3" r dir out code
  set_model "$model"
  for r in $(seq 1 "$REPEATS"); do
    dir="$OUT/$backend/$tag/rep$r"; mkdir -p "$dir"
    out="$dir/$BOOK.json"
    if [ -f "$out" ]; then echo "=== [$backend/$tag/rep$r] already complete ===" | tee -a "$MASTER"; continue; fi
    "$LMS" unload --all >/dev/null 2>&1
    echo "=== [$backend/$tag/rep$r] $model start $(date -Is) ===" | tee -a "$MASTER"
    if "$PY" three_pass_generate.py "uploads/$BOOK.txt" --pass2-on-exhaustion fail \
        --output "$out" >> "$dir/$BOOK.log" 2>&1; then
      code=0
    else
      code=$?
    fi
    echo "=== [$backend/$tag/rep$r] exit=$code $(date -Is) ===" | tee -a "$MASTER"
  done
}

phase () {  # backend engine
  echo "########## PHASE $1 START $(date -Is) ##########" | tee -a "$MASTER"
  select_runtime "$2"
  run_arm "$1" gemma   "$GEMMA"
  run_arm "$1" qwen27b "$QWEN27"
  run_arm "$1" qwen9b  "$QWEN9"
  echo "########## PHASE $1 COMPLETE $(date -Is) ##########" | tee -a "$MASTER"
}

main () {
  echo "########## RUNTIME A/B START $(date -Is) (REPEATS=$REPEATS) ##########" | tee -a "$MASTER"
  phase vulkan "$VULKAN"
  phase rocm   "$ROCM"
  set_model "$GEMMA"
  echo "########## RUNTIME A/B COMPLETE $(date -Is) ##########" | tee -a "$MASTER"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
