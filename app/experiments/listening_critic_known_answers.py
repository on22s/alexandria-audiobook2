"""Known-answer content arm for the #1547 listening critic.

Reads the #1434 verifier artifact (190 clean controls, 144 designed faults),
resolves its audio, and scores the critic's content findings against labels
known by construction. Only public-domain/synthetic material is accepted.
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from config_settings import load_app_config
from llm_provider import make_llm_client
from experiments.audio_judge_probe import (
    ensure_spend_capacity, get_judge_config, get_model_pricing, get_response_cost,
    load_ledger, save_ledger,
)
from experiments.listening_critic import critique_line, has_content_finding

LICENSE = "public-domain-synthetic"
GROUP_DIRS = {
    "alice": "alice/wav",
    "numbers": "numbers/wav",
    "w1435_U": "w1435_U/wav",
    "w1435_W": "w1435_U/wav",
    "v": "verify/wav",
    "r": "verify/wav",
}
DEFECT_DIR = "v1434/derived"


def ensure_license(value):
    """Refuse anything not declared public-domain/synthetic (Rule 27)."""
    if value != LICENSE:
        raise ValueError(f"Refusing unspecified material: pass --license {LICENSE}")


def resolve_audio(row, audio_root):
    """Map one #1434 row to its WAV without relying on ambiguous basenames."""
    if row.get("fault") != "control":
        directory = DEFECT_DIR
    else:
        directory = GROUP_DIRS.get(row.get("group"))
        if directory is None:
            raise ValueError(f"Unknown #1434 group: {row.get('group')!r}")
    path = Path(audio_root) / directory / row["wav"]
    if not path.is_file():
        raise FileNotFoundError(f"#1434 audio not found: {path}")
    return path


def load_cases(results_path, audio_root):
    """Load every #1434 row and resolve its audio."""
    cases = []
    for line in Path(results_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        audio = resolve_audio(row, audio_root)
        cases.append({
            "case_id": row["case_id"],
            "group": row["group"],
            "fault": row["fault"],
            "expect": row["expect"],
            "text": row["text"],
            "audio": str(audio),
            "asr_flagged": row.get("status") != "no_content_candidate",
        })
    return cases


def summarize(cases):
    """Precision/recall over evaluated cases, with errors kept separate."""
    evaluated = [case for case in cases if not case.get("error")]
    tp = sum(1 for case in evaluated if case["expect"] == "defect" and case["predicted"])
    fn = sum(1 for case in evaluated if case["expect"] == "defect" and not case["predicted"])
    fp = sum(1 for case in evaluated if case["expect"] == "clean" and case["predicted"])
    tn = sum(1 for case in evaluated if case["expect"] == "clean" and not case["predicted"])
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else None)
    per_fault = defaultdict(lambda: {"n": 0, "flagged": 0})
    per_group = defaultdict(lambda: {"n": 0, "flagged": 0})
    for case in evaluated:
        group = per_group[case["group"]]
        group["n"] += 1
        group["flagged"] += int(case["predicted"])
        if case["expect"] == "defect":
            fault = per_fault[case["fault"]]
            fault["n"] += 1
            fault["flagged"] += int(case["predicted"])
    for entry in per_fault.values():
        entry["recall"] = entry["flagged"] / entry["n"] if entry["n"] else None
    return {
        "evaluated": len(evaluated),
        "errors": len(cases) - len(evaluated),
        "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
        "per_fault": dict(sorted(per_fault.items())),
        "per_group": dict(sorted(per_group.items())),
    }


def asr_baseline(cases):
    """The same artifact's verifier verdicts, for a head-to-head baseline."""
    tp = sum(1 for case in cases if case["expect"] == "defect" and case["asr_flagged"])
    fn = sum(1 for case in cases if case["expect"] == "defect" and not case["asr_flagged"])
    fp = sum(1 for case in cases if case["expect"] == "clean" and case["asr_flagged"])
    tn = sum(1 for case in cases if case["expect"] == "clean" and not case["asr_flagged"])
    return {"tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None}


def run_case(client, model, pricing, case):
    findings, usage, raw = critique_line(client, model, case["audio"], case["text"])
    cost = get_response_cost(usage, pricing)
    return {**case, "predicted": has_content_finding(findings), "findings": findings,
            "raw": raw, "cost_usd": cost}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, help="#1434 results.jsonl")
    parser.add_argument("--audio-root", required=True, help="Directory containing the #1434 clip sets")
    parser.add_argument("--license", required=True, help=f"Must be {LICENSE}")
    parser.add_argument("--config", default=str(APP_DIR / "config.json"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--model", default=None, help="Override the remote profile's model")
    parser.add_argument("--max-spend-usd", type=float, default=5.0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    ensure_license(args.license)
    cases = load_cases(args.results, args.audio_root)
    if args.limit is not None:
        cases = cases[:args.limit]
    profile = get_judge_config(load_app_config(args.config))
    model = args.model or profile["model_name"]
    pricing = get_model_pricing(profile["base_url"], model)
    client = make_llm_client(profile, timeout=120)
    out = Path(args.out) if args.out else APP_DIR.parent / "ab_test_runtime" / "listening_critic_1547" / "known_answers_receipt.json"
    ledger_path = Path(args.ledger) if args.ledger else out.with_suffix(".spend.json")
    ledger = load_ledger(ledger_path)

    started = time.time()
    results = []
    for index, case in enumerate(cases, 1):
        try:
            result = run_case(client, model, pricing, case)
            ensure_spend_capacity(ledger["spent_usd"], args.max_spend_usd, result["cost_usd"])
            ledger["spent_usd"] += result["cost_usd"] or 0.0
            ledger["entries"].append({"case_id": case["case_id"], "cost_usd": result["cost_usd"],
                                      "at": time.time()})
            results.append(result)
        except Exception as exc:
            results.append({**case, "error": f"{type(exc).__name__}: {exc}"})
        if index % 20 == 0:
            print(f"{index}/{len(cases)} cases", file=sys.stderr)
        save_ledger(ledger_path, ledger)

    receipt = {
        "model": model,
        "results_path": str(Path(args.results).resolve()),
        "audio_root": str(Path(args.audio_root).resolve()),
        "license": args.license,
        "started_at": started,
        "elapsed_seconds": round(time.time() - started, 3),
        "spent_usd": ledger["spent_usd"],
        "max_spend_usd": args.max_spend_usd,
        "summary": summarize(results),
        "asr_baseline": asr_baseline(results),
        "cases": results,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"summary": receipt["summary"], "asr_baseline": receipt["asr_baseline"],
                      "spent_usd": receipt["spent_usd"], "receipt": str(out)}, indent=2))
    return 0 if receipt["summary"]["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
