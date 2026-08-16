"""Find a measure that agrees with a listener's ear, so it can be used where
the listener has no ear to use.

THE PROBLEM THIS SOLVES. The only person who can rate this project's output
speaks English. Japanese and Chinese are shipped anyway, and for those the
most that can honestly be reported is "it sounds off" or "it sounds robotic" -
a verdict about naturalness with no way to say whether the words were even
right. Every other quality axis has an instrument; this one has a person, and
the person only covers one of three languages.

So use the language they DO speak to calibrate an instrument, then trust the
instrument in the two they do not.

WHY THIS IS NOT CIRCULAR. The metrics here are not tuned to the ratings. They
are the ones `voice_compare_view.voice_quality` already computes, chosen for a
documented reason before any of this: a study found jitter the single most
discriminative measure between speakers, and a synthetic voice can be too
CLEAN - human phonation wobbles cycle to cycle and a vocoder often does not.
Near-zero jitter against a human's natural variation is exactly the "robotic"
tell, and no embedding distance reports it. What is unknown is whether that
theory matches THIS pipeline's failures, which is what a rating settles.

WHAT WOULD FALSIFY IT. If the ratings do not track any measure - if the clip a
listener calls robotic has ordinary jitter, ordinary spread, ordinary HNR -
then these numbers do not capture what an ear hears, and reporting them for
Japanese would be worse than reporting nothing, because it would look like
evidence. A null result here is a real answer and must be recorded as one.

WHAT ELSE WAS CONSIDERED, 2026-08-16, so nobody re-treads it:

- **torchaudio SQUIM** (`SQUIM_SUBJECTIVE`) is already installed here, which
  made it the obvious first try. It is **blind to this axis**. Every clip in
  the package scores 4.99, saturated at the ceiling. It is not broken - adding
  noise walks it down 4.99 -> 3.65 -> 3.21 -> 2.52 - it simply measures
  DISTORTION, and synthetic speech that sounds robotic is not distorted. Free,
  local, and useless here.
- **UTMOSv2** won VoiceMOS 2024 and is the state of the art for exactly this
  question (naturalness of high-quality synthetic speech). Not installed.
  Trained predominantly on English, so applying it to Japanese is the same
  leap of faith this file exists to avoid taking silently.
- **NISQA** predicts naturalness plus four perceptual dimensions - noisiness,
  coloration, discontinuity, loudness - which are diagnostic rather than a
  single score, and "discontinuity" is close to what a listener means by
  choppy. Not installed.
- **TTSDS2** (2025) is the only candidate MULTILINGUAL by design, covering 14
  languages, which is precisely our problem. Two catches: it reports Spearman
  correlation with human judgement that "never surpasses 0.8", and it pins
  `numpy<2.0.0` while this environment runs numpy 2.2.6 - so it needs its own
  environment rather than an install here.

The Praat measures below are used first because they are already present, they
discriminate between these clips (SQUIM does not), and their reasoning was
written down before any rating existed. If a rating shows they do not track
the ear, UTMOSv2 or NISQA are the next things to try, not more Praat.

TWO STEPS.

    --measure   score every clip in the listening package, and write a blank
                rating sheet. Metrics are computed WITHOUT reading the
                concealed key, so nothing here can leak which arm is which.
    --score     join a filled-in sheet against the key and report which
                measure, if any, predicts the verdict.
"""
import argparse
import glob
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PACKAGE = os.path.join(REPO, "ab_test_runtime", "blinded_listening")
KEY = os.path.join(REPO, "ab_test_runtime", "blinded_listening_concealed_key.json")
# Lower jitter/shimmer and narrower f0 spread are the "too clean, too flat"
# direction; higher HNR is the same tell from the other side.
LOWER_IS_MORE_ROBOTIC = ("jitter_local", "shimmer_local", "f0_spread")
HIGHER_IS_MORE_ROBOTIC = ("hnr_db",)


def measure_clips():
    from voice_compare_view import voice_quality, pitch_stats
    rows = []
    for path in sorted(glob.glob(os.path.join(PACKAGE, "*.wav"))):
        name = os.path.basename(path)
        row = {"clip": name}
        row.update(voice_quality(path))
        row.update(pitch_stats(path))
        rows.append(row)
    return rows


def duplicate_clips():
    """-> {set_id: [[clip, clip], ...]} for clips that are byte-identical.

    ASKED BEFORE RATING, BECAUSE THE ANSWER CHANGES THE TASK. Four of the
    eight sets in this package present three clips of which two are the same
    file, so a third of the question is unanswerable and a conscientious
    rater would burn time deciding between identical audio. Detected by hash
    rather than by trusting the arm labels, since the point is that two
    labelled arms produced one output.
    """
    import hashlib
    seen = {}
    for path in sorted(glob.glob(os.path.join(PACKAGE, "*.wav"))):
        name = os.path.basename(path)
        set_id = name.rsplit("_", 1)[0]
        with open(path, "rb") as handle:
            digest = hashlib.md5(handle.read()).hexdigest()
        seen.setdefault(set_id, {}).setdefault(digest, []).append(name)
    return {s: [c for c in groups.values() if len(c) > 1]
            for s, groups in seen.items()
            if any(len(c) > 1 for c in groups.values())}


