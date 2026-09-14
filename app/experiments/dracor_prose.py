"""Give play-script rows the narrative frames a novel would have.

The DraCor adapters (GOALS 1.2, 2026-09-14) taught nothing the product
window could use: plays label every line but carry no `said X` frame, and
the books that lost are the ones whose attribution runs on frames. This
takes the PDNC-shaped rows `dracor_trainset.py` wrote and inserts frames
whose speaker is KNOWN from the TEI, so the label stays deterministic and
only the narration is synthetic. No LLM: a template says exactly which
line a frame belongs to, which is the one thing the adapter is meant to
learn.

The shapes are the product's own (`three_pass` splits frames into their own
NARRATOR entries, and most quotes have none):

- none  - the line stands bare, as most product quotes do;
- post  - a NARRATOR entry AFTER the line names its speaker ("said Adelbert.");
- pre   - a NARRATOR beat BEFORE the line names its speaker ("Adelbert frowned.").

A neighbouring line gets its own frame by the same draw, so a row's
previous_context is sometimes the PREVIOUS speaker's post-frame and its
next_context the NEXT speaker's beat - the two confusable shapes, with the
truth attached. English rows only: the frames are English.
"""
import argparse
import collections
import glob
import json
import os
import random
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

SAID = ["said", "replied", "asked", "answered", "cried", "muttered", "whispered",
        "added", "continued", "exclaimed", "murmured", "called", "remarked",
        "demanded", "went on", "insisted", "snapped", "sighed", "laughed"]
INVERTIBLE = {"said", "cried", "asked", "replied", "answered"}
BEATS = ["frowned", "nodded", "hesitated", "stepped forward", "looked up",
         "was silent for a moment", "smiled", "turned away", "paused",
         "laughed", "shook off the thought", "drew a breath", "looked at the door"]
TRAILERS = ["", "", "", ", frowning", ", after a moment", ", quietly", ", turning away"]
SHAPES = (("none", 0.35), ("post", 0.35), ("pre", 0.30))


def display_name(roster_name):
    """ADELBERT -> Adelbert, MRS. BENNET -> Mrs. Bennet. Frames say the name
    the way a novel would; the label stays the roster spelling."""
    return " ".join(w.capitalize() for w in roster_name.split())


def draw_shape(rng):
    r = rng.random()
    for shape, p in SHAPES:
        r -= p
        if r < 0:
            return shape
    return SHAPES[-1][0]


def post_frame(name, rng):
    verb = rng.choice(SAID)
    form = rng.random()
    if verb in INVERTIBLE and form < 0.4:
        return "%s %s." % (verb, name)
    return "%s %s%s." % (name, verb, rng.choice(TRAILERS))


def pre_beat(name, rng):
    return "%s %s." % (name, rng.choice(BEATS))


def frame_row(row, rng):
    """-> a copy of `row` with frames drawn for the target and its neighbours,
    plus a `frames` field recording what was drawn (for the audit)."""
    before, target, after = row["context"]
    prev_who, next_who = row.get("context_speakers") or [None, None]
    me = display_name(row["teacher"])
    shape = draw_shape(rng)
    prev_ctx, next_ctx = dict(before), dict(after)
    drawn = {"target": shape, "previous": "line", "next": "line"}
    if shape == "pre":
        prev_ctx = {"type": "NARRATOR", "text": pre_beat(me, rng)}
        drawn["previous"] = "own_beat"
    elif before["type"] == "SPOKEN" and prev_who and draw_shape(rng) == "post":
        prev_ctx = {"type": "NARRATOR", "text": post_frame(display_name(prev_who), rng)}
        drawn["previous"] = "previous_speaker_post"
    if shape == "post":
        next_ctx = {"type": "NARRATOR", "text": post_frame(me, rng)}
        drawn["next"] = "own_post"
    elif after["type"] == "SPOKEN" and next_who and draw_shape(rng) == "pre":
        next_ctx = {"type": "NARRATOR", "text": pre_beat(display_name(next_who), rng)}
        drawn["next"] = "next_speaker_beat"
    out = dict(row)
    out["context"] = [prev_ctx, dict(target), next_ctx]
    out["frames"] = drawn
    return out


def main():
    from experiments.provenance import provenance
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--in-dir", required=True, help="dracor_trainset.py output")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seed", type=int, default=20260914)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    os.makedirs(args.out_dir, exist_ok=True)
    tally, rows_out = collections.Counter(), 0
    for path in sorted(glob.glob(os.path.join(args.in_dir, "train__*.jsonl"))):
        with open(path, encoding="utf-8") as fh, \
                open(os.path.join(args.out_dir, os.path.basename(path)), "w", encoding="utf-8") as out:
            for raw in fh:
                row = json.loads(raw)
                if row.get("language") != "en":
                    tally["skipped_non_english"] += 1
                    continue
                framed = frame_row(row, rng)
                for k, v in framed["frames"].items():
                    tally["%s:%s" % (k, v)] += 1
                out.write(json.dumps(framed, ensure_ascii=False) + "\n")
                rows_out += 1
    src = json.load(open(os.path.join(args.in_dir, "manifest.json"), encoding="utf-8"))
    manifest = {"rows": rows_out, "seed": args.seed, "shapes": dict(SHAPES),
                "tally": dict(sorted(tally.items())), "source_manifest": src.get("provenance"),
                "provenance": provenance(__file__, args)}
    with open(os.path.join(args.out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    print(json.dumps({"rows": rows_out, "tally": manifest["tally"]}, indent=1))


if __name__ == "__main__":
    main()
