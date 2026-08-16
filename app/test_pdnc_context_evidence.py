import json
import os
import tempfile
import unittest

from experiments.pdnc_context_evidence import (
    CONFIRMATORY_BOOKS, PILOT_BOOKS, add_context_evidence_guidance,
    get_pilot_decision, isolate_failed_attribution, require_passing_pilot,
    summarize_paired_rows)
from three_pass_generate import PassExhausted


def rows(outcomes):
    result = []
    for index, (baseline, evidence) in enumerate(outcomes):
        for arm, correct in (("baseline", baseline), ("evidence", evidence)):
            result.append({"arm": arm, "id": str(index), "correct": correct})
    return result


class PdncContextEvidenceTests(unittest.TestCase):
    def test_book_split_is_frozen_and_disjoint(self):
        self.assertEqual(5, len(PILOT_BOOKS))
        self.assertEqual(20, len(CONFIRMATORY_BOOKS))
        self.assertFalse(set(PILOT_BOOKS) & set(CONFIRMATORY_BOOKS))

    def test_prompt_rejects_proximity_as_attribution(self):
        original = "BASE"
        guided = add_context_evidence_guidance(original)
        self.assertTrue(guided.startswith(original))
        self.assertIn("proximity alone", guided)
        self.assertIn("speech attribution", guided)

    def test_summary_uses_only_paired_rows(self):
        sample = rows([(True, True), (True, False), (False, True)])
        sample.append({"arm": "evidence", "id": "unpaired", "correct": True})
        self.assertEqual({"n": 3, "baseline_correct": 2,
                          "evidence_correct": 2, "delta_points": 0.0,
                          "gained": 1, "lost": 1, "p_value": 1.0},
                         summarize_paired_rows(sample))

    def test_pilot_gate_requires_effect_size_and_significance(self):
        passing = rows([(False, True)] * 20 + [(True, True)] * 80)
        decision = get_pilot_decision(passing)
        self.assertTrue(decision["advance"])
        self.assertEqual(20.0, decision["delta_points"])
        no_effect = get_pilot_decision(rows([(True, True)] * 100))
        self.assertFalse(no_effect["advance"])

    def test_confirmatory_requires_explicit_passing_pilot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pilot.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"meta": {"phase": "pilot",
                                     "validation": "ok",
                                     "decision": {"advance": False}}}, handle)
            with self.assertRaisesRegex(ValueError, "did not pass"):
                require_passing_pilot(path)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"meta": {"phase": "pilot",
                                     "validation": "ok",
                                     "decision": {"advance": True}}}, handle)
            self.assertTrue(require_passing_pilot(path)["advance"])

    def test_quality_failure_isolates_only_the_irrecoverable_row(self):
        calls = []

        def attribute(batch, contexts):
            calls.append([entry["text"] for entry in batch])
            if any(entry["text"] == "bad" for entry in batch):
                raise PassExhausted("quality")
            return [{"text": entry["text"], "speaker": "GOOD"}
                    for entry in batch]

        frozen = [{"text": text} for text in ("one", "bad", "three", "four")]
        output, failed = isolate_failed_attribution(
            attribute, frozen, [{}, {}, {}, {}])
        self.assertEqual({1}, failed)
        self.assertEqual(["GOOD", "UNKNOWN", "GOOD", "GOOD"],
                         [entry["speaker"] for entry in output])
        self.assertIn(["bad"], calls)

    def test_non_quality_exception_is_not_split(self):
        def unavailable(batch, contexts):
            raise RuntimeError("endpoint unavailable")

        with self.assertRaisesRegex(RuntimeError, "endpoint unavailable"):
            isolate_failed_attribution(
                unavailable, [{"text": "one"}, {"text": "two"}], [{}, {}])


if __name__ == "__main__":
    unittest.main()
