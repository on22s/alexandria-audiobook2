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


class MembershipRecheckTest(unittest.TestCase):
    """Whether a recorded `in_candidates` is RIGHT, not merely present.

    Rule 21: the recheck is itself an instrument, so it is pinned on cases
    whose answer is known - including the two it must NOT flag. A recheck that
    always finds fault would condemn 168 sound artifacts, and one that never
    does would have missed the 48.
    """

    def _fixtures(self, aliases):
        import json
        import tempfile
        directory = tempfile.mkdtemp()
        with open(os.path.join(directory, "attribution_gold_bk.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"entries": [], "aliases": aliases}, handle)
        return directory

    def _doc(self, rows):
        return {"meta": {"gold_files": {"bk": "sha"}}, "rows": rows}

    def test_it_finds_the_gaen_shape(self):
        # Roster shows the full name, gold asks for the short one, and the
        # evaluator recorded the answer as absent.
        fixtures = self._fixtures([["GAEN", "IZUKO GAEN"]])
        doc = self._doc([{"id": "bk:1", "expected": "GAEN", "correct": False,
                          "in_candidates": False,
                          "candidates": ["IZUKO GAEN", "HANEKAWA"]}])
        result = AUDIT.recheck_membership(doc, fixtures)
        self.assertEqual(1, result["rows_wrongly_unavailable"])
        self.assertEqual(0, result["available_recorded"])
        self.assertEqual(1, result["available_alias_aware"])

    def test_a_correct_artifact_is_not_flagged(self):
        # THE CASE IT MUST NOT FLAG.
        fixtures = self._fixtures([["GAEN", "IZUKO GAEN"]])
        doc = self._doc([{"id": "bk:1", "expected": "GAEN", "correct": True,
                          "in_candidates": True,
                          "candidates": ["GAEN", "HANEKAWA"]}])
        self.assertEqual(
            0, AUDIT.recheck_membership(doc, fixtures)["rows_wrongly_unavailable"])

    def test_a_genuinely_absent_speaker_stays_absent(self):
        # ALSO MUST NOT FLAG: the answer really was not offered, and the fix
        # must not manufacture availability out of an alias nobody was shown.
        fixtures = self._fixtures([["GAEN", "IZUKO GAEN"]])
        doc = self._doc([{"id": "bk:1", "expected": "SENGOKU", "correct": False,
                          "in_candidates": False,
                          "candidates": ["IZUKO GAEN", "HANEKAWA"]}])
        result = AUDIT.recheck_membership(doc, fixtures)
        self.assertEqual(0, result["rows_wrongly_unavailable"])
        self.assertEqual(0, result["available_alias_aware"])

    def test_an_unavailable_gold_is_none_not_agreement(self):
        # Unknown must stay unknown - reporting "nothing wrong here" for an
        # artifact nobody could check is the failure this audit exists to stop.
        doc = self._doc([{"id": "bk:1", "expected": "GAEN", "correct": False,
                          "in_candidates": False, "candidates": ["IZUKO GAEN"]}])
        import tempfile
        self.assertIsNone(AUDIT.recheck_membership(doc, tempfile.mkdtemp()))

    def test_rows_without_a_candidate_list_are_not_checkable(self):
        doc = self._doc([{"id": "bk:1", "expected": "GAEN", "correct": False,
                          "in_candidates": None, "candidates": None}])
        self.assertIsNone(
            AUDIT.recheck_membership(doc, self._fixtures([["GAEN", "IZUKO GAEN"]])))

    def test_conditional_accuracy_is_reported_both_ways(self):
        # The metric that actually moved. Excluding a row the model got wrong
        # flatters the conditional figure; the recheck must show both numbers
        # rather than silently replacing one.
        fixtures = self._fixtures([["GAEN", "IZUKO GAEN"]])
        doc = self._doc([
            {"id": "bk:1", "expected": "HANEKAWA", "correct": True,
             "in_candidates": True, "candidates": ["HANEKAWA", "IZUKO GAEN"]},
            {"id": "bk:2", "expected": "GAEN", "correct": False,
             "in_candidates": False, "candidates": ["HANEKAWA", "IZUKO GAEN"]},
        ])
        result = AUDIT.recheck_membership(doc, fixtures)
        self.assertEqual(100.0, result["conditional_recorded"])
        self.assertEqual(50.0, result["conditional_alias_aware"])


if __name__ == "__main__":
    unittest.main()
