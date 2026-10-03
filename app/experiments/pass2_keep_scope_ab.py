"""Score --pass2-keep-scope line vs batch (#668 follow-up) and apply its gate.

`batch` keeps a pass-2 batch whose only failures are keepable checks whole,
flagging just the refused lines, instead of halving it down to them. This
asks the two questions that decide it: does it save requests, and does it cost
accuracy? The scope only changes what happens AFTER a batch exhausts its
retries, so any accuracy difference comes from lines that `line` would have
re-asked in a smaller batch and `batch` kept from the full one.

Each arm is named NAME=RUNS_DIR:SUFFIX:LOGS_DIR, read as
RUNS_DIR/{book}__{SUFFIX}.json.threepass_checkpoint.json and LOGS_DIR/{book}__{SUFFIX}.log.
Two `line` arms (a fresh one and the #653 A/B's base arm, same settings) give
the run-to-run spread the gate compares against.

THE GATE, fixed before any run (2026-10-03, plan for this PR): `batch` passes if
  1. pooled model calls drop by at least 20% against the fresh `line` arm, AND
  2. pooled named accuracy (strict) is no lower than `line` minus the
     line-vs-line spread, and no more than 0.5 points lower in any case, AND
  3. pooled unnamed accuracy (strict) is no lower than `line`.
Unchecked lines are expected to rise and are reported, not gated.
"""
import argparse
import collections
import json
import os
import re
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.background_speakers import (                  # noqa: E402
    _normalize, get_gold_class, get_title_names, is_same_speaker, load_gold_index)
from experiments.provenance import provenance                   # noqa: E402
from generation_checkpoint_deltas import load_generation_delta_checkpoint  # noqa: E402

REPO = os.path.dirname(APP)
EXPERIMENTS = os.path.join(REPO, "ab_test_runtime", "experiments")
PRICE_IN, PRICE_OUT = 0.66e-6, 1.98e-6        # DeepSeek v4-pro, as the run scripts use
GATE = {"min_call_drop_pct": 20.0, "max_named_drop_pts": 0.5}
CLASSES = ("named", "descriptive", "title_name")


def get_log_counts(path):
    """Model responses, their cost, subdivisions and kept batches from one run log."""
    calls = prompt = completion = subdivisions = kept_batches = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = re.search(r"prompt=(\d+) completion=(\d+)", line)
            if match:
                calls += 1
                prompt += int(match.group(1))
                completion += int(match.group(2))
            subdivisions += "Attribution batch exhausted; subdividing" in line
            kept_batches += "Attribution exhausted; kept the model's last answer" in line
    return {"calls": calls, "cost_usd": round(prompt * PRICE_IN + completion * PRICE_OUT, 4),
            "subdivisions": subdivisions, "kept_batches": kept_batches}


def score_run(checkpoint_path, gold, book, gold_index, title_names):
    """Strict accuracy by gold class, plus the lines this run kept unchecked."""
    checkpoint = load_generation_delta_checkpoint(checkpoint_path)
    named = [e for e in checkpoint.get("named") or [] if e]
    if len(named) != len(checkpoint.get("segmented") or []):
        raise ValueError(f"{checkpoint_path}: pass 2 incomplete")
    occurrences = collections.Counter(_normalize(e.get("text")) for e in checkpoint["segmented"])
    by_text = {}
    for entry in named:
        by_text.setdefault(_normalize(entry.get("text")), entry)
    aliases, labels = gold_index["aliases"][book], gold_index["labels"][book]
    counts = {cls: collections.Counter() for cls in CLASSES}
    unchecked = collections.Counter()
    for entry in gold["entries"]:
        key = _normalize(entry["line"])
        if occurrences.get(key) != 1 or key not in by_text:
            continue
        expected = entry["expected_speaker"].upper().strip()
        answer = by_text[key]
        correct = is_same_speaker(answer.get("speaker"), expected, aliases, labels)
        gold_class = get_gold_class(book, expected, gold_index, title_names)
        gold_class = "named" if gold_class == "descriptor_of_named" else gold_class
        counts[gold_class]["n"] += 1
        counts[gold_class]["correct"] += correct
        if answer.get("attribution_unchecked"):
            unchecked["n"] += 1
            unchecked["correct"] += correct
    return {"accuracy": {cls: dict(c) for cls, c in counts.items()},
            "unchecked_gold_lines": dict(unchecked),
            "unchecked_lines": sum(1 for e in named if e.get("attribution_unchecked")),
            "unknown_lines": sum(1 for e in named
                                 if (e.get("speaker") or "").upper() == "UNKNOWN")}


