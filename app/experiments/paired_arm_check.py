"""Did both arms see the same prompt, and does the result survive the ones that didn't?

WHAT PROMPTED THIS. On 2026-09-07 a Qwen3-14B ladder adapter was reported as
+9.05 points over 221 rows at p=0.0225, with a caveat attached: seven mushoku16
rows had "followed different retry prompts". A paired test assumes both arms
answered the SAME question, so seven rows where they did not are seven rows the
test was not entitled to use - and nothing in the repo could say whether they
mattered. The answer took ten minutes and was worth having:

    pooled, as reported              221 rows  +9.05  p=0.0225
    matched prompts only             214 rows  +8.88  p=0.0248
    the seven dropped rows alone       7 rows +14.29  p=1.0

The caveat was not load-bearing, and saying so is worth more than repeating it.
The same question will be asked of the next paired result, which is why this is
a function and not a scratch script.

WHY prompt_sha256 AND NOT THE PROMPT. Rows carry the hash, not the text
(test_batch_identity_and_selection covers that schema). Two rows agree when
their hashes agree; when a row has no hash at all this reports it as UNKNOWN
rather than as agreement - a NULL == NULL comparison reporting "same prompt" is
the exact bug that made prompt_sha256 useless for its first months, and a check
that inherits it would be worse than no check.

WHAT IT DOES NOT DO. It does not decide whether two runs may be pooled. That
needs the model, adapter, decoding and environment to match, which the artifact
meta records and a reader must weigh. This answers the narrower question of
whether the PAIRING inside a comparison is sound.

The statistics come from experiments.stats. Rule 15: there are already three
hand-rolled McNemars in this tree and this is not the fourth.
"""

import collections
import json
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.stats import exact_mcnemar          # noqa: E402

UNKNOWN = "unknown-prompt"


def arm_rows(doc, arm):
    """-> {row id: row} for one arm of a loaded artifact."""
    return {r["id"]: r for r in doc.get("rows", []) if r.get("arm") == arm}


def prompt_disagreements(base, tuned):
    """-> (mismatched ids, unknown ids) across the ids the two arms share.

    Separated deliberately. A row whose two arms hashed DIFFERENT prompts is a
    known break in the pairing. A row where either side recorded no hash is a
    row that cannot be checked, which is a different claim and must not be
    quietly counted as agreement.
    """
    mismatched, unknown = [], []
    for row_id in sorted(set(base) & set(tuned)):
        one = base[row_id].get("prompt_sha256")
        two = tuned[row_id].get("prompt_sha256")
        if one is None or two is None:
            unknown.append(row_id)
        elif one != two:
            mismatched.append(row_id)
    return mismatched, unknown


def compare(base, tuned, ids=None):
    """-> dict of the paired comparison over `ids` (default: shared ids)."""
    keys = sorted(set(base) & set(tuned)) if ids is None else sorted(ids)
    if not keys:
        return {"n": 0, "base": None, "tuned": None, "delta": None,
                "repaired": 0, "broken": 0, "p": 1.0}
    base_right = sum(1 for k in keys if base[k].get("correct"))
    tuned_right = sum(1 for k in keys if tuned[k].get("correct"))
    repaired = sum(1 for k in keys
                   if tuned[k].get("correct") and not base[k].get("correct"))
    broken = sum(1 for k in keys
                 if base[k].get("correct") and not tuned[k].get("correct"))
    # stats.exact_mcnemar takes (b, c) and returns (p, b, c).
    p = exact_mcnemar(broken, repaired)[0]
    return {"n": len(keys),
            "base": base_right / len(keys) * 100,
            "tuned": tuned_right / len(keys) * 100,
            "delta": (tuned_right - base_right) / len(keys) * 100,
            "repaired": repaired, "broken": broken, "p": p}


def check(docs, arms=("base", "tuned")):
    """-> the full report: per book, pooled, and pooled minus broken pairs."""
    pooled_base, pooled_tuned, books = {}, {}, {}
    mismatched, unknown = set(), set()
    for doc in docs:
        name = (list((doc.get("meta", {}).get("gold_files") or {}))
                or [doc.get("meta", {}).get("gold_path", "?")])[0]
        base, tuned = arm_rows(doc, arms[0]), arm_rows(doc, arms[1])
        bad, none = prompt_disagreements(base, tuned)
        books[name] = compare(base, tuned)
        for row_id in set(base) & set(tuned):
            pooled_base[(name, row_id)] = base[row_id]
            pooled_tuned[(name, row_id)] = tuned[row_id]
        mismatched |= {(name, i) for i in bad}
        unknown |= {(name, i) for i in none}

    shared = set(pooled_base) & set(pooled_tuned)
    return {
        "books": books,
        "pooled": compare(pooled_base, pooled_tuned),
        "mismatched_prompts": sorted(mismatched),
        "unknown_prompts": sorted(unknown),
        "pooled_matched_only": compare(pooled_base, pooled_tuned,
                                       shared - mismatched - unknown),
        # The excluded rows on their own. A caveat that is only ever removed is
        # a caveat nobody can weigh; this says which way it pointed.
        "excluded_rows_alone": compare(pooled_base, pooled_tuned,
                                       mismatched | unknown),
    }


def _line(label, r):
    if not r["n"]:
        return "  %-34s (no rows)" % label
    return ("  %-34s n=%3d  base %5.1f%%  tuned %5.1f%%  delta %+5.2f  "
            "repaired %2d  broken %2d  p=%.4f"
            % (label, r["n"], r["base"], r["tuned"], r["delta"],
               r["repaired"], r["broken"], r["p"]))


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+")
    parser.add_argument("--arms", nargs=2, default=["base", "tuned"])
    parser.add_argument("--out")
    args = parser.parse_args()

    docs = []
    for path in args.artifacts:
        with open(path, encoding="utf-8") as handle:
            docs.append(json.load(handle))
    report = check(docs, tuple(args.arms))

    print("=== per book ===")
    for name, result in sorted(report["books"].items()):
        print(_line(name, result))
    print("\n=== pooled ===")
    print(_line("all books", report["pooled"]))
    bad, none = report["mismatched_prompts"], report["unknown_prompts"]
    print("\n=== pairing integrity ===")
    print("  rows whose arms saw different prompts: %d" % len(bad))
    print("  rows that cannot be checked (no hash): %d" % len(none))
    if bad or none:
        print(_line("pooled, sound pairs only", report["pooled_matched_only"]))
        print(_line("the excluded rows alone", report["excluded_rows_alone"]))

    if args.out:
        from utils import atomic_json_write
        from experiments.provenance import provenance
        report["provenance"] = provenance(__file__, args)
        atomic_json_write(report, args.out)
        print("\nwrote " + args.out)


if __name__ == "__main__":
    main()
