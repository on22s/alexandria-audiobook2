"""Selection-side PDNC pilot using profiles seeded by explicit baseline quotes."""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.scoring import same_speaker, alias_groups
from experiments.stats import paired
from utils import atomic_json_write

GENERIC = ("A ", "AN ", "THE ", "UNKNOWN")


def select(entries, baseline_rows, margin=0.05):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    baseline = {}
    for row in baseline_rows:
        if row["arm"] != "baseline":
            continue
        baseline[row["id"]] = row
        baseline[row["id"].split(":", 1)[-1]] = row
    profiles = {}
    for entry in entries:
        row = baseline.get(entry["id"])
        if (row and entry.get("quote_type") == "Explicit" and row.get("predicted")
                and not str(row["predicted"]).upper().startswith(GENERIC)):
            profiles.setdefault(row["predicted"], []).append(entry["line"])
    names, documents = zip(*[(name, "\n".join(lines)) for name, lines in profiles.items()])
    vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(3, 5), min_df=1)
    matrix = vectorizer.fit_transform(documents)
    output = []
    for entry in entries:
        row = baseline[entry["id"]]
        prediction, reason = row["predicted"], "baseline_default"
        if str(prediction or "UNKNOWN").upper().startswith(GENERIC):
            scores = cosine_similarity(vectorizer.transform([entry["line"]]), matrix)[0]
            order = scores.argsort()[::-1]
            if len(order) and (len(order) == 1 or scores[order[0]] - scores[order[1]] >= margin):
                prediction, reason = names[order[0]], "style_profile_override"
        output.append({"id": entry["id"], "expected": entry["expected_speaker"],
                       "baseline": row["predicted"], "predicted": prediction,
                       "baseline_correct": bool(row["correct"]),
                       "reason": reason, "candidates": row.get("candidates", [])})
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    artifact = json.load(open(args.baseline, encoding="utf-8"))
    entries = json.load(open(args.inputs, encoding="utf-8"))["entries"]
    rows = select(entries, artifact["rows"])
    aliases = alias_groups()
    base = {r["id"]: r["baseline_correct"] for r in rows}
    arm = {r["id"]: (r["baseline_correct"] if r["reason"] == "baseline_default"
                     else same_speaker(r["predicted"], r["expected"], aliases)) for r in rows}
    p, lost, gained, n = paired(base, arm)
    summary = {"n": n, "baseline_correct": sum(base.values()), "style_correct": sum(arm.values()),
               "delta_points": 100 * (sum(arm.values()) - sum(base.values())) / n,
               "gained": gained, "lost": lost, "p_value": p,
               "overrides": sum(r["reason"] != "baseline_default" for r in rows),
               "advance": (100 * (sum(arm.values()) - sum(base.values())) / n >= 3 and p < .05)}
    atomic_json_write({"summary": summary, "rows": rows}, args.out)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
