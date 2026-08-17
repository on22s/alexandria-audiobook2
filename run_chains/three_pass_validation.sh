#!/bin/bash
# Exercise the three-pass path end to end on the current code.
#
# WHY. Every fix this week is present in BOTH generators - verified by reading
# both modules and enforced by test_generation_path_agreement. But structural
# presence is not the same as having been run. Only index18 has completed
# three-pass on the current code; mushoku16 and owarimonogatari3 last ran
# 2026-08-09, before the changes, and grimgar03 has never run three-pass at
# all.
#
# So this is a COMPLETION test, not an accuracy comparison. Reusing the
# 2026-08-09 single arms would mix pipelines and produce a number describing
# neither. The question here is narrower and answerable: does the three-pass
# path finish these books on the code that ships now?
#
# grimgar03 is the interesting one. It has never been through this path, and
# its front matter carries the eight repeated titles that the
# faithful-duplicate fix has to survive - on a path whose gates live in
# pass_quality rather than chunk_quality.
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git

# HOLD THE REAL LOCK INSTEAD OF GUESSING WHO IS RUNNING. The poll loop this
# replaces enumerated script names, so a GPU job nobody listed was invisible -
# `measure_respellings.py` held the card for seventeen hours on 2026-08-17 and
# appears in none of those lists. It also raced between "nothing is running"
# and "start mine". gpu_job.sh's flock does not care what the other job is
# called. Same idiom as run_chains/pdnc_context_evidence.sh; the sentinel is
# exported by gpu_job.sh, so this is safe when nested.
if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
    exec "$REPO/gpu_job.sh" "three_pass_validation" \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
fi
L="$REPO/ab_test_runtime/logs"
PY="$REPO/app/env/bin/python"
IN="$REPO/ab_test_runtime/results/collect_all_20260722-155801/inputs"
OUT="$REPO/ab_test_runtime/three_pass_validation"
BACKUP="$L/config.json.tpv_backup"
mkdir -p "$OUT"
cd "$REPO/app"

restore() { [ -f "$BACKUP" ] && command cp -f "$BACKUP" "$REPO/app/config.json"; }
trap restore EXIT INT TERM

# Never run alongside another generator: two against one server took identical
# calls from 45 tok/s to 5.7 and cost 2h12m.

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

for book in grimgar03 mushoku16 owarimonogatari3; do
    echo ""
    echo "=== three-pass $book  $(date -u +%FT%TZ) ==="
    timeout 28800 "$PY" -u three_pass_generate.py "$IN/$book.txt" \
        --output "$OUT/$book.json" --pass2-on-exhaustion fallback \
        > "$L/tpv_$book.log" 2>&1
    echo "  rc=$?"
    grep -E "Stripped publisher|Split into" "$L/tpv_$book.log" | sed 's/^/  /' | cut -c1-95
    echo "  written: $([ -f "$OUT/$book.json" ] && echo yes || echo NO)"
    "$PY" - "$OUT/$book.json.threepass_manifest.json" <<'PYEOF'
import json, os, sys
p = sys.argv[1]
if os.path.exists(p):
    d = json.load(open(p, encoding="utf-8"))
    t = d.get("telemetry") or {}
    print(f"  status={d.get('status')}  model={t.get('model_name')}  "
          f"failed_pass={d.get('failed_pass')} chunk={d.get('failed_chunk')}")
else:
    print("  no manifest")
PYEOF
done
echo ""
echo "THREE-PASS VALIDATION DONE $(date -u +%FT%TZ)"
