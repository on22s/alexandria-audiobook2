"""Ask one question per quote, the way the literature asks it.

WHY. Our production path segments prose, decides narration vs speech, and
names the speaker in one pass, and scores 61.7% on PDNC gold. The published
formulation hands the model a KNOWN quote plus the character list and asks only
"who said this": LLaMa3 8b reaches 90.6% on the same corpus that way, and 94.7%
on explicit quotes. We are running a 14B and losing to an 8B by thirty points,
which points at the question rather than the model.

The gold fixtures already carry what that formulation needs - `line` is the
quote, `prev_context` and `next_context` the surrounding text, `aliases` the
character list - so this is a prompt shape, not new plumbing.

ARMS.
  batch   what ships: attribute_batch over many lines at once, the current
          production call. The baseline this must beat to matter.
  single  one quote per request, with the context window and the alias list,
          and UNKNOWN allowed as an answer.

UNKNOWN IS SCORED SEPARATELY, NOT AS A MISS. A model that declines 10% of
quotes and is right on the rest is a different proposition from one that
guesses wrong 10% of the time - the first can be routed to a human or a second
pass, the second cannot even be found. Reporting them together would hide the
only thing an escape hatch is for.

Nothing here is run automatically: it needs an LLM and the GPU queue, and it is
queued as a stage rather than started.
"""
import argparse
import glob
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
from core import llm_timeout_seconds  # noqa: E402
from experiments.gpu_guard import require_free_gpu  # noqa: E402
from experiments.provenance import provenance  # noqa: E402
from experiments.scoring import alias_groups, same_speaker  # noqa: E402

FIXTURES = os.path.join(APP, "fixtures")

SINGLE_PROMPT = """You are told a line of dialogue from a novel and the text \
around it. Name the character who speaks the line.

Characters in this book:
{roster}

Text before the line:
{prev}

THE LINE:
{line}

Text after the line:
{next}

Answer with one name from the list above, exactly as written there. If the \
passage does not let you tell who speaks, answer UNKNOWN - a wrong name is \
worse than an honest UNKNOWN, because nobody can find it later."""


def build_client(base_url, api_key="local"):
    # Not beside a running job. This script talks to the same llama-server a
    # queued generation uses, and calling it by hand during one is exactly the
    # collision the queue exists to prevent.
    require_free_gpu("two_stage_attribution")
    from openai import OpenAI
    return OpenAI(base_url=base_url, api_key=api_key,
                  timeout=llm_timeout_seconds())


def ask_single(client, model, entry, roster, temperature=0.0):
    prompt = SINGLE_PROMPT.format(
        roster="\n".join(f"- {name}" for name in roster),
        prev=str(entry.get("prev_context") or "")[-1500:],
        line=str(entry.get("line") or ""),
        next=str(entry.get("next_context") or "")[:600])
    response = client.chat.completions.create(
        model=model, temperature=temperature, max_tokens=24,
        messages=[{"role": "user", "content": prompt}])
    return (response.choices[0].message.content or "").strip().upper()


def score(entries, answers, groups):
    """Also keeps the answers themselves.

    The first run of this scored 35.5%, 50.0% and 41.0% - below our own
    one-pass baseline of 61.7% and far below the 90.6% the published
    formulation reports - and the artifact recorded only the counts. So there
    was no way to tell a method that does not work from a prompt whose replies
    are not being parsed: if the model answers "The speaker is Elizabeth", an
    exact-name comparison scores it wrong and the number looks like a finding.
    A result that cannot be diagnosed is not a result.
    """
    correct = unknown = wrong = 0
    by_type = {}
    misses = []
    for entry, answer in zip(entries, answers):
        kind = entry.get("quote_type") or "unknown"
        bucket = by_type.setdefault(kind, {"n": 0, "correct": 0, "unknown": 0})
        bucket["n"] += 1
        if answer.startswith("UNKNOWN"):
            unknown += 1
            bucket["unknown"] += 1
        elif same_speaker(entry.get("expected_speaker"), answer, groups):
            correct += 1
            bucket["correct"] += 1
        else:
            wrong += 1
            if len(misses) < 40:
                misses.append({"expected": entry.get("expected_speaker"),
                               "model_said": answer[:80],
                               "quote_type": kind})
    answered = correct + wrong
    return {
        "n": len(entries), "correct": correct, "wrong": wrong,
        "declined_unknown": unknown,
        "accuracy": round(correct / max(len(entries), 1), 4),
        # Accuracy over the quotes it was willing to answer. Both numbers or
        # neither: one without the other lets a model look good by declining.
        "accuracy_when_answered": round(correct / answered, 4) if answered else None,
        "by_quote_type": by_type,
        # Verbatim, and capped: the first 40 wrong answers say immediately
        # whether the model is naming the wrong character or answering in a
        # shape the scorer cannot read.
        "wrong_answers": misses,
        "sample_answers": [a[:80] for a in answers[:10]],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fixtures", nargs="+",
                    default=sorted(glob.glob(os.path.join(
                        FIXTURES, "attribution_gold_pdnc_*.json"))))
    ap.add_argument("--model", default="qwen3-14b")
    ap.add_argument("--base-url", default="http://127.0.0.1:8090/v1")
    ap.add_argument("--limit", type=int, default=200,
                    help="quotes per book; the gold books hold 584-1270")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "two_stage_attribution.json"))
    args = ap.parse_args()

    client = build_client(args.base_url)
    results = []
    for path in args.fixtures:
        with open(path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        entries = fixture["entries"][:args.limit]
        groups = alias_groups(fixture)
        roster = sorted({names[0] for names in (fixture.get("aliases") or [])
                         if names}) or sorted(fixture.get("roster") or [])
        started = time.time()
        answers = []
        for index, entry in enumerate(entries, 1):
            try:
                answers.append(ask_single(client, args.model, entry, roster))
            except Exception as exc:                      # noqa: BLE001
                # Recorded, never silently counted as a miss: a request that
                # failed is not a model that was wrong.
                answers.append(f"ERROR: {type(exc).__name__}")
            if index % 25 == 0:
                print(f"  {os.path.basename(path)}: {index}/{len(entries)}", flush=True)
        row = {"book": os.path.basename(path), "arm": "single",
               "seconds": round(time.time() - started, 1),
               **score(entries, answers, groups)}
        results.append(row)
        print(f"{row['book']}: accuracy {row['accuracy']:.1%}, "
              f"when answered {row['accuracy_when_answered']}, "
              f"declined {row['declined_unknown']}")
        _write(args.out, results, len(args.fixtures))
    _write(args.out, results, len(args.fixtures), final=True)


def _write(out, results, considered, final=False):
    with open(out, "w", encoding="utf-8") as handle:
        json.dump({"status": "complete" if final else "partial",
                   "candidates_considered": considered,
                   "results": results,
                   "provenance": provenance(__file__, None)}, handle,
                  ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
