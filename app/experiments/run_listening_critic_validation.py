"""Run the #1547 critic on the LoRA listener set and the designed fault arm.

No more listener rounds: the main round supplies listener truth for style and
instruct; the supplement labels supply known-answer voice/pause evidence. The
gate is reported only for dimensions with listener positives, and the
known-answer arm is labelled as such.
"""

import argparse
import json
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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
from experiments.listening_critic import (
    critique_line_review, critique_scene_review, has_review_finding,
)

ASPECTS = {
    "line": ("instruct", "naturalness", "content"),
    "scene": ("voice", "pause", "emotion"),
}


def load_main_labels(fixture, keys_path, responses_path):
    key = json.loads(Path(keys_path).read_text(encoding="utf-8"))
    responses = json.loads(Path(responses_path).read_text(encoding="utf-8"))
    source = {entry["item_id"]: responses["responses"][entry["trial_id"]]
              for entry in key["trials"] if not entry["repeat"]}
    labels = {}
    for line in fixture["lines"]:
        ratings = source[line["id"]]["ratings"]
        labels[line["id"]] = {"instruct": ratings["instruct_fit"] <= 2,
                              "naturalness": ratings["naturalness"] <= 2,
                              "content": ratings["content_correct"] <= 2}
    for scene in fixture["scenes"]:
        ratings = source[scene["id"]]["ratings"]
        labels[scene["id"]] = {"voice": ratings["voice_consistency"] <= 2,
                               "pause": ratings["pause_fit"] <= 2,
                               "emotion": ratings["emotion_fit"] <= 2}
    return labels, source


def predict_line(findings):
    return {"instruct": has_review_finding(findings, "style", "instruct"),
            "naturalness": has_review_finding(findings, "style", "naturalness"),
            "content": has_review_finding(findings, "content")}


def predict_scene(findings):
    return {"voice": has_review_finding(findings, "voice"),
            "pause": has_review_finding(findings, "pause"),
            "emotion": has_review_finding(findings, "style", "emotion")}


