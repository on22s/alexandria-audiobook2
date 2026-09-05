#!/usr/bin/env bash
# Does context keep paying past 3,200 characters?
#
# Widening 400 -> 3,200 was worth +12.7 points on Explicit and took the overall
# to 65.6% (two_stage_attribution_w3200.json, 2,494 PDNC rows). The curve had
# not flattened, and nobody tried a third width because the window was a
# function default in pdnc_fixture.py rather than a flag.
#
# The closest published analogue chunks FIVE TIMES WIDER. Llama-3 8b reaches
# 90.6% on PDNC zero-shot (arXiv 2406.11380) at 4096 tokens - roughly 16,000
# characters. Its 90.6% is not a target, because the prompt carries a gold
# character-to-alias map and 1.2 measured that our roster holds the right name
# 85% of the time while the model picks it 29.9%. But the WIDTH is not an
# oracle. It is a parameter we already have.
#
# w3200 is not re-run: it is already measured at 65.6% on these same rows.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
MODEL="$(find "$HOME" -maxdepth 6 -name 'Qwen3-14B-Q4_K_M.gguf' 2>/dev/null | head -1)"
[ -s "$MODEL" ] || { echo "Qwen3-14B gguf not found" >&2; exit 1; }
SRV="$(command -v llama-server)"
[ -x "$SRV" ] || { echo "no llama-server" >&2; exit 1; }
echo "model: $MODEL"

for w in w8000 w16000; do
  for n in prideandprejudice theawakening thesignofthefour; do
    f="$R/app/fixtures/attribution_gold_pdnc_${n}_${w}.json"
    [ -s "$f" ] || { echo "fixture missing: $(basename "$f") - rebuild with pdnc_fixture.py --context-chars" >&2; exit 1; }
  done
done

# 16,000 chars either side is ~8k tokens of context before prompt and roster,
# so the server needs room well beyond the 32k a 3,200 run was comfortable in.
pkill -x llama-server 2>/dev/null; sleep 5
"$SRV" -m "$MODEL" --port 8099 --host 127.0.0.1 -ngl 99 -c 32768 --parallel 1 \
    > /home/fakemitch/llama_window_sweep.log 2>&1 &
ready=0
for i in $(seq 1 90); do
    curl -s --max-time 3 http://127.0.0.1:8099/v1/models >/dev/null 2>&1 && { ready=1; break; }
    sleep 10
done
[ "$ready" = 1 ] || { echo "server never became ready" >&2; tail -5 /home/fakemitch/llama_window_sweep.log >&2; pkill -x llama-server; exit 1; }
echo "server ready after $((i*10))s"

for w in w8000 w16000; do
    art="$R/ab_test_runtime/experiments/two_stage_attribution_${w}.json"
    if [ -s "$art" ]; then echo "SKIP $w"; continue; fi
    "$PY" -u app/experiments/two_stage_attribution.py \
        --fixtures "$R/app/fixtures/attribution_gold_pdnc_prideandprejudice_${w}.json" \
                   "$R/app/fixtures/attribution_gold_pdnc_theawakening_${w}.json" \
                   "$R/app/fixtures/attribution_gold_pdnc_thesignofthefour_${w}.json" \
        --base-url http://127.0.0.1:8099/v1 --model qwen3-14b \
        --tag "window-${w}" --out "$art" \
        > "/home/fakemitch/window_${w}.log" 2>&1
    rc=$?
    stamp="$(date -Is)"
    echo "[$stamp] SWEEP $w rc=$rc"
done
pkill -x llama-server 2>/dev/null
echo "ALL DONE $(date -Is)"
