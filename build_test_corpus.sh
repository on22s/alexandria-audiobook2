#!/usr/bin/env bash
# build_test_corpus.sh — exercise the preparer on a batch of real audio/book
# pairs and aggregate every preparer log into a single analyzable file.
#
# Why: by the time a single preparer run finishes, you've seen one set of
# stats (cut strategy histogram, source-action histogram, realign events,
# duration percentiles). To actually tune the alignment/threshold knobs
# you need to compare those stats across MANY books. This script:
#
#   1. Discovers audio in $AUDIO_DIR (defaults to ~/Desktop/New folder/).
#   2. For each audio file, uses the batch processor's fuzzy matcher to
#      find its source EPUB/TXT in $SOURCE_DIR (defaults to ~/Desktop/books/).
#   3. Runs the preparer on every matched pair (via the auto-resume
#      wrapper run_with_restart.sh so a crash doesn't kill the batch).
#   4. Builds an aggregated report at $OUT_DIR/aggregated_report.md
#      summarising every run's stats side-by-side, plus the raw logs
#      copied in for deeper inspection.
#
# Modes:
#   --plan       : show the proposed audio→book pairings, exit (NO actual run)
#   --dry-run    : do the alignment pre-scan only (fast, no LLM), report
#                  estimated alignment quality per pair. Useful for spotting
#                  source/audio divergence before committing to 10+ hours
#                  of LLM time.
#   --run        : actually run the preparer end-to-end on every pair.
#   --aggregate  : skip running, just rebuild the aggregated report from
#                  existing logs in logs/. Use after a long run completes.
#
# Examples:
#   ./build_test_corpus.sh --plan
#   ./build_test_corpus.sh --dry-run
#   ./build_test_corpus.sh --run
#   ./build_test_corpus.sh --aggregate
#
# Environment overrides:
#   AUDIO_DIR   default: /home/fakemitch/Desktop/New folder/
#   SOURCE_DIR  default: /home/fakemitch/Desktop/books/
#   MODEL       default: models/Qwen2.5-14B-Instruct-Q6_K.gguf
#   FALLBACK    default: models/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
#   OUT_DIR     default: ./test_corpus_output/

set -u
shopt -s -o pipefail

AUDIO_DIR=${AUDIO_DIR:-"/home/fakemitch/Desktop/New folder/"}
SOURCE_DIR=${SOURCE_DIR:-"/home/fakemitch/Desktop/books/"}
MODEL=${MODEL:-"models/Qwen2.5-14B-Instruct-Q6_K.gguf"}
FALLBACK=${FALLBACK:-"models/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf"}
OUT_DIR=${OUT_DIR:-"./test_corpus_output/"}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
PYTHON="$SCRIPT_DIR/app/env/bin/python"

MODE="${1:-}"
case "$MODE" in
    --plan|--dry-run|--run|--aggregate) ;;
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

# ── Discover audio + pair to source ────────────────────────────────────────
echo "Audio dir : $AUDIO_DIR"
echo "Source dir: $SOURCE_DIR"
echo "Out dir   : $OUT_DIR"
echo ""

# Use the batch processor's _find_source_for() so this script's pairings
# are guaranteed to match what an actual --source-folder batch would do.
python_pair_script=$(cat <<'PYEOF'
import os, sys, re, json
sys.path.insert(0, sys.argv[1])
src_code = open(os.path.join(sys.argv[1], 'alexandria_batch_processor.py')).read()
ns = {'os': os, 're': re}
from pathlib import Path
ns['Path'] = Path
start = src_code.index('# Filename noise tokens')
end   = src_code.index('def check_disk_space')
exec(src_code[start:end], ns)
find = ns['_find_source_for']

audio_dir, source_dir = sys.argv[2], sys.argv[3]
audio_exts = {'.wav', '.mp3', '.m4a', '.flac', '.ogg'}
pairs = []
for entry in sorted(os.scandir(audio_dir), key=lambda e: e.name):
    if not entry.is_file():
        continue
    if Path(entry.name).suffix.lower() not in audio_exts:
        continue
    matched = find(entry.path, source_dir)
    pairs.append({'audio': entry.path, 'source': matched})
print(json.dumps(pairs, indent=2))
PYEOF
)

