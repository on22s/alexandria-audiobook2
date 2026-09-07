import json
from pathlib import Path
import tempfile
import unittest

from experiments.alias_correction_rescore import rescore_document
from experiments.scoring import alias_groups


class AliasCorrectionRescoreTests(unittest.TestCase):
    def test_records_changed_score_without_mutating_artifact(self):
        groups = alias_groups({"aliases": [["TSUKIHI", "TSUKIHI ARARAGI"]]})
        document = {"rows": [
            {"id": "one", "arm": "base", "expected": "TSUKIHI",
             "predicted": "TSUKIHI ARARAGI", "correct": False},
            {"id": "two", "arm": "base", "expected": "TSUKIHI",
             "predicted": "KAREN ARARAGI", "correct": False},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "artifact.json")
            path.write_text(json.dumps(document), encoding="utf-8")
            result = rescore_document(path, groups, "TSUKIHI")
            self.assertEqual(1, result["changed_row_evaluations"])
            self.assertEqual(1, result["arms"]["base"]["new_correct"])
            self.assertEqual(document, json.loads(path.read_text(encoding="utf-8")))

    def test_unrelated_rows_produce_no_ledger_entry(self):
        groups = alias_groups({"aliases": [["TSUKIHI", "TSUKIHI ARARAGI"]]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "artifact.json")
            path.write_text(json.dumps({"rows": [{"expected": "KAREN"}]}),
                            encoding="utf-8")
            self.assertIsNone(rescore_document(path, groups, "TSUKIHI"))


if __name__ == "__main__":
    unittest.main()
