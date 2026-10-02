"""Test whether explicit attribution guidance closes the unseen-PDNC gap.

The five books used to diagnose failure classes are the pilot set.  The other
twenty unseen books stay sealed until the paired pilot clears its fixed gate.
"""
import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
import sys
import time


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
RUNTIME_ROOT = os.environ.get(
    "ALEXANDRIA_RUNTIME_ROOT", os.path.join(REPO, "ab_test_runtime"))
sys.path.insert(0, APP)

from experiments.manifest import ExperimentRecord, validate_stored_summary  # noqa: E402
from experiments.pdnc_fixture import build as build_fixture  # noqa: E402
from experiments.scoring import alias_groups, same_speaker  # noqa: E402
from experiments.stats import paired  # noqa: E402


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
DEFAULT_LIMIT = 120
BATCH = 25
PILOT_MIN_DELTA = 3.0
PILOT_MAX_P = 0.05
CONFIRMATORY_TARGET = 78.6


def get_context_books(phase, intervention):
    if phase not in ("pilot", "confirmatory") or intervention not in ("evidence", "sequence", "targeted_sequence"):
        raise ValueError("unknown context experiment phase or intervention")
    if intervention == "targeted_sequence":
        return TARGETED_PILOT_BOOKS if phase == "pilot" else TARGETED_CONFIRMATORY_BOOKS
    return PILOT_BOOKS if phase == "pilot" else CONFIRMATORY_BOOKS


def get_context_arm_names(intervention):
    if intervention == "targeted_sequence":
        return ("baseline", "sequence", "targeted_sequence")
    if intervention not in ("evidence", "sequence"):
        raise ValueError("unknown context intervention")
    return ("baseline", intervention)


def get_completed_context_result(path, bundle_path, phase, intervention, limit):
    """Read only a finalized result covering its exact declared input bundle."""
    books = get_context_books(phase, intervention)
    arms = get_context_arm_names(intervention)
    if type(limit) is not int or limit < 1:
        raise ValueError("sample limit must be a positive integer")
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or not isinstance(document.get("meta"), dict):
        raise ValueError("context result must contain metadata")
    meta = document["meta"]
    finished = meta.get("finished")
    if (meta.get("validation") != "ok" or type(finished) not in (int, float)
            or finished <= 0 or (type(finished) is float and not math.isfinite(finished))):
        raise ValueError("context result lacks successful final completion")
    if meta.get("phase") != phase or meta.get("intervention") != intervention:
        raise ValueError("context result is from another phase or intervention")
    decoding = meta.get("decoding")
    if (not isinstance(decoding, dict) or decoding.get("books") != list(books)
            or type(decoding.get("limit")) is not int or decoding["limit"] != limit):
        raise ValueError("context result declares another book sample")
    with open(bundle_path, "rb") as handle:
        raw = handle.read()
    if meta.get("gold_sha256") != hashlib.sha256(raw).hexdigest():
        raise ValueError("context result does not match its input bundle")
    bundle = json.loads(raw)
    if (not isinstance(bundle, dict) or bundle.get("books") != list(books)
            or type(bundle.get("limit")) is not int or bundle["limit"] != limit
            or not isinstance(bundle.get("entries"), list)):
        raise ValueError("context input bundle declares another sample")
    expected, counts = {}, {book: 0 for book in books}
    for entry in bundle["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("context bundle entry requires an id")
        matches = [book for book in books if entry["id"].startswith(book + "-")]
        if len(matches) != 1:
            raise ValueError("context bundle entry is from another book")
        identifier = matches[0] + ":" + entry["id"]
        if (identifier in expected or not isinstance(entry.get("line"), str)
                or not isinstance(entry.get("expected_speaker"), str) or not entry["expected_speaker"]):
            raise ValueError("context bundle has duplicate or invalid gold evidence")
        expected[identifier] = (entry["line"], entry["expected_speaker"])
        counts[matches[0]] += 1
    if any(not 1 <= count <= limit for count in counts.values()):
        raise ValueError("context bundle has an empty or oversized book sample")
    rows = document.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("context result has no rows")
    seen = {arm: set() for arm in arms}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("arm"), str)
                or row["arm"] not in seen or not isinstance(row.get("id"), str)):
            raise ValueError("context row has an invalid arm or id")
        arm, identifier = row["arm"], row["id"]
        if identifier not in expected or identifier in seen[arm]:
            raise ValueError("context row has duplicate or unexpected identity")
        if (row.get("line"), row.get("expected")) != expected[identifier]:
            raise ValueError("context row disagrees with input gold evidence")
        if (type(row.get("correct")) is not bool or "predicted" not in row
                or (row["predicted"] is not None and not isinstance(row["predicted"], str))):
            raise ValueError("context row has malformed prediction or correctness")
        seen[arm].add(identifier)
    if any(ids != set(expected) for ids in seen.values()):
        raise ValueError("context result does not cover the complete paired sample")
    summary = document.get("summary")
    if not isinstance(summary, dict) or set(summary) != set(arms):
        raise ValueError("context result summary has missing or unexpected arms")
    for bucket in summary.values():
        if not isinstance(bucket, dict) or any(type(bucket.get(k)) is not int for k in ("n", "correct")):
            raise ValueError("context summary counts must be integers")
    problems = validate_stored_summary(document)
    if problems:
        raise ValueError("; ".join(problems))
    if phase == "pilot":
        decision = meta.get("decision")
        if (not isinstance(decision, dict) or type(decision.get("advance")) is not bool
                or decision != get_pilot_decision(rows, intervention)):
            raise ValueError("pilot decision does not match the completed paired measurements")
    return document