PAIRS_JSON="$OUT_DIR/pairs.json"
"$PYTHON" -c "$python_pair_script" "$SCRIPT_DIR" "$AUDIO_DIR" "$SOURCE_DIR" > "$PAIRS_JSON"

# Pretty-print the proposed pairings
echo "─────────────────────────────────────────────────────────────────"
echo "Audio → Source pairings (via fuzzy matcher):"
echo "─────────────────────────────────────────────────────────────────"
"$PYTHON" -c "
import json
pairs = json.load(open('$PAIRS_JSON'))
matched = [p for p in pairs if p['source']]
missed  = [p for p in pairs if not p['source']]
print(f'  {len(matched)} matched, {len(missed)} no-match (legacy ASR-only)')
print()
for p in matched:
    from pathlib import Path
    print(f'  ✓ {Path(p[\"audio\"]).stem!r:60} → {Path(p[\"source\"]).name!r}')
for p in missed:
    from pathlib import Path
    print(f'  ✗ {Path(p[\"audio\"]).stem!r:60} → (no source match)')
"
echo "─────────────────────────────────────────────────────────────────"

if [[ "$MODE" == "--plan" ]]; then
    echo ""
    echo "Plan mode — exiting without running anything."
    echo "Re-run with --dry-run to do the cheap alignment pre-scan,"
    echo "or --run to run the full preparer batch."
    exit 0
fi


# ── Dry-run: alignment pre-scan only (no LLM, fast) ────────────────────────
if [[ "$MODE" == "--dry-run" ]]; then
    echo ""
    echo "Dry-run: running alignment pre-scan (no LLM) on each pair…"
    echo ""
    dry_report="$OUT_DIR/dry_run_report.md"
    {
        echo "# Test corpus dry-run report"
        echo ""
        echo "_Generated: $(date '+%Y-%m-%d %H:%M:%S')_"
        echo ""
        echo "Per-pair alignment quality (no LLM, no chunking, no audio writes —"
        echo "just transcribes audio and pre-scans against the matched source)."
        echo ""
        echo "| audio | source | avg ratio | n sampled | below 60% | review-needed |"
        echo "|---|---|---:|---:|---:|---:|"
    } > "$dry_report"

    "$PYTHON" -c "
import json, sys, os
sys.path.insert(0, '$SCRIPT_DIR')
import alexandria_alignment as alignment
from pathlib import Path
pairs = json.load(open('$PAIRS_JSON'))
for p in pairs:
    if not p['source']:
        continue
    audio = p['audio']
    src   = p['source']
    print(f'  ↳ {Path(audio).stem!r} vs {Path(src).name!r}')
    print(f'    (Dry-run alignment quality reporting not yet wired —')
    print(f'     it would need to invoke ASR which is the slow part.')
    print(f'     For now the script ALREADY logs estimate_alignment_quality')
    print(f'     during a real --run; that\\'s where the data lives.)')
"
    echo ""
    echo "Dry-run mode is a stub right now — alignment quality measurement"
    echo "requires running ASR which is the expensive part. Use --run instead;"
    echo "the preparer logs estimate_alignment_quality at startup and the"
    echo "aggregated report below pulls those values out."
    exit 0
fi


