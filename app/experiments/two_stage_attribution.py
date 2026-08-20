"""Ask one question per quote, the way the literature asks it.

THE QUESTION. Our production path segments prose, decides narration from
speech, and names the speaker in one pass. The published formulation hands the
model a KNOWN quote plus the cast and asks only "who said this": LLaMa3 8b
reports 90.6% on PDNC that way, 94.7% on explicit quotes.

THE BAR IS 83.6%, NOT 61.7%. An earlier version of this file compared itself
against 61.7%, which is the baseline arm of the narrator-prior experiment on
two different, deliberately weak PDNC books. The one-pass number on the three
books THIS script runs is 83.6% over 360 rows (GOALS.md, goal 1.3). Quoting the
wrong baseline made the gap look like 29 points when it is 7, and a 7-point gap
is a different decision.

WHAT THIS RECORDS. Every row, through ExperimentRecord: quote id, expected,
predicted, correct, and the raw reply. Four wrong results came out of this
harness before it kept rows - counts alone cannot tell a method that does not
work from a prompt whose replies are not parsed, and cannot be compared
row-by-row against pdnc_eval's batch arm, which does carry ids.

THREE CATEGORIES, KEPT APART. A model that DECLINES (UNKNOWN) is not a model
that is WRONG, and neither is a request that never reached a model. Declines
can be routed to a second pass; wrong answers cannot be found; failures say
nothing about the method at all. `accuracy_when_answered` is the figure
comparable to the published forced-choice number.

SAMPLING IS RANDOM AND SEEDED. --limit used to take the first N entries, and
the head of these books is not representative: The Sign of the Four's first 200
are 59.5% anaphoric against 36.2% for the book, and the three books skew in
opposite directions, so part of their spread was a slicing artifact.
"""
import argparse
import glob
import json
import os
import random
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
from core import llm_timeout_seconds  # noqa: E402
from experiments.gpu_guard import require_free_gpu  # noqa: E402
from experiments.manifest import ExperimentRecord  # noqa: E402
from experiments.scoring import alias_groups, normalize, same_speaker  # noqa: E402

FIXTURES = os.path.join(APP, "fixtures")
DECLINE = "UNKNOWN"

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

