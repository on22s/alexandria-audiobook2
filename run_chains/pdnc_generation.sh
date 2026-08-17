#!/bin/bash
# Generate public-domain novels: the first non-light-novel text this pipeline
# has ever been asked to produce a script from.
#
# WHY THIS MATTERS MORE THAN MORE LIGHT NOVELS. Goal 1.3 records that every
# method finding in this project rests on four Japanese light novels in
# translation, and that the app scores 71.0% on 25 unseen PDNC novels against
# 83.6% on the three it quotes. Those were ATTRIBUTION scores against gold
# rows - the generation pipeline itself has never run on this prose at all.
#
# The static checks already found something: measuring quote balance per
# physical line scored all 28 novels 34.5-84.3% "unbalanced", because
# public-domain text is hard-wrapped and one sentence spans several lines.
# Emma read 84.3% with no defect. That metric was calibrated on light novels,
# which happen to put one paragraph per line, and it was wrong three times
# before this corpus exposed it. Fixed to measure paragraphs; PDNC now reads
# 0.0-15.1%, median 0.2%.
#
# The gates themselves are clean on all 28: none would be refused, none carry
# a replacement character, and publisher matter is stripped from only 2. So
# this run asks the remaining question - does GENERATION work on English
# classics, or is it shaped around light-novel conventions in ways no static
# check reveals?
#
# ORDER. Smallest first for fastest signal. Two of the four have gold
# annotations (TheSignOfTheFour, TheAwakening), so if generation succeeds they
# can be scored against goal 1.3 rather than only counted for completion.
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git

# HOLD THE REAL LOCK INSTEAD OF GUESSING WHO IS RUNNING. The poll loop this
# replaces enumerated script names, so a GPU job nobody listed was invisible -
# `measure_respellings.py` held the card for seventeen hours on 2026-08-17 and
# appears in none of those lists. It also raced between "nothing is running"
# and "start mine". gpu_job.sh's flock does not care what the other job is
# called. Same idiom as run_chains/pdnc_context_evidence.sh; the sentinel is
# exported by gpu_job.sh, so this is safe when nested.
if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
    exec "$REPO/gpu_job.sh" "pdnc_generation" \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
fi
L="$REPO/ab_test_runtime/logs"
PY="$REPO/app/env/bin/python"
IN="$REPO/ab_test_runtime/pdnc_inputs"
OUT="$REPO/ab_test_runtime/pdnc_generated"
BACKUP="$L/config.json.pdnc_backup"
mkdir -p "$OUT"
cd "$REPO/app"

restore() { [ -f "$BACKUP" ] && command cp -f "$BACKUP" "$REPO/app/config.json"; }
trap restore EXIT INT TERM


if ! curl -s -m 20 http://127.0.0.1:8090/v1/models | grep -q qwen3; then
    echo "ABORT: no qwen3 server on 8090"; exit 1
fi
command cp -f "$REPO/app/config.json" "$BACKUP"
"$PY" - <<'PYEOF'
import json
p = "/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/config.json"
d = json.load(open(p, encoding="utf-8"))
for key in ("llm", "llm_local"):
    if isinstance(d.get(key), dict):
        d[key]["model_name"] = "qwen3-14b"
json.dump(d, open(p, "w", encoding="utf-8"), indent=2)
print("config -> qwen3-14b")
PYEOF

for book in DaisyMiller TheSignOfTheFour AlicesAdventuresInWonderland TheAwakening; do
    echo ""
    echo "=== $book  $(date -u +%FT%TZ) ==="
    timeout 43200 "$PY" -u generate_script.py "$IN/$book.txt" \
        --output "$OUT/$book.json" > "$L/pdnc_$book.log" 2>&1
    echo "  rc=$?"
    grep -E "Stripped publisher|Split into|WARNING: source" "$L/pdnc_$book.log" | sed 's/^/  /' | cut -c1-92
    echo "  chunks done : $(grep -c 'Got .* entries' "$L/pdnc_$book.log")"
    echo "  retries     : $(grep -c 'failed quality validation' "$L/pdnc_$book.log")"
    echo "  ceiling hits: $(grep -c 'completion=16384' "$L/pdnc_$book.log")"
    echo "  written     : $([ -f "$OUT/$book.json" ] && echo yes || echo NO)"
    grep -E "^Error" "$L/pdnc_$book.log" | tail -2 | sed 's/^/  /' | cut -c1-92
done
echo ""
echo "PDNC GENERATION DONE $(date -u +%FT%TZ)"
