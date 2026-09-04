#!/usr/bin/env bash
# Measure the five shipped-book loanwords goal 5.5 still needs.
#
# WHY ONLY FIVE. The scan found 18 shipped-book terms in neither state. Triage
# classified 13 out of scope: 9 names (Ram's nickname for Subaru, afterword
# credits, the author's pen name Nezumi-iro Neko), 1 place, 1 Spanish idiom
# ("mano a mano"), 2 onomatopoeia. Measuring those would put an author's
# credits and a Spanish phrase into a Japanese-loanword lexicon.
#
# TWO PASSES, DELIBERATELY. measure_respellings filters on an EXACT verdict
# match, and `meimei` is attributed `unattributed` while the other four are
# `ja`. Editing the verdict to force one pass would falsify the input, so the
# unattributed term runs separately and its artifact says so.
#
# --min-books 1 IS REQUIRED. The default is 20 and would silently drop `deka`
# (6 books) and `kuchibashi` (4) - the run would report on two terms while
# looking like it reported on five.
set -uo pipefail
R=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
cd "$R"
C="$R/ab_test_runtime/five_terms_candidates.json"
test -s "$C" || { echo "candidates file missing"; exit 1; }

for verdict in ja unattributed; do
    out="$R/ab_test_runtime/experiments/respelling_five_terms_${verdict}.json"
    [ -s "$out" ] && { echo "SKIP $verdict"; continue; }
    echo "=== $verdict $(date -Is)"
    ./app/env/bin/python -u app/experiments/measure_respellings.py \
        --candidates "$C" --verdict "$verdict" --min-books 1 \
        --work "$R/ab_test_runtime/respelling_five_${verdict}" \
        --out "$out" > "/tmp/five_${verdict}.log" 2>&1
    rc=$?
    echo "[$(date -Is)] DONE $verdict rc=$rc"
done
echo "COMPLETE $(date -Is)" | tee "$R/ab_test_runtime/five_terms.complete"
