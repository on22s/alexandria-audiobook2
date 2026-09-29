"""Does a freshly trained voice adapter know how to stop talking?

THE FAILURE THIS GATES. Two adapters trained on 2026-08-06 - one English, one
Japanese - produced exactly 163.8 seconds of audio for every held-out line,
against a human reference of 7.3 seconds. 163.8s is the token ceiling: they
never emitted an end of speech and were cut off, every time, on every line.

Both were trained at train_lora.py's CLI default of `--lr 5e-6`. All 75
adapters that actually work in this library were trained at 1e-6. The default
is five times the rate the library uses and is used by no working adapter, so
anyone driving the CLI without --lr gets this.

It cost two three-hour generation runs that timed out and produced nothing,
and a queued sweep would have trained seven more the same way.

WHY A SEPARATE GATE RATHER THAN A CHECK INSIDE TRAINING. Training loss does not
show it: the runaway adapters reached 2.9 and 3.4, which look unremarkable. The
defect is only visible in GENERATED OUTPUT, so the only honest test is to
generate a few lines and measure them.

CHEAP ON PURPOSE. A handful of lines against their human durations answers it
in a couple of minutes. A runaway LINE is not subtle - an order of magnitude
out, not a few percent - so this needs no statistics, only a look.

JUDGED ON THE WORST LINE, NOT THE MEDIAN (2026-09-28). A runaway adapter does
not have to run away on every line: an LJSpeech adapter at lr 2e-6 ran 16x and
18x on two of its five lines and 0.9-1.1x on the other three, so its median was
1.1x and the median rule passed it. A listener hits those lines. The median is
still reported.
"""
import argparse
import json
import os
import statistics
import sys


def judge_stop_ratios(ratios, max_ratio):
    """(passed, verdict) for generated/human duration ratios: every line must be
    within max_ratio. Pure, so the rule is tested without generating audio."""
    median, worst = statistics.median(ratios), max(ratios)
    over = sum(1 for r in ratios if r > max_ratio)
    if not over:
        return True, (f"PASS - the adapter stops. Worst line {worst:.1f}x the human "
                      f"duration, median {median:.1f}x.")
    return False, (f"FAIL - the adapter does not stop on {over} of {len(ratios)} lines "
                   f"(worst {worst:.1f}x, median {median:.1f}x the human duration); "
                   f"the 2026-08-06 runaway adapters were 12x and hit a 22x ceiling. "
                   f"Do not spend a generation run on this. Check --lr: the library "
                   f"trains at 1e-6 and train_lora.py defaults to 5e-6; 2e-6 also "
                   f"runs away (2026-09-28).")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", required=True,
                    help="build.json with test rows carrying human durations")
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--config", default=os.path.join(APP, "config.json"))
    ap.add_argument("--lines", type=int, default=5)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--max-ratio", type=float, default=3.0,
                    help="generated/human duration above which the adapter is "
                         "judged not to stop. 3x is generous - the observed "
                         "failure was 12x and the ceiling 22x")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    build = json.load(open(args.build, encoding="utf-8"))
    rows = build["test"][:args.lines]
    if not rows:
        sys.exit("no test rows in the build")
    out_dir = args.out_dir or os.path.join(os.path.dirname(args.adapter),
                                           "stop_check")
    os.makedirs(out_dir, exist_ok=True)

    from tts import TTSEngine
    from experiments.generation import render, GenerationFailed
    import soundfile as sf
    engine = TTSEngine(json.load(open(args.config, encoding="utf-8")))
    entry = {"type": "lora", "adapter_path": os.path.relpath(args.adapter, REPO),
             "seed": str(args.seed)}

    print(f"{len(rows)} lines through {os.path.basename(args.adapter)}\n")
    print(f"  {'id':30}{'human':>9}{'gen':>9}{'ratio':>8}")
    ratios, measured = [], []
    for row in rows:
        wav = os.path.join(out_dir, row["id"] + ".wav")
        try:
            render(engine, row["text"], "", "SPEAKER", {"SPEAKER": entry},
                   entry, wav)
        except GenerationFailed as exc:
            print(f"  {row['id'][:28]:30} GENERATION FAILED: {str(exc)[:50]}")
            continue
        info = sf.info(wav)
        gen = info.frames / float(info.samplerate)
        human = float(row["seconds"])
        ratio = gen / max(human, 1e-6)
        ratios.append(ratio)
        measured.append({"id": row["id"], "human_seconds": round(human, 2),
                         "generated_seconds": round(gen, 2),
                         "ratio": round(ratio, 2)})
        print(f"  {row['id'][:28]:30}{human:8.1f}s{gen:8.1f}s{ratio:8.1f}x")

    if not ratios:
        sys.exit("nothing generated; cannot judge the adapter")

    median = statistics.median(ratios)
    worst = max(ratios)
    ok, verdict = judge_stop_ratios(ratios, args.max_ratio)
    print(f"\n  median {median:.1f}x, worst {worst:.1f}x, "
          f"threshold {args.max_ratio:.1f}x (every line)")
    print(f"\n  {verdict}")

    doc = {"adapter": os.path.relpath(args.adapter, REPO),
           "seed": args.seed, "max_ratio": args.max_ratio,
           "median_ratio": median, "worst_ratio": worst,
           "passed": ok, "verdict": verdict, "lines": measured}
    try:
        from experiments.provenance import provenance
        doc["provenance"] = provenance(__file__, args)
    except Exception as exc:                            # noqa: BLE001
        doc["provenance"] = {"error": str(exc)[:120]}
    out = args.out or os.path.join(REPO, "ab_test_runtime", "experiments",
                                   "adapter_stop_check.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    print(f"\nwrote {out}")
    # Non-zero so a chain refuses to continue into a doomed generation run.
    sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
