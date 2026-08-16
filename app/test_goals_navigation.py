"""GOALS.md is split by status, and both halves of that claim are checked.

A pointer to a line number rots the first time anyone edits above it, and a
document that sorts goals by status rots the first time one changes status.
Neither failure announces itself: the file still renders, the number still
looks like a number, and a goal that quietly moved from OPEN to MET sits in
the wrong half being read as work that is left.

So the navigation note is not trusted. It is recomputed here.
"""
import re
import unittest
from pathlib import Path

GOALS = Path(__file__).resolve().parent.parent / "GOALS.md"
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

    def test_the_stated_line_number_is_where_met_goals_actually_start(self):
        lines = read_goals()
        actual = lines.index(MET_HEADING) + 1
        stated = [int(m.group(1)) for m in
                  (re.search(r"met goals begin at line \*{0,2}(\d+)", line)
                   for line in lines) if m]
        self.assertTrue(stated, "the navigation note is missing its line number")
        self.assertEqual([actual], stated,
                         f"the note points at line {stated}, but "
                         f"'{MET_HEADING}' is on line {actual}. Update the note.")

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


if __name__ == "__main__":
    unittest.main()
