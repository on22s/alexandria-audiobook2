"""Rescore stored PDNC artifacts against the fixtures' current alias groups.

Every lora_serving_eval artifact stores each row's prediction, so a change to
what counts as the same speaker needs no GPU: re-ask `same_speaker` with the
alias groups the fixtures carry now. Written for the reviewed silent aliases of
2026-09-28 (`pdnc_fixture.REVIEWED_SILENT_ALIASES`: SMALL for the Wooden-Legged
Man and four more), which flipped paired adapter verdicts on the nine-novel
panel.

WHAT IT REPORTS, per artifact and arm: stored vs rescored accuracy, and for each
non-base arm the paired delta against base with an exact sign test under both
scorings, marking every verdict (+, -, 0 at p<0.05) that changes. Every row
whose score moved is tallied by (book, expected, predicted), so a change that
is NOT one of the reviewed pairs is visible instead of being folded in.

Rows scored only when the book's fixture exists in --fixtures; books without
one keep their stored score and are listed as such.
"""
import argparse, collections, glob, json, os, sys
from math import comb

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
from experiments.provenance import provenance  # noqa: E402
from experiments.scoring import alias_groups, same_speaker  # noqa: E402


def sign_p(wins, losses):
    n, k = wins + losses, min(wins, losses)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0


def verdict(delta, p):
    return ("+" if delta > 0 else "-") if p < 0.05 else "0"


def paired(base, arm, key):
    ids = set(base) & set(arm)
    wins = sum(1 for i in ids if arm[i][key] and not base[i][key])
    losses = sum(1 for i in ids if base[i][key] and not arm[i][key])
    return {"n": len(ids), "delta_pts": round(100 * (wins - losses) / len(ids), 2) if ids else None,
            "wins": wins, "losses": losses, "p": sign_p(wins, losses)}


def rescore(path, groups_by_book, moved, missing):
    rows = json.load(open(path)).get("rows") or []
    arms = collections.defaultdict(dict)
    for row in rows:
        book = str(row.get("id", "")).split(":")[0]
        stored = str(row.get("correct")) == "True"
        groups = groups_by_book.get(book)
        if groups is None:
            if book.startswith("pdnc_"):
                missing[book] += 1
            now = stored
        else:
            now = same_speaker(row.get("expected"), row.get("predicted"), groups)
        if now != stored:
            moved[(book, row.get("expected"), str(row.get("predicted")), "up" if now else "down")] += 1
        arms[row.get("arm")][row["id"]] = {"stored": stored, "rescored": now}
    out = {"artifact": os.path.basename(path), "arms": {}}
    for arm, got in arms.items():
        out["arms"][arm] = {"n": len(got),
                            "stored_pct": round(100 * sum(r["stored"] for r in got.values()) / len(got), 2),
                            "rescored_pct": round(100 * sum(r["rescored"] for r in got.values()) / len(got), 2)}
        if arm != "base" and "base" in arms:
            s, r = paired(arms["base"], got, "stored"), paired(arms["base"], got, "rescored")
            out["arms"][arm].update({"paired_stored": s, "paired_rescored": r,
                                     "verdict_changed": verdict(s["delta_pts"] or 0, s["p"]) != verdict(r["delta_pts"] or 0, r["p"])})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifacts", nargs="+", required=True, help="directories searched recursively")
    ap.add_argument("--fixtures", default=os.path.join(APP, "fixtures"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    groups_by_book = {}
    for f in glob.glob(os.path.join(args.fixtures, "attribution_gold_pdnc_*.json")):
        name = os.path.basename(f)[len("attribution_gold_"):-len(".json")]
        if "_w" in name.split("pdnc_", 1)[1]:
            continue  # context-width variants carry the same groups (tested)
        groups_by_book[name] = alias_groups(json.load(open(f, encoding="utf-8")))
    seen = {}
    for d in args.artifacts:
        for f in sorted(glob.glob(os.path.join(d, "**", "lora_serving_eval__*.json"), recursive=True)):
            seen.setdefault(os.path.basename(f), f)  # cloud pulls hold copies
    moved, missing, results = collections.Counter(), collections.Counter(), []
    for name, path in sorted(seen.items()):
        try:
            res = rescore(path, groups_by_book, moved, missing)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            results.append({"artifact": name, "error": repr(exc)})
            continue
        if any(a.get("stored_pct") != a.get("rescored_pct") or a.get("verdict_changed")
               for a in res["arms"].values()):
            results.append(res)
    changed = [(r["artifact"], arm, a["paired_stored"], a["paired_rescored"])
               for r in results for arm, a in r.get("arms", {}).items() if a.get("verdict_changed")]
    summary = {"artifacts_scanned": len(seen), "artifacts_moved": sum("arms" in r for r in results),
               "verdicts_changed": len(changed), "errors": sum("error" in r for r in results),
               "books_without_fixture_rows": dict(missing)}
    json.dump({"summary": summary,
               "moved_rows": [{"book": k[0], "expected": k[1], "predicted": k[2], "direction": k[3], "rows": v}
                              for k, v in moved.most_common()],
               "results": results, "provenance": provenance(__file__, args)},
              open(args.out, "w"), indent=1)
    print(json.dumps(summary))
    for k, v in moved.most_common(15):
        print(f"  {v:5}  {k}")
    for name, arm, s, r in changed:
        print(f"  VERDICT {name[20:100]:80} {arm}: {s['delta_pts']:+.2f} p={s['p']:.2g} -> {r['delta_pts']:+.2f} p={r['p']:.2g}")


if __name__ == "__main__":
    main()
