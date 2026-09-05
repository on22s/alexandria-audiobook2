#!/usr/bin/env bash
# Are the nine "broken" shipped adapters broken, or measured against the wrong
# person?
#
# WHAT PROMPTED THIS. Rebuilding five of them produced a result nobody expected
# in the middle column: two shipped adapters scored far HIGHER on voice-filtered
# held-out clips than the library records.
#
#     husky_baritone_20s_m_supernatural   library 0.160 -> 0.548  PASS
#     crisp_mezzo_30s_f                   library 0.426 -> 0.552  PASS
#
# The first is one of three "stable failures" that 13 and then 21 independent
# validation draws agreed was broken at 0.079-0.160. A 0.39 gap is not seed
# noise.
#
# THE HYPOTHESIS IS ABOUT THE CLIPS, NOT THE ADAPTER. library_voice_fidelity
# scores each adapter against its OWN dataset's val split, and those splits
# predate the same-voice guard added on 2026-09-05 - the one that found Waking
# Gods has twelve narrators and Dracula nine, and that 16 of 17 Waking Gods
# volumes are a different voice from the trained one. If a val split carries
# wrong-speaker clips, the library measured the adapter against somebody else
# and recorded it as a broken voice.
#
# So: score each shipped adapter on clips that are voice-verified against its
# own trained volume AND that it never trained on. build_unseen_holdout does
# both and refuses when it cannot verify, which is the answer for the cast
# productions either way.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
ZIPS="${ALEXANDRIA_ZIPS_DIR:-$HOME/Desktop/zips2}"
WORK="$R/ab_test_runtime/rescore_voicefiltered"
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
EMB="$R/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || EMB="$MAIN/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || { echo "no embedding cache" >&2; exit 1; }
export ALEXANDRIA_SIBLING_PYTHON="${ALEXANDRIA_SIBLING_PYTHON:-/home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python}"
[ -x "$ALEXANDRIA_SIBLING_PYTHON" ] || { echo "no speechbrain interpreter" >&2; exit 1; }
# Count adapters, do not merely test that a directory exists: a worktree has a
# lora_models directory because git tracks the manifest, and none of the 75
# safetensors are tracked.
has_adapters () { find "$1" -name adapter_model.safetensors 2>/dev/null | head -1; }
LORA="$R/lora_models"
[ -n "$(has_adapters "$LORA")" ] || LORA="$MAIN/lora_models"
[ -n "$(has_adapters "$LORA")" ] || { echo "no shipped adapters found" >&2; exit 1; }
echo "shipped adapters: $LORA"

while IFS=$'\t' read -r name zip book libscore; do
    [ -z "$name" ] && continue
    art="$R/ab_test_runtime/experiments/rescore_vf__${name}.json"
    [ -s "$art" ] && { echo "SKIP $name"; continue; }
    dir="$WORK/$name"
    if [ ! -f "$dir/holdout.json" ]; then
        "$PY" -u app/experiments/build_unseen_holdout.py \
            --trained-zip "$ZIPS/_deduped/$zip" --source-dir "$ZIPS/$book" \
            --embeddings "$EMB" --out "$dir" --lines 20 \
            > "/tmp/vf_${name}_build.log" 2>&1
        brc=$?
        echo "BUILD $name rc=$brc (library $libscore)"
    fi
    if [ ! -s "$dir/val/metadata.jsonl" ]; then
        echo "  REFUSED $name - no voice-verified clips available"
        continue
    fi
    "$PY" -u app/experiments/verify_adapter_identity.py \
        --adapter "$LORA/$name" --dataset "$dir" --lines 12 --out "$art" \
        > "/tmp/vf_${name}_gate.log" 2>&1
    rc=$?
    ts="$(date -Is)"
    echo "[$ts] GATE $name rc=$rc (library $libscore)"
done < "$R/ab_test_runtime/rescore_failing_list.tsv"
echo "ALL DONE $(date -Is)"
