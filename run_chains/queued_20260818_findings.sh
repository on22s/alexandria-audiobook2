#!/usr/bin/bash
# Work queued from 2026-08-18's findings. NOT STARTED - launch when you want it.
#
#   ./run_chains/queued_20260818_findings.sh
#
# Ordered by what invalidates other work if left undone, not by cost.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
inputs="$runtime/results/collect_all_20260722-155801/inputs"
STAGE_LOG_DIR="$runtime/logs/queued_20260818"
source "$REPO/run_chains/lib/stage.sh"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"

# ---------------------------------------------------------------- 1. index18
# ITS SOURCE FILE IS CORRUPT AND 32 EXPERIMENTS REST ON IT. 6,662 replacement
# characters, 1.4% of the text - literal EF BF BD bytes, so the file was
# WRITTEN after a lossy decode - and not one quotation mark or apostrophe
# survives. Our own gate would refuse it: MAX_REPLACEMENT_SHARE is 0.005.
#
# So index18 is not "the hard book that exposed five blockers"; it is a book
# whose dialogue markup was destroyed before any model saw it, and GOALS.md
# quotes per-book numbers from it. Re-extract first: every method comparison
# involving this book is confounded until it is clean.
# The damage cannot be repaired by re-reading the file - the original bytes
# are gone - and the EPUB is not on this machine, so this stage AUDITS and
# stops rather than pretending to fix. It names the artifacts that cite a
# failing book, which is what decides how much else has to be re-run.
run_stage source_encoding_audit 10m -- \
    "$python" -u "$REPO/app/experiments/audit_source_encoding.py"

# ------------------------------------------------- 2. two-stage attribution
# The published formulation - given the quote and the character list, name the
# speaker - reaches 90.6% on PDNC with an 8B model. Ours segments, classifies
# and attributes in one pass and scores 61.7% with a 14B. The gold fixtures
# already carry the spans, the context and the aliases, so this is a question
# shape rather than new plumbing. UNKNOWN is offered and scored separately: a
# model that declines is a different proposition from one that guesses.
run_stage two_stage_attribution 4h -- \
    env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
    "$REPO/gpu_job.sh" two_stage_attribution \
    "$python" -u "$REPO/app/experiments/two_stage_attribution.py" --limit 200

# ------------------------------------------- 3. mushoku18 with its narrator
# The narrator prior is wired into generation and unmeasured there. This book
# is the case it was built for: first person, the narrator speaks aloud, and
# 51% of its spoken lines stayed with NARRATOR. Same book, same settings, one
# variable - so it is comparable to itself even without an answer key.
run_stage mushoku18_narrator 5h -- \
    env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
    "$REPO/gpu_job.sh" mushoku18_narrator \
    "$python" -u "$REPO/app/generate_script.py" "$inputs/mushoku18.txt" \
    --narrator RUDEUS --output "$runtime/unseen_books/mushoku18_narrator.json"

# --------------------------------------------------- 4. a second -ay voice
# Every respelling number in this project comes from one voice. If -ay's
# advantage is a property of that voice rather than of English orthography,
# this is where it shows.
run_stage e_row_second_voice 2h -- \
    "$REPO/gpu_job.sh" e_row_second_voice \
    "$python" -u "$REPO/app/experiments/measure_respellings.py" \
    --min-books 5 --only-e-row --e-spelling ay --limit 200 \
    --work "$runtime/respelling_voice2" \
    --out "$runtime/experiments/respelling_e_row__ay_voice2.json"

stage_summary queued_20260818

echo
echo "WHAT IS NOT HERE, deliberately:"
echo "  - more light-novel method experiments, until index18 is re-extracted."
echo "    If corrupt text has been feeding the four-book set, running more of"
echo "    them measures an encoding bug more precisely."
echo "  - the second listening test. It needs the separator arms to finish"
echo "    first, and then a person: the ear is the only instrument that has"
echo "    been right about respellings so far."
