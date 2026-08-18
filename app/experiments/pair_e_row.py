"""Compare an alternative /e/ respelling against the shipped -eh, PAIRED.

THE QUESTION. Whole-word recovery over 7,607 rescored terms is 15.0% overall
but 6.6% for words containing an -eh mora against 18.0% for words without one,
and the gap survives a length control at every length (2.3-3.9x, n=2,031).
That names one row of the derivation table. It does not say the row is wrong -
"seh" may read as a schwa, or the model may simply prefer other shapes, and
both predict the same penalty. So the replacement is measured.

WHY PAIRED. The arms differ only in that row, so a between-sample comparison
of headline percentages would be measuring which terms each run happened to
draw. Every number here is computed over terms present in BOTH artifacts.

THE CONTROL IS THE POINT. Each artifact also holds a `plain` arm - no
respelling at all - which is the SAME condition measured twice, in two runs,
hours apart. It is the noise floor, and it is not small: on the -e arm, 34 of
391 terms flipped verdict on identical input, so the pipeline is materially
less deterministic than the temperature-0 LLM work would suggest (that was the
LLM; this is TTS -> ASR). If a difference in the respelled arm is not clearly
larger than the difference in the plain arm, it is not a difference.

Measured 2026-08-17, -e arm: respelled 2.0% against -eh's 7.9%, McNemar
p=1.9e-4; plain control p=0.39. The alternative lost four-fold.
"""
import argparse
import json
import os
import re
import sys
from math import comb

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASELINE = os.path.join(REPO, "ab_test_runtime", "experiments",
                        "respelling_measure_rescored.json")


def load(path):
    """-> ({term: row} for scored rows, [rows the runner skipped])."""
    with open(path, encoding="utf-8") as fh:
        results = json.load(fh)["results"]
    scored = {r["term"]: r for r in results if "plain_recovers_word" in r}
    return scored, [r for r in results if "plain_recovers_word" not in r]


def degenerate(transcript):
    """One kana repeated: TTS collapse, not a mispronunciation.

    `sensei` came back as すすすすすすすすす. Counting that as a failed
    respelling would blame the derivation table for a synthesis failure, so it
    is reported separately rather than folded into the rate.
    """
    return bool(re.fullmatch(r"(.)\1{5,}", (transcript or "").strip()))


def mcnemar(wins, losses):
    """Two-sided exact p over the DISCORDANT pairs only.

    Terms both arms get right, or both get wrong, carry no information about
    which arm is better and are correctly excluded.
    """
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    return min(1.0, sum(comb(n, i) for i in range(k + 1)) * 2 / 2 ** n)


def compare(arm_path, baseline_path=BASELINE):
    arm, skipped = load(arm_path)
    base, _ = load(baseline_path)
    shared = sorted(set(arm) & set(base))
    out = {
        "arm": os.path.basename(arm_path),
        "baseline": os.path.basename(baseline_path),
        "arm_scored": len(arm), "arm_skipped": len(skipped),
        "shared_terms": len(shared),
    }
    if not shared:
        out["error"] = "no shared terms; nothing can be compared"
        return out
    for field, label in (("respelled_recovers_word", "respelled"),
                         ("plain_recovers_word", "plain")):
        wins = [t for t in shared if arm[t][field] and not base[t][field]]
        losses = [t for t in shared if base[t][field] and not arm[t][field]]
        out[label] = {
            "arm_rate": sum(arm[t][field] for t in shared) / len(shared),
            "baseline_rate": sum(base[t][field] for t in shared) / len(shared),
            "arm_only": len(wins), "baseline_only": len(losses),
            "p_value": mcnemar(len(wins), len(losses)),
            "arm_wins_examples": wins[:12],
            "baseline_wins_examples": losses[:12],
        }
    out["degenerate_clips"] = {
        "arm": sum(degenerate(arm[t]["respelled_heard"]) for t in shared),
        "baseline": sum(degenerate(base[t]["respelled_heard"]) for t in shared),
    }
    return out


def render(result):
    lines = [f"=== {result['arm']} vs {result['baseline']} ==="]
    if "error" in result:
        return "\n".join(lines + ["  " + result["error"]])
    lines.append(f"  scored={result['arm_scored']} skipped={result['arm_skipped']} "
                 f"shared={result['shared_terms']}")
    for label in ("respelled", "plain"):
        r = result[label]
        tail = "  <- same condition twice: the noise floor" if label == "plain" else ""
        lines.append(f"  {label:10s} arm {r['arm_rate']:6.1%}  "
                     f"baseline {r['baseline_rate']:6.1%}   "
                     f"discordant {r['arm_only']}/{r['baseline_only']}  "
                     f"p={r['p_value']:.2e}{tail}")
    d = result["degenerate_clips"]
    lines.append(f"  degenerate clips: arm {d['arm']}, baseline {d['baseline']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("arms", nargs="+", help="arm artifact(s) to compare")
    ap.add_argument("--baseline", default=BASELINE)
    ap.add_argument("--out", help="write the comparison as JSON")
    args = ap.parse_args()

    results = [compare(a, args.baseline) for a in args.arms]
    for result in results:
        print(render(result))
    if args.out:
        # WHICH baseline, by hash. This is DVC's `deps` idea in miniature -
        # record the inputs a stage consumed, by content, so a later reader can
        # tell whether the copy they hold is the one that produced the number -
        # and it is what pays for gpu_job.sh no longer counting a rewritten
        # artifact as dirt. The dirty-tree gate no longer counts a
        # modified artifact, deliberately - a run rewriting its outputs is not
        # a run whose code changed - so the comparison records the inputs it
        # READ instead. An edited baseline is the failure this catches, and it
        # is one the old flag could only have called "something changed".
        sys.path.insert(0, os.path.join(REPO, "app"))
        from experiments.manifest import read_inputs
        payload = {"comparisons": results,
                   "read_inputs": read_inputs([args.baseline, *args.arms], REPO)}
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
