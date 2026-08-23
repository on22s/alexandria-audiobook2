"""Frozen PDNC pilot using the published UAR_scene character representation."""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.scoring import alias_groups, same_speaker
from experiments.stats import paired
from utils import atomic_json_write

GENERIC = ("A ", "AN ", "THE ", "UNKNOWN")


def is_generic(name):
    return str(name or "UNKNOWN").upper().startswith(GENERIC)


def get_profiles(entries, baseline):
    profiles = {}
    for entry in entries:
        row = baseline[entry["id"]]
        if entry.get("quote_type") == "Explicit" and not is_generic(row.get("predicted")):
            profiles.setdefault(row["predicted"], []).append(entry["line"])
    return profiles


def main():
    import torch
    import torch.nn.functional as functional
    from transformers import AutoModel, AutoTokenizer

    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--margin", type=float, default=0.05)
    parser.add_argument("--revision", default="89713b0911254c6c40d3211a7a4380fbe5f9d127")
    args = parser.parse_args()
    artifact = json.load(open(args.baseline, encoding="utf-8"))
    entries = json.load(open(args.inputs, encoding="utf-8"))["entries"]
    baseline = {}
    for row in artifact["rows"]:
        if row["arm"] == "baseline":
            baseline[row["id"].split(":", 1)[-1]] = row
    profiles = get_profiles(entries, baseline)
    tokenizer = AutoTokenizer.from_pretrained("gasmichel/UAR_scene", revision=args.revision,
                                               trust_remote_code=True)
    model = AutoModel.from_pretrained("gasmichel/UAR_scene", revision=args.revision,
                                      trust_remote_code=True).to(args.device).eval()

    names, vectors = [], []
    with torch.inference_mode():
        for name, quotes in profiles.items():
            tokens = tokenizer(quotes[:8], max_length=64, truncation=True,
                               padding="max_length", return_tensors="pt")
            vector = model(tokens.input_ids[None].to(args.device),
                           tokens.attention_mask[None].to(args.device))
            names.append(name)
            vectors.append(functional.normalize(vector, dim=-1)[0].cpu())
        matrix = torch.stack(vectors)

        rows = []
        for entry in entries:
            base = baseline[entry["id"]]
            prediction, reason = base["predicted"], "baseline_default"
            if is_generic(prediction):
                tokens = tokenizer([entry["line"]], max_length=64, truncation=True,
                                   padding="max_length", return_tensors="pt")
                query = model(tokens.input_ids[None].to(args.device),
                              tokens.attention_mask[None].to(args.device))
                scores = matrix @ functional.normalize(query, dim=-1)[0].cpu()
                order = torch.argsort(scores, descending=True)
                candidates = set(base.get("candidates") or [])
                order = [int(i) for i in order if not candidates or names[int(i)] in candidates]
                if order and (len(order) == 1 or scores[order[0]] - scores[order[1]] >= args.margin):
                    prediction, reason = names[order[0]], "luar_profile_override"
            rows.append({"id": entry["id"], "expected": entry["expected_speaker"],
                         "baseline": base["predicted"], "predicted": prediction,
                         "baseline_correct": bool(base["correct"]), "reason": reason})

    aliases = alias_groups()
    base_scores = {r["id"]: r["baseline_correct"] for r in rows}
    arm_scores = {r["id"]: (r["baseline_correct"] if r["reason"] == "baseline_default"
                            else same_speaker(r["predicted"], r["expected"], aliases)) for r in rows}
    p_value, lost, gained, n = paired(base_scores, arm_scores)
    summary = {"n": n, "baseline_correct": sum(base_scores.values()),
               "luar_correct": sum(arm_scores.values()), "gained": gained, "lost": lost,
               "p_value": p_value, "overrides": sum(r["reason"] != "baseline_default" for r in rows),
               "advance": gained > lost and p_value < 0.05, "model_revision": args.revision}
    atomic_json_write({"summary": summary, "rows": rows}, args.out)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