def get_pct(counter):
    return round(100 * counter.get("correct", 0) / counter["n"], 2) if counter.get("n") else None


def get_summary(rows, arms):
    summary = {}
    for arm in arms:
        pooled = {cls: collections.Counter() for cls in CLASSES}
        totals = collections.Counter()
        for row in (r for r in rows if r["arm"] == arm):
            for cls in CLASSES:
                pooled[cls].update(row["accuracy"].get(cls, {}))
            for key in ("calls", "cost_usd", "subdivisions", "kept_batches",
                        "unchecked_lines", "unknown_lines"):
                totals[key] += row[key]
        summary[arm] = {**{f"{cls}_pct": get_pct(pooled[cls]) for cls in CLASSES},
                        **{f"{cls}_n": pooled[cls]["n"] for cls in CLASSES},
                        **{k: round(v, 4) if isinstance(v, float) else v
                           for k, v in totals.items()}}
    return summary


def get_gate(summary, candidate, baseline, repeat):
    """The pre-registered verdict, with every number it used."""
    base, cand, rep = summary[baseline], summary[candidate], summary.get(repeat)
    call_drop = round(100 * (base["calls"] - cand["calls"]) / base["calls"], 1)
    spread = round(abs(base["named_pct"] - rep["named_pct"]), 2) if rep else None
    allowed = min(spread if spread is not None else 0.0, GATE["max_named_drop_pts"])
    named_delta = round(cand["named_pct"] - base["named_pct"], 2)
    unnamed_delta = round(cand["descriptive_pct"] - base["descriptive_pct"], 2)
    checks = {"calls_drop_pct": call_drop,
              "calls_pass": call_drop >= GATE["min_call_drop_pct"],
              "named_delta_pts": named_delta, "line_vs_line_spread_pts": spread,
              "named_allowed_drop_pts": allowed, "named_pass": named_delta >= -allowed,
              "unnamed_delta_pts": unnamed_delta, "unnamed_pass": unnamed_delta >= 0}
    checks["passes"] = checks["calls_pass"] and checks["named_pass"] and checks["unnamed_pass"]
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fixtures", default=os.path.join(APP, "fixtures"))
    parser.add_argument("--books", nargs="+", required=True)
    parser.add_argument("--arm", action="append", required=True,
                        help="NAME=RUNS_DIR:SUFFIX:LOGS_DIR, repeatable")
    parser.add_argument("--candidate", default="batch")
    parser.add_argument("--baseline", default="line")
    parser.add_argument("--repeat", default="line_repeat")
    parser.add_argument("--output", default=os.path.join(EXPERIMENTS, "pass2_keep_scope_ab.json"))
    args = parser.parse_args(argv)

    gold_index, title_names = load_gold_index(args.fixtures), get_title_names()
    arms = {}
    for spec in args.arm:
        name, rest = spec.split("=", 1)
        runs, suffix, logs = rest.split(":")
        arms[name] = (runs, suffix, logs)
    rows = []
    for book in args.books:
        with open(os.path.join(args.fixtures, f"attribution_gold_{book}.json"),
                  encoding="utf-8") as handle:
            gold = json.load(handle)
        for name, (runs, suffix, logs) in arms.items():
            row = {"book": book, "arm": name,
                   **get_log_counts(os.path.join(logs, f"{book}__{suffix}.log")),
                   **score_run(os.path.join(runs, f"{book}__{suffix}.json.threepass_checkpoint.json"),
                               gold, book, gold_index, title_names)}
            rows.append(row)
    summary = get_summary(rows, list(arms))
    doc = {"gate_definition": GATE, "rows": rows, "summary": summary,
           "gate": get_gate(summary, args.candidate, args.baseline, args.repeat),
           "provenance": provenance(__file__, args)}
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1)
    print(json.dumps({"summary": summary, "gate": doc["gate"]}, indent=1))


if __name__ == "__main__":
    main()
