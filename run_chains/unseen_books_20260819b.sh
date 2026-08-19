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
    exec "$REPO/gpu_job.sh" "unseen_books_b" \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
fi
L="$REPO/ab_test_runtime/logs"
PY="$REPO/app/env/bin/python"
IN="$REPO/ab_test_runtime/results/collect_all_20260722-155801/inputs"
OUT="$REPO/ab_test_runtime/unseen_books"
BACKUP="$L/config.json.unseen_backup"
mkdir -p "$OUT"
cd "$REPO/app"

# A LEFTOVER BACKUP MEANS THE LAST RUN DIED. `timeout --kill-after` ends in
# SIGKILL, which no trap can catch, so on 2026-08-19 the EXIT trap never ran:
# app/config.json was left pointing at qwen3-14b, and the NEXT run then copied
# that modified file over the backup - destroying the only record of what the
# setting had been. Restoring first makes the recovery survive a kill, and
# deleting the backup on the way out means its presence is itself the signal
# that a run did not finish.
restore() {
    [ -f "$BACKUP" ] || return 0
    command cp -f "$BACKUP" "$REPO/app/config.json"
    rm -f "$BACKUP"
}
if [ -f "$BACKUP" ]; then
    echo "a previous run left config.json modified; restoring it before starting"
    restore
fi
trap restore EXIT INT TERM


# START THE SERVER RATHER THAN COMPLAINING ABOUT ITS ABSENCE. This aborted
# instantly at 01:57Z on 2026-08-18 and gave a 6-hour slot back to nobody: the
# overnight driver stops llama-server between stages to reclaim VRAM (right
# for the TTS stages, since nothing else ever reclaims that 8.4 GB), and this
# chain needed one and would not start it. Every caller would otherwise have
# to know which stages need a server - so the stage that needs it asks for it.
#
# ensure_llama_server.sh is idempotent and reuses a healthy server, so calling
# it when one is already up costs a health check.
if ! curl -s -m 20 http://127.0.0.1:8090/v1/models | grep -q qwen3; then
    echo "no qwen3 server on 8090; starting one"
    "$REPO/ensure_llama_server.sh" || { echo "ABORT: could not start a server"; exit 1; }
    if ! curl -s -m 60 http://127.0.0.1:8090/v1/models | grep -q qwen3; then
        echo "ABORT: server started but is not serving qwen3 on 8090"; exit 1
    fi
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

# ARTIFACT EXISTS IS NOT ARTIFACT FINISHED, so ask the file rather than `-f`:
# generate_script.py writes as it goes, and a book cut off mid-run leaves a
# JSON that a bare existence check would skip forever.
book_complete() {
    "$PY" - "$1" <<'PYEOF' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        doc = json.load(handle)
except Exception:
    sys.exit(1)
entries = doc if isinstance(doc, list) else doc.get("entries") or doc.get("script")
sys.exit(0 if isinstance(entries, list) and len(entries) > 50 else 1)
PYEOF
}

failed_books=0; skipped_books=0; total_books=0; failed_names=""
for book in mushoku18 grimgar06 mushoku23 arc4_volume10wn; do
    total_books=$((total_books + 1))
    echo ""
    echo "=== $book  $(date -u +%FT%TZ) ==="
    if book_complete "$OUT/$book.json"; then
        echo "  SKIP - already generated ($(stat -c%s "$OUT/$book.json") bytes)"
        skipped_books=$((skipped_books + 1))
        continue
    fi
    timeout 43200 "$PY" -u generate_script.py "$IN/$book.txt" \
        --output "$OUT/$book.json" > "$L/unseen_$book.log" 2>&1
    rc=$?
    echo "  rc=$rc"
    # Judge by the ARTIFACT as well as the code: a book can exit 0 having
    # written nothing, and "written: NO" below is already computed from the
    # file. Both must be right for the book to count as produced.
    if [ "$rc" -ne 0 ] || ! book_complete "$OUT/$book.json"; then
        failed_books=$((failed_books + 1))
        failed_names="$failed_names $book"
    fi
    grep -E "Stripped publisher|Stripped [0-9]+ characters|Split into" "$L/unseen_$book.log" \
        | sed 's/^/  /' | cut -c1-95
    echo "  chunks done : $(grep -c 'Got .* entries' "$L/unseen_$book.log")"
    echo "  retries     : $(grep -c 'failed quality validation' "$L/unseen_$book.log")"
    echo "  ceiling hits: $(grep -c 'completion=16384' "$L/unseen_$book.log")"
    echo "  written     : $([ -f "$OUT/$book.json" ] && echo yes || echo NO)"
    grep -E "^Error" "$L/unseen_$book.log" | tail -2 | sed 's/^/  /' | cut -c1-95
done
echo ""
# A BOOK THAT FAILED MUST NOT REPORT DONE. grimgar06 died on 2026-08-19 with
# rc=1 ("chunk 29/70 failed validation after retries"), wrote no output, and
# this script still printed DONE and exited 0 - so gpu_job.sh logged OK and the
# chain counted the stage as a pass. One of four books produced nothing and
# every layer above said success (Rule 8).
if [ "$failed_books" -gt 0 ]; then
    echo "UNSEEN BOOKS INCOMPLETE $(date -u +%FT%TZ): $failed_books of $total_books failed"
    echo "  failed: $failed_names"
    exit 1
fi
echo "UNSEEN BOOKS DONE $(date -u +%FT%TZ) ($total_books books, $skipped_books already present)"
