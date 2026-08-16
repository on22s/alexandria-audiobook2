"""Test whether explicit attribution guidance closes the unseen-PDNC gap.

The five books used to diagnose failure classes are the pilot set.  The other
twenty unseen books stay sealed until the paired pilot clears its fixed gate.
"""
import argparse
from dataclasses import replace
import hashlib
import json
import os
import sys
import time


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
RUNTIME_ROOT = os.environ.get(
    "ALEXANDRIA_RUNTIME_ROOT", os.path.join(REPO, "ab_test_runtime"))
sys.path.insert(0, APP)

from default_prompts import load_attribute_prompts  # noqa: E402
from experiments.manifest import ExperimentRecord  # noqa: E402
from experiments.pdnc_fixture import build as build_fixture  # noqa: E402
from experiments.pdnc_narrator_prior import get_llama_server_environment  # noqa: E402
from experiments.scoring import alias_groups, same_speaker  # noqa: E402
from experiments.stats import paired  # noqa: E402
from generate_script import LLMGenParams  # noqa: E402
from openai import OpenAI  # noqa: E402
from three_pass_generate import PassExhausted, attribute_batch  # noqa: E402
from utils import atomic_json_write  # noqa: E402


PILOT_BOOKS = ("AnneOfGreenGables", "MansfieldPark", "Persuasion",
               "TheGambler", "TheSunAlsoRises")
CONFIRMATORY_BOOKS = (
    "AHandfulOfDust", "APassageToIndia", "ARoomWithAView",
    "AlicesAdventuresInWonderland", "DaisyMiller", "Emma", "HardTimes",
    "HowardsEnd", "NightAndDay", "NorthangerAbbey", "OliverTwist",
    "SenseAndSensibility", "TheAgeOfInnocence", "TheInvisibleMan",
    "TheManWhoWasThursday", "TheMysteriousAffairAtStyles",
    "ThePictureOfDorianGray", "TheSportOfTheGods",
    "WhereAngelsFearToTread", "WinnieThePooh")
TARGETED_PILOT_BOOKS = CONFIRMATORY_BOOKS[:5]
TARGETED_CONFIRMATORY_BOOKS = CONFIRMATORY_BOOKS[5:]
BATCH = 25
PILOT_MIN_DELTA = 3.0
PILOT_MAX_P = 0.05
CONFIRMATORY_TARGET = 78.6


def add_context_evidence_guidance(system_prompt):
    """Return the single intervention; the production prompt stays unchanged."""
    return system_prompt.rstrip() + """

EVIDENCE PRIORITY:
- First identify explicit speech attribution in previous_context or
  next_context, such as a speech verb or an unambiguous reply/action beat tied
  to a roster character.
- A character name merely appearing nearby is NOT evidence that the character
  spoke the target line. Never select a speaker from proximity alone.
- When explicit attribution conflicts with a guess based on conversational
  turn-taking, follow the explicit attribution. Otherwise use the normal rules.
"""


def add_sequence_guidance(system_prompt):
    """Resolve ordered targets as dialogue sequences without forced alternation."""
    return system_prompt.rstrip() + """

DIALOGUE SEQUENCE:
- The numbered targets are in chronological order, but unattributed quotations
  may have been omitted. Treat two targets as adjacent turns only when their
  previous_context/next_context overlap or otherwise show the same exchange.
- Resolve an exchange jointly: anchor any explicitly attributed turn first,
  then use replies, questions, action beats, and established participants to
  resolve the neighboring turns.
- Never alternate speakers mechanically. One speaker may have consecutive
  targets, and a skipped quotation may interrupt the visible sequence.
- Explicit local attribution overrides a turn-taking inference.
"""


def select_targeted_sequence(entries, baseline, sequence):
    """Choose sequence only for two predeclared, observable signal classes."""
    generic_prefixes = ("A ", "AN ", "THE ")
    alternating = set()
    index = 0
    while index + 3 < len(entries):
        first = sequence.get(entries[index]["id"])
        second = sequence.get(entries[index + 1]["id"])
        if not first or not second or first == second or "UNKNOWN" in (first, second):
            index += 1
            continue
        end = index + 2
        while (end < len(entries)
               and sequence.get(entries[end]["id"])
               == (first if (end - index) % 2 == 0 else second)):
            end += 1
        if end - index >= 4:
            alternating.update(entry["id"] for entry in entries[index:end])
            index = end
        else:
            index += 1
    selected = {}
    for entry in entries:
        identity = entry["id"]
        base = baseline.get(identity)
        candidate = sequence.get(identity)
        generic_upgrade = (str(base or "").upper().startswith(generic_prefixes)
                           and candidate not in (None, "UNKNOWN")
                           and not str(candidate).upper().startswith(
                               generic_prefixes))
        use_sequence = generic_upgrade or identity in alternating
        selected[identity] = {
            "speaker": candidate if use_sequence else base,
            "reason": ("generic_role_to_named" if generic_upgrade else
                       "alternating_two_speaker_run" if identity in alternating
                       else "baseline_default")}
    return selected


