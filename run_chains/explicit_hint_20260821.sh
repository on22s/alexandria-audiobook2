#!/usr/bin/bash
# Does telling the model to obey an explicit attribution fix the easy category?
#
# THE MEASUREMENT THAT PROMPTED THIS. On the 2,494 stored PDNC rows our
# accuracy by quote type is Implicit .629, Anaphoric .712, Explicit .645 -
# Explicit, where the text NAMES the speaker beside the line, is our
# second-worst category. Three independent systems put it at ~.99:
#
#   ModernBERT joint scoring, 2026        .993   (with gold mentions)
#   Elson & McKeown regex, AAAI 2010      .99    (no oracle)
#   our own trigram rule, PR #372         .9899  (no oracle)
#
# And the answer was in front of the model: reconstructing the exact prompt for
# every wrong Explicit row shows the gold speaker's name present in 186 of 193.
# The model reads "said Mr. Darcy" and answers ELIZABETH.
#
# ONE SENTENCE, ONE VARIABLE. The control arm is byte-identical to the shipped
# prompt (pinned by app/tests/test_prompt_variant.py); the treatment appends
# EXPLICIT_HINT and nothing else. Same --seed and --limit, so both arms see the
# same rows.
#
# WHY THIS IS WORTH GPU TIME WHEN THE TRIGRAM IS FREE. The trigram fires on
# 0.75% of lines in our own 29 shipped books - it fixes 16 of 36,642 - because
# light-novel prose rarely uses the construction. If the fix has to work on OUR
# books it has to come from the model, not from a regex.
#
# COST. 543 Explicit rows across three fixtures, two arms, ~1,100 calls. No
# prior run of this shape exists, so the estimate is from the full 2,494-row
# run and is a guess, not a measurement: call it 1-2 hours total.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/explicit_hint_20260821"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

"$python" -c "
import sys; sys.path.insert(0, '$REPO/app')
from experiments.two_stage_attribution import PROMPT_VARIANTS
assert 'explicit_hint' in PROMPT_VARIANTS, PROMPT_VARIANTS" || {
    echo "REFUSING: this checkout has no explicit_hint variant; the treatment"
    echo "  arm would run the control twice under two names."
    exit 1; }

start_server() {
    ALEXANDRIA_QWEN3_MODEL="${ALEXANDRIA_QWEN3_MODEL:-/home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}" \
        "$REPO/ensure_llama_server.sh" > "$STAGE_LOG_DIR/server.log" 2>&1 \
        && echo "llama-server up" || echo "llama-server FAILED to start"
}
start_server

# shuffled_roster joins the chain from #383: when the model is wrong its
# answer sits earlier in the alphabetical cast list than the correct one 67.2%
# of the time (p = 1.3e-23). It is the better-motivated of the two treatments,
# since #382 showed the model is not short of information.
for variant in control explicit_hint shuffled_roster inner_narration speaker_not_addressee; do
    run_stage "explicit_$variant" 3h -- \
        env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
        "$REPO/gpu_job.sh" "explicit_$variant" \
        "$python" -u "$REPO/app/experiments/two_stage_attribution.py" \
        --quote-type Explicit --limit 1000 --seed 20260819 \
        --prompt-variant "$variant" --keep-prompts \
        --tag "explicit_$variant" \
        --out "$runtime/experiments/two_stage_attribution__explicit_$variant.json"
    stage_commit_artifacts "explicit_$variant" "$REPO"
done

# runtime is passed in: REPO is a shell variable, not an exported one, and
# os.environ.get("REPO", ".") would have summarised the wrong directory in
# silence.
"$python" - "$runtime" <<'PYEOF'
import json, os, sys
runtime = sys.argv[1]
for variant in ("control", "explicit_hint", "shuffled_roster",
                "inner_narration", "speaker_not_addressee"):
    path = os.path.join(runtime, "experiments",
                        "two_stage_attribution__explicit_%s.json" % variant)
    try:
        d = json.load(open(path))
    except Exception as exc:
        print("  %-14s no artifact (%s)" % (variant, exc)); continue
    rows = d.get("rows") or []
    n = len(rows); c = sum(1 for r in rows if r.get("correct"))
    print("  %-14s n=%-5d correct=%-5d acc=%.4f  meta.variant=%s"
          % (variant, n, c, (c / n) if n else 0,
             (d.get("meta") or {}).get("prompt_variant")))
PYEOF

stage_summary explicit_hint_20260821
