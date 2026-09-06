#!/usr/bin/env bash
# Extend the end-to-end PDNC baseline from three books to eight.
#
# WHY. `two_stage_attribution_w3200.json` reads 65.6% over 2,494 rows, and that
# is the number this project compares against the literature - specifically
# against BookNLP-OG's 0.40, the ONLY published PDNC figure measured end to end
# where the system builds its own character list. Everything else on that
# leaderboard is scored with the gold alias map supplied.
#
# But our 65.6% is three books: PrideAndPrejudice, TheAwakening,
# TheSignOfTheFour. On 2026-09-06 a coverage audit of the light novels found
# that 54 of 58 evaluations there silently omitted the book holding half the
# gold, and that the per-book target was therefore a statement about three
# books rather than four. The same question had never been asked of PDNC.
#
# THE FIVE ADDED BOOKS ARE NEVER-TRAINED. The author-held-out manifest excludes
# AUST, CHOP and DOYL; Emma, MansfieldPark, NorthangerAbbey, Persuasion and
# SenseAndSensibility are the five excluded Austen novels that no arm has yet
# been run on at w3200. Adding them takes the baseline from 2,494 rows to
# 7,643 and from one Austen novel to six.
#
# WHAT THIS IS NOT. Not an adapter test and not an intervention: the same
# unrestricted arm, the same window, the same model and seed as the run it
# extends, so the eight-book number is comparable to the three-book one and the
# difference is corpus rather than method.
#
# WHAT IT COULD SHOW. If 65.6% holds across eight books, the comparison against
# BookNLP-OG rests on something much broader. If it moves a lot, then the
# headline number was a property of three books, and this project has just
# spent a day learning what that costs.
#
# Resumable by artifact. rc after a pipeline is ${PIPESTATUS[0]}, per #505.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
BASE_URL="${PDNC_BASE_URL:-http://127.0.0.1:8090/v1}"
ART="$R/ab_test_runtime/experiments/two_stage_attribution_w3200_eight.json"
[ -s "$ART" ] && { echo "SKIP - artifact exists"; exit 0; }

# A DEAD ENDPOINT MUST NOT LOOK LIKE A FINISHED RUN. On 2026-09-06 this exact
# chain family exited 0 with no artifact because the server was down and the
# error was swallowed by a pipeline; gpu_job logged OK.
curl -s --max-time 10 "${BASE_URL%/v1}/props" >/dev/null 2>&1 || {
    echo "no llama.cpp at $BASE_URL - start a server for qwen3-14b first" >&2
    exit 1
}
FIX=""
for b in prideandprejudice theawakening thesignofthefour \
         emma mansfieldpark northangerabbey persuasion senseandsensibility; do
    f="$R/app/fixtures/attribution_gold_pdnc_${b}_w3200.json"
    test -s "$f" || { echo "fixture missing: $b" >&2; exit 1; }
    FIX="$FIX $f"
done
echo "fixtures: $(echo $FIX | wc -w)"
"$PY" -u app/experiments/two_stage_attribution.py \
    --fixtures $FIX --limit 0 --seed 20260819 --tag w3200_eight \
    --base-url "$BASE_URL" --out "$ART" 2>&1 | tail -40
rc=${PIPESTATUS[0]}
echo "[$(date -Is)] RUN rc=$rc"
[ "$rc" -eq 0 ] || exit "$rc"
[ -s "$ART" ] || { echo "reported success but wrote no artifact" >&2; exit 1; }
echo "ALL DONE $(date -Is)"
