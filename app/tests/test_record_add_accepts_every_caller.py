"""Every record.add(...) call in app/experiments must match ExperimentRecord.add.

WHAT THIS COST. PR #671 folded the probe worktrees' lora_serving_eval into main.
That harness passes `reasoning=` to ExperimentRecord.add; the probe copy of
manifest.py had grown the parameter, main's had not. Every serving cell run from
main therefore died on its first scored row:

    TypeError: ExperimentRecord.add() got an unexpected keyword argument 'reasoning'

It surfaced 2026-09-26 as a 28-book goal-1.3 run that failed after 17 s and left
the card idle all night. No test ran the harness end to end, and the harness's
own tests inspect source, so nothing noticed the call and the signature had
drifted apart. This checks the pair directly, for every caller.
"""
import ast
import inspect
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.manifest import ExperimentRecord  # noqa: E402

EXPERIMENTS = os.path.join(REPO, "app", "experiments")


def record_add_keywords():
    """(file:line, keyword) for every keyword passed to a `record.add(...)` call."""
    found = []
    for name in sorted(os.listdir(EXPERIMENTS)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(EXPERIMENTS, name)
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "record"):
                found += [(f"{name}:{node.lineno}", kw.arg) for kw in node.keywords if kw.arg]
    return found


class RecordAddAcceptsEveryCaller(unittest.TestCase):

    def test_every_keyword_a_caller_passes_exists_on_add(self):
        accepted = set(inspect.signature(ExperimentRecord.add).parameters)
        calls = record_add_keywords()
        self.assertTrue(calls, "no record.add keyword calls found; this test has "
                               "stopped discriminating")
        unknown = [f"{where} {kw}=" for where, kw in calls if kw not in accepted]
        self.assertEqual([], unknown,
                         "these record.add calls pass a keyword ExperimentRecord.add "
                         "does not accept; the run will crash on its first row")

    def test_the_serving_harness_passes_reasoning(self):
        """The case that broke: keep it pinned so the check above stays meaningful."""
        self.assertIn("reasoning", {kw for where, kw in record_add_keywords()
                                    if where.startswith("lora_serving_eval.py")})

    def test_reasoning_is_stored_on_the_row(self):
        record = ExperimentRecord.__new__(ExperimentRecord)
        record.rows = []
        record.add("lora", "b:1", "line", "EMMA", "EMMA", True, reasoning="because")
        self.assertEqual("because", record.rows[0]["reasoning"])
        record.add("base", "b:1", "line", "EMMA", None, False)
        self.assertIsNone(record.rows[1]["reasoning"])


if __name__ == "__main__":
    unittest.main()