def isolate_failed_attribution(attribute, frozen, contexts):
    """Split quality-exhausted batches; mark only irreducible rows UNKNOWN."""
    try:
        return attribute(frozen, contexts), set()
    except PassExhausted:
        if len(frozen) == 1:
            return [{"text": frozen[0]["text"], "speaker": "UNKNOWN"}], {0}
        midpoint = len(frozen) // 2
        left, left_failed = isolate_failed_attribution(
            attribute, frozen[:midpoint], contexts[:midpoint])
        right, right_failed = isolate_failed_attribution(
            attribute, frozen[midpoint:], contexts[midpoint:])
        return left + right, left_failed | {
            midpoint + index for index in right_failed}


def summarize_paired_rows(rows, candidate_arm="evidence"):
    answers = {arm: {row["id"]: bool(row["correct"])
                     for row in rows if row["arm"] == arm}
               for arm in ("baseline", candidate_arm)}
    shared = set(answers["baseline"]) & set(answers[candidate_arm])
    baseline_correct = sum(answers["baseline"][key] for key in shared)
    evidence_correct = sum(answers[candidate_arm][key] for key in shared)
    p_value, lost, gained, n = paired(
        answers["baseline"], answers[candidate_arm])
    delta = (100.0 * (evidence_correct - baseline_correct) / n) if n else 0.0
    return {"n": n, "baseline_correct": baseline_correct,
            "evidence_correct": evidence_correct, "delta_points": delta,
            "gained": gained, "lost": lost, "p_value": p_value}


def get_pilot_decision(rows, candidate_arm="evidence"):
    result = summarize_paired_rows(rows, candidate_arm)
    result["advance"] = (result["n"] > 0
                         and result["delta_points"] >= PILOT_MIN_DELTA
                         and result["p_value"] < PILOT_MAX_P)
    result["gate"] = {"minimum_delta_points": PILOT_MIN_DELTA,
                      "maximum_p_exclusive": PILOT_MAX_P}
    return result