def get_context_pilot_state(path, bundle_path, intervention, limit=DEFAULT_LIMIT):
    """Return a scientific verdict only for a complete, input-bound pilot."""
    try:
        document = get_completed_context_result(path, bundle_path, "pilot", intervention, limit)
    except (OSError, ValueError):
        return "missing"
    return "pass" if document["meta"]["decision"]["advance"] else "fail"


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
    from three_pass_generate import PassExhausted

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


def require_passing_pilot(path, bundle_path=None, intervention=None, limit=DEFAULT_LIMIT):
    with open(path, encoding="utf-8") as handle:
        artifact = json.load(handle)
    decision = (artifact.get("meta") or {}).get("decision") or {}
    if (artifact.get("meta") or {}).get("phase") != "pilot":
        raise ValueError("pilot artifact does not identify the pilot phase")
    if (artifact.get("meta") or {}).get("validation") != "ok":
        raise ValueError("pilot artifact did not pass structural validation")
    if decision.get("advance") is not True:
        raise ValueError("pilot gate did not pass; confirmatory run is forbidden")
    intervention = intervention or artifact["meta"].get("intervention")
    bundle_path = bundle_path or os.path.join(
        RUNTIME_ROOT, "pdnc_inputs", f"pdnc_{intervention}__pilot.json")
    completed = get_completed_context_result(path, bundle_path, "pilot", intervention, limit)
    return completed["meta"]["decision"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--phase", choices=("pilot", "confirmatory"),
                        default="pilot")
    parser.add_argument("--pilot-artifact")
    parser.add_argument("--check-artifact", help="validate a saved final result without inference")
    parser.add_argument("--intervention",
                        choices=("evidence", "sequence", "targeted_sequence"),
                        default="evidence")
    parser.add_argument("--model", default="qwen3-14b")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090/v1")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--tag", default="local-llamacpp")
    args = parser.parse_args()
    if args.check_artifact:
        bundle_path = os.path.join(RUNTIME_ROOT, "pdnc_inputs", f"pdnc_{args.intervention}__{args.phase}.json")
        try:
            get_completed_context_result(args.check_artifact, bundle_path, args.phase, args.intervention, args.limit)
        except (OSError, ValueError) as error:
            parser.exit(1, f"REFUSING incomplete context result: {error}\n")
        return

    from default_prompts import load_attribute_prompts
    from experiments.pdnc_narrator_prior import get_llama_server_environment
    from generate_script import LLMGenParams
    from openai import OpenAI
    from three_pass_generate import attribute_batch
    from utils import atomic_json_write

    if args.phase == "confirmatory":
        if not args.pilot_artifact:
            parser.error("--pilot-artifact is required for confirmatory phase")
        require_passing_pilot(args.pilot_artifact, os.path.join(
            RUNTIME_ROOT, "pdnc_inputs", f"pdnc_{args.intervention}__pilot.json"),
            args.intervention, args.limit)
    books = get_context_books(args.phase, args.intervention)
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
    candidate_arm = get_context_arm_names(args.intervention)[1]
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
        "pilot": list(get_context_books("pilot", args.intervention)),
        "confirmatory": list(get_context_books("confirmatory", args.intervention))}
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
    expected_arms = get_context_arm_names(args.intervention)
    out = record.write(os.path.join(
        RUNTIME_ROOT, "experiments", stem + ".json"),
        contract={"expected_arms": expected_arms,
                  "expected_ids": expected_ids,
                  "require_clean_tree": True})
    print(json.dumps(decision, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
