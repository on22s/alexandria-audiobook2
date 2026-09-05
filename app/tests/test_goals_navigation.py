"""GOALS.md is split by status, and both halves of that claim are checked.

A document that sorts goals by status rots the first time one changes status,
and the failure does not announce itself: the file still renders, and a goal
that quietly moved from OPEN to MET sits in the wrong half being read as work
that is left. So the split is recomputed here rather than trusted.

THE LINE NUMBER IS GONE ON PURPOSE. The navigation note used to name the line
Part II starts on, and this file checked it, so it was never WRONG. It was
still a derived value stored in a hand-edited document, and it conflicted on
every pull request that added a paragraph above Part II - three times on
2026-09-04 alone. Both sides of such a conflict are always wrong: two branches
that each grow the open half by a different amount produce two different
numbers, and the number after merging is neither. GOALS.md carries prose, so
it cannot take the `merge=ours` driver the other derived files use. Removing
the number is the only fix that ends it, and a test now keeps it from coming
back.

Run this module directly to print where Part II starts.
"""
import re
import unittest
from pathlib import Path

GOALS = Path(__file__).resolve().parent.parent.parent / "GOALS.md"
MET_HEADING = "# Part II — Met"
OPEN_HEADING = "# Part I — Open"


def read_goals():
    return GOALS.read_text(encoding="utf-8").split("\n")


def goals_by_part(lines):
    """-> {"open": [(id, [body])], "met": [...]} split at the Part II heading."""
    parts = {"open": [], "met": []}
    where = None
    current = None
    for line in lines:
        if line.strip() == OPEN_HEADING:
            where, current = "open", None
            continue
        if line.strip() == MET_HEADING:
            where, current = "met", None
            continue
        match = re.match(r"^### (\d+\.\d+) ", line)
        if match and where:
            current = (match.group(1), [])
            parts[where].append(current)
            continue
        if re.match(r"^#{1,2} ", line):
            current = None
            continue
        if current is not None:
            current[1].append(line)
    return parts


def verdict(body):
    """MET only when the goal says so and does not also say OPEN.

    Same rule the reorganisation used. A goal claiming both - "MET on jitter,
    OPEN on tract length" - is unfinished, and belongs with the work.
    """
    marked = " ".join(line for line in body
                      if not line.startswith(">")
                      and re.search(r"\b(MET|OPEN|NO BASELINE)\b", line))
    if not marked.strip():
        return "OPEN"
    return "MET" if ("MET" in marked and "OPEN" not in marked) else "OPEN"


class GoalsNavigationTests(unittest.TestCase):

    def test_the_document_names_no_line_number(self):
        """Keeps the conflict generator from being reintroduced.

        Checking a hardcoded number is not enough - the old test did exactly
        that and passed, while the number still had to be resolved by hand on
        every merge. The only stable state is not storing it.
        """
        offenders = [f"line {i + 1}: {line.strip()[:70]}"
                     for i, line in enumerate(read_goals())
                     if re.search(r"(begin|start|found|is)\w*\s+at\s+line\s+"
                                  r"\*{0,2}\d+", line, re.I)]
        self.assertEqual([], offenders,
                         "a line number is stored in GOALS.md again. It will "
                         "conflict on every PR that edits above it, and both "
                         "sides of that conflict will be wrong:\n  "
                         + "\n  ".join(offenders))

    def test_both_parts_are_present_and_findable(self):
        """What the pointer was for: the split must exist and be locatable."""
        lines = read_goals()
        self.assertIn(OPEN_HEADING, lines)
        self.assertIn(MET_HEADING, lines)
        self.assertLess(lines.index(OPEN_HEADING), lines.index(MET_HEADING),
                        "open goals come first")

    def test_every_goal_sits_in_the_half_its_status_says(self):
        parts = goals_by_part(read_goals())
        self.assertTrue(parts["open"] and parts["met"], "both parts must exist")
        misfiled = []
        for identifier, body in parts["open"]:
            if verdict(body) == "MET":
                misfiled.append(f"{identifier} is MET but sits under {OPEN_HEADING}")
        for identifier, body in parts["met"]:
            if verdict(body) != "MET":
                misfiled.append(f"{identifier} is not MET but sits under {MET_HEADING}")
        self.assertEqual([], misfiled,
                         "a goal changed status and was not moved:\n  "
                         + "\n  ".join(misfiled))

    def test_no_goal_is_listed_twice_or_lost(self):
        parts = goals_by_part(read_goals())
        found = [i for i, _ in parts["open"]] + [i for i, _ in parts["met"]]
        self.assertEqual(len(found), len(set(found)),
                         "a goal appears in both parts")
        every = re.findall(r"^### (\d+\.\d+) ", "\n".join(read_goals()), re.M)
        self.assertEqual(sorted(every), sorted(found),
                         "a goal is outside both parts and would be invisible "
                         "to the split")


def _report():
    lines = read_goals()
    print(f"{MET_HEADING} starts at line {lines.index(MET_HEADING) + 1} "
          f"of GOALS.md ({len(lines)} lines).")
    parts = goals_by_part(lines)
    print(f"  open: {len(parts['open'])} goals   met: {len(parts['met'])} goals")


if __name__ == "__main__":
    _report()
    unittest.main()
