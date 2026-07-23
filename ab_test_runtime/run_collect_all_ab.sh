#!/usr/bin/env bash
# Eight-book, all-local-model diagnostic A/B. Expected model failures are
# collected by three_pass_generate.py instead of stopping the remaining book.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/app"
PY="$APP/env/bin/python"
LMS="/home/fakemitch/.lmstudio/bin/lms"
STAMP="${STAMP:-$(date +%Y%m%d-%H%M%S)}"
OUT="${OUT:-$ROOT/ab_test_runtime/results/collect_all_$STAMP}"
DRY_RUN="${DRY_RUN:-0}"

MODELS=(
  gemma-4-e4b-uncensored-hauhaucs-aggressive-q8_k_p.gguf
  qwen3.5-9b-uncensored-hauhaucs-aggressive
  ministral-3-14b-instruct-2512
  ministral-3-14b-instruct-2512-absolute-heresy-i1
  gemma-4-12b-coder-fable5-composer2.5-v1
  qwen3.6-27b-uncensored-hauhaucs-aggressive
)

BOOK_TAGS=(mushoku16 grimgar03 mushoku18 grimgar06 index18 owarimonogatari3 mushoku23 arc4_volume10wn)
BOOK_PATHS=(
  "/run/media/fakemitch/HHD2/old computer/audio book folder/already moved light novels/Mushoku Tensei/Mushoku Tensei - Baka-Tsuki Volume 16.epub"
  "/run/media/fakemitch/HHD2/old computer/audio book folder/already moved light novels/Hai to Gensou no Grimgar (grimgar of fantasy and ash)/epubs/Hai to Gensou no Grimgar - Volume 03 - You Have to Accept That Things Won't Always Go Your Way.epub"
  "/run/media/fakemitch/HHD2/old computer/audio book folder/already moved light novels/Mushoku Tensei/Mushoku Tensei - Baka-Tsuki Volume 18.epub"
  "/run/media/fakemitch/HHD2/old computer/audio book folder/already moved light novels/Hai to Gensou no Grimgar (grimgar of fantasy and ash)/epubs/Hai to Gensou no Grimgar - Volume 06 - Towards a Glory Not Worth Taking.epub"
  "/run/media/fakemitch/HHD2/old computer/audio book folder/Light Novels/A Certain Magical Index/A Certain Magical Index - Volume 18/A Certain Magical Index - Volume 18.txt"
  "/run/media/fakemitch/HHD2/old computer/Downloads/Nisio Issin, Illustrated by VOFAN/Owarimonogatri Part 3 (11)/Owarimonogatri Part 3 - Nisio Issin, Illustrated by VOFAN.epub"
  "/run/media/fakemitch/HHD2/old computer/audio book folder/already moved light novels/Mushoku Tensei/Mushoku Tensei - Baka-Tsuki Volume 23.epub"
  "$APP/uploads/Arc 4 - Volume 10wn.txt"
)

get_model () {
  cd "$APP" && "$PY" - <<'PY'
import json, os
from utils import get_app_config_path, get_runtime_data_dir
app=os.getcwd(); root=os.path.dirname(app)
p=get_app_config_path(get_runtime_data_dir(root), root, app)
print((json.load(open(p)).get("llm") or {}).get("model_name", ""))
PY
}

set_model () {
  cd "$APP" && "$PY" - "$1" <<'PY'
import json, os, sys
from utils import atomic_json_write, get_app_config_path, get_runtime_data_dir
app=os.getcwd(); root=os.path.dirname(app)
p=get_app_config_path(get_runtime_data_dir(root), root, app)
c=json.load(open(p)); c.setdefault("llm", {})["model_name"]=sys.argv[1]
atomic_json_write(c, p)
PY
}

prepare_book () {
  (cd "$APP" && "$PY" - "$1" "$2") <<'PY'
from pathlib import Path
import sys
from routers.script import extract_epub_text
source, output = map(Path, sys.argv[1:])
text = extract_epub_text(str(source)) if source.suffix.lower() == ".epub" else source.read_text(encoding="utf-8", errors="replace")
output.write_text(text, encoding="utf-8")
PY
}

ORIGINAL_MODEL="$(get_model)"
trap 'set_model "$ORIGINAL_MODEL"' EXIT
mkdir -p "$OUT/inputs"

for i in "${!BOOK_TAGS[@]}"; do
  if [ ! -f "${BOOK_PATHS[$i]}" ]; then
    echo "ERROR missing book: ${BOOK_PATHS[$i]}" >&2
    exit 1
  fi
done

if [ "$DRY_RUN" = "1" ]; then
  printf 'output=%s\nmodels=%s\nbooks=%s\n' "$OUT" "${#MODELS[@]}" "${#BOOK_TAGS[@]}"
  printf 'model: %s\n' "${MODELS[@]}"
  printf 'book: %s\n' "${BOOK_TAGS[@]}"
  exit 0
fi

for i in "${!BOOK_TAGS[@]}"; do
  prepare_book "${BOOK_PATHS[$i]}" "$OUT/inputs/${BOOK_TAGS[$i]}.txt"
done

for model in "${MODELS[@]}"; do
  set_model "$model"
  for tag in "${BOOK_TAGS[@]}"; do
    dir="$OUT/$model/$tag"
    mkdir -p "$dir"
    "$LMS" unload --all >/dev/null 2>&1 || true
    echo "=== $model / $tag start $(date -Is) ===" | tee -a "$OUT/run.log"
    (cd "$APP" && "$PY" three_pass_generate.py "$OUT/inputs/$tag.txt" \
      --pass2-on-exhaustion fail --collect-all-failures \
      --output "$dir/result.json") >> "$dir/run.log" 2>&1
    code=$?
    echo "=== $model / $tag exit=$code $(date -Is) ===" | tee -a "$OUT/run.log"
  done
done
