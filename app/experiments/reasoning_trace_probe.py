"""What does the model deliberate about when it gets a speaker wrong?

Reads a two_stage_attribution artifact whose raw responses carry the
model's <think> block (the --reasoning runs keep reasoning_content) and
asks, without inference:

- does deliberation length predict a wrong answer (calibration by
  think-length decile);
- which hesitation words ("Wait", "Alternatively", "not sure") separate
  wrong from right;
- which failure SHAPES the wrong traces show, by regex over the trace:
  the model treats the line as narration, reasons from a character's
  personality instead of the passage, confuses the addressee with the
  speaker, follows an alternation assumption, or reads the previous /
  next speaker off the wrong line.

Every category is a regex over 1,200 traces hand-checked on the samples the
script prints; the counts are exact, the category names are inferences.
"""
import argparse
import collections
import json
import os
import re
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

HESITATION = {
    "wait": r"\bwait\b", "hmm": r"\bhmm\b", "alternatively": r"\balternativ",
    "not sure": r"not (entirely |really |completely )?sure|unclear|ambiguous|hard to (say|tell)",
    "but": r"\bbut\b", "maybe": r"\bmaybe\b|\bperhaps\b|\bmight\b|\bcould be\b",
}
SHAPES = {
    "treats line as narration": r"narrat(ive|ion|or)\b.{0,60}(not|isn't|rather than) (a )?(dialogue|speech|spoken)|seems to be (a )?narrat|is narration|part of the narrati",
    "personality prior": r"known for|is (the )?(proud|arrogant|kind|shy|type)|character(istic)? of|would (say|typically)|in character for|personality",
    "addressee for speaker": r"(addressed|speaking|talking|said) to \w|being addressed|the addressee|is being spoken to",
    "alternation assumption": r"alternat|turn[- ]taking|back and forth|the (other|previous) speaker (was|is)|so the next (line|speaker)",
    "neighbour misread": r"(previous|next|preceding|following) (line|sentence|speech|dialogue).{0,80}(said|spoke|says) by",
    "no cue found": r"no (clear |explicit |direct )?(tag|attribution|indication|mention)|doesn't (say|specify|indicate) who|not explicitly",
}


def think_of(row):
    raw = row.get("raw_response") or ""
    m = re.search(r"<think>(.*?)</think>", raw, re.S)
    return (m.group(1) if m else raw).strip()


def main():
    from experiments.provenance import provenance
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--samples", type=int, default=3, help="wrong traces to print per shape")
    args = ap.parse_args()
    rows = json.load(open(args.artifact, encoding="utf-8"))["rows"]
    for r in rows:
        r["_think"] = think_of(r)
        r["_len"] = len(r["_think"])
        r["_correct"] = str(r.get("correct")) == "True"
    wrong = [r for r in rows if not r["_correct"]]
    right = [r for r in rows if r["_correct"]]
    out = {"rows": len(rows), "wrong": len(wrong),
           "think_chars_median": {"wrong": statistics.median(r["_len"] for r in wrong),
                                  "right": statistics.median(r["_len"] for r in right)}}
    # calibration by length decile
    ordered = sorted(rows, key=lambda r: r["_len"])
    deciles = []
    for i in range(10):
        chunk = ordered[i * len(ordered) // 10:(i + 1) * len(ordered) // 10]
        if chunk:
            deciles.append({"decile": i + 1, "max_chars": chunk[-1]["_len"],
                            "accuracy": round(sum(r["_correct"] for r in chunk) / len(chunk), 3), "n": len(chunk)})
    out["accuracy_by_think_length_decile"] = deciles
    # hesitation words
    hes = {}
    for name, rx in HESITATION.items():
        cw = sum(1 for r in wrong if re.search(rx, r["_think"], re.I))
        cr = sum(1 for r in right if re.search(rx, r["_think"], re.I))
        hes[name] = {"wrong_share": round(cw / max(1, len(wrong)), 3), "right_share": round(cr / max(1, len(right)), 3)}
    out["hesitation"] = hes
    # failure shapes
    shapes = {}
    samples = {}
    for name, rx in SHAPES.items():
        hits_w = [r for r in wrong if re.search(rx, r["_think"], re.I)]
        hits_r = [r for r in right if re.search(rx, r["_think"], re.I)]
        shapes[name] = {"wrong": len(hits_w), "wrong_share": round(len(hits_w) / max(1, len(wrong)), 3),
                        "right_share": round(len(hits_r) / max(1, len(right)), 3),
                        "wrong_accuracy_when_present": round(len(hits_w) / max(1, len(hits_w) + len(hits_r)), 3)}
        samples[name] = [{"expected": r["expected"], "predicted": r["predicted"], "line": r["line"][:100],
                          "trace": r["_think"][:700]} for r in hits_w[:args.samples]]
    out["failure_shapes"] = shapes
    out["samples"] = samples
    # which wrong answer: UNKNOWN vs a lead vs another name
    top = collections.Counter(r["expected"] for r in rows).most_common(3)
    leads = {n for n, _ in top}
    out["wrong_pick"] = {"UNKNOWN": sum(1 for r in wrong if (r["predicted"] or "").upper() == "UNKNOWN"),
                         "a_frequent_speaker": sum(1 for r in wrong if (r["predicted"] or "").upper() in leads),
                         "other_name": sum(1 for r in wrong if (r["predicted"] or "").upper() not in leads | {"UNKNOWN"})}
    out["provenance"] = provenance(__file__, args)
    json.dump(out, open(args.out, "w", encoding="utf-8"), indent=1)
    print(json.dumps({k: out[k] for k in ("rows", "wrong", "think_chars_median", "accuracy_by_think_length_decile", "hesitation", "failure_shapes", "wrong_pick")}, indent=1))
    for name, ss in samples.items():
        print(f"\n=== {name}")
        for s in ss[:2]:
            print(f"  [{s['expected']} -> {s['predicted']}] {s['line']!r}\n    {s['trace'][:500]!r}")


if __name__ == "__main__":
    main()
