"""Adjudicate Qwen/ModernBookNLP disagreements by full-name likelihood."""
import argparse
import json
import os
import sys

import requests

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.full_sequence_scoring import LlamaSequenceScorer  # noqa: E402
from experiments.scoring import normalize  # noqa: E402
from utils import atomic_json_write  # noqa: E402


PROMPT = """Identify who speaks THE LINE. Use the surrounding evidence. The
answer must be exactly one of these candidates:
{candidates}

Text before:
{prev}

THE LINE:
{line}

Text after:
{next}

Answer with one candidate name and nothing else."""


def load_rows(path, arm=None):
    document = json.load(open(path, encoding="utf-8"))
    return [row for row in document.get("rows", [])
            if arm is None or row.get("arm") == arm]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qwen", required=True)
    parser.add_argument("--qwen-arm", default="evidence")
    parser.add_argument("--modern", required=True, nargs="+")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--model", default="qwen3-14b")
    parser.add_argument("--method", choices=("likelihood", "generation"),
                        default="likelihood")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    qwen = {row["id"]: row for row in load_rows(args.qwen, args.qwen_arm)}
    systems = [qwen]
    for path in args.modern:
        systems.append({row["id"]: row for row in load_rows(path)})
    bundle = json.load(open(args.bundle, encoding="utf-8"))
    contexts = {}
    for entry in bundle["entries"]:
        for book in bundle["books"]:
            if entry["id"].startswith(book + "-"):
                contexts[book + ":" + entry["id"]] = entry
                break

    scorer = LlamaSequenceScorer(requests.Session(), args.base_url, args.model)
    rows = []
    for row_id, qrow in qwen.items():
        predictions = []
        correctness = {}
        for system in systems:
            source = system.get(row_id)
            if not source or not source.get("predicted"):
                continue
            prediction = source["predicted"]
            key = normalize(prediction)
            if key not in {normalize(value) for value in predictions}:
                predictions.append(prediction)
            correctness[key] = correctness.get(key, False) or bool(source.get("correct"))
        if len(predictions) < 2 or row_id not in contexts:
            continue
        context = contexts[row_id]
        prompt = PROMPT.format(candidates="\n".join("- " + p for p in predictions),
                               prev=context.get("prev_context", ""),
                               line=context.get("line", ""),
                               next=context.get("next_context", ""))
        ranking = None
        if args.method == "likelihood":
            ranking = scorer.rank(prompt, predictions)
            winner = ranking[0]["candidate"]
        else:
            result = scorer._post("/v1/chat/completions", {
                "model": args.model, "temperature": 0.0, "max_tokens": 32,
                "messages": [{"role": "user", "content": prompt}],
            })
            raw = result["choices"][0]["message"]["content"].strip()
            matches = [candidate for candidate in predictions
                       if normalize(candidate) == normalize(raw)]
            winner = matches[0] if len(matches) == 1 else qrow.get("predicted")
        rows.append({"id": row_id, "expected": qrow.get("expected"),
                     "predicted": winner,
                     "correct": correctness.get(normalize(winner), False),
                     "qwen_correct": bool(qrow.get("correct")),
                     "ranking": ranking,
                     "method": args.method})
        if len(rows) % 10 == 0:
            atomic_json_write({"status": "running", "rows": rows}, args.out)
        if args.limit and len(rows) >= args.limit:
            break
    gains = sum(row["correct"] and not row["qwen_correct"] for row in rows)
    losses = sum(row["qwen_correct"] and not row["correct"] for row in rows)
    document = {"status": "complete", "method": args.method,
                "scope": "system disagreements only",
                "summary": {"n": len(rows), "correct": sum(r["correct"] for r in rows),
                            "qwen_correct": sum(r["qwen_correct"] for r in rows),
                            "gains": gains, "losses": losses}, "rows": rows}
    atomic_json_write(document, args.out)
    print(json.dumps(document["summary"], indent=1))


if __name__ == "__main__":
    main()
