import importlib.util
import os
import unittest


APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(APP, "experiments", "audit_candidate_membership.py")
SPEC = importlib.util.spec_from_file_location("audit_candidate_membership", PATH)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class CandidateMembershipAuditTest(unittest.TestCase):
    def test_null_is_instrumentation_missing_not_zero_available(self):
        rows = [{"in_candidates": None}, {"in_candidates": None}]
        self.assertEqual("all_null", AUDIT.classify_rows(rows))

    def test_false_values_are_populated_measurements(self):
        rows = [{"in_candidates": False}, {"in_candidates": False}]
        self.assertEqual("populated", AUDIT.classify_rows(rows))

    def test_mixed_rows_are_not_reported_as_fully_instrumented(self):
        rows = [{"in_candidates": True}, {"in_candidates": None}]
        self.assertEqual("mixed", AUDIT.classify_rows(rows))

    def test_old_rows_without_the_field_are_distinct_from_null(self):
        self.assertEqual("field_absent", AUDIT.classify_rows([{}, {}]))


if __name__ == "__main__":
    unittest.main()
