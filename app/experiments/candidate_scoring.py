"""Score every candidate instead of asking the model to name one.

SIG (Speaker Identification in Literature via Prompt-Based Generation, arXiv
2312.14590) is the only published method that shares our architecture - prompt
an LLM with the quote, its context and a candidate list. It reports two ways to
read an answer out: direct generation, which is what we do, or "the highest
generation probability of each speaker candidate", which we have never tried.

WHY IT IS BETTER THAN SHUFFLING. #383 measured a list-order bias: when the
model is wrong, its answer sits earlier in the alphabetical cast list than the
correct one 67.2% of the time, p = 1.3e-23. `shuffled_roster` randomises the
order so the bias averages out. Scoring removes the mechanism instead - each
candidate is evaluated on its own and there is no list to be near the top of.
#386 sharpened the target: the bias is strongest (74.7%) on the 340 rows where
the context named nobody, which is exactly where a scored comparison has to do
the work rather than the ordering.

WHAT THIS CANNOT DO WITHOUT THE SERVER. The scoring needs per-token log
probabilities back from the endpoint. `preflight` demands them in the exact
shape the scorer reads and REFUSES otherwise, because a scorer that silently
falls back to generation would report a number for an experiment that did not
happen - which is the failure this repository keeps finding.
"""
import argparse
import json
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402
from experiments.scoring import normalize  # noqa: E402


def first_token_of(name):
    """-> the leading fragment a model would emit for `name`.

    Candidates are compared on their FIRST token because that is what a single
    scored generation step exposes. Two candidates sharing one are
    indistinguishable this way and must be reported, not silently tied.
    """
    text = (name or "").strip()
    return text.split()[0] if text else ""


def collisions(names):
    """-> {first token: [names]} for candidates a one-step score cannot separate."""
    groups = {}
    for name in names:
        groups.setdefault(normalize(first_token_of(name)), []).append(name)
    return {k: v for k, v in groups.items() if len(v) > 1 and k}


def read_logprobs(choice):
    """-> {token: logprob} from the first generated position, or None.

    Written against the OpenAI-compatible shape llama.cpp serves:
    choices[0].logprobs.content[0].top_logprobs = [{token, logprob}, ...].
    Anything else returns None and the caller refuses.
    """
    logprobs = (choice or {}).get("logprobs") or {}
    content = logprobs.get("content") or []
    if not content:
        return None
    top = content[0].get("top_logprobs")
    if not isinstance(top, list) or not top:
        return None
    out = {}
    for item in top:
        token = item.get("token")
        value = item.get("logprob")
        if token is None or value is None:
            return None
        out[token] = float(value)
    return out or None


def score_candidates(token_logprobs, names):
    """-> [(name, logprob)] best first, for candidates the step could see.

    A candidate whose first token never appears among the returned alternatives
    is ABSENT, not improbable: the endpoint returns a truncated list, so a
    missing token means "not in the top k", and assigning it a floor would
    invent a comparison the data does not support.
    """
    lookup = {normalize(t): v for t, v in token_logprobs.items()}
    scored = []
    for name in names:
        key = normalize(first_token_of(name))
        if key in lookup:
            scored.append((name, lookup[key]))
    scored.sort(key=lambda pair: -pair[1])
    return scored


def decide(token_logprobs, names):
    """-> (winner or None, why). None means the step could not choose."""
    scored = score_candidates(token_logprobs, names)
    if not scored:
        return None, "no candidate's first token was among the returned alternatives"
    if len(scored) > 1 and math.isclose(scored[0][1], scored[1][1], abs_tol=1e-9):
        return None, "tie between %s and %s" % (scored[0][0], scored[1][0])
    return scored[0][0], None


