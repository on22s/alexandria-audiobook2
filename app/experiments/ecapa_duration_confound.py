"""Is our speaker-similarity number partly a statement about clip length?

The VoxSRC retrospective (TASLP 2024) reports that speaker verification gets
steadily better with utterance duration - their 2023 Track 2 winner reaches
0.03% EER on pairs where both utterances exceed 8 seconds. Duration is a
first-order factor in how reliable a speaker embedding is.

Our similarity goals are scored with ECAPA over clips whose lengths we do not
control, so the question is whether goal 2.1's headline is measuring voices or
measuring seconds. This answers it from artifacts already on disk: no GPU, no
synthesis, no new scoring.

Three series per language, all against the same clip durations:

    clone            the synthetic arm's ECAPA
    human_vs_human   the ceiling, one human against another
    ratio            clone / ceiling, which is what the goal reports

If the ratio tracks duration, the goal is confounded. If only the raw series
do, the ratio is doing its job and RAW ECAPA IS THE NUMBER THAT MUST NOT BE
COMPARED across arms whose clips differ in length.
"""
import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

from experiments.provenance import provenance  # noqa: E402

SERIES = ("clone", "human_vs_human")


def spearman(xs, ys):
    """Rank correlation, written out so this runs without scipy."""
    n = len(xs)
    if n < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None

    def ranks(values):
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            mean_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = mean_rank
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return None if not dx or not dy else num / (dx * dy)


def quartiles(pairs):
    """-> mean of the metric in each duration quartile, shortest clips first."""
    if len(pairs) < 8:
        return None
    ordered = sorted(pairs)
    size = len(ordered) // 4
    values = [b for _, b in ordered]
    return [round(sum(values[i * size:(i + 1) * size]) / size, 4) for i in range(4)]


def series_rows(doc, series):
    """-> [(seconds, metric)] for one series, skipping rows missing either."""
    out = []
    for row in doc.get("rows") or []:
        seconds = row.get("human_seconds")
        block = row.get(series)
        if not seconds or not isinstance(block, dict):
            continue
        value = block.get("ecapa")
        if value is not None:
            out.append((seconds, value))
    return out


def ratio_rows(doc):
    """-> [(seconds, clone/ceiling)], the quantity the goal actually reports."""
    out = []
    for row in doc.get("rows") or []:
        seconds = row.get("human_seconds")
        clone = (row.get("clone") or {}).get("ecapa")
        ceiling = (row.get("human_vs_human") or {}).get("ecapa")
        if seconds and clone is not None and ceiling:
            out.append((seconds, clone / ceiling))
    return out


def analyse(path):
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    out = {}
    for name in SERIES:
        pairs = series_rows(doc, name)
        if len(pairs) < 8:
            continue
        out[name] = {
            "n": len(pairs),
            "spearman": None if spearman([a for a, _ in pairs],
                                         [b for _, b in pairs]) is None
            else round(spearman([a for a, _ in pairs], [b for _, b in pairs]), 4),
            "by_duration_quartile": quartiles(pairs),
        }
    pairs = ratio_rows(doc)
    if len(pairs) >= 8:
        seconds = sorted(a for a, _ in pairs)
        out["ratio"] = {
            "n": len(pairs),
            "spearman": None if spearman([a for a, _ in pairs],
                                         [b for _, b in pairs]) is None
            else round(spearman([a for a, _ in pairs], [b for _, b in pairs]), 4),
            "by_duration_quartile": quartiles(pairs),
        }
        out["median_seconds"] = round(seconds[len(seconds) // 2], 2)
        out["seconds_range"] = [round(seconds[0], 2), round(seconds[-1], 2)]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scores", nargs="+",
                    default=sorted(glob.glob(os.path.join(
                        REPO, "ab_test_runtime", "experiments",
                        "longref__*_score.json"))),
                    help="score artifacts carrying human_seconds per row")
    ap.add_argument("--flat", type=float, default=0.10,
                    help="|spearman| below this counts as no duration trend")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    languages = {}
    for path in args.scores:
        name = os.path.basename(path)
        key = name.split("__")[1].split("_")[0] if "__" in name else name
        result = analyse(path)
        if result:
            result["artifact"] = name
            languages[key] = result
    if not languages:
        raise SystemExit("no score artifact had rows with human_seconds")

    ratios = [v["ratio"]["spearman"] for v in languages.values()
              if v.get("ratio", {}).get("spearman") is not None]
    raw = [v[s]["spearman"] for v in languages.values() for s in SERIES
           if v.get(s, {}).get("spearman") is not None]
    verdict = (
        "the ratio is flat while the raw series climb: the goal's number is "
        "not a duration artifact, and RAW ecapa must not be compared across "
        "arms whose clips differ in length"
        if ratios and all(abs(r) < args.flat for r in ratios)
        and raw and max(raw) >= args.flat
        else "the ratio tracks duration; the goal's number is confounded")

    doc = {"status": "complete", "provenance": provenance(__file__, args),
           "scope": "ECAPA against clip duration, from stored score artifacts; "
                    "nothing was re-synthesised or re-scored",
           "flat_threshold": args.flat,
           "languages": languages, "verdict": verdict}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)

    print("%-6s %-15s %8s %9s %s" % ("lang", "series", "rho", "median s", "quartiles"))
    for key in sorted(languages):
        entry = languages[key]
        for name in list(SERIES) + ["ratio"]:
            block = entry.get(name)
            if not block:
                continue
            print("%-6s %-15s %8.3f %9s  %s" % (
                key, name, block["spearman"],
                entry.get("median_seconds", ""),
                " ".join("%.3f" % q for q in block["by_duration_quartile"])))
    print("\n%s" % verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
