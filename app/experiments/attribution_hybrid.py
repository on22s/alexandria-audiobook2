"""Select and evaluate a Qwen + ModernBookNLP speaker-attribution hybrid.

The pilot artifact chooses one policy.  The evaluation artifact may only apply
that frozen policy; it cannot tune a threshold on evaluation labels.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.scoring import alias_groups, same_speaker  # noqa: E402
from experiments.stats import exact_mcnemar  # noqa: E402


def index_rows(rows):
    """Return rows keyed by ID, refusing ambiguous inputs."""
    out = {}
    for row in rows:
        row_id = row.get("id")
        if not row_id:
            raise ValueError("every prediction row needs an id")
        if row_id in out:
            raise ValueError("duplicate prediction id: %s" % row_id)
        out[row_id] = row
    return out


def choose_prediction(qwen, modern, policy):
    """Return ``(prediction, source)`` under one frozen policy."""
    qpred = qwen.get("predicted")
    mpred = modern.get("predicted")
    if policy["kind"] == "qwen":
        return qpred, "qwen"
    if policy["kind"] == "modern":
        return mpred, "modern"
    if policy["kind"] == "quote_type":
        if modern.get("quote_type") in policy["modern_types"] and mpred:
            return mpred, "modern"
        return qpred, "qwen"
    if policy["kind"] == "modern_confidence":
        confidence = modern.get("confidence")
        if mpred and confidence is not None and confidence >= policy["threshold"]:
            return mpred, "modern"
        return qpred, "qwen"
    if policy["kind"] == "narrator_aware":
        if modern.get("narrator"):
            return qpred, "qwen"
        return (mpred, "modern") if mpred else (qpred, "qwen")
    raise ValueError("unknown hybrid policy: %s" % policy["kind"])


def evaluate(qwen_rows, modern_rows, policy, groups=()):
    """Evaluate on every Qwen row; missing Modern rows are explicit abstentions."""
    qidx, midx = index_rows(qwen_rows), index_rows(modern_rows)
    shared = set(qidx) & set(midx)
    rows = []
    for row_id in sorted(qidx):
        qwen = qidx[row_id]
        modern = midx.get(row_id, {"id": row_id, "expected": qwen.get("expected"),
                                  "predicted": None, "confidence": None})
        if row_id in midx and qwen.get("expected") != modern.get("expected"):
            raise ValueError("gold disagreement for %s" % row_id)
        predicted, source = choose_prediction(qwen, modern, policy)
        expected = qwen.get("expected")
        qwen_correct = (bool(qwen["correct"]) if "correct" in qwen else
                        bool(qwen.get("predicted")) and same_speaker(
                            expected, qwen.get("predicted"), groups))
        modern_correct = (bool(modern["correct"]) if "correct" in modern else
                          bool(modern.get("predicted")) and same_speaker(
                              expected, modern.get("predicted"), groups))
        rows.append({
            "id": row_id, "expected": expected, "predicted": predicted,
            "source": source,
            "correct": modern_correct if source == "modern" else qwen_correct,
            "qwen_correct": qwen_correct, "modern_correct": modern_correct,
            "quote_type": modern.get("quote_type"),
            "modern_confidence": modern.get("confidence"),
            "narrator": modern.get("narrator"),
        })
    return rows, {
        "qwen_rows": len(qidx), "modern_rows": len(midx), "shared_rows": len(shared),
        "missing_from_qwen": len(set(midx) - set(qidx)),
        "missing_from_modern": len(set(qidx) - set(midx)),
    }


def accuracy(rows, field="correct"):
    return sum(bool(r.get(field)) for r in rows) / len(rows) if rows else None


def candidate_policies():
    policies = [{"kind": "qwen"}, {"kind": "modern"}]
    policies.extend({"kind": "quote_type", "modern_types": list(types)} for types in (
        ("Anaphoric",), ("Implicit",), ("Anaphoric", "Implicit"), ("Explicit",)))
    policies.extend({"kind": "modern_confidence", "threshold": value / 20}
                    for value in range(1, 20))
    policies.append({"kind": "narrator_aware"})
    return policies


def select_policy(qwen_rows, modern_rows, groups=()):
    """Choose on pilot labels only; ties prefer fewer ModernBookNLP decisions."""
    scored = []
    for policy in candidate_policies():
        rows, coverage = evaluate(qwen_rows, modern_rows, policy, groups)
        modern_uses = sum(r["source"] == "modern" for r in rows)
        scored.append((accuracy(rows), -modern_uses, json.dumps(policy, sort_keys=True),
                       policy, rows, coverage))
    best = max(scored, key=lambda item: item[:3])
    return best[3], best[4], best[5]


def summarise(rows):
    q_only = sum(r["qwen_correct"] and not r["modern_correct"] for r in rows)
    m_only = sum(r["modern_correct"] and not r["qwen_correct"] for r in rows)
    hybrid_only = sum(r["correct"] and not r["qwen_correct"] for r in rows)
    qwen_only = sum(r["qwen_correct"] and not r["correct"] for r in rows)
    return {
        "n": len(rows), "qwen_accuracy": accuracy(rows, "qwen_correct"),
        "modern_accuracy": accuracy(rows, "modern_correct"),
        "hybrid_accuracy": accuracy(rows),
        "qwen_only_vs_modern_only": [q_only, m_only],
        "hybrid_gains_vs_qwen": hybrid_only,
        "hybrid_losses_vs_qwen": qwen_only,
        "hybrid_vs_qwen_mcnemar_p": exact_mcnemar(hybrid_only, qwen_only),
    }


def load_rows(path, arm=None):
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    rows = document.get("rows") or []
    if not rows and document and all(isinstance(value, dict)
                                     for value in document.values()):
        selected_arm = arm or "base"
        rows = []
        for book, value in document.items():
            for source in (value.get(selected_arm) or {}).get("rows") or []:
                item = dict(source)
                item["id"] = "%s:%s" % (book, item["id"])
                rows.append(item)
        arm = None
    return [row for row in rows if arm is None or row.get("arm") == arm]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--qwen", required=True)
    parser.add_argument("--modern", required=True, nargs="+")
    parser.add_argument("--qwen-arm", help="select one arm from a multi-arm artifact")
    parser.add_argument("--fixture", help="optional fixture supplying alias groups")
    parser.add_argument("--policy-in", help="apply a pilot-frozen policy")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    groups = ()
    if args.fixture:
        with open(args.fixture, encoding="utf-8") as handle:
            groups = alias_groups(json.load(handle))
    qwen_rows = load_rows(args.qwen, args.qwen_arm)
    modern_rows = []
    for path in args.modern:
        modern_rows.extend(load_rows(path))
    if args.policy_in:
        with open(args.policy_in, encoding="utf-8") as handle:
            policy = json.load(handle)["selected_policy"]
        rows, coverage = evaluate(qwen_rows, modern_rows, policy, groups)
        phase = "evaluation"
    else:
        policy, rows, coverage = select_policy(qwen_rows, modern_rows, groups)
        phase = "pilot"
    if not rows:
        raise SystemExit("no shared prediction IDs")
    document = {"status": "complete", "phase": phase, "selected_policy": policy,
                "coverage": coverage, "summary": summarise(rows), "rows": rows}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)
    print(json.dumps({"phase": phase, "policy": policy,
                      "coverage": coverage, "summary": document["summary"]}, indent=1))


if __name__ == "__main__":
    main()