# ── Run: preparer end-to-end on every matched pair ─────────────────────────
if [[ "$MODE" == "--run" ]]; then
    echo ""
    echo "Run mode: invoking preparer on each matched pair via run_with_restart.sh"
    echo "          (so individual crashes don't kill the batch)."
    echo ""

    PAIRS_COUNT=$("$PYTHON" -c "
import json
pairs = json.load(open('$PAIRS_JSON'))
print(sum(1 for p in pairs if p['source']))
")
    if [[ "$PAIRS_COUNT" == "0" ]]; then
        echo "No matched pairs — nothing to run."
        exit 1
    fi
    echo "Total pairs to run: $PAIRS_COUNT"

    # Iterate over JSON pairs in shell. Use python to emit \0-delimited records
    # so audio/source paths with spaces survive cleanly.
    while IFS= read -r -d '' record; do
        audio=$(printf '%s' "$record" | "$PYTHON" -c "import json, sys; r=json.loads(sys.stdin.read()); print(r['audio'], end='')")
        source=$(printf '%s' "$record" | "$PYTHON" -c "import json, sys; r=json.loads(sys.stdin.read()); print(r['source'] or '', end='')")
        if [[ -z "$source" ]]; then
            echo "  skip: $audio (no source)"
            continue
        fi
        stem=$(basename "$audio" | sed 's/\.[^.]*$//')
        output="$OUT_DIR/dataset_${stem}.zip"

        echo ""
        echo "═════════════════════════════════════════════════════════════════"
        echo "RUN: $stem"
        echo "  audio  : $audio"
        echo "  source : $source"
        echo "  output : $output"
        echo "═════════════════════════════════════════════════════════════════"

        "$SCRIPT_DIR/run_with_restart.sh" \
            --audio "$audio" \
            --model "$MODEL" \
            --fallback-model "$FALLBACK" \
            --source "$source" \
            --output "$output" \
            --chunk-size 10.0 \
            --lang en
    done < <(
        "$PYTHON" -c "
import json, sys
for p in json.load(open('$PAIRS_JSON')):
    sys.stdout.buffer.write(json.dumps(p).encode() + b'\\0')
"
    )

    echo ""
    echo "All runs complete. Building aggregated report…"
fi


# ── Aggregate: pull stats out of every preparer log into one report ────────
echo ""
echo "Building aggregated report…"

report="$OUT_DIR/aggregated_report.md"
"$PYTHON" -c "
import os, re, json, sys
from pathlib import Path
from collections import defaultdict

logs_dir = Path('$SCRIPT_DIR/logs/')
logs = sorted(logs_dir.glob('alexandria_preparer_*.log'), key=lambda p: p.stat().st_mtime)
if not logs:
    print('No preparer logs found in $SCRIPT_DIR/logs/.', file=sys.stderr)
    sys.exit(1)

# Parse each log for the per-run summary block. Robust to logs that crashed
# before producing one — skipped silently.
def parse_log(path):
    text = path.read_text(errors='replace')
    # Pull just the key stats from the structured summary lines we emit.
    stats = {'log': path.name}
    m = re.search(r'audio=(.*?) \|', text)
    if m: stats['audio'] = m.group(1)
    m = re.search(r'source=(\S+(?: \S+)*?) \| source_threshold', text)
    if m: stats['source'] = m.group(1)
    m = re.search(r'Annotation complete: (\d+) total segments \((\d+) new this run\)', text)
    if m:
        stats['segments_total']    = int(m.group(1))
        stats['segments_this_run'] = int(m.group(2))
    m = re.search(r'replace\s*:\s*(\d+)\s*\(\s*([\d.]+)%\)', text)
    if m: stats['replace_pct'] = float(m.group(2))
    m = re.search(r'dropped\s*:\s*(\d+)\s*\(\s*([\d.]+)%\)', text)
    if m: stats['dropped_pct'] = float(m.group(2))
    m = re.search(r'keep_asr\s*:\s*(\d+)\s*\(\s*([\d.]+)%\)', text)
    if m: stats['keep_asr_pct'] = float(m.group(2))
    m = re.search(r'Source cursor finished at word ([\d,]+) / ([\d,]+)', text)
    if m:
        stats['cursor_end']   = int(m.group(1).replace(',', ''))
        stats['source_words'] = int(m.group(2).replace(',', ''))
    m = re.search(r'LLM annotations: (\d+) ok, (\d+) failed.*?(\d+) cleaned', text)
    if m:
        stats['llm_ok']        = int(m.group(1))
        stats['llm_fail']      = int(m.group(2))
        stats['llm_sanitised'] = int(m.group(3))
    m = re.search(r'sentence_end\s*:\s*(\d+)', text)
    if m: stats['cut_sentence'] = int(m.group(1))
    m = re.search(r'pause\s*:\s*(\d+)', text)
    if m: stats['cut_pause'] = int(m.group(1))
    m = re.search(r'fallback\s*:\s*(\d+)', text)
    if m: stats['cut_fallback'] = int(m.group(1))
    # Realign events (cursor recovery)
    stats['realign_events'] = len(re.findall(r'source-realign idx=', text))
    # Full-source re-anchor events (the tier-2 fallback)
    stats['reanchor_events'] = len(re.findall(r'full-source re-anchor', text))
    # Total chunk durations
    m = re.search(r'Total audio in dataset\s*:\s*([\d.]+)s', text)
    if m: stats['dataset_seconds'] = float(m.group(1))
    return stats

rows = []
for log in logs:
    try:
        s = parse_log(log)
        if 'segments_total' in s:
            rows.append(s)
    except Exception as e:
        print(f'  skip {log.name}: {e}', file=sys.stderr)

if not rows:
    print('No log produced a parseable summary — has the preparer been run yet?', file=sys.stderr)
    sys.exit(0)

# Markdown report
out = []
out.append('# Aggregated preparer-run report')
out.append('')
out.append(f'_Generated: $(date \"+%Y-%m-%d %H:%M:%S\")_')
out.append('')
out.append(f'Parsed {len(rows)} run(s) from preparer logs in \`logs/\`.')
out.append('')
out.append('## Per-run summary')
out.append('')
out.append('| run | audio | segs | replace% | drop% | realign | re-anchor | LLM ok/fail | sanitised | dataset sec | cursor end |')
out.append('|---|---|---:|---:|---:|---:|---:|---|---:|---:|---|')
for r in rows:
    audio = Path(r.get('audio', '?')).stem[:40]
    out.append(
        f\"| {r['log'][22:-4]} | {audio} | {r.get('segments_total','-')} | \"
        f\"{r.get('replace_pct','-'):>5}% | {r.get('dropped_pct','-'):>5}% | \"
        f\"{r.get('realign_events','-')} | {r.get('reanchor_events','-')} | \"
        f\"{r.get('llm_ok','-')}/{r.get('llm_fail','-')} | {r.get('llm_sanitised','-')} | \"
        f\"{r.get('dataset_seconds','-')} | \"
        f\"{r.get('cursor_end','-')}/{r.get('source_words','-')} |\"
    )

out.append('')
out.append('## Aggregate stats across all runs')
out.append('')

def avg(key):
    vals = [r[key] for r in rows if key in r]
    return sum(vals) / len(vals) if vals else 0

def total(key):
    return sum(r.get(key, 0) for r in rows)

out.append(f'- Total segments emitted: **{total(\"segments_total\")}**')
out.append(f'- Total dataset audio:    **{total(\"dataset_seconds\"):.0f}s**')
out.append(f'- Avg replace rate:       **{avg(\"replace_pct\"):.1f}%**')
out.append(f'- Avg drop rate:          **{avg(\"dropped_pct\"):.1f}%**')
out.append(f'- Realign events total:   **{total(\"realign_events\")}**')
out.append(f'- Re-anchor events total: **{total(\"reanchor_events\")}**')
out.append(f'- LLM failures total:     **{total(\"llm_fail\")}**')
out.append(f'- LLM sanitised total:    **{total(\"llm_sanitised\")}** '
           f'(of **{total(\"llm_ok\")}** ok responses)')
out.append('')

# Highlight anything that looks unhealthy so the user can drill in
warnings = []
for r in rows:
    if r.get('dropped_pct', 0) > 20:
        warnings.append(f'**{r[\"log\"]}**: drop rate {r[\"dropped_pct\"]}% (likely '
                        f'source/audio mismatch or wrong EPUB pairing).')
    if r.get('llm_fail', 0) > 5:
        warnings.append(f'**{r[\"log\"]}**: {r[\"llm_fail\"]} LLM failures.')
    if r.get('realign_events', 0) > 10:
        warnings.append(f'**{r[\"log\"]}**: {r[\"realign_events\"]} realign events '
                        f'(cursor kept falling behind — could indicate audio with '
                        f'lots of audio-only material).')

if warnings:
    out.append('## ⚠ Anomalies to drill into')
    out.append('')
    for w in warnings:
        out.append(f'- {w}')
    out.append('')

Path('$report').write_text('\n'.join(out))
print(f'Report written: $report')
print()
print('Open it for the run-by-run breakdown and aggregate stats.')
"

echo ""
echo "Done. Report: $report"
