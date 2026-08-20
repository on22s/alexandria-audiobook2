"""Should a respelling be applied always, or only where the plain reading fails?

WHY IT MATTERS. Measured across every row, respelling LOSES: 131 wins against
219 losses over 1,582 terms, p=2.96e-06. Read as a verdict on respelling that
says "turn it off". Read as an average it hides two opposite populations, and
the split is what should decide the setting:

  - terms the plain rendering already says correctly, where a respelling can
    only do damage, and
  - terms it fails, which are the only reason the feature exists.

The second is the population the shipped feature is aimed at, and no arm had
ever reported it separately. This does, from an artifact already on disk, with
no GPU.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance          # noqa: E402
from experiments.stats import clopper_pearson, exact_mcnemar  # noqa: E402


def split(rows):
    """-> (terms plain failed, terms plain got right). Paired rows only."""
    paired = [r for r in rows
              if "plain_recovers_word" in r and "respelled_recovers_word" in r]
    return ([r for r in paired if not r["plain_recovers_word"]],
            [r for r in paired if r["plain_recovers_word"]],
            paired)


def analyse(path):
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    failed, worked, paired = split(doc.get("results") or [])
    if not paired:
        raise SystemExit("%s has no paired rows to split" % path)

    rescued = sum(1 for r in failed if r["respelled_recovers_word"])
    broken = sum(1 for r in worked if not r["respelled_recovers_word"])
    p_all, wins, losses = exact_mcnemar(
        sum(1 for r in paired
            if r["respelled_recovers_word"] and not r["plain_recovers_word"]),
        sum(1 for r in paired
            if r["plain_recovers_word"] and not r["respelled_recovers_word"]))
    rescue_lo, rescue_hi = clopper_pearson(rescued, len(failed)) if failed else (0.0, 0.0)
    break_lo, break_hi = clopper_pearson(broken, len(worked)) if worked else (0.0, 0.0)
    return {
        "artifact": os.path.basename(path),
        "completeness": doc.get("status"),
        "paired_terms": len(paired),
        "applied_to_everything": {
            "wins": wins, "losses": losses, "p_value": p_all,
            "net_words": rescued - broken,
        },
        "where_plain_fails": {
            "n": len(failed), "rescued": rescued,
            "rescue_pct": round(100.0 * rescued / len(failed), 1) if failed else None,
            "ci95": [round(rescue_lo, 1), round(rescue_hi, 1)],
        },
        "where_plain_already_works": {
            "n": len(worked), "broken": broken,
            "break_pct": round(100.0 * broken / len(worked), 1) if worked else None,
            "ci95": [round(break_lo, 1), round(break_hi, 1)],
        },
        "net_if_applied_selectively": rescued,
    }


def render(s):
    fails, works = s["where_plain_fails"], s["where_plain_already_works"]
    everything = s["applied_to_everything"]
    return "\n".join([
        "=== %s (%s, %d paired terms) ===" % (s["artifact"], s["completeness"],
                                              s["paired_terms"]),
        "  applied to everything      %d wins / %d losses, p=%.3g, net %+d words"
        % (everything["wins"], everything["losses"], everything["p_value"],
           everything["net_words"]),
        "",
        "  where plain FAILS  n=%-5d rescued %4d = %4.1f%%  (95%% CI %.1f-%.1f)"
        % (fails["n"], fails["rescued"], fails["rescue_pct"],
           fails["ci95"][0], fails["ci95"][1]),
        "  where plain WORKS  n=%-5d broke   %4d = %4.1f%%  (95%% CI %.1f-%.1f)"
        % (works["n"], works["broken"], works["break_pct"],
           works["ci95"][0], works["ci95"][1]),
        "",
        "  net if applied only where the plain reading fails: %+d words"
        % s["net_if_applied_selectively"],
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("artifacts", nargs="+")
    parser.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "respelling_selectivity.json"))
    args = parser.parse_args()
    results = [analyse(a) for a in args.artifacts]
    for r in results:
        print(render(r))
        print()
    payload = {"comparisons": results, "provenance": provenance(__file__, args)}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
