"""Test known-narrator metadata on the two weakest first-person PDNC books.

The Gambler is the decisive case: its narrator owns 56 of the first 120 gold
quotes, but the saved base run scored 0/56 and never emitted his roster name.
The intervention supplies one book-level fact and changes nothing else.
"""
import argparse
import collections
from dataclasses import replace
import hashlib
import json
import os
import sys
import time
import urllib.request


REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

from default_prompts import load_attribute_prompts  # noqa: E402
from experiments.manifest import ExperimentRecord  # noqa: E402
from narrator_prompt import (  # noqa: E402
    add_first_person_awareness, add_narrator_prior)
from experiments.pdnc_fixture import build as build_fixture  # noqa: E402
from experiments.scoring import alias_groups, same_speaker  # noqa: E402
from experiments.stats import paired  # noqa: E402
from generate_script import LLMGenParams  # noqa: E402
from openai import OpenAI  # noqa: E402
from three_pass_generate import attribute_batch  # noqa: E402


NARRATORS = {"TheGambler": "ALEXIS IVANOVITCH",
             "TheSunAlsoRises": "JAKE BARNES",
             "TheMysteriousAffairAtStyles": "MR. HASTINGS"}
BATCH = 25


def get_llama_server_environment(base_url, expected_model):
    """The server's real configuration, or raise. Provenance, not diagnostics.

    Delegates to lmstudio_settings.get_llama_cpp_status, which reads the same
    /props endpoint this used to parse itself. Two copies of "what is the
    server running" would drift (Rule 15), and the app's own status route
    needed this reader anyway - it had been reporting every llama.cpp model as
    unloaded because it only knew how to ask `lms ps`.

    Still RAISES where the shared reader returns a status: an artifact with an
    unverified model state cannot be compared against another run, so this
    caller refuses rather than recording a guess.
    """
    from lmstudio_settings import get_llama_cpp_status

    state = get_llama_cpp_status(base_url, expected_model, timeout=10)
    if state is None:
        raise RuntimeError(f"no llama.cpp /props at {base_url}")
    if not state["loaded"]:
        raise RuntimeError(
            f"server model {state.get('server_alias')!r} != {expected_model!r}")
    if not state.get("context_length") or not state.get("parallel"):
        raise RuntimeError("llama.cpp did not report context/slot configuration")
    return {"loaded": True, "verified_model": expected_model,
            "server_alias": state["server_alias"],
            "context_length": state["context_length"],
            "parallel": state["parallel"],
            "optimized": None, "runtime": "llama.cpp",
            "model_path": state.get("model_path"),
            "model_ftype": state.get("model_ftype"),
            "reasoning_format": state.get("reasoning_format")}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--books", nargs="+", default=list(NARRATORS))
    parser.add_argument("--model", default="qwen/qwen3-14b")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090/v1")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--tag", default="local-llamacpp")
    parser.add_argument("--arms", nargs="+",
                        choices=("baseline", "generic", "narrator"),
                        default=["baseline", "narrator"])
    args = parser.parse_args()

    unknown = [book for book in args.books if book not in NARRATORS]
    if unknown:
        raise SystemExit(f"narrator metadata is not defined for {unknown}")
    data = os.path.join(REPO, "ab_test_runtime", "pdnc", "data")
    fixtures = {book: build_fixture(data, book) for book in args.books}
    fixture_dir = os.path.join(REPO, "ab_test_runtime", "pdnc_inputs")
    os.makedirs(fixture_dir, exist_ok=True)
    fixture_paths = {}
    for book, fixture in fixtures.items():
        path = os.path.join(fixture_dir, f"{book}.narrator_prior_fixture.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(fixture, handle, ensure_ascii=False, indent=1)
        fixture_paths[book] = path
    base_system, _ = load_attribute_prompts()
    client = OpenAI(base_url=args.base_url, api_key="local")
    params = LLMGenParams(max_tokens=2000, context_length=32768,
                          temperature=0.0, attribute_temperature=0.0,
                          top_p=0.8, reasoning_effort="none")
    environment = get_llama_server_environment(args.base_url, args.model)
    first_gold = fixture_paths[args.books[0]]
    record = ExperimentRecord(
        "pdnc_narrator_prior", REPO, args.model, args.base_url, first_gold,
        {"temperature": 0.0, "batch": BATCH, "limit": args.limit,
         "narrators": NARRATORS},
        notes="Baseline production attribution prompt versus the same prompt "
              "with known first-person narrator metadata on two weak PDNC books.",
        environment=environment)
    record.meta["gold_files"] = {
        book: hashlib.sha256(open(os.path.join(
            data, book, "quotation_info.csv"), "rb").read()).hexdigest()
        for book in args.books}
    checkpoint = os.path.join(
        REPO, "ab_test_runtime", "experiments",
        f"pdnc_narrator_prior__{args.tag}.json.ckpt")
    record.enable_checkpoint(checkpoint)

    summary = {}
    for book, fixture in fixtures.items():
        entries = fixture["entries"][:args.limit]
        roster = fixture["roster"]
        groups = alias_groups(fixture)
        narrator = NARRATORS[book]
        available_arms = {
            "baseline": params,
            "generic": replace(
                params, system_prompt=add_first_person_awareness(base_system)),
            "narrator": replace(
                params, system_prompt=add_narrator_prior(
                    base_system, narrator)),
        }
        arms = {arm: available_arms[arm] for arm in args.arms}
        print(f"\n{book}: {len(entries)} quotes, narrator {narrator}",
              flush=True)
        for arm, arm_params in arms.items():
            started = time.time()
            for start in range(0, len(entries), BATCH):
                block = entries[start:start + BATCH]
                pending = [entry for entry in block if not record.done(
                    arm, f"{book}:{entry['id']}")]
                if not pending:
                    continue
                frozen = [{"type": "SPOKEN", "text": entry["line"]}
                          for entry in pending]
                contexts = [{
                    "previous_context": {"type": "NARRATOR",
                                         "text": entry["prev_context"]},
                    "next_context": {"type": "NARRATOR",
                                     "text": entry["next_context"]},
                } for entry in pending]
                try:
                    output = attribute_batch(
                        client, args.model, frozen, arm_params, roster,
                        neighbor_contexts=contexts)
                except Exception as exc:  # one policy: record every failed row
                    print(f"  {arm} block {start // BATCH}: "
                          f"{type(exc).__name__}", flush=True)
                    output = [{} for _ in pending]
                for offset, entry in enumerate(pending):
                    predicted = ((output[offset] or {}).get("speaker")
                                 if offset < len(output) else None)
                    record.add(
                        arm, f"{book}:{entry['id']}", entry["line"],
                        entry["expected_speaker"], predicted,
                        same_speaker(entry["expected_speaker"], predicted,
                                     groups),
                        candidates=roster,
                        provenance=f"{arm}|{book}|narrator={narrator}")
            rows = [row for row in record.rows
                    if row["arm"] == arm and row["id"].startswith(book + ":")]
            correct = sum(bool(row["correct"]) for row in rows)
            own = [row for row in rows
                   if same_speaker(row["expected"], narrator, groups)]
            own_correct = sum(bool(row["correct"]) for row in own)
            summary.setdefault(book, {})[arm] = {
                "n": len(rows), "correct": correct,
                "narrator_n": len(own), "narrator_correct": own_correct,
            }
            print(f"  {arm:9} {correct}/{len(rows)} = "
                  f"{100 * correct / max(len(rows), 1):.1f}% | narrator "
                  f"{own_correct}/{len(own)} | {time.time() - started:.0f}s",
                  flush=True)

    if "baseline" in args.arms:
        print("\npaired changes")
        for book in args.books:
            answers = {arm: {row["id"]: bool(row["correct"])
                             for row in record.rows
                             if row["arm"] == arm
                             and row["id"].startswith(book + ":")}
                       for arm in args.arms}
            for arm in args.arms:
                if arm == "baseline":
                    continue
                p_value, lost, gained, n = paired(
                    answers["baseline"], answers[arm])
                print(f"  {book:20} {arm:9} +{gained}/-{lost} of {n}, "
                      f"p={p_value:.4g}")
    record.meta["book_summary"] = summary
    out = record.write(os.path.join(
        REPO, "ab_test_runtime", "experiments",
        f"pdnc_narrator_prior__{args.tag}.json"),
        contract={"expected_arms": tuple(args.arms)})
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
