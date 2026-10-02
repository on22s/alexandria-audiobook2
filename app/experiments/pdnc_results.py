"""Validate persisted PDNC paired rows before resuming or reporting a campaign."""
import argparse
import json
from pathlib import Path


def get_expected_entries(fixture, limit):
    if not isinstance(fixture, dict) or not isinstance(fixture.get("book"), str) or not fixture["book"]:
        raise ValueError("fixture must name its book")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    entries = fixture.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("fixture must contain entries")
    selected = entries[:limit]
    expected = {}
    for entry in selected:
        if not isinstance(entry, dict):
            raise ValueError("fixture entry must be an object")
        identifier, speaker = entry.get("id"), entry.get("expected_speaker")
        if not isinstance(identifier, str) or not identifier or not isinstance(speaker, str) or not speaker:
            raise ValueError("fixture entries require nonempty ids and expected speakers")
        if identifier in expected:
            raise ValueError("fixture contains duplicate ids")
        expected[identifier] = speaker
    return expected


def get_pdnc_result(document, fixture, limit):
    """Return row-derived counts only for the complete requested paired sample."""
    expected = get_expected_entries(fixture, limit)
    book = fixture["book"]
    if not isinstance(document, dict) or set(document) != {book}:
        raise ValueError("result must contain exactly the requested book")
    arms = document[book]
    if not isinstance(arms, dict) or set(arms) != {"base", "lora"}:
        raise ValueError("result must contain base and lora arms")
    counts = {}
    for arm in ("base", "lora"):
        bucket = arms[arm]
        if not isinstance(bucket, dict):
            raise ValueError(f"{arm}: arm must be an object")
        rows = bucket.get("rows")
        if not isinstance(rows, list) or len(rows) != len(expected):
            raise ValueError(f"{arm}: rows do not cover the requested sample")
        seen, correct = set(), 0
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{arm}: row must be an object")
            identifier = row.get("id")
            if not isinstance(identifier, str) or identifier not in expected or identifier in seen:
                raise ValueError(f"{arm}: duplicate or unexpected row id")
            if row.get("expected") != expected[identifier]:
                raise ValueError(f"{arm}: row expected speaker differs from fixture")
            if type(row.get("correct")) is not bool:
                raise ValueError(f"{arm}: row correctness must be boolean")
            if "predicted" not in row or (row["predicted"] is not None and not isinstance(row["predicted"], str)):
                raise ValueError(f"{arm}: row prediction must be text or null")
            seen.add(identifier)
            correct += row["correct"]
        if seen != set(expected):
            raise ValueError(f"{arm}: missing requested row ids")
        if type(bucket.get("n")) is not int or bucket["n"] != len(rows):
            raise ValueError(f"{arm}: declared n differs from row count")
        if type(bucket.get("correct")) is not int or bucket["correct"] != correct:
            raise ValueError(f"{arm}: declared correct differs from rows")
        counts[arm] = correct
    return {"book": book, "n": len(expected), **counts}


def get_goal13_results(repo, runtime, heldout, development, limit, result_prefix="goal13"):
    """Validate every book before returning any reportable comparison."""
    if result_prefix not in ("goal13", "goal13mm"):
        raise ValueError("unknown Goal 1.3 result prefix")
    results = []
    for half, stems in (("heldout", heldout), ("development", development)):
        if not stems:
            raise ValueError(f"{half}: no books requested")
        for stem in stems:
            fixture = json.loads((Path(repo) / "app/fixtures" / f"attribution_gold_pdnc_{stem}.json").read_text())
            path = Path(runtime) / "experiments" / f"pdnc_eval__{result_prefix}_{half}_{stem}.json"
            try:
                result = get_pdnc_result(json.loads(path.read_text()), fixture, limit)
            except (OSError, ValueError) as error:
                raise ValueError(f"{half}/{stem}: {error}") from error
            results.append({"half": half, **result})
    return results


def print_goal13_summary(results):
    print("\n%-13s %-24s %7s %8s %8s %7s" % ("half", "book", "n", "base", "lora", "delta"))
    totals = {}
    for half in ("heldout", "development"):
        agg = [0, 0, 0]
        for result in (row for row in results if row["half"] == half):
            n, base, lora = result["n"], result["base"], result["lora"]
            agg[0] += n; agg[1] += base; agg[2] += lora
            print("%-13s %-24s %7d %7.1f%% %7.1f%% %+6.1f" %
                  (half, result["book"], n, 100*base/n, 100*lora/n, 100*(lora-base)/n))
        n, base, lora = agg
        totals[half] = (100*base/n, 100*lora/n, n)
        print("%-13s %-24s %7d %7.1f%% %7.1f%% %+6.1f  <-- pooled" %
              (half, "ALL", n, totals[half][0], totals[half][1], totals[half][1]-totals[half][0]))
    gap = totals["development"][1] - totals["heldout"][1]
    print("\nDEV MINUS HELD-OUT (lora arm): %+.1f points" % gap)
    print("A large positive gap is memorisation; a small one is transfer.")
    print("All five held-out books are Austen: this does not test register transfer.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("fixture", "validate"):
        command = sub.add_parser(action)
        command.add_argument("--fixture", required=True)
        command.add_argument("--limit", type=int, required=True)
        if action == "validate": command.add_argument("--artifact", required=True)
    summary = sub.add_parser("summary")
    summary.add_argument("--repo", required=True)
    summary.add_argument("--runtime", required=True)
    summary.add_argument("--heldout", required=True)
    summary.add_argument("--development", required=True)
    summary.add_argument("--limit", type=int, required=True)
    summary.add_argument("--result-prefix", choices=("goal13", "goal13mm"), default="goal13")
    args = parser.parse_args()
    try:
        if args.action == "summary":
            results = get_goal13_results(args.repo, args.runtime, args.heldout.split(), args.development.split(), args.limit, args.result_prefix)
            print_goal13_summary(results)
        else:
            fixture = json.loads(Path(args.fixture).read_text())
            if args.action == "fixture": get_expected_entries(fixture, args.limit)
            else: get_pdnc_result(json.loads(Path(args.artifact).read_text()), fixture, args.limit)
    except (OSError, ValueError) as error:
        parser.exit(1, f"REFUSING PDNC result: {error}\n")


if __name__ == "__main__":
    main()
