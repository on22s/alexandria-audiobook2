"""An evaluator must record the roster it showed the model.

`ExperimentRecord.add` takes `candidates`, and the row schema has always had
`candidates` and `in_candidates`. distill_eval and lora_serving_eval never
passed it, so every artifact either wrote them as empty/None - the schema said
the field existed and the data said nothing.

WHAT THAT COST. On 2026-08-30 the adapters were found to refuse, and to refuse
the rows they would have got wrong. Two explanations survive that finding:

    roster defect     the model declines because the gold genuinely is not in
                      the roster it was shown
    learned refusal   the adapter declines regardless

They imply different fixes, and `in_candidates` is exactly the field that
separates them. It was None on all 766 rows of every serving evaluation, so
the question could not be asked of any artifact already on disk - it needed a
re-run, not a re-analysis. grammar_constraint passes candidates and its rows
are populated 556/556, which is why the roster analysis worked there and
nowhere else.

These tests assert the CALL, because the artifact only exists after a GPU run.
The companion check on a written artifact is
test_record_stamps_every_gold.py's approach: build a record and read its rows.
"""
import ast
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.manifest import ExperimentRecord  # noqa: E402

EXPERIMENTS = os.path.join(REPO, "app", "experiments")
# Evaluators that show the model a roster and score its answer against gold.
MUST_RECORD = ("distill_eval.py", "lora_serving_eval.py")
ENV = {"loaded": True, "context_length": 32768, "parallel": 1}


def record_add_calls(path):
    """Every record.add(...) call in one module, as AST nodes."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    calls = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "record"):
            calls.append(node)
    return calls


class EvaluatorsRecordTheRosterTest(unittest.TestCase):

    def test_every_scoring_call_passes_candidates(self):
        offenders = []
        for name in MUST_RECORD:
            path = os.path.join(EXPERIMENTS, name)
            calls = record_add_calls(path)
            self.assertTrue(calls, f"{name}: no record.add calls found; this "
                                   "test has stopped discriminating")
            for call in calls:
                if not any(kw.arg == "candidates" for kw in call.keywords):
                    offenders.append(f"{name}:{call.lineno}")
        self.assertEqual(
            [], offenders,
            "these record.add calls omit candidates, so in_candidates will be "
            "None and a refusal cannot be told from a roster that never held "
            "the answer: " + ", ".join(offenders))

    def test_a_recorded_row_reports_membership_both_ways(self):
        """The field is only useful if it discriminates, so check both."""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            gold = os.path.join(d, "attribution_gold_x.json")
            with open(gold, "w", encoding="utf-8") as fh:
                json.dump({"entries": [{"id": "x-1"}]}, fh)
            rec = ExperimentRecord("t", d, "m", "u", gold,
                                   {"temperature": 0.0}, environment=ENV)
            rec.add("base", "x-1", "line", "ALICE", "ALICE", True,
                    candidates=["ALICE", "BOB"])
            rec.add("base", "x-2", "line", "CAROL", "ALICE", False,
                    candidates=["ALICE", "BOB"])
        present, absent = rec.rows[0], rec.rows[1]
        self.assertTrue(present["in_candidates"],
                        "gold in the roster must read as present")
        self.assertFalse(absent["in_candidates"],
                         "gold outside the roster must read as absent")
        self.assertEqual(["ALICE", "BOB"], present["candidates"])

    def test_an_empty_roster_is_not_silently_a_pass(self):
        """The exact shape of the bug: a row that records no roster at all."""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            gold = os.path.join(d, "attribution_gold_x.json")
            with open(gold, "w", encoding="utf-8") as fh:
                json.dump({"entries": [{"id": "x-1"}]}, fh)
            rec = ExperimentRecord("t", d, "m", "u", gold,
                                   {"temperature": 0.0}, environment=ENV)
            rec.add("base", "x-1", "line", "ALICE", "ALICE", True)
        row = rec.rows[0]
        self.assertFalse(row.get("candidates"),
                         "no roster was passed, so none should be claimed")
        self.assertIsNone(row.get("in_candidates"),
                          "membership is UNKNOWN without a roster and must not "
                          "read as False, which would look like a real absence")


if __name__ == "__main__":
    unittest.main()