def preflight(client, model):
    """-> None when the endpoint returns usable logprobs, else a reason string."""
    try:
        response = client.chat.completions.create(
            model=model, temperature=0.0, max_tokens=1,
            logprobs=True, top_logprobs=5,
            extra_body={"reasoning_effort": "none"},
            messages=[{"role": "user", "content": "Answer with one word: ELIZABETH"}])
    except Exception as exc:                                # noqa: BLE001
        return "%s: %s" % (type(exc).__name__, str(exc)[:160])
    try:
        choice = json.loads(response.model_dump_json())["choices"][0]
    except Exception as exc:                                # noqa: BLE001
        return "unreadable response: %s" % exc
    return None if read_logprobs(choice) else \
        "endpoint returned no per-token alternatives; scoring is not available"


def survey_rosters(fixtures_dir):
    """-> per-book collision counts, the thing that decides feasibility."""
    import glob
    from experiments.two_stage_attribution import roster_lines
    out, names_total, blocked_total = [], 0, 0
    for path in sorted(glob.glob(os.path.join(
            fixtures_dir, "attribution_gold_pdnc_*_w3200.json"))):
        with open(path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        names = [line.split(" [also")[0] for line in roster_lines(fixture)]
        groups = collisions(names)
        blocked = sum(len(v) for v in groups.values())
        names_total += len(names)
        blocked_total += blocked
        worst = max(groups.items(), key=lambda kv: len(kv[1])) if groups else (None, [])
        out.append({"book": os.path.basename(path),
                    "candidates": len(names),
                    "indistinguishable_on_first_token": blocked,
                    "share": round(blocked / len(names), 4) if names else None,
                    "worst_prefix": worst[0],
                    "worst_prefix_count": len(worst[1]),
                    "groups": {k: v for k, v in sorted(
                        groups.items(), key=lambda kv: -len(kv[1]))[:5]}})
    return out, names_total, blocked_total


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fixtures", default=os.path.join(REPO, "app", "fixtures"))
    ap.add_argument("--base-url", default="http://127.0.0.1:8090/v1")
    ap.add_argument("--model", default="qwen3-14b")
    ap.add_argument("--check-endpoint", action="store_true",
                    help="also ask the server whether it returns logprobs; "
                         "skipped by default because it competes with a "
                         "running arm for a parallel=1 server")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    books, names_total, blocked_total = survey_rosters(args.fixtures)
    if not books:
        raise SystemExit("no _w3200 fixtures under %s" % args.fixtures)
    share = blocked_total / names_total

    reason = "not checked"
    if args.check_endpoint:
        from openai import OpenAI
        reason = preflight(OpenAI(base_url=args.base_url, api_key="none"), args.model)

    doc = {
        "status": "complete",
        "provenance": provenance(__file__, args),
        "scope": "can SIG's candidate-scoring readout be used here, and at "
                 "what granularity. Offline: no model was called unless "
                 "--check-endpoint was passed",
        "idea": "SIG (arXiv 2312.14590) reads the answer out as the highest "
                "generation probability over enumerated candidates rather than "
                "by direct generation, which removes the list-order mechanism "
                "#383 measured instead of randomising it",
        "books": books,
        "candidates_total": names_total,
        "indistinguishable_on_first_token": blocked_total,
        "share_indistinguishable": round(share, 4),
        "endpoint_logprobs": (None if reason == "not checked"
                              else (reason is None)),
        "endpoint_reason": reason,
        "verdict": (
            "one-step scoring is NOT usable: %.0f%% of candidates share a "
            "first token, so the score would be a comparison between "
            "honorifics. Correct implementation is full-sequence scoring, "
            "which costs one forward pass per candidate per quote"
            % (100 * share)
            if share > 0.10 else
            "one-step scoring is viable on these rosters"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%-34s %10s %10s  %s" % ("book", "candidates", "collide", "worst"))
    for b in books:
        print("%-34s %10d %9d%%  %s x%d"
              % (b["book"][21:-11], b["candidates"],
                 round(100 * b["share"]), b["worst_prefix"], b["worst_prefix_count"]))
    print("\noverall %d of %d (%.0f%%)" % (blocked_total, names_total, 100 * share))
    print("\n%s" % doc["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
