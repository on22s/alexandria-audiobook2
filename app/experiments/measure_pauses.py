"""Do respellings make the voice break the word into pieces?

THE LISTENER'S HYPOTHESIS, AND WHERE IT CAME FROM. The 2026-08-18 blinded
listening test (respelling_earcheck.json) disagreed with the ASR metric on 6 of
8 terms, and six of the eight notes said the same thing unprompted: "weird
pauses", "sounded robotic", "the biggest problem is the pausing". That is a
mechanism, not an impression: a hyphenated respelling like `ee-say-kah-ee` may
be making the voice utter pieces rather than a word.

WHY IT WOULD FOOL THE METRIC. `recovers_word` asks whether the term's kana
reading appears unbroken and in order in the transcript. A voice that says the
pieces cleanly, with gaps, satisfies that exactly - each piece transcribes
correctly - while a listener hears a chopped, robotic non-word. The metric and
the ear would then diverge systematically, which is what the listening test
found.

A first pass over the eight tested terms pointed the right way and could not
carry weight: median internal pause 0.06s plain against 0.73s respelled, but a
paired sign test at n=8 gives p=0.45. This runs it over as many terms as have
clips on disk.

Internal pauses only: leading and trailing room tone is not the voice breaking
a word up, and counting it would swamp the signal being measured.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from math import comb

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

ARMS = {
    "plain": ("respelling_measure", "_plain.wav"),
    "eh": ("respelling_measure", "_respelled.wav"),
    "ay": ("respelling_e_row_ay", "_respelled.wav"),
}


def internal_pauses(path, floor_db=-35, min_seconds=0.12, edge_seconds=1.5):
    """-> (count, total seconds) of silences inside the utterance."""
    proc = subprocess.run(
        ["ffmpeg", "-i", path, "-af",
         f"silencedetect=noise={floor_db}dB:d={min_seconds}", "-f", "null", "-"],
        capture_output=True, text=True)
    durations = [float(x) for x in
                 re.findall(r"silence_duration: ([0-9.]+)", proc.stderr)]
    inner = [d for d in durations if d < edge_seconds]
    return len(inner), round(sum(inner), 3)


def sign_test(pairs):
    """Paired sign test. Deliberately not a t-test: pause seconds are bounded
    below at zero and far from normal, and n is small."""
    up = sum(1 for a, b in pairs if b > a)
    down = sum(1 for a, b in pairs if b < a)
    n, k = up + down, min(up, down)
    if n == 0:
        return {"more": up, "fewer": down, "n": 0, "p": 1.0}
    p = min(1.0, sum(comb(n, i) for i in range(k + 1)) * 2 / 2 ** n)
    return {"more": up, "fewer": down, "n": n, "p": p}


def parse_arm(spec):
    """`name=dir` -> (name, work directory, clip suffix).

    Needed the moment a new arm exists: the separator experiment writes into
    respelling_sep_<form>/, and a pause run that silently measured only the
    three original arms would have answered a question nobody asked - the
    failure this repo keeps repeating with instruments.
    """
    name, _, directory = spec.partition("=")
    if not name or not directory:
        raise argparse.ArgumentTypeError(f"expected name=directory, got {spec!r}")
    return name, directory, "_respelled.wav"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--arm", action="append", type=parse_arm, default=None,
                    help="extra arm as name=work_directory, repeatable")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "respelling_pauses.json"))
    args = ap.parse_args()

    arms = dict(ARMS)
    for name, directory, suffix in (args.arm or []):
        arms[name] = (directory, suffix)
    runtime = os.path.join(REPO, "ab_test_runtime")
    plain_dir = os.path.join(runtime, ARMS["plain"][0])
    terms = sorted(f[:-len("_plain.wav")] for f in os.listdir(plain_dir)
                   if f.endswith("_plain.wav"))
    rows, done = [], 0
    for term in terms:
        paths = {arm: os.path.join(runtime, d, term + suffix)
                 for arm, (d, suffix) in arms.items()}
        if not all(os.path.exists(p) for p in paths.values()):
            continue                      # every arm or none: a partial row
        row = {"term": term}              # would bias whichever arm is missing
        for arm, path in paths.items():
            count, seconds = internal_pauses(path)
            row[arm] = {"pauses": count, "pause_seconds": seconds}
        rows.append(row)
        done += 1
        if done % 25 == 0:
            print(f"  {done} terms", flush=True)
            _write(args.out, rows, len(terms))
        if done >= args.limit:
            break

    _write(args.out, rows, len(terms), final=True)
    names = [a for a in arms if any(a in r for r in rows)]
    for a, b in ((x, y) for i, x in enumerate(names) for y in names[i + 1:]):
        stat = sign_test([(r[a]["pause_seconds"], r[b]["pause_seconds"]) for r in rows])
        print(f"{b} pauses more than {a}: {stat['more']}/{stat['n']} discordant, p={stat['p']:.2e}")


def _pairs(rows):
    """Every pair of arms actually present, not a hardcoded three."""
    names = [n for n in dict.fromkeys(k for r in rows for k in r if k != "term")]
    return {f"{b}_vs_{a}": sign_test(
                [(r[a]["pause_seconds"], r[b]["pause_seconds"]) for r in rows
                 if a in r and b in r])
            for i, a in enumerate(names) for b in names[i + 1:]}


def _write(out, rows, considered, final=False):
    doc = {
        "status": "complete" if final else "partial",
        "candidates_considered": considered,
        "note": "internal pauses only; leading and trailing room tone excluded",
        "results": rows,
        "summary": _pairs(rows),
        "provenance": provenance(__file__, None),
    }
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
