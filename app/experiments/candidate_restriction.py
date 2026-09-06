#!/usr/bin/env python3
"""What does restricting the candidate roster cost in recall?

The lead from arXiv 2307.03734 is that narrowing the candidate set is worth
0.40 -> 0.78. Before spending LLM time, measure offline what each narrowing
rule COSTS: if the right speaker falls out of the set, no amount of better
selection can recover them.
"""
import sys, json, glob, re, statistics, collections
sys.path.insert(0, "app/experiments"); sys.path.insert(0, "app")
from experiments.two_stage_attribution import roster_lines

fx = sorted(glob.glob("app/fixtures/attribution_gold_pdnc_*_w3200.json"))
print(f"fixtures: {len(fx)}", flush=True)
agg = collections.defaultdict(lambda: [[], 0, 0])

for f in fx:
    d = json.load(open(f))
    ents = d.get("entries", [])
    lines = roster_lines(d)
    forms = {}
    for l in lines:
        head = l.split(" [also:")[0]
        rest = re.findall(r"\[also: (.+)\]", l)
        forms[head] = [head] + ([x.strip() for x in rest[0].split(",")] if rest else [])
    # ONE compiled pattern per character, reused for every row. The first
    # version rebuilt these inside the row loop and re-scanned the whole book
    # per character per row - 74 x 2494 full-text regex scans, which never
    # finished.
    pat = {c: re.compile("|".join(r"\b" + re.escape(n.upper()) + r"\b" for n in ns))
           for c, ns in forms.items()}
    book = " ".join(((e.get("prev_context") or "") + " " + (e.get("line") or "")
                     + " " + (e.get("next_context") or "")) for e in ents).upper()
    in_book = [c for c, p in pat.items() if p.search(book)]
    cast = list(forms)

    for e in ents:
        exp = (e.get("expected_speaker") or "").upper()
        ctx = ((e.get("prev_context") or "") + " " + (e.get("line") or "")
               + " " + (e.get("next_context") or "")).upper()
        local = [c for c, p in pat.items() if p.search(ctx)]
        for label, cand in (("full roster (what runs today)", cast),
                            ("named anywhere in the book", in_book),
                            ("named in this quote's own context", local)):
            hit = any(exp == n.upper() for c in cand for n in forms[c])
            a = agg[label]
            a[0].append(len(cand)); a[1] += hit; a[2] += 1

print(f"\n{'rule':38} {'median cands':>13} {'recall':>8}", flush=True)
print("-" * 62, flush=True)
for label, (sizes, ok, tot) in agg.items():
    print(f"{label:38} {statistics.median(sizes):13.0f} {100*ok/tot:7.1f}%", flush=True)
full = agg["full roster (what runs today)"]
local = agg["named in this quote's own context"]
break_even = 0.656 / (local[1] / local[2])
print(f"\nquotes: {full[2]}", flush=True)
print(f"\nPRE-REGISTERED THRESHOLD. Today's arm reads 0.656 over these rows with"
      f"\nall {statistics.median(full[0]):.0f} candidates. Restricting to the local"
      f" set keeps the right\nspeaker {100*local[1]/local[2]:.1f}% of the time, so the"
      f" restricted run beats today only if\nit picks correctly on more than"
      f" {100*break_even:.1f}% of the rows it retains.", flush=True)

import os
out = os.path.join("ab_test_runtime", "experiments", "candidate_restriction.json")
doc = {
    "note": "What each candidate-restriction rule costs in RECALL, measured "
            "offline before any LLM run. A speaker who falls out of the "
            "candidate set cannot be recovered by better selection.",
    "why": "arXiv 2307.03734 reports 0.40 end-to-end against 0.78 once "
           "candidates are restricted to coreference-resolved mentions - the "
           "largest method effect in external_comparability.json. This asks "
           "what the same idea costs here, using name matching rather than a "
           "coreference model.",
    "quotes": full[2],
    "rules": [{"rule": k, "median_candidates": statistics.median(v[0]),
               "recall": round(v[1] / v[2], 4)} for k, v in agg.items()],
    "current_accuracy_full_roster": 0.656,
    "break_even_accuracy_on_retained_rows": round(break_even, 4),
    "limitations": [
        "Three PDNC books, 2,494 quotes. English only.",
        "Name matching, not coreference: a speaker referred to only by pronoun "
        "in the window is dropped, which is most of the 7.3 points lost.",
        "Recall is an upper bound on what restriction can achieve, not a "
        "prediction that selection improves at all.",
    ],
}
try:
    sys.path.insert(0, os.path.join("app", "experiments"))
    from provenance import provenance
    doc["provenance"] = provenance(__file__, {})
except Exception as exc:
    doc["provenance"] = {"error": str(exc)[:120]}
os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump(doc, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print(f"\nwrote {out}", flush=True)
