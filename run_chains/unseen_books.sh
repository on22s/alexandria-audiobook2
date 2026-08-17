#!/bin/bash
# Four books the app has never generated, against the current pipeline.
#
# WHY THESE. Goal 3.1's claim rests on four books; the corpus holds eight, and
# the other four have never been generated once. index18 showed what an unseen
# book does to this pipeline - five distinct blockers, none of which any
# previously-tested book had exposed - so these are where the next ones live.
#
# ORDER IS SMALLEST FIRST, deliberately. A blocker found on mushoku18 in three
# hours is worth more than the same blocker found on arc4_volume10wn in eight,
# and each book's result informs whether the next is worth starting.
#
# WHAT EACH IS FOR:
#   mushoku18        20.3% quote imbalance, the highest in the corpus
#   grimgar06        25 paragraphs of publisher matter, same series as
#                    grimgar03 so the comparison is close
#   mushoku23        large, 829 KB
#   arc4_volume10wn  the fan-compiler "wn" format strip_known_front_matter was
#                    written for, never exercised on the current single-pass path
#
# COMPLETION ONLY. None has a gold fixture, so accuracy cannot be scored. That
# is fine: 3.1 measures whether chunks finish, and a book that cannot be
# generated cannot be scored either way.
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git

# HOLD THE REAL LOCK INSTEAD OF GUESSING WHO IS RUNNING.
#
# This used to poll: `while pgrep -f "three_pass_generate.py" ...; do sleep;
# done`. Three faults, and the third is the one that bit:
#
#   1. It races. Between "nothing is running" and "start mine", something else
#      can start - which is the concurrency this is meant to prevent.
#   2. `pgrep -f` matches any command line CONTAINING the string, including the
#      shell that ran it.
#   3. IT ENUMERATES SCRIPT NAMES, so a GPU job nobody listed is invisible. On
#      2026-08-17 `measure_respellings.py` held the card for seventeen hours and
#      appears in none of these lists; this chain would have launched straight
#      into it.
#
# gpu_job.sh's flock is the actual mutex - it does not care what the other job
# is called - and re-execing through it also brings the dirty-tree gate and the
# provenance stamp. The sentinel prevents infinite re-exec. Same idiom as
# run_chains/pdnc_context_evidence.sh, which already did this correctly.
if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
    exec "$REPO/gpu_job.sh" "unseen_books" \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
fi
L="$REPO/ab_test_runtime/logs"
PY="$REPO/app/env/bin/python"
IN="$REPO/ab_test_runtime/results/collect_all_20260722-155801/inputs"
OUT="$REPO/ab_test_runtime/unseen_books"
BACKUP="$L/config.json.unseen_backup"
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

for book in mushoku18 grimgar06 mushoku23 arc4_volume10wn; do
    echo ""
    echo "=== $book  $(date -u +%FT%TZ) ==="
    timeout 43200 "$PY" -u generate_script.py "$IN/$book.txt" \
        --output "$OUT/$book.json" > "$L/unseen_$book.log" 2>&1
    rc=$?
    echo "  rc=$rc"
    grep -E "Stripped publisher|Stripped [0-9]+ characters|Split into" "$L/unseen_$book.log" \
        | sed 's/^/  /' | cut -c1-95
    echo "  chunks done : $(grep -c 'Got .* entries' "$L/unseen_$book.log")"
    echo "  retries     : $(grep -c 'failed quality validation' "$L/unseen_$book.log")"
    echo "  ceiling hits: $(grep -c 'completion=16384' "$L/unseen_$book.log")"
    echo "  written     : $([ -f "$OUT/$book.json" ] && echo yes || echo NO)"
    grep -E "^Error" "$L/unseen_$book.log" | tail -2 | sed 's/^/  /' | cut -c1-95
done
echo ""
echo "UNSEEN BOOKS DONE $(date -u +%FT%TZ)"
