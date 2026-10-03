"""Does per-line age follow a light-novel time skip? (#653, before per-state voices)

Book: Mushoku Tensei Volume 01 (the owner's copy; text and outputs stay out of
the repository). The narrator-protagonist Rudeus is reborn as a baby and the
volume follows him to about seven, stating his age as it goes. The timeline
below is those statements, by chapter - the gold for this check:

    Prologue  "a thirty-four-year-old man" (his previous life)      adult
    Ch 1-2    "he isn't even a year old yet"                         infant
    Ch 3      "I was only two years old"                             toddler
    Ch 4      "I turned three years old"                             toddler/young_child
    Ch 5-7    "a five-year-old", "your five-year-old son"            young_child
    Ch 8-9    "I was six years old"                                  child
    Ch 10-11  "I was now seven years old"                            child

THE TRAP this also measures: Ch 9 says "I looked like a child, but mentally, I
was an unemployed bum over forty years old". For a voice the right answer is
the body's age (a child speaks the line aloud); the prompt says "looks and
acts", which could pull the model toward ADULT.

CHECKS, fixed before any run (2026-10-03), on 2 runs with
generation.three_pass_speaker_traits on:
  1. Rudeus's modal age_group per chapter is within one band of the timeline
     (owner: one band does not change a voice) in >= 80% of the chapters he
     speaks in, in BOTH runs.
  2. His Voices summary shows him growing up (at least one state change, to an
     older band) with no change back down by 2+ bands.
  3. No character with >= 10 lines flips A -> B -> A by 2+ bands more than once.
  4. Reported, not gated: post-prologue Rudeus lines labelled ADULT or older.
Per-state voices (PR 2) is built on per-line age only if 1-3 pass.
"""
import argparse
import collections
import json
import os
import re
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.provenance import provenance                      # noqa: E402
from speaker_traits import AGE_GROUP_NAMES, get_age_distance, get_speaker_trait_summary  # noqa: E402

PROTAGONIST = ("RUDEUS", "RUDY", "RUDEUS GREYRAT")
TIMELINE = {0: ("adult",), 1: ("infant",), 2: ("infant",), 3: ("toddler",),
            4: ("toddler", "young_child"), 5: ("young_child",), 6: ("young_child",),
            7: ("young_child",), 8: ("child",), 9: ("child",), 10: ("child",), 11: ("child",)}
MIN_CHAPTER_SHARE = 0.8
MIN_LINES_FOR_FLICKER = 10


def get_chapters(entries):
    """-> chapter number per entry (0 = prologue), from the headings.

    Pass 1 leaves a heading INSIDE a longer narration entry ("...graduation?
    Chapter 6: Reasons for Respect I had not left..."), so it is searched for
    anywhere in the entry. An entry holding several headings is the table of
    contents and is ignored. The first version only matched a heading at the
    start of an entry and put all ~224 of Rudeus's lines in one chapter -
    caught on an interim read, fixed before the final scoring.
    """
    chapter, out = 0, []
    for entry in entries:
        headings = re.findall(r"Chapter (\d+):", entry.get("text") or "")
        if len(headings) == 1 and entry.get("speaker") == "NARRATOR":
            chapter = int(headings[0])
        out.append(chapter)
    return out


def is_protagonist(speaker):
    value = (speaker or "").upper()
    return any(value == name or value.startswith(name + " ") for name in PROTAGONIST)


def get_flips(ages):
    """A -> B -> A reversals of 2+ bands in one character's known ages."""
    runs = [age for age in ages if age != "unknown"]
    collapsed = [age for i, age in enumerate(runs) if i == 0 or age != runs[i - 1]]
    return sum(1 for a, b, c in zip(collapsed, collapsed[1:], collapsed[2:])
               if a == c and get_age_distance(a, b) >= 2)


def score_run(entries):
    chapters = get_chapters(entries)
    by_chapter = collections.defaultdict(list)
    lines_by_speaker = collections.defaultdict(list)
    adult_after_prologue = 0
    for entry, chapter in zip(entries, chapters):
        if "speaker_age_group" not in entry:
            continue
        lines_by_speaker[entry["speaker"]].append(entry)
        if is_protagonist(entry["speaker"]):
            by_chapter[chapter].append(entry["speaker_age_group"])
            if chapter > 0 and entry["speaker_age_group"] in ("adult", "middle_aged", "elderly"):
                adult_after_prologue += 1
    per_chapter = {}
    for chapter, ages in sorted(by_chapter.items()):
        known = [a for a in ages if a != "unknown"]
        modal = collections.Counter(known).most_common(1)[0][0] if known else "unknown"
        expected = TIMELINE.get(chapter, ())
        within = modal != "unknown" and any(get_age_distance(modal, e) <= 1 for e in expected)
        per_chapter[chapter] = {"lines": len(ages), "modal": modal, "expected": list(expected),
                                "within_one_band": within}
    protagonist_lines = [e for s, lines in lines_by_speaker.items() if is_protagonist(s) for e in lines]
    summary = get_speaker_trait_summary(protagonist_lines) or {"states": []}
    states = [s["age_group"] for s in summary["states"]]
    grew = any(AGE_GROUP_NAMES.index(b) > AGE_GROUP_NAMES.index(a)
               for a, b in zip(states, states[1:]) if "unknown" not in (a, b))
    fell = any(AGE_GROUP_NAMES.index(a) - AGE_GROUP_NAMES.index(b) >= 2
               for a, b in zip(states, states[1:]) if "unknown" not in (a, b))
    flips = {s: get_flips([e["speaker_age_group"] for e in lines])
             for s, lines in lines_by_speaker.items() if len(lines) >= MIN_LINES_FOR_FLICKER}
    scored = [c for c in per_chapter.values() if c["modal"] != "unknown"]
    share = sum(c["within_one_band"] for c in scored) / len(scored) if scored else 0.0
    return {"per_chapter": per_chapter, "chapter_share": round(share, 3),
            "protagonist_states": summary["states"], "grew": grew, "fell_two_bands": fell,
            "flips": flips, "adult_lines_after_prologue": adult_after_prologue,
            "checks": {"1_timeline": share >= MIN_CHAPTER_SHARE, "2_growth": grew and not fell,
                       "3_no_flicker": all(n <= 1 for n in flips.values())}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run", action="append", required=True, help="a script / checkpoint JSON")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    from generation_checkpoint_deltas import load_generation_delta_checkpoint
    runs = []
    for path in args.run:
        data = load_generation_delta_checkpoint(path)
        entries = [e for e in (data.get("named") if isinstance(data, dict) else data) or [] if e]
        runs.append({"run": os.path.basename(path), **score_run(entries)})
    verdict = all(all(r["checks"].values()) for r in runs)
    doc = {"runs": runs, "per_state_voices_supported": verdict, "provenance": provenance(__file__, args)}
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1)
    print(json.dumps({"verdict": verdict, "runs": [{k: r[k] for k in ("run", "chapter_share",
          "protagonist_states", "flips", "adult_lines_after_prologue", "checks")} for r in runs]},
          indent=1))


if __name__ == "__main__":
    main()
