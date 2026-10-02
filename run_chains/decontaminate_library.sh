#!/bin/bash
# Goal 2.7: retrain the 60 adapters that trained on their own test data.
#
# WHAT IS WRONG NOW. Every dataset zip splits 180 train / 20 val, but 60 of the
# 75 shipped adapters record training on all 200 clips. Their held-out scores
# are therefore measured partly on clips they memorised, which makes every
# voice-similarity number in the library an upper bound rather than a result.
# The 7 adapters promoted on 2026-08-08 are the only ones trained on the split
# alone, and they are the proof this works: the worst went 0.004 -> 0.701.
#
# NOTHING IS OVERWRITTEN BY THIS SCRIPT. It retrains into a working directory
# and gates each result independently, producing a checkable list. Replacing 60
# shipped voices is a deployment decision with its own rollback, and it belongs
# to a human who has read the list - promote_adapters.py exists for that and
# refuses anything that does not beat what it replaces.
#
# MEDOID REFERENCE. --use-medoid picks the reference clip most similar to the
# rest of the dataset instead of whatever happened to be first. That was the
# other half of the 2026-08-08 finding: a wrong-speaker reference cost 0.541 on
# identical data and seed. Retraining without it would fix the contamination
# and leave the bigger defect in place.
set -uo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd) || exit 1

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
    exec "$REPO/gpu_job.sh" "decontaminate_library" \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
else
    bash "$REPO/gpu_job.sh" --check-lock-owner "${ALEXANDRIA_GPU_LOCK_PID:-}" || exit 1
fi
L="$REPO/ab_test_runtime/logs"
PY="$REPO/app/env/bin/python"
LIST="$REPO/ab_test_runtime/contaminated_adapters.txt"
WORK="$REPO/ab_test_runtime/decontaminate"
mkdir -p "$WORK" "$L" "$REPO/ab_test_runtime/experiments"
cd "$REPO/app"


if ! adapter_lines=$("$PY" - "$LIST" "$REPO" <<'PYLIST'
import sys
from pathlib import Path
from voice_manifest import validate_adapter_name
try:
    names = []
    for number, line in enumerate(Path(sys.argv[1]).read_text(encoding="utf-8").splitlines(), 1):
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        validate_adapter_name(name)
        if name in names:
            raise ValueError(f"duplicate adapter on line {number}: {name}")
        if not (Path(sys.argv[2]) / "lora_models" / name).is_dir():
            raise ValueError(f"adapter on line {number} does not exist: {name}")
        names.append(name)
    if not names:
        raise ValueError("adapter list has no adapter IDs")
    print("\n".join(names))
except (OSError, UnicodeError, ValueError) as exc:
    sys.exit(f"Invalid adapter list: {exc}")
PYLIST
); then
    exit 1
fi
ADAPTERS=()
while IFS= read -r adapter; do
    ADAPTERS+=("$adapter")
done <<< "$adapter_lines"
failures=0
reports=()
echo "retraining ${#ADAPTERS[@]} adapters on the 180-clip train split"

# Batched so a failure costs one batch, not the night, and so partial results
# are readable before the whole run finishes.
BATCH=10
for ((i=0; i<${#ADAPTERS[@]}; i+=BATCH)); do
    slice=("${ADAPTERS[@]:i:BATCH}")
    tag=$(( i / BATCH + 1 ))
    echo ""
    echo "=== batch $tag: ${#slice[@]} adapters  $(date -u +%FT%TZ) ==="
    report="$REPO/ab_test_runtime/experiments/decontaminate_batch$tag.json"
    command=("$PY" -u experiments/retrain_honest.py
        --adapters "${slice[@]}" --use-medoid --resume
        --work "$WORK/batch$tag" --out "$report")
    timeout 43200 "${command[@]}" > "$L/decontaminate_batch$tag.log" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        "${command[@]}" --check-artifact "$report" >> "$L/decontaminate_batch$tag.log" 2>&1
        rc=$?
    fi
    echo "  rc=$rc"
    if [ "$rc" -eq 0 ]; then
        reports+=("$report")
    else
        failures=$((failures + 1))
    fi
    tail -4 "$L/decontaminate_batch$tag.log" | sed 's/^/  /' | cut -c1-110
done

echo ""
echo "=== summary: which retrains beat what ships  $(date -u +%FT%TZ) ==="
"$PY" - "$REPO" "${reports[@]}" <<'PYEOF'
import json, sys
from pathlib import Path
repo = Path(sys.argv[1])
base = {r["adapter"]: r.get("ecapa") for r in json.load(open(
    repo / "ab_test_runtime/experiments/library_voice_fidelity_n10.json", encoding="utf-8"))["results"]}
rows = []
for path in sys.argv[2:]:
    for r in json.load(open(path, encoding="utf-8")).get("results", []):
        new = r.get("new_ecapa_heldout")
        old = base.get(r["adapter"])
        if new is None or old is None:
            continue
        rows.append((r["adapter"], old, new))
better = [r for r in rows if r[2] >= 0.45 and r[2] > r[1] + 0.05]
print(f"  retrained: {len(rows)}   worth promoting (>=0.45 and +0.05): {len(better)}")
for name, old, new in sorted(better, key=lambda x: -(x[2] - x[1]))[:15]:
    print(f"    {name[:36]:38} {old:.3f} -> {new:.3f}  (+{new - old:.3f})")
print("\n  NOTHING WAS OVERWRITTEN. To promote, review the list then run:")
print("    app/env/bin/python promote_adapters.py --dry-run")
PYEOF
summary_rc=$?
if [ "$summary_rc" -ne 0 ]; then
    failures=$((failures + 1))
fi
echo ""
if [ "$failures" -ne 0 ]; then
    echo "DECONTAMINATION INCOMPLETE: $failures failed batches/summary $(date -u +%FT%TZ)"
    exit 1
fi
echo "DECONTAMINATION DONE $(date -u +%FT%TZ)"
