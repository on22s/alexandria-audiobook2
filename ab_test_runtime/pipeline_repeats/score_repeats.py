"""Score every pipeline repeat and report the run-level distribution."""
import json, glob, os, statistics, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from tools.audit.pipeline_repeat_scoring import get_pipeline_repeat_scores
sys.path.insert(0, os.path.join(ROOT, "app"))
from generation_checkpoint_deltas import load_generation_delta_checkpoint
D = ROOT + "/ab_test_runtime/pipeline_repeats"
gold = json.load(open(ROOT + "/app/fixtures/attribution_gold_grimgar03_provisional.json"))

runs = {}
for f in sorted(glob.glob(D + "/run*.json.threepass_checkpoint.json")):
    tag = os.path.basename(f).split(".")[0]
    d = load_generation_delta_checkpoint(f); seg = d["segmented"]
    scored = get_pipeline_repeat_scores(d, gold)["scored"]
    runs[tag] = (scored, len(seg))

if not runs:
    raise SystemExit("no completed repeats to score")
print(f"\n{'run':8} {'segments':>9} {'scored':>7} {'correct':>8} {'accuracy':>9}")
accs = []
for tag, (s, nseg) in sorted(runs.items()):
    a = sum(s.values()) / len(s) * 100
    accs.append(a)
    print(f"{tag:8} {nseg:9} {len(s):7} {sum(s.values()):8} {a:8.2f}%")

print(f"\nRUN-LEVEL DISTRIBUTION over {len(accs)} runs")
print(f"  mean {statistics.mean(accs):.2f}%   SD {statistics.stdev(accs) if len(accs)>1 else 0:.2f} pt"
      f"   range {min(accs):.2f} - {max(accs):.2f} ({max(accs)-min(accs):.2f} pt)")
if len(accs) > 1:
    sd = statistics.stdev(accs)
    print(f"  two independent runs differ by up to ~{1.96*sd*(2**0.5):.1f} pt at 95% "
          f"(1.96 x SD x sqrt(2))")
    print(f"  section 6.3's decomposition-vs-pipeline gap was 5.5 pt")

# per-line churn between every pair
ids = set.intersection(*[set(s) for s, _ in runs.values()])
tags = sorted(runs)
churn = []
for i in range(len(tags)):
    for j in range(i + 1, len(tags)):
        a, b = runs[tags[i]][0], runs[tags[j]][0]
        churn.append(sum(1 for k in ids if a[k] != b[k]) / len(ids) * 100)
if churn:
    print(f"\nPER-LINE CHURN across {len(churn)} run pairs on {len(ids)} shared lines")
    print(f"  mean {statistics.mean(churn):.1f}%   range {min(churn):.1f} - {max(churn):.1f}%")
    print(f"  (the single pair measured earlier gave 17.9%)")