Answer with ONE name, and nothing else. Use the name at the START of a line \
above - not the alternatives in brackets, which are only there to tell you \
which spellings mean the same person. If the passage does not let you tell who \
speaks, answer UNKNOWN: a wrong name is worse than an honest UNKNOWN, because \
nobody can find it later."""


def build_client(base_url, api_key="local"):
    # Not beside a running job: this talks to the same server a queued
    # generation uses, and calling it by hand during one is the collision the
    # queue exists to prevent.
    require_free_gpu("two_stage_attribution")
    from openai import OpenAI
    return OpenAI(base_url=base_url, api_key=api_key,
                  timeout=llm_timeout_seconds())


def roster_names(lines):
    """-> every name a roster line stands for, canonical and aliases alike.

    "MRS. BENNET [also: BENNET]" -> {"MRS. BENNET", "BENNET"}. Used for the
    artifact's candidate set, which is a question about membership rather than
    about how the cast was shown to the model.
    """
    names = []
    for line in lines:
        match = re.match(r"^(.*?)\s*\[also:\s*(.*?)\]\s*$", line)
        if not match:
            names.append(line.strip())
            continue
        names.append(match.group(1).strip())
        names.extend(alias.strip() for alias in match.group(2).split(","))
    return [n for n in names if n]


def roster_lines(fixture):
    """-> one line per character, canonical name FIRST, aliases in brackets.

    The canonical name is the one the gold actually uses. PDNC alias groups are
    sorted alphabetically, so group[0] is not it: taking group[0] decorated
    ELIZA and left ELIZABETH (401 gold lines) bare, inflated a 74-name cast to
    84 with shadow duplicates, and gave ten characters who never speak an
    alias list of their own.
    """
    speakers = {e.get("expected_speaker") for e in fixture.get("entries", [])}
    aliases = {}
    for group in (fixture.get("aliases") or []):
        names = [n for n in group if n]
        if not names:
            continue
        # Prefer a name the gold speaks with; else the roster's own spelling.
        canonical = next((n for n in names if n in speakers), None)
        if canonical is None:
            canonical = next((n for n in names
                              if n in (fixture.get("roster") or [])), names[0])
        aliases[canonical] = sorted(n for n in names if n != canonical)
    cast = list(fixture.get("roster") or []) or sorted(aliases)
    for name in aliases:
        if name not in cast:
            cast.append(name)
    lines = []
    for name in sorted(set(cast)):
        rest = aliases.get(name)
        lines.append(f"{name} [also: {', '.join(rest)}]" if rest else name)
    return lines


def clean_answer(text):
    """-> the name the model meant, from whatever shape it replied in.

    Replies arrive decorated: **NAME**, "NAME", `NAME.`, or the whole roster
    line echoed back because the prompt asked for it verbatim. An exact-string
    comparison scored every one of those wrong, and 19 of 84 cast lines could
    only be answered in a form that failed.
    """
    answer = (text or "").strip()
    answer = re.sub(r"^[\s*_`\"'“‘>-]+|[\s*_`\"'”’.]+$", "", answer)
    answer = answer.split("[also:")[0].split("(also")[0]      # echoed cast line
    answer = answer.split("\n")[0]
    return answer.strip().upper()


def is_decline(answer):
    """UNKNOWN in any decoration the model chooses to wrap it in."""
    return normalize(answer).startswith(DECLINE)


def ask(client, model, entry, roster, decoding):
    """-> (answer, raw, failure_reason). failure_reason set means no answer."""
    prompt = SINGLE_PROMPT.format(
        roster="\n".join(f"- {name}" for name in roster),
        prev=str(entry.get("prev_context") or ""),
        line=str(entry.get("line") or ""),
        next=str(entry.get("next_context") or ""))
    try:
        response = client.chat.completions.create(
            model=model, temperature=decoding["temperature"],
            max_tokens=decoding["max_tokens"],
            # REASONING OFF. Every sibling harness pins this; without it a
            # thinking model spends the whole budget on its preamble - measured
            # at 213 tokens on this box - and returns an empty string that
            # scores as a WRONG ANSWER rather than as a failure.
            extra_body={"reasoning_effort": "none"},
            messages=[{"role": "user", "content": prompt}])
    except Exception as exc:                                  # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {str(exc)[:120]}"
    choice = response.choices[0]
    raw = choice.message.content or ""
    if choice.finish_reason == "length" and not clean_answer(raw):
        # Truncated before saying anything: a budget problem, not an opinion.
        return None, raw, "truncated: finish_reason=length with no name"
    if not clean_answer(raw):
        return None, raw, "empty reply"
    return clean_answer(raw), raw, None


def summarise(rows):
    """Counts that keep failures, declines and wrong answers apart."""
    failed = sum(1 for r in rows if r["predicted"] is None)
    declined = sum(1 for r in rows if r["predicted"] == DECLINE)
    correct = sum(1 for r in rows if r["correct"])
    answered = len(rows) - failed - declined
    by_type = {}
    for row in rows:
        kind = (row.get("candidate_provenance") or "unknown").split("|")[-1]
        bucket = by_type.setdefault(kind, {"n": 0, "correct": 0,
                                           "declined": 0, "failed": 0,
                                           "wrong": 0})
        bucket["n"] += 1
        if row["predicted"] is None:
            bucket["failed"] += 1
        elif row["predicted"] == DECLINE:
            bucket["declined"] += 1
        elif row["correct"]:
            bucket["correct"] += 1
        else:
            bucket["wrong"] += 1
    return {
        "n": len(rows), "failed_requests": failed, "declined": declined,
        "answered": answered, "correct": correct,
        # The figure comparable with the published forced-choice number.
        "accuracy_when_answered": (round(correct / answered, 4)
                                   if answered else None),
        # Declines charged as misses, for a run that must answer everything.
        "accuracy_counting_declines": (round(correct / (answered + declined), 4)
                                       if answered + declined else None),
        "by_quote_type": by_type,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fixtures", nargs="+",
                    default=sorted(glob.glob(os.path.join(
                        FIXTURES, "attribution_gold_pdnc_*.json"))))
    ap.add_argument("--model", default="qwen3-14b")
    ap.add_argument("--base-url", default="http://127.0.0.1:8090/v1")
    ap.add_argument("--limit", type=int, default=200,
                    help="quotes per book, sampled at random (see --seed)")
    ap.add_argument("--seed", type=int, default=20260819)
    ap.add_argument("--max-tokens", type=int, default=32)
    ap.add_argument("--tag", default="current",
                    help="artifact suffix; two runs with different --limit "
                         "must not share one path")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = args.out or os.path.join(
        REPO, "ab_test_runtime", "experiments",
        f"two_stage_attribution__{args.tag}.json")

    # WHICH MODEL IS ACTUALLY LOADED. models.list() proves a server answers,
    # not which weights it holds - and llama.cpp ignores the model field in the
    # request, so a leftover server (or a LoRA left at a persisted scale) would
    # be measured and reported as this one.
    from experiments.pdnc_narrator_prior import get_llama_server_environment
    environment = get_llama_server_environment(args.base_url, args.model)

    client = build_client(args.base_url)
    decoding = {"temperature": 0.0, "max_tokens": args.max_tokens,
                "reasoning_effort": "none", "limit": args.limit,
                "seed": args.seed, "sampling": "random",
                "context": "as stored in the fixture (400 chars each side)"}
    record = ExperimentRecord(
        "two_stage_attribution", REPO, args.model, args.base_url,
        args.fixtures[0], decoding,
        notes="One request per quote with the cast supplied, against the "
              "one-pass baseline of 83.6% on these same three books "
              "(GOALS.md goal 1.3). Not comparable to 61.7%, which is a "
              "different experiment on different books.",
        environment=environment)
    record.enable_checkpoint(out + ".ckpt")

    for path in args.fixtures:
        book = os.path.basename(path).replace(".json", "")
        with open(path, encoding="utf-8") as handle:
            fixture = json.load(handle)
        entries = fixture["entries"]
        if args.limit and args.limit < len(entries):
            entries = random.Random(args.seed).sample(entries, args.limit)
        groups = alias_groups(fixture)
        roster = roster_lines(fixture)
        for index, entry in enumerate(entries, 1):
            gold_id = f"{book}:{entry['id']}"
            if record.done("single", gold_id):
                continue
            answer, raw, failure = ask(client, args.model, entry, roster, decoding)
            predicted = None if failure else answer
            correct = bool(predicted and predicted != DECLINE
                           and same_speaker(entry.get("expected_speaker"),
                                            predicted, groups))
            # THE NAMES, not the display lines. `roster` is prompt text -
            # "MRS. BENNET [also: BENNET]" - and manifest.add records
            # `in_candidates` as an exact membership test, so passing the
            # decorated form made it False for every character that has an
            # alias: it reported the expected speaker missing from the cast on
            # 2,250 of 2,494 rows when the true figure is ZERO. The prompt keeps
            # the aliases; the artifact records the set they belong to.
            record.add("single", gold_id, entry.get("line"),
                       entry.get("expected_speaker"), predicted, correct,
                       candidates=roster_names(roster),
                       provenance=f"single|{book}|{entry.get('quote_type')}",
                       raw=(raw if raw is not None else failure))
            if index % 25 == 0:
                print(f"  {book}: {index}/{len(entries)}", flush=True)
        rows = [r for r in record.rows if r["id"].startswith(book + ":")]
        stats = summarise(rows)
        # No format specifier on a value that can be None: that raised
        # TypeError on the all-failed path and lost the book's evidence.
        print(f"{book}: answered {stats['answered']}/{stats['n']}, "
              f"correct {stats['correct']}, declined {stats['declined']}, "
              f"failed {stats['failed_requests']}, "
              f"accuracy_when_answered {stats['accuracy_when_answered']}")

    record.meta["per_book"] = {
        os.path.basename(p).replace(".json", ""): summarise(
            [r for r in record.rows
             if r["id"].startswith(os.path.basename(p).replace(".json", "") + ":")])
        for p in args.fixtures}
    record.meta["overall"] = summarise(record.rows)
    written = record.write(out)
    print(f"\nwrote {written}")


if __name__ == "__main__":
    main()