def score(cases):
    tp = sum(1 for truth, pred in cases if truth and pred)
    fn = sum(1 for truth, pred in cases if truth and not pred)
    fp = sum(1 for truth, pred in cases if not truth and pred)
    tn = sum(1 for truth, pred in cases if not truth and not pred)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {"n": len(cases), "positives": tp + fn, "tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "precision": precision, "recall": recall}


def bootstrap(cases, metric, rounds=5000, seed=1547):
    if not cases:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(rounds):
        sample = [cases[rng.randrange(len(cases))] for _ in cases]
        result = score(sample)
        value = result[metric]
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    return {"lower": values[int(0.025 * len(values))],
            "upper": values[int(0.975 * len(values)) - 1]}


def run_item(fn, *args):
    findings, usage, raw = fn(*args)
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    return {"findings": findings, "usage": usage, "raw": raw}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(APP_DIR / "fixtures" / "alice_pd_listening.json"))
    parser.add_argument("--main-clips", required=True)
    parser.add_argument("--main-key", required=True)
    parser.add_argument("--main-responses", required=True)
    parser.add_argument("--supplement-clips", required=True)
    parser.add_argument("--supplement-labels", required=True)
    parser.add_argument("--config", default=str(APP_DIR / "config.json"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--max-spend-usd", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    labels, _ = load_main_labels(fixture, args.main_key, args.main_responses)
    profile = get_judge_config(load_app_config(args.config))
    model = args.model or profile["model_name"]
    pricing = get_model_pricing(profile["base_url"], model)
    client = make_llm_client(profile, timeout=120)
    ledger_path = Path(args.ledger) if args.ledger else Path(args.out).with_suffix(".spend.json")
    ledger = load_ledger(ledger_path)

    run = {"model": model, "main": {"items": {}, "errors": []}, "supplement": {"items": {}, "errors": []}}
    spend = ledger["spent_usd"]
    for section, items in (("main", fixture["lines"]),):
        for item in items:
            if args.limit is not None and len(run[section]["items"]) >= args.limit:
                break
            item_id = item["id"]
            path = Path(args.main_clips) / f"{item_id}.wav"
            try:
                if item_id.startswith("line_"):
                    result = run_item(critique_line_review, client, model, path,
                                      item["text"], item.get("instruct", ""))
                    predicted = predict_line(result["findings"])
                else:
                    result = run_item(critique_scene_review, client, model, path, item["lines"])
                    predicted = predict_scene(result["findings"])
                cost = get_response_cost(result["usage"], pricing)
                ensure_spend_capacity(spend, args.max_spend_usd, cost)
                spend += cost or 0.0
                ledger["entries"].append({"section": section, "item": item_id, "cost_usd": cost})
                ledger["spent_usd"] = spend
                run[section]["items"][item_id] = {**result, "predicted": predicted, "cost_usd": cost}
                print(f"{section} {item_id} done", file=sys.stderr)
            except Exception as exc:
                run[section]["errors"].append({"item": item_id, "error": f"{type(exc).__name__}: {exc}"})
                print(f"{section} {item_id} ERROR {exc}", file=sys.stderr)
            save_ledger(ledger_path, ledger)

    supplement_labels = json.loads(Path(args.supplement_labels).read_text(encoding="utf-8"))
    scenes = {scene["id"]: scene for scene in fixture["scenes"]}
    lock = threading.Lock()

    def run_supplement(item_id, label):
        path = Path(args.supplement_clips) / f"{item_id}.wav"
        scene = scenes[label["scene"]]
        return item_id, label, run_item(critique_scene_review, client, model, path, scene["lines"], 300)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(run_supplement, item_id, label)
                   for item_id, label in supplement_labels.items()]
        for future in futures:
            try:
                item_id, label, result = future.result()
                cost = get_response_cost(result["usage"], pricing)
                with lock:
                    ensure_spend_capacity(spend, args.max_spend_usd, cost)
                    spend += cost or 0.0
                    ledger["entries"].append({"section": "supplement", "item": item_id, "cost_usd": cost})
                    ledger["spent_usd"] = spend
                    run["supplement"]["items"][item_id] = {**result, "predicted": predict_scene(result["findings"]),
                                                           "label": label, "cost_usd": cost}
                    save_ledger(ledger_path, ledger)
                print(f"supplement {item_id} done", file=sys.stderr)
            except Exception as exc:
                with lock:
                    run["supplement"]["errors"].append({"item": "unknown", "error": f"{type(exc).__name__}: {exc}"})
                print(f"supplement ERROR {exc}", file=sys.stderr)

    metrics = {}
    for item_id, entry in run["main"]["items"].items():
        label = labels[item_id]
        predicted = entry.get("predicted", {})
        for aspect in ASPECTS["line" if item_id.startswith("line_") else "scene"]:
            truth = label[aspect]
            metrics.setdefault(aspect, []).append((truth, bool(predicted.get(aspect))))
    run["main"]["metrics"] = {}
    for aspect, cases in metrics.items():
        result = score(cases)
        result["bootstrap"] = {"precision": bootstrap(cases, "precision"),
                               "recall": bootstrap(cases, "recall")}
        run["main"]["metrics"][aspect] = result

    run["supplement"]["metrics"] = {}
    for aspect in ("voice", "pause"):
        cases = []
        for item_id, entry in run["supplement"]["items"].items():
            label = entry["label"]
            truth = label["fault"] == "voice" if aspect == "voice" else label["fault"] in ("pause_long", "pause_short")
            cases.append((truth, bool(entry["predicted"].get(aspect))))
        if cases:
            result = score(cases)
            result["bootstrap"] = {"precision": bootstrap(cases, "precision"),
                                   "recall": bootstrap(cases, "recall")}
            run["supplement"]["metrics"][aspect] = result

    run["spend_usd"] = spend
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary = {"main_metrics": run["main"]["metrics"],
               "supplement_metrics": run["supplement"]["metrics"],
               "main_errors": len(run["main"]["errors"]),
               "supplement_errors": len(run["supplement"]["errors"]),
               "spend_usd": spend}
    print(json.dumps(summary, indent=2))
    instruct = run["main"]["metrics"].get("instruct", {})
    lower = (instruct.get("bootstrap") or {}).get("recall", {})
    gate = (lower.get("lower") is not None and lower["lower"] >= 0.5
            and (instruct.get("precision") or 0) >= 0.5)
    print(json.dumps({"listener_instruct_gate": gate,
                      "listener_truth_categories": [a for a, m in run["main"]["metrics"].items()
                                                    if m["positives"] > 0]}))
    return 0 if not run["main"]["errors"] and not run["supplement"]["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
