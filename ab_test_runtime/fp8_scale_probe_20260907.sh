#!/usr/bin/env bash
# Is the FP8 checkpoint's unplaced gate_proj scale why nothing ever stops?
#
# WHAT PROMPTED THIS. qwen38-multientry-thinking-low-8k burned 12.5 hours of
# H100 on 2026-09-07 for zero valid answers: 36 of 36 generations ended
# finish_reason=length at exactly 8000 tokens, none on `stop`, every window
# PassExhausted, both arms 0. Killed at 36 of 80 - the remaining 44 rows were
# ~15 more hours to restate the same verdict at a larger n.
#
# The candidate mechanism is on the log's first page:
#
#   model.language_model.layers.{0...63}.mlp.gate_proj.weight_scale_inv | UNEXPECTED
#
# The FP8 kernel preflight PASSED on that run, so "the kernel was missing" -
# the explanation for the earlier FP8 dead runs - is already ruled out here.
#
# WHY IT IS TWO LOADS AND NOT AN EVAL. Nothing about books, gold, adapters or
# thinking mode differs between the runs that work and this one; the checkpoint
# does. So the probe holds everything else identical and changes only that,
# and it needs no adapter at all. Both checkpoints are already on this box.
#
# COST. Minutes. The run that raised the question cost 12.5 hours, which is the
# whole argument for asking the cheap question first.
#
# Never write "rc=$?" after a pipeline: a chain reported ALL DONE over a
# RuntimeError that way on 2026-09-06. Resumable by artifact.
set -uo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1

FP8="${FP8_MODEL:-/home/ubuntu/models/Qwen3.8-27B-FP8}"
BF16="${BF16_MODEL:-/home/ubuntu/models/Qwen3.8-27B-BF16}"
TAG="${FP8_TAG:-fp8-scale-probe-$(date +%Y%m%d)}"
ART="$R/ab_test_runtime/experiments/fp8_scale_probe__${TAG}.json"
PY="${FP8_PY:-python3}"

[ -s "$ART" ] && { echo "SKIP - artifact exists: $ART"; exit 0; }
# Name the checkpoints first and by name. A probe whose control is missing
# would run one arm and compare it against nothing.
for m in "$FP8" "$BF16"; do
    test -s "$m/config.json" || { echo "checkpoint missing: $m" >&2; exit 1; }
done

# Same flags the failing run used to reach the FP8 path, so this probe loads
# the checkpoint the same way rather than a differently-configured one.
export HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

echo "repo commit: $(git -C "$R" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "fp8:  $FP8"
echo "bf16: $BF16"

"$PY" -u app/experiments/fp8_scale_load_probe.py \
    --models "$FP8" "$BF16" --max-new-tokens 200 --out "$ART" \
    2>&1 | tee "/home/ubuntu/${TAG}.log"
rc=${PIPESTATUS[0]}

# The artifact, not the exit code (Rule 20). A probe that exits 0 having
# written nothing has measured nothing.
if [ "$rc" -ne 0 ] || [ ! -s "$ART" ]; then
    echo "FAILED rc=$rc artifact=$( [ -s "$ART" ] && echo present || echo absent )" >&2
    exit 1
fi
"$PY" - "$ART" <<'PYSUM'
import json, sys
doc = json.load(open(sys.argv[1]))
for arm in doc["arms"]:
    if not arm.get("loaded"):
        print("  %-46s DID NOT LOAD: %s" % (arm["model"].split("/")[-1],
                                            arm.get("error"))); continue
    print("  %-46s %-6s %4d/%d tokens  unplaced scales: %d" % (
        arm["model"].split("/")[-1], arm["finish_reason"],
        arm["tokens_produced"], arm["tokens_requested"],
        arm["unplaced_scale_count"]))
PYSUM
echo "ALL DONE $(date -Is)"
