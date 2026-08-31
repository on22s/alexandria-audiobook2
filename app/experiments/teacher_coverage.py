"""Every routed row reached the teacher, or its loss was written down.

THE INVARIANT:

    routed disagreements == teacher rows + explicitly recorded failures

It fails today by 195 rows. Measured 2026-08-31 across the eight routed
light novels:

    reborn02        131 of 573 lost   23% of the book, 93 separate runs
    nightingale11    39 of 782 lost   two windows
    sabikui05        17 of 643 lost
    otherside07       8 of 795 lost   one window

All 195 are GENUINE DISAGREEMENTS - not filtered, not an empty cheap arm, not
the two cheap arms agreeing. The teacher simply returned nothing and the
pipeline recorded nothing, so the corpus is quietly 4% short and no artifact
says so. reborn02's losses are scattered across 93 runs spanning the whole
book rather than clustered, which is a different failure from the others and
is the one to look at first.

WHY REFUSING IS THE RIGHT RESPONSE. A 23% silent loss on one book does not
announce itself in a trained adapter: it produces a believable model that is
worse than it should be, for a reason nobody can see afterwards. The cost of
stopping is minutes; the cost of not stopping is an adapter whose deficit is
indistinguishable from a bad recipe.

HOW TO SATISFY IT WITHOUT WEAKENING IT. Write the losses down. A sidecar
`routed__<book>.failures.json` beside the routing artifact, listing the
segment indices that failed and why, converts an unexplained gap into a
recorded one and the invariant passes. That is the point: the requirement is
not that nothing is ever lost, it is that nothing is lost SILENTLY.
"""
import glob
import json
import os


class CoverageError(RuntimeError):
    """Routed rows are missing from the corpus and their loss is unrecorded."""


def get_routed_counts(routed_dir):
    """-> {book: number of rows routed to the teacher}."""
    out = {}
    for path in sorted(glob.glob(os.path.join(routed_dir, "routed__*.json"))):
        if path.endswith(".failures.json"):
            continue
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        out[doc["book"]] = len(doc.get("routed") or [])
    return out


def get_recorded_failures(routed_dir):
    """-> {book: number of losses the producer wrote down}.

    A book with no sidecar has recorded nothing, which is different from
    having recorded zero: both give 0 here, and the invariant treats them the
    same, because an unwritten failure and an unhappened one are
    indistinguishable to a later reader. That is the whole problem.
    """
    out = {}
    for path in sorted(glob.glob(os.path.join(routed_dir, "routed__*.failures.json"))):
        book = os.path.basename(path)[len("routed__"):-len(".failures.json")]
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        entries = doc.get("failures") if isinstance(doc, dict) else doc
        out[book] = len(entries or [])
    return out


def get_teacher_counts(paths):
    """-> {book: rows present}, counting the rows themselves, not filenames."""
    out = {}
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    book = json.loads(line).get("book")
                except ValueError:
                    continue
                if book:
                    out[book] = out.get(book, 0) + 1
    return out


def get_coverage_report(routed_dir, teacher_paths):
    """-> [{book, routed, teacher_rows, recorded_failures, unexplained}].

    Only books that were ROUTED are reported: a corpus may legitimately carry
    other sources (PDNC replay, older light novels) that this directory says
    nothing about, and calling those a coverage failure would make the check
    fire on every mixture.
    """
    routed = get_routed_counts(routed_dir)
    failures = get_recorded_failures(routed_dir)
    present = get_teacher_counts(teacher_paths)
    report = []
    for book in sorted(routed):
        rows = present.get(book, 0)
        recorded = failures.get(book, 0)
        report.append({
            "book": book,
            "routed": routed[book],
            "teacher_rows": rows,
            "recorded_failures": recorded,
            "unexplained": routed[book] - rows - recorded,
        })
    return report


def format_report(report):
    lines = [f"{'book':16} {'routed':>7} {'rows':>7} {'recorded':>9} {'unexplained':>12}"]
    for r in report:
        lines.append(f"{r['book']:16} {r['routed']:7} {r['teacher_rows']:7} "
                     f"{r['recorded_failures']:9} {r['unexplained']:12}")
    return "\n".join(lines)


def assert_coverage(routed_dir, teacher_paths, allow_unexplained=False):
    """Raise unless every routed row is present or its loss is recorded.

    `allow_unexplained` exists for the case where a corpus is deliberately a
    subset. It does not silence the report - the caller still gets it - it only
    stops the refusal, and anything using it should say why in its own
    provenance.
    """
    report = get_coverage_report(routed_dir, teacher_paths)
    gaps = [r for r in report if r["unexplained"] > 0]
    if not gaps or allow_unexplained:
        return report
    total = sum(r["unexplained"] for r in gaps)
    raise CoverageError(
        f"{total} routed rows are missing from the corpus and nothing records "
        f"why. Training on a corpus with silent losses produces an adapter "
        f"whose deficit cannot be diagnosed afterwards.\n"
        + format_report(report)
        + "\n\nEither rebuild the missing rows, or write them down in "
          "routed__<book>.failures.json beside the routing artifact so the "
          "loss is explained rather than invisible.")
