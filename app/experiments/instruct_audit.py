"""What pass 3 actually writes into `instruct`, audited against the per-line
instruct rules ported in instruct_lexicon (buddies fork, MIT).

The fork's claim, measured on the CustomVoice path there and not yet here:
only delivery language (emotion / pacing / rate) moves the performance; timbre
and identity words belong in the constant character style; actions and scene
notes are wasted tokens. Before deciding whether to adopt the rules - or to
measure their acoustic effect with voice_drift.py - the cheap question is how
often our own scripts break them. This runs over every script on disk with
instructs (the saved library, the active script, the committed three-pass
artifacts) and reports, per source and per generating model where known, the
share of instructs that carry each finding, the length distribution, and
examples. CPU only; no model is called.

Read the numbers with the instrument's limits in mind (test_instruct_audit
pins them): on this project's English instructs `no_delivery` fires on lines
that plainly carry delivery language the fork's lexicon lacks ("smooth
delivery", "quick, precise reading"), and `punctuation` fires on any
apostrophe. `timbre`, `over_specified` and `long` are the readings to trust.
"""
import argparse
import collections
import glob
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.instruct_lexicon import audit_instruct, count_clauses  # noqa: E402

SOURCES = (
    ("library", "scripts/*.json"),
    ("active", "annotated_script.json"),
    ("three_pass_artifacts", "ab_test_runtime/pipeline_repeats/run*.json"),
    ("three_pass_artifacts", "ab_test_runtime/three_pass_vs_single/*__three_pass.json"),
    ("three_pass_artifacts", "ab_test_runtime/cloud_e2e/*.json"),
)


def load_entries(path):
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data if isinstance(data, list) else (data.get("entries") if isinstance(data, dict) else None)
    return [e for e in (entries or []) if isinstance(e, dict) and e.get("instruct")]


def model_for(path):
    sidecar = path + ".generation_quality.json"
    if os.path.exists(sidecar):
        try:
            return json.load(open(sidecar, encoding="utf-8")).get("model_name") or "unknown"
        except (OSError, ValueError):
            return "unknown"
    try:
        data = json.load(open(path, encoding="utf-8"))
        if isinstance(data, dict):
            return (data.get("meta") or {}).get("model") or data.get("model_name") or "unknown"
    except (OSError, ValueError):
        pass
    return "unknown"


def audit_file(path, source, examples, max_examples=4):
    entries = load_entries(path)
    if not entries:
        return None
    model = model_for(path)
    codes = collections.Counter()
    words, clauses = [], []
    for entry in entries:
        text = str(entry["instruct"])
        findings = audit_instruct(text, str(entry.get("speaker") or ""))
        seen = {f["code"] for f in findings}
        codes.update(seen)
        if not seen:
            codes["conformant"] += 1
        words.append(len(text.split()))
        clauses.append(count_clauses(text))
        for f in findings:
            bucket = examples.setdefault(f["code"], [])
            if len(bucket) < max_examples and text not in [b["instruct"] for b in bucket]:
                bucket.append({"instruct": text[:160], "detail": f["detail"][:120],
                               "file": os.path.basename(path), "model": model})
    return {"file": os.path.basename(path), "source": source, "model": model,
            "instructs": len(entries), "codes": dict(codes),
            "mean_words": round(statistics.mean(words), 2),
            "p90_words": sorted(words)[int(0.9 * (len(words) - 1))],
            "mean_clauses": round(statistics.mean(clauses), 2),
            "share_over_3_clauses": round(sum(c > 3 for c in clauses) / len(clauses), 4)}


def summarize(rows, key):
    groups = collections.defaultdict(lambda: {"instructs": 0, "codes": collections.Counter(), "files": 0})
    for row in rows:
        g = groups[row[key]]
        g["instructs"] += row["instructs"]
        g["files"] += 1
        g["codes"].update(row["codes"])
    out = {}
    for name, g in groups.items():
        n = g["instructs"]
        out[name] = {"files": g["files"], "instructs": n,
                     "share": {code: round(count / n, 4) for code, count in sorted(g["codes"].items())}}
    return out


def main():
    from experiments.provenance import provenance
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", default=REPO, help="checkout whose scripts/ and ab_test_runtime/ to read")
    args = ap.parse_args()
    rows, examples = [], {}
    for source, pattern in SOURCES:
        for path in sorted(glob.glob(os.path.join(args.repo, pattern))):
            if path.endswith((".voice_config.json", ".generation_quality.json")) or "threepass" in path:
                continue
            row = audit_file(path, source, examples)
            if row:
                rows.append(row)
    by_source = summarize(rows, "source")
    by_model = summarize(rows, "model")
    total = sum(r["instructs"] for r in rows)
    print(f"{len(rows)} files, {total} instructs")
    for name, g in by_source.items():
        print(f"\n{name}: {g['files']} files, {g['instructs']} instructs")
        for code, share in sorted(g["share"].items(), key=lambda kv: -kv[1]):
            print(f"   {code:15s} {100 * share:5.1f}%")
    print("\nby model:")
    for name, g in sorted(by_model.items(), key=lambda kv: -kv[1]["instructs"]):
        s = g["share"]
        print(f"   {name[:40]:40s} n={g['instructs']:6d}  conformant {100 * s.get('conformant', 0):5.1f}%  "
              f"timbre {100 * s.get('timbre', 0):4.1f}%  no_delivery {100 * s.get('no_delivery', 0):4.1f}%  "
              f"over_specified {100 * s.get('over_specified', 0):4.1f}%  long {100 * s.get('long', 0):4.1f}%")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"experiment": "instruct_audit", "rules": "experiments.instruct_lexicon (buddies fork, MIT)",
                   "files": rows, "by_source": by_source, "by_model": by_model, "examples": examples,
                   "total_instructs": total, "provenance": provenance(__file__, args)}, fh, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
