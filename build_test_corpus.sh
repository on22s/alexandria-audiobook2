#!/usr/bin/env bash
# build_test_corpus.sh — exercise the preparer on a batch of real audio/book
# pairs and aggregate this corpus run’s structured annotation summaries.
#
# Why: by the time a single preparer run finishes, you've seen one set of
# stats (cut strategy histogram, source-action histogram, realign events,
# duration percentiles). To actually tune the alignment/threshold knobs
# you need to compare those stats across MANY books. This script:
#
#   1. Discovers audio in the required $AUDIO_DIR.
#   2. For each audio file, uses the batch processor's fuzzy matcher to
#      find its source EPUB/TXT in the required $SOURCE_DIR.
#   3. Runs the preparer on every matched pair (via the auto-resume
#      wrapper run_with_restart.sh so a crash doesn't kill the batch).
#   4. Builds an aggregated report at $OUT_DIR/aggregated_report.md
#      summarising this corpus run’s completed pairs side-by-side and
#      retaining failed attempts explicitly.
#
# Modes:
#   --plan       : show the proposed audio→book pairings, exit (NO actual run)
#   --dry-run    : run ASR and sampled alignment (no LLM), report
#                  estimated alignment quality per pair. Useful for spotting
#                  source/audio divergence before committing to 10+ hours
#                  of LLM time.
#   --run        : actually run the preparer end-to-end on every pair.
#   --aggregate  : skip running, just rebuild the aggregated report from
#                  the owned corpus attempt index. Use after a long run completes.
#
# Examples:
#   ./build_test_corpus.sh --plan
#   ./build_test_corpus.sh --dry-run
#   ./build_test_corpus.sh --run
#   ./build_test_corpus.sh --aggregate
#
# Environment overrides:
#   AUDIO_DIR   required audiobook directory
#   SOURCE_DIR  required source-book directory
#   MODEL       default: models/Qwen2.5-14B-Instruct-Q6_K.gguf
#   FALLBACK    default: models/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
#   OUT_DIR     default: ./test_corpus_output/

set -u
shopt -s -o pipefail

# Project-root model paths (where the GGUF files actually live, not models/).
MODEL=${MODEL:-"Qwen2.5-14B-Instruct-Q6_K.gguf"}
FALLBACK=${FALLBACK:-"Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf"}
OUT_DIR=${OUT_DIR:-"./test_corpus_output/"}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
PYTHON="$SCRIPT_DIR/app/env/bin/python"

MODE="${1:-}"
case "$MODE" in
    --plan|--dry-run|--run)
    : "${AUDIO_DIR:?Set AUDIO_DIR to the audiobook directory}"
    : "${SOURCE_DIR:?Set SOURCE_DIR to the source-book directory}"
        ;;
    --aggregate) ;;
    -h|--help|"")
        sed -n '2,32p' "$0"
        exit 0
        ;;
    *)
        echo "Unknown mode: $MODE" >&2
        echo "Run with --help to see modes." >&2
        exit 2
        ;;
esac

mkdir -p "$OUT_DIR"

if [[ "$MODE" != "--aggregate" ]]; then

# ── Discover audio + pair to source ────────────────────────────────────────
echo "Audio dir : $AUDIO_DIR"
echo "Source dir: $SOURCE_DIR"
echo "Out dir   : $OUT_DIR"
echo ""

# Use the batch processor's _find_source_for() so this script's pairings
# are guaranteed to match what an actual --source-folder batch would do.
python_pair_script=$(cat <<'PYEOF'
import os, sys, re, json, ast, hashlib
sys.path.insert(0, sys.argv[1])
src_code = open(os.path.join(sys.argv[1], 'alexandria_batch_processor.py')).read()
ns = {'os': os, 're': re}
from pathlib import Path
ns['Path'] = Path
start = src_code.index('# Filename noise tokens')
end   = src_code.index('def check_disk_space')
exec(src_code[start:end], ns)
find = ns['_find_source_for']
# Reuse the batch processor's single output-name decision without importing
# its logger or GPU dependencies during pair discovery.
output_function = next(node for node in ast.parse(src_code).body
                       if isinstance(node, ast.FunctionDef) and node.name == 'get_output_name')
ns['hashlib'] = hashlib
exec(compile(ast.Module(body=[output_function], type_ignores=[]), '<batch-output-name>', 'exec'), ns)

audio_dir, source_dir = sys.argv[2], sys.argv[3]
audio_exts = {'.wav', '.mp3', '.m4a', '.flac', '.ogg'}
pairs = []
for entry in sorted(os.scandir(audio_dir), key=lambda e: e.name):
    if not entry.is_file():
        continue
    if Path(entry.name).suffix.lower() not in audio_exts:
        continue
    matched = find(entry.path, source_dir)
    pairs.append({'audio': entry.path, 'source': matched,
                  'output_name': ns['get_output_name'](entry.path)})
print(json.dumps(pairs, indent=2))
PYEOF
)

PAIRS_JSON="$OUT_DIR/pairs.json"
"$PYTHON" -c "$python_pair_script" "$SCRIPT_DIR" "$AUDIO_DIR" "$SOURCE_DIR" > "$PAIRS_JSON"

# Pretty-print the proposed pairings
echo "─────────────────────────────────────────────────────────────────"
echo "Audio → Source pairings (via fuzzy matcher):"
echo "─────────────────────────────────────────────────────────────────"
"$PYTHON" - "$PAIRS_JSON" <<'PYEOF'
import sys
import json
pairs = json.load(open(sys.argv[1]))
matched = [p for p in pairs if p['source']]
missed  = [p for p in pairs if not p['source']]
print(f'  {len(matched)} matched, {len(missed)} no-match (legacy ASR-only)')
print()
for p in matched:
    from pathlib import Path
    print(f'  ✓ {Path(p["audio"]).stem!r:60} → {Path(p["source"]).name!r}')
for p in missed:
    from pathlib import Path
    print(f'  ✗ {Path(p["audio"]).stem!r:60} → (no source match)')
PYEOF
echo "─────────────────────────────────────────────────────────────────"

if [[ "$MODE" == "--plan" ]]; then
    echo ""
    echo "Plan mode — exiting without running anything."
    echo "Re-run with --dry-run for ASR and sampled alignment (no LLM),"
    echo "or --run to run the full preparer batch."
    exit 0
fi


# ── Dry-run: ASR and sampled alignment only, through the locked wrapper ──
if [[ "$MODE" == "--dry-run" ]]; then
    "$PYTHON" "$SCRIPT_DIR/corpus_alignment_prescan.py" \
        --repo "$SCRIPT_DIR" --pairs "$PAIRS_JSON" --output-dir "$OUT_DIR"
    exit $?
fi

fi


# ── Owned structured run/aggregation ───────────────────────────────────────
if [[ "$MODE" == "--run" ]]; then
    "$PYTHON" "$SCRIPT_DIR/corpus_run_report.py" --repo "$SCRIPT_DIR" \
        --output-dir "$OUT_DIR" --pairs "$PAIRS_JSON" --model "$MODEL" --fallback "$FALLBACK"
else
    "$PYTHON" "$SCRIPT_DIR/corpus_run_report.py" --repo "$SCRIPT_DIR" --output-dir "$OUT_DIR"
fi
status=$?
if [[ "$status" != "0" ]]; then
    exit "$status"
fi
echo "Done. Report: $OUT_DIR/aggregated_report.md"
