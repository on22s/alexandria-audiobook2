"""Build a blinded listening package for the separator forms.

WHY. The instrument and the ear have disagreed here before. On 2026-08-18 the
ASR metric said `-ay` recovered whole words twice as often as `-eh`, and on the
four terms that justified the change the listener chose `-ay` zero times. The
mechanism turned out to be pauses, and the measurement now says separators
cause them: `none` is indistinguishable from un-respelled audio (43/74,
p=0.20), `space` pauses (87/107, p=3.8e-11), `dot` pauses hardest (115/118,
p=1.65e-30).

That is a measurement of silence, not of how anything sounds. The listener is
the only instrument for the second question, and this builds the package.

BLINDED PROPERLY. The four takes of each term are shuffled per term with a
recorded seed, and the key is written to a SEPARATE file that the page never
contains - so the answer cannot be read out of the HTML, deliberately or
otherwise. The page records only "term -> chosen letter"; this script decodes.

The listener does not speak Japanese, which is the right shape for this
question: they are not being asked whether the word is correct, only which
take sounds like a person saying a word rather than a machine spelling one.
"""
import argparse
import base64
import json
import os
import random
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

RUNTIME = os.path.join(REPO, "ab_test_runtime")
# form -> (work directory, clip suffix). `plain` is the un-respelled control
# and must be in every set: without it "which sounds best" cannot distinguish
# "this respelling is good" from "all of them are worse than doing nothing".
FORMS = {
    "plain":  ("respelling_measure",  "_plain.wav"),
    "hyphen": ("respelling_measure",  "_respelled.wav"),
    "none":   ("respelling_sep_none", "_respelled.wav"),
    "dot":    ("respelling_sep_dot",  "_respelled.wav"),
}
LETTERS = "ABCD"


def encode(path, bitrate="32k"):
    """-> a base64 opus data URI, or None if ffmpeg cannot read the clip."""
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", path, "-c:a", "libopus", "-b:a", bitrate,
         "-f", "ogg", "-"], capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return None
    return "data:audio/ogg;base64," + base64.b64encode(result.stdout).decode()


def pause_spread(pauses_path):
    """-> {term: {form: pause_seconds}} from the measured artifact."""
    with open(pauses_path, encoding="utf-8") as handle:
        rows = json.load(handle)["results"]
    out = {}
    for row in rows:
        seconds = {form: row[form]["pause_seconds"]
                   for form in FORMS if form in row}
        # The artifact names the shipped form `eh`, this package calls it
        # `hyphen`; same clips, clearer to a listener.
        if "eh" in row:
            seconds["hyphen"] = row["eh"]["pause_seconds"]
        out[row["term"]] = seconds
    return out


def choose_terms(spread, count, max_seconds=8.0):
    """Widest measured spread first, so the contrast is audible at all.

    A term whose forms all pause identically tests nothing: the listener would
    be guessing, and a package of those would return noise that looks like
    "no difference". Clips above max_seconds are dropped as TTS collapses -
    `meiru` recorded a 14.3s gap, which is a synthesis failure rather than a
    separator effect and would tell the listener only that one take is broken.
    """
    ranked = []
    for term, seconds in spread.items():
        # RANK on whatever the pause artifact measured, but REQUIRE the clips.
        # The 3-arm artifact holds plain/none/space/dot and no hyphen, so
        # demanding a pause figure for every FORM rejected all 119 terms - the
        # ranking input and the package contents are different questions.
        if not seconds:
            continue
        if not all(os.path.exists(os.path.join(RUNTIME, d, term + suffix))
                   for d, suffix in FORMS.values()):
            continue
        values = list(seconds.values())
        if max(values) > max_seconds:
            continue
        ranked.append((max(values) - min(values), term))
    ranked.sort(reverse=True)
    return [term for _, term in ranked[:count]]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--terms", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260819)
    parser.add_argument("--pauses", default=os.path.join(
        RUNTIME, "experiments", "respelling_pauses_separators_3arm.json"))
    parser.add_argument("--out-data", default=os.path.join(
        RUNTIME, "experiments", "earcheck_separator_package.json"))
    parser.add_argument("--out-key", default=os.path.join(
        RUNTIME, "experiments", "earcheck_separator_key.json"))
    args = parser.parse_args()

    spread = pause_spread(args.pauses)
    terms = choose_terms(spread, args.terms)
    if not terms:
        raise SystemExit("no term has a clip in every form; nothing to build")

    rng = random.Random(args.seed)
    package, key = [], []
    for term in terms:
        order = list(FORMS)
        rng.shuffle(order)
        takes, mapping = [], {}
        for letter, form in zip(LETTERS, order):
            directory, suffix = FORMS[form]
            audio = encode(os.path.join(RUNTIME, directory, term + suffix))
            if audio is None:
                break
            takes.append({"letter": letter, "audio": audio})
            mapping[letter] = form
        if len(takes) != len(FORMS):
            print("  skipped %s: a take would not encode" % term, flush=True)
            continue
        package.append({"term": term, "takes": takes})
        key.append({"term": term, "letters": mapping,
                    "measured_pause_seconds": spread[term]})
        print("  %-14s %s" % (term, " ".join(mapping[l] for l in LETTERS)),
              flush=True)

    if not package:
        raise SystemExit("every term failed to encode; package not written")

    with open(args.out_data, "w", encoding="utf-8") as handle:
        json.dump({"terms": package, "seed": args.seed,
                   "provenance": provenance(__file__, args)}, handle)
    with open(args.out_key, "w", encoding="utf-8") as handle:
        json.dump({"key": key, "seed": args.seed,
                   "provenance": provenance(__file__, args)}, handle, indent=1)
    size = os.path.getsize(args.out_data) / 1048576
    print("wrote %s (%.1f MB) and the concealed key %s"
          % (args.out_data, size, args.out_key))


if __name__ == "__main__":
    main()
