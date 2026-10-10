#!/usr/bin/bash
# A SECOND English reference set, which is what goal 2.9 actually needs.
#
# 2.9 asks whether English agreeing with its reference far worse than either
# CJK language - 0.29-0.34 f0 correlation against 0.72-0.74, roughly three
# times the gross pitch error - is "a real weakness of that arm or an artifact
# of that eval set". Re-running at n=150 settled that the deficit is stable and
# that the ARM difference is small, and settled nothing about the question:
# every English number in the document comes from LJSpeech, and more draws from
# the same recordings cannot separate those two explanations.
#
# EIGHT DIFFERENT ENGLISH SPEAKERS, from the user's own audiobooks, each with a
# held-out val split and its own trained adapter. Different voices, books and
# recording conditions. If English still scores near 0.3 across all eight, the
# eval set is exonerated and the finding belongs to the arm; if it does not,
# LJSpeech was the problem and every English figure in 2.9 needs re-reading.
#
# The eight span the passing identity range (0.555 to 0.781), not the top
# eight: a highlight reel of the best adapters would answer a different
# question. Twenty lines each, so no single voice carries the result.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/second_english_eval_20260820"
work="$runtime/second_english_eval"
mkdir -p "$STAGE_LOG_DIR" "$work"
source "$REPO/run_chains/lib/stage.sh"
source "$REPO/run_chains/lib/queue.sh"

selection="$(mktemp "$work/.voice-selection.XXXXXX")" || exit 1
trap 'rm -f "$selection"' EXIT
"$python" - "$runtime/second_english_voices.txt" > "$selection" <<'PY_SELECTION'
import math
from pathlib import Path
import sys

rows = []
names, adapters = set(), set()
for line_number, raw in enumerate(Path(sys.argv[1]).read_text(encoding="utf-8").splitlines(), 1):
    if not raw.strip():
        continue
    fields = raw.split("|")
    if len(fields) != 3 or any(not field.strip() for field in fields):
        raise SystemExit(f"REFUSING: voice selection line {line_number} needs name|adapter|score")
    name, adapter, score = fields
    try:
        value = float(score)
    except ValueError:
        raise SystemExit(f"REFUSING: invalid identity score on line {line_number}: {score!r}")
    if not math.isfinite(value) or not -1 <= value <= 1:
        raise SystemExit(f"REFUSING: identity score outside finite cosine range on line {line_number}")
    if name in names or adapter in adapters:
        raise SystemExit(f"REFUSING: duplicate voice or adapter on line {line_number}")
    names.add(name)
    adapters.add(adapter)
    rows.append(raw)
if not rows:
    raise SystemExit("REFUSING: voice selection is empty")
print("\n".join(rows))
PY_SELECTION
selection_rc=$?
[ "$selection_rc" -eq 0 ] || exit "$selection_rc"

running() { chain_running "$1"; }
for chain in overnight_20260820b.sh settle_2_6_20260820.sh; do
    stage_note "waiting for $chain"
    while running "$chain"; do sleep 120; done
done
stage_note "queue clear; building the second English set"

while IFS='|' read -r name adapter_rel score; do
    stage_note "selected $name (identity score $score)"
    data="$REPO/$adapter_rel/data"
    build="$work/${name}_build.json"

    "$python" -u "$REPO/app/experiments/library_eval_build.py" \
        --dataset "$data" --out "$build" \
        >> "$STAGE_LOG_DIR/build.log" 2>&1
    build_rc=$?
    STAGE_TOTAL=$((STAGE_TOTAL + 1))
    record_stage_result "build_$name" "$build_rc"
    if [ "$build_rc" -ne 0 ]; then
        stage_note "build failed: $name"
        continue
    fi

    run_stage "gen_$name" 2h --needs-vram -- \
        "$REPO/gpu_job.sh" "eng2_$name" \
        "$python" -u "$REPO/app/experiments/ljspeech_generate.py" \
        --build "$build" --adapter "$REPO/$adapter_rel/adapter" \
        --out-dir "$work/$name" --limit 0 --arms lora clone \
        --out "$runtime/experiments/second_english__${name}_generate.json"

done < "$selection"

# Finish all GPU generation attempts before CPU prosody scoring.
while IFS='|' read -r name adapter_rel score; do
    run_stage "prosody_$name" 1h --requires-ok "gen_$name" -- \
        "$python" -u "$REPO/app/experiments/prosody_fidelity.py" \
        --generated "$runtime/experiments/second_english__${name}_generate.json" \
        --limit 0 \
        --out "$runtime/experiments/prosody_second_english__${name}.json"
    if ! is_stage_successful "prosody_$name"; then
        continue
    fi
    stage_commit_artifacts "second_english_$name" "$REPO" \
        "$runtime/experiments/second_english__${name}_generate.json" \
        "$runtime/experiments/prosody_second_english__${name}.json"
done < "$selection"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary second_english_eval_20260820