def require_passing_pilot(path):
    with open(path, encoding="utf-8") as handle:
        artifact = json.load(handle)
    decision = (artifact.get("meta") or {}).get("decision") or {}
    if (artifact.get("meta") or {}).get("phase") != "pilot":
        raise ValueError("pilot artifact does not identify the pilot phase")
    if (artifact.get("meta") or {}).get("validation") != "ok":
        raise ValueError("pilot artifact did not pass structural validation")
    if decision.get("advance") is not True:
        raise ValueError("pilot gate did not pass; confirmatory run is forbidden")
    return decision


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--phase", choices=("pilot", "confirmatory"),
                        default="pilot")
    parser.add_argument("--pilot-artifact")
    parser.add_argument("--intervention",
                        choices=("evidence", "sequence", "targeted_sequence"),
                        default="evidence")
    parser.add_argument("--model", default="qwen3-14b")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090/v1")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--tag", default="local-llamacpp")
    args = parser.parse_args()

    if args.phase == "confirmatory":
        if not args.pilot_artifact:
            parser.error("--pilot-artifact is required for confirmatory phase")
        require_passing_pilot(args.pilot_artifact)
    if args.intervention == "targeted_sequence":
        books = (TARGETED_PILOT_BOOKS if args.phase == "pilot"
                 else TARGETED_CONFIRMATORY_BOOKS)
    else:
        books = PILOT_BOOKS if args.phase == "pilot" else CONFIRMATORY_BOOKS
    data = os.path.join(RUNTIME_ROOT, "pdnc", "data")
    fixtures = {book: build_fixture(data, book) for book in books}
    bundle_path = os.path.join(
        RUNTIME_ROOT, "pdnc_inputs",
        f"pdnc_{args.intervention}__{args.phase}.json")
    bundle = {"entries": [entry for book in books
                          for entry in fixtures[book]["entries"][:args.limit]],
              "books": list(books), "limit": args.limit}
    atomic_json_write(bundle, bundle_path)

    base_system, _ = load_attribute_prompts()
    params = LLMGenParams(max_tokens=2000, context_length=32768,
                          temperature=0.0, attribute_temperature=0.0,
                          top_p=0.8, reasoning_effort="none")
    guidance = (add_context_evidence_guidance if args.intervention == "evidence"
                else add_sequence_guidance)
    candidate_arm = ("sequence" if args.intervention == "targeted_sequence"
                     else args.intervention)
    arms = {"baseline": params,
            candidate_arm: replace(
                params, system_prompt=guidance(base_system))}
    client = OpenAI(base_url=args.base_url, api_key="local")
    environment = get_llama_server_environment(args.base_url, args.model)
    record = ExperimentRecord(
        f"pdnc_{args.intervention}", REPO, args.model, args.base_url, bundle_path,
        {"temperature": 0.0, "batch": BATCH, "limit": args.limit,
         "phase": args.phase, "books": list(books),
         "arms": list(arms)},
        notes=("Production baseline versus explicit-attribution evidence priority."
               if args.intervention == "evidence" else
               "Production baseline versus sequence-aware dialogue resolution."),
        environment=environment)
    record.meta["phase"] = args.phase
    record.meta["intervention"] = args.intervention
    record.meta["book_split"] = {
        "pilot": list(TARGETED_PILOT_BOOKS if
                      args.intervention == "targeted_sequence" else PILOT_BOOKS),
        "confirmatory": list(TARGETED_CONFIRMATORY_BOOKS if
                             args.intervention == "targeted_sequence"
                             else CONFIRMATORY_BOOKS)}
    record.meta["system_prompt_sha256"] = {
        arm: hashlib.sha256((arm_params.system_prompt or base_system).encode(
            "utf-8")).hexdigest()
        for arm, arm_params in arms.items()}
    stem = f"pdnc_{args.intervention}__{args.phase}__{args.tag}"
    checkpoint = os.path.join(
        RUNTIME_ROOT, "experiments", stem + ".json.ckpt")
    record.enable_checkpoint(checkpoint)
    isolated_failures = []

    for book in books:
        fixture = fixtures[book]
        entries = fixture["entries"][:args.limit]
        roster = fixture["roster"]
        groups = alias_groups(fixture)
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
                # One consistent error policy: attribute_batch exhausts its
                # retries, then the run exits without checkpointing this block.
                # Resume retries the whole failed block under identical inputs.
                def attribute(current, current_contexts):
                    attempts = []
                    try:
                        return attribute_batch(
                            client, args.model, current, arm_params, roster,
                            neighbor_contexts=current_contexts,
                            attempt_observer=attempts.append)
                    except PassExhausted:
                        # Transport exhaustion is not a scientific UNKNOWN and
                        # must stop the run. Only deterministic response-quality
                        # exhaustion is eligible for row isolation.
                        if attempts and all(
                                attempt.get("outcome") == "api_error"
                                for attempt in attempts):
                            raise RuntimeError(
                                "LLM endpoint exhausted; refusing to split a "
                                "transport failure")
                        raise

                output, failed_offsets = isolate_failed_attribution(
                    attribute, frozen, contexts)
                for offset, entry in enumerate(pending):
                    predicted = (output[offset] or {}).get("speaker")
                    isolated = offset in failed_offsets
                    if isolated:
                        isolated_failures.append(
                            {"arm": arm, "id": f"{book}:{entry['id']}"})
                    record.add(
                        arm, f"{book}:{entry['id']}", entry["line"],
                        entry["expected_speaker"], predicted,
                        same_speaker(entry["expected_speaker"], predicted,
                                     groups),
                        candidates=roster,
                        provenance=(f"{arm}|{args.phase}|{book}"
                                    f"{'|isolated_exhaustion' if isolated else ''}"))
            rows = [row for row in record.rows if row["arm"] == arm
                    and row["id"].startswith(book + ":")]
            correct = sum(bool(row["correct"]) for row in rows)
            print(f"{book:30} {arm:8} {correct}/{len(rows)} "
                  f"({time.time() - started:.0f}s)", flush=True)

        if args.intervention == "targeted_sequence":
            predictions = {
                arm: {row["id"].split(":", 1)[1]: row.get("predicted")
                      for row in record.rows if row["arm"] == arm
                      and row["id"].startswith(book + ":")}
                for arm in ("baseline", "sequence")}
            selected = select_targeted_sequence(
                entries, predictions["baseline"], predictions["sequence"])
            for entry in entries:
                identity = f"{book}:{entry['id']}"
                if record.done("targeted_sequence", identity):
                    continue
                choice = selected[entry["id"]]
                record.add(
                    "targeted_sequence", identity, entry["line"],
                    entry["expected_speaker"], choice["speaker"],
                    same_speaker(entry["expected_speaker"], choice["speaker"],
                                 groups), candidates=roster,
                    provenance=(f"targeted_sequence|{args.phase}|{book}|"
                                f"{choice['reason']}"))

    summary = summarize_paired_rows(record.rows, args.intervention)
    decision = (get_pilot_decision(record.rows, args.intervention)
                if args.phase == "pilot" else {
        **summary,
        "meets_goal": (100.0 * summary["evidence_correct"] / summary["n"]
                       >= CONFIRMATORY_TARGET if summary["n"] else False),
        "minimum_accuracy": CONFIRMATORY_TARGET})
    record.meta["decision"] = decision
    record.meta["isolated_failures"] = isolated_failures
    expected_ids = {f"{book}:{entry['id']}" for book in books
                    for entry in fixtures[book]["entries"][:args.limit]}
    expected_arms = tuple(arms) + (("targeted_sequence",)
                                   if args.intervention == "targeted_sequence"
                                   else ())
    out = record.write(os.path.join(
        RUNTIME_ROOT, "experiments", stem + ".json"),
        contract={"expected_arms": expected_arms,
                  "expected_ids": expected_ids,
                  "require_clean_tree": True})
    print(json.dumps(decision, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
