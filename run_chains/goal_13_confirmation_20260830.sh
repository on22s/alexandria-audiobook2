#!/bin/bash
# Goal 1.3: does the balanced adapter's gain survive on books it never trained on?
#
# WHAT THIS ANSWERS. GOALS 1.3 asks for two things a broader confirmation must
# have, and until #420 neither was possible:
#
#   never-trained books   the 89.1% rests on Pride and Prejudice, The Awakening
#                         and The Sign of the Four. Those three are the only
#                         never-trained books that had gold fixtures, so they
#                         are already spent as confirmation.
#   dev vs held-out       "compare the same adapter on both development and
#                         held-out sets" - never measured, because there was
#                         nothing to compare against.
#
# #420 built five Austen novels the adapter never saw (author AUST excluded
# wholesale from its twenty-novel manifest). This chain runs the SAME adapter
# over both halves under one server load:
#
#   held-out    Emma, MansfieldPark, NorthangerAbbey, Persuasion,
#               SenseAndSensibility        - 5,149 quotations available
#   development AHandfulOfDust, TheGambler, TheMysteriousAffairAtStyles
#               - all three ARE in the training manifest
#
# A dev score far above the held-out score is memorisation; a small gap is
# transfer. Neither number alone says which.
#
# WHAT IT CANNOT SAY. All five held-out books are Austen. This widens the
# held-out set without widening the register, so a good result confirms
# transfer beyond the TRAINING NOVELS, not beyond Austen. Do not let a
# five-book number be quoted as general generalisation.
#
# COST, MEASURED - and the first estimate here was wrong by an order of
# magnitude. It was derived per-ROW from a different harness (lora_serving_eval,
# 766 rows in 2,890 s) and read as 4-6 hours. pdnc_eval batches 25 rows into ONE
# request, so the unit is the batch, not the row: a 25-row batch of Emma took
# 14.3 s base and 11.3 s lora against this server, prompt 8,664 tokens.
#
# That makes the FULL fixtures affordable, so this takes no --limit by default
# and scores every quotation rather than a 300-row subsample:
#
#   held-out     5,149 rows   development  4,965 rows
#   10,114 rows x 2 arms = 810 batches x ~13 s = ~2.9 hours
#
# Rosters differ more than row counts do - AHandfulOfDust has 104 characters
# against Emma's 16 - so its prompts are larger; 32K context has ample room at
# batch 25, and GOAL13_BATCH lowers it if a book ever refuses.
#
# One gpu_job per book, so an interrupted run resumes instead of restarting,
# and ensure_llama_server keeps ONE model load across all of them.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/goal_13_confirmation_20260830"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

ADAPTER="$runtime/distill/gguf/new_20260824/adapter_author_heldout_balanced.gguf"
PORT="${LLAMA_PORT:-8090}"
# Every quotation, not a subsample: at the measured rate the whole corpus
# costs ~3 hours, and a 300-row cap would throw away 70% of the evidence
# for no saving worth having. Set GOAL13_LIMIT to subsample deliberately.
LIMIT="${GOAL13_LIMIT:-100000}"
BATCH="${GOAL13_BATCH:-25}"

HELDOUT="emma mansfieldpark northangerabbey persuasion senseandsensibility"
DEVELOPMENT="ahandfulofdust thegambler themysteriousaffairatstyles"

# REFUSE EARLY RATHER THAN AT HOUR FOUR. Every input is checked before the
# first model load, because a missing fixture discovered after three books is
# three books of GPU spent on a run that cannot be summarised.
for stem in $HELDOUT $DEVELOPMENT; do
    fx="$REPO/app/fixtures/attribution_gold_pdnc_${stem}.json"
    [ -s "$fx" ] || { echo "REFUSING: missing fixture $fx" >&2; exit 1; }
done
[ -s "$ADAPTER" ] || { echo "REFUSING: missing adapter $ADAPTER" >&2; exit 1; }

# The adapter must be LOADED by the server: pdnc_eval switches arms with
# POST /lora-adapters, which can only scale an adapter that is already there.
LLAMA_PORT="$PORT" "$REPO/ensure_llama_server.sh" "$ADAPTER" \
    > "$STAGE_LOG_DIR/server.log" 2>&1 \
    && echo "llama-server up with $(basename "$ADAPTER")" \
    || { echo "REFUSING: llama-server failed to start" >&2; exit 1; }

run_book() {
    local half=$1 stem=$2
    local name="goal13_${half}_${stem}"
    local out="$runtime/experiments/pdnc_eval__${name}.json"
    if [ -s "$out" ]; then
        stage_note "SKIP $name (already complete)"
        return
    fi
    run_stage "$name" 3h -- \
        env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
        "$REPO/gpu_job.sh" "$name" \
        "$python" -u "$REPO/app/experiments/pdnc_eval.py" \
        --fixtures "$REPO/app/fixtures/attribution_gold_pdnc_${stem}.json" \
        --base_url "http://127.0.0.1:$PORT/v1" \
        --model qwen/qwen3-14b \
        --limit "$LIMIT" \
        --batch "$BATCH" \
        --out "$out"
    stage_commit_artifacts "$name" "$REPO"
}

# Held-out first: if the night is cut short, the half that answers the goal is
# the half that exists.
for stem in $HELDOUT;     do run_book heldout "$stem"; done
for stem in $DEVELOPMENT; do run_book development "$stem"; done

"$python" - "$runtime" "$HELDOUT" "$DEVELOPMENT" <<'PYEOF'
import json, os, sys
runtime, halves = sys.argv[1], (("heldout", sys.argv[2]), ("development", sys.argv[3]))
print("\n%-13s %-24s %7s %8s %8s %7s" % ("half", "book", "n", "base", "lora", "delta"))
totals = {}
for half, stems in halves:
    agg = [0, 0, 0]
    for stem in stems.split():
        p = os.path.join(runtime, "experiments", "pdnc_eval__goal13_%s_%s.json" % (half, stem))
        try:
            d = json.load(open(p))
        except Exception as exc:
            print("%-13s %-24s no artifact (%s)" % (half, stem, type(exc).__name__)); continue
        for book, arms in d.items():
            if not isinstance(arms, dict) or "base" not in arms:
                continue
            b, l = arms["base"], arms["lora"]
            n = b.get("n") or len(b.get("rows") or [])
            bc = b.get("correct", sum(1 for r in b.get("rows", []) if r.get("ok")))
            lc = l.get("correct", sum(1 for r in l.get("rows", []) if r.get("ok")))
            agg[0] += n; agg[1] += bc; agg[2] += lc
            print("%-13s %-24s %7d %7.1f%% %7.1f%% %+6.1f"
                  % (half, book, n, 100*bc/n, 100*lc/n, 100*(lc-bc)/n))
    if agg[0]:
        totals[half] = (100*agg[1]/agg[0], 100*agg[2]/agg[0], agg[0])
        print("%-13s %-24s %7d %7.1f%% %7.1f%% %+6.1f  <-- pooled"
              % (half, "ALL", agg[0], totals[half][0], totals[half][1],
                 totals[half][1]-totals[half][0]))
if len(totals) == 2:
    gap = totals["development"][1] - totals["heldout"][1]
    print("\nDEV MINUS HELD-OUT (lora arm): %+.1f points" % gap)
    print("A large positive gap is memorisation; a small one is transfer.")
    print("All five held-out books are Austen: this does not test register transfer.")
PYEOF

stage_summary goal_13_confirmation_20260830
