#!/usr/bin/env bash
# Runtime backend A/B: run the FULL three-pass A/B suite (gemma, qwen27b, qwen9b)
# on Volume 10wn, entirely under Vulkan, then switch the LM Studio GGUF runtime to
# ROCm and run the qualified suite again. Models must pass the short three-pass
# corpus and one full run before repeats continue. Every run writes its own manifest
# (per-pass elapsed + resolution counts) and run.log (per-call tokens/sec), so
# analyze_runtime_ab.py can rank speed + quality across all runs afterward.
#
# Outputs never overwrite: ab_test_runtime/<backend>/<model>/rep<N>/. Resumable -
# a run whose final <book>.json exists is skipped. VRAM freed before every run and
# before every runtime switch.
set -eu
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${APP:-/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app}"
OUT="${OUT:-/home/fakemitch/pinokio/api/alexandria-audiobook2.git/ab_test_runtime}"
PY="${PY:-$APP/env/bin/python}"; LMS="${LMS:-/home/fakemitch/.lmstudio/bin/lms}"
MASTER="$OUT/run.log"
REPEATS="${REPEATS:-3}"
QUALIFICATION_CORPUS="${QUALIFICATION_CORPUS:-$SCRIPT_DIR/qualification_corpus.txt}"
RUN_FAILURES=0
cd "$APP" || exit 1
BOOK="Arc 4 - Volume 10wn"
GEMMA="gemma-4-e4b-uncensored-hauhaucs-aggressive"
QWEN9="qwen3.5-9b-uncensored-hauhaucs-aggressive"
QWEN27="qwen3.6-27b-uncensored-hauhaucs-aggressive"
VULKAN="llama.cpp-linux-x86_64-vulkan-avx2@2.26.0"
ROCM="llama.cpp-linux-x86_64-amd-rocm-avx2@2.26.0"

set_model () { "$PY" - "$1" <<'PY'
import json, os, sys
from utils import atomic_json_write, get_app_config_path, get_runtime_data_dir
app = os.getcwd(); root = os.path.dirname(app)
p = get_app_config_path(get_runtime_data_dir(root), root, app)
with open(p, encoding="utf-8") as fh: c = json.load(fh)
if c.get("llm_mode", "local") != "local":
    raise SystemExit("runtime A/B requires llm_mode=local")
base = str((c.get("llm") or {}).get("base_url", ""))
if not any(host in base for host in ("localhost", "127.0.0.1")):
    raise SystemExit(f"runtime A/B requires a local LM Studio base_url, got {base!r}")
c.setdefault("llm", {})["model_name"] = sys.argv[1]
atomic_json_write(c, p)
PY
}

get_model () { "$PY" - <<'PY'
import json, os
from utils import get_app_config_path, get_runtime_data_dir
app = os.getcwd(); root = os.path.dirname(app)
p = get_app_config_path(get_runtime_data_dir(root), root, app)
with open(p, encoding="utf-8") as fh: print((json.load(fh).get("llm") or {}).get("model_name", ""))
PY
}

is_complete () { "$PY" - "$1" "$2" <<'PY'
import json, os, sys
out, model = sys.argv[1:]
manifest = out + ".threepass_manifest.json"
try:
    with open(out, encoding="utf-8") as fh: entries = json.load(fh)
    with open(manifest, encoding="utf-8") as fh: data = json.load(fh)
except (OSError, ValueError):
    raise SystemExit(1)
ok = isinstance(entries, list) and data.get("status") == "complete"
ok = ok and (data.get("fingerprint") or {}).get("model_name") == model
raise SystemExit(0 if ok else 1)
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
    if is_complete "$out" "$model"; then echo "=== [$backend/$tag/rep$r] already complete ===" | tee -a "$MASTER"; continue; fi
    "$LMS" unload --all >/dev/null 2>&1
    echo "=== [$backend/$tag/rep$r] $model start $(date -Is) ===" | tee -a "$MASTER"
    if "$PY" three_pass_generate.py "uploads/$BOOK.txt" --pass2-on-exhaustion fail \
        --output "$out" >> "$dir/$BOOK.log" 2>&1; then
      code=0
    else
      code=$?
      RUN_FAILURES=$((RUN_FAILURES + 1))
    fi
    echo "=== [$backend/$tag/rep$r] exit=$code $(date -Is) ===" | tee -a "$MASTER"
    if [ "$code" -ne 0 ]; then
      echo "=== [$backend/$tag] first incomplete full run; skipping remaining repeats ===" | tee -a "$MASTER"
      break
    fi
  done
}

qualify_model () {  # backend tag model
  local backend="$1" tag="$2" model="$3" dir out code
  set_model "$model"
  dir="$OUT/$backend/$tag/qualification"; mkdir -p "$dir"
  out="$dir/qualification.json"
  if is_complete "$out" "$model"; then return 0; fi
  "$LMS" unload --all >/dev/null 2>&1
  echo "=== [$backend/$tag/qualification] start $(date -Is) ===" | tee -a "$MASTER"
  if "$PY" three_pass_generate.py "$QUALIFICATION_CORPUS" \
      --pass2-on-exhaustion fail --output "$out" >> "$dir/qualification.log" 2>&1; then
    code=0
  else
    code=$?
    RUN_FAILURES=$((RUN_FAILURES + 1))
  fi
  echo "=== [$backend/$tag/qualification] exit=$code $(date -Is) ===" | tee -a "$MASTER"
  return "$code"
}

qualify_then_run () {  # backend tag model
  if qualify_model "$1" "$2" "$3"; then
    run_arm "$1" "$2" "$3"
  else
    echo "=== [$1/$2] qualification failed; skipping full runs ===" | tee -a "$MASTER"
  fi
}

phase () {  # backend engine
  echo "########## PHASE $1 START $(date -Is) ##########" | tee -a "$MASTER"
  select_runtime "$2"
  qualify_then_run "$1" gemma   "$GEMMA"
  qualify_then_run "$1" qwen27b "$QWEN27"
  qualify_then_run "$1" qwen9b  "$QWEN9"
  echo "########## PHASE $1 COMPLETE $(date -Is) ##########" | tee -a "$MASTER"
}

main () {
  ORIGINAL_MODEL="$(get_model)"
  set_model "$ORIGINAL_MODEL"  # preflight effective config/mode before any runtime mutation
  trap 'set_model "$ORIGINAL_MODEL"' EXIT
  echo "########## RUNTIME A/B START $(date -Is) (REPEATS=$REPEATS) ##########" | tee -a "$MASTER"
  phase vulkan "$VULKAN"
  phase rocm   "$ROCM"
  if [ "$RUN_FAILURES" -gt 0 ]; then
    echo "########## RUNTIME A/B INCOMPLETE failures=$RUN_FAILURES $(date -Is) ##########" | tee -a "$MASTER" >&2
    return 1
  fi
  echo "########## RUNTIME A/B COMPLETE $(date -Is) ##########" | tee -a "$MASTER"
  trap - EXIT
  set_model "$ORIGINAL_MODEL"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