def run_measure(args):
    rows = measure_clips()
    if not rows:
        sys.exit(f"no clips in {PACKAGE}")
    duplicates = duplicate_clips()
    sets = {}
    for row in rows:
        set_id, letter = row["clip"].rsplit("_", 1)
        sets.setdefault(set_id, []).append(letter[0])
    sheet = {
        "instructions":
            "For each set, listen to every clip and write the letter of the "
            "one that sounds MOST ROBOTIC - flattest, most machine-like, "
            "least like a person reading. Judge delivery only; ignore which "
            "reading you prefer and ignore the words. Leave a set blank if "
            "you genuinely cannot tell, which is itself a useful answer.",
        "identical_clips": {s: g for s, g in sorted(duplicates.items())},
        "ratings": {set_id: "" for set_id in sorted(sets)},
    }
    if duplicates:
        sheet["instructions"] += (
            f" NOTE: {len(duplicates)} of {len(sets)} sets contain two clips "
            f"that are the same file - see identical_clips. Those two cannot "
            f"differ, so judge the remaining one against either of them and "
            f"do not spend time telling the pair apart.")
    document = {"clips": rows, "sets": len(sets),
                "identical_clips": duplicates,
                "note": "measured without reading the concealed key"}
    for path, data in ((args.out, document), (args.sheet, sheet)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, ensure_ascii=False)
    print(f"measured {len(rows)} clips across {len(sets)} sets -> {args.out}")
    print(f"rating sheet (fill in the letters)      -> {args.sheet}")
    return 0


def run_score(args):
    with open(args.sheet, encoding="utf-8") as handle:
        ratings = {k: v.strip().upper()
                   for k, v in json.load(handle)["ratings"].items() if v.strip()}
    if not ratings:
        sys.exit("the rating sheet is empty - nothing to correlate")
    with open(args.out, encoding="utf-8") as handle:
        clips = {row["clip"]: row for row in json.load(handle)["clips"]}
    with open(KEY, encoding="utf-8") as handle:
        key = {s["id"]: s["mapping"] for s in json.load(handle)["sets"]}

    metrics = [m for m in LOWER_IS_MORE_ROBOTIC + HIGHER_IS_MORE_ROBOTIC]
    hits = {m: 0 for m in metrics}
    scored = 0
    arm_calls = {}
    for set_id, letter in ratings.items():
        chosen = f"{set_id}_{letter}.wav"
        members = [c for c in clips if c.startswith(set_id + "_")]
        if chosen not in clips or len(members) < 2:
            continue
        scored += 1
        arm = key.get(set_id, {}).get(chosen, {}).get("arm", "?")
        arm_calls[arm] = arm_calls.get(arm, 0) + 1
        for metric in metrics:
            values = {c: clips[c].get(metric) for c in members}
            if any(v is None for v in values.values()):
                continue
            # Does the metric point at the same clip the listener did?
            pick = (min if metric in LOWER_IS_MORE_ROBOTIC else max)(
                values, key=values.get)
            hits[metric] += (pick == chosen)
    if not scored:
        sys.exit("no rated set could be matched to measured clips")

    chance = statistics.mean(
        1 / len([c for c in clips if c.startswith(s + "_")])
        for s in ratings if [c for c in clips if c.startswith(s + "_")])
    print(f"{scored} rated sets, chance = {chance*100:.0f}%\n")
    print(f"{'measure':16}{'agrees':>8}{'of':>4}{'':3}{'rate':>7}")
    results = {}
    for metric in metrics:
        rate = hits[metric] / scored
        results[metric] = {"agreed": hits[metric], "of": scored,
                           "rate": round(rate, 3)}
        print(f"{metric:16}{hits[metric]:>8}{scored:>4}{'':3}{rate*100:>6.0f}%")
    best = max(results, key=lambda m: results[m]["rate"])
    print(f"\nwhich arm the listener called robotic: {arm_calls}")
    if results[best]["rate"] <= chance:
        print("\nNO MEASURE BEATS CHANCE. These numbers do not capture what "
              "the ear heard, and must not be reported for Japanese or "
              "Chinese as if they did. That is a real answer, not a failed "
              "run.")
    else:
        print(f"\nbest: {best} at {results[best]['rate']*100:.0f}% against "
              f"{chance*100:.0f}% chance, on {scored} sets - a direction, not "
              f"yet a validated instrument. Say n every time it is quoted.")
    document = {"scored_sets": scored, "chance": round(chance, 3),
                "agreement": results, "arms_called_robotic": arm_calls}
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1)
    print(f"\nwrote {args.report}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "robotic_proxy_clips.json"))
    ap.add_argument("--sheet", default=os.path.join(
        REPO, "ab_test_runtime", "blinded_listening", "rating_sheet.json"))
    ap.add_argument("--report", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "robotic_proxy.json"))
    args = ap.parse_args()
    if args.measure == args.score:
        sys.exit("pass exactly one of --measure or --score")
    return run_measure(args) if args.measure else run_score(args)


if __name__ == "__main__":
    sys.exit(main())
